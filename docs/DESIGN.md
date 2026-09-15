# QobuzConnectLMS — technical design

Status: implemented, validated by listening on a real LMS 9.1.1 + Qobuz plugin 3.7.1
+ squeezelite player (playback, gapless, LMS CLI events).

## 1. Goal

Make Lyrion Music Server (LMS) players selectable as **Qobuz Connect** devices in the
official Qobuz app, without modifying LMS and without routing audio through a new
component.

Also supported: gapless playback between consecutive Qobuz tracks (§4.5) and
event-driven state from the LMS CLI (§4.4).

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
┌────────────┐ mDNS + HTTP  ┌───────────────────── qobuz-proxy process ─────────────────────┐
│ Qobuz app  │─────────────►│ connect/discovery  ── one Connect device per LMS player        │
│  (phone)   │              │ connect/ws_manager ── WebSocket session ──────────┐            │
└─────┬──────┘              │ playback/player    ── queue, state machine        │            │
      │ Qobuz Connect       │ backends/lms       ── LMSBackend (this fork)      │            │
      ▼ (cloud)             └───────────────┬───────────────▲───────────────────┼────────────┘
┌──────────────┐◄───────────────────────────┼───────────────┼───────────────────┘
│ Qobuz servers│          commands + status │               │ change notifications
└──────────────┘          HTTP JSON-RPC 9000│               │ CLI subscribe 9090
                                            ▼               │
                                  ┌──────────────────────────┴─┐   SlimProto   ┌─────────┐
                                  │ LMS + Qobuz plugin         │──────────────►│ player  │
                                  │ resolves qobuz://<id>.flac │               └─────────┘
                                  └─────────────┬──────────────┘
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

Commands and status reads are `POST http://<lms_host>:<lms_port>/jsonrpc.js` with
`{"method": "slim.request", "params": [<player_mac>, [<command>...]]}`.

| Backend method | LMS command |
|---|---|
| `connect()` | `serverstatus 0 50` (player lookup, by MAC or name) |
| `play(url, meta)` | `playlist repeat 0`, `playlist shuffle 0`, then `playlist play qobuz://<track_id>.flac <title>` |
| `pause()` / `resume()` | `pause 1` / `pause 0` |
| `stop()` | `stop` (only if the proxy still owns the player) |
| `seek(ms)` | `time <seconds>` |
| `set_volume(v)` / `get_volume()` | `mixer volume <v>` / `status` → `mixer volume` |
| `get_state()` / `get_position()` | `status 0 10 tags:u` → `mode`, `time`, `duration`, `playlist_cur_index`, `playlist_loop[].url` |
| `set_next_track(url, meta)` | `playlist add qobuz://<next_id>.flac <title>` |
| `clear_next_track()` | deferred `playlist delete <index>` of the armed track (§4.5) |

Change notifications come from the LMS CLI (TCP, `lms_cli_port`, default 9090):
`subscribe playlist,pause,mixer,client`, then one line per event,
`<url-encoded player id> <command> <args...>` (§4.4).

`qobuz://<id>.flac` is the URL scheme handled by `Plugins::Qobuz::ProtocolHandler`;
the plugin substitutes its preferred format when resolving it.

At connect time a player **name** is accepted and resolved to its MAC, which is then
used for every call (names can change, MACs do not).

### 4.2 Ownership

The backend remembers the URL it asked LMS to play (`_current_url`) and the armed next
track (`_next_url`, §4.5). It **owns** the player while the item at
`playlist_cur_index` has one of these two URLs.

- State and position are reported only while owned; otherwise the backend reports
  `STOPPED` / position 0.
- `stop()` sends `stop` to LMS only while owned, so a stop from the Qobuz app never
  interrupts playback started from another LMS controller.
- `resume()` returns `False` when not owned: upstream's player then stays `PAUSED`
  and pushes that state to the app instead of pretending to play.

### 4.3 State loop

