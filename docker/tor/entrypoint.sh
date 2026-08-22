#!/bin/sh
# Computes ControlPort auth at container start rather than baking a
# hashed password into the image, so the same image can be reused with a
# per-environment secret. Two secret sources are supported:
#
#   TOR_CONTROL_PASSWORD_FILE -- preferred. Path to a file (e.g. a
#   Docker/Compose secret mount) containing the plaintext password. Never
#   required to be present as a plaintext environment variable value --
#   only a FILE PATH (not a secret itself) travels through the container
#   environment/`docker inspect`.
#
#   TOR_CONTROL_PASSWORD -- local dev/CI fallback only, kept for
#   docker-compose.tor.yml compatibility. Production should use
#   TOR_CONTROL_PASSWORD_FILE instead.
#
# Residual exposure (documented, not eliminated): Tor's own `--hash-password
# PASSWORD` CLI subcommand has no stdin/file-based variant in its
# documented interface (verified against `tor --hash-password --help`
# output -- see docker/tor/README-secret-model.md), so the plaintext
# password necessarily appears, briefly, as an argv value of the
# `tor --hash-password` subprocess this script launches. That exposure
# window is bounded to this one subprocess's lifetime (sub-second, at
# container boot, before the long-lived `tor` daemon process ever
# starts), and is only observable to something already inside this
# container's PID namespace or the host's `docker top`/`/proc` -- this
# container runs nothing but Tor, so nothing else is co-resident to read
# it. The plaintext is never written to disk, never logged, and both
# shell variables holding it are unset immediately after use.
set -eu

RUNTIME_DIR=/run/tor
DATA_DIR=/var/lib/tor
LOG_DIR=/var/log/tor
TORRC_PATH="$RUNTIME_DIR/torrc"

# Generated torrc now lives under /run/tor (a dedicated writable
# tmpfs/volume mount -- see docker-compose.prod.tor.yml), not
# /etc/tor/torrc as before. /etc/tor only ever needs to be READ (it holds
# the image-baked torrc.base), which is what lets the container run with
# a read-only root filesystem plus a small, explicit set of writable
# mounts, instead of requiring the whole image writable just so this one
# generated file can be created at boot.
mkdir -p "$RUNTIME_DIR" "$DATA_DIR" "$LOG_DIR"

if [ -n "${TOR_CONTROL_PASSWORD_FILE:-}" ]; then
    if [ ! -r "$TOR_CONTROL_PASSWORD_FILE" ]; then
        echo "TOR_CONTROL_PASSWORD_FILE is set but not readable: $TOR_CONTROL_PASSWORD_FILE" >&2
        exit 1
    fi
    TOR_CONTROL_PASSWORD="$(cat "$TOR_CONTROL_PASSWORD_FILE")"
elif [ -n "${TOR_CONTROL_PASSWORD:-}" ]; then
    : # dev/CI fallback -- value already in TOR_CONTROL_PASSWORD from the environment.
else
    echo "Either TOR_CONTROL_PASSWORD_FILE or TOR_CONTROL_PASSWORD is required to start Tor." >&2
    exit 1
fi

if [ -z "$TOR_CONTROL_PASSWORD" ]; then
    echo "Resolved Tor control password is empty (empty file or empty env value)." >&2
    exit 1
fi

HASHED_PASSWORD=$(tor --hash-password "$TOR_CONTROL_PASSWORD" | tail -n 1)
unset TOR_CONTROL_PASSWORD

{
    cat /etc/tor/torrc.base
    echo "HashedControlPassword $HASHED_PASSWORD"
} > "$TORRC_PATH"
unset HASHED_PASSWORD

# torrc.base sets `User debian-tor` so Tor drops privileges after
# binding its listener sockets -- DataDirectory must already be owned by
# that user or Tor refuses to start. Explicit and idempotent regardless
# of what the debian tor package's own postinst already set up.

# The container's writable layer survives a plain `docker stop`/`start`
# (unlike a full recreate), so a prior Tor process's "Bootstrapped 100%"
# line can still be sitting in this file when a new process starts. Tor
# opens `Log notice file` targets in append mode and never truncates on
# its own -- without this, the healthcheck (which greps this file) can
# report healthy for the OLD process's bootstrap before the new one has
# bootstrapped at all. Truncate/recreate it here, before Tor is exec'd,
# so every line in it can only ever belong to the current process.
: > "$LOG_DIR/notices.log"

chown -R debian-tor:debian-tor "$DATA_DIR" "$LOG_DIR" "$RUNTIME_DIR"
chmod 700 "$DATA_DIR"

exec tor -f "$TORRC_PATH"
