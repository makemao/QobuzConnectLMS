# QobuzConnectLMS — technical design

Status: implemented, validated by listening on a real LMS 9.1.1 + Qobuz plugin 3.7.1
+ squeezelite player.

## 1. Goal

Make Lyrion Music Server (LMS) players selectable as **Qobuz Connect** devices in the
official Qobuz app, without modifying LMS and without routing audio through a new
component.

Gapless playback between consecutive Qobuz tracks is supported (§4.4).

Non-goals: replacing LMS's Qobuz plugin, exposing LMS browsing in the Qobuz app.

## 2. Options considered

| Option | Verdict |
|---|---|
| Official Qobuz Connect SDK | Not available: partner/certified manufacturers only. |
| Connect receiver playing to a local sound card, captured by LMS as a stream | Loses metadata, player sync and bit-perfect playback; extra latency. |
| qobuz-proxy DLNA backend | LMS players are not DLNA renderers. |
| Native Perl LMS plugin implementing the protocol | Re-implements the protobuf/WebSocket protocol in Perl; high cost and maintenance. |
| **Connect device outside LMS, LMS used as the renderer via JSON-RPC** | **Chosen.** Reuses a working protocol implementation; LMS keeps streaming. Same pattern as the Spotty plugin (Spotify Connect for LMS). |

## 3. Architecture

```
┌──────────────┐  mDNS + HTTP   ┌──────────────────────── qobuz-proxy process ─────────────────────────┐
│  Qobuz app   │───────────────►│ connect/discovery   ── announces one device per speaker              │
│  (phone)     │                │ connect/ws_manager  ── WebSocket session with Qobuz servers          │
└──────┬───────┘                │ playback/player     ── queue, state machine, reports to the app      │
       │ Qobuz Connect          │ backends/lms        ── LMSBackend (this fork)                        │
       ▼ (cloud, protobuf/WS)   └───────────────────────────────┬──────────────────────────────────────┘
┌──────────────┐                                                │ HTTP JSON-RPC  (/jsonrpc.js)
│ Qobuz servers│◄───────────────────────────────────────────────┤
└──────────────┘                                                ▼
                                                   ┌──────────────────────────┐   SlimProto   ┌─────────┐
                                                   │ LMS + Qobuz plugin       │──────────────►│ player  │
                                                   │ resolves qobuz://<id>    │               └─────────┘
                                                   └──────────┬───────────────┘
                                                              │ HTTPS
                                                              ▼
                                                        Qobuz CDN
```

Everything above the backend line is upstream qobuz-proxy and is unchanged: protocol,
authentication, discovery, queue, state reporting. The fork adds one backend
implementing upstream's `AudioBackend` interface (`qobuz_proxy/backends/base.py`) plus
the configuration plumbing.

Key property: the backend only exchanges **commands and state** with LMS. The
streaming URL the player computes (Qobuz CDN) is ignored; LMS's Qobuz plugin fetches
the track with its own account session.

## 4. The `lms` backend

File: `qobuz_proxy/backends/lms/backend.py` (`LMSBackend`).

### 4.1 LMS API

All calls are `POST http://<lms_host>:<lms_port>/jsonrpc.js` with
`{"method": "slim.request", "params": [<player_mac>, [<command>...]]}`.

| Backend method | LMS command |
|---|---|
| `connect()` | `serverstatus 0 50` (player lookup, by MAC or name) |
| `play(url, meta)` | `playlist repeat 0`, `playlist shuffle 0`, then `playlist play qobuz://<track_id>.flac <title>` |
| `pause()` / `resume()` | `pause 1` / `pause 0` |
| `stop()` | `stop` (only if the proxy still owns the player) |
| `seek(ms)` | `time <seconds>` |
| `set_volume(v)` / `get_volume()` | `mixer volume <v>` / `status` → `mixer volume` |
| `get_state()` / `get_position()` | `status 0 10 tags:u` → `mode`, `time`, `playlist_cur_index`, `playlist_loop[].url` |
| `set_next_track(url, meta)` | `playlist add qobuz://<next_id>.flac <title>` |
| `clear_next_track()` | `playlist delete <index>` of the armed track |

