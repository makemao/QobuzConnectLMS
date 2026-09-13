# QobuzConnectLMS — technical design

Status: implemented, validated by listening on a real LMS 9.1.1 + Qobuz plugin 3.7.1
+ squeezelite player.

## 1. Goal

Make Lyrion Music Server (LMS) players selectable as **Qobuz Connect** devices in the
official Qobuz app, without modifying LMS and without routing audio through a new
component.

Non-goals: gapless playback, replacing LMS's Qobuz plugin, exposing LMS browsing in
the Qobuz app.

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
| `play(url, meta)` | `playlist repeat 0`, then `playlist play qobuz://<track_id>.flac <title>` |
| `pause()` / `resume()` | `pause 1` / `pause 0` |
| `stop()` | `stop` (only if the proxy still owns the player) |
| `seek(ms)` | `time <seconds>` |
| `set_volume(v)` / `get_volume()` | `mixer volume <v>` / `status` → `mixer volume` |
| `get_state()` / `get_position()` | `status - 1 tags:u` → `mode`, `time`, `playlist_loop[0].url` |

`qobuz://<id>.flac` is the URL scheme handled by `Plugins::Qobuz::ProtocolHandler`;
the plugin substitutes its preferred format when resolving it.

At connect time a player **name** is accepted and resolved to its MAC, which is then
used for every call (names can change, MACs do not).

### 4.2 Ownership

The backend remembers the URL it asked LMS to play (`_current_url`). It **owns** the
player while `status.playlist_loop[0].url == _current_url`.

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
not owned  ──(grace period elapsed)──►  release: _current_url = None, notify STOPPED
                                         (no track_ended → the Qobuz queue does not advance)
owned, PLAYING → STOPPED  ──(grace elapsed)──►  notify track_ended, then STOPPED
owned, other transition  ──►  notify state change
owned, PLAYING  ──►  notify position
```

`track_ended` is what makes upstream's player advance to the next queue item and call
`play()` again. This works because the LMS playlist contains a single item: when it
finishes, LMS goes to `stop` instead of playing something else.

**Grace period** (`PLAYBACK_START_GRACE_PERIOD_SECONDS`, 6 s): right after
`playlist play`, LMS needs time to resolve the Qobuz URL, and `status` may still show
the previous URL or `stop`. Transitions read during this window are ignored, so a
slow start is neither seen as a takeover nor as a track end.

**Status cache** (`STATUS_CACHE_SECONDS`, 0.3 s): upstream's player also polls
`get_state()` / `get_position()` twice per second; a short cache avoids doubling the
requests. Commands invalidate it.

### 4.4 State mapping

| LMS `mode` | `PlaybackState` |
|---|---|
| `play` | `PLAYING` |
| `pause` | `PAUSED` |
| `stop` / unknown | `STOPPED` |

Muted players report a negative `mixer volume`; the absolute value is used.

### 4.5 Errors

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
- pause/resume, seek/position, volume clamping and muted volume.

Manual validation on real hardware: device visible in the Qobuz app, playback, pause,
skip, natural advance, volume, quality change, and release when a radio is started
from LMS.

## 8. Future work

- Gapless: pre-load the next Qobuz track with `playlist add` and detect the index
  change (upstream's `set_next_track` hook).
- Event-driven state via the LMS CLI `subscribe` (port 9090) instead of polling.
- LMS authentication (HTTP basic auth).
- Add LMS player discovery to the web UI "Add speaker" form.
- piCorePlayer: installed as a self-contained folder with start/stop scripts
  (`contrib/picoreplayer/`); a native `.tcz` extension would integrate with the web UI.
