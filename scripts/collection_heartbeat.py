"""
Testable, atomic, run-identified heartbeat writer for the collection
cycle, replacing the embedded Python heredoc previously inlined inside
scripts/run_collection_cycle_safe.sh.

Invoked as a short-lived CLI, one call per state transition:

    python3 -m scripts.collection_heartbeat start   --run-id <uuid> --stage cycle_started --message "..."
    python3 -m scripts.collection_heartbeat progress --run-id <uuid> --stage ...           --message "..." [--metrics-json '{...}']
    python3 -m scripts.collection_heartbeat finish   --run-id <uuid> --stage cycle_finished --message "..." [--status success] [--useful-ingestion] [--final-outcome ...] [--metrics-json '{...}']
    python3 -m scripts.collection_heartbeat fail     --run-id <uuid> --stage ...           --message "..." [--status failed|aborted_auth|skipped_running] [--error-category ...] [--metrics-json '{...}']

Design contract (see docs/PRODUCTION_RUNBOOK.md for the operator summary):

  - One inter-process, non-blocking `flock` per invocation -- held only
    for the duration of that single read-modify-write, tied to a file
    descriptor so it always releases on process exit (normal, error, or
    signal), never left permanently held by a crash.
  - Atomic write: a temp file in the same directory as the state file,
    fsync'd, then promoted with os.replace -- a reader can never observe
    a partially-written state file.
  - Missing, empty, or malformed state always recovers to a safe empty
    object rather than raising -- a corrupt state file must never crash
    a heartbeat write.
  - `start` unconditionally claims the heartbeat for its run_id. This is
    intentional and safe ONLY because the caller (run_collection_cycle_safe.sh)
    already holds its own top-level cycle lock before ever calling `start`
    -- lock acquisition is the authoritative liveness signal, so a fresh
    `start` is always allowed to overwrite whatever a previous (by
    definition no-longer-lock-holding) run left behind, including a
    stale "running" marker from a killed process.
  - `progress` / `finish` / `fail` are only accepted if the state file's
    current run_id still matches the caller's run_id. If a *different*
    run_id currently owns the file, the write is REFUSED (raises
    StaleRunError, CLI exits non-zero) instead of overwriting -- this is
    what stops an older/slower run from clobbering a newer run's terminal
    or progress state. Under normal operation this should never trigger,
    because the top-level cycle lock prevents two runs from being active
    at once; this is defense in depth, not the primary guard.
  - `progress_seq` increases by exactly 1 on every accepted write for the
    owning run_id (reset to 0 by `start`).
  - `last_success_at` and `last_useful_ingestion_at` are carry-forward
    fields: preserved across writes unless the current write explicitly
    sets them (a run with no new useful ingestion must not erase the
    record of the last run that had some).
  - Two DIFFERENT PIDs are recorded, neither used as a liveness signal:
    `owner_pid` is the STABLE PID of the run_collection_cycle_safe.sh
    process for this entire run, supplied explicitly by the caller via
    `--owner-pid "$$"` on every single call (start/progress/finish/fail)
    -- it does not change across a run's calls, because each call is a
    short-lived `python3 -m scripts.collection_heartbeat` helper process
    invoked BY that stable shell process, not the shell process itself.
    `writer_pid` is `os.getpid()` of that short-lived helper -- it is
    expected to be DIFFERENT on every single call, and exists purely as
    a diagnostic breadcrumb (e.g. to spot an unexpectedly long-running
    helper invocation in a process list at the moment of a hang). Even
    `owner_pid` is NOT used as a cross-container liveness check: the
    collection cycle's collector subprocesses run inside the `api`
    Docker container while this module and the wrapper run on the host,
    so a PID recorded here lives in a different PID namespace than any
    in-container process. Canonical liveness/progress signals in this
    phase are run_id, updated_at, last_progress_at, progress_seq, and
    stage -- not either PID.
"""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 1

DEFAULT_STATE_PATH = "/opt/jobpulse/logs/collection_heartbeat.json"
DEFAULT_LOCK_PATH = "/opt/jobpulse/logs/collection_heartbeat.lock"

STATE_PATH_ENV_VAR = "JOBPULSE_COLLECTION_HEARTBEAT_STATE_PATH"
LOCK_PATH_ENV_VAR = "JOBPULSE_COLLECTION_HEARTBEAT_LOCK_PATH"

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_ABORTED_AUTH = "aborted_auth"
STATUS_SKIPPED_RUNNING = "skipped_running"

