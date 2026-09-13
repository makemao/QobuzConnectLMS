"""
Lyrion Music Server (LMS) backend.

The Qobuz Connect session stays here; audio does not. On play, the LMS player is
told to play ``qobuz://<track_id>.flac`` and LMS's own Qobuz plugin fetches and
streams the track (hi-res, multi-room sync and transcoding stay LMS's business).
State, position and volume are polled back over LMS's JSON-RPC API.

Gapless: the LMS playlist holds at most the current track and the next Qobuz track.
LMS/squeezelite then chain them without a gap; the poll loop sees the current index
move to the armed track and reports the transition. The finished track is removed so
the playlist stays at two items.
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
# Our playlist never exceeds two items; a few more show a takeover clearly.
STATUS_PLAYLIST_ITEMS = 10

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
        self._next_url: Optional[str] = None  # armed for gapless, queued after current
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
            self._status = await self._request(
                ["status", "0", str(STATUS_PLAYLIST_ITEMS), "tags:u"]
            )
            self._status_at = time.monotonic()
        return self._status

    def _invalidate_status(self) -> None:
        self._status_at = 0.0

    @staticmethod
    def _playlist_urls(status: dict[str, Any]) -> list[Optional[str]]:
        """URLs of the first playlist items, by playlist index."""
        urls: list[Optional[str]] = []
        for item in status.get("playlist_loop") or []:
            if isinstance(item, dict):
                url = item.get("url")
                urls.append(str(url) if url else None)
        return urls

    @staticmethod
    def _cur_index(status: dict[str, Any]) -> int:
        try:
            return int(status.get("playlist_cur_index", 0))
        except (TypeError, ValueError):
            return 0

    def _status_url(self, status: dict[str, Any]) -> Optional[str]:
        """URL of the item LMS is currently playing."""
        urls = self._playlist_urls(status)
        index = self._cur_index(status)
        return urls[index] if 0 <= index < len(urls) else None

    def _owns_player(self, status: dict[str, Any]) -> bool:
        """True while LMS plays the track we started, or the next one we armed."""
        url = self._status_url(status)
        if url is None:
            return False
        return url in (self._current_url, self._next_url)

    def _index_of(self, status: dict[str, Any], url: str, after: int) -> Optional[int]:
        for index, item_url in enumerate(self._playlist_urls(status)):
            if index > after and item_url == url:
                return index
        return None

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
        self._next_url = None  # `playlist play` replaces the whole LMS playlist
        try:
            # The Qobuz Connect queue is the queue: no LMS repeat/shuffle reordering.
            await self._request(["playlist", "repeat", "0"])
            await self._request(["playlist", "shuffle", "0"])
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
        self._next_url = None
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
    # Gapless
    # =========================================================================

    @property
    def supports_gapless(self) -> bool:
        return True

    async def set_next_track(
        self, url: str, metadata: BackendTrackMetadata, queue_item_id: int = 0
    ) -> bool:
        """Queue the next Qobuz track right after the current one in LMS."""
        next_url = track_url(metadata.track_id)
        # Same track twice in a row: transitions could not be told apart by URL.
        # Let the normal track-ended path restart it instead.
        if not self._current_url or next_url == self._current_url:
            return False
        try:
            status = await self._player_status(fresh=True)
            if self._status_url(status) != self._current_url:
                return False
            cur = self._cur_index(status)
            if self._next_url == next_url and self._index_of(status, next_url, cur) is not None:
                return True  # already armed (the player re-arms in bursts)
            if self._next_url:
                await self.clear_next_track()
                status = await self._player_status(fresh=True)
                cur = self._cur_index(status)
            if len(self._playlist_urls(status)) > cur + 1:
                # Items after the current one that we did not put there: not ours to reorder
                logger.debug("Gapless: LMS playlist has foreign items after current, not arming")
                return False
            await self._request(["playlist", "add", next_url, metadata.title])
        except Exception as e:
            logger.warning(f"Gapless: failed to queue next track in LMS: {e}")
            return False
        self._next_url = next_url
        self._invalidate_status()
        logger.info(f"Gapless: queued next track in LMS: {metadata.artist} - {metadata.title}")
        return True

    async def clear_next_track(self) -> None:
        """Remove the armed next track from the LMS playlist."""
        next_url = self._next_url
        if not next_url:
            return
        self._next_url = None
        try:
            status = await self._player_status(fresh=True)
            if self._status_url(status) == next_url:
                # LMS already moved on to it: removing it would stop playback. Keep
                # ownership; the player's next command (play/skip) replaces the playlist.
                self._current_url = next_url
                return
            index = self._index_of(status, next_url, self._cur_index(status))
            if index is not None:
                await self._request(["playlist", "delete", str(index)])
        except Exception as e:
            logger.warning(f"Gapless: failed to remove next track from LMS: {e}")
        self._invalidate_status()

    async def _handle_transition(self, status: dict[str, Any]) -> None:
        """LMS moved from the current track to the armed one."""
        previous_url = self._current_url
        self._current_url = self._next_url
        self._next_url = None
        self._playback_started_at = time.monotonic()
        logger.info("Gapless: LMS moved to the next track")
        self._notify_next_track_started()
        self._notify_position_update(int(float(status.get("time") or 0) * 1000))
        # Drop the finished track so the LMS playlist stays [current, next]
        if previous_url:
            urls = self._playlist_urls(status)
            cur = self._cur_index(status)
            for index in range(cur - 1, -1, -1):
                if urls[index] == previous_url:
                    try:
                        await self._request(["playlist", "delete", str(index)])
                    except Exception as e:
                        logger.debug(f"Gapless: could not remove finished track: {e}")
                    break
        self._invalidate_status()

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
                    self._next_url = None
                    self._notify_state_change(PlaybackState.STOPPED)
                    continue

                if self._next_url and self._status_url(status) == self._next_url:
                    await self._handle_transition(status)
                    continue

                new_state = _MODE_TO_STATE.get(str(status.get("mode")), PlaybackState.STOPPED)
                if new_state != self._state:
                    if self._state == PlaybackState.PLAYING and new_state == PlaybackState.STOPPED:
                        if in_grace:
                            continue
                        logger.debug("LMS track ended")
                        # An armed next track that LMS did not reach is dropped: the
                        # player's track-ended handling starts it with play().
                        self._next_url = None
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
