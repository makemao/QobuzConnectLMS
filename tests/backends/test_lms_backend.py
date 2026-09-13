"""Tests for the Lyrion Music Server backend, against a fake LMS JSON-RPC server."""

import asyncio
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qobuz_proxy.backends.lms import backend as lms_module
from qobuz_proxy.backends.lms import LMSBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

MAC = "00:11:22:33:44:55"


class FakeLMS:
    """Minimal LMS: one player with a playlist, JSON-RPC commands recorded."""

    def __init__(self) -> None:
        self.commands: list[list[Any]] = []
        self.mode = "stop"
        self.playlist: list[str] = []
        self.cur = 0
        self.time = 0.0
        self.volume = 40

    def replace(self, url: str) -> None:
        """Another LMS controller loads something else on the player."""
        self.playlist, self.cur, self.mode, self.time = [url], 0, "play", 0.0

    def finish_current(self) -> None:
        """The current track ends: LMS chains to the next item, or stops."""
        if self.cur + 1 < len(self.playlist):
            self.cur += 1
            self.time = 0.0
        else:
            self.mode = "stop"

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        player, cmd = body["params"]
        self.commands.append(cmd)
        result: dict[str, Any] = {}
        if cmd[0] == "serverstatus":
            result = {
                "version": "9.1.1",
                "players_loop": [{"playerid": MAC, "name": "Living Room"}],
            }
        elif cmd[0] == "status":
            result = {
                "mode": self.mode,
                "time": self.time,
                "mixer volume": self.volume,
                "playlist_cur_index": str(self.cur),
                "playlist_tracks": len(self.playlist),
            }
            if self.playlist:
                result["playlist_loop"] = [
                    {"playlist index": i, "url": url} for i, url in enumerate(self.playlist)
                ]
        elif cmd[:2] == ["playlist", "play"]:
            self.replace(cmd[2])
        elif cmd[:2] == ["playlist", "add"]:
            self.playlist.append(cmd[2])
        elif cmd[:2] == ["playlist", "delete"]:
            index = int(cmd[2])
            if index == self.cur:
                self.mode = "stop"  # LMS stops when the playing track is removed
            del self.playlist[index]
            if index < self.cur:
                self.cur -= 1
        elif cmd[0] == "pause":
            self.mode = "pause" if cmd[1] == "1" else "play"
        elif cmd[0] == "stop":
            self.mode = "stop"
        elif cmd[0] == "time":
            self.time = float(cmd[1])
        elif cmd[:2] == ["mixer", "volume"]:
            self.volume = int(cmd[2])
        return web.json_response({"id": 1, "method": "slim.request", "result": result})


