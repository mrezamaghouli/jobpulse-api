"""Central Tor circuit lifecycle control (Phase 1: manually controlled;
Phase 2: adds observability, structured events, and explicit manual
verify/quarantine/recover -- still no automatic rotation of any kind).

Collector subprocesses must never talk to the Tor control port directly.
This module is the only thing that does.

Ownership model: two PostgreSQL advisory locks (mutual exclusion) plus
one persisted timer (cooldown/timing) -- these are orthogonal concerns.
Holding a lock only means "no one else may act right now"; it says
nothing about whether Tor itself would consider another NEWNYM
worthwhile yet. Conflating the two was the Phase-1 gap this module
originally had.

  1. circuit_key lock -- guards the logical circuit's row in
     `tor_circuits` (its draining/ready/quarantined bookkeeping). Keeps
     two processes from editing the SAME named circuit's state at once.

  2. Tor-instance lock, keyed by (control_host, control_port) -- guards
     the actual NEWNYM ControlPort call. NEWNYM mutates the whole Tor
     PROCESS, not one named application-level circuit_key. Phase 1 runs
     a single Tor instance, but nothing stops two different circuit_key
     values from being configured to point at that same instance's
     control port -- without this second lock, both could send NEWNYM
     concurrently, which is exactly the race this tier exists to close.
     request_new_identity() acquires/releases it internally; callers
     only need to hold the circuit_key lock (tier 1) before calling in.

  3. Persisted per-instance NEWNYM cooldown clock (`tor_instances.
     last_newnym_at`) -- stem's own Controller.is_newnym_available() /
     get_newnym_wait() track a `_last_newnym` timestamp that lives on
     the Python Controller OBJECT and resets to 0 on every new
     connection (verified against the installed stem 1.8.2 source).
     Since this module opens a fresh control-port connection on every
     request_new_identity() call -- and every collector subprocess is a
     fresh process -- that in-object bookkeeping alone can NEVER detect
     cooldown across separate invocations; a brand-new Controller always
     reports "available". This table is what actually enforces "don't
     fire NEWNYM again too soon" across those separate invocations.
     stem's live check is still consulted per connection (see
     request_new_identity) as a defensive, honestly-inert-today
     secondary signal -- kept for forward compatibility with a future
     phase that might reuse a longer-lived Controller connection, where
     it would become load-bearing. It is not what makes cooldown work
     today; the persisted timer is.

Phase 1 scope: a single manually-triggered lifecycle --
    acquire circuit lock -> mark draining -> NEWNYM (acquires/releases
    the instance lock internally, respects the persisted cooldown) ->
    optional verify -> mark ready (or quarantined on failure) ->
    release circuit lock
driven by a CLI entry point (`python -m scripts.tor.circuit_manager`).

Phase 2 scope (this module, added on top of Phase 1 without changing its
behavior): read-only observability fields on tor_circuits (failure
counters, last verified time, last error category -- see
scripts/tor/observability.py for the read side), a bounded structured
event log (tor_circuit_events), and three new EXPLICIT, OPERATOR/TEST
invoked operations: verify_circuit() (verification only, never NEWNYM),
quarantine_circuit(), and recover_circuit(). None of Phase 2 is wired to
any real target's HTTP response -- there is no automatic rotation,
automatic quarantine, or automatic recovery based on a 429/403/timeout
from anywhere. That remains a later, NOT-yet-implemented phase, and
must not be added here without updating this module's scope note.

Both advisory lock tiers are strictly non-blocking (pg_try_advisory_lock):
a contended lock raises immediately rather than queuing, so callers
never busy-wait or retry in a tight loop. Cooldown waits are bounded by
TOR_NEWNYM_MAX_WAIT_SECONDS -- at most one `time.sleep()`, never a loop;
exceeding that bound raises a transient NewnymCooldownError instead of
blocking indefinitely.

NEWNYM does not guarantee a different exit IP -- callers that need IP
verification must pass verify_fn explicitly; this module never assumes
success without it. Nor does Tor's control protocol reject a NEWNYM
sent before the cooldown interval elapses -- it is accepted but widely
considered ineffective (circuits take time to rebuild); the cooldown
here exists to avoid wasting NEWNYM calls, not to satisfy a protocol
requirement we've verified Tor enforces server-side.

Failure-counter semantics (Phase 2):
  - GENUINE failures (ControlPort connection/authentication failure,
    NEWNYM exchange failure, verification failure) increment both
    failure_count and consecutive_failure_count and record a normalized
    last_error_category. Only rotate_circuit()'s own control-port/NEWNYM
    or verify_fn failure also quarantines the circuit; verify_circuit()
    failures record the same counters but deliberately do NOT quarantine
    (see verify_circuit's docstring).
  - TRANSIENT states (CircuitLockError, TorInstanceBusyError,
    NewnymCooldownError) NEVER touch failure_count or
    consecutive_failure_count and never quarantine -- contention and
    cooldown are not circuit faults.
  - A successful VERIFIED rotation, or a successful standalone
    verify_circuit(), resets consecutive_failure_count to 0, clears
    last_error_category, and updates last_verified_at.
  - A successful rotation WITHOUT verification (no verify_fn passed)
    never touches last_verified_at (a rotation is not proof of a working
    exit IP) and leaves failure_count/consecutive_failure_count/
    last_error_category untouched -- an unverified rotation proves
    nothing about whether the circuit actually works, so it must not be
    allowed to silently clear failure history either.
  - Manual quarantine_circuit()/recover_circuit() are explicit operator
    actions: neither increments or resets the failure counters. Only a
    genuine failure increments them, and only a verified success resets
    them -- manual state changes leave that history alone.
"""
import argparse
import json
import math
import re
import time
import zlib
from datetime import datetime, timezone

import psycopg2
from stem import Signal
from stem.control import Controller

from app.config import (
    get_postgres_config,
    get_tor_control_host,
    get_tor_control_password,
    get_tor_control_port,
    get_tor_event_max_rows,
    get_tor_newnym_max_wait_seconds,
    get_tor_newnym_min_interval_seconds,
    get_tor_stale_draining_threshold_seconds,
)


STATUS_READY = "ready"
STATUS_DRAINING = "draining"
STATUS_COOLING_DOWN = "cooling_down"
STATUS_QUARANTINED = "quarantined"

DEFAULT_CIRCUIT_KEY = "default"

_ALLOWED_STATUS_FIELDS = {
    "request_count",
    "last_exit_ip",
    "last_rotated_at",
    "cooldown_until",
    "failure_count",
    "consecutive_failure_count",
    "last_error_category",
    "last_verified_at",
}

