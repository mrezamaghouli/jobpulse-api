"""Tests for scripts/tor/circuit_manager.py -- the Phase 1 manually
controlled circuit lifecycle (two-tier advisory-lock-guarded NEWNYM +
persisted state). No real Docker, PostgreSQL, or Tor process is ever
used: psycopg2 and stem.Controller are the only patched symbols,
mirroring the mocking pattern already used in
tests/test_collector_outcomes.py for scripts/collector_postgres.py.

What these tests guard against:
  - two processes mutating the same circuit_key at once
  - two different circuit_key values sending NEWNYM to the same Tor
    instance (control host:port) at once -- NEWNYM is process-scoped,
    not circuit-scoped, so this needs its own lock tier
  - a failed NEWNYM silently leaving the circuit marked "ready"
  - shared-instance contention being mistaken for a circuit fault
    (quarantining a healthy circuit just because another one was mid-NEWNYM)
  - either advisory lock being left held after a failure
  - an unlock failure masking the real error that was propagating
  - NEWNYM retrying forever instead of raising after a bounded number of
    attempts
  - the control password leaking into a raised exception's message
  - unvalidated column names reaching the dynamic UPDATE statement
  - NEWNYM being sent again before Tor's cooldown interval has elapsed,
    across SEPARATE request_new_identity() invocations/connections
    (stem's own Controller.is_newnym_available()/get_newnym_wait() reset
    per connection -- verified against installed stem 1.8.2 source --
    so only a persisted cross-invocation timer can catch this)
  - an active cooldown becoming an unbounded sleep or a busy retry loop
  - cooldown contention being mistaken for a circuit fault
"""
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.tor.circuit_manager as cm
from scripts.tor.local_http_simulator import (
    LocalHttpSimulator,
    SimulatedConnectionFailure,
    SimulatedTimeout,
    always_403,
    always_timeout,
    rate_limited_after,
)


class FakeCursor:
    def __init__(self, deny_lock_keys=None, last_newnym_at=None, raise_on_sql_substring=None):
        self.executed = []
        self.deny_lock_keys = deny_lock_keys or set()
        self.last_newnym_at = last_newnym_at
        # When set, execute() raises RuntimeError the moment a statement
        # containing this substring runs -- used to prove emit_event()'s
        # transactional rollback (see test_emit_event_rolls_back_*).
        self.raise_on_sql_substring = raise_on_sql_substring

    def execute(self, sql, params=None):
        if self.raise_on_sql_substring and self.raise_on_sql_substring in sql:
            raise RuntimeError(f"simulated failure executing statement containing {self.raise_on_sql_substring!r}")
        self.executed.append((sql, params))

    def fetchone(self):
        sql, params = self.executed[-1]

        if "pg_try_advisory_lock" in sql:
            key = params[0]
            return (key not in self.deny_lock_keys,)

        if "pg_advisory_unlock" in sql:
            return (True,)

        if "SELECT last_newnym_at FROM tor_instances" in sql:
            return (self.last_newnym_at,)

        return None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, deny_lock_keys=None, last_newnym_at=None, raise_on_sql_substring=None):
        self.cursor_obj = FakeCursor(
            deny_lock_keys=deny_lock_keys, last_newnym_at=last_newnym_at,
            raise_on_sql_substring=raise_on_sql_substring,
        )
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True

    @property
    def executed(self):
        return self.cursor_obj.executed


def statuses_set(fake_conn):
    """Every status value this connection's UPDATE statements ACTUALLY
    set, in order -- skips UPDATE tor_circuits statements that don't
    touch the status column at all (e.g. verify_circuit()'s success/
    failure paths and _record_genuine_failure(quarantine=False), which
    by design never change status)."""
    result = []
    for sql, params in fake_conn.executed:
        if sql.strip().startswith("UPDATE tor_circuits") and "status = %s" in sql:
            result.append(params[0])
    return result


def unlock_call_count(fake_conn, lock_scope):
    key = cm._advisory_lock_key(lock_scope)
    return sum(
        1
        for sql, params in fake_conn.executed
        if "pg_advisory_unlock" in sql and params[0] == key
    )


# =====================================================================
# Pure helpers
# =====================================================================

def test_advisory_lock_key_is_deterministic():
    assert cm._advisory_lock_key("default") == cm._advisory_lock_key("default")


def test_advisory_lock_key_differs_per_scope():
    assert cm._advisory_lock_key("default") != cm._advisory_lock_key("linkedin")


def test_instance_lock_key_differs_by_host_and_port():
    a = cm._instance_lock_key("127.0.0.1", 9051)
    b = cm._instance_lock_key("127.0.0.1", 9052)
    c = cm._instance_lock_key("other-host", 9051)
    assert len({a, b, c}) == 3


def test_instance_lock_key_is_distinguishable_from_a_plausible_circuit_key():
    # Guards against the instance lock scope accidentally colliding with
    # an application-chosen circuit_key string.
    assert cm._instance_lock_key("127.0.0.1", 9051) != "default"


def test_redact_secret_replaces_password():
    assert cm._redact_secret("auth failed for hunter2", "hunter2") == "auth failed for ***REDACTED***"


def test_redact_secret_noop_when_no_secret_configured():
    assert cm._redact_secret("auth failed", "") == "auth failed"


def test_try_acquire_advisory_lock_true():
    conn = FakeConnection()
    assert cm.try_acquire_advisory_lock(conn, "default") is True


def test_try_acquire_advisory_lock_false_when_contended():
    conn = FakeConnection(deny_lock_keys={cm._advisory_lock_key("default")})
    assert cm.try_acquire_advisory_lock(conn, "default") is False


# =====================================================================
# _set_circuit_status: dynamic SQL column allowlist
# =====================================================================

def test_set_circuit_status_rejects_unknown_field_names():
    conn = FakeConnection()

    with pytest.raises(ValueError):
        cm._set_circuit_status(conn, "default", cm.STATUS_READY, drop_table_jobs="x")

    # Nothing should have been executed -- validation happens before SQL.
    assert conn.executed == []


def test_set_circuit_status_accepts_allowed_fields():
    conn = FakeConnection()
    cm._set_circuit_status(conn, "default", cm.STATUS_READY, request_count=0, last_exit_ip="1.2.3.4")
    assert conn.commit_count == 1
    sql, params = conn.executed[-1]
    assert "request_count = %s" in sql
    assert "last_exit_ip = %s" in sql


# =====================================================================
# rotate_circuit: happy path
# =====================================================================

def test_rotate_circuit_success_marks_ready_with_observed_ip():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.rotate_circuit(circuit_key="default", verify_fn=lambda: "1.2.3.4")

    assert fake_newnym.called
    assert fake_newnym.call_args.args[0] is fake_conn  # connection is threaded through
    assert result["status"] == cm.STATUS_READY
    assert result["exit_ip"] == "1.2.3.4"
    assert statuses_set(fake_conn) == [cm.STATUS_DRAINING, cm.STATUS_READY]
    assert fake_conn.closed is True


