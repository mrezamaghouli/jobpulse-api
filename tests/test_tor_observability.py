"""Tests for scripts/tor/observability.py -- the Phase 2 read-only
inspection layer over persisted circuit_manager.py state. No real
Docker, PostgreSQL, or Tor process is ever used; mirrors the mocking
pattern used in tests/test_tor_circuit_manager.py.

What these tests guard against:
  - inspect_circuit()/inspect_instance() ever calling pg_try_advisory_lock
    to check busy state (which would itself briefly take the lock)
  - the ControlPort password ever appearing in inspect_instance()'s output
  - metrics_snapshot() introducing any IP/circuit_key/instance_key label
  - metrics_snapshot() treating tor_instances as if it had a real
    per-row quarantine concept it does not have
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.tor.circuit_manager as cm
import scripts.tor.observability as obs


class FakeCursor:
    """Dispatches by SQL substring, same style as
    tests/test_tor_circuit_manager.py's FakeCursor, extended with
    fetchall() and a couple of Phase 2-specific query shapes
    (pg_locks EXISTS, GROUP BY status, event aggregates, bootstrap
    state, event-window metadata)."""

    def __init__(self, lock_held_keys=None, status_counts=None,
                 last_newnym_at=None, event_counts=None, duration_aggregates=None,
                 instance_row_exists=True, circuit_row=None,
                 bootstrap_status=None, last_bootstrap_checked_at=None,
                 last_bootstrap_ready_at=None, last_bootstrap_error_category=None,
                 event_window=None, existing_tables=None):
        self.executed = []
        self.lock_held_keys = lock_held_keys or set()
        self.status_counts = status_counts or {}
        self.last_newnym_at = last_newnym_at
        self.event_counts = event_counts or {}
        self.duration_aggregates = duration_aggregates or {}
        self.instance_row_exists = instance_row_exists
        # The row inspect_circuit()'s direct SELECT should return for
        # the requested circuit_key -- an 11-tuple matching (circuit_key,
        # status, request_count, last_exit_ip, last_rotated_at,
        # cooldown_until, failure_count, consecutive_failure_count,
        # last_error_category, last_verified_at, updated_at), or None
        # for "circuit_key has never been created."
        self.circuit_row = circuit_row
        # Mirrors the real schema's DEFAULT 'unknown' -- a freshly
        # created row (or one this fixture never overrides) must never
        # silently look "ready".
        self.bootstrap_status = bootstrap_status or cm.BOOTSTRAP_STATUS_UNKNOWN
        self.last_bootstrap_checked_at = last_bootstrap_checked_at
        self.last_bootstrap_ready_at = last_bootstrap_ready_at
        self.last_bootstrap_error_category = last_bootstrap_error_category
        self.event_window = event_window or (0, None, None)
        # Which Phase 2 tables to_regclass() should report as existing --
        # defaults to all three present, matching the "normal" case most
        # existing tests exercise. Pass e.g. existing_tables={"tor_circuits"}
        # to simulate the other two never having been created yet.
        self.existing_tables = (
            existing_tables if existing_tables is not None
            else {"tor_circuits", "tor_instances", "tor_circuit_events"}
        )

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        sql, params = self.executed[-1]

        if "to_regclass" in sql:
            table_name = params[0]
            return (table_name if table_name in self.existing_tables else None,)

        if "SELECT circuit_key, status, request_count" in sql:
            return self.circuit_row

        if "pg_locks" in sql and "EXISTS" in sql:
            key = params[0]
            return (key in self.lock_held_keys,)

        if "last_newnym_at, bootstrap_status" in sql:
            if not self.instance_row_exists:
                return None
            return (
                self.last_newnym_at,
                self.bootstrap_status,
                self.last_bootstrap_checked_at,
                self.last_bootstrap_ready_at,
                self.last_bootstrap_error_category,
            )

        if "SELECT COUNT(*) FROM tor_circuit_events WHERE event_type" in sql:
            event_type = params[0]
            return (self.event_counts.get(event_type, 0),)

        if "COUNT(*), MIN(created_at), MAX(created_at)" in sql:
            return self.event_window

        if "COUNT(*)" in sql and "duration_seconds" in sql:
            event_type = params[0]
            agg = self.duration_aggregates.get(event_type, (0, None, None, None, None))
            return agg

        return None

    def fetchall(self):
        sql, params = self.executed[-1]

        if "SELECT status, COUNT(*) FROM tor_circuits GROUP BY status" in sql:
            return list(self.status_counts.items())

        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, **kwargs):
        self.cursor_obj = FakeCursor(**kwargs)
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


# =====================================================================
# _is_lock_held: non-mutating, never acquires
# =====================================================================

def test_is_lock_held_never_calls_pg_try_advisory_lock():
    fake_conn = FakeConnection(lock_held_keys={cm._advisory_lock_key("default")})
    obs._is_lock_held(fake_conn, "default")

    for sql, _ in fake_conn.executed:
        assert "pg_try_advisory_lock" not in sql
        assert "pg_advisory_unlock" not in sql
        assert "pg_locks" in sql


def test_is_lock_held_true_when_key_present():
    fake_conn = FakeConnection(lock_held_keys={cm._advisory_lock_key("default")})
    assert obs._is_lock_held(fake_conn, "default") is True


def test_is_lock_held_false_when_key_absent():
    fake_conn = FakeConnection(lock_held_keys=set())
    assert obs._is_lock_held(fake_conn, "default") is False


def test_is_lock_held_query_matches_proven_pg16_mapping():
    """See scripts/tor/observability.py module docstring: this exact
    classid=0/objsubid=1 mapping was proven against a real, disposable
    PostgreSQL 16 instance before this query was written."""
    fake_conn = FakeConnection()
    obs._is_lock_held(fake_conn, "default")

    sql, params = fake_conn.executed[-1]
    assert "locktype = 'advisory'" in sql
    assert "classid = 0" in sql
    assert "objsubid = 1" in sql
    assert "granted = true" in sql
    assert params[0] == cm._advisory_lock_key("default")


# =====================================================================
# inspect_circuit
# =====================================================================

def test_inspect_circuit_returns_none_when_table_does_not_exist_yet():
    """No CREATE TABLE has ever run for this database -- inspect_circuit()
    must report this honestly (None) via a read-only to_regclass check,
    never by mutating anything into existence."""
    fake_conn = FakeConnection(existing_tables=set())
    assert obs.inspect_circuit(fake_conn, "default") is None


def test_inspect_circuit_returns_none_when_circuit_row_absent():
    fake_conn = FakeConnection(circuit_row=None)
    assert obs.inspect_circuit(fake_conn, "default") is None


def test_inspect_circuit_includes_lock_and_staleness_flags(monkeypatch):
    monkeypatch.setenv("TOR_STALE_DRAINING_THRESHOLD_SECONDS", "60")
    now = cm._utc_now()
    stale_updated_at = now - timedelta(seconds=120)

    fake_conn = FakeConnection(
        lock_held_keys={cm._advisory_lock_key("default")},
        circuit_row=(
            "default", cm.STATUS_DRAINING, 0, None, None, None,
            0, 0, None, None, stale_updated_at,
        ),
    )

    result = obs.inspect_circuit(fake_conn, "default")

    assert result["is_locked"] is True
    assert result["is_stale_draining"] is True
    assert result["circuit_key"] == "default"


def test_inspect_circuit_not_stale_when_status_is_ready(monkeypatch):
    monkeypatch.setenv("TOR_STALE_DRAINING_THRESHOLD_SECONDS", "60")
    now = cm._utc_now()
    old_updated_at = now - timedelta(seconds=120)

    fake_conn = FakeConnection(
        lock_held_keys=set(),
        circuit_row=(
            "default", cm.STATUS_READY, 0, "1.2.3.4", old_updated_at, None,
            0, 0, None, old_updated_at, old_updated_at,
        ),
    )

    result = obs.inspect_circuit(fake_conn, "default")

    # Old updated_at, but status != draining -- must never be flagged stale.
    assert result["is_stale_draining"] is False


# =====================================================================
# inspect_instance
# =====================================================================

def test_inspect_instance_never_returns_control_password(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_CONTROL_PASSWORD", "super-secret-value")

    fake_conn = FakeConnection()
    result = obs.inspect_instance(fake_conn)

    serialized = str(result)
    assert "super-secret-value" not in serialized
    assert "password" not in result


def test_inspect_instance_endpoint_values_come_from_active_config(monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "tor-host")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_SOCKS_HOST", "tor-host")
    monkeypatch.setenv("TOR_SOCKS_PORT", "9050")

    fake_conn = FakeConnection()
    result = obs.inspect_instance(fake_conn)

    assert result["control_host"] == "tor-host"
    assert result["control_port"] == 9051
    assert result["socks_host"] == "tor-host"
    assert result["socks_port"] == 9050
    assert result["endpoint_source"] == "active_configuration"


def test_inspect_instance_reflects_lock_state():
    fake_conn = FakeConnection(lock_held_keys=set())
    result_not_busy = obs.inspect_instance(fake_conn)
    assert result_not_busy["is_locked"] is False

    fake_conn2 = FakeConnection()
    instance_key = result_not_busy["instance_key"]
    fake_conn2.cursor_obj.lock_held_keys = {cm._advisory_lock_key(instance_key)}
    result_busy = obs.inspect_instance(fake_conn2)
    assert result_busy["is_locked"] is True


def test_inspect_instance_reports_unknown_and_not_ready_when_never_checked():
    """A freshly-created (or never-checked) instance row defaults to
    bootstrap_status='unknown' -- is_ready must be False, never assumed
    True from the absence of a lock."""
    fake_conn = FakeConnection(bootstrap_status=cm.BOOTSTRAP_STATUS_UNKNOWN)
    result = obs.inspect_instance(fake_conn)

    assert result["bootstrap_status"] == cm.BOOTSTRAP_STATUS_UNKNOWN
    assert result["is_ready"] is False


def test_inspect_instance_reports_no_row_at_all_as_unknown_and_not_ready():
    """Before record_bootstrap_started() has ever run, there is no
    tor_instances row at all -- must behave identically to an
    explicit 'unknown' row, never crash and never default to ready."""
    fake_conn = FakeConnection(instance_row_exists=False)
    result = obs.inspect_instance(fake_conn)

    assert result["bootstrap_status"] == cm.BOOTSTRAP_STATUS_UNKNOWN
    assert result["is_ready"] is False
    assert result["last_bootstrap_ready_at"] is None


def test_inspect_instance_reports_ready_only_when_persisted_status_is_ready():
    now = cm._utc_now()
    fake_conn = FakeConnection(
        bootstrap_status=cm.BOOTSTRAP_STATUS_READY,
        last_bootstrap_checked_at=now,
        last_bootstrap_ready_at=now,
    )
    result = obs.inspect_instance(fake_conn)

    assert result["bootstrap_status"] == cm.BOOTSTRAP_STATUS_READY
    assert result["is_ready"] is True
    assert result["last_bootstrap_ready_at"] == now


def test_inspect_instance_reports_failed_bootstrap_with_error_category():
    fake_conn = FakeConnection(
        bootstrap_status=cm.BOOTSTRAP_STATUS_FAILED,
        last_bootstrap_error_category=cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE,
    )
    result = obs.inspect_instance(fake_conn)

    assert result["bootstrap_status"] == cm.BOOTSTRAP_STATUS_FAILED
    assert result["is_ready"] is False
    assert result["last_bootstrap_error_category"] == cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE


def test_inspect_instance_readiness_independent_of_lock_state():
    """The core correctness fix: is_locked and is_ready must never be
    derived from one another."""
    fake_conn_locked_ready = FakeConnection(
        lock_held_keys={cm._advisory_lock_key(
            cm._instance_lock_key(cm.get_tor_control_host(), cm.get_tor_control_port())
        )},
        bootstrap_status=cm.BOOTSTRAP_STATUS_READY,
    )
    result = obs.inspect_instance(fake_conn_locked_ready)
    assert result["is_locked"] is True
    assert result["is_ready"] is True

    fake_conn_unlocked_unknown = FakeConnection(
        lock_held_keys=set(),
        bootstrap_status=cm.BOOTSTRAP_STATUS_UNKNOWN,
    )
    result2 = obs.inspect_instance(fake_conn_unlocked_unknown)
    assert result2["is_locked"] is False
    assert result2["is_ready"] is False


def test_inspect_instance_reports_unknown_when_table_does_not_exist_yet():
    """No CREATE TABLE has ever run for tor_instances -- inspect_instance()
    must report bootstrap_status='unknown'/is_ready=False honestly via a
    read-only to_regclass check, never by creating the table."""
    fake_conn = FakeConnection(existing_tables=set())
    result = obs.inspect_instance(fake_conn)

    assert result["bootstrap_status"] == cm.BOOTSTRAP_STATUS_UNKNOWN
    assert result["is_ready"] is False


# =====================================================================
# metrics_snapshot
# =====================================================================

def test_metrics_snapshot_has_no_ip_or_key_labels():
    fake_conn = FakeConnection(
        status_counts={cm.STATUS_READY: 3, cm.STATUS_QUARANTINED: 1},
    )
    result = obs.metrics_snapshot(fake_conn)

    serialized_keys = str(list(result.keys()))
    assert "circuit_key" not in serialized_keys
    assert "instance_key" not in serialized_keys
    assert "ip" not in serialized_keys.lower()


def test_metrics_snapshot_circuit_counts_from_group_by():
    fake_conn = FakeConnection(
        status_counts={cm.STATUS_READY: 3, cm.STATUS_QUARANTINED: 2, cm.STATUS_DRAINING: 1},
    )
    result = obs.metrics_snapshot(fake_conn)

    assert result["tor_circuits_total"] == 6
    assert result["tor_circuits_ready"] == 3
    assert result["tor_circuits_quarantined"] == 2


def test_metrics_snapshot_instance_never_ready_when_bootstrap_never_checked():
    """Correctness fix: 'not busy' != 'ready'. A freshly-created (or
    never-checked) instance defaults to bootstrap_status='unknown' --
    readiness must be 0, never assumed True just because nothing is
    holding the lock."""
    fake_conn = FakeConnection(lock_held_keys=set(), status_counts={})
    result = obs.metrics_snapshot(fake_conn)

    assert result["tor_instances_total"] == 1
    assert result["tor_instances_ready"] == 0
    # No independent instance-level quarantine concept in this schema.
    assert result["tor_instances_quarantined"] == 0


def test_metrics_snapshot_instance_readiness_independent_of_lock_state():
    """Lock availability and bootstrap readiness are DELIBERATELY
    independent facts. Proves both directions: locked+ready still
    reports ready=1; unlocked+never-checked still reports ready=0."""
    instance_key = cm._instance_lock_key(cm.get_tor_control_host(), cm.get_tor_control_port())

    locked_and_ready = FakeConnection(
        status_counts={},
        lock_held_keys={cm._advisory_lock_key(instance_key)},
        bootstrap_status=cm.BOOTSTRAP_STATUS_READY,
    )
    result = obs.metrics_snapshot(locked_and_ready)
    assert result["tor_instances_ready"] == 1, "a held lock must not suppress a persisted ready status"

    unlocked_and_unknown = FakeConnection(
        status_counts={},
        lock_held_keys=set(),
        bootstrap_status=cm.BOOTSTRAP_STATUS_UNKNOWN,
    )
    result = obs.metrics_snapshot(unlocked_and_unknown)
    assert result["tor_instances_ready"] == 0, "a free lock must not manufacture readiness on its own"


def test_metrics_snapshot_instance_not_ready_when_bootstrap_failed():
    fake_conn = FakeConnection(status_counts={}, bootstrap_status=cm.BOOTSTRAP_STATUS_FAILED)
    result = obs.metrics_snapshot(fake_conn)
    assert result["tor_instances_ready"] == 0


def test_metrics_snapshot_event_counters_use_retained_window_naming():
    """Renamed from *_total to *_retained: these are bounded by event
    retention (TOR_EVENT_MAX_ROWS), never a true lifetime total -- see
    metrics_snapshot()'s docstring correction."""
    fake_conn = FakeConnection(
        status_counts={},
        event_counts={
            cm.EVENT_ROTATION_REQUESTED: 10,
            cm.EVENT_ROTATION_SUCCESS: 7,
            cm.EVENT_ROTATION_FAILED: 3,
            cm.EVENT_VERIFICATION_FAILED: 2,
            cm.EVENT_LOCK_CONTENDED: 1,
        },
    )
    result = obs.metrics_snapshot(fake_conn)

    assert result["tor_rotation_attempts_retained"] == 10
    assert result["tor_rotation_success_retained"] == 7
    assert result["tor_rotation_failures_retained"] == 3
    assert result["tor_verification_failures_retained"] == 2
    assert result["tor_lock_contention_retained"] == 1

    # The old, misleading *_total names must be gone entirely.
    for old_key in (
        "tor_rotation_attempts_total", "tor_rotation_success_total",
        "tor_rotation_failures_total", "tor_verification_failures_total",
        "tor_lock_contention_total",
    ):
        assert old_key not in result