TERMINAL_STATUSES = frozenset({STATUS_SUCCESS, STATUS_FAILED, STATUS_ABORTED_AUTH, STATUS_SKIPPED_RUNNING})
VALID_STATUSES = TERMINAL_STATUSES | {STATUS_RUNNING}

# Carry-forward fields: preserved from the prior state unless this write
# explicitly supplies a new value. Everything else is fully replaced by
# the current run's write.
_CARRY_FORWARD_FIELDS = ("last_success_at", "last_useful_ingestion_at")


class StaleRunError(Exception):
    """Raised when a write is refused because a different run_id
    currently owns the heartbeat state."""


class LockBusyError(Exception):
    """Raised when the heartbeat lock is already held by another writer."""


def default_state_path() -> Path:
    return Path(os.getenv(STATE_PATH_ENV_VAR, DEFAULT_STATE_PATH))


def default_lock_path() -> Path:
    return Path(os.getenv(LOCK_PATH_ENV_VAR, DEFAULT_LOCK_PATH))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextlib.contextmanager
def _state_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusyError(f"Lock held: {lock_path}") from exc
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


def _load_state(state_path: Path) -> dict:
    """Missing, empty, or malformed state always recovers to a safe empty
    dict -- never raises."""
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state_atomic(state_path: Path, state: dict) -> None:
    directory = state_path.parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".collection_heartbeat.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _apply_update(
    prior: dict,
    *,
    run_id: str,
    owner_pid: int,
    status: str,
    stage: str,
    message: str,
    is_start: bool,
    metrics: Optional[dict],
    final_outcome: Optional[str],
    error_category: Optional[str],
    useful_ingestion: bool,
) -> dict:
    now = _now_iso()
    prior_run_id = prior.get("run_id")

    if not is_start and prior_run_id is not None and prior_run_id != run_id:
        raise StaleRunError(
            f"heartbeat is owned by run_id={prior_run_id!r}, refusing write from run_id={run_id!r}"
        )

    if is_start:
        progress_seq = 0
        started_at = now
        # A fresh run's metrics start clean -- never inherited from
        # whatever the *previous* run last reported, even though other
        # carry-forward fields (last_success_at, last_useful_ingestion_at)
        # deliberately persist across runs.
        current_metrics = metrics
    else:
        progress_seq = int(prior.get("progress_seq") or 0) + 1
        started_at = prior.get("started_at") or now
        current_metrics = metrics if metrics is not None else prior.get("current_metrics")

    new_state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "owner_pid": owner_pid,
        "writer_pid": os.getpid(),
        "status": status,
        # Legacy alias for app/admin_status.py's build_alerts(), which is
        # intentionally NOT modified in this phase and reads "last_status" /
        # "last_message". Kept identical to `status`/`message` so the
        # existing, already-hardened (commit 83d7b00) alert path keeps
        # working unchanged against this richer schema. Phase 2B should
        # retire this duplication once admin_status.py reads the new
        # field names natively.
        "last_status": status,
        "stage": stage,
        "message": message,
        "last_message": message,
        "started_at": started_at,
        "updated_at": now,
        "last_progress_at": now,
        "finished_at": prior.get("finished_at"),
        "progress_seq": progress_seq,
        "last_success_at": prior.get("last_success_at"),
        "last_useful_ingestion_at": prior.get("last_useful_ingestion_at"),
        "current_metrics": current_metrics,
        "final_outcome": final_outcome,
        "error_category": error_category,
    }

    if status in TERMINAL_STATUSES:
        new_state["finished_at"] = now

    if status == STATUS_SUCCESS:
        new_state["last_success_at"] = now

    if useful_ingestion:
        new_state["last_useful_ingestion_at"] = now

    return new_state