A background task reads `status` and reconciles it while a track is owned. It runs
when woken by an LMS event (§4.4), or after `POLL_INTERVAL_SECONDS` (1 s) without
events, or `EVENT_POLL_INTERVAL_SECONDS` (5 s) as a safety net while events flow.

```
LMS on a cleared next track (§4.5)  ──►  hand back: notify track_ended
not owned  ──(grace period elapsed)──►  release: forget current/next, notify STOPPED
                                         (no track_ended → the Qobuz queue does not advance)
owned, LMS now on the armed next track  ──►  gapless transition (§4.5)
cleared next track older than REARM_GRACE_SECONDS  ──►  delete it from LMS
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

**Status cache**: upstream's player also calls `get_state()` / `get_position()` twice
per second. Without events, reads are shared for `STATUS_CACHE_SECONDS` (0.3 s). With
events, any LMS change invalidates the cache, so reads are shared up to the safety-net
interval (5 s). Commands always invalidate it. The position is extrapolated from the
last read while playing (`time + elapsed`, capped at `duration`), which is also what
the Qobuz app does between state reports.

### 4.4 LMS CLI events

Polling alone means LMS-side changes (pause from another controller, track change,
takeover) are seen up to a second late, and LMS is queried two to three times per
second even when nothing happens. LMS pushes change notifications on its CLI port:

```
proxy → LMS:  subscribe playlist,pause,mixer,client
LMS → proxy:  00%3A11%3A22%3A33%3A44%3A55 playlist newsong Song 1
              00%3A11%3A22%3A33%3A44%3A55 pause 1
              aa%3Abb%3A... mixer volume 30          (another player: ignored)
```

The events task only uses notifications as **wake-up signals**: a line for the
configured player invalidates the status cache and wakes the state loop, which reads
`status` and applies exactly the same reconciliation as in polling mode. The
notification content is not interpreted, so there is a single source of truth
(`status`) and no second state machine to keep consistent.

- **Connection lifecycle**: opened at `connect()` when `lms_cli_port` is non-zero,
  closed at `disconnect()`. After each (re)subscribe the state loop is woken once, since
  changes may have been missed while not subscribed.
- **Loss**: the backend immediately falls back to 1 s polling and wakes the state
  loop, then reconnects with exponential backoff (2 s → 60 s).
- **Unavailable CLI** (disabled in LMS, firewalled, `lms_cli_port: 0`): polling mode,
  same behavior as before events existed.
- **Safety net**: while events flow, the state loop still reads `status` every 5 s.

### 4.5 Gapless

Without gapless, each track starts only after the proxy has noticed the previous one
stopped and LMS has resolved and buffered the new URL: an audible gap on albums meant
to be heard continuously. LMS and squeezelite already chain playlist items without a
gap, provided the next item is **in the LMS playlist before the current one ends**.
The backend uses upstream's gapless hooks (`supports_gapless`, `set_next_track`,
`clear_next_track`, `on_next_track_started`) to keep the LMS playlist at
`[current, next]`:

```
Qobuz queue  ... [ A ] [ B ] [ C ] ...
                   │     │
play(A)            ▼     │
LMS playlist     [ A ]   │
set_next_track(B)        ▼
LMS playlist     [ A ] [ B ]            LMS/squeezelite pre-buffer B and chain A → B
A ends, LMS index 0 → 1  (CLI event: playlist newsong)
state loop sees current item == B:  _current_url = B, notify next_track_started,
                                    playlist delete 0  (finished A)
