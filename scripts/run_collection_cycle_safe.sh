#!/usr/bin/env bash
# Safe collection cycle wrapper (phase 2A, adversarially hardened).
#
# Reliability contract (see docs/PRODUCTION_RUNBOOK.md for the operator
# summary):
#   - One UUID run_id is generated once per invocation and attached to
#     every heartbeat write for this run (scripts/collection_heartbeat.py),
#     along with this shell process's own stable PID as `owner_pid`.
#   - A non-blocking top-level flock is acquired BEFORE any heartbeat is
#     written or any collector operation is invoked. On contention this
#     script logs a stable "already_running" event, creates no run_id,
#     touches no heartbeat state, and exits 0 (documented duplicate-run
#     status -- the other invocation is actively doing the work, this is
#     not a failure).
#   - Every Docker/PostgreSQL/alert-sender external call is bounded by
#     GNU `timeout` (no --foreground, so the whole child process group is
#     killed on timeout, including grandchildren). A timeout is treated
#     as a failure of that step, with a distinct `category=timeout` (or
#     `timeout_utility_missing` if `timeout` isn't installed) persisted
#     into the heartbeat's error_category and logged.
#   - Queue-count PostgreSQL queries never default a failed/timed-out/
#     malformed read to zero: failure is propagated as a distinct cycle
#     failure (exit 7), never silently treated as "0 pending".
#   - A failed cycle NEVER exits 0 based solely on process exit status.
#     Exit codes (small, documented taxonomy):
#       0 = technical success (including a zero-yield or no-op cycle)
#       2 = auth/preflight failure
#       3 = heartbeat or history persistence failure (nothing else went wrong)
#       4 = seed step failure
#       5 = queue-processing step failure (including a missing/malformed
#           process summary after an otherwise-successful docker exec)
#       6 = reconciliation step failure
#       7 = queue-count (pending/running) DB-query failure
#       8 = the process summary was structurally VALID but its own
#           classification is not a success outcome (e.g. partial_failure:
#           some demand-queue queries failed and some succeeded) -- the
#           docker-exec step itself exited 0 and real work happened, but
#           the batch is not reported as an unqualified technical success.
#           Distinct from 5, which is a missing/malformed/unreadable
#           summary, not a validly-read non-success one.
#   - Alert delivery (send_telegram_alerts.py) is best-effort and its own
#     exit code is logged but NEVER allowed to replace or hide the exit
#     code already determined by the collector failure that triggered it.
set -u

# validate_duration_seconds <raw_value> <default> <min> <max>
# Identical contract to the helper of the same name in
# scripts/production_health_alert.sh (duplicated here rather than
# sourced, for the same reason: that script has top-level executable
# logic that would run if sourced). Prints a safe, validated non-negative
# integer; any value that is not a plain unsigned integer of at most 9
# digits (avoids bash arithmetic overflow) within [min,max] falls back to
# <default>. The raw value is never echoed/logged.
validate_duration_seconds() {
  local raw="$1" default="$2" min="$3" max="$4"
  local trimmed="$raw"
  trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"

  if [[ "$trimmed" =~ ^[0-9]{1,9}$ ]] && [ "$trimmed" -ge "$min" ] && [ "$trimmed" -le "$max" ]; then
    printf '%s' "$trimmed"
  else
    printf '%s' "$default"
  fi
}

ROOT="${JOBPULSE_COLLECTION_ROOT:-/opt/jobpulse}"
cd "$ROOT" || exit 1

if [ -f "$ROOT/.collection.env" ]; then
  set -a
  . "$ROOT/.collection.env"
  set +a
fi

COMPOSE_FILE="${JOBPULSE_COLLECTION_COMPOSE_FILE:-$ROOT/docker-compose.prod.yml}"
LOG_FILE="${JOBPULSE_COLLECTION_LOG_FILE:-$ROOT/logs/collection_cycle.log}"
HEARTBEAT_STATE_PATH="${JOBPULSE_COLLECTION_HEARTBEAT_STATE_PATH:-$ROOT/logs/collection_heartbeat.json}"
HEARTBEAT_LOCK_PATH="${JOBPULSE_COLLECTION_HEARTBEAT_LOCK_PATH:-$ROOT/logs/collection_heartbeat.lock}"
CYCLE_LOCK_PATH="${JOBPULSE_COLLECTION_CYCLE_LOCK_PATH:-$ROOT/state/run_collection_cycle.lock}"
HISTORY_FILE="${JOBPULSE_COLLECTION_HISTORY_FILE:-$ROOT/logs/collection_history.jsonl}"
HISTORY_LOCK_PATH="${JOBPULSE_COLLECTION_HISTORY_LOCK_PATH:-$ROOT/logs/collection_history.lock}"
TELEGRAM_ALERTS_SCRIPT="${JOBPULSE_TELEGRAM_ALERTS_SCRIPT:-$ROOT/scripts/send_telegram_alerts.py}"
PYTHON_BIN="${JOBPULSE_PYTHON_BIN:-python3}"

SEED_LIMIT="${SEED_LIMIT:-10}"
PROCESS_LIMIT="${PROCESS_LIMIT:-5}"
BACKLOG_LIMIT="${BACKLOG_LIMIT:-50}"

