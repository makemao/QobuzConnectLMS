#!/bin/sh
# QobuzConnectLMS - stop the service and its supervisor.
RUN=/tmp/qconnect-lms
mkdir -p "$RUN"
touch "$RUN/stop"
for f in supervisor.pid proxy.pid; do
    [ -f "$RUN/$f" ] && kill "$(cat "$RUN/$f")" 2>/dev/null
done
sleep 2
[ -f "$RUN/proxy.pid" ] && kill -9 "$(cat "$RUN/proxy.pid")" 2>/dev/null
rm -f "$RUN/proxy.pid" "$RUN/supervisor.pid"
echo "QobuzConnectLMS stopped"