def test_metrics_snapshot_duration_aggregates_shape():
    fake_conn = FakeConnection(
        status_counts={},
        duration_aggregates={
            cm.EVENT_ROTATION_SUCCESS: (5, 10.0, 1.0, 4.0, 2.0),
            cm.EVENT_VERIFICATION_SUCCESS: (0, 0, None, None, None),
        },
    )
    result = obs.metrics_snapshot(fake_conn)

    rotation_duration = result["tor_rotation_duration_seconds"]
    assert rotation_duration == {
        "scope": "retained_window", "count": 5, "sum": 10.0, "min": 1.0, "max": 4.0, "avg": 2.0,
    }

    verification_duration = result["tor_verification_duration_seconds"]
    assert verification_duration == {
        "scope": "retained_window", "count": 0, "sum": 0.0, "min": None, "max": None, "avg": None,
    }


def test_metrics_snapshot_includes_event_window_metadata():
    now = cm._utc_now()
    earlier = now - timedelta(hours=1)

    fake_conn = FakeConnection(status_counts={}, event_window=(42, earlier, now))
    result = obs.metrics_snapshot(fake_conn)

    window = result["event_window"]
    assert window["retained_rows"] == 42
    assert window["max_rows"] > 0
    assert window["oldest_event_at"] == earlier
    assert window["newest_event_at"] == now


