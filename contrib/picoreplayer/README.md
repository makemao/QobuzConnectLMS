# Running QobuzConnectLMS on piCorePlayer

This guide installs QobuzConnectLMS **on the piCorePlayer box that runs LMS**, with
nothing installed into the system: everything lives in one folder on the persistent
SD card partition, and removing that folder (plus one boot setting) removes it all.

Tested on piCorePlayer 10.0 (Raspberry Pi 4, aarch64) with its bundled Python 3.11,
LMS 9.1.1 and the Qobuz plugin 3.7.1.

## Prerequisites

- piCorePlayer with **LMS running on the same box** and the **Qobuz plugin logged in**.
- Python 3.10+ available as `python3` (check with `python3 --version` over SSH). If it
  is missing, install the `python3.11.tcz` extension from the piCorePlayer web UI
  (*Main page* → *Extensions*) so it is loaded at boot.
- Internet access from the box (to download the code and Python packages).
- SSH access as `tc`.

## Layout

```
/mnt/mmcblk0p2/qconnect-lms/
├── app/          code
├── venv/         dedicated Python environment (~60 MB)
├── data/         config.yaml + Qobuz token cache (private)
├── start.sh      start (idempotent, waits for LMS, restarts on crash)
├── stop.sh       stop
└── status.sh     status + last log lines
```

Logs are kept in RAM (`/tmp/qconnect-lms/`) to spare the SD card; they are lost at
reboot.

## Install

Over SSH as `tc`:

```sh
D=/mnt/mmcblk0p2/qconnect-lms
sudo mkdir $D && sudo chown tc:staff $D
mkdir -p $D/data && cd $D

# Code
curl -fsSL -o /tmp/src.tar.gz https://codeload.github.com/makemao/QobuzConnectLMS/tar.gz/refs/heads/main
mkdir app && tar -xzf /tmp/src.tar.gz -C app --strip-components=1 && rm /tmp/src.tar.gz

# Python environment (prebuilt wheels only: no compiler needed)
python3 -m venv venv
./venv/bin/pip install --no-cache-dir --only-binary=:all: ./app

# Service scripts
cp app/contrib/picoreplayer/*.sh . && chmod 755 *.sh
chmod 700 data
```

Find your player's MAC address (or use its exact name):

```sh
curl -s -d '{"id":1,"method":"slim.request","params":["",["players","0","50"]]}' \
  http://127.0.0.1:9000/jsonrpc.js
```

Create `data/config.yaml` (one entry per player to expose):

```yaml
qobuz:
  max_quality: 27
server:
  http_port: 8689
logging:
  level: info
speakers:
  - name: "Living Room (LMS)"
    backend: lms
    lms_host: 127.0.0.1
    lms_port: 9000
    lms_player: "00:11:22:33:44:55"
```

```sh
chmod 600 data/config.yaml
./start.sh
./status.sh
```

Then open **http://\<pcp-address\>:8689**, click **Log in to Qobuz**, and pick the
device in the Qobuz app.

## Start at boot

piCorePlayer web UI → *Tweaks* → *User commands*: put the following in a free slot
and save:

```
/mnt/mmcblk0p2/qconnect-lms/start.sh
```

User commands run as root before LMS starts; `start.sh` re-runs itself as `tc`,
returns immediately and waits (up to 10 minutes) for LMS JSON-RPC before starting
the proxy.

If you edit `/usr/local/etc/pcp/pcp.cfg` by hand instead of using the web UI, run
`sudo pcp bu` afterwards so the change survives a reboot.

## Operate

```sh
cd /mnt/mmcblk0p2/qconnect-lms
./status.sh 50     # state + last 50 log lines
./stop.sh
./start.sh
```

## Update

```sh
cd /mnt/mmcblk0p2/qconnect-lms
./stop.sh
curl -fsSL -o /tmp/src.tar.gz https://codeload.github.com/makemao/QobuzConnectLMS/tar.gz/refs/heads/main
rm -rf app && mkdir app && tar -xzf /tmp/src.tar.gz -C app --strip-components=1 && rm /tmp/src.tar.gz
./venv/bin/pip install --no-cache-dir --only-binary=:all: ./app
cp app/contrib/picoreplayer/*.sh . && chmod 755 *.sh
./start.sh
```

## Uninstall

1. `/mnt/mmcblk0p2/qconnect-lms/stop.sh`
2. Remove the line from *Tweaks* → *User commands* and save.
3. `sudo rm -rf /mnt/mmcblk0p2/qconnect-lms`

## Troubleshooting

- **`not running` and no log line after boot**: LMS was not reachable within 10
  minutes, or the user command is not set. Run `./start.sh` by hand and check
  `./status.sh`.
- **`LMS player '…' not found`**: wrong MAC/name in `data/config.yaml`.
- **Device not in the Qobuz app**: phone and piCorePlayer must be on the same LAN;
  piCorePlayer's own mDNS responder (`pcpmdnsd`) can stay running alongside.
- **Gap between tracks**: check that the log shows `Gapless: queued next track in LMS`
  and `Gapless: LMS moved to the next track`:
  `grep Gapless /tmp/qconnect-lms/qconnect-lms.log`.
