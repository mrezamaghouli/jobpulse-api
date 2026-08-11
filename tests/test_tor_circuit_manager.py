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


class FakeCursor:
    def __init__(self, deny_lock_keys=None, last_newnym_at=None):
        self.executed = []
        self.deny_lock_keys = deny_lock_keys or set()
        self.last_newnym_at = last_newnym_at

    def execute(self, sql, params=None):
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
    def __init__(self, deny_lock_keys=None, last_newnym_at=None):
        self.cursor_obj = FakeCursor(deny_lock_keys=deny_lock_keys, last_newnym_at=last_newnym_at)
        self.commit_count = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1

    def close(self):
        self.closed = True

    @property
    def executed(self):
        return self.cursor_obj.executed


def statuses_set(fake_conn):
    """Every status value this connection's UPDATE statements set, in order."""
    result = []
    for sql, params in fake_conn.executed:
        if sql.strip().startswith("UPDATE tor_circuits"):
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
        "default", cm.STATUS_READY, 0, "1.2.3.4", None, None
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