def test_metrics_snapshot_handles_tor_circuits_absent_honestly():
    fake_conn = FakeConnection(existing_tables={"tor_instances", "tor_circuit_events"})
    result = obs.metrics_snapshot(fake_conn)

    assert result["tor_circuits_total"] == 0
    assert result["tor_circuits_ready"] == 0
    assert result["tor_circuits_quarantined"] == 0


def test_metrics_snapshot_handles_tor_circuit_events_absent_honestly():
    fake_conn = FakeConnection(existing_tables={"tor_circuits", "tor_instances"})
    result = obs.metrics_snapshot(fake_conn)

    assert result["tor_rotation_attempts_retained"] == 0
    assert result["tor_rotation_success_retained"] == 0
    assert result["tor_rotation_failures_retained"] == 0
    assert result["tor_verification_failures_retained"] == 0
    assert result["tor_lock_contention_retained"] == 0
    assert result["tor_rotation_duration_seconds"] == {
        "scope": "retained_window", "count": 0, "sum": 0.0, "min": None, "max": None, "avg": None,
    }
    window = result["event_window"]
    assert window["retained_rows"] == 0
    assert window["max_rows"] > 0
    assert window["oldest_event_at"] is None
    assert window["newest_event_at"] is None


def test_metrics_snapshot_handles_all_tables_absent_honestly():
    """The very first call ever made against a brand new database, before
    any circuit_manager.py operation has run -- must return a fully
    honest all-empty/never-ready snapshot, never an error, and never a
    manufactured CREATE TABLE."""
    fake_conn = FakeConnection(existing_tables=set())
    result = obs.metrics_snapshot(fake_conn)

    assert result["tor_circuits_total"] == 0
    assert result["tor_instances_ready"] == 0
    assert result["tor_rotation_attempts_retained"] == 0
    assert result["event_window"]["retained_rows"] == 0

    for sql, _ in fake_conn.executed:
        assert sql.strip().upper().startswith("SELECT")
    assert fake_conn.commit_count == 0
    assert fake_conn.rollback_count == 0


