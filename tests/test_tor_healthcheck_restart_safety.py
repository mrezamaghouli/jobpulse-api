"""Regression guard for the restart-stale Tor healthcheck defect.

Root cause: torrc.base's `Log notice file /var/log/tor/notices.log`
directive opens that file in append mode -- Tor never truncates it on
its own. A container's writable layer survives a plain `docker stop` /
`docker start` (unlike a full recreate), so a PRIOR Tor process's
"Bootstrapped 100% (done): Done" line can still be sitting in the file
when a NEW process starts. Since docker-compose.tor.yml's healthcheck
is a bare `grep -q "Bootstrapped 100%" ...` -- which matches anywhere in
the file regardless of which process wrote it -- a restarted container
could report healthy immediately, before the new Tor process has
bootstrapped at all.

These are structural/configuration-level checks against the entrypoint
script and compose healthcheck text, not a real Docker restart -- see
the Phase 1 runtime validation report for the live restart test against
the isolated Tor test container, and
tests/test_tor_real_integration.py::test_real_container_restart_does_not_report_stale_health
for the Phase 3 real-container proof of the same property.

Phase 3 note: entrypoint.sh now generates the resolved paths from
LOG_DIR/DATA_DIR/RUNTIME_DIR shell variables (RUNTIME_DIR/torrc moved off
/etc/tor so the container can run with a read-only root filesystem --
see docker/tor/README-secret-model.md and docker-compose.prod.tor.yml)
rather than hardcoding the literal paths inline. These tests check both
that the variables resolve to the same literal paths as before AND that
the same ordering invariants (mkdir before truncate, truncate before
chown, chown/chmod before `exec tor`) still hold against the new,
variable-based script.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT_PATH = REPO_ROOT / "docker" / "tor" / "entrypoint.sh"
COMPOSE_PATH = REPO_ROOT / "docker-compose.tor.yml"

ENTRYPOINT_SOURCE = ENTRYPOINT_PATH.read_text()
COMPOSE_SOURCE = COMPOSE_PATH.read_text()

NOTICES_LOG = '"$LOG_DIR/notices.log"'


def _index_of(substring: str, text: str = ENTRYPOINT_SOURCE) -> int:
    index = text.find(substring)
    assert index != -1, f"expected to find {substring!r} in entrypoint.sh"
    return index


def test_entrypoint_truncates_notices_log_before_starting_tor():
    """The exact mechanism doesn't matter (`: >`, `truncate`, `rm -f`
    followed by recreation, etc.) as long as SOME command clears the
    file's prior content before `exec tor` runs."""
    truncate_index = _index_of(NOTICES_LOG)
    exec_tor_index = _index_of("exec tor")

    assert truncate_index < exec_tor_index, (
        "notices.log must be cleared before the new Tor process starts writing to it"
    )


def test_notices_log_directory_exists_before_truncation():
    mkdir_index = _index_of("mkdir -p")
    truncate_index = _index_of(NOTICES_LOG)

    assert mkdir_index < truncate_index, (
        "/var/log/tor must exist before we can (re)create notices.log inside it"
    )


def test_ownership_is_reapplied_after_truncation():
    """Truncation happens as root (entrypoint runs as root); the fix
    must not leave notices.log root-owned when debian-tor (the user Tor
    drops privileges to) needs to write to it."""
    truncate_index = _index_of(NOTICES_LOG)
    chown_index = _index_of("chown -R debian-tor:debian-tor")

    assert truncate_index < chown_index, (
        "chown must run AFTER truncation so the recreated/truncated file "
        "ends up owned by debian-tor, not root"
    )
    assert '"$LOG_DIR"' in ENTRYPOINT_SOURCE[chown_index:chown_index + 80]
    # LOG_DIR must actually resolve to the real notices.log directory --
    # a variable name alone proves nothing without pinning its value too.
    assert "LOG_DIR=/var/log/tor" in ENTRYPOINT_SOURCE


def test_data_directory_permissions_unchanged():
    """This fix must not weaken DataDirectory's mode -- Tor refuses to
    start if it isn't 700."""
    assert 'chmod 700 "$DATA_DIR"' in ENTRYPOINT_SOURCE
    assert "DATA_DIR=/var/lib/tor" in ENTRYPOINT_SOURCE


def test_fix_does_not_touch_control_port_authentication():
    """Guard against scope creep: this is a readiness/log fix only."""
    assert "HashedControlPassword" in ENTRYPOINT_SOURCE
    assert "TOR_CONTROL_PASSWORD" in ENTRYPOINT_SOURCE


def test_fix_does_not_introduce_sleep_based_readiness():
    assert not re.search(r"\bsleep\b", ENTRYPOINT_SOURCE), (
        "readiness must remain event/state-driven (log content), not a fixed sleep"
    )


def test_compose_healthcheck_still_targets_current_process_semantics():
    """The healthcheck text itself is unchanged (still greps for the
    bootstrap line) -- restart-safety comes entirely from the log file
    now containing only the current process's lines, not from a
    healthcheck rewrite."""
    assert 'grep", "-q", "Bootstrapped 100%", "/var/log/tor/notices.log"' in COMPOSE_SOURCE


def test_compose_does_not_expose_additional_ports():
    assert "ports:" not in COMPOSE_SOURCE, (
        "the tor service must keep using `expose`, not `ports` -- no new host exposure"
    )
    assert 'expose:\n      - "9050"\n      - "9051"' in COMPOSE_SOURCE