def test_rotate_circuit_releases_circuit_lock_on_success():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        cm.rotate_circuit(circuit_key="default")

    assert unlock_call_count(fake_conn, "default") == 1


def test_rotate_circuit_without_verify_fn_leaves_exit_ip_none():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.rotate_circuit(circuit_key="default")

    assert result["exit_ip"] is None


# =====================================================================
# rotate_circuit: circuit_key lock contention
# =====================================================================

def test_rotate_circuit_raises_when_circuit_lock_already_held():
    fake_conn = FakeConnection(deny_lock_keys={cm._advisory_lock_key("default")})

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.CircuitLockError):
            cm.rotate_circuit(circuit_key="default")

    assert not fake_newnym.called
    # No state-changing UPDATE should have run without the lock.
    assert statuses_set(fake_conn) == []


# =====================================================================
# rotate_circuit: NEWNYM failure (genuine fault -> quarantine)
# =====================================================================

def test_rotate_circuit_quarantines_and_releases_lock_on_newnym_failure():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.CircuitRotationError("control port unreachable"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.CircuitRotationError):
            cm.rotate_circuit(circuit_key="default")

    assert statuses_set(fake_conn) == [cm.STATUS_DRAINING, cm.STATUS_QUARANTINED]
    assert unlock_call_count(fake_conn, "default") == 1, "circuit lock must be released even when NEWNYM fails"


def test_rotate_circuit_quarantines_on_verify_fn_failure():
    fake_conn = FakeConnection()

    def failing_verify():
        raise RuntimeError("exit IP check failed")

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(RuntimeError):
            cm.rotate_circuit(circuit_key="default", verify_fn=failing_verify)

    assert statuses_set(fake_conn) == [cm.STATUS_DRAINING, cm.STATUS_QUARANTINED]


# =====================================================================
# rotate_circuit: Tor-instance contention is NOT a circuit fault
# =====================================================================

def test_rotate_circuit_reverts_to_ready_not_quarantined_on_instance_busy():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.TorInstanceBusyError("instance busy"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.TorInstanceBusyError):
            cm.rotate_circuit(circuit_key="default")

    # DRAINING then back to READY -- never QUARANTINED for contention.
    assert statuses_set(fake_conn) == [cm.STATUS_DRAINING, cm.STATUS_READY]
    assert unlock_call_count(fake_conn, "default") == 1


# =====================================================================
# rotate_circuit: unlock failure must not mask the real error
# =====================================================================