# =====================================================================
# Genuinely read-only: every executed statement is SELECT, and
# connection.commit()/rollback() are NEVER called by any function in
# this module -- this is what "read-only" actually means, not merely a
# docstring claim.
# =====================================================================

def _assert_select_only_and_no_commits(fake_conn):
    import re

    for sql, _ in fake_conn.executed:
        normalized = sql.strip().upper()
        assert normalized.startswith("SELECT"), f"non-SELECT statement executed: {sql!r}"
        for forbidden in ("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE"):
            # Word-boundary match, not a bare substring check: legitimate
            # column names in this schema (created_at, updated_at)
            # contain "CREATE"/"UPDATE" as substrings but are not the
            # SQL keywords -- \b won't match between "UPDATE" and the
            # immediately-following "D" in "UPDATED_AT" since both are
            # word characters (underscore counts), so this correctly
            # tells a real UPDATE statement apart from a column named
            # updated_at.
            assert not re.search(rf"\b{forbidden}\b", normalized), f"{forbidden} found in statement: {sql!r}"
    assert fake_conn.commit_count == 0, "observability must never call connection.commit()"
    assert fake_conn.rollback_count == 0, "observability must never call connection.rollback()"


def test_is_lock_held_is_select_only():
    fake_conn = FakeConnection(lock_held_keys={cm._advisory_lock_key("default")})
    obs._is_lock_held(fake_conn, "default")
    _assert_select_only_and_no_commits(fake_conn)


