"""
Structured, versioned collector result: the truthful, machine-readable
record of a single `python -m scripts.collector_postgres` invocation.

This module exists because the collector's previous behavior made claims
that were not proven by any observed effect:

  1. `inserted_or_updated_count` was incremented unconditionally after
     calling `insert_job()`, even on the internal early-return paths where
     `insert_job()` never executed an INSERT/UPDATE statement at all.
  2. `skipped_duplicate_count` was declared, printed, and never
     incremented anywhere -- a permanently-dead counter reported as if it
     were real.
  3. Zero jobs discovered, all jobs filtered, and jobs actually inserted
     were all reported via the same "finished successfully" exit code (0)
     and free-text stdout that no caller ever parsed.
  4. (Adversarial pass) An early revision of this module classified
     `success_with_new_rows` from a whole-table `SELECT COUNT(*)`
     before/after delta. That is not proof this collector inserted
     anything: a concurrent process inserting or deleting rows during the
     same window changes the delta without this collector having written
     a single row, and vice versa. That delta is no longer computed or
     used anywhere in this module or in scripts/collector_postgres.py.

Every outcome below is derived only from evidence the collector actually
observed: a discovered row count, or a per-statement `RETURNING` result
from the exact `INSERT ... ON CONFLICT DO UPDATE` this collector itself
executed (see `ROW_OUTCOME_INSERTED` / `ROW_OUTCOME_UPDATED_EXISTING`
below and scripts/collector_postgres.py:insert_job() for the SQL and its
documented limitations) -- never a global aggregate, and never inferred
or assumed.
"""
import dataclasses
import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 1

RESULT_PATH_ENV_VAR = "JOBPULSE_COLLECTOR_RESULT_PATH"

# --- per-row outcomes returned by insert_job() -----------------------------
# Every early-return path in insert_job() -- and every path that reaches
# its SQL statement -- must return one of these instead of None, so the
# caller can count based on proven fact, never assumption.
ROW_OUTCOME_INSERTED = "inserted"
ROW_OUTCOME_UPDATED_EXISTING = "updated_existing"
ROW_OUTCOME_FILTERED_HEADER_ARTIFACT = "filtered_header_artifact"
ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER = "filtered_missing_identifier"

ROW_OUTCOMES = frozenset({
    ROW_OUTCOME_INSERTED,
    ROW_OUTCOME_UPDATED_EXISTING,
    ROW_OUTCOME_FILTERED_HEADER_ARTIFACT,
    ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER,
})

# --- whole-run outcomes (this module's `outcome` field) --------------------
OUTCOME_SUCCESS_WITH_NEW_ROWS = "success_with_new_rows"
OUTCOME_SUCCESS_NO_NEW_ROWS = "success_no_new_rows"
OUTCOME_SUCCESS_NO_RESULTS = "success_no_results"
OUTCOME_SUCCESS_FILTERED_ALL = "success_filtered_all"
OUTCOME_FAILED_FETCH = "failed_fetch"
OUTCOME_FAILED_PERSIST = "failed_persist"
OUTCOME_FAILED_INTERNAL = "failed_internal"

OUTCOMES = frozenset({
    OUTCOME_SUCCESS_WITH_NEW_ROWS,
    OUTCOME_SUCCESS_NO_NEW_ROWS,
    OUTCOME_SUCCESS_NO_RESULTS,
    OUTCOME_SUCCESS_FILTERED_ALL,
    OUTCOME_FAILED_FETCH,
    OUTCOME_FAILED_PERSIST,
    OUTCOME_FAILED_INTERNAL,
})

SUCCESS_OUTCOMES = frozenset({
    OUTCOME_SUCCESS_WITH_NEW_ROWS,
    OUTCOME_SUCCESS_NO_NEW_ROWS,
    OUTCOME_SUCCESS_NO_RESULTS,
    OUTCOME_SUCCESS_FILTERED_ALL,
})

FAILED_OUTCOMES = frozenset({
    OUTCOME_FAILED_FETCH,
    OUTCOME_FAILED_PERSIST,
    OUTCOME_FAILED_INTERNAL,
})

# --- error categories --------------------------------------------------
ERROR_CATEGORY_FETCH = "fetch_error"
ERROR_CATEGORY_PERSIST = "persistence_error"
ERROR_CATEGORY_INTERNAL = "internal_error"