# ---- validated, bounded external-call timeouts ----
# See docs/PRODUCTION_RUNBOOK.md for the exact defaults/ranges and why
# they were chosen. STEP covers the 4 docker-exec collector operations
# (auth preflight, seed, process, reconcile); DB_QUERY covers the
# lightweight queue-count/reset queries; ALERT covers the best-effort
# telegram alert sender.
STEP_TIMEOUT_SECONDS="$(validate_duration_seconds "${JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS:-}" 1800 1 21600)"
DB_QUERY_TIMEOUT_SECONDS="$(validate_duration_seconds "${JOBPULSE_COLLECTION_DB_QUERY_TIMEOUT_SECONDS:-}" 30 1 300)"
ALERT_TIMEOUT_SECONDS="$(validate_duration_seconds "${JOBPULSE_COLLECTION_ALERT_TIMEOUT_SECONDS:-}" 60 1 600)"
KILL_AFTER_SECONDS="$(validate_duration_seconds "${JOBPULSE_COLLECTION_KILL_AFTER_SECONDS:-}" 10 1 60)"

# ---- host-vs-inner deadline ordering (dual-deadline design) ----
# STEP_TIMEOUT_SECONDS/KILL_AFTER_SECONDS above are the INNER deadline:
# they are passed straight through as `scripts.run_with_deadline`'s own
# `--seconds`/`--kill-after`, running INSIDE the api container. An
# earlier revision of this dual-deadline design used the EXACT SAME
# values for the HOST-side `run_with_timeout` wrapping the whole
# `docker compose exec` call -- meaning both deadlines could fire at
# essentially the same instant, giving the in-container runner no head
# start to do its own SIGTERM -> grace -> SIGKILL process-group cleanup
# before the host-side `timeout` kills/disconnects the `docker` CLI out
# from under it. Fixed: the HOST-side backstop is always the inner
# deadline PLUS the inner kill-after grace PLUS a fixed positive margin
# for docker-exec/interpreter startup and IPC overhead, so the
# in-container runner always gets the first, uncontested opportunity to
# terminate and reap its own process group. This holds by construction
# regardless of malformed input, because the addition below always
# operates on the ALREADY-VALIDATED (safely-defaulted-if-malformed)
# variables above, never on raw environment input directly.
HOST_BACKSTOP_MARGIN_SECONDS="$(validate_duration_seconds "${JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS:-}" 30 1 300)"
HOST_STEP_TIMEOUT_SECONDS=$((STEP_TIMEOUT_SECONDS + KILL_AFTER_SECONDS + HOST_BACKSTOP_MARGIN_SECONDS))
# Same rationale for the DB-query path: DB_QUERY_TIMEOUT_SECONDS is used
# as PostgreSQL's own server-side `statement_timeout` (see queue_count()/
# reset_recent_running() below); the HOST-side `run_with_timeout` around
# the `docker compose exec ... psql ...` call must be strictly larger,
# so the server gets the first opportunity to abort the query on its own
# terms before the host-side client is killed.
HOST_DB_QUERY_TIMEOUT_SECONDS=$((DB_QUERY_TIMEOUT_SECONDS + HOST_BACKSTOP_MARGIN_SECONDS))

# GNU coreutils `timeout` is required to bound every Docker/DB/alert
# command below. Resolved once, up front: if it's missing, every such
# call fails clearly (see run_with_timeout) instead of running unbounded.
TIMEOUT_BIN="$(command -v timeout 2>/dev/null || true)"

mkdir -p "$ROOT/logs" "$ROOT/state" "$(dirname "$HEARTBEAT_STATE_PATH")" "$(dirname "$CYCLE_LOCK_PATH")" 2>/dev/null || true

ts() {
  date -Is
}

log() {
  echo "$(ts) $1" >> "$LOG_FILE"
}

# run_with_timeout <duration_seconds> <kill_after_seconds> <cmd...>
# Runs <cmd...> under GNU coreutils `timeout`, bounding it to
# <duration_seconds> and escalating to SIGKILL after an additional
# <kill_after_seconds> grace period. Deliberately does NOT pass
# --foreground: without it, `timeout` puts <cmd...> in its own new
# process group and signals the WHOLE group on timeout, so any
# subprocess the command itself forks (as `docker` sometimes does) is
# killed too -- no child or grandchild process may remain running after a
# timeout.
#
# Sets TIMEOUT_LAST_CATEGORY to "" on a normal (bounded) run -- regardless
# of the wrapped command's own exit code -- "timeout" if the bound was
# hit (exit 124/137/143), or "timeout_utility_missing" if GNU `timeout`
# isn't installed. Callers check this AFTER checking the return code.
#
# Must be called directly in the current shell (redirect its own stdout
# to a file if needed), NEVER inside `$(...)` -- command substitution
# runs in a subshell, and TIMEOUT_LAST_CATEGORY set there would be
# invisible to the caller once the subshell exits. (Same rule as
# scripts/production_health_alert.sh's identically-named helper.)
TIMEOUT_LAST_CATEGORY=""

run_with_timeout() {
  local duration="$1" kill_after="$2"
  shift 2
  TIMEOUT_LAST_CATEGORY=""

  if [ -z "$TIMEOUT_BIN" ]; then
    TIMEOUT_LAST_CATEGORY="timeout_utility_missing"
    return 1
  fi

  "$TIMEOUT_BIN" -k "${kill_after}" "${duration}" "$@"
  local rc=$?

  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] || [ "$rc" -eq 143 ]; then
    TIMEOUT_LAST_CATEGORY="timeout"
  fi

  return "$rc"
}

