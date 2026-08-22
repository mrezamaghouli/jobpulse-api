"""
Real PostgreSQL 16 integration evidence for the Phase 2 Tor
control-plane additions in scripts/tor/circuit_manager.py and
scripts/tor/observability.py.

WHY THIS FILE EXISTS: every test in tests/test_tor_circuit_manager.py
and tests/test_tor_observability.py proves only that this code executes
the SQL strings it constructs and correctly interprets a MOCKED cursor's
return values. It does NOT prove that PostgreSQL 16 itself actually
accepts the DDL (including the new `ADD COLUMN IF NOT EXISTS` schema
evolution), that advisory locks behave as documented under real
concurrent sessions, or -- critically for
scripts/tor/observability.py's pg_locks-based busy inspection -- that
the classid/objid/objsubid mapping this module assumes for the
single-bigint-argument pg_try_advisory_lock()/pg_advisory_unlock()
family is actually what PostgreSQL 16 produces. That mapping (locktype=
'advisory', classid=0, objid=<key>, objsubid=1, granted=true) was first
proven manually against a disposable postgres:16-alpine container before
observability.py was written; this file is the automated, repeatable
version of that same proof (see test_pg_locks_busy_inspection_is_accurate
below).

This file mirrors tests/test_upsert_returning_integration.py's own
pattern exactly: gated on an explicit, disposable test DSN
(JOBPULSE_TOR_TEST_POSTGRES_DSN), SKIPPED (not silently passed) when
unset, and never connects to production. The CI job that supplies this
DSN (see .github/workflows/ci.yml, job `tor-postgres-integration`) also
sets POSTGRES_HOST/PORT/DB/USER/PASSWORD to the SAME disposable
database, because scripts/tor/circuit_manager.py's real functions
(rotate_circuit, verify_circuit, quarantine_circuit, recover_circuit,
emit_event, ...) each open their OWN connection via
app.config.get_postgres_config() -- there is no dependency-injection
point for a test DSN in that module's public API (matching its Phase 1
design), so pointing get_postgres_config() itself at the disposable
database is what lets this file exercise those REAL functions
end-to-end rather than reimplementing their SQL. The only thing mocked
in this file is stem.control.Controller -- there is no real Tor daemon
in CI, and proving PostgreSQL-side locking/schema/retention behavior
does not require one; scripts/tor/verify_tor_connectivity.py's own
Playwright-based exit-IP check is separately, purely unit-tested
elsewhere (tests/test_verify_tor_connectivity.py) with no PostgreSQL
dependency at all.
"""
import os
import sys
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_DSN_ENV_VAR = "JOBPULSE_TOR_TEST_POSTGRES_DSN"

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_DSN_ENV_VAR),
    reason=(
        f"No local PostgreSQL 16 binaries were found in this environment, and "
        f"{TEST_DSN_ENV_VAR} is not set to a disposable test database DSN. "
        f"This is a real, unexecuted blocker -- NOT evidence the Phase 2 Tor "
        f"schema/locking/retention behavior works against real PostgreSQL 16. "
        f"Set {TEST_DSN_ENV_VAR} (and POSTGRES_HOST/PORT/DB/USER/PASSWORD to "
        f"the SAME disposable database, since scripts/tor/circuit_manager.py's "
        f"real functions read those via app.config.get_postgres_config()) to "
        f"run this file for real, or rely on the CI job that provides a "
        f"PostgreSQL 16 service container."
    ),
)


