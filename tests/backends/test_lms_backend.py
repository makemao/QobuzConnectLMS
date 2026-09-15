"""Tests for the Lyrion Music Server backend, against a fake LMS (JSON-RPC + CLI events)."""

import asyncio
import urllib.parse
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from qobuz_proxy.backends.lms import backend as lms_module
from qobuz_proxy.backends.lms import LMSBackend
from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

MAC = "00:11:22:33:44:55"


class FakeLMS:
    """Minimal LMS: one player with a playlist, JSON-RPC commands recorded, CLI events."""

    def __init__(self) -> None:
        self.commands: list[list[Any]] = []
        self.status_requests = 0
        self.mode = "stop"
        self.playlist: list[str] = []
        self.cur = 0
        self.time = 0.0
        self.volume = 40
        self.cli_server: asyncio.Server | None = None
        self.cli_clients: list[asyncio.StreamWriter] = []
        self.subscriptions: list[str] = []

    # --- LMS CLI (port 9090) -------------------------------------------------

    async def start_cli(self) -> int:
        self.cli_server = await asyncio.start_server(self._cli_client, "127.0.0.1", 0)
        return int(self.cli_server.sockets[0].getsockname()[1])

    async def _cli_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.cli_clients.append(writer)
        while line := await reader.readline():
            text = line.decode().strip()
            if text.startswith("subscribe "):
                self.subscriptions.append(text)
                writer.write((urllib.parse.quote(text, safe=" ") + "\n").encode())
                await writer.drain()

    async def notify(self, *tokens: str, player: str = MAC) -> None:
        """Push a player notification to subscribed CLI clients."""
        line = " ".join(urllib.parse.quote(t, safe="") for t in (player, *tokens)) + "\n"
        for writer in self.cli_clients:
            writer.write(line.encode())
            await writer.drain()

    async def drop_cli_clients(self) -> None:
        for writer in self.cli_clients:
            writer.close()
        self.cli_clients.clear()

    async def stop_cli(self) -> None:
        await self.drop_cli_clients()
        if self.cli_server:
            self.cli_server.close()
            await self.cli_server.wait_closed()

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
            self.status_requests += 1
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
    monkeypatch.setattr(lms_module, "REARM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(lms_module, "EVENTS_RECONNECT_MIN_SECONDS", 0.1)
    monkeypatch.setattr(lms_module, "APP_VOLUME_GRACE_SECONDS", 0.2)
    fake = FakeLMS()
    app = web.Application()
    app.router.add_post("/jsonrpc.js", fake.handle)
    server = TestServer(app)
    await server.start_server()
    yield fake, server
    await fake.stop_cli()
    await server.close()


async def _connected(
    server: TestServer, player: str = "Living Room", cli_port: int = 0
) -> LMSBackend:
    """Backend on the fake LMS; LMS CLI events are off unless a CLI port is given."""
    backend = LMSBackend(host=server.host, port=server.port, player_id=player, cli_port=cli_port)
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


def _deletes(fake: FakeLMS) -> list[list[Any]]:
    return [c for c in fake.commands if c[:2] == ["playlist", "delete"]]


async def test_cleared_next_track_is_removed_after_grace(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))
    await backend.clear_next_track()
    assert fake.playlist == ["qobuz://1.flac", "qobuz://2.flac"]  # not yet: re-arm window
    await _wait_for(lambda: fake.playlist == ["qobuz://1.flac"])
    assert fake.mode == "play"
    await backend.disconnect()


async def test_rearming_same_track_keeps_the_lms_item(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"), queue_item_id=1)
    # The Qobuz app resends the queue: the player clears and re-arms the same track
    await backend.clear_next_track()
    assert await backend.set_next_track("", _meta("2"), queue_item_id=2)
    await asyncio.sleep(0.4)  # past the removal grace
    assert fake.playlist == ["qobuz://1.flac", "qobuz://2.flac"]
    assert len(_adds(fake)) == 1
    assert not _deletes(fake)
    await backend.disconnect()


async def test_arming_a_different_track_replaces_the_cleared_one(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))
    await backend.clear_next_track()
    assert await backend.set_next_track("", _meta("4"))
    assert fake.playlist == ["qobuz://1.flac", "qobuz://4.flac"]
    # Arming over an armed track also replaces it
    assert await backend.set_next_track("", _meta("5"))
    assert fake.playlist == ["qobuz://1.flac", "qobuz://5.flac"]
    await backend.disconnect()


async def test_lms_reaching_a_cleared_track_hands_back_to_the_player(
    lms, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, server = lms
    monkeypatch.setattr(lms_module, "REARM_GRACE_SECONDS", 30.0)
    backend = await _connected(server)
    ended: list[bool] = []
    started: list[bool] = []
    backend.on_track_ended(lambda: ended.append(True))
    backend.on_next_track_started(lambda: started.append(True))
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))
    await backend.clear_next_track()
    fake.finish_current()  # track 1 ends before the cleared track 2 is removed
    await _wait_for(lambda: ended)
    assert not started  # the queue no longer wants track 2: the player decides what plays
    # The player then plays its real next track, replacing the LMS playlist
    await backend.play("", _meta("7"))
    assert fake.playlist == ["qobuz://7.flac"]
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


# =============================================================================
# LMS CLI events
# =============================================================================