`qobuz://<id>.flac` is the URL scheme handled by `Plugins::Qobuz::ProtocolHandler`;
the plugin substitutes its preferred format when resolving it.

At connect time a player **name** is accepted and resolved to its MAC, which is then
used for every call (names can change, MACs do not).

### 4.2 Ownership

The backend remembers the URL it asked LMS to play (`_current_url`) and the armed next
track (`_next_url`, §4.4). It **owns** the player while the item at
`playlist_cur_index` has one of these two URLs.

- State and position are reported only while owned; otherwise the backend reports
  `STOPPED` / position 0.
- `stop()` sends `stop` to LMS only while owned, so a stop from the Qobuz app never
  interrupts playback started from another LMS controller.
- `resume()` returns `False` when not owned: upstream's player then stays `PAUSED`
  and pushes that state to the app instead of pretending to play.

### 4.3 Polling loop

A background task polls `status` every `POLL_INTERVAL_SECONDS` (1 s) while a track is
owned:

```
not owned  ──(grace period elapsed)──►  release: forget current/next, notify STOPPED
                                         (no track_ended → the Qobuz queue does not advance)
owned, LMS now on the armed next track  ──►  gapless transition (§4.4)
owned, PLAYING → STOPPED  ──(grace elapsed)──►  notify track_ended, then STOPPED
owned, other transition  ──►  notify state change
owned, PLAYING  ──►  notify position
```

`track_ended` is what makes upstream's player advance to the next queue item and call
`play()` again. It is the fallback when no next track is armed: the LMS playlist then
ends with the current track, so LMS goes to `stop` instead of playing something else.

**Grace period** (`PLAYBACK_START_GRACE_PERIOD_SECONDS`, 6 s): right after
`playlist play`, LMS needs time to resolve the Qobuz URL, and `status` may still show
the previous URL or `stop`. Transitions read during this window are ignored, so a
slow start is neither seen as a takeover nor as a track end.

**Status cache** (`STATUS_CACHE_SECONDS`, 0.3 s): upstream's player also polls
`get_state()` / `get_position()` twice per second; a short cache avoids doubling the
requests. Commands invalidate it.

### 4.4 Gapless

Without gapless, each track starts only after the proxy has noticed the previous one
stopped (up to one poll interval) and LMS has resolved and buffered the new URL: an
audible gap on albums meant to be heard continuously. LMS and squeezelite already
chain playlist items without a gap, provided the next item is **in the LMS playlist
before the current one ends**. The backend uses upstream's gapless hooks
(`supports_gapless`, `set_next_track`, `clear_next_track`, `on_next_track_started`) to
keep the LMS playlist at `[current, next]`:

```
Qobuz queue  ... [ A ] [ B ] [ C ] ...
                   │     │
play(A)            ▼     │
LMS playlist     [ A ]   │
set_next_track(B)        ▼
LMS playlist     [ A ] [ B ]            LMS/squeezelite pre-buffer B and chain A → B
A ends, LMS index 0 → 1
poll sees current item == B:  _current_url = B, notify next_track_started,
                              playlist delete 0  (finished A)
LMS playlist     [ B ]
player re-arms:  set_next_track(C) → [ B ] [ C ] ...
```

Rules:

- **Arming** (`set_next_track`) only happens while LMS is on the proxy's current track
  and nothing else follows it in the LMS playlist; foreign items are never reordered or
  removed. It is idempotent: upstream's player re-arms in bursts.
- **Same track twice in a row** (e.g. repeat-one) is not armed: transitions are
  detected by URL, which would be ambiguous. The regular track-ended path restarts it.
- **Queue edits, skips, stops**: the player calls `clear_next_track`, which deletes the
  armed item by index. `play()` and `stop()` also forget it (`playlist play` replaces
  the whole playlist).
- **Race — LMS already moved to the armed track when it is cleared**: deleting the
  playing item would stop playback, so it is kept and adopted as the current track;
  the player's next command replaces the playlist.
- **Armed track never reached** (LMS stopped instead, e.g. URL failure): the armed URL is
  dropped and `track_ended` is reported, so the player starts the next track with `play()`.
