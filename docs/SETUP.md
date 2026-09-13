# Reference setup

The installation QobuzConnectLMS was built and validated on, and how it works together
with its companion project **[DevialetLMSBridge](https://github.com/makemao/DevialetLMSBridge)**
(volume of a Devialet Phantom driven by LMS, with the Shield remote as master control).
Addresses and identifiers of the real installation are intentionally left out.

> The same overview is kept in DevialetLMSBridge's
> [docs/SETUP.md](https://github.com/makemao/DevialetLMSBridge/blob/main/docs/SETUP.md).
> Keep both in sync when the setup changes.

## 1. Hi-fi and cinema setup

### 1.1 Overview

```
┌─────────────────────────────┐   SMB (music files)   ┌──────────────────────────────────────────────┐
│ NAS — openmediavault         │ ◄──────────────────── │ Raspberry Pi 4 — piCorePlayer 10 (Ethernet)   │
│  share "musiclib" (~1 TB)    │                       │                                              │
└─────────────────────────────┘                       │  Lyrion Music Server 9.1.1                   │
                                                      │   ├─ library: NAS share (/mnt/MUSICLIB)       │
┌─────────────────────────────┐   Qobuz streams        │   ├─ Qobuz plugin 3.7.1 (up to 24/192)       │
│ Qobuz (cloud)                │ ◄──────────────────── │   └─ player "squeezelite" (volume fixed 100%)│
└─────────────────────────────┘                       │  QobuzConnectLMS · Devialet LMS bridge        │
                                                      │  squeezelite ──► HiFiBerry Digi Pro (S/PDIF)  │
                                                      └───────────────────────┬──────────────────────┘
                                                                              │ optical (TOSLINK)
┌───────────────┐ HDMI ┌─────────────────────┐ HDMI ┌──────────────────┐     ▼
│ Full HD TV     │ ◄─── │ HDMI audio extractor │ ◄─── │ NVIDIA Shield     │  ┌──────────────────────┐
└───────────────┘      │                      │      │ (Android TV 11)   │  │ S/PDIF switch (auto) │
                       └──────────┬───────────┘      └──────────────────┘  └──────────┬───────────┘
                                  └──────────── optical (TOSLINK) ──────────────┘      │ optical
                                                                                       ▼
                                                      ┌──────────────────────────────────────────────┐
                                                      │ Devialet Phantom I 103 dB (2020) — stereo pair│
                                                      │  leader (left) + right, optical input, DAC +  │
                                                      │  amplification; volume set by the bridge       │
                                                      └──────────────────────────────────────────────┘
```

### 1.2 Components

| Component | Model / version | Role and notable settings |
|---|---|---|
| Music storage | NAS running **openmediavault**, SMB share `musiclib` (~1 TB, ~310 GB used) | Local music library; mounted read/write on the Pi over SMB 3.1.1 as `/mnt/MUSICLIB` |
| Music server + player host | **Raspberry Pi 4 Model B** (4 GB), **piCorePlayer 10.0.0** (kernel 6.6.67), wired Ethernet | RAM-based OS; persistent data on the SD card partition. Wi-Fi and on-board Bluetooth disabled (`dtoverlay=disable-wifi`, `disable-bt`) |
| Music server | **Lyrion Music Server 9.1.1** on the Pi | Media folder `/mnt/MUSICLIB` (the NAS share); Qobuz plugin 3.7.1 with preferred format 27 (FLAC up to 24-bit/192 kHz); UPnP Bridge plugin installed |
| Player | **squeezelite 2.0.0** (piCorePlayer build) | Output `hw:CARD=sndrpihifiberry`; LMS player **volume control: output level fixed at 100 %** (`digitalVolumeControl = 0`): the stream leaves the Pi unaltered |
| Digital output | **HiFiBerry Digi Pro** (WM8804, `dtoverlay=hifiberry-digi-pro`) | S/PDIF, optical out; dedicated audio clocks (not the Pi's); up to 24/192 |
| Cinema source | **NVIDIA Shield** (Android TV 11) → **HDMI audio extractor** → **Full HD TV** | Off (asleep) most of the time. The extractor passes video to the TV over HDMI and sends the audio over optical to the switch. Set to **HDMI fixed volume**. *Network debugging* enabled: its Bluetooth remote is read over network ADB to become the master control (never used to wake it) |
| Switch | **Automatic S/PDIF (optical) switch**, 2 inputs → 1 output | Pi (HiFiBerry) and HDMI extractor in, Phantom out; selects the active input by itself |
| Link | Optical TOSLINK cables | Digital and galvanically isolated: no electrical path between the sources and the speakers |
| Speakers | **Devialet Phantom I 103 dB** (2020 generation), stereo pair (leader = left, right), firmware DOS 2.19.1 (reported model: "Phantom 103 dB") | Receive the optical input on the leader and do the D/A conversion and amplification; volume controlled over the Devialet IP Control API. Other inputs available: AirPlay 2, Spotify Connect, UPnP, Roon Ready (RAAT), Bluetooth, second optical |

### 1.3 Design choices this setup relies on

- **No native Qobuz Connect on the speakers**: this Phantom I (2020) offers AirPlay 2,
  Spotify Connect, UPnP, Roon Ready, Bluetooth and optical inputs, but no Qobuz Connect
  (its IP Control API lists no such source). QobuzConnectLMS provides it through LMS,
  which keeps the whole bit-perfect optical chain and the single volume path below.
- **Bit-perfect digital path, volume in the speakers**: LMS does not attenuate (fixed
  100 %), so the full-resolution signal reaches the Phantom, and the Phantom's own volume
  is the only volume stage. This is why LMS volume changes must be forwarded to the
  Phantom (the volume bridge), and why there is no double attenuation.
- **Everything goes through LMS**: the local library (NAS), Qobuz (plugin and Qobuz
  Connect) and radios all play on the same LMS player, so one volume path and one set of
  controls cover every source.
- **One volume stage for music and cinema**: both sources reach the Phantom through the
  S/PDIF switch, so the Phantom's volume (driven by LMS through the volume bridge) is the
  volume of everything, including films.
- **Master control: the Shield remote**. Its keys are read over network ADB (read-only,
  also while the Shield sleeps) by `shield_remote.py`: volume keys change the LMS volume,
  hence the Phantom's, for every source; play/pause, fast forward and rewind drive the
  music when it plays or when the TV is off, and are left to the film otherwise ("TV on =
  film"). The Shield is set to fixed volume and never woken up. A Hue Tap Dial knob over
  Zigbee (`tapdial.py`) is kept as an alternative, not installed.
- **Optical isolation**: extra hardware on the Pi (for instance a USB Zigbee coordinator
  for the alternative knob) cannot inject electrical noise into the speakers.
- During validation, Qobuz tracks in 16/44.1, 24/96 and 24/192 played through this chain.

## 2. Services on the Pi

| Service | Project | Role | Start at boot (piCorePlayer *User commands*) |
|---|---|---|---|
| LMS + squeezelite | piCorePlayer | music server and player | piCorePlayer |
| **QobuzConnectLMS** | this repo | player selectable in the Qobuz app (Qobuz Connect) | `/mnt/mmcblk0p2/qconnect-lms/start.sh` |
| **Volume bridge** | DevialetLMSBridge | LMS player volume → Phantom volume | `/mnt/mmcblk0p2/devialet-lms-bridge/start.sh` |
| **Shield remote service** | DevialetLMSBridge | Shield remote keys → LMS volume and music | `/mnt/mmcblk0p2/devialet-lms-bridge/start.sh remote` |
| Tap Dial service | DevialetLMSBridge | alternative: Hue Tap Dial knob → LMS commands | `start.sh tapdial` (not installed) |

Each project lives in its own folder on the persistent SD partition, with its own start /
stop / status scripts, and logs in RAM (`/tmp/qconnect-lms/`,
`/tmp/devialet-lms-bridge/<service>/`). Nothing is installed into the system besides the
piCorePlayer user command entries (and, for a USB Zigbee coordinator, the `usb-serial`
extension).

QobuzConnectLMS on piCorePlayer: [contrib/picoreplayer/README.md](../contrib/picoreplayer/README.md).

## 3. How the pieces talk to each other

```
Qobuz app ──Qobuz Connect──► QobuzConnectLMS ──JSON-RPC 9000──► LMS ──► squeezelite ──optical──► Phantom
                                  ▲                               │
                                  └── CLI 9090 events ────────────┤
                                                                  │ CLI 9090: prefset server volume
Shield remote ──Bluetooth──► NVIDIA Shield                        ▼
                                  │                         volume bridge ──IP Control API──► Phantom
                                  │ network ADB (getevent, read-only)                            ▲
                                  ▼                                                              │
                        shield_remote.py ──JSON-RPC 9000──► LMS                                  │
                                                                                                 │
NVIDIA Shield ──HDMI──► HDMI audio extractor ──HDMI──► TV                                        │
                             └──optical──► S/PDIF switch (auto) ──optical────────────────────────┘

(alternative, not installed: Hue Tap Dial ──Zigbee──► tapdial.py ──JSON-RPC 9000──► LMS)
```

- **Everything goes through LMS.** QobuzConnectLMS and the remote service only send
  LMS commands; they never talk to the Phantom. The volume bridge is the only component that
  talks to the Phantom.
- **Volume from the Qobuz app**: QobuzConnectLMS sets the LMS player volume
  (`mixer volume`); LMS emits `prefset server volume <n>`; the volume bridge forwards it
  to the Phantom (capped). The app, LMS web UI, the remote and the Phantom stay in sync.
- **Mute**: LMS stores a muted volume as a negative value; the bridge sets the Phantom
  to 0 and restores it on unmute.
- **LMS CLI (9090)** is used by several clients at once, each on its own connection:
  QobuzConnectLMS subscribes to `playlist,pause,mixer,client` events, the volume bridge to
  `prefset`. Subscriptions are per connection and do not interfere.
- **Ports on the Pi**: 9000 (LMS web / JSON-RPC), 9090 (LMS CLI), 8689 (QobuzConnectLMS web
  UI), 8690 (Qobuz Connect device).

## 4. Interactions to know

- **Watching a film**: the Shield remote's volume keys change the Phantom volume through
  LMS; play/pause, fast forward and rewind act on the film (the Shield receives them
  itself) while the music is not playing. Changing the volume from the Qobuz app or LMS
  web UI also changes the film's volume.
- **TV off (Shield asleep)**: the Shield remote is a music remote: volume, play/pause, next
  (fast forward), previous (rewind), mute (microphone). Measured: the keys are reported
  while the Shield sleeps, and none of these wakes it; its power-state query does not
  wake it either.
- **Keys that wake the Shield**: back, menu, the arrows and center button, the shortcut key,
  power. After such a key with the TV off, the Shield is awake: media keys and the
  microphone go to it (film / Assistant) until it sleeps again; put it back to sleep with
  power.
- **Automatic switch**: starting music on the Pi during a film may make the switch select
  the Pi; this is why fast forward / rewind act on the film, not on the music, while the TV
  is on and the music is not playing.
- **Startup volume**: the volume bridge sets LMS and the Phantom to its start volume
  (20 %) when it starts, including after a reboot. A Qobuz Connect session started just
  after boot therefore begins at that volume.
- **Takeover**: starting something else on the player from LMS (radio, library) releases
  the player in QobuzConnectLMS; the volume bridge keeps following the LMS volume
  regardless of the source.
- **Gapless and events** (QobuzConnectLMS) only concern the LMS playlist and state; they
  have no effect on the volume path.
- **Volume cap**: the Phantom never goes above the bridge's `max` (60), whatever the Qobuz
  app or the remote asks.
- **Remote media keys** (play/pause, next, previous) act on the LMS player: during a
  Qobuz Connect session, QobuzConnectLMS sees the LMS change through its events.
  Rewind restarts the track during a Qobuz Connect session: the LMS playlist only holds
  the current and next tracks, the previous one stays in the Qobuz queue.

## 5. Status

| Component | Status |
|---|---|
| QobuzConnectLMS (Qobuz Connect, gapless, LMS events) | Deployed and validated by listening, including after reboot |
| Volume bridge | **Deployed and validated** (replaces the first `phantom_bridge.py`, kept on the Pi as a fallback): split-line fix, mute, startup alignment of LMS and the Phantom to 20 %; restarts on its own after a reboot. Mute from the Qobuz app still to be checked |
| Shield (cinema) | HDMI fixed volume set, network debugging enabled, ADB key authorized; power state and remote keys read without waking it (measured) |
| Shield remote service | **Deployed and validated by use**: volume (music and film), play/pause on the film, rewind, double rewind → FIP favorite, microphone mute and volume while muted; restarts on its own after a reboot (connected to the Shield about 1 min after boot). First use showed a network connection to the Shield that hung without closing: fixed with a heartbeat, and volume up made safe (cap, repeat limit, steps from the Phantom's real volume) |
| Hue Tap Dial service | Kept as an alternative: implemented and tested without hardware; coordinator purchase on hold |