def test_unlock_failure_does_not_mask_the_original_exception():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.CircuitRotationError("newnym exploded"),
         ), \
         mock.patch.object(
             cm, "release_advisory_lock",
             side_effect=RuntimeError("connection already closed"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.CircuitRotationError, match="newnym exploded"):
            cm.rotate_circuit(circuit_key="default")

    # Even though the connection is still "closed" in the outer finally.
    assert fake_conn.closed is True


# =====================================================================
# request_new_identity: two-tier locking, bounded retries, redaction
# =====================================================================

def _controller_cm(controller_mock):
    context = mock.MagicMock()
    context.__enter__.return_value = controller_mock
    context.__exit__.return_value = False
    return context


def test_request_new_identity_sends_newnym_after_authenticating(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "test-password")
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    fake_conn = FakeConnection()
    controller = mock.MagicMock()

    with mock.patch.object(cm, "Controller") as fake_controller_cls:
        fake_controller_cls.from_port.return_value = _controller_cm(controller)

        cm.request_new_identity(fake_conn)

    controller.authenticate.assert_called_once_with(password="test-password")
    controller.signal.assert_called_once_with(cm.Signal.NEWNYM)

    instance_scope = cm._instance_lock_key("127.0.0.1", 9051)
    assert unlock_call_count(fake_conn, instance_scope) == 1


def test_request_new_identity_raises_busy_without_touching_control_port_when_instance_locked(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    instance_scope = cm._instance_lock_key("127.0.0.1", 9051)
    fake_conn = FakeConnection(deny_lock_keys={cm._advisory_lock_key(instance_scope)})

    with mock.patch.object(cm, "Controller") as fake_controller_cls:
        with pytest.raises(cm.TorInstanceBusyError):
            cm.request_new_identity(fake_conn)

    assert not fake_controller_cls.from_port.called


def test_request_new_identity_retries_bounded_then_raises(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "")
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    fake_conn = FakeConnection()

    with mock.patch.object(cm, "Controller") as fake_controller_cls:
        fake_controller_cls.from_port.side_effect = OSError("connection refused")

        with pytest.raises(cm.CircuitRotationError):
            cm.request_new_identity(fake_conn, max_retries=3)

    assert fake_controller_cls.from_port.call_count == 3


def test_request_new_identity_redacts_password_from_raised_error(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "super-secret-value")
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    fake_conn = FakeConnection()

    with mock.patch.object(cm, "Controller") as fake_controller_cls:
        fake_controller_cls.from_port.side_effect = RuntimeError(
            "auth failed with password super-secret-value"
        )

        with pytest.raises(cm.CircuitRotationError) as exc_info:
            cm.request_new_identity(fake_conn, max_retries=1)

    assert "super-secret-value" not in str(exc_info.value)
    assert "REDACTED" in str(exc_info.value)


def test_request_new_identity_releases_instance_lock_even_on_failure(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    fake_conn = FakeConnection()
    instance_scope = cm._instance_lock_key("127.0.0.1", 9051)

    with mock.patch.object(cm, "Controller") as fake_controller_cls:
        fake_controller_cls.from_port.side_effect = OSError("connection refused")

        with pytest.raises(cm.CircuitRotationError):
            cm.request_new_identity(fake_conn, max_retries=1)

    assert unlock_call_count(fake_conn, instance_scope) == 1


# =====================================================================
# request_new_identity: persisted cross-invocation NEWNYM cooldown
#
# stem's Controller.is_newnym_available()/get_newnym_wait() track a
# `_last_newnym` timestamp that lives on the Python Controller object
# and is reset to 0.0 in __init__ (confirmed against the installed stem
# 1.8.2 source) -- so a fresh connection always reports "available",
# regardless of what a DIFFERENT prior connection/process did. Since
# request_new_identity() opens a fresh connection every call, that
# in-object bookkeeping alone cannot detect cooldown across separate
# invocations. These tests exercise the persisted tor_instances timer
# that actually does.
# =====================================================================

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _patch_now(monkeypatch, now=FIXED_NOW):
    monkeypatch.setattr(cm, "_utc_now", lambda: now)


def test_request_new_identity_no_wait_when_newnym_immediately_available(monkeypatch):
    """No prior NEWNYM on record -> zero cooldown -> no sleep at all."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    _patch_now(monkeypatch)

    fake_conn = FakeConnection(last_newnym_at=None)
    controller = mock.MagicMock()

    with mock.patch.object(cm, "Controller") as fake_controller_cls, \
         mock.patch.object(cm, "time") as fake_time:
        fake_controller_cls.from_port.return_value = _controller_cm(controller)

        cm.request_new_identity(fake_conn)

    assert not fake_time.sleep.called
    controller.signal.assert_called_once_with(cm.Signal.NEWNYM)


def test_request_new_identity_sleeps_bounded_amount_when_cooldown_below_max(monkeypatch):
    """NEWNYM temporarily unavailable, but the remaining wait fits under
    max_wait_seconds -> a single bounded sleep, then NEWNYM proceeds."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MIN_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "5")
    _patch_now(monkeypatch)

    # Last NEWNYM was 7s ago -> 3s remaining, fits under max_wait=5.
    fake_conn = FakeConnection(last_newnym_at=FIXED_NOW - timedelta(seconds=7))
    controller = mock.MagicMock()

    with mock.patch.object(cm, "Controller") as fake_controller_cls, \
         mock.patch.object(cm, "time") as fake_time:
        fake_controller_cls.from_port.return_value = _controller_cm(controller)

        cm.request_new_identity(fake_conn)

    fake_time.sleep.assert_called_once()
    slept_seconds = fake_time.sleep.call_args.args[0]
    assert 2.9 <= slept_seconds <= 3.1
    controller.signal.assert_called_once_with(cm.Signal.NEWNYM)


def test_request_new_identity_raises_cooldown_error_when_wait_exceeds_max(monkeypatch):
    """Remaining wait exceeds max_wait_seconds -> raise immediately,
    never sleep, never touch the control port at all."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MIN_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "5")
    _patch_now(monkeypatch)

    # Last NEWNYM was 1s ago -> 9s remaining, exceeds max_wait=5.
    fake_conn = FakeConnection(last_newnym_at=FIXED_NOW - timedelta(seconds=1))

    with mock.patch.object(cm, "Controller") as fake_controller_cls, \
         mock.patch.object(cm, "time") as fake_time:
        with pytest.raises(cm.NewnymCooldownError):
            cm.request_new_identity(fake_conn)

    assert not fake_time.sleep.called
    assert not fake_controller_cls.from_port.called


def test_request_new_identity_cooldown_exceeded_does_not_retry_or_busy_wait(monkeypatch):
    """Even with a generous max_retries, an over-budget cooldown fails
    once and immediately -- no retry loop, no busy-wait spin."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MIN_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "5")
    _patch_now(monkeypatch)

    fake_conn = FakeConnection(last_newnym_at=FIXED_NOW - timedelta(seconds=1))

    with mock.patch.object(cm, "Controller") as fake_controller_cls, \
         mock.patch.object(cm, "time") as fake_time:
        with pytest.raises(cm.NewnymCooldownError):
            cm.request_new_identity(fake_conn, max_retries=5)

    assert fake_controller_cls.from_port.call_count == 0
    assert fake_time.sleep.call_count == 0


def test_request_new_identity_releases_instance_lock_when_cooldown_exceeded(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MIN_INTERVAL_SECONDS", "10")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "5")
    _patch_now(monkeypatch)

    fake_conn = FakeConnection(last_newnym_at=FIXED_NOW - timedelta(seconds=1))
    instance_scope = cm._instance_lock_key("127.0.0.1", 9051)

    with mock.patch.object(cm, "Controller"), \
         mock.patch.object(cm, "time"):
        with pytest.raises(cm.NewnymCooldownError):
            cm.request_new_identity(fake_conn)

    assert unlock_call_count(fake_conn, instance_scope) == 1


def test_rotate_circuit_reverts_to_ready_not_quarantined_on_cooldown():
    """Cooldown contention is timing, not a circuit fault -- must not be
    treated the same as a genuine NEWNYM/verification failure."""
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.NewnymCooldownError("cooldown active"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.NewnymCooldownError):
            cm.rotate_circuit(circuit_key="default")

    assert statuses_set(fake_conn) == [cm.STATUS_DRAINING, cm.STATUS_READY]
    assert unlock_call_count(fake_conn, "default") == 1


def test_request_new_identity_respects_live_stem_check_within_bound(monkeypatch):
    """Defensive secondary check: even though a fresh connection's own
    is_newnym_available() is a structural no-op today (see module
    docstring), the code path that would consult it is still correct if
    it were ever to report unavailable within bounds."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "5")
    _patch_now(monkeypatch)

    fake_conn = FakeConnection(last_newnym_at=None)
    controller = mock.MagicMock()
    controller.is_newnym_available.return_value = False
    controller.get_newnym_wait.return_value = 2.0

    with mock.patch.object(cm, "Controller") as fake_controller_cls, \
         mock.patch.object(cm, "time") as fake_time:
        fake_controller_cls.from_port.return_value = _controller_cm(controller)

        cm.request_new_identity(fake_conn)

    fake_time.sleep.assert_called_once_with(2.0)
    controller.signal.assert_called_once_with(cm.Signal.NEWNYM)


def test_request_new_identity_live_stem_check_exceeding_max_raises_cooldown(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "5")
    _patch_now(monkeypatch)

    fake_conn = FakeConnection(last_newnym_at=None)
    controller = mock.MagicMock()
    controller.is_newnym_available.return_value = False
    controller.get_newnym_wait.return_value = 20.0

    with mock.patch.object(cm, "Controller") as fake_controller_cls, \
         mock.patch.object(cm, "time") as fake_time:
        fake_controller_cls.from_port.return_value = _controller_cm(controller)

        with pytest.raises(cm.NewnymCooldownError):
            cm.request_new_identity(fake_conn)

    assert not controller.signal.called
    assert not fake_time.sleep.called


# =====================================================================
# get_circuit_state
# =====================================================================

def test_get_circuit_state_returns_none_when_absent():
    fake_conn = FakeConnection()
    fake_conn.cursor_obj.fetchone = lambda: None

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2:
        fake_psycopg2.connect.return_value = fake_conn

        assert cm.get_circuit_state("default") is None


def test_get_circuit_state_returns_persisted_fields():
    fake_conn = FakeConnection()
    fake_conn.cursor_obj.fetchone = lambda: (
        "default", cm.STATUS_READY, 0, "1.2.3.4", None, None,
        0, 0, None, None, None,
    )

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2:
        fake_psycopg2.connect.return_value = fake_conn

        state = cm.get_circuit_state("default")

    assert state == {
        "circuit_key": "default",
        "status": cm.STATUS_READY,
        "request_count": 0,
        "last_exit_ip": "1.2.3.4",
        "last_rotated_at": None,
        "cooldown_until": None,
        "failure_count": 0,
        "consecutive_failure_count": 0,
        "last_error_category": None,
        "last_verified_at": None,
        "updated_at": None,
    }


# =====================================================================
# DDL structural sanity checks -- NOT PostgreSQL validation
#
# These are plain string assertions against our own SQL constants. They
# catch an accidental typo/edit (e.g. a column silently dropped, a
# UNIQUE constraint removed from the ON CONFLICT target) but prove
# NOTHING about whether the SQL is syntactically valid PostgreSQL, runs
# correctly against PostgreSQL 16, or behaves correctly under real
# concurrent sessions/advisory locks/TIMESTAMPTZ adaptation via
# psycopg2. No local PostgreSQL 16 binaries are available in this
# environment (command -v postgres/initdb/pg_ctl/psql all fail, same as
# tests/test_upsert_returning_integration.py's own skip condition) --
# real execution of tor_circuits/tor_instances DDL against PostgreSQL 16
# remains UNVERIFIED until a disposable PostgreSQL 16 environment (local
# or the CI service container) actually runs it.
# =====================================================================

def test_ddl_uses_idempotent_create_table_if_not_exists():
    assert "CREATE TABLE IF NOT EXISTS tor_circuits" in cm.CREATE_TOR_CIRCUITS_TABLE_SQL
    assert "CREATE TABLE IF NOT EXISTS tor_instances" in cm.CREATE_TOR_INSTANCES_TABLE_SQL


def test_ddl_timestamp_columns_are_timezone_aware():
    """Regression guard for the earlier TIMESTAMP/TIMESTAMPTZ mismatch:
    _utc_now() produces timezone-aware datetimes, so every timestamp
    column that stores one of its values must be TIMESTAMPTZ, not the
    bare TIMESTAMP type (which would silently drop/misinterpret tzinfo
    per the session's TimeZone setting)."""
    for column in ("last_rotated_at", "cooldown_until", "created_at", "updated_at"):
        assert f"{column} TIMESTAMPTZ" in cm.CREATE_TOR_CIRCUITS_TABLE_SQL, column

    for column in ("last_newnym_at", "created_at", "updated_at"):
        assert f"{column} TIMESTAMPTZ" in cm.CREATE_TOR_INSTANCES_TABLE_SQL, column


def test_ddl_unique_constraint_matches_on_conflict_target():
    """ON CONFLICT (col) DO NOTHING requires col to actually carry a
    UNIQUE (or PK) constraint, or PostgreSQL rejects the statement
    outright -- this at least keeps the two declarations from silently
    drifting apart in this source file. Does not prove PostgreSQL
    accepts either statement (see section docstring above)."""
    assert "circuit_key VARCHAR(100) NOT NULL UNIQUE" in cm.CREATE_TOR_CIRCUITS_TABLE_SQL
    assert "ON CONFLICT (circuit_key) DO NOTHING" in inspect.getsource(cm._ensure_circuit_row)

    assert "instance_key VARCHAR(200) NOT NULL UNIQUE" in cm.CREATE_TOR_INSTANCES_TABLE_SQL
    assert "ON CONFLICT (instance_key) DO NOTHING" in inspect.getsource(cm._ensure_instance_row)


# =====================================================================
# Phase 2 DDL: schema evolution must be idempotent
# =====================================================================

def test_ddl_observability_columns_use_add_column_if_not_exists():
    for column in (
        "failure_count INTEGER NOT NULL DEFAULT 0",
        "consecutive_failure_count INTEGER NOT NULL DEFAULT 0",
        "last_error_category VARCHAR(100)",
        "last_verified_at TIMESTAMPTZ",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in cm.ALTER_TOR_CIRCUITS_ADD_OBSERVABILITY_COLUMNS_SQL, column


def test_ensure_tor_circuits_table_runs_the_alter_every_time():
    """Running ensure_tor_circuits_table() must be safe against a
    database that already has the Phase 2 columns -- ADD COLUMN IF NOT
    EXISTS makes repeated runs a no-op rather than an error."""
    fake_conn = FakeConnection()
    cm.ensure_tor_circuits_table(fake_conn)
    cm.ensure_tor_circuits_table(fake_conn)

    alter_calls = [sql for sql, _ in fake_conn.executed if "ADD COLUMN IF NOT EXISTS" in sql]
    assert len(alter_calls) == 2


def test_ddl_events_table_is_idempotent_and_bounded_shape():
    assert "CREATE TABLE IF NOT EXISTS tor_circuit_events" in cm.CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL
    assert "event_type VARCHAR(50) NOT NULL" in cm.CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL
    assert "detail JSONB NOT NULL DEFAULT '{}'::jsonb" in cm.CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL
    assert "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP" in cm.CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL


def test_ddl_events_indexes_cover_bounded_inspection_queries():
    for index_target in ("created_at", "circuit_key", "event_type"):
        assert f"ON tor_circuit_events ({index_target})" in cm.CREATE_TOR_CIRCUIT_EVENTS_INDEXES_SQL


# =====================================================================
# emit_event: allowlist, secret-safety, size cap
# =====================================================================

def test_emit_event_rejects_unknown_event_type():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(fake_conn, "not_an_approved_event_type", circuit_key="default")
    assert fake_conn.executed == []


def test_emit_event_rejects_unknown_detail_field():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_SUCCESS, circuit_key="default",
                detail={"control_password": "leaked"},
            )
    assert fake_conn.executed == []


def test_emit_event_rejects_oversized_detail():
    fake_conn = FakeConnection()
    huge_reason = "x" * 3000
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default",
                detail={"reason_code": huge_reason},
            )
    assert fake_conn.executed == []


def test_emit_event_accepts_all_approved_event_types():
    for event_type in cm._ALLOWED_EVENT_TYPES:
        fake_conn = FakeConnection()
        with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
            cm.emit_event(fake_conn, event_type, circuit_key="default")
        insert_calls = [sql for sql, _ in fake_conn.executed if "INSERT INTO tor_circuit_events" in sql]
        assert len(insert_calls) == 1, event_type


def test_emit_event_accepts_allowlisted_detail_fields():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(
            fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default", instance_key="inst",
            detail={
                "error_category": cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE,
                "duration_seconds": 1.23,
                "attempt_count": 3,
                "reason_code": "control_port_unreachable",
            },
        )
    sql, params = fake_conn.executed[-2]  # insert happens before the prune's DELETE
    assert "INSERT INTO tor_circuit_events" in sql
    assert params[0] == cm.EVENT_ROTATION_FAILED
    assert params[1] == "default"
    assert params[2] == "inst"


def test_emit_event_never_stores_exit_ip_field():
    """The exit IP already lives on tor_circuits.last_exit_ip -- the
    event detail allowlist must not have a field that could duplicate
    it into an unbounded, growing event history."""
    assert "exit_ip" not in cm._ALLOWED_EVENT_DETAIL_FIELDS
    assert "ip" not in cm._ALLOWED_EVENT_DETAIL_FIELDS


def test_emit_event_detail_allowlist_has_no_secret_shaped_fields():
    forbidden_substrings = ("password", "cookie", "session", "header", "token", "auth")
    for field in cm._ALLOWED_EVENT_DETAIL_FIELDS:
        for forbidden in forbidden_substrings:
            assert forbidden not in field.lower(), field


# =====================================================================
# emit_event: bounded retention
# =====================================================================

def test_emit_event_prunes_after_insert_using_configured_max_rows():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=42):
        cm.emit_event(fake_conn, cm.EVENT_RECOVERED, circuit_key="default")

    delete_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("DELETE FROM tor_circuit_events")
    ]
    assert len(delete_calls) == 1
    _, params = delete_calls[0]
    assert params[0] == 42


def test_get_tor_event_max_rows_is_clamped_to_a_sane_range(monkeypatch):
    import app.config as config

    monkeypatch.setenv("TOR_EVENT_MAX_ROWS", "1")
    assert config.get_tor_event_max_rows() == 100  # clamped up to the minimum

    monkeypatch.setenv("TOR_EVENT_MAX_ROWS", "999999999")
    assert config.get_tor_event_max_rows() == 100000  # clamped down to the maximum

    monkeypatch.setenv("TOR_EVENT_MAX_ROWS", "5000")
    assert config.get_tor_event_max_rows() == 5000


# =====================================================================
# verify_circuit: never NEWNYM, never draining, explicit failure rules
# =====================================================================

def test_verify_circuit_requires_verify_fn():
    with pytest.raises(ValueError):
        cm.verify_circuit(circuit_key="default", verify_fn=None)


def test_verify_circuit_success_updates_exit_ip_and_last_verified_at():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.verify_circuit(circuit_key="default", verify_fn=lambda: "9.9.9.9")

    assert not fake_newnym.called, "verify_circuit must never send NEWNYM"
    assert result["exit_ip"] == "9.9.9.9"
    assert result["last_verified_at"] is not None

    # Never marks draining, never sets `status` at all.
    assert statuses_set(fake_conn) == []

    update_calls = [sql for sql, _ in fake_conn.executed if sql.strip().startswith("UPDATE tor_circuits")]
    assert len(update_calls) == 1
    assert "last_exit_ip = %s" in update_calls[0]
    assert "last_verified_at = %s" in update_calls[0]
    assert "consecutive_failure_count = 0" in update_calls[0]
    assert "last_error_category = NULL" in update_calls[0]
    assert "status" not in update_calls[0]


def test_verify_circuit_releases_lock_on_success():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        fake_psycopg2.connect.return_value = fake_conn

        cm.verify_circuit(circuit_key="default", verify_fn=lambda: "1.1.1.1")

    assert unlock_call_count(fake_conn, "default") == 1


def test_verify_circuit_failure_increments_failure_counters_but_does_not_quarantine():
    fake_conn = FakeConnection()

    def failing_verify():
        raise RuntimeError("simulated verification failure")

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(RuntimeError):
            cm.verify_circuit(circuit_key="default", verify_fn=failing_verify)

    assert not fake_newnym.called

    # No status transition of any kind -- never quarantined.
    assert statuses_set(fake_conn) == []

    update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_circuits")
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert "failure_count = failure_count + 1" in sql
    assert "consecutive_failure_count = consecutive_failure_count + 1" in sql
    assert "last_error_category = %s" in sql
    assert params[0] == cm.ERROR_CATEGORY_VERIFICATION_FAILED

    assert unlock_call_count(fake_conn, "default") == 1


def test_verify_circuit_raises_when_circuit_lock_already_held():
    fake_conn = FakeConnection(deny_lock_keys={cm._advisory_lock_key("default")})

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.CircuitLockError):
            cm.verify_circuit(circuit_key="default", verify_fn=lambda: "1.2.3.4")


# =====================================================================
# quarantine_circuit / recover_circuit: operator actions, not failures
# =====================================================================

def test_quarantine_circuit_sets_status_without_touching_failure_counters():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.quarantine_circuit(circuit_key="default", reason_code="manual_test")

    assert result["status"] == cm.STATUS_QUARANTINED
    assert statuses_set(fake_conn) == [cm.STATUS_QUARANTINED]

    update_calls = [sql for sql, _ in fake_conn.executed if sql.strip().startswith("UPDATE tor_circuits")]
    assert len(update_calls) == 1
    assert "failure_count" not in update_calls[0]
    assert unlock_call_count(fake_conn, "default") == 1


def test_recover_circuit_sets_status_ready_without_touching_failure_counters():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.recover_circuit(circuit_key="default")

    assert result["status"] == cm.STATUS_READY
    assert statuses_set(fake_conn) == [cm.STATUS_READY]

    update_calls = [sql for sql, _ in fake_conn.executed if sql.strip().startswith("UPDATE tor_circuits")]
    assert len(update_calls) == 1
    assert "failure_count" not in update_calls[0]
    assert unlock_call_count(fake_conn, "default") == 1


def test_quarantine_and_recover_never_send_newnym():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        cm.quarantine_circuit(circuit_key="default")
        cm.recover_circuit(circuit_key="default")

    assert not fake_newnym.called


# =====================================================================
# rotate_circuit: Phase 2 failure-counter semantics
# =====================================================================

def test_rotate_circuit_genuine_control_port_failure_increments_failure_counters():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.CircuitRotationError("control port unreachable"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.CircuitRotationError):
            cm.rotate_circuit(circuit_key="default")

    failure_update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_circuits") and "failure_count = failure_count + 1" in sql
    ]
    assert len(failure_update_calls) == 1
    sql, params = failure_update_calls[0]
    assert "consecutive_failure_count = consecutive_failure_count + 1" in sql
    assert params[0] == cm.STATUS_QUARANTINED
    assert params[1] == cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE


def test_rotate_circuit_verify_fn_failure_increments_failure_counters_with_verification_category():
    fake_conn = FakeConnection()

    def failing_verify():
        raise RuntimeError("exit IP check failed")

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(RuntimeError):
            cm.rotate_circuit(circuit_key="default", verify_fn=failing_verify)

    failure_update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_circuits") and "failure_count = failure_count + 1" in sql
    ]
    assert len(failure_update_calls) == 1
    _, params = failure_update_calls[0]
    assert params[1] == cm.ERROR_CATEGORY_VERIFICATION_FAILED


def test_rotate_circuit_verified_success_resets_consecutive_failures_and_sets_last_verified_at():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        cm.rotate_circuit(circuit_key="default", verify_fn=lambda: "5.6.7.8")

    success_update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_circuits") and "last_verified_at = %s" in sql
    ]
    assert len(success_update_calls) == 1
    sql, params = success_update_calls[0]
    assert "consecutive_failure_count = 0" in sql
    assert "last_error_category = NULL" in sql
    assert "status = %s" in sql
    assert params[0] == cm.STATUS_READY


def test_rotate_circuit_unverified_success_never_touches_last_verified_at_or_failure_state():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        cm.rotate_circuit(circuit_key="default")

    for sql, _ in fake_conn.executed:
        if sql.strip().startswith("UPDATE tor_circuits"):
            assert "last_verified_at" not in sql
            assert "failure_count" not in sql


