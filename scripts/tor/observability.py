"""Phase 2 read-only observability for the Tor circuit control plane.

Every function in this module executes ONLY SELECT statements (plus, on
the connection object itself, `.cursor()`/`.close()` on cursors) --
never CREATE, ALTER, INSERT, UPDATE, DELETE, and never calls
connection.commit()/connection.rollback(). In particular, this module
NEVER calls circuit_manager.py's ensure_tor_circuits_table()/
ensure_tor_instances_table()/ensure_tor_circuit_events_table() (each of
those runs CREATE TABLE IF NOT EXISTS and, for two of them, an ALTER TABLE
ADD COLUMN IF NOT EXISTS -- real schema mutations, entirely appropriate
for circuit_manager.py's own mutating operations, but not something a
module claiming to be read-only may trigger as a side effect of merely
being asked to look at something).

Table existence is instead checked honestly via a read-only
`SELECT to_regclass(...)` before querying it. If a Phase 2 table has
never been created yet (e.g. observability is queried before any
circuit_manager.py operation has ever run), this module reports the
absence honestly rather than manufacturing state:
  - tor_circuits absent  -> inspect_circuit() returns None;
    metrics_snapshot()'s circuit counts are all 0
  - tor_instances absent -> inspect_instance() reports
    bootstrap_status='unknown', is_ready=False (never assumed ready)
  - tor_circuit_events absent -> all *_retained counters are 0, duration
    aggregates are empty, event_window reports retained_rows=0

All mutation happens exclusively in scripts/tor/circuit_manager.py; this
module only reads what that one has already written (or honestly
reports that it hasn't written anything yet).

Single-instance model (Phase 2, design section H): this module does NOT
introduce a multi-instance Tor pool. The "instance" it reports on is
always the CURRENTLY CONFIGURED ControlPort endpoint
(TOR_CONTROL_HOST/TOR_CONTROL_PORT) -- host/port/SOCKS values returned
by inspect_instance() are read LIVE from app.config, not from a
historical per-row record; tor_instances itself only ever stores
last_newnym_at, keyed by an instance_key derived from that same live
configuration. inspect_instance()'s "endpoint_source" field makes this
explicit. A real multi-instance pool would need a different data model
and is out of scope here.

Lock/busy inspection (design section G): deliberately does NOT call
pg_try_advisory_lock to check whether a lock is held -- doing so would
itself briefly TAKE the lock just to answer the question, contending
with a real in-flight rotation. Instead this queries pg_locks directly.
The exact locktype/classid/objid/objsubid tuple PostgreSQL uses for the
single-bigint-argument pg_try_advisory_lock()/pg_advisory_lock() family
was proven against a real, disposable PostgreSQL 16 instance BEFORE this
module was written (three cases: held, explicitly unlocked while the
session stays open, and released automatically when the session
disconnects without an explicit unlock -- the crash-equivalent case).
See tests/test_tor_circuit_manager_postgres_integration.py for the
automated version of that same proof. The mapping: locktype='advisory',
classid=0 (our lock keys -- see circuit_manager._advisory_lock_key --
are always a non-negative 31-bit CRC32 value, so the high 32 bits of the
bigint key argument are always 0), objid=<key>, objsubid=1,
granted=true. pg_locks is a PostgreSQL system view, always queryable --
no existence check needed for it.
"""
from app.config import get_tor_socks_host, get_tor_socks_port
import scripts.tor.circuit_manager as cm


def _table_exists(connection, table_name: str) -> bool:
    """Read-only existence check via to_regclass -- returns NULL (not an
    error) for a table that doesn't exist yet, which is exactly the
    honest, non-mutating signal every function below needs before
    querying a Phase 2 table that circuit_manager.py may not have
    created yet."""
    cursor = connection.cursor()
    cursor.execute("SELECT to_regclass(%s);", (table_name,))
    (regclass,) = cursor.fetchone()
    cursor.close()
    return regclass is not None


