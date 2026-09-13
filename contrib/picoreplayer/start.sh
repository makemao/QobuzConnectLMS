#!/bin/sh
# QobuzConnectLMS - start the service (idempotent).
# Safe to call at boot from piCorePlayer "User commands" (runs as root, before LMS
# is up): it re-runs itself as tc, returns immediately, waits for LMS in the
# background and restarts the proxy if it crashes. Logs go to RAM (/tmp).

D=/mnt/mmcblk0p2/qconnect-lms
RUN=/tmp/qconnect-lms
LOG=$RUN/qconnect-lms.log

if [ "$(id -u)" = "0" ]; then
    exec su tc -c "$D/start.sh"
fi

mkdir -p "$RUN"
if [ -f "$RUN/supervisor.pid" ] && kill -0 "$(cat "$RUN/supervisor.pid")" 2>/dev/null; then
    echo "QobuzConnectLMS already running (supervisor pid $(cat "$RUN/supervisor.pid"))"
    exit 0
fi
rm -f "$RUN/stop"

(
    # Wait up to 10 minutes for LMS JSON-RPC (LMS gives an empty reply to GET: POST a real query)
    i=0
    while [ $i -lt 120 ] && ! curl -sf -m 3 -o /dev/null \
        -d '{"id":1,"method":"slim.request","params":["",["version","?"]]}' \
        http://127.0.0.1:9000/jsonrpc.js; do
        i=$((i + 1))
        sleep 5
    done
    while [ ! -f "$RUN/stop" ]; do
        echo "$(date '+%F %T') supervisor: starting qobuz-proxy" >> "$LOG"
        cd "$D/data" || exit 1
        QOBUZPROXY_DATA_DIR="$D/data" "$D/venv/bin/qobuz-proxy" --config "$D/data/config.yaml" >> "$LOG" 2>&1 &
        echo $! > "$RUN/proxy.pid"
        wait $!
        rc=$?
        echo "$(date '+%F %T') supervisor: qobuz-proxy exited ($rc)" >> "$LOG"
        # Keep the RAM log bounded
        if [ "$(wc -c < "$LOG")" -gt 5000000 ]; then
            tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
        fi
        [ -f "$RUN/stop" ] || sleep 10
    done
    rm -f "$RUN/proxy.pid" "$RUN/supervisor.pid"
) > /dev/null 2>&1 &
echo $! > "$RUN/supervisor.pid"
echo "QobuzConnectLMS starting (log: $LOG)"