def test_inspect_circuit_is_select_only_when_present():
    fake_conn = FakeConnection(
        circuit_row=("default", cm.STATUS_READY, 0, "1.2.3.4", None, None, 0, 0, None, None, None),
    )
    obs.inspect_circuit(fake_conn, "default")
    _assert_select_only_and_no_commits(fake_conn)


def test_inspect_circuit_is_select_only_when_table_absent():
    fake_conn = FakeConnection(existing_tables=set())
    obs.inspect_circuit(fake_conn, "default")
    _assert_select_only_and_no_commits(fake_conn)


def test_inspect_instance_is_select_only_when_present():
    fake_conn = FakeConnection(bootstrap_status=cm.BOOTSTRAP_STATUS_READY)
    obs.inspect_instance(fake_conn)
    _assert_select_only_and_no_commits(fake_conn)


def test_inspect_instance_is_select_only_when_table_absent():
    fake_conn = FakeConnection(existing_tables=set())
    obs.inspect_instance(fake_conn)
    _assert_select_only_and_no_commits(fake_conn)


def test_metrics_snapshot_is_select_only_when_present():
    fake_conn = FakeConnection(
        status_counts={cm.STATUS_READY: 2},
        event_counts={cm.EVENT_ROTATION_SUCCESS: 3},
        bootstrap_status=cm.BOOTSTRAP_STATUS_READY,
    )
    obs.metrics_snapshot(fake_conn)
    _assert_select_only_and_no_commits(fake_conn)