def _require_disposable_postgres_env():
    """Hard safety guard: app.config.get_postgres_config() -- which
    scripts/tor/circuit_manager.py's real functions call internally --
    DEFAULTS to host=localhost/db=jobpulse/user=jobpulse_user when those
    env vars are unset. On a machine that also happens to be running
    this project's own dev/prod-like PostgreSQL container on the
    default port with those same default credentials, an unset or
    mis-set POSTGRES_* env block would silently point every "disposable"
    test connection in this file at that REAL database instead of a
    disposable one. Refuses to run (raises, causing an error rather than
    a silent skip) unless POSTGRES_DB is both set AND clearly named as a
    disposable test database -- mirrors tests/test_upsert_returning_integration.py's
    own jobpulse_upsert_test naming convention."""
    postgres_db = os.environ.get("POSTGRES_DB", "")
    if "test" not in postgres_db.lower():
        raise RuntimeError(
            "Refusing to run: POSTGRES_DB "
            f"({postgres_db!r}) is not clearly a disposable test database "
            "(must contain 'test' in its name). scripts/tor/circuit_manager.py's "
            "real functions read POSTGRES_HOST/PORT/DB/USER/PASSWORD directly "
            "via get_postgres_config(), which defaults to this project's own "
            "dev/prod-like database credentials when unset -- this guard exists "
            "specifically to prevent this integration test from ever writing "
            "into that database by accident."
        )


def _drop_tor_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS tor_circuits, tor_instances, tor_circuit_events CASCADE;"
        )
    conn.commit()


@pytest.fixture(autouse=True)
def _safety_guard():
    """Applies to EVERY test in this file automatically, regardless of
    which other fixtures it requests -- several tests below call the
    real cm.rotate_circuit()/verify_circuit()/etc, which read
    POSTGRES_HOST/PORT/DB/USER/PASSWORD directly, entirely independent
    of the pg_conn fixture's own explicit-DSN connection."""
    _require_disposable_postgres_env()
    yield


@pytest.fixture
def pg_conn():
    import psycopg2

    dsn = os.environ[TEST_DSN_ENV_VAR]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    _drop_tor_tables(conn)

    yield conn

    _drop_tor_tables(conn)
    conn.close()


@pytest.fixture
def cm():
    """Imported lazily, inside a fixture, so module collection never
    fails even in an environment where psycopg2/stem aren't installed --
    matching this repo's other DB-gated integration test's own
    import-inside-fixture style."""
    import scripts.tor.circuit_manager as circuit_manager
    return circuit_manager


@pytest.fixture
def obs():
    import scripts.tor.observability as observability
    return observability


def _second_connection():
    import psycopg2
    dsn = os.environ[TEST_DSN_ENV_VAR]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    return conn


# =====================================================================
# Schema creation is idempotent
# =====================================================================

def test_schema_creation_is_idempotent(pg_conn, cm):
    cm.ensure_tor_circuits_table(pg_conn)
    cm.ensure_tor_circuits_table(pg_conn)
    cm.ensure_tor_instances_table(pg_conn)
    cm.ensure_tor_instances_table(pg_conn)
    cm.ensure_tor_circuit_events_table(pg_conn)
    cm.ensure_tor_circuit_events_table(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('tor_circuits');")
        assert cur.fetchone()[0] == "tor_circuits"
        cur.execute("SELECT to_regclass('tor_instances');")
        assert cur.fetchone()[0] == "tor_instances"
        cur.execute("SELECT to_regclass('tor_circuit_events');")
        assert cur.fetchone()[0] == "tor_circuit_events"


# =====================================================================
# ADD COLUMN IF NOT EXISTS upgrades an existing Phase-1-only schema
# =====================================================================

def test_alter_add_column_if_not_exists_upgrades_existing_phase1_schema(pg_conn, cm):
    # Hand-create a Phase-1-shaped tor_circuits table -- deliberately
    # WITHOUT any of the Phase 2 observability columns -- to prove the
    # upgrade path a real, already-deployed Phase 1 database would hit.
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE tor_circuits (
                id SERIAL PRIMARY KEY,
                circuit_key VARCHAR(100) NOT NULL UNIQUE,
                status VARCHAR(50) NOT NULL DEFAULT 'ready',
                request_count INTEGER NOT NULL DEFAULT 0,
                last_exit_ip VARCHAR(64),
                last_rotated_at TIMESTAMPTZ,
                cooldown_until TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tor_circuits';"
        )
        columns_before = {row[0] for row in cur.fetchall()}
    assert "failure_count" not in columns_before

    cm.ensure_tor_circuits_table(pg_conn)  # runs CREATE TABLE IF NOT EXISTS + the ALTER

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tor_circuits';"
        )
        columns_after = {row[0] for row in cur.fetchall()}

    for column in ("failure_count", "consecutive_failure_count", "last_error_category", "last_verified_at"):
        assert column in columns_after, column

    # Running it again against an ALREADY-upgraded schema must not error.
    cm.ensure_tor_circuits_table(pg_conn)