# Categories a *reader* of the result (linkedin_plan_collect.py, or any
# other caller of read_result()) can encounter on top of the ones the
# collector itself ever writes into a result document -- these describe a
# problem observing/validating the result, not a problem the collector
# reported about itself. Kept as three DISTINCT categories rather than
# collapsing "couldn't read it" and "read it but it's garbage" into one:
#   - missing_result: the file does not exist.
#   - result_read_error: the file exists but could not be read (permission
#     denied, is a directory, I/O error, etc.) -- an infrastructure
#     problem, not evidence the collector produced anything malformed.
#   - invalid_result: the file was read successfully but its content is
#     not a valid, internally-consistent CollectorResult.
ERROR_CATEGORY_MISSING_RESULT = "missing_result"
ERROR_CATEGORY_RESULT_READ_ERROR = "result_read_error"
ERROR_CATEGORY_INVALID_RESULT = "invalid_result"

# --- validation bounds -------------------------------------------------
MAX_PROVIDER_LEN = 200
MAX_ERROR_TEXT_LEN = 2000
MAX_COUNTER_VALUE = 1_000_000
MAX_DURATION_SECONDS = 7 * 24 * 3600.0  # 1 week -- generous, but not unbounded


@dataclasses.dataclass
class CollectorResult:
    schema_version: int
    provider: str
    started_at: str
    finished_at: str
    duration_seconds: float
    jobs_discovered: int
    jobs_valid: int
    jobs_filtered_invalid: int
    jobs_filtered_non_linkedin: int
    jobs_filtered_header_artifact: int
    jobs_filtered_missing_identifier: int
    rows_inserted: int
    rows_updated_existing: int
    persistence_errors: int
    outcome: str
    error_category: Optional[str] = None
    sanitized_error: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def determine_outcome(
    jobs_discovered: int,
    rows_inserted: int,
    rows_updated_existing: int,
    persistence_errors: int,
) -> str:
    """Deterministic, pure outcome classifier. No I/O, no exceptions.

    `rows_inserted` / `rows_updated_existing` must come only from proven
    per-statement RETURNING evidence (see insert_job()) -- never from a
    whole-table COUNT(*) delta, which cannot distinguish this collector's
    own writes from a concurrent process's.

    Rules, in order:
      1. Any persistence error observed while writing rows -> failed_persist.
         (Caller is responsible for not calling this at all on a fetch-stage
         failure -- that path builds failed_fetch directly, before any of
         these counters exist.)
      2. Nothing discovered at all -> success_no_results.
      3. Something discovered, but no SQL write was ever proven (every row
         was filtered somewhere -- outer validity/source check or
         insert_job()'s own internal skip) -> success_filtered_all.
      4. At least one row is PROVEN inserted (RETURNING (xmax = 0) = true
         on its own INSERT ... ON CONFLICT DO UPDATE statement) ->
         success_with_new_rows.
      5. At least one row was written but none was a proven insert (every
         write instead conflicted and updated an existing row) ->
         success_no_new_rows.
    """
    if persistence_errors > 0:
        return OUTCOME_FAILED_PERSIST

    if jobs_discovered == 0:
        return OUTCOME_SUCCESS_NO_RESULTS

    if rows_inserted == 0 and rows_updated_existing == 0:
        return OUTCOME_SUCCESS_FILTERED_ALL

    if rows_inserted > 0:
        return OUTCOME_SUCCESS_WITH_NEW_ROWS

    return OUTCOME_SUCCESS_NO_NEW_ROWS


