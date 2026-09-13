# QobuzConnectLMS

**Qobuz Connect for Lyrion Music Server (LMS, formerly Logitech Media Server).**

Pick your Squeezebox / squeezelite / piCorePlayer players directly from the official
Qobuz app, like any Qobuz Connect speaker: play, pause, skip, seek and change the
volume from your phone, while LMS keeps doing the actual streaming.

QobuzConnectLMS is a fork of [qobuz-proxy](https://github.com/leolobato/qobuz-proxy)
that adds an **`lms` backend**. The upstream DLNA and local-audio backends are kept.

## How it works

```
 Qobuz app ──Qobuz Connect (cloud)──► QobuzConnectLMS ──JSON-RPC──► LMS ──► players
                                       (one virtual       plays qobuz://<id>.flac
                                        device per         with its Qobuz plugin
                                        LMS player)
```

- Each configured LMS player is announced on your network as a Qobuz Connect device.
- When you play a track from the Qobuz app, QobuzConnectLMS asks LMS to play
  `qobuz://<track_id>.flac` on that player. **Audio never goes through the proxy**:
  LMS and its Qobuz plugin fetch and stream the track, so LMS's format preferences,
  transcoding and player sync keep working.
- Playback state, position and volume are polled back from LMS and reported to the
  Qobuz app.
- **Gapless**: the next track of the Qobuz queue is added to the LMS playlist in
  advance, so LMS chains tracks without a gap (live albums, classical, DJ mixes).
- If you start something else on the player from LMS (a radio, your library...), the
  proxy releases the player: the Qobuz queue does not advance and a stop from the
  Qobuz app will not interrupt what you started.

See [docs/DESIGN.md](docs/DESIGN.md) for the technical design.

## Requirements

- Lyrion Music Server with the **Qobuz plugin** installed and logged in to your
  Qobuz account (tested with LMS 9.1.1 and plugin 3.7.1).
- An active Qobuz subscription.
- An always-on machine on the **same LAN** as the phone running the Qobuz app, with
  Python 3.10+ (or Docker). It can be the LMS host itself or any other machine.
- Network access from that machine to the LMS web port (default `9000`).

## Installation

### piCorePlayer

To run it on the piCorePlayer box that hosts LMS, with a start-at-boot script and
nothing installed into the system, follow
[contrib/picoreplayer/README.md](contrib/picoreplayer/README.md).

### Without Docker

```bash
git clone https://github.com/makemao/QobuzConnectLMS.git
cd QobuzConnectLMS
pip install .
cp config.yaml.example config.yaml   # then edit it, see below
qobuz-proxy --config config.yaml
```

### Docker

```bash
docker build -t qobuzconnectlms .
docker run -d --name qobuzconnectlms --network host \
  -v ./data:/data \
  qobuzconnectlms
```

Put your `config.yaml` in `./data/`. Host networking is required for mDNS discovery
(see [Network](#network)).

## Configuration

Add one speaker per LMS player in `config.yaml`:

```yaml
qobuz:
  max_quality: 27

speakers:
  - name: "Living Room (LMS)"        # name shown in the Qobuz app
    backend: lms
    lms_host: "192.168.1.10"         # LMS server address
    lms_port: 9000                   # LMS web / JSON-RPC port
    lms_player: "00:11:22:33:44:55"  # player MAC address, or its exact LMS name

  - name: "Kitchen (LMS)"
    backend: lms
    lms_host: "192.168.1.10"
    lms_player: "Kitchen"
```

To find player MAC addresses: LMS web UI → *Settings* → *Information*, or

```bash
curl -s -d '{"id":1,"method":"slim.request","params":["",["players","0","50"]]}' \
  http://<lms-host>:9000/jsonrpc.js
```

A single speaker can also be configured with environment variables:
`QOBUZPROXY_BACKEND=lms`, `QOBUZPROXY_DEVICE_NAME`, `QOBUZPROXY_LMS_HOST`,
`QOBUZPROXY_LMS_PORT`, `QOBUZPROXY_LMS_PLAYER` (see `.env.example`; comma-separated
values define several speakers).

> LMS speakers are configured in `config.yaml` or environment variables. The web UI
> "Add speaker" form only knows the upstream DLNA and local backends.

### Authentication

1. Start QobuzConnectLMS.
2. Open **http://<host>:8689** and click **Log in to Qobuz**.
3. Sign in on the Qobuz page; you are redirected back, authenticated.

The token is cached in `~/.qobuz-proxy/` (or `/data` in Docker), never in
`config.yaml`. Keep that directory private.

### Audio quality

`max_quality` is what the proxy announces to the Qobuz app. The stream actually
played is chosen by **LMS's Qobuz plugin** (its *preferred format* setting) and by
what your player supports.

## Network

- The Qobuz app finds devices with **mDNS**: the machine running QobuzConnectLMS must
  be on the same LAN/VLAN as your phone. In Docker, use `--network host`.
- The app then connects to the device's HTTP port (auto-assigned from `8690`, one
  per speaker); allow it in your firewall on private networks.
- Windows works for testing (Ctrl+C to stop), Linux is the intended target.

## Known limitations

- **Unofficial.** Qobuz Connect is not a public API: this relies on a
  reverse-engineered protocol and may break if Qobuz changes it.
- When playing, the proxy replaces the player's current LMS playlist with the Qobuz
  track (plus the next one, for gapless) and turns LMS repeat and shuffle off for that
  player.
- Gapless is skipped when the same track is queued twice in a row (it is restarted
  normally instead).
- LMS player groups/sync are driven by LMS: pick the group's master player.

## Development

```bash
pip install uv
uv sync
uv run pytest tests/backends/test_lms_backend.py   # LMS backend tests (fake LMS server)
uv run pytest --ignore-glob='*local*'              # everything except local-audio tests
uv run ruff check qobuz_proxy/ tests/
```

## Acknowledgments

- [qobuz-proxy](https://github.com/leolobato/qobuz-proxy) by leolobato — the Qobuz
  Connect device implementation this fork is built on.
- [StreamCore32](https://github.com/tobiasguyer/StreamCore32) by Tobias Guyer — the
  original Qobuz Connect reverse-engineering work.
- [Lyrion Music Server](https://lyrion.org) and its Qobuz plugin.

## Disclaimer

Not affiliated with Qobuz or the Lyrion project. For personal use with your own
Qobuz subscription. Like upstream, this project was developed with AI assistance
([Claude Code](https://claude.ai/claude-code)) under human guidance and review.

## License

[MIT](LICENSE) — original copyright of qobuz-proxy retained.