CREATE_TOR_CIRCUITS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tor_circuits (
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

# Phase 2 additions to a Phase-1-created (or fresh) tor_circuits table.
# ADD COLUMN IF NOT EXISTS makes this safe to run every time
# ensure_tor_circuits_table() runs, against either a database that
# already has these columns or one that has never seen Phase 2 at all --
# no separate one-shot migration step, no manual DBA action required.
#
# last_verified_at is DELIBERATELY separate from last_rotated_at: a
# rotation (NEWNYM) can succeed without ever being verified (no verify_fn
# passed), so "when did we last rotate" and "when did we last actually
# confirm the exit IP" are two different, independently-true facts and
# must never be inferred from one another.
ALTER_TOR_CIRCUITS_ADD_OBSERVABILITY_COLUMNS_SQL = """
ALTER TABLE tor_circuits
    ADD COLUMN IF NOT EXISTS failure_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_error_category VARCHAR(100),
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;
"""

CREATE_TOR_INSTANCES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tor_instances (
    id SERIAL PRIMARY KEY,
    instance_key VARCHAR(200) NOT NULL UNIQUE,
    last_newnym_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
"""

# Phase 2 correction: "the instance's NEWNYM ControlPort lock is not
# currently held" is NOT the same fact as "Tor has actually finished
# bootstrapping and is routable" -- a lock being free says nothing about
# whether anyone has ever successfully confirmed bootstrap. This persists
# an explicit, separately-observed bootstrap fact instead. Written only
# by scripts/tor/verify_tor_connectivity.py's check_bootstrap_status()
# (via record_bootstrap_started/ready/failed below) -- never inferred,
# never defaulted to "ready".
BOOTSTRAP_STATUS_UNKNOWN = "unknown"
BOOTSTRAP_STATUS_CHECKING = "checking"
BOOTSTRAP_STATUS_READY = "ready"
BOOTSTRAP_STATUS_FAILED = "failed"

_ALLOWED_BOOTSTRAP_STATUSES = {
    BOOTSTRAP_STATUS_UNKNOWN,
    BOOTSTRAP_STATUS_CHECKING,
    BOOTSTRAP_STATUS_READY,
    BOOTSTRAP_STATUS_FAILED,
}

ALTER_TOR_INSTANCES_ADD_BOOTSTRAP_COLUMNS_SQL = """
ALTER TABLE tor_instances
    ADD COLUMN IF NOT EXISTS bootstrap_status VARCHAR(50) NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS last_bootstrap_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_bootstrap_ready_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_bootstrap_error_category VARCHAR(100);
"""

# Bounded, secret-safe lifecycle event log. `detail` is restricted at the
# application layer (see _ALLOWED_EVENT_DETAIL_FIELDS/emit_event) to a
# small allowlist of normalized, non-secret fields -- never a raw
# exception string, never a ControlPort password, never cookies/session
# state, never browser headers, and never a duplicate of the exit IP
# already tracked on tor_circuits.last_exit_ip.
CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tor_circuit_events (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(50) NOT NULL,
    circuit_key VARCHAR(100),
    instance_key VARCHAR(200),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Bounded-inspection indexes only -- this table's own retention (see
# _prune_tor_circuit_events_in_transaction) is what keeps it small;
# these indexes just keep "recent events" / "events for this circuit" / "events of this
# type" queries from ever needing a full sequential scan.
CREATE_TOR_CIRCUIT_EVENTS_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_tor_circuit_events_created_at
    ON tor_circuit_events (created_at);
CREATE INDEX IF NOT EXISTS idx_tor_circuit_events_circuit_key
    ON tor_circuit_events (circuit_key);
CREATE INDEX IF NOT EXISTS idx_tor_circuit_events_event_type
    ON tor_circuit_events (event_type);
"""

# ---------------------------------------------------------------------
# Approved lifecycle event types (Phase 2, section B). bootstrap_started/
# bootstrap_ready/bootstrap_failed are emitted by
# record_bootstrap_started/ready/failed() below, called from
# scripts/tor/verify_tor_connectivity.py's check_bootstrap_status() --
# the control-plane/verification side observing real bootstrap state,
# never the docker/tor container itself (which stays PostgreSQL-free).
# ---------------------------------------------------------------------
EVENT_BOOTSTRAP_STARTED = "bootstrap_started"
EVENT_BOOTSTRAP_READY = "bootstrap_ready"
EVENT_BOOTSTRAP_FAILED = "bootstrap_failed"
EVENT_VERIFICATION_STARTED = "verification_started"
EVENT_VERIFICATION_SUCCESS = "verification_success"
EVENT_VERIFICATION_FAILED = "verification_failed"
EVENT_ROTATION_REQUESTED = "rotation_requested"
EVENT_ROTATION_STARTED = "rotation_started"
EVENT_ROTATION_SUCCESS = "rotation_success"
EVENT_ROTATION_FAILED = "rotation_failed"
EVENT_COOLDOWN_BLOCKED = "cooldown_blocked"
EVENT_LOCK_CONTENDED = "lock_contended"
EVENT_QUARANTINED = "quarantined"
EVENT_RECOVERED = "recovered"

_ALLOWED_EVENT_TYPES = {
    EVENT_BOOTSTRAP_STARTED,
    EVENT_BOOTSTRAP_READY,
    EVENT_BOOTSTRAP_FAILED,
    EVENT_VERIFICATION_STARTED,
    EVENT_VERIFICATION_SUCCESS,
    EVENT_VERIFICATION_FAILED,
    EVENT_ROTATION_REQUESTED,
    EVENT_ROTATION_STARTED,
    EVENT_ROTATION_SUCCESS,
    EVENT_ROTATION_FAILED,
    EVENT_COOLDOWN_BLOCKED,
    EVENT_LOCK_CONTENDED,
    EVENT_QUARANTINED,
    EVENT_RECOVERED,
}

# Normalized, fixed, non-secret failure categories -- never the raw
# exception text (which could, in principle, contain redacted-but-still
# sensitive detail depending on what stem/psycopg2 chooses to include).
ERROR_CATEGORY_CONTROL_PORT_FAILURE = "control_port_failure"
ERROR_CATEGORY_VERIFICATION_FAILED = "verification_failed"
ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE = "bootstrap_incomplete"

_ALLOWED_ERROR_CATEGORIES = {
    ERROR_CATEGORY_CONTROL_PORT_FAILURE,
    ERROR_CATEGORY_VERIFICATION_FAILED,
    ERROR_CATEGORY_BOOTSTRAP_INCOMPLETE,
}

# Event `detail` allowlist: normalized, bounded, non-secret fields only.
# Never a circuit_key/instance_key duplicate (those are already real
# columns), never an IP, never anything free-text from an exception.
# The field-NAME allowlist alone is not sufficient -- a free-text string,
# a URL, or secret material could still be smuggled in through
# `reason_code`'s VALUE even though its name is approved. Every value is
# validated below (_validate_event_detail) before any SQL executes.
_ALLOWED_EVENT_DETAIL_FIELDS = {
    "error_category",
    "duration_seconds",
    "attempt_count",
    "reason_code",
}

# reason_code must be a normalized, machine-readable token -- never
# free-form prose, a URL, or anything that could carry secret material.
_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")

# Generous but bounded upper limits -- reject implausible/malicious
# values outright rather than silently accepting them.
_MAX_DURATION_SECONDS = 3600
_MAX_ATTEMPT_COUNT = 1000

_MAX_EVENT_DETAIL_BYTES = 2048


def _validate_error_category(value) -> None:
    """Reusable validator: value must be a string drawn from the fixed
    _ALLOWED_ERROR_CATEGORIES enum. Shared by _validate_event_detail()
    (for the event `detail.error_category` field) and by any mutating
    function that accepts an error_category directly as an argument
    (e.g. record_bootstrap_failed()), so both call sites enforce
    IDENTICAL rules rather than two hand-maintained copies drifting
    apart. Raises ValueError -- callers must validate BEFORE any SQL
    executes, never after."""
    if not isinstance(value, str) or value not in _ALLOWED_ERROR_CATEGORIES:
        raise ValueError(f"Invalid error_category: {value!r}")


def _validate_reason_code(value) -> None:
    """Reusable validator: value must be a normalized, machine-readable
    token matching _REASON_CODE_PATTERN. Shared by _validate_event_detail()
    (for the event `detail.reason_code` field) and by any mutating
    function that accepts a reason_code directly as an argument (e.g.
    quarantine_circuit()), so both call sites enforce IDENTICAL rules.
    Raises ValueError -- callers must validate BEFORE any SQL executes,
    never after."""
    if not isinstance(value, str) or not _REASON_CODE_PATTERN.match(value):
        raise ValueError(f"Invalid reason_code: {value!r} (must match {_REASON_CODE_PATTERN.pattern})")


def _validate_event_detail(detail: dict) -> None:
    """Validates both the FIELD NAMES (already-established allowlist)
    and, critically, the VALUES -- a value-blind allowlist would still
    let a caller smuggle a secret, a URL, or arbitrary prose through a
    permitted field name like reason_code. Raises ValueError, before any
    SQL executes, on the first invalid field encountered. error_category/
    reason_code validation is delegated to the shared
    _validate_error_category()/_validate_reason_code() helpers above so
    this never drifts from what a direct mutating-function argument
    (record_bootstrap_failed's error_category, quarantine_circuit's
    reason_code) accepts."""
    unknown_fields = set(detail) - _ALLOWED_EVENT_DETAIL_FIELDS

    if unknown_fields:
        raise ValueError(f"Unsupported event detail field(s): {sorted(unknown_fields)}")

    if "error_category" in detail:
        _validate_error_category(detail["error_category"])

    if "reason_code" in detail:
        _validate_reason_code(detail["reason_code"])

    if "duration_seconds" in detail:
        value = detail["duration_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid duration_seconds: {value!r} (must be int or float)")
        if not math.isfinite(value):
            raise ValueError(f"Invalid duration_seconds: {value!r} (must be finite)")
        if value < 0 or value > _MAX_DURATION_SECONDS:
            raise ValueError(f"duration_seconds out of range: {value!r} (0 <= x <= {_MAX_DURATION_SECONDS})")

    if "attempt_count" in detail:
        value = detail["attempt_count"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Invalid attempt_count: {value!r} (must be int)")
        if value < 0 or value > _MAX_ATTEMPT_COUNT:
            raise ValueError(f"attempt_count out of range: {value!r} (0 <= x <= {_MAX_ATTEMPT_COUNT})")


class CircuitLockError(Exception):
    """Raised when the circuit_key advisory lock cannot be acquired."""


class TorInstanceBusyError(Exception):
    """Raised when another in-flight rotation already holds the lock for
    this Tor instance's ControlPort -- NEWNYM is instance-scoped, not
    circuit-scoped (see module docstring). Transient contention, not a
    circuit fault: callers must not busy-retry in a tight loop."""


class NewnymCooldownError(Exception):
    """Raised when a NEWNYM cooldown (persisted cross-invocation timer,
    or -- defensively -- stem's own live per-connection check) is active
    longer than TOR_NEWNYM_MAX_WAIT_SECONDS. Transient timing state, not
    a circuit or Tor-instance fault: callers must not busy-retry."""


class CircuitRotationError(Exception):
    """Raised when the Tor control-port NEWNYM exchange itself fails."""


def _utc_now():
    return datetime.now(timezone.utc)


def _advisory_lock_key(lock_scope: str) -> int:
    """CRC32 is a 32-bit hash: two different lock_scope strings could in
    principle collide onto the same key. Accepted for Phase 1/2's tiny,
    known key space (a handful of circuit_keys plus one tor-instance
    key per configured Tor instance) -- revisit with a wider hash or an
    allocated-key table if the number of distinct scopes grows large."""
    return zlib.crc32(lock_scope.encode("utf-8")) & 0x7FFFFFFF


def _instance_lock_key(control_host: str, control_port: int) -> str:
    return f"tor-instance:{control_host}:{control_port}"


def _redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


def ensure_tor_circuits_table(connection) -> None:
    """Idempotent: safe to call on every startup/every operation, against
    a database that has never seen this table, one created by Phase 1
    only, or one already fully upgraded to Phase 2 -- CREATE TABLE IF NOT
    EXISTS plus ADD COLUMN IF NOT EXISTS never fail or duplicate work on
    repeated runs."""
    cursor = connection.cursor()
    cursor.execute(CREATE_TOR_CIRCUITS_TABLE_SQL)
    cursor.execute(ALTER_TOR_CIRCUITS_ADD_OBSERVABILITY_COLUMNS_SQL)
    connection.commit()
    cursor.close()


def _ensure_circuit_row(connection, circuit_key: str) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO tor_circuits (circuit_key, status)
        VALUES (%s, %s)
        ON CONFLICT (circuit_key) DO NOTHING;
        """,
        (circuit_key, STATUS_READY),
    )
    connection.commit()
    cursor.close()


def try_acquire_advisory_lock(connection, lock_scope: str) -> bool:
    """Non-blocking: returns False immediately if another session already
    holds this lock scope, instead of queueing/waiting. Generic over
    lock_scope -- used for both circuit_key locks and Tor-instance locks
    (see module docstring for the two-tier model)."""
    cursor = connection.cursor()
    cursor.execute(
        "SELECT pg_try_advisory_lock(%s);",
        (_advisory_lock_key(lock_scope),),
    )
    acquired = cursor.fetchone()[0]
    cursor.close()
    return bool(acquired)


def release_advisory_lock(connection, lock_scope: str) -> None:
    cursor = connection.cursor()
    cursor.execute(
        "SELECT pg_advisory_unlock(%s);",
        (_advisory_lock_key(lock_scope),),
    )
    cursor.fetchone()
    cursor.close()


def ensure_tor_instances_table(connection) -> None:
    """Idempotent, same discipline as ensure_tor_circuits_table(): safe
    against a database that has never seen this table, one created
    before the Phase 2 bootstrap-status columns existed, or one already
    fully upgraded."""
    cursor = connection.cursor()
    cursor.execute(CREATE_TOR_INSTANCES_TABLE_SQL)
    cursor.execute(ALTER_TOR_INSTANCES_ADD_BOOTSTRAP_COLUMNS_SQL)
    connection.commit()
    cursor.close()


def _ensure_instance_row(connection, instance_key: str) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO tor_instances (instance_key)
        VALUES (%s)
        ON CONFLICT (instance_key) DO NOTHING;
        """,
        (instance_key,),
    )
    connection.commit()
    cursor.close()


def record_bootstrap_started(connection, instance_key: str) -> None:
    """Called by scripts/tor/verify_tor_connectivity.py's
    check_bootstrap_status() when an explicit bootstrap verification
    begins -- never automatically, never from collector traffic. Moves
    the persisted bootstrap_status to 'checking' and emits
    bootstrap_started. Does NOT claim readiness: readiness is only ever
    set by record_bootstrap_ready() below, after bootstrap is actually
    confirmed at 100%."""
    ensure_tor_instances_table(connection)
    ensure_tor_circuit_events_table(connection)
    _ensure_instance_row(connection, instance_key)

    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE tor_instances
        SET bootstrap_status = %s,
            last_bootstrap_checked_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE instance_key = %s;
        """,
        (BOOTSTRAP_STATUS_CHECKING, instance_key),
    )
    connection.commit()
    cursor.close()

    emit_event(connection, EVENT_BOOTSTRAP_STARTED, instance_key=instance_key)


def record_bootstrap_ready(connection, instance_key: str) -> None:
    """Called ONLY after bootstrap progress has actually been observed
    at 100% (see check_bootstrap_status()'s PROGRESS=100 check) --
    never inferred, never defaulted. Clears any prior
    last_bootstrap_error_category."""
    ensure_tor_instances_table(connection)
    ensure_tor_circuit_events_table(connection)
    _ensure_instance_row(connection, instance_key)

    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE tor_instances
        SET bootstrap_status = %s,
            last_bootstrap_checked_at = CURRENT_TIMESTAMP,
            last_bootstrap_ready_at = CURRENT_TIMESTAMP,
            last_bootstrap_error_category = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE instance_key = %s;
        """,
        (BOOTSTRAP_STATUS_READY, instance_key),
    )
    connection.commit()
    cursor.close()

    emit_event(connection, EVENT_BOOTSTRAP_READY, instance_key=instance_key)


def record_bootstrap_failed(connection, instance_key: str, error_category: str) -> None:
    """Called when bootstrap verification genuinely fails (ControlPort
    connection/authentication failure, or bootstrap confirmed NOT yet at
    100%). error_category must be one of _ALLOWED_ERROR_CATEGORIES --
    never raw exception text, never a secret. Validated FIRST, before
    any table is ensured, any row is touched, any UPDATE runs, or any
    event is emitted -- an invalid error_category raises ValueError with
    zero mutation and zero event insert, exactly as an invalid
    error_category passed via emit_event()'s detail would."""
    _validate_error_category(error_category)

    ensure_tor_instances_table(connection)
    ensure_tor_circuit_events_table(connection)
    _ensure_instance_row(connection, instance_key)

    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE tor_instances
        SET bootstrap_status = %s,
            last_bootstrap_checked_at = CURRENT_TIMESTAMP,
            last_bootstrap_error_category = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE instance_key = %s;
        """,
        (BOOTSTRAP_STATUS_FAILED, error_category, instance_key),
    )
    connection.commit()
    cursor.close()

    emit_event(
        connection, EVENT_BOOTSTRAP_FAILED, instance_key=instance_key,
        detail={"error_category": error_category},
    )


def _get_last_newnym_at(connection, instance_key: str):
    cursor = connection.cursor()
    cursor.execute(
        "SELECT last_newnym_at FROM tor_instances WHERE instance_key = %s;",
        (instance_key,),
    )
    row = cursor.fetchone()
    cursor.close()
    return row[0] if row else None


def _record_newnym_sent(connection, instance_key: str, when) -> None:
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE tor_instances
        SET last_newnym_at = %s, updated_at = CURRENT_TIMESTAMP
        WHERE instance_key = %s;
        """,
        (when, instance_key),
    )
    connection.commit()
    cursor.close()


def _persisted_cooldown_remaining(connection, instance_key: str, now=None) -> float:
    """Seconds remaining before another NEWNYM is due for this Tor
    instance, per OUR OWN persisted record -- not stem's in-object
    bookkeeping, which cannot see across separate connections/processes
    (see module docstring). 0.0 if no prior NEWNYM is on record."""
    now = now if now is not None else _utc_now()
    last_newnym_at = _get_last_newnym_at(connection, instance_key)

    if last_newnym_at is None:
        return 0.0

    min_interval = get_tor_newnym_min_interval_seconds()
    elapsed = (now - last_newnym_at).total_seconds()

    return max(0.0, min_interval - elapsed)


def _set_circuit_status(connection, circuit_key: str, status: str, **fields) -> None:
    """fields become `column = %s` assignments -- column NAMES are
    interpolated (values never are), so only names in
    _ALLOWED_STATUS_FIELDS are accepted. This is a fixed, developer-only
    call surface today (no external input reaches `fields`), but the
    allowlist keeps it that way structurally rather than by convention,
    since later phases are expected to add more callers here."""
    unknown_fields = set(fields) - _ALLOWED_STATUS_FIELDS

    if unknown_fields:
        raise ValueError(f"Unsupported tor_circuits field(s): {sorted(unknown_fields)}")

    assignments = ["status = %s", "updated_at = CURRENT_TIMESTAMP"]
    values = [status]

    for column, value in fields.items():
        assignments.append(f"{column} = %s")
        values.append(value)

    values.append(circuit_key)

    cursor = connection.cursor()
    cursor.execute(
        f"UPDATE tor_circuits SET {', '.join(assignments)} WHERE circuit_key = %s;",
        values,
    )
    connection.commit()
    cursor.close()


def _record_genuine_failure(connection, circuit_key: str, error_category: str, quarantine: bool) -> None:
    """Increments failure_count/consecutive_failure_count and records a
    normalized last_error_category for a GENUINE failure only -- never
    call this for CircuitLockError/TorInstanceBusyError/
    NewnymCooldownError, which must never move these counters (see
    module docstring). Only sets status = STATUS_QUARANTINED when
    quarantine=True: rotate_circuit()'s own control-port/NEWNYM or
    verify_fn failures quarantine; verify_circuit()'s standalone
    verification failures deliberately do not (see verify_circuit)."""
    cursor = connection.cursor()

    if quarantine:
        cursor.execute(
            """
            UPDATE tor_circuits
            SET status = %s,
                failure_count = failure_count + 1,
                consecutive_failure_count = consecutive_failure_count + 1,
                last_error_category = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE circuit_key = %s;
            """,
            (STATUS_QUARANTINED, error_category, circuit_key),
        )
    else:
        cursor.execute(
            """
            UPDATE tor_circuits
            SET failure_count = failure_count + 1,
                consecutive_failure_count = consecutive_failure_count + 1,
                last_error_category = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE circuit_key = %s;
            """,
            (error_category, circuit_key),
        )

    connection.commit()
    cursor.close()


def _record_verification_success(
    connection, circuit_key: str, observed_ip, when,
    status=None, last_rotated_at=None, request_count=None,
) -> None:
    """Records a successful, TRUSTED verification: last_exit_ip,
    last_verified_at, resets consecutive_failure_count to 0, clears
    last_error_category. Never infers last_verified_at from
    last_rotated_at -- a rotation may happen without verification, so
    these stay independent facts (see module docstring).

    `status` is only written when explicitly given: rotate_circuit()'s
    own verified-success path moves status to STATUS_READY (and also
    passes last_rotated_at/request_count, so the whole verified-rotation
    outcome lands in a SINGLE UPDATE rather than two separate writes);
    verify_circuit()'s standalone success must NOT change status,
    last_rotated_at, or request_count at all (verification alone is
    never a rotation or a status transition -- only rotate_circuit() and
    quarantine_circuit()/recover_circuit() change status)."""
    assignments = [
        "last_exit_ip = %s",
        "last_verified_at = %s",
        "consecutive_failure_count = 0",
        "last_error_category = NULL",
        "updated_at = CURRENT_TIMESTAMP",
    ]
    values = [observed_ip, when]

    if status is not None:
        assignments.insert(0, "status = %s")
        values.insert(0, status)

    if last_rotated_at is not None:
        assignments.append("last_rotated_at = %s")
        values.append(last_rotated_at)

    if request_count is not None:
        assignments.append("request_count = %s")
        values.append(request_count)

    values.append(circuit_key)

    cursor = connection.cursor()
    cursor.execute(
        f"UPDATE tor_circuits SET {', '.join(assignments)} WHERE circuit_key = %s;",
        values,
    )
    connection.commit()
    cursor.close()


def ensure_tor_circuit_events_table(connection) -> None:
    cursor = connection.cursor()
    cursor.execute(CREATE_TOR_CIRCUIT_EVENTS_TABLE_SQL)
    cursor.execute(CREATE_TOR_CIRCUIT_EVENTS_INDEXES_SQL)
    connection.commit()
    cursor.close()


def _prune_tor_circuit_events_in_transaction(cursor) -> None:
    """Keeps tor_circuit_events bounded to get_tor_event_max_rows() rows,
    oldest rows first. Takes an existing CURSOR (not a connection) and
    deliberately does NOT commit -- see emit_event(), which runs this in
    the SAME transaction as the preceding INSERT and commits both (or
    neither) together. Not a background cleanup daemon: retention is
    enforced synchronously and deterministically on every write, never
    drifting unbounded between runs. Touches ONLY tor_circuit_events;
    never backups, never any other table."""
    max_rows = get_tor_event_max_rows()

    cursor.execute(
        """
        DELETE FROM tor_circuit_events
        WHERE id IN (
            SELECT id FROM tor_circuit_events
            ORDER BY id DESC
            OFFSET %s
        );
        """,
        (max_rows,),
    )


def emit_event(connection, event_type: str, circuit_key: str = None, instance_key: str = None, detail: dict = None) -> None:
    """Appends one bounded, secret-safe lifecycle event and prunes old
    rows so the table never grows unbounded -- the INSERT and the prune
    DELETE run in ONE transaction with a SINGLE commit
    (_prune_tor_circuit_events_in_transaction). If the prune fails for
    any reason, the whole transaction rolls back: the event insert is
    NEVER left committed on its own, and "successful emit_event" always
    means "row count is at or under the configured cap," never "the
    event was inserted but retention silently failed to run."

    `detail` field NAMES are restricted to a small, fixed, non-secret
    allowlist (_ALLOWED_EVENT_DETAIL_FIELDS), and every VALUE is also
    validated (_validate_event_detail) -- a name-only allowlist would
    still let a caller smuggle a secret, URL, or free-form text through
    an approved field like reason_code. ControlPort passwords,
    cookies/session state, raw exception text, browser headers, and the
    observed exit IP (already persisted on tor_circuits.last_exit_ip)
    must never reach this table. json.dumps() is called WITHOUT
    default=str: an unknown/non-JSON-serializable value must raise, not
    be silently coerced to a string that could carry unvalidated
    content."""
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise ValueError(f"Unsupported tor_circuit_events event_type: {event_type!r}")

    detail = detail or {}
    _validate_event_detail(detail)

    serialized = json.dumps(detail)
    serialized_bytes = len(serialized.encode("utf-8"))

    if serialized_bytes > _MAX_EVENT_DETAIL_BYTES:
        raise ValueError(
            f"Event detail exceeds {_MAX_EVENT_DETAIL_BYTES} byte cap "
            f"({serialized_bytes} bytes) for event_type={event_type!r}"
        )

    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO tor_circuit_events (event_type, circuit_key, instance_key, detail)
            VALUES (%s, %s, %s, %s::jsonb);
            """,
            (event_type, circuit_key, instance_key, serialized),
        )
        _prune_tor_circuit_events_in_transaction(cursor)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def request_new_identity(connection, max_retries: int = 3, max_wait_seconds=None) -> None:
    """Sends NEWNYM to the Tor control port.

    Caller must already hold the circuit_key advisory lock (tier 1).
    This function acquires and releases the Tor-INSTANCE advisory lock
    (tier 2) itself, on the same connection/session passed in, so that
    two different circuit_key values sharing this Tor instance can never
    send NEWNYM at the same time. Raises TorInstanceBusyError -- not
    CircuitRotationError -- if that lock is already held; this is
    contention, not a circuit fault, and must not be busy-retried.

    Before sending NEWNYM, checks the PERSISTED cross-invocation cooldown
    clock (tier 3, tor_instances.last_newnym_at -- see module docstring
    for why stem's own live check cannot do this alone). If the
    remaining cooldown fits within max_wait_seconds (default from
    TOR_NEWNYM_MAX_WAIT_SECONDS), sleeps once for exactly that long --
    a single bounded sleep, never a retry loop or busy-wait. If it does
    not fit, raises NewnymCooldownError immediately without waiting or
    sending NEWNYM: a clear transient result, not a fault.

    Also consults the live Controller.is_newnym_available()/
    get_newnym_wait() after connecting, as a defensive secondary check
    -- see module docstring for why this is currently a structural
    no-op (a fresh Controller always reports available) and why it is
    kept anyway.

    Bounded retries only for the NEWNYM exchange itself; never loops
    forever on control-port failure. NewnymCooldownError raised from
    the live check is never treated as retryable.

    Emits (Phase 2) lock_contended when the instance lock is contended,
    and cooldown_blocked when either cooldown check exceeds
    max_wait_seconds -- both BEFORE raising, both transient/non-fault
    events that never touch tor_circuits' failure counters.
    """
    if max_wait_seconds is None:
        max_wait_seconds = get_tor_newnym_max_wait_seconds()

    control_host = get_tor_control_host()
    control_port = get_tor_control_port()
    instance_lock_scope = _instance_lock_key(control_host, control_port)

    if not try_acquire_advisory_lock(connection, instance_lock_scope):
        ensure_tor_circuit_events_table(connection)
        emit_event(
            connection,
            EVENT_LOCK_CONTENDED,
            instance_key=instance_lock_scope,
            detail={"reason_code": "instance_lock_held"},
        )
        raise TorInstanceBusyError(
            f"Tor instance {control_host}:{control_port} is already handling "
            "a NEWNYM request from another circuit."
        )

    try:
        ensure_tor_instances_table(connection)
        ensure_tor_circuit_events_table(connection)
        _ensure_instance_row(connection, instance_lock_scope)

        persisted_wait = _persisted_cooldown_remaining(connection, instance_lock_scope)

        if persisted_wait > 0:
            if persisted_wait > max_wait_seconds:
                emit_event(
                    connection,
                    EVENT_COOLDOWN_BLOCKED,
                    instance_key=instance_lock_scope,
                    detail={
                        "reason_code": "persisted_cooldown",
                        "duration_seconds": persisted_wait,
                    },
                )
                raise NewnymCooldownError(
                    f"NEWNYM cooldown active for Tor instance "
                    f"{control_host}:{control_port}: {persisted_wait:.1f}s "
                    f"remaining, exceeds max_wait_seconds={max_wait_seconds}"
                )

            time.sleep(persisted_wait)

        control_password = get_tor_control_password()
        last_error = None

        for attempt in range(max_retries):
            try:
                with Controller.from_port(
                    address=control_host,
                    port=control_port,
                ) as controller:
                    if control_password:
                        controller.authenticate(password=control_password)
                    else:
                        controller.authenticate()

                    # Defensive, currently-inert secondary check -- see
                    # module docstring. Kept because a fresh connection
                    # always reports available today, but this stays
                    # correct if that assumption ever changes.
                    if not controller.is_newnym_available():
                        stem_wait = controller.get_newnym_wait()

                        if stem_wait > max_wait_seconds:
                            emit_event(
                                connection,
                                EVENT_COOLDOWN_BLOCKED,
                                instance_key=instance_lock_scope,
                                detail={
                                    "reason_code": "live_stem_cooldown",
                                    "duration_seconds": stem_wait,
                                },
                            )
                            raise NewnymCooldownError(
                                f"Tor reports {stem_wait:.1f}s until NEWNYM is "
                                f"available on this connection, exceeds "
                                f"max_wait_seconds={max_wait_seconds}"
                            )

                        if stem_wait > 0:
                            time.sleep(stem_wait)

                    controller.signal(Signal.NEWNYM)
                    _record_newnym_sent(connection, instance_lock_scope, _utc_now())
                    return

            except NewnymCooldownError:
                # Timing, not a retryable Tor failure -- propagate as-is.
                raise

            except Exception as error:
                last_error = error

        raise CircuitRotationError(
            f"NEWNYM failed after {max_retries} attempt(s): "
            f"{_redact_secret(str(last_error), control_password)}"
        )

    finally:
        try:
            release_advisory_lock(connection, instance_lock_scope)
        except Exception as unlock_error:
            print(
                f"WARNING: failed to release Tor instance lock "
                f"'{instance_lock_scope}': {unlock_error}"
            )


def rotate_circuit(circuit_key: str = DEFAULT_CIRCUIT_KEY, verify_fn=None) -> dict:
    """Full manually-controlled circuit lifecycle for Phase 1/2.

    verify_fn, if given, is called with no arguments after NEWNYM and
    must return the observed exit IP as a string (or raise on failure).
    It is injected by the caller (e.g. a Playwright-based IP check in
    scripts/tor/verify_tor_connectivity.py) so this module has no
    browser/network dependency of its own.

    Raises CircuitLockError if another process already owns this
    circuit_key, TorInstanceBusyError if another circuit is currently
    mid-NEWNYM on the same Tor instance, or NewnymCooldownError if the
    instance's NEWNYM cooldown hasn't elapsed within the configured max
    wait -- callers must not busy-retry on any of these. None of the
    three quarantines the circuit, and none touches the failure
    counters: contention and cooldown are not circuit faults. Genuine
    NEWNYM/verification failures do quarantine and do increment the
    failure counters (see module docstring for the exact rules).

    Only a rotation that ALSO passes verification updates
    last_verified_at / resets consecutive_failure_count -- an unverified
    rotation (verify_fn=None) proves nothing about the exit IP actually
    working, so it deliberately leaves last_verified_at and the failure
    counters untouched.
    """
    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)
        ensure_tor_circuit_events_table(connection)
        _ensure_circuit_row(connection, circuit_key)

        emit_event(connection, EVENT_ROTATION_REQUESTED, circuit_key=circuit_key)

        if not try_acquire_advisory_lock(connection, circuit_key):
            emit_event(
                connection,
                EVENT_LOCK_CONTENDED,
                circuit_key=circuit_key,
                detail={"reason_code": "circuit_lock_held"},
            )
            raise CircuitLockError(
                f"circuit '{circuit_key}' is already being managed by another process"
            )

        try:
            _set_circuit_status(connection, circuit_key, STATUS_DRAINING)
            emit_event(connection, EVENT_ROTATION_STARTED, circuit_key=circuit_key)

            started_at = _utc_now()
            instance_key = _instance_lock_key(get_tor_control_host(), get_tor_control_port())

            try:
                request_new_identity(connection)
            except (TorInstanceBusyError, NewnymCooldownError):
                # Shared-instance contention or an active NEWNYM cooldown
                # is not a fault of THIS circuit -- revert to ready (not
                # quarantined), and never touch the failure counters, so
                # transient timing/contention never requires manual
                # recovery and never looks like a real failure.
                _set_circuit_status(connection, circuit_key, STATUS_READY)
                raise
            except Exception:
                rotation_duration = (_utc_now() - started_at).total_seconds()
                emit_event(
                    connection,
                    EVENT_ROTATION_FAILED,
                    circuit_key=circuit_key,
                    instance_key=instance_key,
                    detail={
                        "error_category": ERROR_CATEGORY_CONTROL_PORT_FAILURE,
                        "duration_seconds": rotation_duration,
                    },
                )
                _record_genuine_failure(
                    connection, circuit_key, ERROR_CATEGORY_CONTROL_PORT_FAILURE, quarantine=True,
                )
                raise

            rotation_duration = (_utc_now() - started_at).total_seconds()
            emit_event(
                connection,
                EVENT_ROTATION_SUCCESS,
                circuit_key=circuit_key,
                instance_key=instance_key,
                detail={"duration_seconds": rotation_duration},
            )

            if verify_fn is not None:
                emit_event(connection, EVENT_VERIFICATION_STARTED, circuit_key=circuit_key)
                verify_started_at = _utc_now()

                try:
                    observed_ip = verify_fn()
                except Exception:
                    verify_duration = (_utc_now() - verify_started_at).total_seconds()
                    emit_event(
                        connection,
                        EVENT_VERIFICATION_FAILED,
                        circuit_key=circuit_key,
                        detail={
                            "error_category": ERROR_CATEGORY_VERIFICATION_FAILED,
                            "duration_seconds": verify_duration,
                        },
                    )
                    _record_genuine_failure(
                        connection, circuit_key, ERROR_CATEGORY_VERIFICATION_FAILED, quarantine=True,
                    )
                    raise

                verify_duration = (_utc_now() - verify_started_at).total_seconds()
                verified_at = _utc_now()
                emit_event(
                    connection,
                    EVENT_VERIFICATION_SUCCESS,
                    circuit_key=circuit_key,
                    detail={"duration_seconds": verify_duration},
                )
                # Single UPDATE: verified rotation success moves status to
                # READY, resets request_count, records last_rotated_at,
                # AND records the verification facts -- all one write, so
                # exactly one READY transition is ever observed for this
                # outcome (not two separate updates racing/duplicating).
                _record_verification_success(
                    connection, circuit_key, observed_ip, verified_at,
                    status=STATUS_READY, last_rotated_at=verified_at, request_count=0,
                )

                duration_seconds = (_utc_now() - started_at).total_seconds()

                return {
                    "circuit_key": circuit_key,
                    "status": STATUS_READY,
                    "exit_ip": observed_ip,
                    "duration_seconds": duration_seconds,
                }

            observed_ip = None
            duration_seconds = (_utc_now() - started_at).total_seconds()

            _set_circuit_status(
                connection,
                circuit_key,
                STATUS_READY,
                request_count=0,
                last_exit_ip=observed_ip,
                last_rotated_at=_utc_now(),
            )

            return {
                "circuit_key": circuit_key,
                "status": STATUS_READY,
                "exit_ip": observed_ip,
                "duration_seconds": duration_seconds,
            }

        finally:
            # Never let an unlock failure mask the exception (if any)
            # already propagating out of the try/except above -- log and
            # continue instead of re-raising here.
            try:
                release_advisory_lock(connection, circuit_key)
            except Exception as unlock_error:
                print(
                    f"WARNING: failed to release advisory lock for circuit "
                    f"'{circuit_key}': {unlock_error}"
                )

    finally:
        connection.close()


def verify_circuit(circuit_key: str = DEFAULT_CIRCUIT_KEY, verify_fn=None) -> dict:
    """Manual, lock-guarded verification ONLY (Phase 2).

    - Acquires the existing circuit_key advisory lock (same tier
      rotate_circuit() uses), so a verify and a rotate can never race on
      the same circuit's bookkeeping.
    - NEVER sends NEWNYM and NEVER marks the circuit draining -- this is
      strictly a read-and-confirm operation against whatever exit IP the
      circuit currently has.
    - verify_fn is REQUIRED (unlike rotate_circuit's optional verify_fn):
      calling this function without one to check is a caller error, not
      a valid "unverified verify."
    - On success: updates last_exit_ip, last_verified_at, resets
      consecutive_failure_count to 0, clears last_error_category.
      Deliberately does NOT change `status` -- verification succeeding
      is not, by itself, a status transition (only rotate_circuit() and
      quarantine_circuit()/recover_circuit() change status).
    - On failure: increments failure_count/consecutive_failure_count,
      records a normalized last_error_category, emits
      verification_failed -- and deliberately does NOT quarantine. Only
      rotate_circuit()'s own verify_fn failure (which happens as part of
      an active rotation) quarantines; a standalone verification check
      failing must not, by itself, force an operator into
      quarantine_circuit()/recover_circuit() just to keep using an
      already-working circuit that merely failed one verification pass.
    - Releases the lock reliably on every path (success, failure, or an
      unexpected error), matching rotate_circuit()'s own guarantee.
    """
    if verify_fn is None:
        raise ValueError("verify_circuit() requires verify_fn")

    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)
        ensure_tor_circuit_events_table(connection)
        _ensure_circuit_row(connection, circuit_key)

        if not try_acquire_advisory_lock(connection, circuit_key):
            emit_event(
                connection,
                EVENT_LOCK_CONTENDED,
                circuit_key=circuit_key,
                detail={"reason_code": "circuit_lock_held"},
            )
            raise CircuitLockError(
                f"circuit '{circuit_key}' is already being managed by another process"
            )

        try:
            emit_event(connection, EVENT_VERIFICATION_STARTED, circuit_key=circuit_key)
            started_at = _utc_now()

            try:
                observed_ip = verify_fn()
            except Exception:
                duration_seconds = (_utc_now() - started_at).total_seconds()
                emit_event(
                    connection,
                    EVENT_VERIFICATION_FAILED,
                    circuit_key=circuit_key,
                    detail={
                        "error_category": ERROR_CATEGORY_VERIFICATION_FAILED,
                        "duration_seconds": duration_seconds,
                    },
                )
                _record_genuine_failure(
                    connection, circuit_key, ERROR_CATEGORY_VERIFICATION_FAILED, quarantine=False,
                )
                raise

            duration_seconds = (_utc_now() - started_at).total_seconds()
            verified_at = _utc_now()

            emit_event(
                connection,
                EVENT_VERIFICATION_SUCCESS,
                circuit_key=circuit_key,
                detail={"duration_seconds": duration_seconds},
            )
            _record_verification_success(connection, circuit_key, observed_ip, verified_at, status=None)

            return {
                "circuit_key": circuit_key,
                "exit_ip": observed_ip,
                "last_verified_at": verified_at,
                "duration_seconds": duration_seconds,
            }

        finally:
            try:
                release_advisory_lock(connection, circuit_key)
            except Exception as unlock_error:
                print(
                    f"WARNING: failed to release advisory lock for circuit "
                    f"'{circuit_key}' during verify: {unlock_error}"
                )

    finally:
        connection.close()


def quarantine_circuit(circuit_key: str = DEFAULT_CIRCUIT_KEY, reason_code: str = None) -> dict:
    """Explicit OPERATOR/TEST action (Phase 2): moves a circuit straight
    to STATUS_QUARANTINED.

    NEVER triggered automatically by an HTTP status code, a real
    target's response, or any other Phase 2 component -- there is no
    call site anywhere in this codebase that invokes this function based
    on a 429/403/timeout from a real request, and none should ever be
    added without updating this module's scope note first.

    Manual quarantine is an operator action, not a failure: it does NOT
    increment failure_count or consecutive_failure_count (see module
    docstring) -- only a genuine rotation/verification failure does
    that.

    If reason_code is given, it is validated FIRST -- before even
    opening a database connection, let alone acquiring the circuit
    lock, mutating status, or emitting an event. An invalid reason_code
    raises ValueError with zero UPDATE, zero event insert, and no
    status mutation whatsoever.
    """
    if reason_code is not None:
        _validate_reason_code(reason_code)

    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)
        ensure_tor_circuit_events_table(connection)
        _ensure_circuit_row(connection, circuit_key)

        if not try_acquire_advisory_lock(connection, circuit_key):
            raise CircuitLockError(
                f"circuit '{circuit_key}' is already being managed by another process"
            )

        try:
            _set_circuit_status(connection, circuit_key, STATUS_QUARANTINED)
            detail = {"reason_code": reason_code} if reason_code else {}
            emit_event(connection, EVENT_QUARANTINED, circuit_key=circuit_key, detail=detail)

            return {"circuit_key": circuit_key, "status": STATUS_QUARANTINED}

        finally:
            try:
                release_advisory_lock(connection, circuit_key)
            except Exception as unlock_error:
                print(
                    f"WARNING: failed to release advisory lock for circuit "
                    f"'{circuit_key}' during quarantine: {unlock_error}"
                )

    finally:
        connection.close()


def recover_circuit(circuit_key: str = DEFAULT_CIRCUIT_KEY) -> dict:
    """Explicit OPERATOR/TEST action (Phase 2): manually clears a
    quarantined circuit -- or one left stale in STATUS_DRAINING by a
    crash (see scripts/tor/observability.py's stale-draining detection)
    -- back to STATUS_READY.

    NEVER issues NEWNYM, NEVER contacts any target, and is NEVER
    triggered automatically by an HTTP status code, a timer, or any
    other Phase 2 component. Recovery is deliberately NOT automatic:
    an operator (or a test) must call this explicitly, precisely so a
    crash-stale circuit can never silently un-stick itself in a way that
    masks what actually happened.

    Deliberately does NOT reset failure_count/consecutive_failure_count:
    recovery is not proof the underlying issue is fixed, only that an
    operator has decided the circuit may be used again. Only a
    subsequent VERIFIED success (verify_circuit() or a verified
    rotate_circuit()) resets the failure history.
    """
    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)
        ensure_tor_circuit_events_table(connection)
        _ensure_circuit_row(connection, circuit_key)

        if not try_acquire_advisory_lock(connection, circuit_key):
            raise CircuitLockError(
                f"circuit '{circuit_key}' is already being managed by another process"
            )

        try:
            _set_circuit_status(connection, circuit_key, STATUS_READY)
            emit_event(connection, EVENT_RECOVERED, circuit_key=circuit_key)

            return {"circuit_key": circuit_key, "status": STATUS_READY}

        finally:
            try:
                release_advisory_lock(connection, circuit_key)
            except Exception as unlock_error:
                print(
                    f"WARNING: failed to release advisory lock for circuit "
                    f"'{circuit_key}' during recover: {unlock_error}"
                )

    finally:
        connection.close()


def get_circuit_state(circuit_key: str = DEFAULT_CIRCUIT_KEY):
    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)

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

        return {
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

    finally:
        connection.close()


def is_draining_stale(updated_at, now=None) -> bool:
    """True if a circuit's `updated_at` is older than
    get_tor_stale_draining_threshold_seconds() while its status is still
    STATUS_DRAINING -- i.e. a rotation that started but never finished
    (most likely a crash mid-NEWNYM). Pure function: callers (see
    scripts/tor/observability.py) are responsible for confirming
    status == STATUS_DRAINING before treating this as meaningful; this
    function only answers the "how old is updated_at" half of that
    question, and does not itself take any recovery action -- staleness
    is surfaced for an operator to act on via recover_circuit(), never
    auto-corrected here (see module docstring)."""
    if updated_at is None:
        return False

    now = now if now is not None else _utc_now()
    threshold = get_tor_stale_draining_threshold_seconds()

    return (now - updated_at).total_seconds() > threshold


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Manually-controlled Tor circuit lifecycle (Phase 1/2)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    rotate_parser = subparsers.add_parser(
        "rotate", help="Acquire the circuit lock and request a new identity."
    )
    rotate_parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)

    status_parser = subparsers.add_parser(
        "status", help="Print the persisted state of a circuit."
    )
    status_parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Print the persisted state of a circuit (alias of status)."
    )
    inspect_parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)

    quarantine_parser = subparsers.add_parser(
        "quarantine", help="Manually quarantine a circuit (operator/test action)."
    )
    quarantine_parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)
    quarantine_parser.add_argument("--reason-code", default=None)

    recover_parser = subparsers.add_parser(
        "recover", help="Manually recover a quarantined/stale-draining circuit to ready."
    )
    recover_parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)

    verify_parser = subparsers.add_parser(
        "verify",
        help=(
            "Manually verify a circuit's current exit IP WITHOUT sending "
            "NEWNYM (Phase 3 dark-launch preferred validation -- see "
            "verify_circuit())."
        ),
    )
    verify_parser.add_argument("--circuit-id", default=DEFAULT_CIRCUIT_KEY)

    return parser.parse_args()


def main():
    args = _parse_args()

    if args.command == "rotate":
        result = rotate_circuit(circuit_key=args.circuit_id)
        print(f"Circuit '{args.circuit_id}' rotated: {result}")

    elif args.command in ("status", "inspect"):
        state = get_circuit_state(circuit_key=args.circuit_id)
        print(f"Circuit '{args.circuit_id}': {state}")

    elif args.command == "quarantine":
        result = quarantine_circuit(circuit_key=args.circuit_id, reason_code=args.reason_code)
        print(f"Circuit '{args.circuit_id}' quarantined: {result}")

    elif args.command == "recover":
        result = recover_circuit(circuit_key=args.circuit_id)
        print(f"Circuit '{args.circuit_id}' recovered: {result}")

    elif args.command == "verify":
        # Deferred import: scripts.tor.verify_tor_connectivity already
        # imports FROM this module, so importing it at module top level
        # here would be circular. check_exit_ip() is the same neutral,
        # non-LinkedIn connectivity check that verify_tor_connectivity's
        # own `--rotate` path passes to rotate_circuit() as verify_fn --
        # reused here as verify_circuit()'s verify_fn instead, which
        # NEVER sends NEWNYM (see verify_circuit()'s docstring). This is
        # the preferred Phase 3 dark-launch validation path: confirm the
        # circuit works without rotating it.
        from scripts.tor.verify_tor_connectivity import check_exit_ip

        result = verify_circuit(circuit_key=args.circuit_id, verify_fn=check_exit_ip)
        print(f"Circuit '{args.circuit_id}' verified (no rotation): {result}")


if __name__ == "__main__":
    main()
