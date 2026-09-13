# Reference setup

The installation QobuzConnectLMS was built and validated on, and how it works together
with its companion project **[DevialetLMSBridge](https://github.com/makemao/DevialetLMSBridge)**
(volume of a Devialet Phantom driven by LMS, with an optional physical knob).
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
| Cinema source | **NVIDIA Shield** (Android TV 11) → **HDMI audio extractor** → **Full HD TV** | Off (asleep) most of the time. The extractor passes video to the TV over HDMI and sends the audio over optical to the switch. The Shield remote's volume keys change the Shield's own volume, not the Phantom's. *Network debugging* enabled so that the knob can pause a film (never used to wake it) |
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
- **Master control**: the Hue Tap Dial (planned) controls volume and mute of every source
  through LMS; play/pause targets the music when it plays, otherwise the Shield when it
  is awake ("TV on = film"); previous/next are music-only and never start music during a
  film. The Shield is only contacted on a button press and never woken up.
- **Optical isolation**: extra hardware on the Pi (for instance a USB Zigbee coordinator
  for the knob) cannot inject electrical noise into the speakers.
- During validation, Qobuz tracks in 16/44.1, 24/96 and 24/192 played through this chain.

## 2. Services on the Pi

| Service | Project | Role | Start at boot (piCorePlayer *User commands*) |
|---|---|---|---|
| LMS + squeezelite | piCorePlayer | music server and player | piCorePlayer |
| **QobuzConnectLMS** | this repo | player selectable in the Qobuz app (Qobuz Connect) | `/mnt/mmcblk0p2/qconnect-lms/start.sh` |
| **Volume bridge** | DevialetLMSBridge | LMS player volume → Phantom volume | `/mnt/mmcblk0p2/devialet-lms-bridge/start.sh` (planned, see §5) |
| **Tap Dial service** | DevialetLMSBridge | Hue Tap Dial knob → LMS commands | `/mnt/mmcblk0p2/devialet-lms-bridge/start.sh tapdial` (planned) |

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
Hue Tap Dial ──Zigbee──► tapdial.py ──JSON-RPC 9000──► LMS         ▼
                            │                                volume bridge ──IP Control API──► Phantom
                            │ network ADB (play/pause, only if awake)                            ▲
                            ▼                                                                    │
                      NVIDIA Shield ──HDMI──► HDMI audio extractor ──HDMI──► TV                  │
                                                   └──optical──► S/PDIF switch (auto) ──optical──┘
```

- **Everything goes through LMS.** QobuzConnectLMS and the Tap Dial only send LMS
  commands; they never talk to the Phantom. The volume bridge is the only component that
  talks to the Phantom.
- **Volume from the Qobuz app**: QobuzConnectLMS sets the LMS player volume
  (`mixer volume`); LMS emits `prefset server volume <n>`; the volume bridge forwards it
  to the Phantom (capped). The app, LMS web UI, the knob and the Phantom stay in sync.
- **Mute**: LMS stores a muted volume as a negative value; the bridge sets the Phantom
  to 0 and restores it on unmute.
- **LMS CLI (9090)** is used by several clients at once, each on its own connection:
  QobuzConnectLMS subscribes to `playlist,pause,mixer,client` events, the volume bridge to
  `prefset`. Subscriptions are per connection and do not interfere.
- **Ports on the Pi**: 9000 (LMS web / JSON-RPC), 9090 (LMS CLI), 8689 (QobuzConnectLMS web
  UI), 8690 (Qobuz Connect device).

## 4. Interactions to know

- **Watching a film**: the knob's dial and mute act on the film too (same Phantom volume);
  play/pause pauses the film when the Shield is awake and the music is not playing;
  previous/next do nothing. Changing the volume from the Qobuz app or LMS web UI also
  changes the film's volume.
- **Shield asleep**: the knob behaves as a music remote; the Shield is not contacted for
  volume or mute, and play/pause/previous/next only check its power state (read-only) —
  that this check does not wake it is to be confirmed on the device.
- **Shield remote volume**: today it changes the Shield's own output volume, not the
  Phantom's, so in cinema mode the Phantom volume has to be set from the Devialet app
  (or from LMS / the knob). Capturing the remote's volume keys is being studied.
- **Automatic switch**: starting music on the Pi during a film may make the switch select
  the Pi; this is why previous/next are ignored and play/pause targets the Shield while
  the TV is on.
- **Startup volume**: the volume bridge sets LMS and the Phantom to its start volume
  (20 %) when it starts, including after a reboot. A Qobuz Connect session started just
  after boot therefore begins at that volume.
- **Takeover**: starting something else on the player from LMS (radio, library) releases
  the player in QobuzConnectLMS; the volume bridge keeps following the LMS volume
  regardless of the source.
- **Gapless and events** (QobuzConnectLMS) only concern the LMS playlist and state; they
  have no effect on the volume path.
- **Volume cap**: the Phantom never goes above the bridge's `max` (60), whatever the Qobuz
  app or the knob asks.

## 5. Status

| Component | Status |
|---|---|
| QobuzConnectLMS (Qobuz Connect, gapless, LMS events) | Deployed and validated by listening, including after reboot |
| Volume bridge | The first version (`phantom_bridge.py`) runs in production; the DevialetLMSBridge rewrite (split-line fix, mute, startup alignment) is tested but **not deployed yet** |
| Shield (cinema) | Network debugging enabled, ADB key authorized, power state read successfully (read-only); cinema routing implemented and tested without the knob |
| Hue Tap Dial service | Implemented and tested without hardware; **coordinator not purchased yet** (Sonoff ZBDongle-P USB or SMLIGHT SLZB-06 network) |