@pytest.fixture
async def lms(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(lms_module, "POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(lms_module, "PLAYBACK_START_GRACE_PERIOD_SECONDS", 0.0)
    monkeypatch.setattr(lms_module, "STATUS_CACHE_SECONDS", 0.0)
    fake = FakeLMS()
    app = web.Application()
    app.router.add_post("/jsonrpc.js", fake.handle)
    server = TestServer(app)
    await server.start_server()
    yield fake, server
    await server.close()


async def _connected(server: TestServer, player: str = "Living Room") -> LMSBackend:
    backend = LMSBackend(host=server.host, port=server.port, player_id=player)
    assert await backend.connect()
    return backend


def _meta(track_id: str = "12345") -> BackendTrackMetadata:
    return BackendTrackMetadata(track_id=track_id, title="Song")


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_connect_resolves_player_name_to_mac(lms) -> None:
    _, server = lms
    backend = await _connected(server)
    assert backend.get_info().device_id == MAC
    await backend.disconnect()


async def test_connect_fails_for_unknown_player(lms) -> None:
    _, server = lms
    backend = LMSBackend(host=server.host, port=server.port, player_id="Kitchen")
    assert not await backend.connect()


async def test_play_hands_the_qobuz_track_to_lms(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("https://cdn.qobuz.example/ignored", _meta("987"))
    assert ["playlist", "play", "qobuz://987.flac", "Song"] in fake.commands
    assert await backend.get_state() == PlaybackState.PLAYING
    await backend.disconnect()


async def test_natural_end_reports_track_ended(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    ended: list[bool] = []
    backend.on_track_ended(lambda: ended.append(True))
    await backend.play("", _meta())
    fake.mode = "stop"
    await _wait_for(lambda: ended)
    await backend.disconnect()


async def test_takeover_releases_player_without_advancing_queue(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    ended: list[bool] = []
    states: list[PlaybackState] = []
    backend.on_track_ended(lambda: ended.append(True))
    backend.on_state_change(states.append)
    await backend.play("", _meta())
    fake.replace("file:///music/other.flac")  # user picked something else in LMS
    await _wait_for(lambda: PlaybackState.STOPPED in states)
    assert not ended
    # ...and stopping afterwards must not stop the user's playback
    fake.commands.clear()
    await backend.stop()
    assert ["stop"] not in fake.commands
    await backend.disconnect()


async def test_pause_resume_seek_volume(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("", _meta())
    await backend.pause()
    assert fake.mode == "pause"
    assert await backend.resume()
    assert fake.mode == "play"
    await backend.seek(42_500)
    assert await backend.get_position() == 42_500
    await backend.set_volume(130)
    assert fake.volume == 100
    fake.volume = -25  # muted in LMS
    assert await backend.get_volume() == 25
    await backend.disconnect()


# =============================================================================
# Gapless
# =============================================================================


def _adds(fake: FakeLMS) -> list[list[Any]]:
    return [c for c in fake.commands if c[:2] == ["playlist", "add"]]


async def test_set_next_track_queues_it_after_current_once(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    assert backend.supports_gapless
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"), queue_item_id=7)
    assert await backend.set_next_track("", _meta("2"), queue_item_id=7)  # re-arm burst
    assert fake.playlist == ["qobuz://1.flac", "qobuz://2.flac"]
    assert len(_adds(fake)) == 1
    await backend.disconnect()


async def test_gapless_transition_reports_next_track_started(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    started: list[bool] = []
    ended: list[bool] = []
    states: list[PlaybackState] = []
    backend.on_next_track_started(lambda: started.append(True))
    backend.on_track_ended(lambda: ended.append(True))
    backend.on_state_change(states.append)
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))

    fake.finish_current()  # LMS chains to track 2 without stopping
    await _wait_for(lambda: started)

    assert not ended
    assert PlaybackState.STOPPED not in states
    await _wait_for(lambda: fake.playlist == ["qobuz://2.flac"])  # finished track removed
    assert fake.mode == "play"
    assert await backend.get_state() == PlaybackState.PLAYING
    # The following track can be armed after the new current one
    assert await backend.set_next_track("", _meta("3"))
    assert fake.playlist == ["qobuz://2.flac", "qobuz://3.flac"]
    await backend.disconnect()


async def test_last_track_without_next_still_ends_naturally(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    ended: list[bool] = []
    backend.on_track_ended(lambda: ended.append(True))
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))
    fake.finish_current()
    await _wait_for(lambda: fake.playlist == ["qobuz://2.flac"])
    fake.finish_current()  # nothing armed after track 2
    await _wait_for(lambda: ended)
    await backend.disconnect()


async def test_clear_next_track_removes_it_from_lms(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))
    await backend.clear_next_track()
    assert fake.playlist == ["qobuz://1.flac"]
    assert fake.mode == "play"
    # Re-arming a different next track replaces the old one
    assert await backend.set_next_track("", _meta("2"))
    assert await backend.set_next_track("", _meta("4"))
    assert fake.playlist == ["qobuz://1.flac", "qobuz://4.flac"]
    await backend.disconnect()


async def test_clear_after_lms_moved_on_keeps_playing_and_ownership(
    lms, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, server = lms
    # Slow poll: the queue edit lands before the backend notices LMS chained tracks
    monkeypatch.setattr(lms_module, "POLL_INTERVAL_SECONDS", 0.3)
    backend = await _connected(server)
    states: list[PlaybackState] = []
    backend.on_state_change(states.append)
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))

    fake.finish_current()
    await backend.clear_next_track()

    assert fake.mode == "play"
    assert not [c for c in fake.commands if c[:2] == ["playlist", "delete"]]
    assert await backend.get_state() == PlaybackState.PLAYING
    await asyncio.sleep(0.7)
    assert PlaybackState.STOPPED not in states  # not mistaken for a takeover
    await backend.disconnect()


async def test_next_track_not_armed_for_same_track_or_foreign_items(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("", _meta("1"))
    assert not await backend.set_next_track("", _meta("1"))  # repeated track
    fake.playlist.append("file:///music/foreign.flac")
    assert not await backend.set_next_track("", _meta("2"))
    assert fake.playlist == ["qobuz://1.flac", "file:///music/foreign.flac"]
    await backend.disconnect()


async def test_play_discards_armed_next_track(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    started: list[bool] = []
    backend.on_next_track_started(lambda: started.append(True))
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))
    await backend.play("", _meta("9"))  # skip from the Qobuz app
    assert fake.playlist == ["qobuz://9.flac"]
    assert await backend.set_next_track("", _meta("2"))
    assert fake.playlist == ["qobuz://9.flac", "qobuz://2.flac"]
    assert not started
    await backend.disconnect()