heartbeat() {
  # heartbeat <op> [args...] -- every call is attached to this run's
  # RUN_ID and RUN_OWNER_PID and the shared state/lock paths above.
  # Returns the CLI's own exit code (0 on success, non-zero on
  # lock-busy-after-retries, stale-run rejection, or write failure) so
  # callers can treat a heartbeat persistence failure as the real failure
  # it is, per the wrapper exit-code contract. The CLI itself retries
  # transient lock contention internally (bounded, see
  # scripts/collection_heartbeat.py) before ever returning failure here.
  "$PYTHON_BIN" -m scripts.collection_heartbeat \
    --state-path "$HEARTBEAT_STATE_PATH" \
    --lock-path "$HEARTBEAT_LOCK_PATH" \
    "$@" --run-id "$RUN_ID" --owner-pid "$RUN_OWNER_PID"
}

# heartbeat_progress_or_die <stage> <message> [metrics_json]
# A progress-heartbeat write is no longer a best-effort warning: per the
# phase 2A reliability contract, a persistent progress-write failure must
# stop the cycle BEFORE the next collector stage runs, not silently
# continue with an unrecorded gap in the run's history. Attempts one
# best-effort `fail` write (its own success is not required) purely for
# operator visibility, then always exits 3.
heartbeat_progress_or_die() {
  local stage="$1" message="$2" metrics_json="${3:-}"
  local args=(progress --stage "$stage" --message "$message")
  if [ -n "$metrics_json" ]; then
    args+=(--metrics-json "$metrics_json")
  fi

  if heartbeat "${args[@]}" >> "$LOG_FILE" 2>&1; then
    return 0
  fi

  log "SAFE_CYCLE_HEARTBEAT_FAILURE: progress write failed at stage=${stage}; aborting cycle before the next step."
  heartbeat fail --stage "$stage" --message "Heartbeat progress persistence failed at stage=${stage}." \
    --error-category heartbeat_persistence_error >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: the follow-up fail-write also failed at stage=${stage}."
  append_history "failed" "Heartbeat progress persistence failed at stage=${stage}." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist history after a heartbeat failure at stage=${stage}."
  exit 3
}

# append_history <status> <message>
# Durable, append-only, ONE-JSON-OBJECT-PER-LINE record of this run's
# terminal outcome, independent of the heartbeat file's current
# (overwritable-by-a-future-run) snapshot. Field shape (status, message,
# started_at, finished_at, duration_seconds, pending_before, running_before,
# pending_after) intentionally matches the pre-existing schema
# app/admin_status.py's get_collection_performance() already parses
# (unmodified this phase) -- see tests/test_collection_cycle_wrapper.py
# for a regression test that runs the REAL parser against a file this
# function produces. Locked (flock, held only for this one append) and
# fsync'd before the lock is released, so concurrent appends (e.g. an
# admin-triggered manual cycle racing the cron one) can never interleave
# a partial line. Returns the underlying python process's exit code --
# callers MUST check it; a history-write failure must never be silently
# treated as durably recorded.
append_history() {
  local status="$1" message="$2"
  "$PYTHON_BIN" - "$HISTORY_FILE" "$HISTORY_LOCK_PATH" "$RUN_ID" "$status" "$message" "$CYCLE_STARTED_AT" \
    "${PENDING_COUNT:-}" "${RUNNING_COUNT:-}" "${PENDING_AFTER:-}" <<'PY'
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
run_id = sys.argv[3]
status = sys.argv[4]
message = sys.argv[5]
started_at = sys.argv[6]
pending_before_raw = sys.argv[7]
running_before_raw = sys.argv[8]
pending_after_raw = sys.argv[9]

now = datetime.now(timezone.utc).isoformat()

duration_seconds = None
try:
    start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finish_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    duration_seconds = round((finish_dt - start_dt).total_seconds(), 2)
except Exception:
    pass


def as_int_or_none(raw):
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


event = {
    "timestamp": now,
    "run_id": run_id,
    "status": status,
    "message": message,
    "started_at": started_at,
    "finished_at": now,
    "duration_seconds": duration_seconds,
    "pending_before": as_int_or_none(pending_before_raw),
    "running_before": as_int_or_none(running_before_raw),
    "pending_after": as_int_or_none(pending_after_raw),
}
line = json.dumps(event, sort_keys=True) + "\n"

path.parent.mkdir(parents=True, exist_ok=True)
lock_path.parent.mkdir(parents=True, exist_ok=True)

# Bounded, non-blocking retry loop -- NOT a plain blocking flock. A
# concurrent holder that never releases (e.g. a genuinely stuck prior
# invocation) must cause this to fail after a bounded wait, never hang
# the whole cycle indefinitely: every external/shared-resource wait in
# this script is bounded, and the history lock is a shared resource like
# any other.
MAX_ATTEMPTS = 25
DELAY_SECONDS = 0.2

with open(lock_path, "a+") as lock_file:
    acquired = False
    for attempt in range(MAX_ATTEMPTS):
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(DELAY_SECONDS)

    if not acquired:
        print("append_history: lock busy after bounded retries", file=sys.stderr)
        sys.exit(1)

    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
PY
}

run_telegram_alerts() {
  # Best-effort: logged, but its own exit code must never override the
  # exit code the caller has already decided on for the actual collector
  # failure that triggered this call. Bounded like every other external
  # call in this script.
  run_with_timeout "$ALERT_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
    "$PYTHON_BIN" "$TELEGRAM_ALERTS_SCRIPT" >> "$ROOT/logs/telegram_alerts.log" 2>&1
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    log "ALERT_SENDER_FAILED rc=${rc} category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit} (not masking the collector failure that triggered this call)"
  fi
}