LMS playlist     [ B ]
player re-arms:  set_next_track(C) → [ B ] [ C ] ...
```

Rules:

- **Arming** (`set_next_track`) only happens while LMS is on the proxy's current track
  and nothing else follows it in the LMS playlist; foreign items are never reordered or
  removed. Arming the already armed track is a no-op.
- **Deferred removal / re-arm**: the Qobuz app resends the queue in bursts with new
  queue item ids, and upstream's player then clears and re-arms the *same* track.
  `clear_next_track` therefore does not delete immediately: it marks the item as
  cleared (`_orphan_url`). Re-arming the same track within `REARM_GRACE_SECONDS` (2 s)
  adopts the existing LMS item — the playlist is not touched, so LMS keeps any
  pre-buffered data. Arming a different track removes the cleared one right away;
  otherwise the state loop removes it once the grace has elapsed.
- **LMS reaches a cleared track before its removal** (the current track ended inside
  the grace window): the queue no longer wants it, so the backend reports
  `track_ended` for the finished track and lets the player `play()` its real next track,
  which replaces the LMS playlist.
- **Same track twice in a row** (e.g. repeat-one) is not armed: transitions are
  detected by URL, which would be ambiguous. The regular track-ended path restarts it.
- **Skips and stops**: `play()` and `stop()` forget armed and cleared tracks
  (`playlist play` replaces the whole playlist).
- **Race — LMS already moved to the armed track when it is cleared**: deleting the
  playing item would stop playback, so it is kept and adopted as the current track;
  the player's next command replaces the playlist.
- **Armed track never reached** (LMS stopped instead, e.g. URL failure): the armed URL is
  dropped and `track_ended` is reported, so the player starts the next track with `play()`.
- Only items **other than the playing one** are ever deleted; LMS stops playback when
  the playing item is removed (`Slim::Player::Playlist::removeTrack`).

### 4.6 State mapping

| LMS `mode` | `PlaybackState` |
|---|---|
| `play` | `PLAYING` |
| `pause` | `PAUSED` |
| `stop` / unknown | `STOPPED` |

Muted players report a negative `mixer volume`; `get_volume()` uses the absolute value.

**Pause / resume from another LMS controller** (a remote, the LMS web UI): the
player's monitor loop polls `get_state()` every 0.5 s. While `PLAYING`, a `PAUSED`
backend switches the player to `PAUSED` and reports it; while `PAUSED`, a `PLAYING`
backend confirmed by `_PAUSED_PLAY_CONFIRMATIONS` (2) consecutive polls switches it to
`PLAYING`, reports the state and the resumed position to the app, and continues the
play-report session (no new start). A resume is not reported while an app command holds
the playback lock (the app's own resume is on the way). Found on the reference system:
before this, a resume from a remote left the app showing paused while LMS played.

**Volume changed outside the app** (another LMS controller: a remote, the LMS web UI,
Home Assistant): the state loop compares the `mixer volume` of each status read with the
last volume known to the app (`_known_volume`) and, when it differs, fires the backend's
`volume_change` callback; `QobuzPlayer._on_backend_volume_change` updates its cached
volume and sends `RNDR_SRVR_VOLUME_CHANGED`, the message already used after an app
change, so the app's volume bar follows.

- While the player is idle (no Qobuz track owned) the loop does not read the status; an
  LMS `mixer` CLI event still triggers one read for the volume.
- Muted (negative) is reported as 0; unmuting reports the restored volume.
- A volume set by the app becomes the known volume (no echo), and differences seen in the
  following `APP_VOLUME_GRACE_SECONDS` (1 s) are ignored: a status read that started
  before the app change would otherwise make the app slider jump back.
- The first volume seen after start is recorded, not reported; the player dedupes equal
  values and ignores device changes in fixed-volume mode.

### 4.7 Errors

- LMS unreachable at startup → `connect()` returns `False`, the speaker fails to start
  and upstream's retry logic applies.
- `play()` failure → `playback_error` callback, nothing owned.
- State loop errors are logged at debug level and retried on the next tick; a short LMS
  outage does not end the session.
- LMS CLI errors → polling fallback and reconnection (§4.4).

## 5. Configuration plumbing

Changes outside the backend are mechanical and follow upstream's patterns for the
`dlna` and `local` backends:

- `config.py`: `LMSConfig` (`host`, `port`, `player`, `cli_port`),
  `SpeakerConfig.lms_*` fields, YAML (`backend.lms` and `speakers[].lms_*`),
  environment variables (`QOBUZPROXY_LMS_HOST/PORT/PLAYER/CLI_PORT`, comma-separated for
  several speakers), validation (`cli_port` 0 = events disabled), serialization back
  to `config.yaml`.
- `cli.py`: `--backend-type lms`, `--lms-host`, `--lms-port`, `--lms-player`,
  `--lms-cli-port`.
- `backends/factory.py`: `BackendFactory.create_lms()` and registry entry `lms`.
- `speaker.py`: passes the LMS settings to the component config; with
  `max_quality: auto`, announces Hi-Res 192k (LMS decides the actual format).
- `app.py`: keeps LMS fields when a speaker is added or edited through the API; signal
  handlers are optional so the service also starts on Windows event loops.

## 6. Security and privacy

- Qobuz credentials: OAuth token cached by upstream in `~/.qobuz-proxy/` (or `/data`),
  never in `config.yaml` nor in the repository.
- LMS JSON-RPC and CLI have no authentication by default; the proxy only needs LAN
  access to them. LMS password protection is not supported yet.
- The web UI (port 8689) and device ports are unauthenticated, like upstream: expose
  them only on a trusted LAN.

## 7. Testing

`tests/backends/test_lms_backend.py` runs the backend against an in-process fake LMS
(aiohttp JSON-RPC server + asyncio TCP CLI server) with shortened timings:

- player name resolved to MAC; unknown player refused;
- `play()` sends `qobuz://<id>.flac` and reports `PLAYING`;
- natural end fires `track_ended`;
- takeover releases the player without `track_ended`, and a later `stop()` does not
  stop LMS;
