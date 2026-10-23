#!/bin/sh
# Fix ownership of the mounted uploads folder (older versions wrote it as root),
# then drop root privileges before starting the app.
set -e
if [ "$(id -u)" = "0" ]; then
    chown -R noder:noder /app/uploads 2>/dev/null || true
    exec setpriv --reuid=noder --regid=noder --init-groups "$@"
fi
exec "$@"
