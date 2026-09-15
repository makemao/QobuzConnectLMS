# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

QobuzConnectLMS makes **Lyrion Music Server (LMS)** players selectable as Qobuz Connect devices in the official Qobuz app. It is a fork of [qobuz-proxy](https://github.com/leolobato/qobuz-proxy), a headless Qobuz Connect device, and adds an **`lms` backend**: the Connect session lives in this process, while audio is played by LMS itself (`qobuz://<track_id>.flac` via LMS's Qobuz plugin). The upstream **DLNA** and **local audio** backends are kept.

Technical design of the LMS integration: `docs/DESIGN.md`. Keep it in sync when the backend's behavior changes.

## Commands

This project uses **[uv](https://docs.astral.sh/uv/)** to manage the local environment (`.venv` + `uv.lock`). Do not use `pip`/`venv` directly.

```bash
# Setup
uv sync                                     # Create .venv, install runtime deps + dev group (default), write uv.lock
uv sync --all-extras                        # Also install the local audio backend extra (sounddevice, numpy, soundfile)

# Run (uv run executes inside .venv — no manual activation needed)
uv run qobuz-proxy --config config.yaml    # Then visit http://localhost:8689 to log in
uv run qobuz-proxy --backend-type lms --lms-host <host> --lms-player <mac-or-name> --name "Living Room (LMS)"
uv run qobuz-proxy --discover              # Find DLNA renderers (upstream backend)

# Test
uv run pytest tests/backends/test_lms_backend.py   # LMS backend (fake LMS JSON-RPC server)
uv run pytest --ignore-glob='*local*'              # Everything except local-audio tests (need the [local] extra)
uv run pytest                                      # All tests (with uv sync --all-extras)

# Code quality
uv run ruff format qobuz_proxy/ tests/     # Format (100 char line length)
uv run ruff check qobuz_proxy/ tests/      # Lint
uv run mypy qobuz_proxy/                    # Type check (strict; pre-existing upstream errors — add no new ones)
```

Run Python via `uv run` (or `uv run python`), never bare `python`/`python3` and never `pip install`. Add/remove dependencies with `uv add` / `uv remove` so `pyproject.toml` and `uv.lock` stay in sync.

## Protocol Buffer Compilation

Generated files are committed. Re-run only if `.proto` files change:

```bash
protoc --python_out=qobuz_proxy/proto -I protos protos/*.proto

# Fix relative imports in generated files (macOS uses sed -i '', Linux uses sed -i)
sed -i'' -e 's/^import qconnect_common_pb2/from . import qconnect_common_pb2/g' qobuz_proxy/proto/qconnect_payload_pb2.py qobuz_proxy/proto/qconnect_queue_pb2.py
sed -i'' -e 's/^import qconnect_queue_pb2/from . import qconnect_queue_pb2/g' qobuz_proxy/proto/qconnect_payload_pb2.py
```

Proto files in `protos/`: `qconnect_common.proto`, `qconnect_envelope.proto`, `qconnect_payload.proto`, `qconnect_queue.proto`, `ws.proto`

## Architecture

### Component Wiring (app.py / speaker.py)

`QobuzProxy` in `app.py` is the main orchestrator; each configured speaker is a `Speaker` (`speaker.py`) bundling its own components:

1. **Auth** (`auth/`): OAuth login via Qobuz desktop app credentials (`oauth.py`), MD5-signed API requests (`api_client.py`), token persistence (`credentials.py`, cache in `~/.qobuz-proxy/` or `/data`), session/JWT tokens (`tokens.py`).
2. **Connect** (`connect/`): Registers as mDNS device + HTTP discovery endpoints (`discovery.py`), manages WebSocket connection to Qobuz servers (`ws_manager.py`), encodes/decodes protobuf messages (`protocol.py`)
3. **Playback** (`playback/`): State machine player (`player.py`), queue management (`queue.py`), track metadata from Qobuz API (`metadata.py`), command handlers (`command_handler.py`, `queue_handler.py`, `volume_handler.py`), periodic state reporting to Qobuz app (`state_reporter.py`)
4. **Backend** (`backends/`): Abstract `AudioBackend` interface (`base.py`), factory/registry pattern (`factory.py`). Implementations:
   - **LMS** (`lms/`): `LMSBackend` drives one LMS player over HTTP JSON-RPC (`/jsonrpc.js`). Plays `qobuz://<track_id>.flac`, reads `status` for state/position/volume (woken by LMS CLI events on port 9090, polling as fallback), detects natural track end and takeovers, and implements gapless by keeping the LMS playlist at `[current, next]` (see `docs/DESIGN.md` §4.3-4.5). No audio passes through the proxy.
   - **DLNA** (`dlna/`, upstream): SOAP/UPnP client (`client.py`), device capability detection (`capabilities.py`), audio proxy server (`proxy_server.py`)
   - **Local** (`local/`, upstream): Downloads FLAC, decodes to float32, plays via PortAudio (`backend.py`), ring buffer (`ring_buffer.py`), sounddevice output stream (`stream.py`). Optional deps: `sounddevice`, `numpy`, `soundfile`

### Key Data Flows

**Qobuz app command → LMS playback**: WebSocket message → `protocol.py` decodes → `ws_manager.py` dispatches → `command_handler.py`/`queue_handler.py` → `player.py` state machine → `LMSBackend.play()` → JSON-RPC `playlist play qobuz://<id>.flac` → LMS Qobuz plugin streams from the Qobuz CDN to the player. The CDN URL computed by the player is ignored by this backend.

**LMS state → Qobuz app**: `LMSBackend._poll_state_loop` wakes on an LMS CLI event (or every 1 s without events, 5 s as a safety net with events) and reads `status 0 10 tags:u` (mode, time, duration, `playlist_cur_index`, playlist URLs) → state/position callbacks → `player.py` → `state_reporter.py` → WebSocket → app. `PLAYING → stop` on a track the proxy still owns fires `track_ended`, which makes the player start the next queue item (fallback when no next track is armed).

**LMS CLI events**: `LMSBackend._events_loop` keeps a TCP connection to `lms_cli_port` subscribed to `playlist,pause,mixer,client`. A line for our player only invalidates the status cache and wakes the state loop — notifications are never interpreted, `status` stays the single source of truth. While events flow, status reads are cached up to 5 s and the position is extrapolated. On loss: immediate fallback to 1 s polling and reconnection with backoff; after each subscribe the state loop resyncs once.

**Gapless (LMS)**: the player arms the next queue item → `LMSBackend.set_next_track()` → `playlist add qobuz://<next>.flac`, so the LMS playlist is `[current, next]` and LMS/squeezelite chain them. When the state loop sees LMS's current item become the armed URL, the backend fires `next_track_started` (player updates the current track and re-arms the following one) and deletes the finished item. `clear_next_track()` does not delete right away: the item becomes `_orphan_url` for `REARM_GRACE_SECONDS`, so the frequent clear + re-arm of the *same* track keeps the LMS item untouched; otherwise it is deleted by the state loop (or immediately when a different track is armed). If LMS reaches a cleared track first, the backend reports `track_ended` and the player plays its real next track. Details and edge cases: `docs/DESIGN.md` §4.5.

**Ownership rule (LMS)**: the backend owns the player only while LMS's current item is the `qobuz://` URL it started or the armed next one. When a user plays something else from LMS, the backend releases the player without `track_ended` (the Qobuz queue does not advance) and never sends `stop` to LMS afterwards. A grace period after `play()` ignores transient LMS states while the URL resolves.

**Audio streaming (DLNA, upstream)**: Qobuz CDN → `proxy_server.py` (aiohttp server on port 7120) → DLNA device.

**Audio streaming (Local, upstream)**: Qobuz CDN → aiohttp download → soundfile FLAC decode → `RingBuffer` → PortAudio callback → speakers

### Quality

With `max_quality: auto`, DLNA queries device capabilities (`capabilities.py`); LMS and local backends announce Hi-Res 192k. For LMS, the format actually streamed is decided by the LMS Qobuz plugin's *preferred format* setting.

## Configuration Priority

1. CLI arguments (highest) → 2. Environment variables → 3. YAML config file → 4. Code defaults

LMS settings: `backend.lms.{host,port,player,cli_port}` or per speaker `lms_host` / `lms_port` / `lms_player` / `lms_cli_port` (YAML); `QOBUZPROXY_LMS_HOST`, `QOBUZPROXY_LMS_PORT`, `QOBUZPROXY_LMS_PLAYER`, `QOBUZPROXY_LMS_CLI_PORT` (env, comma-separated for several speakers); `--lms-host`, `--lms-port`, `--lms-player`, `--lms-cli-port` (CLI). `lms_cli_port: 0` disables events. Adding a field means touching `config.py` (dataclasses, YAML, env, validation, serialization), `speaker.py` (`_build_component_config`, status), `app.py` (add/edit speaker API) and `cli.py`.

Other key env vars: `QOBUZ_AUTH_TOKEN`, `QOBUZ_USER_ID`, `QOBUZ_MAX_QUALITY`, `QOBUZPROXY_BACKEND`, `QOBUZPROXY_DEVICE_NAME`, `QOBUZPROXY_DLNA_IP`, `QOBUZPROXY_AUDIO_DEVICE`, `QOBUZPROXY_LOG_LEVEL`

The web UI "Add speaker" form only supports the DLNA and local backends; LMS speakers are configured in YAML/env.

## Code Style

- **Ruff** for both formatting (`ruff format`) and linting, 100 char line length, **mypy** strict
- Type hints required on all public functions, Google-style docstrings only for non-obvious APIs
- All I/O is async. No blocking calls in main event loop
- Never log passwords or auth tokens
- Never commit real IP addresses, player MAC addresses, player names or tokens (use `192.168.1.x`, `00:11:22:33:44:55`, "Living Room")

## Testing

- `asyncio_mode = "auto"` in pyproject.toml — no `@pytest.mark.asyncio` decorators needed
- Tests mirror source structure in `tests/`
- LMS backend tests run against an in-process fake LMS (`tests/backends/test_lms_backend.py`) with patched timings (`POLL_INTERVAL_SECONDS`, `EVENT_POLL_INTERVAL_SECONDS`, `PLAYBACK_START_GRACE_PERIOD_SECONDS`, `STATUS_CACHE_SECONDS`, `REARM_GRACE_SECONDS`, `EVENTS_RECONNECT_MIN_SECONDS`). `FakeLMS` models a real playlist: `finish_current()` chains to the next item or stops, `replace()` simulates another LMS controller, deleting the playing item stops playback. It also runs a CLI server: `start_cli()`, `notify(...)` pushes a player notification, `drop_cli_clients()` simulates a lost connection; `status_requests` counts reads. `_connected()` disables events by default (`cli_port=0`); event tests use `_connected_with_events()` with the `slow_polling` fixture so only events can explain a reaction. Extend it rather than hitting a real server.
- When changing gapless, ownership or events logic, check that a test fails with the logic disabled, not only that it passes.
- Playback behavior still needs a manual check on a real LMS + player + Qobuz app.

## Commit Convention

`feat(module):`, `fix(module):`, `refactor(module):`, `test(module):`, `docs:`

## Reference Materials

- **Protocol reference**: [StreamCore32](https://github.com/tobiasguyer/StreamCore32) (C++ ESP32 Qobuz Connect implementation)
- **Upstream**: [qobuz-proxy](https://github.com/leolobato/qobuz-proxy) — check it for protocol/player fixes worth merging
- **LMS**: JSON-RPC/CLI documentation in the LMS web UI (*Help → Technical Information → The Lyrion Music Server Command Line Interface*); Qobuz URL handling in the plugin's `ProtocolHandler.pm`

## Debugging

### Reference Implementation

For Qobuz Connect protocol issues, the key files in [StreamCore32](https://github.com/tobiasguyer/StreamCore32) are:
- `stream/qobuz/src/QobuzPlayer.cpp` — Position tracking, state management
- `stream/qobuz/src/QobuzStream.cpp` — WebSocket message handling

### Inspecting an LMS player

```bash
curl -s -d '{"id":1,"method":"slim.request","params":["<player-mac>",["status","0","10","tags:u"]]}' http://<lms-host>:9000/jsonrpc.js
```

`mode` (play/pause/stop), `time` (seconds), `playlist_cur_index` and `playlist_loop[].url` are what the backend reads. While Qobuz Connect plays, the playlist should be `[current]` or `[current, next]`; the backend logs every arm and transition on lines starting with `Gapless:`.

To watch LMS events (what the backend subscribes to):

```bash
printf 'subscribe playlist,pause,mixer,client\n' | nc <lms-host> 9090
```

Logs: `Listening to LMS events on …` when subscribed, `LMS events lost (…), falling back to polling` on loss.

### Position Tracking Data Flow

```
LMS status (time, seconds)                     DLNA GetPositionInfo (RelTime)
  → LMSBackend.get_position() / poll loop        → DLNABackend.get_position()
  → Player._on_position_update() (sets _position_value_ms + _position_timestamp_ms)
  → StateReporter._build_state_report() (reads player._position_value_ms)
  → Protocol.encode_state_update() (encodes Position{timestamp, value})
  → WebSocket → Qobuz app
```

The protocol uses `Position { timestamp: fixed64, value: uint32 }`. The app interpolates: `value + (now - timestamp)`.

### Common Issues

1. **Device not visible in the app**: mDNS blocked — different VLAN, no host networking in Docker, or firewall blocking the per-speaker HTTP port (8690+)
2. **"LMS player not found" at startup**: wrong `lms_player` (MAC or exact name) or LMS unreachable on `lms_port`
3. **Track does not start on LMS**: LMS Qobuz plugin not logged in, or account without access to the track; check the LMS server log
4. **Queue does not advance**: another LMS controller changed the player's playlist (takeover → released on purpose), or LMS repeat re-enabled
5. **Gap between tracks**: no `Gapless: queued next track in LMS` line (the Qobuz app sent no next track, the same track repeats, or foreign items follow the current one in the LMS playlist) — see DESIGN §4.5
6. **Slow reaction to LMS-side changes**: no `Listening to LMS events` line (CLI port wrong, blocked, or `lms_cli_port: 0`) → polling mode
7. **State not updating**: `_playback_monitor_loop` only polls when `_state == PlaybackState.PLAYING`
8. **Protocol encoding**: Log values passed to `encode_state_update()` — binary issues are invisible otherwise

### Known Issues

**Mute in the Qobuz app does nothing**: the server does not forward it to renderers (verified with debug logs: no message reaches the device). Not fixable in the proxy.

**Qobuz app shows wrong quality** (upstream FR-DLNA-08): App always displays "Hi-Res 96k" regardless of actual streaming quality.

## Docker

Uses `network_mode: host` (required for mDNS). The compose file builds the image locally (the upstream image has no LMS backend). Ports: 8689 (web UI), 8690+ (per-speaker discovery), 7120 (DLNA audio proxy only). See `docker-compose.yaml` and `.env.example`.