def test_rotate_circuit_instance_busy_never_touches_failure_counters():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.TorInstanceBusyError("instance busy"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.TorInstanceBusyError):
            cm.rotate_circuit(circuit_key="default")

    for sql, _ in fake_conn.executed:
        if sql.strip().startswith("UPDATE tor_circuits"):
            assert "failure_count" not in sql
    assert statuses_set(fake_conn) == [cm.STATUS_DRAINING, cm.STATUS_READY]


def test_rotate_circuit_cooldown_never_touches_failure_counters():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(
             cm, "request_new_identity",
             side_effect=cm.NewnymCooldownError("cooldown active"),
         ):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.NewnymCooldownError):
            cm.rotate_circuit(circuit_key="default")

    for sql, _ in fake_conn.executed:
        if sql.strip().startswith("UPDATE tor_circuits"):
            assert "failure_count" not in sql


def test_rotate_circuit_lock_contention_never_touches_failure_counters():
    fake_conn = FakeConnection(deny_lock_keys={cm._advisory_lock_key("default")})

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(cm.CircuitLockError):
            cm.rotate_circuit(circuit_key="default")

    assert not fake_newnym.called
    for sql, _ in fake_conn.executed:
        if sql.strip().startswith("UPDATE tor_circuits"):
            assert "failure_count" not in sql