# queue_count <status> <out_var_name>
# Sets $<out_var_name> to the numeric count on success and
# QUEUE_COUNT_LAST_CATEGORY to "". On ANY failure -- non-zero exit,
# timeout, or output that is not a clean non-negative integer -- returns
# 1 and sets QUEUE_COUNT_LAST_CATEGORY to a stable category, WITHOUT ever
# writing to the out variable. Callers must never fall back to treating a
# failed read as "0" -- see docs/PRODUCTION_RUNBOOK.md for the exact
# defect this replaces (a `docker ... | tr -d ...` pipeline whose exit
# status was always `tr`'s, which almost always succeeds regardless of
# whether `docker`/`psql` actually produced a real count).
# Must be called directly (never via $(...)) for the same subshell-scoping
# reason as run_with_timeout.
QUEUE_COUNT_LAST_CATEGORY=""

queue_count() {
  local status="$1"
  local __out_var="$2"
  local out_file
  out_file="$(mktemp)" || { QUEUE_COUNT_LAST_CATEGORY="temp_file_failed"; return 1; }

  # `SET statement_timeout` is the FIRST, server-visible deadline --
  # PostgreSQL itself aborts a query that runs longer than
  # DB_QUERY_TIMEOUT_SECONDS, independent of the host-side psql process.
  # The HOST-side `run_with_timeout` below is a strictly larger backstop
  # (HOST_DB_QUERY_TIMEOUT_SECONDS = DB_QUERY_TIMEOUT_SECONDS + margin),
  # so the server always gets the first opportunity to abort the query on
  # its own terms before the host-side client is killed.
  run_with_timeout "$HOST_DB_QUERY_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
    docker compose -f "$COMPOSE_FILE" exec -T db \
    psql -U jobpulse_user -d jobpulse -tA -c "
      SET statement_timeout = '${DB_QUERY_TIMEOUT_SECONDS}s';
      SELECT COUNT(*)
      FROM job_search_demand_queue
      WHERE status = '${status}';
    " >"$out_file" 2>>"$LOG_FILE"
  local rc=$?

  # Only the LAST non-empty line matters: `-tA` (tuples-only, unaligned)
  # suppresses column headers/footers and, per psql's documented
  # behavior, command-completion tags for non-SELECT statements too --
  # but this is not verifiable against a real PostgreSQL binary in this
  # environment (see docs/PRODUCTION_RUNBOOK.md), so parsing only the
  # final line is a deliberate defensive choice: it produces the correct
  # result whether or not the preceding `SET statement_timeout` line
  # happens to print anything.
  #
  # Trims only LEADING and TRAILING whitespace on that line -- never
  # internal whitespace. `tr -d '[:space:]'` (the original defect this
  # replaces) deletes whitespace anywhere in the string, so malformed
  # output like "1 5" would silently become the valid-looking "15"
  # instead of being rejected by the ^[0-9]+$ check below.
  local out
  out="$(tail -n 1 "$out_file" 2>/dev/null)"
  out="${out#"${out%%[![:space:]]*}"}"
  out="${out%"${out##*[![:space:]]}"}"
  rm -f "$out_file"

  if [ "$rc" -ne 0 ]; then
    QUEUE_COUNT_LAST_CATEGORY="${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
    return 1
  fi

  if ! [[ "$out" =~ ^[0-9]+$ ]]; then
    QUEUE_COUNT_LAST_CATEGORY="malformed_output"
    return 1
  fi

  QUEUE_COUNT_LAST_CATEGORY=""
  printf -v "$__out_var" '%s' "$out"
  return 0
}

reset_recent_running() {
  local since_sql="$1"
  local reason="$2"

  log "Resetting recent running queue rows after failure. since=${since_sql}, reason=${reason}"

  # Same server-first statement_timeout-then-host-backstop rationale as
  # queue_count() above.
  run_with_timeout "$HOST_DB_QUERY_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
    docker compose -f "$COMPOSE_FILE" exec -T db \
    psql -U jobpulse_user -d jobpulse -c "
      SET statement_timeout = '${DB_QUERY_TIMEOUT_SECONDS}s';
      UPDATE job_search_demand_queue
      SET
        status = 'pending',
        locked_at = NULL,
        fail_count = COALESCE(fail_count, 0) + 1,
        last_error = CONCAT_WS(' | ', NULLIF(last_error, ''), '${reason}')
      WHERE status = 'running'
        AND (
          locked_at IS NULL
          OR locked_at >= TIMESTAMP '${since_sql}'
        )
      RETURNING id, raw_query, status, locked_at;
    " >> "$LOG_FILE" 2>&1
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    log "WARNING reset_recent_running_failed category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
  fi
}

# read_process_summary <summary_path>
# Sets PROCESS_SUMMARY_OK (0/1), PROCESS_SUMMARY_CATEGORY,
# PROCESS_SUMMARY_OUTCOME, PROCESS_SUMMARY_IS_SUCCESS (0/1),
# PROCESS_SUMMARY_USEFUL (0/1), and PROCESS_SUMMARY_METRICS_JSON from the
# structured summary scripts/process_search_demand_queue.py wrote INSIDE
# the api container to the shared logs volume (docker-compose.prod.yml
# bind-mounts ./logs:/app/logs on the api service, so the same file is
# visible here on the host at $ROOT/logs/...). Uses the same
# NUL-delimited-fields + process-substitution `read` loop idiom as
# scripts/production_health_alert.sh's load_state() (never `$(...)` for
# the python call itself, and never eval) so a value cannot be
# misinterpreted as shell syntax and so the fields survive the process
# substitution's own subshell correctly.
#
# PROCESS_SUMMARY_IS_SUCCESS is computed from
# scripts.process_summary.SUCCESS_OUTCOMES -- the SAME set the summary's
# own writer/reader use -- rather than hardcoding "partial_failure" as
# the only non-success value here: any outcome not in that set (today
# only "partial_failure" and "failed" -- "failed" should already have
# been caught by the docker-exec step's own non-zero exit, but this stays
# correct even if that assumption is ever wrong) is treated as a
# terminal FAILURE by the caller, never as success.
read_process_summary() {
  local summary_path="$1"

  PROCESS_SUMMARY_OK="0"
  PROCESS_SUMMARY_CATEGORY="result_read_error"
  PROCESS_SUMMARY_OUTCOME=""
  PROCESS_SUMMARY_IS_SUCCESS="0"
  PROCESS_SUMMARY_USEFUL="0"
  PROCESS_SUMMARY_METRICS_JSON="{}"

  local -a fields=()
  local field
  while IFS= read -r -d '' field; do
    fields+=("$field")
  done < <("$PYTHON_BIN" - "$summary_path" <<'PY' 2>>"$LOG_FILE"
import json
import sys

from scripts.process_summary import SUCCESS_OUTCOMES, SummaryReadError, read_summary

out = sys.stdout.buffer


def emit(*parts):
    for part in parts:
        out.write(str(part).encode("utf-8", errors="replace"))
        out.write(b"\x00")


try:
    summary = read_summary(sys.argv[1])
    metrics = dict(summary.aggregate_collector_metrics)
    metrics["total_queries"] = summary.total_queries
    metrics["successful_queries"] = summary.successful_queries
    metrics["failed_queries"] = summary.failed_queries
    metrics["useful_queries"] = summary.useful_queries
    metrics["zero_yield_queries"] = summary.zero_yield_queries
    metrics["partial_failure"] = summary.partial_failure
    # "useful" (drives --useful-ingestion / last_useful_ingestion_at) is
    # computed from the PROVEN insert count directly, not from the
    # outcome string. This matters specifically for partial_failure: a
    # batch can have rows_inserted > 0 from its successful queries even
    # though the overall outcome is partial_failure (not useful_success)
    # -- last_useful_ingestion_at must still be allowed to advance for
    # that genuinely-proven insert, without the cycle itself becoming a
    # reported success.
    useful = summary.aggregate_collector_metrics.get("rows_inserted", 0) > 0
    emit(
        "1", "", summary.outcome,
        "1" if summary.outcome in SUCCESS_OUTCOMES else "0",
        "1" if useful else "0",
        json.dumps(metrics),
    )
except SummaryReadError as exc:
    emit("0", exc.category, "", "0", "0", "{}")
except Exception:
    emit("0", "result_read_error", "", "0", "0", "{}")
out.flush()
PY
  )

  PROCESS_SUMMARY_OK="${fields[0]:-0}"
  PROCESS_SUMMARY_CATEGORY="${fields[1]:-result_read_error}"
  PROCESS_SUMMARY_OUTCOME="${fields[2]:-}"
  PROCESS_SUMMARY_IS_SUCCESS="${fields[3]:-0}"
  PROCESS_SUMMARY_USEFUL="${fields[4]:-0}"
  PROCESS_SUMMARY_METRICS_JSON="${fields[5]:-\{\}}"
}

# ---- top-level, non-blocking cycle lock ----
# Tied to the file descriptor: bash closes it on any exit path (normal,
# error, or signal), which always releases the flock. Acquired BEFORE any
# heartbeat write or collector operation, per the phase 2A contract -- a
# lost race here creates no run_id and touches no heartbeat state at all,
# so it can never overwrite the active run's progress.
exec {CYCLE_LOCK_FD}>"$CYCLE_LOCK_PATH" || {
  log "ERROR cycle_lock_open_failed path=${CYCLE_LOCK_PATH}"
  exit 1
}

if ! flock -n "$CYCLE_LOCK_FD"; then
  log "SAFE_CYCLE_ALREADY_RUNNING lock=${CYCLE_LOCK_PATH}"
  echo "run_collection_cycle_already_running"
  exit 0
fi

# RUN_ID/RUN_OWNER_PID are generated only AFTER the lock is held -- a
# lock-busy invocation (above) never generates either, never calls
# heartbeat(), and never touches heartbeat state. RUN_OWNER_PID ("$$") is
# this shell process's OWN stable PID for the entire run, passed to every
# heartbeat call as `--owner-pid`; it is distinct from the `writer_pid`
# each short-lived `python3 -m scripts.collection_heartbeat` helper
# invocation records for itself (see scripts/collection_heartbeat.py).
# NEITHER pid is used as a cross-container liveness signal -- the
# collector subprocesses this script triggers via `docker compose exec`
# run in a different PID namespace entirely.
RUN_ID="$("$PYTHON_BIN" -c 'import uuid; print(uuid.uuid4())')"
RUN_OWNER_PID="$$"
CYCLE_STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"

log ""
log "===== SAFE COLLECTION CYCLE run_id=${RUN_ID} owner_pid=${RUN_OWNER_PID} ====="
log "DEADLINE_CONFIG inner_step_timeout=${STEP_TIMEOUT_SECONDS} inner_kill_after=${KILL_AFTER_SECONDS} host_step_timeout=${HOST_STEP_TIMEOUT_SECONDS} db_query_timeout=${DB_QUERY_TIMEOUT_SECONDS} host_db_query_timeout=${HOST_DB_QUERY_TIMEOUT_SECONDS} margin=${HOST_BACKSTOP_MARGIN_SECONDS}"

if ! heartbeat start --stage cycle_started --message "Collection cycle started. Running LinkedIn auth preflight." >> "$LOG_FILE" 2>&1; then
  log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist start heartbeat for run_id=${RUN_ID}."
  exit 3
fi

log "Running LinkedIn auth preflight..."

if ! run_with_timeout "$HOST_STEP_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
  docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.run_with_deadline --seconds "$STEP_TIMEOUT_SECONDS" --kill-after "$KILL_AFTER_SECONDS" -- \
  python -m scripts.linkedin_auth_preflight >> "$LOG_FILE" 2>&1; then
  AUTH_CATEGORY="${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
  log "SAFE_CYCLE_ABORTED: LinkedIn auth preflight failed (category=${AUTH_CATEGORY}). Skipping seed/process/reconcile."
  heartbeat fail --status aborted_auth --stage auth_preflight \
    --message "LinkedIn auth preflight failed. Collection skipped." \
    --error-category "${AUTH_CATEGORY}" >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist aborted_auth heartbeat."
  append_history "aborted_auth" "LinkedIn auth preflight failed. Collection skipped." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist aborted_auth history."
  run_telegram_alerts
  exit 2
fi

log "LinkedIn auth preflight passed."
heartbeat_progress_or_die "auth_preflight_passed" "LinkedIn auth preflight passed."

if ! queue_count pending PENDING_COUNT; then
  log "SAFE_CYCLE_FAILED: queue_count(pending) failed category=${QUEUE_COUNT_LAST_CATEGORY}."
  heartbeat fail --stage queue_count_pending --message "Could not determine pending queue count." \
    --error-category "${QUEUE_COUNT_LAST_CATEGORY}" >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist queue-count-failure heartbeat."
  append_history "failed" "Could not determine pending queue count (category=${QUEUE_COUNT_LAST_CATEGORY})." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist queue-count-failure history."
  run_telegram_alerts
  exit 7
fi

if ! queue_count running RUNNING_COUNT; then
  log "SAFE_CYCLE_FAILED: queue_count(running) failed category=${QUEUE_COUNT_LAST_CATEGORY}."
  heartbeat fail --stage queue_count_running --message "Could not determine running queue count." \
    --error-category "${QUEUE_COUNT_LAST_CATEGORY}" \
    --metrics-json "{\"pending_before\":${PENDING_COUNT}}" >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist queue-count-failure heartbeat."
  append_history "failed" "Could not determine running queue count (category=${QUEUE_COUNT_LAST_CATEGORY})." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist queue-count-failure history."
  run_telegram_alerts
  exit 7
fi

heartbeat_progress_or_die "backlog_checked" "LinkedIn auth passed. Checking backlog." \
  "{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}}"

log "Queue status before cycle: pending=${PENDING_COUNT}, running=${RUNNING_COUNT}, backlog_limit=${BACKLOG_LIMIT}, process_limit=${PROCESS_LIMIT}"

if [ "$RUNNING_COUNT" -gt 0 ]; then
  log "SAFE_CYCLE_SKIPPED: ${RUNNING_COUNT} queue tasks are still running. Skipping this cycle."
  # Not a failure: the wrapper correctly chose to do no work this tick
  # because a previous processing step is still in flight. Exit 0 is
  # correct here -- no error occurred -- but ONLY if both the terminal
  # heartbeat AND the history record actually persist; either failing
  # still forces a non-zero exit, per "no cycle may exit zero when its
  # terminal state was not durably persisted."
  if ! heartbeat finish --status skipped_running --stage backlog_check \
    --message "${RUNNING_COUNT} queue tasks are still running. Cycle skipped." \
    --metrics-json "{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}}" \
    >> "$LOG_FILE" 2>&1; then
    log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist skipped_running heartbeat for run_id=${RUN_ID}."
    append_history "failed" "Cycle correctly skipped but the skipped_running heartbeat could not be persisted." \
      || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist history after a heartbeat failure."
    exit 3
  fi
  if ! append_history "skipped_running" "${RUNNING_COUNT} queue tasks are still running. Cycle skipped."; then
    log "SAFE_CYCLE_HISTORY_FAILURE: could not persist skipped_running history for run_id=${RUN_ID}."
    exit 3
  fi
  exit 0
fi

if [ "$PENDING_COUNT" -ge "$BACKLOG_LIMIT" ]; then
  log "Backlog is high. Skipping seed and processing existing pending queue only."
  heartbeat_progress_or_die "seed_skipped_backlog_high" "Backlog is high (${PENDING_COUNT} >= ${BACKLOG_LIMIT}). Skipping seed."
else
  log "Backlog is acceptable. Seeding priority coverage tasks."
  heartbeat_progress_or_die "seeding" "Seeding priority coverage tasks."

  if ! run_with_timeout "$HOST_STEP_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
    docker compose -f "$COMPOSE_FILE" exec -T api \
    python -m scripts.run_with_deadline --seconds "$STEP_TIMEOUT_SECONDS" --kill-after "$KILL_AFTER_SECONDS" -- \
    python -m scripts.seed_priority_coverage_queue --limit "$SEED_LIMIT" --retry-after-hours 24 >> "$LOG_FILE" 2>&1; then
    SEED_CATEGORY="${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
    log "SAFE_CYCLE_FAILED: seed_priority_coverage_queue failed (category=${SEED_CATEGORY})."
    heartbeat fail --stage seed --message "Seed priority coverage queue failed." \
      --error-category "${SEED_CATEGORY}" \
      --metrics-json "{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}}" \
      >> "$LOG_FILE" 2>&1 \
      || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist seed-failure heartbeat."
    append_history "failed" "Seed priority coverage queue failed." \
      || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist seed-failure history."
    run_telegram_alerts
    exit 4
  fi

  heartbeat_progress_or_die "seed_completed" "Seed priority coverage queue completed."
fi

log "Processing demand queue."
heartbeat_progress_or_die "demand_queue_processing_started" "Processing demand queue."

PROCESS_STARTED_AT_SQL="$(date -u '+%Y-%m-%d %H:%M:%S')"

# scripts/process_search_demand_queue.py writes a versioned, atomic
# structured summary INSIDE the api container to this path, which is
# visible on the host at the same relative location under
# $ROOT/logs/... because docker-compose.prod.yml bind-mounts
# ./logs:/app/logs on the api service (confirmed directly from that
# compose file -- not assumed). See scripts/process_summary.py and
# docs/PRODUCTION_RUNBOOK.md.
PROCESS_SUMMARY_HOST_PATH="$ROOT/logs/.process_summary_${RUN_ID}.json"
PROCESS_SUMMARY_CONTAINER_PATH="/app/logs/.process_summary_${RUN_ID}.json"
rm -f "$PROCESS_SUMMARY_HOST_PATH" 2>/dev/null || true

if ! run_with_timeout "$HOST_STEP_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
  docker compose -f "$COMPOSE_FILE" exec -T \
  -e "JOBPULSE_PROCESS_SUMMARY_RESULT_PATH=${PROCESS_SUMMARY_CONTAINER_PATH}" \
  api python -m scripts.run_with_deadline --seconds "$STEP_TIMEOUT_SECONDS" --kill-after "$KILL_AFTER_SECONDS" -- \
  python -m scripts.process_search_demand_queue --limit "$PROCESS_LIMIT" --workers 1 --skip-company-enrichment --skip-post-processing >> "$LOG_FILE" 2>&1; then
  PROCESS_CATEGORY="${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
  log "SAFE_CYCLE_FAILED: process_search_demand_queue failed (category=${PROCESS_CATEGORY})."
  reset_recent_running "$PROCESS_STARTED_AT_SQL" "reset after process_search_demand_queue failed"
  if ! queue_count pending PENDING_AFTER; then
    log "WARNING queue_count(pending) after process failure also failed category=${QUEUE_COUNT_LAST_CATEGORY}; omitting pending_after from this heartbeat."
    PENDING_AFTER=""
  fi
  METRICS_JSON="{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}"
  [ -n "${PENDING_AFTER:-}" ] && METRICS_JSON="${METRICS_JSON},\"pending_after\":${PENDING_AFTER}"
  METRICS_JSON="${METRICS_JSON}}"
  heartbeat fail --stage process --message "Process search demand queue failed; recent running rows were reset." \
    --error-category "${PROCESS_CATEGORY}" \
    --metrics-json "${METRICS_JSON}" >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist process-failure heartbeat."
  append_history "failed" "Process search demand queue failed; recent running rows were reset." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist process-failure history."
  rm -f "$PROCESS_SUMMARY_HOST_PATH" 2>/dev/null || true
  run_telegram_alerts
  exit 5
fi

# The docker-exec step itself exited 0 -- but per the phase 2A adversarial
# pass, success is NEVER inferred from a bare exit code at any layer of
# this pipeline. Read and strictly validate the structured summary the
# container wrote to the shared logs volume; a missing or malformed
# summary here is itself a process-step failure (exit 5), not a success.
read_process_summary "$PROCESS_SUMMARY_HOST_PATH"
rm -f "$PROCESS_SUMMARY_HOST_PATH" 2>/dev/null || true

if [ "$PROCESS_SUMMARY_OK" != "1" ]; then
  log "SAFE_CYCLE_FAILED: process_search_demand_queue exited 0 but its summary is unusable (category=${PROCESS_SUMMARY_CATEGORY})."
  heartbeat fail --stage process_summary \
    --message "Process summary missing or malformed after an otherwise-successful process step." \
    --error-category "${PROCESS_SUMMARY_CATEGORY}" \
    --metrics-json "{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}}" \
    >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist process-summary-failure heartbeat."
  append_history "failed" "Process summary missing or malformed after an otherwise-successful process step." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist process-summary-failure history."
  run_telegram_alerts
  exit 5
fi

log "Demand queue processing completed. final_outcome=${PROCESS_SUMMARY_OUTCOME}"
heartbeat_progress_or_die "demand_queue_processing_completed" \
  "Demand queue processing completed (outcome=${PROCESS_SUMMARY_OUTCOME})." \
  "$PROCESS_SUMMARY_METRICS_JSON"

log "Reconciling priority coverage."
heartbeat_progress_or_die "reconciliation_started" "Reconciling priority coverage."

if ! run_with_timeout "$HOST_STEP_TIMEOUT_SECONDS" "$KILL_AFTER_SECONDS" \
  docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.run_with_deadline --seconds "$STEP_TIMEOUT_SECONDS" --kill-after "$KILL_AFTER_SECONDS" -- \
  python -m scripts.reconcile_priority_coverage >> "$LOG_FILE" 2>&1; then
  RECONCILE_CATEGORY="${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
  log "SAFE_CYCLE_FAILED: reconcile_priority_coverage failed (category=${RECONCILE_CATEGORY})."
  heartbeat fail --stage reconcile --message "Reconcile priority coverage failed." \
    --error-category "${RECONCILE_CATEGORY}" \
    --metrics-json "{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}}" \
    >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist reconcile-failure heartbeat."
  append_history "failed" "Reconcile priority coverage failed." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist reconcile-failure history."
  run_telegram_alerts
  exit 6
fi

if ! queue_count pending PENDING_AFTER; then
  log "SAFE_CYCLE_FAILED: queue_count(pending) after cycle failed category=${QUEUE_COUNT_LAST_CATEGORY}."
  heartbeat fail --stage queue_count_pending_after --message "Could not determine post-cycle pending queue count." \
    --error-category "${QUEUE_COUNT_LAST_CATEGORY}" \
    --metrics-json "{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT}}" \
    >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist queue-count-failure heartbeat."
  append_history "failed" "Could not determine post-cycle pending queue count (category=${QUEUE_COUNT_LAST_CATEGORY})." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist queue-count-failure history."
  run_telegram_alerts
  exit 7
fi

log "Queue status after cycle: pending=${PENDING_AFTER}"
log "SAFE_COLLECTION_CYCLE_DONE run_id=${RUN_ID} final_outcome=${PROCESS_SUMMARY_OUTCOME}"

# Cycle-level classification is truthful, sourced from
# scripts/process_summary.py's classify_cycle_outcome() (computed inside
# the container from linkedin_plan_collect's own per-query, per-UPSERT
# evidence -- never inferred here). `useful_ingestion` (PROCESS_SUMMARY_USEFUL,
# set in read_process_summary() above) is computed directly from
# aggregate_collector_metrics.rows_inserted > 0 -- NOT from
# outcome == "useful_success" -- so it can still correctly advance
# last_useful_ingestion_at for a genuinely-proven insert even inside an
# otherwise partial_failure batch, without that batch being reported as
# an overall success. The original successful-query and inserted-row
# metrics (from the process summary) are always merged into the terminal
# heartbeat's current_metrics alongside the queue pending/running counts
# -- never dropped, in either the success or the failure branch below.
USEFUL_INGESTION_ARGS=()
if [ "$PROCESS_SUMMARY_USEFUL" = "1" ]; then
  USEFUL_INGESTION_ARGS=(--useful-ingestion)
fi

QUEUE_METRICS_JSON="{\"pending_before\":${PENDING_COUNT},\"running_before\":${RUNNING_COUNT},\"pending_after\":${PENDING_AFTER}}"
FINAL_METRICS_JSON="$("$PYTHON_BIN" -c "
import json, sys
merged = json.loads(sys.argv[1])
merged.update(json.loads(sys.argv[2]))
print(json.dumps(merged))
" "$PROCESS_SUMMARY_METRICS_JSON" "$QUEUE_METRICS_JSON")"

# partial_failure (or, defensively, any outcome scripts.process_summary
# does not classify as a success) is NEVER reported as a successful
# terminal cycle just because the docker-exec step itself exited 0 and
# some real work happened. This is the corrected contract from the
# adversarial review: an earlier revision called `heartbeat finish
# --status success` unconditionally for every structurally-valid summary,
# including final_outcome=partial_failure -- which advanced
# last_success_at, recorded history.status=success, and made
# get_collection_performance() / the existing admin alert builder both
# blind to the incident (build_alerts() only inspects last_status, never
# final_outcome). That is fixed here: status=failed,
# error_category=partial_failure, exit code 8 -- distinct from every
# other failure branch above, all of which are step-level infrastructure
# failures rather than "the steps ran fine but the batch itself was only
# partially successful."
if [ "$PROCESS_SUMMARY_IS_SUCCESS" != "1" ]; then
  log "SAFE_CYCLE_PARTIAL_FAILURE: process summary outcome=${PROCESS_SUMMARY_OUTCOME} is not a success outcome."
  heartbeat fail --stage cycle_finished \
    --message "Collection cycle completed with outcome=${PROCESS_SUMMARY_OUTCOME}; not all queries succeeded." \
    --error-category "${PROCESS_SUMMARY_OUTCOME:-partial_failure}" \
    --final-outcome "${PROCESS_SUMMARY_OUTCOME}" \
    --metrics-json "${FINAL_METRICS_JSON}" \
    "${USEFUL_INGESTION_ARGS[@]}" \
    >> "$LOG_FILE" 2>&1 \
    || log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist partial-failure heartbeat for run_id=${RUN_ID}."
  # History status is "failed", not "partial_failure": app/admin_status.py's
  # unmodified get_collection_performance() only recognizes "success" for
  # its success_count and treats every other status as non-success for
  # avg_drain_per_success purposes -- "failed" is what makes the EXISTING,
  # unmodified reader correctly exclude this cycle from success_count
  # without any phase-2B rewrite. The more precise "partial_failure"
  # label lives in the heartbeat's final_outcome field for any richer
  # reader.
  append_history "failed" "Collection cycle completed with outcome=${PROCESS_SUMMARY_OUTCOME}; not all queries succeeded." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist partial-failure history."
  run_telegram_alerts
  exit 8
fi

if ! heartbeat finish --status success --stage cycle_finished \
  --message "Collection cycle completed successfully (outcome=${PROCESS_SUMMARY_OUTCOME})." \
  --final-outcome "${PROCESS_SUMMARY_OUTCOME}" \
  --metrics-json "${FINAL_METRICS_JSON}" \
  "${USEFUL_INGESTION_ARGS[@]}" \
  >> "$LOG_FILE" 2>&1; then
  log "SAFE_CYCLE_HEARTBEAT_FAILURE: could not persist success heartbeat for run_id=${RUN_ID}."
  append_history "failed" "Cycle work completed but the success heartbeat could not be persisted." \
    || log "SAFE_CYCLE_HISTORY_FAILURE: could not persist history after a heartbeat failure."
  exit 3
fi

if ! append_history "success" "Collection cycle completed successfully (outcome=${PROCESS_SUMMARY_OUTCOME})."; then
  log "SAFE_CYCLE_HISTORY_FAILURE: could not persist success history for run_id=${RUN_ID}."
  exit 3
fi

exit 0
