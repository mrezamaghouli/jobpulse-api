"""
Cycle-level structured summary: aggregates one
`scripts.linkedin_plan_collect` batch report (see
scripts/linkedin_plan_collect.py:build_batch_report()) into the truthful
classification `scripts/run_collection_cycle_safe.sh` needs in order to
record honest useful-ingestion evidence in the heartbeat -- without ever
inferring anything from human-readable stdout or a bare subprocess exit
code.

Transport, and why it is safe: `scripts/process_search_demand_queue.py`
runs INSIDE the `api` Docker container (invoked by the wrapper via
`docker compose exec -T api ...`) and writes this summary to
JOBPULSE_PROCESS_SUMMARY_RESULT_PATH. `run_collection_cycle_safe.sh`
itself runs on the HOST and reads the same env var's path back out
afterward. This is only possible because `docker-compose.prod.yml`
bind-mounts the api service's `/app/logs` directory from the host's
`./logs` (i.e. `$JOBPULSE_COLLECTION_ROOT/logs` in the wrapper's own
terms) -- confirmed directly from that compose file, not assumed. The
wrapper MUST always point this env var at a path under that shared logs
directory; see docs/PRODUCTION_RUNBOOK.md for the exact convention
(matching the pre-existing `JOBPULSE_COLLECTION_HEARTBEAT` /
`/app/logs/collection_heartbeat.json` <-> `/opt/jobpulse/logs/...`
fallback pattern already used by app/admin_status.py).
"""
import dataclasses
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = 1

RESULT_PATH_ENV_VAR = "JOBPULSE_PROCESS_SUMMARY_RESULT_PATH"

# --- cycle-level classification (conservative, evidence-only) --------------
OUTCOME_USEFUL_SUCCESS = "useful_success"
OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS = "technical_success_no_new_rows"
OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS = "technical_success_no_results"
OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL = "technical_success_filtered_all"
OUTCOME_PARTIAL_FAILURE = "partial_failure"
OUTCOME_FAILED = "failed"

OUTCOMES = frozenset({
    OUTCOME_USEFUL_SUCCESS,
    OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS,
    OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS,
    OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL,
    OUTCOME_PARTIAL_FAILURE,
    OUTCOME_FAILED,
})

# partial_failure and failed are deliberately NOT "success" outcomes --
# the wrapper must never call heartbeat `finish` with a success status
# for either of these; see scripts/run_collection_cycle_safe.sh.
SUCCESS_OUTCOMES = frozenset({
    OUTCOME_USEFUL_SUCCESS,
    OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS,
    OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS,
    OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL,
})

ERROR_CATEGORY_MISSING_RESULT = "missing_result"
ERROR_CATEGORY_RESULT_READ_ERROR = "result_read_error"
ERROR_CATEGORY_INVALID_RESULT = "invalid_result"

_AGGREGATE_COUNTER_FIELDS = (
    "jobs_discovered",
    "jobs_valid",
    "jobs_filtered_invalid",
    "jobs_filtered_non_linkedin",
    "jobs_filtered_header_artifact",
    "jobs_filtered_missing_identifier",
    "rows_inserted",
    "rows_updated_existing",
    "persistence_errors",
)

MAX_COUNTER_VALUE = 10_000_000
MAX_GENERATED_AT_LEN = 64


@dataclasses.dataclass
class ProcessSummary:
    schema_version: int
    generated_at: str
    had_pending_targets: bool
    total_queries: int
    successful_queries: int
    failed_queries: int
    useful_queries: int
    zero_yield_queries: int
    skipped_queries: int
    partial_failure: bool
    aggregate_collector_metrics: dict
    outcome: str

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def classify_cycle_outcome(batch_report: Optional[dict], had_pending_targets: bool) -> str:
    """Deterministic, pure. Uses only proven, already-aggregated evidence
    from linkedin_plan_collect's own batch report -- never re-derives or
    guesses anything about individual queries.

    - No pending targets at all: there was nothing to search for, which
      is not a failure -- classified the same as "searched and found
      nothing" (technical_success_no_results).
    - Every query failed (or there were zero queries in a batch that WAS
      attempted): failed.
    - At least one query failed and at least one succeeded: partial_failure
      -- always visible, never silently folded into a success outcome.
    - All queries succeeded: useful_success if any row was PROVEN
      inserted anywhere in the batch, else technical_success_no_new_rows
      if any row was proven updated, else technical_success_no_results /
      technical_success_filtered_all depending on whether anything was
      discovered at all.
    """
    if not had_pending_targets:
        return OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS

    if batch_report is None:
        return OUTCOME_FAILED

    total = batch_report.get("total_queries", 0)
    successful = batch_report.get("successful_queries", 0)

    if total == 0 or successful == 0:
        return OUTCOME_FAILED

    if batch_report.get("partial_failure"):
        return OUTCOME_PARTIAL_FAILURE

    agg = batch_report.get("aggregate_collector_metrics") or {}
    rows_inserted = agg.get("rows_inserted", 0) or 0
    rows_updated_existing = agg.get("rows_updated_existing", 0) or 0
    jobs_discovered = agg.get("jobs_discovered", 0) or 0

    if rows_inserted > 0:
        return OUTCOME_USEFUL_SUCCESS

    if rows_updated_existing > 0:
        return OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS

    if jobs_discovered == 0:
        return OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS

    return OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL


def build_summary(batch_report: Optional[dict], had_pending_targets: bool) -> ProcessSummary:
    batch_report = batch_report or {
        "total_queries": 0, "successful_queries": 0, "failed_queries": 0,
        "useful_queries": 0, "zero_yield_queries": 0, "skipped_queries": 0,
        "partial_failure": False,
        "aggregate_collector_metrics": {field: 0 for field in _AGGREGATE_COUNTER_FIELDS},
    }

    outcome = classify_cycle_outcome(batch_report, had_pending_targets)

    return ProcessSummary(
        schema_version=SCHEMA_VERSION,
        # Timezone-AWARE, UTC. An earlier revision used datetime.now()
        # (naive, local time) -- read_summary() requires a tz-aware
        # timestamp, so a document built by this earlier code would have
        # failed its own reader's validation.
        generated_at=datetime.now(timezone.utc).isoformat(),
        had_pending_targets=had_pending_targets,
        total_queries=int(batch_report.get("total_queries", 0)),
        successful_queries=int(batch_report.get("successful_queries", 0)),
        failed_queries=int(batch_report.get("failed_queries", 0)),
        useful_queries=int(batch_report.get("useful_queries", 0)),
        zero_yield_queries=int(batch_report.get("zero_yield_queries", 0)),
        skipped_queries=int(batch_report.get("skipped_queries", 0)),
        partial_failure=bool(batch_report.get("partial_failure", False)),
        aggregate_collector_metrics={
            field: int((batch_report.get("aggregate_collector_metrics") or {}).get(field, 0) or 0)
            for field in _AGGREGATE_COUNTER_FIELDS
        },
        outcome=outcome,
    )


SUMMARY_FILE_MODE = 0o644


def write_summary_atomic(path: os.PathLike, summary: ProcessSummary) -> None:
    """Same atomicity contract as scripts.collector_result.write_result_atomic
    -- tempfile in the same directory, fsync, os.replace. Raises on
    failure; the caller must treat that as a hard failure, not a silent
    no-op.

    Explicit mode, not umask: this is the ONE write_*_atomic in the
    codebase whose reader crosses a UID boundary -- this function runs
    INSIDE the api container (uid=0/root; see Dockerfile, which has no
    USER directive) and scripts/run_collection_cycle_safe.sh reads the
    published file back on the HOST as a non-root user, via
    docker-compose.prod.yml's `./logs:/app/logs` bind mount.
    `tempfile.mkstemp()` always creates its file at mode 0600 regardless
    of umask (Python's own documented, deliberate hardening for a
    generic temp file that might hold secrets) -- without an explicit
    mode change, the published summary stayed root-only-readable and
    every host-side read failed with ERROR_CATEGORY_RESULT_READ_ERROR
    ("... its summary is unusable (category=result_read_error)" in the
    wrapper's log), exactly the production symptom this fixes. The
    summary contains only operational counts/classification -- no
    credentials, no PII, no job content -- so a world-readable 0644 is
    appropriate.

    Ordering (deliberate): the temp file stays at its private 0600 mode
    for the ENTIRE time it might hold incomplete content -- write, flush,
    fsync all happen first. Only once the content is fully durable does
    `os.fchmod(f.fileno(), SUMMARY_FILE_MODE)` widen the already-open
    file descriptor's permissions -- fchmod is used instead of a path-
    based os.chmod() specifically because the open fd identifies the
    exact inode being published, immune to any path-based TOCTOU
    concern. Only THEN does `os.replace()` publish it. This guarantees
    two invariants simultaneously: (1) nothing with 0644-level access
    can ever observe a half-written temp file (it's 0600 for its entire
    incomplete lifetime), and (2) nothing can ever observe the file at
    the FINAL path with any mode other than 0644 -- os.replace is a
    single rename syscall, so by the time any reader can see `path` at
    all, the inode behind it has already had its final mode set. There
    is no post-rename chmod window.
    """
    path = Path(path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".process_summary.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(summary.to_dict(), f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), SUMMARY_FILE_MODE)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class SummaryReadError(Exception):
    """Raised by read_summary() for a missing, unreadable, or malformed
    summary document. `category` is one of ERROR_CATEGORY_MISSING_RESULT /
    ERROR_CATEGORY_RESULT_READ_ERROR / ERROR_CATEGORY_INVALID_RESULT --
    the same three-way split as scripts.collector_result.ResultReadError,
    for the same reason: an unreadable file is an infrastructure problem,
    not evidence the writer produced something malformed."""

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, message: str):
    if not condition:
        raise SummaryReadError(message, ERROR_CATEGORY_INVALID_RESULT)