# =====================================================================
# request_new_identity: Phase 2 lock_contended / cooldown_blocked events
# =====================================================================

def test_request_new_identity_emits_lock_contended_when_instance_busy(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    instance_key = cm._instance_lock_key("127.0.0.1", 9051)
    fake_conn = FakeConnection(deny_lock_keys={cm._advisory_lock_key(instance_key)})

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(cm.TorInstanceBusyError):
            cm.request_new_identity(fake_conn)

    insert_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if "INSERT INTO tor_circuit_events" in sql
    ]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert params[0] == cm.EVENT_LOCK_CONTENDED
    assert params[2] == instance_key  # instance_key column


def test_request_new_identity_emits_cooldown_blocked_when_persisted_cooldown_exceeds_max(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MIN_INTERVAL_SECONDS", "100")

    fake_conn = FakeConnection(last_newnym_at=cm._utc_now())

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(cm.NewnymCooldownError):
            cm.request_new_identity(fake_conn, max_wait_seconds=1)

    insert_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if "INSERT INTO tor_circuit_events" in sql
    ]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert params[0] == cm.EVENT_COOLDOWN_BLOCKED


# =====================================================================
# is_draining_stale
# =====================================================================

def test_is_draining_stale_true_when_past_threshold(monkeypatch):
    monkeypatch.setenv("TOR_STALE_DRAINING_THRESHOLD_SECONDS", "60")
    now = cm._utc_now()
    old = now - timedelta(seconds=120)
    assert cm.is_draining_stale(old, now=now) is True


def test_is_draining_stale_false_when_within_threshold(monkeypatch):
    monkeypatch.setenv("TOR_STALE_DRAINING_THRESHOLD_SECONDS", "300")
    now = cm._utc_now()
    recent = now - timedelta(seconds=5)
    assert cm.is_draining_stale(recent, now=now) is False


def test_is_draining_stale_false_when_updated_at_is_none():
    assert cm.is_draining_stale(None) is False


# =====================================================================
# CRITICAL BOUNDARY (Phase 2 design, section K): a 429/403/timeout/
# connection-failure from the LOCAL simulator must NEVER, by itself,
# trigger rotate_circuit()/request_new_identity()/any NEWNYM logic.
#
# Each test below builds a plain, Tor-verification-shaped verify_fn
# (takes no arguments, returns an exit IP string, or raises) backed by
# the simulator's output, and calls verify_circuit() with it EXPLICITLY
# -- exactly as a real caller would. request_new_identity is mocked so
# these tests can assert, directly and unambiguously, that NEWNYM was
# never invoked no matter what the simulator produced.
# =====================================================================

def _verify_fn_from_simulator(simulator):
    """Tor-verification-shaped callable: non-2xx or a simulator
    exception both surface as a plain raised error, matching how
    scripts/tor/verify_tor_connectivity.py's real check_exit_ip()/
    parse_tor_ip_check_response() raise TorVerificationError on
    anything that isn't a trusted, verified Tor exit IP."""
    def _verify():
        response = simulator.next_response()
        if response.status_code != 200:
            raise RuntimeError(f"simulated non-200 response: {response.status_code}")
        return "203.0.113.1"
    return _verify


def test_simulated_429_does_not_trigger_rotation():
    fake_conn = FakeConnection()
    simulator = rate_limited_after(0)  # 429 immediately

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(RuntimeError, match="429"):
            cm.verify_circuit(circuit_key="default", verify_fn=_verify_fn_from_simulator(simulator))

    assert not fake_newnym.called, "a simulated 429 must never trigger NEWNYM/rotation"


def test_simulated_403_does_not_trigger_rotation():
    fake_conn = FakeConnection()
    simulator = always_403()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(RuntimeError, match="403"):
            cm.verify_circuit(circuit_key="default", verify_fn=_verify_fn_from_simulator(simulator))

    assert not fake_newnym.called, "a simulated 403 must never trigger NEWNYM/rotation"


def test_simulated_timeout_does_not_trigger_rotation():
    fake_conn = FakeConnection()
    simulator = always_timeout()

    def verify_fn():
        simulator.next_response()  # raises SimulatedTimeout
        return "unreachable"

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(SimulatedTimeout):
            cm.verify_circuit(circuit_key="default", verify_fn=verify_fn)

    assert not fake_newnym.called, "a simulated timeout must never trigger NEWNYM/rotation"


def test_simulated_connection_failure_does_not_trigger_rotation():
    fake_conn = FakeConnection()

    def verify_fn():
        raise SimulatedConnectionFailure("simulated connection failure")

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(SimulatedConnectionFailure):
            cm.verify_circuit(circuit_key="default", verify_fn=verify_fn)

    assert not fake_newnym.called, "a simulated connection failure must never trigger NEWNYM/rotation"


def test_simulated_failure_records_verification_failed_but_never_quarantines():
    """The verification-failure path (proven separately) never
    quarantines -- restated here specifically for a SIMULATOR-backed
    verify_fn, to close the loop between 'the simulator can produce a
    429' and 'a 429 must never escalate to a status change either.'"""
    fake_conn = FakeConnection()
    simulator = always_403()

    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity"):
        fake_psycopg2.connect.return_value = fake_conn

        with pytest.raises(RuntimeError):
            cm.verify_circuit(circuit_key="default", verify_fn=_verify_fn_from_simulator(simulator))

    assert statuses_set(fake_conn) == [], "a simulated verification failure must never change circuit status"


# =====================================================================
# record_bootstrap_started / record_bootstrap_ready / record_bootstrap_failed
# =====================================================================

def test_record_bootstrap_started_sets_checking_and_emits_event():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.record_bootstrap_started(fake_conn, "instance-a")

    update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_instances")
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert "bootstrap_status = %s" in sql
    assert params[0] == cm.BOOTSTRAP_STATUS_CHECKING

    insert_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if "INSERT INTO tor_circuit_events" in sql
    ]
    assert len(insert_calls) == 1
    assert insert_calls[0][1][0] == cm.EVENT_BOOTSTRAP_STARTED