- pause/resume, seek/position, volume clamping and muted volume;
- volume changed on LMS reported while playing (muted as 0), an app change not
  reported back, a stale value right after an app change ignored, a `mixer` event
  reporting while idle and other events not reading the status;
- gapless: next track queued once; transition reported without `track_ended` and
  finished track removed; natural end after the last chained track; clearing after LMS
  already moved on keeps playing and ownership; no arming for a repeated track or with
  foreign items; `play()` discards an armed track;
- deferred removal: cleared track removed after the grace; re-arming the same track
  keeps the LMS item (no delete, no second add); arming a different track replaces the
  cleared one; LMS reaching a cleared track hands back to the player;
- events: subscription; an event wakes the state loop for a gapless transition and for
  a pause from LMS while polling is too slow to notice; events for other players are
  ignored; status reads are shared and the position extrapolated; losing the CLI falls
  back to polling, then reconnects and resubscribes.

The fake LMS models a real playlist (current index, add, delete, chaining, stop when
the playing item is removed) and pushes CLI notifications. Each mechanism (transition
detection, re-arm adoption, event wake-up, event-refreshed cache, polling fallback,
cleared-track hand-back) was checked by disabling it: at least one test fails each time.

Manual validation on real hardware: device visible in the Qobuz app, playback, pause,
skip, natural advance, volume, quality change, release when a radio is started
from LMS, gapless transitions on continuous albums (Pink Floyd, studio and live) with
skips and queue edits during playback, and the same after enabling LMS CLI events and
deferred removal (no repeated re-queueing, playlist stays `[current, next]`).

## 8. Future work

Known limitation: the app's mute button is not delivered to the device. Captured with
`QOBUZPROXY_LOG_LEVEL=DEBUG` on the reference system (2026-09-15): mute and unmute in the
app produced no message for the renderer, neither a dedicated one (the protocol only has
`CTRL_SRVR_MUTE_VOLUME`, app → server, and `SRVR_CTRL_VOLUME_MUTED`, server → apps) nor
`SRVR_RNDR_SET_VOLUME 0`, and no unhandled message type. Nothing to map on the LMS side.


- LMS authentication (HTTP basic auth on JSON-RPC, `login` on the CLI).
- Add LMS player discovery to the web UI "Add speaker" form.
- piCorePlayer: installed as a self-contained folder with start/stop scripts
  (`contrib/picoreplayer/`); a native `.tcz` extension would integrate with the web UI.
