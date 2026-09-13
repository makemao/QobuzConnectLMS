"""
Lyrion Music Server (LMS) backend.

The Qobuz Connect session stays here; audio does not. On play, the LMS player is
told to play ``qobuz://<track_id>.flac`` and LMS's own Qobuz plugin fetches and
streams the track (hi-res, multi-room sync and transcoding stay LMS's business).
State, position and volume are polled back over LMS's JSON-RPC API.
"""

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

from ..base import AudioBackend
from ..types import BackendInfo, BackendTrackMetadata, PlaybackState

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.0
# LMS needs a moment to resolve the qobuz:// URL and start streaming: a "stop"
# read during that window is not the end of the track.
PLAYBACK_START_GRACE_PERIOD_SECONDS = 6.0
STATUS_CACHE_SECONDS = 0.3
REQUEST_TIMEOUT_SECONDS = 5.0

_MODE_TO_STATE = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.STOPPED,
}


def track_url(track_id: str) -> str:
    """LMS Qobuz plugin URL for a track (see Plugins::Qobuz::ProtocolHandler)."""
    return f"qobuz://{track_id}.flac"


class LMSBackend(AudioBackend):
    """Drives one LMS player through the JSON-RPC API."""

    def __init__(
        self,
        host: str,
        player_id: str,
        port: int = 9000,
        name: Optional[str] = None,
    ):
        super().__init__(name=name or f"LMS {player_id}")
        self._host = host
        self._port = port
        self._player_id = player_id
        self._endpoint = f"http://{host}:{port}/jsonrpc.js"
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None

        self._current_url: Optional[str] = None
        self._playback_started_at = 0.0
        self._status: dict[str, Any] = {}
        self._status_at = 0.0

    # =========================================================================
    # JSON-RPC
    # =========================================================================

    async def _request(self, command: list[Any], player: Optional[str] = None) -> dict:
        if not self._session:
            raise RuntimeError("LMS backend not connected")
        payload = {
            "id": 1,
            "method": "slim.request",
            "params": [self._player_id if player is None else player, command],
        }
        async with self._session.post(self._endpoint, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    async def _player_status(self, fresh: bool = False) -> dict[str, Any]:
        if fresh or time.monotonic() - self._status_at > STATUS_CACHE_SECONDS:
            self._status = await self._request(["status", "-", "1", "tags:u"])
            self._status_at = time.monotonic()
        return self._status

    def _invalidate_status(self) -> None:
        self._status_at = 0.0

    @staticmethod
    def _status_url(status: dict[str, Any]) -> Optional[str]:
        loop = status.get("playlist_loop") or []
        if loop and isinstance(loop[0], dict):
            url = loop[0].get("url")
            return str(url) if url else None
        return None

    def _owns_player(self, status: dict[str, Any]) -> bool:
        """True while the LMS player is still playing the track we started."""
        return self._current_url is not None and self._status_url(status) == self._current_url

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def connect(self) -> bool:
        if self._is_connected:
            return True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        )
        try:
            server = await self._request(["serverstatus", "0", "50"], player="")
            players = server.get("players_loop") or []
            match = next(
                (p for p in players if self._player_id in (p.get("playerid"), p.get("name"))),
                None,
            )
            if not match:
                names = [p.get("name") for p in players]
                logger.error(
                    f"LMS player '{self._player_id}' not found on "
                    f"{self._host}:{self._port} (players: {names})"
                )
                await self._close_session()
                return False
            # Accept a player name in the config, but address it by MAC from now on
            self._player_id = match["playerid"]
            logger.info(
                f"Connected to LMS {server.get('version', '?')} player "
                f"'{match.get('name')}' ({self._player_id})"
            )
        except Exception as e:
            logger.error(f"Cannot reach LMS at {self._endpoint}: {e}")
            await self._close_session()
            return False

        self._is_connected = True
        self._poll_task = asyncio.create_task(self._poll_state_loop())
        return True

    async def disconnect(self) -> None:
        self._is_connected = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._close_session()

    async def _close_session(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    # =========================================================================
    # Playback control
    # =========================================================================

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        # The Qobuz CDN url is ignored on purpose: LMS resolves the track itself.
        lms_url = track_url(metadata.track_id)
        try:
            # A single-item LMS playlist: the Qobuz Connect queue is the queue.
            await self._request(["playlist", "repeat", "0"])
            await self._request(["playlist", "play", lms_url, metadata.title])
        except Exception as e:
            logger.error(f"LMS play failed for {lms_url}: {e}")
            self._current_url = None
            self._notify_playback_error(f"LMS play failed: {e}")
            return
        self._current_url = lms_url
        self._playback_started_at = time.monotonic()
        self._invalidate_status()
        self._notify_state_change(PlaybackState.PLAYING)
        self._notify_position_update(0)

    async def pause(self) -> None:
        await self._request(["pause", "1"])
        self._invalidate_status()
        self._notify_state_change(PlaybackState.PAUSED)

    async def resume(self) -> bool:
        if not self._current_url:
            return False
        try:
            status = await self._player_status(fresh=True)
            if not self._owns_player(status):
                return False
            await self._request(["pause", "0"])
        except Exception as e:
            logger.warning(f"LMS resume failed: {e}")
            return False
        self._invalidate_status()
        self._notify_state_change(PlaybackState.PLAYING)
        return True

    async def stop(self, *, next_track_id: Optional[str] = None) -> None:
        try:
            status = await self._player_status(fresh=True)
            # Never stop something the user started from another LMS controller
            if self._owns_player(status):
                await self._request(["stop"])
        except Exception as e:
            logger.warning(f"LMS stop failed: {e}")
        self._current_url = None
        self._invalidate_status()
        self._notify_state_change(PlaybackState.STOPPED)

    async def seek(self, position_ms: int) -> None:
        await self._request(["time", f"{max(0, position_ms) / 1000:.3f}"])
        self._invalidate_status()
        self._notify_position_update(position_ms)

    async def get_position(self) -> int:
        try:
            status = await self._player_status()
        except Exception:
            return 0
        if not self._owns_player(status):
            return 0
        return int(float(status.get("time") or 0) * 1000)

    # =========================================================================
    # Volume
    # =========================================================================

    async def set_volume(self, level: int) -> None:
        level = max(0, min(100, level))
        await self._request(["mixer", "volume", str(level)])
        self._volume = level

    async def get_volume(self) -> int:
        try:
            status = await self._player_status(fresh=True)
            # LMS reports a muted player as a negative volume
            self._volume = max(0, min(100, abs(int(float(status.get("mixer volume", 50))))))
        except Exception:
            pass
        return self._volume

    # =========================================================================
    # State
    # =========================================================================

    async def get_state(self) -> PlaybackState:
        if not self._current_url:
            return PlaybackState.STOPPED
        try:
            status = await self._player_status()
        except Exception:
            return self._state
        if not self._owns_player(status):
            return PlaybackState.STOPPED
        return _MODE_TO_STATE.get(str(status.get("mode")), PlaybackState.STOPPED)

    async def _poll_state_loop(self) -> None:
        while self._is_connected:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                if not self._current_url:
                    continue

                status = await self._player_status(fresh=True)
                in_grace = (
                    time.monotonic() - self._playback_started_at
                    < PLAYBACK_START_GRACE_PERIOD_SECONDS
                )

                if not self._owns_player(status):
                    if in_grace:
                        continue
                    # Someone played something else on this player from LMS:
                    # release it without advancing the Qobuz queue.
                    logger.info("LMS player taken over by another source, releasing it")
                    self._current_url = None
                    self._notify_state_change(PlaybackState.STOPPED)
                    continue

                new_state = _MODE_TO_STATE.get(str(status.get("mode")), PlaybackState.STOPPED)
                if new_state != self._state:
                    if self._state == PlaybackState.PLAYING and new_state == PlaybackState.STOPPED:
                        if in_grace:
                            continue
                        logger.debug("LMS track ended")
                        self._notify_track_ended()
                    self._notify_state_change(new_state)

                if new_state == PlaybackState.PLAYING:
                    self._notify_position_update(int(float(status.get("time") or 0) * 1000))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"LMS poll error: {e}")

    # =========================================================================
    # Info
    # =========================================================================

    def get_info(self) -> BackendInfo:
        return BackendInfo(
            backend_type="lms",
            name=self.name,
            device_id=self._player_id,
            ip=self._host,
            port=self._port,
            model="Lyrion Music Server",
        )