def test_record_bootstrap_ready_sets_ready_and_clears_error_category():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.record_bootstrap_ready(fake_conn, "instance-a")

    update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_instances")
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert params[0] == cm.BOOTSTRAP_STATUS_READY
    assert "last_bootstrap_ready_at = CURRENT_TIMESTAMP" in sql
    assert "last_bootstrap_error_category = NULL" in sql

    insert_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if "INSERT INTO tor_circuit_events" in sql
    ]
    assert insert_calls[0][1][0] == cm.EVENT_BOOTSTRAP_READY


def test_record_bootstrap_failed_sets_failed_with_error_category():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.record_bootstrap_failed(fake_conn, "instance-a", cm.ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE)

    update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_instances")
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert params[0] == cm.BOOTSTRAP_STATUS_FAILED
    assert params[1] == cm.ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE

    insert_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if "INSERT INTO tor_circuit_events" in sql
    ]
    assert insert_calls[0][1][0] == cm.EVENT_BOOTSTRAP_FAILED


def test_record_bootstrap_functions_never_send_newnym():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000), \
         mock.patch.object(cm, "request_new_identity") as fake_newnym:
        cm.record_bootstrap_started(fake_conn, "instance-a")
        cm.record_bootstrap_ready(fake_conn, "instance-a")
        cm.record_bootstrap_failed(fake_conn, "instance-a", cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE)

    assert not fake_newnym.called


# =====================================================================
# Instance readiness schema: ALTER is idempotent, columns present
# =====================================================================

def test_ddl_bootstrap_columns_use_add_column_if_not_exists():
    for column in (
        "bootstrap_status VARCHAR(50) NOT NULL DEFAULT 'unknown'",
        "last_bootstrap_checked_at TIMESTAMPTZ",
        "last_bootstrap_ready_at TIMESTAMPTZ",
        "last_bootstrap_error_category VARCHAR(100)",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in cm.ALTER_TOR_INSTANCES_ADD_BOOTSTRAP_COLUMNS_SQL, column


def test_ensure_tor_instances_table_runs_the_alter_every_time():
    fake_conn = FakeConnection()
    cm.ensure_tor_instances_table(fake_conn)
    cm.ensure_tor_instances_table(fake_conn)

    alter_calls = [sql for sql, _ in fake_conn.executed if "ADD COLUMN IF NOT EXISTS bootstrap_status" in sql]
    assert len(alter_calls) == 2


def test_bootstrap_status_default_is_unknown_not_ready():
    """The schema DEFAULT itself must never silently claim readiness for
    a freshly-created row."""
    assert "DEFAULT 'unknown'" in cm.ALTER_TOR_INSTANCES_ADD_BOOTSTRAP_COLUMNS_SQL
    assert "DEFAULT 'ready'" not in cm.ALTER_TOR_INSTANCES_ADD_BOOTSTRAP_COLUMNS_SQL


# =====================================================================
# emit_event: value-level detail validation (not just field names)
# =====================================================================

def test_emit_event_rejects_unknown_error_category():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default",
                detail={"error_category": "totally_made_up_category"},
            )
    assert fake_conn.executed == []


def test_emit_event_rejects_non_string_error_category():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default",
                detail={"error_category": 12345},
            )
    assert fake_conn.executed == []


@pytest.mark.parametrize("bad_reason_code", [
    "has spaces",
    "UPPERCASE",
    "https://evil.example.com/steal",
    "control-password=hunter2",
    "semi;colon",
    "a" * 65,
    "",
    "line\nbreak",
    "tab\tchar",
    "unicode separator",
])
def test_emit_event_rejects_unsafe_reason_code_values(bad_reason_code):
    """Field-name allowlisting alone is not enough -- reason_code's
    VALUE must be a normalized machine-readable token. Free-form prose,
    URLs, secrets, or control characters must never reach the database
    just because the field NAME is approved."""
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_LOCK_CONTENDED, circuit_key="default",
                detail={"reason_code": bad_reason_code},
            )
    assert fake_conn.executed == []


def test_emit_event_accepts_well_formed_reason_code():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(
            fake_conn, cm.EVENT_LOCK_CONTENDED, circuit_key="default",
            detail={"reason_code": "circuit_lock_held"},
        )
    insert_calls = [sql for sql, _ in fake_conn.executed if "INSERT INTO tor_circuit_events" in sql]
    assert len(insert_calls) == 1


@pytest.mark.parametrize("bad_duration", [
    -1.0,
    -0.001,
    float("inf"),
    float("-inf"),
    float("nan"),
    3600.01,
    "1.5",
    True,
    None,
])
def test_emit_event_rejects_unsafe_duration_seconds_values(bad_duration):
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_SUCCESS, circuit_key="default",
                detail={"duration_seconds": bad_duration},
            )
    assert fake_conn.executed == []


def test_emit_event_accepts_valid_duration_seconds_int_and_float():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(fake_conn, cm.EVENT_ROTATION_SUCCESS, detail={"duration_seconds": 0})
    fake_conn2 = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(fake_conn2, cm.EVENT_ROTATION_SUCCESS, detail={"duration_seconds": 12.5})


@pytest.mark.parametrize("bad_attempt_count", [-1, 1001, 1.5, "3", True, None])
def test_emit_event_rejects_unsafe_attempt_count_values(bad_attempt_count):
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default",
                detail={"attempt_count": bad_attempt_count},
            )
    assert fake_conn.executed == []


def test_emit_event_accepts_valid_attempt_count():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(fake_conn, cm.EVENT_ROTATION_FAILED, detail={"attempt_count": 3})


def test_emit_event_rejects_non_primitive_value_via_value_validator():
    """A non-primitive object for an approved field name (duration_seconds)
    is rejected by _validate_event_detail's own type check -- it never
    reaches json.dumps() at all, which is the strongest possible
    guarantee (rejected before serialization is even attempted, not
    merely handled correctly once there)."""
    class Unserializable:
        def __str__(self):
            return "secret-leak-attempt"

    fake_conn = FakeConnection()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(ValueError):
            cm.emit_event(
                fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default",
                detail={"duration_seconds": Unserializable()},
            )
    assert fake_conn.executed == []


def test_emit_event_json_dumps_call_has_no_default_str_fallback():
    """Regression guard at the source level: every allowed detail field
    is now value-validated to a JSON-native primitive before
    serialization (see _validate_event_detail), so json.dumps() itself
    no longer needs -- and must not use -- a default=str escape hatch
    that could silently coerce an unvalidated object into a string."""
    import inspect
    source = inspect.getsource(cm.emit_event)
    # The function's own docstring legitimately DISCUSSES default=str in
    # prose (explaining why it was removed) -- check the actual CODE
    # (after the docstring), not the whole source text, for the call.
    code_only = ast_body_source(cm.emit_event)
    assert "default=str" not in code_only
    assert "json.dumps(detail)" in code_only