# =====================================================================
# Events insert/query correctly
# =====================================================================

def test_events_insert_and_query_correctly(pg_conn, cm):
    cm.ensure_tor_circuit_events_table(pg_conn)

    cm.emit_event(
        pg_conn, cm.EVENT_ROTATION_SUCCESS, circuit_key="evt-test", instance_key="inst-1",
        detail={"duration_seconds": 1.5, "attempt_count": 1},
    )

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, circuit_key, instance_key, detail FROM tor_circuit_events WHERE circuit_key = %s;",
            ("evt-test",),
        )
        row = cur.fetchone()

    assert row is not None
    event_type, circuit_key, instance_key, detail = row
    assert event_type == cm.EVENT_ROTATION_SUCCESS
    assert circuit_key == "evt-test"
    assert instance_key == "inst-1"
    assert detail == {"duration_seconds": 1.5, "attempt_count": 1}


# =====================================================================
# Event retention stays within configured cap
# =====================================================================

def test_event_retention_stays_within_configured_cap(pg_conn, cm, monkeypatch):
    monkeypatch.setenv("TOR_EVENT_MAX_ROWS", "100")  # clamped minimum in app.config

    cm.ensure_tor_circuit_events_table(pg_conn)

    for i in range(115):
        cm.emit_event(pg_conn, cm.EVENT_RECOVERED, circuit_key=f"retention-{i}")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tor_circuit_events;")
        (count,) = cur.fetchone()
    assert count == 100

    # The retained rows must be the MOST RECENT ones (highest ids) --
    # i.e. the oldest 15 were pruned, not an arbitrary 15.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT circuit_key FROM tor_circuit_events ORDER BY id ASC LIMIT 1;")
        (oldest_remaining,) = cur.fetchone()
    assert oldest_remaining == "retention-15"


# =====================================================================
# Failure counters persist across commits/connections
# =====================================================================

def test_failure_counters_persist_across_commits_and_connections(pg_conn, cm):
    circuit_key = "persist-test"
    cm.ensure_tor_circuits_table(pg_conn)
    cm._ensure_circuit_row(pg_conn, circuit_key)

    with mock.patch.object(cm, "Controller") as fake_controller_cls:
        fake_controller_cls.from_port.side_effect = RuntimeError("simulated control-port failure")

        with pytest.raises(cm.CircuitRotationError):
            cm.rotate_circuit(circuit_key=circuit_key)

    # A FRESH connection, opened after rotate_circuit()'s own connection
    # already closed -- proves the failure state was actually committed,
    # not merely visible within the same session/transaction.
    fresh_conn = _second_connection()
    try:
        with fresh_conn.cursor() as cur:
            cur.execute(
                "SELECT status, failure_count, consecutive_failure_count, last_error_category "
                "FROM tor_circuits WHERE circuit_key = %s;",
                (circuit_key,),
            )
            status, failure_count, consecutive_failure_count, last_error_category = cur.fetchone()
    finally:
        fresh_conn.close()

    assert status == cm.STATUS_QUARANTINED
    assert failure_count == 1
    assert consecutive_failure_count == 1
    assert last_error_category == cm.ERROR_CATEGORY_CONTROL_PORT_FAILURE


# =====================================================================
# Circuit / instance advisory lock contention on real PG16
# =====================================================================

def test_circuit_advisory_lock_contention_on_real_pg16(pg_conn, cm):
    conn_b = _second_connection()
    try:
        assert cm.try_acquire_advisory_lock(pg_conn, "contended-circuit") is True
        assert cm.try_acquire_advisory_lock(conn_b, "contended-circuit") is False

        cm.release_advisory_lock(pg_conn, "contended-circuit")

        assert cm.try_acquire_advisory_lock(conn_b, "contended-circuit") is True
        cm.release_advisory_lock(conn_b, "contended-circuit")
    finally:
        conn_b.close()


def test_instance_advisory_lock_contention_on_real_pg16(pg_conn, cm):
    instance_scope = cm._instance_lock_key("127.0.0.1", 9051)
    conn_b = _second_connection()
    try:
        assert cm.try_acquire_advisory_lock(pg_conn, instance_scope) is True
        assert cm.try_acquire_advisory_lock(conn_b, instance_scope) is False

        cm.release_advisory_lock(pg_conn, instance_scope)
        assert cm.try_acquire_advisory_lock(conn_b, instance_scope) is True
        cm.release_advisory_lock(conn_b, instance_scope)
    finally:
        conn_b.close()


# =====================================================================
# Locks disappear after session disconnect (crash-equivalent)
# =====================================================================

def test_lock_disappears_after_session_disconnect_without_explicit_unlock(cm):
    conn_a = _second_connection()
    conn_b = _second_connection()
    try:
        assert cm.try_acquire_advisory_lock(conn_a, "crash-test") is True
        assert cm.try_acquire_advisory_lock(conn_b, "crash-test") is False

        conn_a.close()  # no pg_advisory_unlock -- the crash-equivalent case

        assert cm.try_acquire_advisory_lock(conn_b, "crash-test") is True
        cm.release_advisory_lock(conn_b, "crash-test")
    finally:
        conn_b.close()


# =====================================================================
# pg_locks read-only busy-state inspection is accurate
# =====================================================================

def test_pg_locks_busy_inspection_is_accurate(pg_conn, cm, obs):
    conn_b = _second_connection()
    try:
        assert obs._is_lock_held(conn_b, "inspect-test") is False

        assert cm.try_acquire_advisory_lock(pg_conn, "inspect-test") is True
        assert obs._is_lock_held(conn_b, "inspect-test") is True

        cm.release_advisory_lock(pg_conn, "inspect-test")
        assert obs._is_lock_held(conn_b, "inspect-test") is False
    finally:
        conn_b.close()


def test_pg_locks_busy_inspection_never_itself_acquires_the_lock(pg_conn, cm, obs):
    """A non-mutating check must never make the lock appear held merely
    by having been queried."""
    obs._is_lock_held(pg_conn, "non-mutating-test")
    assert cm.try_acquire_advisory_lock(pg_conn, "non-mutating-test") is True
    cm.release_advisory_lock(pg_conn, "non-mutating-test")


# =====================================================================
# Two different circuit_keys sharing one instance cannot rotate
# concurrently
# =====================================================================

def test_two_circuit_keys_sharing_one_instance_cannot_rotate_concurrently(pg_conn, cm, monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "shared-instance-host")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    instance_scope = cm._instance_lock_key("shared-instance-host", 9051)

    # Simulates circuit A being mid-NEWNYM: hold the INSTANCE lock open
    # on a separate real session.
    holder_conn = _second_connection()
    try:
        assert cm.try_acquire_advisory_lock(holder_conn, instance_scope) is True

        # A different circuit_key, SAME configured instance -- must be
        # rejected immediately (non-blocking), never silently proceed.
        with pytest.raises(cm.TorInstanceBusyError):
            cm.request_new_identity(pg_conn)
    finally:
        cm.release_advisory_lock(holder_conn, instance_scope)
        holder_conn.close()


# =====================================================================
# Busy/cooldown never increment failure counters (real PG16)
# =====================================================================

def test_instance_busy_never_increments_failure_counters_end_to_end(cm, monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "busy-instance-host")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    circuit_key = "busy-failure-test"
    instance_scope = cm._instance_lock_key("busy-instance-host", 9051)

    holder_conn = _second_connection()
    try:
        assert cm.try_acquire_advisory_lock(holder_conn, instance_scope) is True

        with pytest.raises(cm.TorInstanceBusyError):
            cm.rotate_circuit(circuit_key=circuit_key)
    finally:
        cm.release_advisory_lock(holder_conn, instance_scope)
        holder_conn.close()

    verify_conn = _second_connection()
    try:
        with verify_conn.cursor() as cur:
            cur.execute(
                "SELECT status, failure_count, consecutive_failure_count "
                "FROM tor_circuits WHERE circuit_key = %s;",
                (circuit_key,),
            )
            status, failure_count, consecutive_failure_count = cur.fetchone()
    finally:
        verify_conn.close()

    assert status == cm.STATUS_READY  # reverted, never quarantined
    assert failure_count == 0
    assert consecutive_failure_count == 0


def test_cooldown_never_increments_failure_counters_end_to_end(pg_conn, cm, monkeypatch):
    monkeypatch.setenv("TOR_CONTROL_HOST", "cooldown-host")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")
    monkeypatch.setenv("TOR_NEWNYM_MIN_INTERVAL_SECONDS", "600")
    monkeypatch.setenv("TOR_NEWNYM_MAX_WAIT_SECONDS", "1")

    circuit_key = "cooldown-failure-test"
    instance_scope = cm._instance_lock_key("cooldown-host", 9051)

    cm.ensure_tor_instances_table(pg_conn)
    cm._ensure_instance_row(pg_conn, instance_scope)
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE tor_instances SET last_newnym_at = CURRENT_TIMESTAMP WHERE instance_key = %s;",
            (instance_scope,),
        )
    pg_conn.commit()

    with pytest.raises(cm.NewnymCooldownError):
        cm.rotate_circuit(circuit_key=circuit_key)

    verify_conn = _second_connection()
    try:
        with verify_conn.cursor() as cur:
            cur.execute(
                "SELECT status, failure_count, consecutive_failure_count "
                "FROM tor_circuits WHERE circuit_key = %s;",
                (circuit_key,),
            )
            status, failure_count, consecutive_failure_count = cur.fetchone()
    finally:
        verify_conn.close()

    assert status == cm.STATUS_READY
    assert failure_count == 0
    assert consecutive_failure_count == 0


# =====================================================================
# tor_instances Phase 2 (bootstrap) ALTERs are idempotent
# =====================================================================

def test_tor_instances_alter_add_column_if_not_exists_upgrades_existing_phase1_schema(pg_conn, cm):
    # Hand-create a Phase-1-shaped tor_instances table -- deliberately
    # WITHOUT any of the Phase 2 bootstrap columns.
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE tor_instances (
                id SERIAL PRIMARY KEY,
                instance_key VARCHAR(200) NOT NULL UNIQUE,
                last_newnym_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tor_instances';"
        )
        columns_before = {row[0] for row in cur.fetchall()}
    assert "bootstrap_status" not in columns_before

    cm.ensure_tor_instances_table(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tor_instances';"
        )
        columns_after = {row[0] for row in cur.fetchall()}

    for column in (
        "bootstrap_status", "last_bootstrap_checked_at",
        "last_bootstrap_ready_at", "last_bootstrap_error_category",
    ):
        assert column in columns_after, column

    # Idempotent: running it again against an already-upgraded schema
    # must not error.
    cm.ensure_tor_instances_table(pg_conn)


def test_tor_instances_bootstrap_status_defaults_to_unknown(pg_conn, cm):
    cm.ensure_tor_instances_table(pg_conn)
    cm._ensure_instance_row(pg_conn, "fresh-instance")

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT bootstrap_status FROM tor_instances WHERE instance_key = %s;",
            ("fresh-instance",),
        )
        (bootstrap_status,) = cur.fetchone()

    assert bootstrap_status == cm.BOOTSTRAP_STATUS_UNKNOWN


# =====================================================================
# Bootstrap state persists across connections; readiness is NOT
# inferred from lock availability
# =====================================================================

def test_bootstrap_state_persists_across_connections(pg_conn, cm, obs):
    instance_key = "bootstrap-persist-test"
    cm.ensure_tor_instances_table(pg_conn)

    cm.record_bootstrap_started(pg_conn, instance_key)
    cm.record_bootstrap_ready(pg_conn, instance_key)

    fresh_conn = _second_connection()
    try:
        with fresh_conn.cursor() as cur:
            cur.execute(
                "SELECT bootstrap_status, last_bootstrap_ready_at FROM tor_instances WHERE instance_key = %s;",
                (instance_key,),
            )
            bootstrap_status, last_bootstrap_ready_at = cur.fetchone()
    finally:
        fresh_conn.close()

    assert bootstrap_status == cm.BOOTSTRAP_STATUS_READY
    assert last_bootstrap_ready_at is not None


def test_bootstrap_readiness_is_independent_of_instance_lock_state(pg_conn, cm, obs, monkeypatch):
    """The core correctness proof, against a real database: holding the
    instance's NEWNYM ControlPort lock must NOT suppress a persisted
    'ready' bootstrap status, and releasing it must NOT manufacture
    readiness for an instance that was never actually checked."""
    monkeypatch.setenv("TOR_CONTROL_HOST", "readiness-test-host")
    monkeypatch.setenv("TOR_CONTROL_PORT", "9051")

    instance_key = cm._instance_lock_key("readiness-test-host", 9051)
    cm.ensure_tor_instances_table(pg_conn)

    # Case 1: bootstrap confirmed ready, THEN the lock is independently
    # held by another session -- readiness must survive.
    cm.record_bootstrap_started(pg_conn, instance_key)
    cm.record_bootstrap_ready(pg_conn, instance_key)

    holder_conn = _second_connection()
    try:
        assert cm.try_acquire_advisory_lock(holder_conn, instance_key) is True

        snapshot = obs.inspect_instance(pg_conn)
        assert snapshot["is_locked"] is True
        assert snapshot["is_ready"] is True
    finally:
        cm.release_advisory_lock(holder_conn, instance_key)
        holder_conn.close()

    # Case 2: a DIFFERENT, never-checked instance -- lock is free, but
    # readiness must still be False (never assumed from "not busy").
    other_instance_key = cm._instance_lock_key("never-checked-host", 9051)
    monkeypatch.setenv("TOR_CONTROL_HOST", "never-checked-host")

    snapshot2 = obs.inspect_instance(pg_conn)
    assert snapshot2["is_locked"] is False
    assert snapshot2["is_ready"] is False
    assert snapshot2["bootstrap_status"] == cm.BOOTSTRAP_STATUS_UNKNOWN


def test_pg_locks_busy_state_independent_of_bootstrap_status(pg_conn, cm, obs):
    """Restates the original pg_locks proof (still required by section
    8) alongside the Phase 2 bootstrap columns, on the SAME instance
    row, to confirm the two facts genuinely don't interfere with each
    other's queries."""
    instance_key = "pg-locks-independent-test"
    cm.ensure_tor_instances_table(pg_conn)
    cm.record_bootstrap_ready(pg_conn, instance_key)

    conn_b = _second_connection()
    try:
        assert obs._is_lock_held(conn_b, instance_key) is False
        assert cm.try_acquire_advisory_lock(pg_conn, instance_key) is True
        assert obs._is_lock_held(conn_b, instance_key) is True

        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT bootstrap_status FROM tor_instances WHERE instance_key = %s;",
                (instance_key,),
            )
            (bootstrap_status,) = cur.fetchone()
        assert bootstrap_status == cm.BOOTSTRAP_STATUS_READY

        cm.release_advisory_lock(pg_conn, instance_key)
        assert obs._is_lock_held(conn_b, instance_key) is False
    finally:
        conn_b.close()