_SECRET_PATTERNS = [
    re.compile(r"password\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"://[^/\s@]+:[^/\s@]+@"),  # user:pass@host in a URL/DSN
    re.compile(r"\bAPI[_-]?KEY\S*", re.IGNORECASE),
]


def sanitize_error_text(exc: BaseException, max_len: int = 500) -> str:
    """Turns an exception into a short, safe-to-persist string.

    Never includes: credentials, DSN/URL user:pass segments, or raw
    provider payloads (which, if present at all, only ever show up inside
    an exception's own str() -- this function only ever sees that str(),
    never the payload object itself, so there is nothing else to leak).
    Always bounded in length so a pathological exception message can't
    balloon the result file.
    """
    text = f"{type(exc).__name__}: {exc}"
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    text = text.replace("\x00", "")
    if len(text) > max_len:
        text = text[:max_len] + "...(truncated)"
    return text


def write_result_atomic(path: os.PathLike, result: CollectorResult) -> None:
    """Writes exactly one JSON document via tempfile + fsync + os.replace.

    Raises on any failure -- callers must not swallow this: a failed
    result write must surface as a non-zero process exit, per the phase
    2A contract ("result-write failure must return non-zero"). No partial
    file is ever exposed at `path`: the temp file lives in the same
    directory (so os.replace is atomic on the same filesystem) and is
    only ever renamed into place after a successful fsync.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".collector_result.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ResultReadError(Exception):
    """Raised by read_result() for a missing, unreadable, or malformed
    result file. `category` is one of ERROR_CATEGORY_MISSING_RESULT /
    ERROR_CATEGORY_RESULT_READ_ERROR / ERROR_CATEGORY_INVALID_RESULT.

    The exception message is diagnostic text for logs only -- it may
    describe *which* field/invariant failed, but must never echo the raw
    offending value verbatim (only its type/shape), so a malformed value
    is never itself persisted or logged.
    """

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


def _is_strict_int(value) -> bool:
    """True only for a real int -- bool is a subclass of int in Python
    and must never be accepted where a counter is expected."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(condition: bool, message: str):
    if not condition:
        raise ResultReadError(message, ERROR_CATEGORY_INVALID_RESULT)


def _validate_schema_version(raw: dict):
    value = raw.get("schema_version")
    _require(_is_strict_int(value), "field 'schema_version' must be an integer")
    _require(value == SCHEMA_VERSION, f"unsupported schema_version (expected {SCHEMA_VERSION})")


def _validate_provider(raw: dict) -> str:
    value = raw.get("provider")
    _require(isinstance(value, str) and not isinstance(value, bool), "field 'provider' must be a string")
    _require(0 < len(value) <= MAX_PROVIDER_LEN, "field 'provider' must be a non-empty, bounded string")
    return value


def _validate_timestamp(raw: dict, name: str) -> str:
    value = raw.get(name)
    _require(isinstance(value, str) and not isinstance(value, bool), f"field {name!r} must be a string")
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise ResultReadError(f"field {name!r} is not a valid ISO-8601 timestamp", ERROR_CATEGORY_INVALID_RESULT)
    _require(dt.tzinfo is not None, f"field {name!r} must be timezone-aware")
    return value


def _validate_duration(raw: dict) -> float:
    value = raw.get("duration_seconds")
    _require(_is_strict_number(value), "field 'duration_seconds' must be a number")
    _require(math.isfinite(value), "field 'duration_seconds' must be finite (not NaN/Infinity)")
    _require(0 <= value <= MAX_DURATION_SECONDS, "field 'duration_seconds' is out of bounds")
    return float(value)


_COUNTER_FIELDS = (
    "jobs_discovered", "jobs_valid",
    "jobs_filtered_invalid", "jobs_filtered_non_linkedin",
    "jobs_filtered_header_artifact", "jobs_filtered_missing_identifier",
    "rows_inserted", "rows_updated_existing",
    "persistence_errors",
)


def _validate_counter(raw: dict, name: str) -> int:
    value = raw.get(name)
    _require(_is_strict_int(value), f"field {name!r} must be an integer")
    _require(value >= 0, f"field {name!r} must be non-negative")
    _require(value <= MAX_COUNTER_VALUE, f"field {name!r} exceeds the maximum allowed value")
    return value


def _validate_optional_bounded_string(raw: dict, name: str, max_len: int = MAX_ERROR_TEXT_LEN) -> Optional[str]:
    value = raw.get(name)
    if value is None:
        return None
    _require(isinstance(value, str) and not isinstance(value, bool), f"field {name!r} must be a string or null")
    _require(len(value) <= max_len, f"field {name!r} exceeds the maximum allowed length")
    return value


def _validate_outcome(raw: dict) -> str:
    value = raw.get("outcome")
    _require(isinstance(value, str) and value in OUTCOMES, "field 'outcome' is not a supported value")
    return value


def _validate_invariants(values: dict):
    """Cross-field invariants: a syntactically valid document can still
    describe a physically impossible or self-contradictory result --
    those must be rejected too, not merely type/range-checked field by
    field."""
    jobs_discovered = values["jobs_discovered"]
    jobs_valid = values["jobs_valid"]
    jobs_filtered_invalid = values["jobs_filtered_invalid"]
    jobs_filtered_non_linkedin = values["jobs_filtered_non_linkedin"]
    jobs_filtered_header_artifact = values["jobs_filtered_header_artifact"]
    jobs_filtered_missing_identifier = values["jobs_filtered_missing_identifier"]
    rows_inserted = values["rows_inserted"]
    rows_updated_existing = values["rows_updated_existing"]
    persistence_errors = values["persistence_errors"]
    outcome = values["outcome"]
    error_category = values["error_category"]

    _require(jobs_valid <= jobs_discovered, "jobs_valid cannot exceed jobs_discovered")

    outer_partition = jobs_filtered_invalid + jobs_filtered_non_linkedin + jobs_valid
    _require(outer_partition <= jobs_discovered, "outer filter counters cannot exceed jobs_discovered")

    row_level_total = (
        jobs_filtered_header_artifact + jobs_filtered_missing_identifier
        + rows_inserted + rows_updated_existing
    )
    _require(row_level_total <= jobs_valid, "row-level outcomes cannot exceed jobs_valid")

    # These partitions are only guaranteed EXACT when nothing interrupted
    # the loop partway through (persistence_errors == 0). A persistence
    # error can abort the batch before every discovered/valid row was
    # ever classified, so a strict `<` is legitimate in that case -- but
    # `>` is never legitimate, and is already rejected above regardless.
    if persistence_errors == 0:
        _require(outer_partition == jobs_discovered,
                 "counters must exactly partition jobs_discovered when there were no persistence errors")
        _require(row_level_total == jobs_valid,
                 "row-level outcomes must exactly account for jobs_valid when there were no persistence errors")

    if outcome in SUCCESS_OUTCOMES:
        _require(persistence_errors == 0, "a success_* outcome cannot have persistence_errors > 0")

    if outcome == OUTCOME_SUCCESS_NO_RESULTS:
        _require(jobs_discovered == 0, "success_no_results requires jobs_discovered == 0")

    if outcome == OUTCOME_SUCCESS_FILTERED_ALL:
        _require(jobs_discovered > 0, "success_filtered_all requires jobs_discovered > 0")
        _require(rows_inserted == 0 and rows_updated_existing == 0,
                 "success_filtered_all requires no proven SQL writes")

    if outcome == OUTCOME_SUCCESS_WITH_NEW_ROWS:
        _require(rows_inserted > 0, "success_with_new_rows requires at least one proven inserted row")

    if outcome == OUTCOME_SUCCESS_NO_NEW_ROWS:
        _require(rows_inserted == 0 and rows_updated_existing > 0,
                 "success_no_new_rows requires at least one proven update and zero proven inserts")

    if outcome in FAILED_OUTCOMES:
        _require(error_category is not None, "a failed_* outcome requires a non-null error_category")

    if outcome == OUTCOME_FAILED_PERSIST:
        _require(persistence_errors > 0, "failed_persist requires persistence_errors > 0")


def read_result(path: os.PathLike) -> CollectorResult:
    """Strictly validates a result document read from `path`.

    Raises ResultReadError with a stable `category`:
      - missing_result: the file does not exist.
      - result_read_error: the file exists but reading it failed for an
        infrastructure reason (permission denied, is a directory, etc.).
      - invalid_result: the file was read successfully but its content is
        not a valid, internally-consistent CollectorResult -- malformed
        JSON, wrong types, out-of-range values, or a contradiction between
        `outcome` and the counters (e.g. success_with_new_rows with
        rows_inserted == 0).

    Never persists or logs a raw malformed value -- only field names and
    a description of the violated constraint.
    """
    path = Path(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ResultReadError(f"collector result file missing: {path}", ERROR_CATEGORY_MISSING_RESULT)
    except OSError as exc:
        raise ResultReadError(
            f"collector result file could not be read ({type(exc).__name__})", ERROR_CATEGORY_RESULT_READ_ERROR
        ) from exc

    try:
        raw = json.loads(raw_text)
    except Exception as exc:
        raise ResultReadError(f"collector result file is not valid JSON: {type(exc).__name__}", ERROR_CATEGORY_INVALID_RESULT) from exc

    _require(isinstance(raw, dict), "collector result file does not contain a JSON object")

    _validate_schema_version(raw)
    _validate_outcome(raw)

    values = {
        "schema_version": raw["schema_version"],
        "provider": _validate_provider(raw),
        "started_at": _validate_timestamp(raw, "started_at"),
        "finished_at": _validate_timestamp(raw, "finished_at"),
        "duration_seconds": _validate_duration(raw),
        "outcome": raw["outcome"],
        "error_category": _validate_optional_bounded_string(raw, "error_category"),
        "sanitized_error": _validate_optional_bounded_string(raw, "sanitized_error"),
    }
    for field in _COUNTER_FIELDS:
        values[field] = _validate_counter(raw, field)

    _validate_invariants(values)

    return CollectorResult(**values)