def test_metrics_snapshot_is_select_only_when_all_tables_absent():
    fake_conn = FakeConnection(existing_tables=set())
    obs.metrics_snapshot(fake_conn)
    _assert_select_only_and_no_commits(fake_conn)


def test_event_window_metadata_is_select_only():
    fake_conn = FakeConnection(event_window=(5, None, None))
    obs._event_window_metadata(fake_conn)
    _assert_select_only_and_no_commits(fake_conn)


def _module_source_without_docstrings(module) -> str:
    """Source of `module` with every module/function/class DOCSTRING
    blanked out -- lets a source-level regression test check actual
    CODE without also matching prose that legitimately discusses the
    very thing being guarded against (this module's own docstrings
    explain, in words, that it never calls ensure_tor_*_table() or
    .commit()/.rollback() -- a plain substring search would incorrectly
    flag that explanation as a violation)."""
    import ast
    import inspect

    source = inspect.getsource(module)
    tree = ast.parse(source)
    lines = source.splitlines()

    def _blank_docstring(node):
        body = getattr(node, "body", None)
        # `.body` is a statement LIST on Module/FunctionDef/ClassDef/etc,
        # but a single expression NODE on things like ast.IfExp (a
        # ternary) -- only the list form can ever hold a docstring.
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            doc_node = body[0]
            for i in range(doc_node.lineno - 1, doc_node.end_lineno):
                lines[i] = ""
        for child in ast.iter_child_nodes(node):
            _blank_docstring(child)

    _blank_docstring(tree)
    return "\n".join(lines)


def test_observability_module_source_never_calls_ensure_functions():
    """Source-level regression guard: this module must never call any of
    circuit_manager.py's mutating ensure_tor_*_table() functions,
    regardless of what any individual test's mock happens to catch."""
    code_only = _module_source_without_docstrings(obs)
    for forbidden in (
        "ensure_tor_circuits_table(", "ensure_tor_instances_table(",
        "ensure_tor_circuit_events_table(",
    ):
        assert forbidden not in code_only, forbidden


def test_observability_module_source_never_calls_commit_or_rollback():
    code_only = _module_source_without_docstrings(obs)
    assert ".commit()" not in code_only
    assert ".rollback()" not in code_only