def _validate_generated_at(raw: dict) -> str:
    value = raw.get("generated_at")
    _require(isinstance(value, str) and not isinstance(value, bool), "field 'generated_at' must be a string")
    _require(len(value) <= MAX_GENERATED_AT_LEN, "field 'generated_at' exceeds the maximum allowed length")
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise SummaryReadError("field 'generated_at' is not a valid ISO-8601 timestamp", ERROR_CATEGORY_INVALID_RESULT)
    _require(dt.tzinfo is not None, "field 'generated_at' must be timezone-aware")
    _require(dt.utcoffset() == timedelta(0), "field 'generated_at' must be expressed in UTC (zero offset)")
    return value


def read_summary(path: os.PathLike) -> ProcessSummary:
    """Strictly validates a process summary document, in two layers:

    1. Per-field type/range checks, and a set of individually-named
       cross-field invariants (each with its own descriptive rejection
       message -- see the explicit `_require(...)` calls below).
    2. The ULTIMATE check: reconstructs the canonical batch evidence from
       the document's own fields and calls the SAME deterministic
       `classify_cycle_outcome()` the writer (`build_summary()`) used.
       The persisted `outcome` must equal the recomputed one EXACTLY. A
       document can pass every individual check above and still be
       rejected here if it does not match what classify_cycle_outcome()
       itself would have produced from the same evidence -- this is what
       makes the validation semantic rather than merely structural.

    Never persists or logs a raw malformed value -- only field names and
    a description of the violated constraint.
    """
    path = Path(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SummaryReadError(f"process summary file missing: {path}", ERROR_CATEGORY_MISSING_RESULT)
    except OSError as exc:
        raise SummaryReadError(
            f"process summary file could not be read ({type(exc).__name__})", ERROR_CATEGORY_RESULT_READ_ERROR
        ) from exc

    try:
        raw = json.loads(raw_text)
    except Exception as exc:
        raise SummaryReadError(f"process summary file is not valid JSON: {type(exc).__name__}", ERROR_CATEGORY_INVALID_RESULT) from exc

    _require(isinstance(raw, dict), "process summary file does not contain a JSON object")
    _require(_is_strict_int(raw.get("schema_version")) and raw.get("schema_version") == SCHEMA_VERSION,
              f"unsupported schema_version (expected {SCHEMA_VERSION})")
    _validate_generated_at(raw)
    _require(isinstance(raw.get("had_pending_targets"), bool), "field 'had_pending_targets' must be a boolean")
    _require(isinstance(raw.get("partial_failure"), bool), "field 'partial_failure' must be a boolean")
    _require(raw.get("outcome") in OUTCOMES, "field 'outcome' is not a supported value")

    counter_fields = (
        "total_queries", "successful_queries", "failed_queries",
        "useful_queries", "zero_yield_queries", "skipped_queries",
    )
    for field in counter_fields:
        value = raw.get(field)
        _require(_is_strict_int(value), f"field {field!r} must be an integer")
        _require(0 <= value <= MAX_COUNTER_VALUE, f"field {field!r} is out of bounds")

    agg = raw.get("aggregate_collector_metrics")
    _require(isinstance(agg, dict), "field 'aggregate_collector_metrics' must be an object")
    validated_agg = {}
    for field in _AGGREGATE_COUNTER_FIELDS:
        value = agg.get(field)
        _require(_is_strict_int(value), f"aggregate_collector_metrics[{field!r}] must be an integer")
        _require(0 <= value <= MAX_COUNTER_VALUE, f"aggregate_collector_metrics[{field!r}] is out of bounds")
        validated_agg[field] = value

    total_queries = raw["total_queries"]
    successful_queries = raw["successful_queries"]
    failed_queries = raw["failed_queries"]
    useful_queries = raw["useful_queries"]
    zero_yield_queries = raw["zero_yield_queries"]
    had_pending_targets = raw["had_pending_targets"]
    partial_failure = raw["partial_failure"]
    outcome = raw["outcome"]

    # --- explicit, individually-named cross-field invariants ---
    _require(successful_queries + failed_queries == total_queries,
              "successful_queries + failed_queries must equal total_queries")
    _require(useful_queries <= successful_queries, "useful_queries cannot exceed successful_queries")
    _require(zero_yield_queries <= successful_queries, "zero_yield_queries cannot exceed successful_queries")
    _require(useful_queries + zero_yield_queries <= successful_queries,
              "useful_queries + zero_yield_queries cannot exceed successful_queries")

    # partial_failure is true IF AND ONLY IF both successful_queries > 0
    # and failed_queries > 0 -- checked in both directions, not just when
    # outcome happens to be "partial_failure".
    _require(partial_failure == (successful_queries > 0 and failed_queries > 0),
              "partial_failure must be true exactly when successful_queries > 0 and failed_queries > 0")

    if not had_pending_targets:
        _require(total_queries == 0 and successful_queries == 0 and failed_queries == 0,
                  "had_pending_targets=false requires zero attempted queries")
        _require(outcome == OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS,
                  "had_pending_targets=false requires outcome=technical_success_no_results")

    if outcome == OUTCOME_USEFUL_SUCCESS:
        _require(failed_queries == 0, "useful_success requires zero failed queries")
        _require(validated_agg["rows_inserted"] > 0, "useful_success requires at least one proven inserted row")

    if outcome == OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS:
        _require(failed_queries == 0, "technical_success_no_new_rows requires zero failed queries")
        _require(validated_agg["rows_inserted"] == 0, "technical_success_no_new_rows requires zero proven inserted rows")
        _require(validated_agg["rows_updated_existing"] > 0, "technical_success_no_new_rows requires at least one proven update")

    if outcome == OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS and had_pending_targets:
        _require(failed_queries == 0, "technical_success_no_results (with pending targets) requires zero failed queries")
        _require(validated_agg["jobs_discovered"] == 0, "technical_success_no_results requires zero discovered jobs")

    if outcome == OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL:
        _require(failed_queries == 0, "technical_success_filtered_all requires zero failed queries")
        _require(validated_agg["jobs_discovered"] > 0, "technical_success_filtered_all requires discovered jobs > 0")
        _require(validated_agg["rows_inserted"] == 0 and validated_agg["rows_updated_existing"] == 0,
                  "technical_success_filtered_all requires zero inserted/updated rows")

    if outcome == OUTCOME_PARTIAL_FAILURE:
        _require(successful_queries > 0 and failed_queries > 0,
                  "partial_failure requires both successful_queries > 0 and failed_queries > 0")

    if outcome == OUTCOME_FAILED:
        _require(successful_queries == 0, "failed requires zero successful queries")

    # Aggregate collector counters: sums across every query's own
    # CollectorResult must satisfy the same "no over-count" partition
    # collector_result.py itself enforces per-query -- exact equality is
    # NOT required here (a persistence error in any one query can leave
    # its own partition inexact, and this is a sum across many queries),
    # but an aggregate that claims MORE processed rows than it discovered
    # is never legitimate.
    outer_partition = (
        validated_agg["jobs_filtered_invalid"] + validated_agg["jobs_filtered_non_linkedin"] + validated_agg["jobs_valid"]
    )
    _require(outer_partition <= validated_agg["jobs_discovered"],
              "aggregate outer filter counters cannot exceed aggregate jobs_discovered")
    row_level_total = (
        validated_agg["jobs_filtered_header_artifact"] + validated_agg["jobs_filtered_missing_identifier"]
        + validated_agg["rows_inserted"] + validated_agg["rows_updated_existing"]
    )
    _require(row_level_total <= validated_agg["jobs_valid"],
              "aggregate row-level outcomes cannot exceed aggregate jobs_valid")

    # --- the ultimate check: recompute via the SAME function the writer
    # used, and require an exact match. This is what makes validation
    # semantic, not merely structural -- a document can satisfy every
    # check above individually and still be rejected here if it doesn't
    # match what classify_cycle_outcome() itself would produce.
    reconstructed_batch = {
        "total_queries": total_queries,
        "successful_queries": successful_queries,
        "failed_queries": failed_queries,
        "partial_failure": partial_failure,
        "aggregate_collector_metrics": validated_agg,
    }
    recomputed_outcome = classify_cycle_outcome(reconstructed_batch, had_pending_targets)
    _require(
        recomputed_outcome == outcome,
        f"persisted outcome does not match the outcome recomputed from its own evidence "
        f"(recomputed={recomputed_outcome!r})",
    )

    return ProcessSummary(
        schema_version=raw["schema_version"],
        generated_at=raw["generated_at"],
        had_pending_targets=had_pending_targets,
        total_queries=total_queries,
        successful_queries=successful_queries,
        failed_queries=failed_queries,
        useful_queries=useful_queries,
        zero_yield_queries=zero_yield_queries,
        skipped_queries=raw["skipped_queries"],
        partial_failure=partial_failure,
        aggregate_collector_metrics=validated_agg,
        outcome=outcome,
    )
