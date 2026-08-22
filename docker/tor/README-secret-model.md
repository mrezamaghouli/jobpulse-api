# Tor ControlPort secret model

## Preferred: `TOR_CONTROL_PASSWORD_FILE`

Production points this at a mounted secret file (e.g. a Docker Compose
`secrets:`-style bind mount, or `/run/secrets/tor_control_password`
sourced from `/opt/jobpulse/.tor_control_password`, mode 600, on the host
-- that real production file is never created by this repository). Only
the file PATH travels through the container's environment / `docker
inspect` output; the plaintext password itself never does.

## Fallback: `TOR_CONTROL_PASSWORD`

Local dev/CI only (kept for `docker-compose.tor.yml` compatibility).
`TOR_CONTROL_PASSWORD_FILE` wins whenever both are set.

## `tor --hash-password` and the residual exposure window

`docker/tor/entrypoint.sh` computes `HashedControlPassword` at container
boot by shelling out to Tor's own `tor --hash-password PASSWORD` CLI
subcommand (the standard, documented mechanism for producing a value for
`HashedControlPassword` in torrc -- this script does not reimplement or
invent an alternative hashing scheme).

As documented in Tor's own `--help`/man page, `--hash-password` takes the
password as a literal command-line argument -- there is no stdin- or
file-based variant of this specific flag. That means, for the sub-second
lifetime of the `tor --hash-password ...` subprocess this entrypoint
launches, the plaintext password is present as that subprocess's argv,
which is technically visible to:

- anything else running inside the same container's PID namespace during
  that instant (nothing else does: this container runs only this
  entrypoint and then execs into the long-lived `tor` process -- there is
  no other co-resident process to observe it), and
- the host's own `docker top <container>` / `/proc/<pid>/cmdline` for a
  privileged host operator during that same instant.

This is a real, bounded, documented residual exposure -- not eliminated,
because stock Tor provides no safer interface for this specific
operation. It is minimized by:

- reading the secret from a FILE, not an env var, right up until the
  point `tor --hash-password` is invoked;
- `unset`-ing both the plaintext (`TOR_CONTROL_PASSWORD`) and the
  resulting hash (`HASHED_PASSWORD`) shell variables immediately after
  use;
- never writing the plaintext to disk, never logging it, never passing it
  to any other process; and
- the exposure window closing entirely once `tor --hash-password` exits
  (before Tor itself has even started) -- it does not persist for the
  life of the running Tor process/container, only for that one
  short-lived helper subprocess at boot.

**Verification status**: CONFIRMED empirically against the exact image
built from this Dockerfile (Tor version 0.4.9.11, Debian bookworm-slim),
2026-08-22:

```
$ docker run --rm --entrypoint tor <image> --hash-password "somepassword"
16:909BEAF8C49683A360B920303095D3A39A70B4951CE15AAF8D6E88A22E

$ echo "somepassword" | docker run --rm -i --entrypoint tor <image> --hash-password
[warn] Command-line option '--hash-password' with no value. Failing.
[err] Reading config failed--see warnings above.

$ echo "somepassword" | docker run --rm -i --entrypoint tor <image> --hash-password -
16:0F331F7897D3C480603693AF2214EFA6802FBFF15FFEC1A02C9F679CD9
```

Omitting the argv value fails outright rather than falling back to
stdin; passing `-` as the argv value hashes the literal two-character
string `"-"` (a different, wrong hash) rather than reading stdin. Neither
is a usable stdin/file-based alternative. This confirms `--hash-password`
has no safer interface in this Tor version -- the argv-based residual
exposure window described above is real and not eliminable with stock
Tor tooling, only bounded as described.