@pytest.fixture
def slow_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling too slow to notice anything during a test: only events can."""
    monkeypatch.setattr(lms_module, "POLL_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(lms_module, "EVENT_POLL_INTERVAL_SECONDS", 30.0)


async def _connected_with_events(fake: FakeLMS, server: TestServer) -> LMSBackend:
    cli_port = await fake.start_cli()
    backend = await _connected(server, cli_port=cli_port)
    await _wait_for(lambda: backend._events_connected and fake.subscriptions)
    return backend


async def test_events_subscribe_to_player_notifications(lms) -> None:
    fake, server = lms
    backend = await _connected_with_events(fake, server)
    assert fake.subscriptions == ["subscribe playlist,pause,mixer,client"]
    await backend.disconnect()


async def test_event_wakes_state_loop_for_gapless_transition(lms, slow_polling) -> None:
    fake, server = lms
    backend = await _connected_with_events(fake, server)
    started: list[bool] = []
    backend.on_next_track_started(lambda: started.append(True))
    await backend.play("", _meta("1"))
    assert await backend.set_next_track("", _meta("2"))

    fake.finish_current()
    await asyncio.sleep(0.3)
    assert not started  # polling alone would take 30 s

    await fake.notify("playlist", "newsong", "Song", "1")
    await _wait_for(lambda: started, timeout=1.0)
    await backend.disconnect()


async def test_event_wakes_state_loop_for_pause_from_lms(lms, slow_polling) -> None:
    fake, server = lms
    backend = await _connected_with_events(fake, server)
    states: list[PlaybackState] = []
    backend.on_state_change(states.append)
    await backend.play("", _meta("1"))
    fake.mode = "pause"  # paused from another LMS controller
    await fake.notify("pause", "1")
    await _wait_for(lambda: PlaybackState.PAUSED in states, timeout=1.0)
    await backend.disconnect()


async def test_events_for_other_players_are_ignored(lms, slow_polling) -> None:
    fake, server = lms
    backend = await _connected_with_events(fake, server)
    await backend.play("", _meta("1"))
    await asyncio.sleep(0.1)
    before = fake.status_requests
    await fake.notify("playlist", "newsong", "Other", "0", player="aa:bb:cc:dd:ee:ff")
    await asyncio.sleep(0.3)
    assert fake.status_requests == before
    await backend.disconnect()


async def test_with_events_status_reads_are_shared_and_position_extrapolated(
    lms, slow_polling
) -> None:
    fake, server = lms
    backend = await _connected_with_events(fake, server)
    await backend.play("", _meta("1"))
    await backend.get_state()
    before = fake.status_requests
    first = await backend.get_position()
    for _ in range(10):  # upstream's player asks twice a second
        await backend.get_state()
        await asyncio.sleep(0.03)
    second = await backend.get_position()
    assert fake.status_requests == before  # served from the event-refreshed cache
    assert second > first  # moving forward between LMS reads
    await backend.disconnect()


async def test_losing_events_falls_back_to_polling_then_reconnects(
    lms, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, server = lms
    monkeypatch.setattr(lms_module, "EVENT_POLL_INTERVAL_SECONDS", 30.0)
    monkeypatch.setattr(lms_module, "EVENTS_RECONNECT_MIN_SECONDS", 0.5)
    backend = await _connected_with_events(fake, server)
    ended: list[bool] = []
    backend.on_track_ended(lambda: ended.append(True))
    await backend.play("", _meta("1"))

    await fake.drop_cli_clients()
    await _wait_for(lambda: not backend._events_connected)
    fake.finish_current()  # no event sent: only fallback polling (0.05 s) can see it
    await _wait_for(lambda: ended, timeout=1.0)

    await _wait_for(lambda: backend._events_connected and len(fake.subscriptions) == 2)
    await backend.disconnect()


# =============================================================================
# Volume changed outside the app
# =============================================================================


async def test_volume_changed_on_lms_is_reported_while_playing(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    changes: list[int] = []
    backend.on_volume_change(changes.append)
    await backend.play("", _meta("1"))
    await asyncio.sleep(0.15)  # first read only records the volume
    assert changes == []
    fake.volume = 46  # changed by another LMS controller (remote, web UI)
    await _wait_for(lambda: changes == [46])
    fake.volume = -46  # muted in LMS
    await _wait_for(lambda: changes == [46, 0])
    await backend.disconnect()


async def test_volume_set_from_the_app_is_not_reported_back(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    changes: list[int] = []
    backend.on_volume_change(changes.append)
    await backend.play("", _meta("1"))
    await asyncio.sleep(0.15)
    await backend.set_volume(55)
    await asyncio.sleep(0.3)
    assert changes == []
    await backend.disconnect()


async def test_mixer_event_reports_volume_while_idle(lms, slow_polling) -> None:
    fake, server = lms
    backend = await _connected_with_events(fake, server)
    changes: list[int] = []
    backend.on_volume_change(changes.append)
    fake.volume = 30
    await fake.notify("mixer", "volume", "30")  # first seen: recorded, not reported
    await asyncio.sleep(0.3)
    before = fake.status_requests
    fake.volume = 36
    await fake.notify("mixer", "volume", "36")
    await _wait_for(lambda: changes == [36], timeout=1.0)
    await fake.notify("playlist", "newsong", "Other", "0")  # idle, not a mixer event
    await asyncio.sleep(0.3)
    assert fake.status_requests == before + 1  # only the mixer event read the status
    await backend.disconnect()


async def test_stale_volume_right_after_an_app_change_is_ignored(lms) -> None:
    fake, server = lms
    backend = await _connected(server)
    changes: list[int] = []
    backend.on_volume_change(changes.append)
    await backend.play("", _meta("1"))
    await asyncio.sleep(0.15)
    await backend.set_volume(55)
    fake.volume = 54  # a read that raced the app change
    await asyncio.sleep(0.1)  # within the grace period
    assert changes == []
    await _wait_for(lambda: changes == [54])  # still a real difference afterwards
    await backend.disconnect()