def _is_lock_held(connection, lock_scope: str) -> bool:
    """Non-mutating advisory-lock busy check via pg_locks -- see module
    docstring for the proven classid/objid/objsubid mapping. Never calls
    pg_try_advisory_lock/pg_advisory_unlock. pg_locks is a PostgreSQL
    system view and always exists -- no _table_exists check needed."""
    key = cm._advisory_lock_key(lock_scope)

    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory'
              AND classid = 0
              AND objid = %s
              AND objsubid = 1
              AND granted = true
        );
        """,
        (key,),
    )
    (is_held,) = cursor.fetchone()
    cursor.close()
    return bool(is_held)


def inspect_circuit(connection, circuit_key: str = cm.DEFAULT_CIRCUIT_KEY):
    """Read-only snapshot of one circuit.

    Deliberately does NOT call circuit_manager.get_circuit_state(): that
    function (a) calls ensure_tor_circuits_table() internally (a real
    schema mutation this module must never trigger) and (b) opens its
    OWN connection via psycopg2.connect(**get_postgres_config()) rather
    than using the connection passed in here. This function instead
    queries the SAME columns directly, read-only, on the connection the
    caller supplied.

    Returns None if tor_circuits doesn't exist yet, or if this specific
    circuit_key has never been created -- both cases mean "nothing to
    report," never an error."""
    if not _table_exists(connection, "tor_circuits"):
        return None

    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT circuit_key, status, request_count, last_exit_ip,
               last_rotated_at, cooldown_until, failure_count,
               consecutive_failure_count, last_error_category,
               last_verified_at, updated_at
        FROM tor_circuits
        WHERE circuit_key = %s;
        """,
        (circuit_key,),
    )
    row = cursor.fetchone()
    cursor.close()

    if row is None:
        return None

    state = {
        "circuit_key": row[0],
        "status": row[1],
        "request_count": row[2],
        "last_exit_ip": row[3],
        "last_rotated_at": row[4],
        "cooldown_until": row[5],
        "failure_count": row[6],
        "consecutive_failure_count": row[7],
        "last_error_category": row[8],
        "last_verified_at": row[9],
        "updated_at": row[10],
    }

    is_locked = _is_lock_held(connection, circuit_key)
    is_stale_draining = (
        state["status"] == cm.STATUS_DRAINING
        and cm.is_draining_stale(state["updated_at"])
    )

    return {
        **state,
        "is_locked": is_locked,
        "is_stale_draining": is_stale_draining,
    }


def inspect_instance(connection):
    """Read-only snapshot of THE single configured Tor instance (see
    module docstring -- Phase 2 introduces no instance pool). The
    ControlPort password is never read or returned here.

    `is_locked` (whether the NEWNYM ControlPort lock is currently held)
    and `is_ready` (whether bootstrap has actually been OBSERVED and
    confirmed at 100%, via scripts/tor/verify_tor_connectivity.py's
    check_bootstrap_status() -> circuit_manager.record_bootstrap_ready())
    are DELIBERATELY independent facts -- a free lock says nothing about
    whether Tor has ever finished bootstrapping. If tor_instances
    doesn't exist yet, or this instance's row has never been created, or
    bootstrap has never been checked (bootstrap_status == 'unknown', the
    schema DEFAULT), is_ready is False, never True -- readiness is never
    assumed, only ever explicitly observed."""
    control_host = cm.get_tor_control_host()
    control_port = cm.get_tor_control_port()
    instance_key = cm._instance_lock_key(control_host, control_port)

    row = None

    if _table_exists(connection, "tor_instances"):
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT last_newnym_at, bootstrap_status, last_bootstrap_checked_at,
                   last_bootstrap_ready_at, last_bootstrap_error_category
            FROM tor_instances WHERE instance_key = %s;
            """,
            (instance_key,),
        )
        row = cursor.fetchone()
        cursor.close()

    if row is None:
        last_newnym_at = None
        bootstrap_status = cm.BOOTSTRAP_STATUS_UNKNOWN
        last_bootstrap_checked_at = None
        last_bootstrap_ready_at = None
        last_bootstrap_error_category = None
    else:
        (
            last_newnym_at, bootstrap_status, last_bootstrap_checked_at,
            last_bootstrap_ready_at, last_bootstrap_error_category,
        ) = row

    is_locked = _is_lock_held(connection, instance_key)
    is_ready = bootstrap_status == cm.BOOTSTRAP_STATUS_READY

    return {
        "instance_key": instance_key,
        "control_host": control_host,
        "control_port": control_port,
        "socks_host": get_tor_socks_host(),
        "socks_port": get_tor_socks_port(),
        "last_newnym_at": last_newnym_at,
        "is_locked": is_locked,
        "bootstrap_status": bootstrap_status,
        "is_ready": is_ready,
        "last_bootstrap_checked_at": last_bootstrap_checked_at,
        "last_bootstrap_ready_at": last_bootstrap_ready_at,
        "last_bootstrap_error_category": last_bootstrap_error_category,
        # Explicit per design section H: these host/port values are read
        # live from the active configuration on every call, not stored
        # per-row -- there is no historical "this instance used to be at
        # a different endpoint" record anywhere in this schema.
        "endpoint_source": "active_configuration",
    }