- Only the item **before** the current index is deleted after a transition; LMS stops
  playback when the playing item is removed (`Slim::Player::Playlist::removeTrack`).

Known limitation: when the Qobuz app resends the next-track info with a different
queue item id (it does so in bursts), upstream's player clears and re-arms the same
track, i.e. `playlist delete` then `playlist add`. This is harmless while the current
track has more than a few seconds left (observed on real hardware: re-arms 8-16 s before
the end still chained without a gap). A re-arm in the last seconds, after LMS started
pre-buffering the next track, could break that one transition. A possible improvement is
to defer the deletion so that re-arming the same URL keeps the existing LMS item.

### 4.5 State mapping

| LMS `mode` | `PlaybackState` |
|---|---|
| `play` | `PLAYING` |
| `pause` | `PAUSED` |
| `stop` / unknown | `STOPPED` |

Muted players report a negative `mixer volume`; the absolute value is used.

### 4.6 Errors

- LMS unreachable at startup → `connect()` returns `False`, the speaker fails to start
  and upstream's retry logic applies.
- `play()` failure → `playback_error` callback, nothing owned.
- Poll errors are logged at debug level and retried on the next tick; a short LMS
  outage does not end the session.

## 5. Configuration plumbing

Changes outside the backend are mechanical and follow upstream's patterns for the
`dlna` and `local` backends:

- `config.py`: `LMSConfig` (`host`, `port`, `player`), `SpeakerConfig.lms_*` fields,
  YAML (`backend.lms` and `speakers[].lms_*`), environment variables
  (`QOBUZPROXY_LMS_HOST/PORT/PLAYER`, comma-separated for several speakers),
  validation, serialization back to `config.yaml`.
- `backends/factory.py`: `BackendFactory.create_lms()` and registry entry `lms`.
- `speaker.py`: passes the LMS settings to the component config; with
  `max_quality: auto`, announces Hi-Res 192k (LMS decides the actual format).
- `app.py`: keeps LMS fields when a speaker is added or edited through the API; signal
  handlers are optional so the service also starts on Windows event loops.

## 6. Security and privacy

- Qobuz credentials: OAuth token cached by upstream in `~/.qobuz-proxy/` (or `/data`),
  never in `config.yaml` nor in the repository.
- LMS JSON-RPC has no authentication by default; the proxy only needs LAN access to it.
  If LMS password protection is enabled, it is not supported yet.
- The web UI (port 8689) and device ports are unauthenticated, like upstream: expose
  them only on a trusted LAN.

## 7. Testing

`tests/backends/test_lms_backend.py` runs the backend against an in-process fake LMS
JSON-RPC server (aiohttp) with shortened timings:

- player name resolved to MAC; unknown player refused;
- `play()` sends `qobuz://<id>.flac` and reports `PLAYING`;
- natural end fires `track_ended`;
- takeover releases the player without `track_ended`, and a later `stop()` does not
  stop LMS;
- pause/resume, seek/position, volume clamping and muted volume;
- gapless: next track queued once despite re-arm bursts; transition reported without
  `track_ended` and finished track removed; natural end after the last chained track;
  clearing removes the armed item; clearing after LMS already moved on keeps playing
  and ownership; no arming for a repeated track or with foreign items; `play()`
  discards an armed track.

The fake LMS models a real playlist (current index, add, delete, chaining, stop when
the playing item is removed). The gapless transition tests were checked to fail when
transition detection is disabled.

Manual validation on real hardware: device visible in the Qobuz app, playback, pause,
skip, natural advance, volume, quality change, release when a radio is started
from LMS, and gapless transitions on continuous albums (Pink Floyd, studio and live)
with skips and queue edits during playback.

## 8. Future work

- Event-driven state via the LMS CLI `subscribe` (port 9090) instead of polling.
- LMS authentication (HTTP basic auth).
- Add LMS player discovery to the web UI "Add speaker" form.
- piCorePlayer: installed as a self-contained folder with start/stop scripts
  (`contrib/picoreplayer/`); a native `.tcz` extension would integrate with the web UI.
