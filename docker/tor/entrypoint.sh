#!/bin/sh
# Computes ControlPort auth at container start from TOR_CONTROL_PASSWORD
# rather than baking a hashed password into the image, so the same image
# can be reused with a per-developer/per-CI-run secret. Local dev/testing
# only -- see docker/tor/torrc.base for scope notes.
set -eu

if [ -z "${TOR_CONTROL_PASSWORD:-}" ]; then
    echo "TOR_CONTROL_PASSWORD is required to start the local Tor test instance." >&2
    exit 1
fi

HASHED_PASSWORD=$(tor --hash-password "$TOR_CONTROL_PASSWORD" | tail -n 1)

{
    cat /etc/tor/torrc.base
    echo "HashedControlPassword $HASHED_PASSWORD"
} > /etc/tor/torrc

# torrc.base sets `User debian-tor` so Tor drops privileges after
# binding its listener sockets -- DataDirectory must already be owned by
# that user or Tor refuses to start. Explicit and idempotent regardless
# of what the debian tor package's own postinst already set up.
mkdir -p /var/lib/tor /var/log/tor

# The container's writable layer survives a plain `docker stop`/`start`
# (unlike a full recreate), so a prior Tor process's "Bootstrapped 100%"
# line can still be sitting in this file when a new process starts. Tor
# opens `Log notice file` targets in append mode and never truncates on
# its own -- without this, the healthcheck (which greps this file) can
# report healthy for the OLD process's bootstrap before the new one has
# bootstrapped at all. Truncate/recreate it here, before Tor is exec'd,
# so every line in it can only ever belong to the current process.
: > /var/log/tor/notices.log

chown -R debian-tor:debian-tor /var/lib/tor /var/log/tor
chmod 700 /var/lib/tor

exec tor -f /etc/tor/torrc