_EMPTY_DURATION_AGGREGATE = {
    "scope": "retained_window",
    "count": 0,
    "sum": 0.0,
    "min": None,
    "max": None,
    "avg": None,
}


def _event_count(connection, event_type: str) -> int:
    if not _table_exists(connection, "tor_circuit_events"):
        return 0

    cursor = connection.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM tor_circuit_events WHERE event_type = %s;",
        (event_type,),
    )
    (count,) = cursor.fetchone()
    cursor.close()
    return count


def _duration_aggregate(connection, event_type: str) -> dict:
    """count/sum/min/max/avg of detail->>'duration_seconds' for one
    event_type, over whatever events retention has not yet pruned. No
    IP, circuit_key, or instance_key label -- this is a single scalar
    aggregate per event_type, not a per-circuit/per-instance series.
    `scope` is always 'retained_window': explicitly NOT a lifetime
    aggregate -- see metrics_snapshot()'s module-level note on why
    these numbers can only ever describe the currently-retained events,
    never a true historical total. Returns the honest all-zero/None
    shape if tor_circuit_events doesn't exist yet."""
    if not _table_exists(connection, "tor_circuit_events"):
        return dict(_EMPTY_DURATION_AGGREGATE)

    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM((detail->>'duration_seconds')::double precision), 0),
            MIN((detail->>'duration_seconds')::double precision),
            MAX((detail->>'duration_seconds')::double precision),
            AVG((detail->>'duration_seconds')::double precision)
        FROM tor_circuit_events
        WHERE event_type = %s
          AND detail ? 'duration_seconds';
        """,
        (event_type,),
    )
    count, total, minimum, maximum, average = cursor.fetchone()
    cursor.close()

    return {
        "scope": "retained_window",
        "count": count,
        "sum": float(total) if total is not None else 0.0,
        "min": float(minimum) if minimum is not None else None,
        "max": float(maximum) if maximum is not None else None,
        "avg": float(average) if average is not None else None,
    }


def _event_window_metadata(connection) -> dict:
    """Describes the CURRENT extent of the retained event window itself
    -- how many rows are actually present right now, the configured cap,
    and the oldest/newest event timestamps still retained. Exists so
    every *_retained counter and duration aggregate below can be read
    alongside honest context about what "retained" currently covers,
    rather than requiring the caller to separately know
    TOR_EVENT_MAX_ROWS. Returns retained_rows=0 with no
    oldest/newest timestamps if tor_circuit_events doesn't exist yet."""
    if not _table_exists(connection, "tor_circuit_events"):
        return {
            "retained_rows": 0,
            "max_rows": cm.get_tor_event_max_rows(),
            "oldest_event_at": None,
            "newest_event_at": None,
        }

    cursor = connection.cursor()
    cursor.execute(
        "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM tor_circuit_events;"
    )
    retained_rows, oldest_event_at, newest_event_at = cursor.fetchone()
    cursor.close()

    return {
        "retained_rows": retained_rows,
        "max_rows": cm.get_tor_event_max_rows(),
        "oldest_event_at": oldest_event_at,
        "newest_event_at": newest_event_at,
    }