def update_heartbeat(
    *,
    run_id: str,
    owner_pid: int,
    status: str,
    stage: str,
    message: str,
    is_start: bool = False,
    metrics: Optional[dict] = None,
    final_outcome: Optional[str] = None,
    error_category: Optional[str] = None,
    useful_ingestion: bool = False,
    state_path: Optional[Path] = None,
    lock_path: Optional[Path] = None,
    lock_retry_attempts: int = 1,
    lock_retry_delay_seconds: float = 0.0,
) -> dict:
    """`lock_retry_attempts` bounds how many times a LockBusyError (the
    heartbeat lock being held by another concurrent writer) is retried
    before giving up and raising -- default 1 means "try once, no
    retries," preserving the library's original fail-fast behavior for
    direct callers. The CLI (main(), below) passes a higher default so
    real transient contention (e.g. an overlapping progress write racing
    a slow terminal write) gets a bounded number of short retries instead
    of failing the whole cycle on the first collision."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")

    state_path = Path(state_path) if state_path else default_state_path()
    lock_path = Path(lock_path) if lock_path else default_lock_path()

    attempts = max(1, int(lock_retry_attempts))
    delay = max(0.0, float(lock_retry_delay_seconds))

    for attempt in range(1, attempts + 1):
        try:
            with _state_lock(lock_path):
                prior = _load_state(state_path)
                new_state = _apply_update(
                    prior,
                    run_id=run_id,
                    owner_pid=owner_pid,
                    status=status,
                    stage=stage,
                    message=message,
                    is_start=is_start,
                    metrics=metrics,
                    final_outcome=final_outcome,
                    error_category=error_category,
                    useful_ingestion=useful_ingestion,
                )
                _write_state_atomic(state_path, new_state)
            return new_state
        except LockBusyError:
            if attempt >= attempts:
                raise
            time.sleep(delay)

    raise AssertionError("unreachable")  # pragma: no cover


def start(run_id: str, owner_pid: int, stage: str, message: str, metrics: Optional[dict] = None, **kwargs) -> dict:
    return update_heartbeat(
        run_id=run_id, owner_pid=owner_pid, status=STATUS_RUNNING, stage=stage, message=message,
        is_start=True, metrics=metrics, **kwargs,
    )


def progress(run_id: str, owner_pid: int, stage: str, message: str, metrics: Optional[dict] = None, **kwargs) -> dict:
    return update_heartbeat(
        run_id=run_id, owner_pid=owner_pid, status=STATUS_RUNNING, stage=stage, message=message,
        is_start=False, metrics=metrics, **kwargs,
    )


def finish(
    run_id: str, owner_pid: int, stage: str, message: str, status: str = STATUS_SUCCESS,
    metrics: Optional[dict] = None, final_outcome: Optional[str] = None,
    useful_ingestion: bool = False, **kwargs,
) -> dict:
    return update_heartbeat(
        run_id=run_id, owner_pid=owner_pid, status=status, stage=stage, message=message,
        is_start=False, metrics=metrics, final_outcome=final_outcome,
        useful_ingestion=useful_ingestion, **kwargs,
    )


def fail(
    run_id: str, owner_pid: int, stage: str, message: str, status: str = STATUS_FAILED,
    metrics: Optional[dict] = None, error_category: Optional[str] = None, **kwargs,
) -> dict:
    return update_heartbeat(
        run_id=run_id, owner_pid=owner_pid, status=status, stage=stage, message=message,
        is_start=False, metrics=metrics, error_category=error_category, **kwargs,
    )


def _parse_metrics_json(raw: Optional[str]) -> Optional[dict]:
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("--metrics-json must decode to a JSON object")
    return parsed


DEFAULT_LOCK_RETRY_ATTEMPTS = 5
DEFAULT_LOCK_RETRY_DELAY_SECONDS = 0.2
MAX_LOCK_RETRY_ATTEMPTS = 100
MAX_LOCK_RETRY_DELAY_SECONDS = 10.0


def _parse_bounded_int(raw, default: int, minimum: int, maximum: int) -> int:
    """Never raises. Malformed, empty, non-integer, or out-of-range input
    silently falls back to `default` -- the raw value is never echoed."""
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError):
        return default
    if value < minimum or value > maximum:
        return default
    return value


def _parse_bounded_float(raw, default: float, minimum: float, maximum: float) -> float:
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (ValueError, TypeError):
        return default
    if not (value == value) or value in (float("inf"), float("-inf")):  # NaN/Infinity guard, no math import needed
        return default
    if value < minimum or value > maximum:
        return default
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collection cycle heartbeat writer.")
    parser.add_argument("--state-path", default=None)
    parser.add_argument("--lock-path", default=None)
    parser.add_argument("--lock-retry-attempts", default=None)
    parser.add_argument("--lock-retry-delay-seconds", default=None)

    subparsers = parser.add_subparsers(dest="operation", required=True)

    def add_common(sub):
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--owner-pid", required=True, type=int)
        sub.add_argument("--stage", required=True)
        sub.add_argument("--message", required=True)
        sub.add_argument("--metrics-json", default=None)

    p_start = subparsers.add_parser("start")
    add_common(p_start)

    p_progress = subparsers.add_parser("progress")
    add_common(p_progress)

    p_finish = subparsers.add_parser("finish")
    add_common(p_finish)
    p_finish.add_argument("--status", default=STATUS_SUCCESS, choices=sorted(TERMINAL_STATUSES))
    p_finish.add_argument("--final-outcome", default=None)
    p_finish.add_argument("--useful-ingestion", action="store_true")

    p_fail = subparsers.add_parser("fail")
    add_common(p_fail)
    p_fail.add_argument(
        "--status", default=STATUS_FAILED,
        choices=[STATUS_FAILED, STATUS_ABORTED_AUTH, STATUS_SKIPPED_RUNNING],
    )
    p_fail.add_argument("--error-category", default=None)
    # A failed/non-success terminal state can still carry a precise
    # cycle-level classification (e.g. "partial_failure") and can still
    # advance last_useful_ingestion_at if at least one row was genuinely
    # proven inserted -- see scripts/run_collection_cycle_safe.sh's
    # partial-failure handling. Neither flag changes `status` itself.
    p_fail.add_argument("--final-outcome", default=None)
    p_fail.add_argument("--useful-ingestion", action="store_true")

    return parser


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        metrics = _parse_metrics_json(args.metrics_json)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"collection_heartbeat: invalid --metrics-json: {exc}", file=sys.stderr)
        return 2

    state_path = Path(args.state_path) if args.state_path else None
    lock_path = Path(args.lock_path) if args.lock_path else None

    lock_retry_attempts = _parse_bounded_int(
        args.lock_retry_attempts
        if args.lock_retry_attempts is not None
        else os.environ.get("JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_ATTEMPTS"),
        DEFAULT_LOCK_RETRY_ATTEMPTS, 1, MAX_LOCK_RETRY_ATTEMPTS,
    )
    lock_retry_delay_seconds = _parse_bounded_float(
        args.lock_retry_delay_seconds
        if args.lock_retry_delay_seconds is not None
        else os.environ.get("JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_DELAY_SECONDS"),
        DEFAULT_LOCK_RETRY_DELAY_SECONDS, 0.0, MAX_LOCK_RETRY_DELAY_SECONDS,
    )

    retry_kwargs = dict(lock_retry_attempts=lock_retry_attempts, lock_retry_delay_seconds=lock_retry_delay_seconds)

    try:
        if args.operation == "start":
            state = start(args.run_id, args.owner_pid, args.stage, args.message, metrics,
                           state_path=state_path, lock_path=lock_path, **retry_kwargs)
        elif args.operation == "progress":
            state = progress(args.run_id, args.owner_pid, args.stage, args.message, metrics,
                              state_path=state_path, lock_path=lock_path, **retry_kwargs)
        elif args.operation == "finish":
            state = finish(
                args.run_id, args.owner_pid, args.stage, args.message, status=args.status,
                metrics=metrics, final_outcome=args.final_outcome,
                useful_ingestion=args.useful_ingestion,
                state_path=state_path, lock_path=lock_path, **retry_kwargs,
            )
        elif args.operation == "fail":
            state = fail(
                args.run_id, args.owner_pid, args.stage, args.message, status=args.status,
                metrics=metrics, error_category=args.error_category,
                final_outcome=args.final_outcome, useful_ingestion=args.useful_ingestion,
                state_path=state_path, lock_path=lock_path, **retry_kwargs,
            )
        else:
            print(f"collection_heartbeat: unknown operation: {args.operation}", file=sys.stderr)
            return 2
    except LockBusyError as exc:
        print(f"collection_heartbeat: lock busy: {exc}", file=sys.stderr)
        return 1
    except StaleRunError as exc:
        print(f"collection_heartbeat: stale run rejected: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"collection_heartbeat: write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"HEARTBEAT_OK run_id={state['run_id']} status={state['status']} "
          f"stage={state['stage']} progress_seq={state['progress_seq']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
