"""Central Tor circuit lifecycle control (Phase 1: manually controlled).

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

Phase 1 scope only: a single manually-triggered lifecycle --
    acquire circuit lock -> mark draining -> NEWNYM (acquires/releases
    the instance lock internally, respects the persisted cooldown) ->
    optional verify -> mark ready (or quarantined on failure) ->
    release circuit lock
driven by a CLI entry point (`python -m scripts.tor.circuit_manager`).
Automatic rotation on failure signals, response classification, and
per-target/multi-instance circuit pools are later phases and are NOT
implemented here -- do not add them without updating this module's
scope note.

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
"""
import argparse
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
    get_tor_newnym_max_wait_seconds,
    get_tor_newnym_min_interval_seconds,
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

CREATE_TOR_INSTANCES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tor_instances (
    id SERIAL PRIMARY KEY,
    instance_key VARCHAR(200) NOT NULL UNIQUE,
    last_newnym_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
"""


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
    principle collide onto the same key. Accepted for Phase 1's tiny,
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
    cursor = connection.cursor()
    cursor.execute(CREATE_TOR_CIRCUITS_TABLE_SQL)
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
    cursor = connection.cursor()
    cursor.execute(CREATE_TOR_INSTANCES_TABLE_SQL)
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
    """
    if max_wait_seconds is None:
        max_wait_seconds = get_tor_newnym_max_wait_seconds()

    control_host = get_tor_control_host()
    control_port = get_tor_control_port()
    instance_lock_scope = _instance_lock_key(control_host, control_port)

    if not try_acquire_advisory_lock(connection, instance_lock_scope):
        raise TorInstanceBusyError(
            f"Tor instance {control_host}:{control_port} is already handling "
            "a NEWNYM request from another circuit."
        )

    try:
        ensure_tor_instances_table(connection)
        _ensure_instance_row(connection, instance_lock_scope)

        persisted_wait = _persisted_cooldown_remaining(connection, instance_lock_scope)

        if persisted_wait > 0:
            if persisted_wait > max_wait_seconds:
                raise NewnymCooldownError(
                    f"NEWNYM cooldown active for Tor instance "
                    f"{control_host}:{control_port}: {persisted_wait:.1f}s "
                    f"remaining, exceeds max_wait_seconds={max_wait_seconds}"
                )

            time.sleep(persisted_wait)

        control_password = get_tor_control_password()
        last_error = None

        for _ in range(max_retries):
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
    """Full manually-controlled circuit lifecycle for Phase 1.

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
    three quarantines the circuit: contention and cooldown are not
    circuit faults. Genuine NEWNYM/verification failures do quarantine.
    """
    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)
        _ensure_circuit_row(connection, circuit_key)

        if not try_acquire_advisory_lock(connection, circuit_key):
            raise CircuitLockError(
                f"circuit '{circuit_key}' is already being managed by another process"
            )

        try:
            _set_circuit_status(connection, circuit_key, STATUS_DRAINING)

            started_at = _utc_now()
            request_new_identity(connection)

            observed_ip = verify_fn() if verify_fn is not None else None
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

        except (TorInstanceBusyError, NewnymCooldownError):
            # Shared-instance contention or an active NEWNYM cooldown is
            # not a fault of THIS circuit -- revert to ready (not
            # quarantined) so transient timing/contention never requires
            # manual recovery.
            _set_circuit_status(connection, circuit_key, STATUS_READY)
            raise

        except Exception:
            _set_circuit_status(connection, circuit_key, STATUS_QUARANTINED)
            raise

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


def get_circuit_state(circuit_key: str = DEFAULT_CIRCUIT_KEY):
    connection = psycopg2.connect(**get_postgres_config())

    try:
        ensure_tor_circuits_table(connection)

        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT circuit_key, status, request_count, last_exit_ip,
                   last_rotated_at, cooldown_until
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
        }

    finally:
        connection.close()


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Manually-controlled Tor circuit lifecycle (Phase 1)."
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

    return parser.parse_args()


def main():
    args = _parse_args()

    if args.command == "rotate":
        result = rotate_circuit(circuit_key=args.circuit_id)
        print(f"Circuit '{args.circuit_id}' rotated: {result}")

    elif args.command == "status":
        state = get_circuit_state(circuit_key=args.circuit_id)
        print(f"Circuit '{args.circuit_id}': {state}")


if __name__ == "__main__":
    main()