def ast_body_source(func) -> str:
    """Source of `func`'s body only, excluding its docstring -- lets a
    source-level regression test check actual code without also
    matching prose in the docstring that discusses the very thing being
    guarded against."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    func_node = tree.body[0]
    body = func_node.body

    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # drop the docstring node

    return "\n".join(ast.get_source_segment(source, node) for node in body)


def test_emit_event_max_sized_legitimate_detail_stays_under_size_cap():
    """Every field is now individually value-validated and bounded
    (reason_code <= 64 chars, error_category from a short fixed enum,
    duration_seconds/attempt_count numeric) -- a maximally-sized, fully
    legitimate combination of all four allowed fields must never itself
    trip the byte cap. The size cap remains as defense-in-depth (kept
    per design) for any future field whose value-level bound might be
    looser than these four."""
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(
            fake_conn, cm.EVENT_ROTATION_FAILED, circuit_key="default",
            detail={
                "reason_code": "a" * 64,
                "error_category": cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE,
                "duration_seconds": 3600,
                "attempt_count": 1000,
            },
        )
    insert_calls = [sql for sql, _ in fake_conn.executed if "INSERT INTO tor_circuit_events" in sql]
    assert len(insert_calls) == 1


def test_emit_event_size_cap_constant_still_present():
    assert cm._MAX_EVENT_DETAIL_BYTES == 2048


# =====================================================================
# emit_event: transactional insert + prune
# =====================================================================

def test_emit_event_commits_insert_and_prune_together_on_success():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.emit_event(fake_conn, cm.EVENT_RECOVERED, circuit_key="default")

    assert fake_conn.commit_count == 1, "INSERT and prune DELETE must share exactly one commit"
    assert fake_conn.rollback_count == 0


def test_emit_event_rolls_back_when_prune_fails_and_insert_is_not_left_committed():
    """Deliberately makes the prune DELETE fail and proves the preceding
    INSERT was never committed on its own -- INSERT+DELETE+COMMIT must
    behave as one atomic unit."""
    fake_conn = FakeConnection(raise_on_sql_substring="DELETE FROM tor_circuit_events")

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(RuntimeError, match="simulated failure"):
            cm.emit_event(fake_conn, cm.EVENT_RECOVERED, circuit_key="default")

    # The INSERT was attempted (it appears in the fake's executed log --
    # the fake only raises on the LATER DELETE, matching a real DB where
    # the INSERT itself succeeds within the transaction before the
    # prune step fails), but crucially it was never COMMITTED: no
    # commit() call happened, and rollback() was called exactly once --
    # this is what actually proves the INSERT was not left durably
    # persisted, since a real database only makes a statement's effects
    # visible to other connections after COMMIT.
    insert_calls = [sql for sql, _ in fake_conn.executed if "INSERT INTO tor_circuit_events" in sql]
    assert len(insert_calls) == 1, "the INSERT should have been attempted before the DELETE failed"
    assert fake_conn.commit_count == 0, "commit must never happen if the prune step failed"
    assert fake_conn.rollback_count == 1, "rollback must be called so the INSERT is not left committed"


def test_emit_event_rolls_back_when_insert_itself_fails():
    fake_conn = FakeConnection(raise_on_sql_substring="INSERT INTO tor_circuit_events")

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        with pytest.raises(RuntimeError, match="simulated failure"):
            cm.emit_event(fake_conn, cm.EVENT_RECOVERED, circuit_key="default")

    assert fake_conn.commit_count == 0
    assert fake_conn.rollback_count == 1


# =====================================================================
# Shared validators: _validate_error_category / _validate_reason_code
# =====================================================================

def test_validate_error_category_accepts_all_approved_categories():
    for category in cm._ALLOWED_ERROR_CATEGORIES:
        cm._validate_error_category(category)  # must not raise


def test_validate_error_category_rejects_unknown_value():
    with pytest.raises(ValueError):
        cm._validate_error_category("not_a_real_category")


def test_validate_reason_code_accepts_well_formed_value():
    cm._validate_reason_code("circuit_lock_held")  # must not raise


@pytest.mark.parametrize("bad_value", ["has spaces", "UPPER", "a" * 65, "", None, 123])
def test_validate_reason_code_rejects_unsafe_values(bad_value):
    with pytest.raises(ValueError):
        cm._validate_reason_code(bad_value)


def test_validate_event_detail_delegates_to_shared_validators():
    """Source-level guard: _validate_event_detail must call the SAME
    shared helpers quarantine_circuit()/record_bootstrap_failed() use,
    not a second hand-maintained copy of the same rules."""
    import inspect
    source = inspect.getsource(cm._validate_event_detail)
    assert "_validate_error_category(" in source
    assert "_validate_reason_code(" in source


# =====================================================================
# quarantine_circuit(reason_code=...): validated BEFORE any mutation
# =====================================================================

@pytest.mark.parametrize("bad_reason_code", ["has spaces", "UPPERCASE", "a" * 65, ""])
def test_quarantine_circuit_rejects_unsafe_reason_code_before_any_mutation(bad_reason_code):
    with mock.patch.object(cm, "psycopg2") as fake_psycopg2:
        with pytest.raises(ValueError):
            cm.quarantine_circuit(circuit_key="default", reason_code=bad_reason_code)

    # Validation happens before psycopg2.connect() is even called --
    # zero DB round-trip of any kind for an invalid reason_code.
    assert not fake_psycopg2.connect.called


def test_quarantine_circuit_accepts_well_formed_reason_code():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.quarantine_circuit(circuit_key="default", reason_code="operator_manual_test")

    assert result["status"] == cm.STATUS_QUARANTINED
    assert statuses_set(fake_conn) == [cm.STATUS_QUARANTINED]


def test_quarantine_circuit_none_reason_code_is_not_validated_or_required():
    fake_conn = FakeConnection()
    with mock.patch.object(cm, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        fake_psycopg2.connect.return_value = fake_conn

        result = cm.quarantine_circuit(circuit_key="default")  # reason_code=None (default)

    assert result["status"] == cm.STATUS_QUARANTINED


# =====================================================================
# record_bootstrap_failed(error_category=...): validated BEFORE mutation
# =====================================================================

def test_record_bootstrap_failed_rejects_unknown_error_category_before_any_mutation():
    fake_conn = FakeConnection()

    with pytest.raises(ValueError):
        cm.record_bootstrap_failed(fake_conn, "instance-a", "not_a_real_category")

    # Zero mutation of any kind -- not even ensure_tor_instances_table's
    # own CREATE/ALTER should have run.
    assert fake_conn.executed == []
    assert fake_conn.commit_count == 0


def test_record_bootstrap_failed_accepts_approved_error_category():
    fake_conn = FakeConnection()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=1000):
        cm.record_bootstrap_failed(fake_conn, "instance-a", cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE)

    update_calls = [
        (sql, params) for sql, params in fake_conn.executed
        if sql.strip().startswith("UPDATE tor_instances")
    ]
    assert len(update_calls) == 1
    assert update_calls[0][1][1] == cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE
