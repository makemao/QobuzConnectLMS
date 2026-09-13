#!/bin/sh
# QobuzConnectLMS - status and last log lines.
RUN=/tmp/qconnect-lms
if [ -f "$RUN/proxy.pid" ] && kill -0 "$(cat "$RUN/proxy.pid")" 2>/dev/null; then
    echo "running (pid $(cat "$RUN/proxy.pid"))"
else
    echo "not running"
fi
tail -n "${1:-20}" "$RUN/qconnect-lms.log" 2>/dev/null