# =====================================================================
# Event insert + retention prune are atomic (real PostgreSQL 16)
# =====================================================================

def test_event_insert_and_retention_prune_are_atomic_on_success(pg_conn, cm):
    cm.ensure_tor_circuit_events_table(pg_conn)

    cm.emit_event(pg_conn, cm.EVENT_RECOVERED, circuit_key="atomic-success-test")

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM tor_circuit_events WHERE circuit_key = %s;",
            ("atomic-success-test",),
        )
        (count,) = cur.fetchone()
    assert count == 1


def test_failed_prune_does_not_leave_a_committed_event(pg_conn, cm, monkeypatch):
    """Deliberately makes the prune DELETE fail against a REAL
    PostgreSQL 16 connection (a negative OFFSET is invalid SQL --
    PostgreSQL raises 'OFFSET must not be negative') and proves the
    preceding INSERT was never left committed on its own."""
    cm.ensure_tor_circuit_events_table(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tor_circuit_events;")
        (count_before,) = cur.fetchone()

    with mock.patch.object(cm, "get_tor_event_max_rows", return_value=-1):
        with pytest.raises(Exception):
            cm.emit_event(pg_conn, cm.EVENT_RECOVERED, circuit_key="atomic-failure-test")

    # The failed transaction leaves pg_conn's own session in an aborted
    # state until rolled back -- exactly what emit_event()'s except
    # block does internally. A FRESH connection proves the INSERT was
    # never durably committed, independent of pg_conn's own session state.
    fresh_conn = _second_connection()
    try:
        with fresh_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM tor_circuit_events WHERE circuit_key = %s;",
                ("atomic-failure-test",),
            )
            (count_for_key,) = cur.fetchone()
    finally:
        fresh_conn.close()

    assert count_for_key == 0, "the INSERT must not be committed when the prune step fails"

    # pg_conn itself must remain usable afterward -- emit_event's own
    # rollback() must have cleared the aborted-transaction state, not
    # left the connection stuck.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tor_circuit_events;")
        (count_after,) = cur.fetchone()
    assert count_after == count_before


# =====================================================================
# Detail validators reject unsafe values BEFORE any SQL insertion
# =====================================================================

def test_detail_validators_reject_unsafe_values_before_sql_insertion(pg_conn, cm):
    cm.ensure_tor_circuit_events_table(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tor_circuit_events;")
        (count_before,) = cur.fetchone()

    unsafe_details = [
        {"reason_code": "'; DROP TABLE tor_circuit_events; --"},
        {"reason_code": "has spaces and Prose"},
        {"error_category": "not_a_real_category"},
        {"duration_seconds": float("inf")},
        {"attempt_count": -1},
    ]

    for detail in unsafe_details:
        with pytest.raises(ValueError):
            cm.emit_event(pg_conn, cm.EVENT_ROTATION_FAILED, circuit_key="unsafe-detail-test", detail=detail)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tor_circuit_events;")
        (count_after,) = cur.fetchone()

    assert count_after == count_before, "no unsafe detail value may ever reach an INSERT statement"


# =====================================================================
# observability.py is genuinely read-only (real PostgreSQL 16 proof)
# =====================================================================

def test_observability_never_creates_tables_on_a_fresh_database(pg_conn, obs):
    """The strongest possible proof: call every observability function
    against a REAL database where none of the three Phase 2 tables have
    ever been created, and confirm none of them exist afterward either."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('tor_circuits');")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT to_regclass('tor_instances');")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT to_regclass('tor_circuit_events');")
        assert cur.fetchone()[0] is None

    circuit_result = obs.inspect_circuit(pg_conn, "never-created")
    instance_result = obs.inspect_instance(pg_conn)
    metrics_result = obs.metrics_snapshot(pg_conn)

    assert circuit_result is None
    assert instance_result["bootstrap_status"] == "unknown"
    assert instance_result["is_ready"] is False
    assert metrics_result["tor_circuits_total"] == 0
    assert metrics_result["tor_instances_ready"] == 0
    assert metrics_result["event_window"]["retained_rows"] == 0

    # The real proof: still absent after every observability call above.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('tor_circuits');")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT to_regclass('tor_instances');")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT to_regclass('tor_circuit_events');")
        assert cur.fetchone()[0] is None


def test_observability_reports_honestly_once_tables_exist_but_are_empty(pg_conn, cm, obs):
    """Tables created (by circuit_manager.py, the only thing allowed to
    do that) but genuinely empty -- observability must report zero/
    not-ready, not error, and still create nothing further itself."""
    cm.ensure_tor_circuits_table(pg_conn)
    cm.ensure_tor_instances_table(pg_conn)
    cm.ensure_tor_circuit_events_table(pg_conn)

    assert obs.inspect_circuit(pg_conn, "never-created-either") is None

    instance_result = obs.inspect_instance(pg_conn)
    assert instance_result["bootstrap_status"] == "unknown"
    assert instance_result["is_ready"] is False

    metrics_result = obs.metrics_snapshot(pg_conn)
    assert metrics_result["tor_circuits_total"] == 0
    assert metrics_result["event_window"]["retained_rows"] == 0


# =====================================================================
# Mutation inputs validated BEFORE mutation (real PostgreSQL 16)
# =====================================================================

def test_quarantine_circuit_unsafe_reason_code_leaves_no_row_or_event(pg_conn, cm):
    circuit_key = "quarantine-validation-test"

    with pytest.raises(ValueError):
        cm.quarantine_circuit(circuit_key=circuit_key, reason_code="has spaces and Prose")

    fresh_conn = _second_connection()
    try:
        with fresh_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('tor_circuits');")
            circuits_table_exists = cur.fetchone()[0] is not None

            if circuits_table_exists:
                cur.execute(
                    "SELECT COUNT(*) FROM tor_circuits WHERE circuit_key = %s;",
                    (circuit_key,),
                )
                (circuit_row_count,) = cur.fetchone()
            else:
                circuit_row_count = 0

            cur.execute("SELECT to_regclass('tor_circuit_events');")
            events_table_exists = cur.fetchone()[0] is not None

            if events_table_exists:
                cur.execute(
                    "SELECT COUNT(*) FROM tor_circuit_events WHERE circuit_key = %s;",
                    (circuit_key,),
                )
                (event_count,) = cur.fetchone()
            else:
                event_count = 0
    finally:
        fresh_conn.close()

    assert circuit_row_count == 0, "an invalid reason_code must never even create the circuit row"
    assert event_count == 0, "an invalid reason_code must never emit a quarantined event"


def test_record_bootstrap_failed_unsafe_error_category_leaves_no_row_or_event(pg_conn, cm):
    instance_key = "bootstrap-failed-validation-test"

    with pytest.raises(ValueError):
        cm.record_bootstrap_failed(pg_conn, instance_key, "not_a_real_category")

    fresh_conn = _second_connection()
    try:
        with fresh_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('tor_instances');")
            instances_table_exists = cur.fetchone()[0] is not None

            if instances_table_exists:
                cur.execute(
                    "SELECT COUNT(*) FROM tor_instances WHERE instance_key = %s;",
                    (instance_key,),
                )
                (instance_row_count,) = cur.fetchone()
            else:
                instance_row_count = 0

            cur.execute("SELECT to_regclass('tor_circuit_events');")
            events_table_exists = cur.fetchone()[0] is not None

            if events_table_exists:
                cur.execute(
                    "SELECT COUNT(*) FROM tor_circuit_events WHERE instance_key = %s;",
                    (instance_key,),
                )
                (event_count,) = cur.fetchone()
            else:
                event_count = 0
    finally:
        fresh_conn.close()

    assert instance_row_count == 0, "an invalid error_category must never even create the instance row"
    assert event_count == 0, "an invalid error_category must never emit a bootstrap_failed event"
