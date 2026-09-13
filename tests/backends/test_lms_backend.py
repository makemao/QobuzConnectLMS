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
    """Minimal LMS: one player, a one-item playlist, JSON-RPC commands recorded."""

    def __init__(self) -> None:
        self.commands: list[list[Any]] = []
        self.mode = "stop"
        self.url: str | None = None
        self.time = 0.0
        self.volume = 40

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
            result = {"mode": self.mode, "time": self.time, "mixer volume": self.volume}
            if self.url:
                result["playlist_loop"] = [{"url": self.url}]
        elif cmd[:2] == ["playlist", "play"]:
            self.url, self.mode, self.time = cmd[2], "play", 0.0
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
    fake.url = "file:///music/other.flac"  # user picked something else in LMS
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