def metrics_snapshot(connection) -> dict:
    """Read-only structured metrics derived from persisted state and the
    bounded tor_circuit_events history.

    Deliberately NOT process-local authoritative counters (this process
    may not be the one that actually performed any of the underlying
    events -- circuit_manager.py's operations are typically invoked from
    separate short-lived CLI processes) and NOT a Prometheus exposition
    (that integration, if wanted, is a later decision explicitly
    deferred here per the Phase 2 design review).

    IMPORTANT naming/semantics correction: the tor_*_retained counters
    below are NOT lifetime totals. Event retention (TOR_EVENT_MAX_ROWS,
    see circuit_manager.emit_event()) deletes the oldest rows once the
    table exceeds its cap, and does so GLOBALLY across all event types,
    not per type -- a burst of lock_contended/cooldown_blocked events
    can push older rotation_success/verification_success events out of
    the window entirely. These keys were renamed from an earlier
    `*_total` naming, which falsely implied a persistent monotonic
    count, specifically so no caller mistakes a bounded, prunable
    snapshot for a true historical total. See event_window below for the
    exact extent of what "retained" covers right now (row count,
    configured cap, oldest/newest timestamps still present) -- read
    together, not in isolation.

    tor_instances_total/ready/quarantined reflect Phase 2's single-
    configured-instance model (see module docstring, design section H):
    there is always exactly one configured instance (total=1);
    "quarantined" has no independent meaning for an instance in this
    schema -- only circuits carry a quarantined status -- so
    tor_instances_quarantined is always 0. tor_instances_ready reflects
    ONLY the persisted, explicitly-observed bootstrap_status (== 'ready'
    -- see inspect_instance()'s is_ready) -- it is NOT derived from
    whether the instance's ControlPort lock happens to be free right
    now; those are independent facts, and a never-checked instance
    reports not-ready (0), never a default of ready.

    Every code path in this function -- including when tor_circuits or
    tor_circuit_events has never been created yet -- executes SELECT
    statements only; no table is ever created as a side effect of
    calling this function.

    No IP, circuit_key, or instance_key label anywhere in the output.
    """
    if _table_exists(connection, "tor_circuits"):
        cursor = connection.cursor()
        cursor.execute("SELECT status, COUNT(*) FROM tor_circuits GROUP BY status;")
        status_counts = {status: count for status, count in cursor.fetchall()}
        cursor.close()
    else:
        status_counts = {}

    circuits_ready = status_counts.get(cm.STATUS_READY, 0)
    circuits_quarantined = status_counts.get(cm.STATUS_QUARANTINED, 0)
    circuits_total = sum(status_counts.values())

    instance = inspect_instance(connection)

    return {
        "tor_instances_total": 1,
        "tor_instances_ready": 1 if instance["is_ready"] else 0,
        "tor_instances_quarantined": 0,
        "tor_circuits_total": circuits_total,
        "tor_circuits_ready": circuits_ready,
        "tor_circuits_quarantined": circuits_quarantined,
        "tor_rotation_attempts_retained": _event_count(connection, cm.EVENT_ROTATION_REQUESTED),
        "tor_rotation_success_retained": _event_count(connection, cm.EVENT_ROTATION_SUCCESS),
        "tor_rotation_failures_retained": _event_count(connection, cm.EVENT_ROTATION_FAILED),
        "tor_verification_failures_retained": _event_count(connection, cm.EVENT_VERIFICATION_FAILED),
        "tor_lock_contention_retained": _event_count(connection, cm.EVENT_LOCK_CONTENDED),
        "tor_rotation_duration_seconds": _duration_aggregate(connection, cm.EVENT_ROTATION_SUCCESS),
        "tor_verification_duration_seconds": _duration_aggregate(connection, cm.EVENT_VERIFICATION_SUCCESS),
        "event_window": _event_window_metadata(connection),
    }
