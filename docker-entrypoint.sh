#!/bin/sh
# Drop root privileges before starting the app — but only if the unprivileged
# user can actually write to the mounted uploads folder. On hosts where root in
# the container may not chown the bind mount (unprivileged LXC, NFS root_squash,
# SELinux, user namespaces…) we fall back to root instead of crashing.
set -e

if [ "$(id -u)" != "0" ]; then
    exec "$@"
fi

chown -R noder:noder /app/uploads 2>/dev/null || true

if setpriv --reuid=noder --regid=noder --init-groups \
        sh -c 'mkdir -p /app/uploads/pdf /app/uploads/audio && test -w /app/uploads/pdf && test -w /app/uploads/audio' 2>/dev/null; then
    # setpriv keeps HOME=/root, which the app user can't read (asyncpg looks for ~/.postgresql)
    export HOME=/home/noder
    exec setpriv --reuid=noder --regid=noder --init-groups "$@"
fi

echo "ADVARSEL: /app/uploads kan ikke skrives af brugeren 'noder' (uid 1000) — kører som root." >&2
echo "          Ret det på værten med:  chown -R 1000:1000 ./uploads" >&2
mkdir -p /app/uploads/pdf /app/uploads/audio
exec "$@"
