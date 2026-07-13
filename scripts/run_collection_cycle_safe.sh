#!/usr/bin/env bash
set -u

cd /opt/jobpulse

if [ -f /opt/jobpulse/.collection.env ]; then
  set -a
  . /opt/jobpulse/.collection.env
  set +a
fi

COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"
LOG_FILE="/opt/jobpulse/logs/collection_cycle.log"
HEARTBEAT_FILE="/opt/jobpulse/logs/collection_heartbeat.json"

SEED_LIMIT="${SEED_LIMIT:-10}"
PROCESS_LIMIT="${PROCESS_LIMIT:-5}"
BACKLOG_LIMIT="${BACKLOG_LIMIT:-50}"

mkdir -p /opt/jobpulse/logs

log() {
  echo "$1" >> "$LOG_FILE"
}

write_heartbeat() {
  local status="$1"
  local message="$2"
  local pending_before="${3:-}"
  local running_before="${4:-}"
  local pending_after="${5:-}"

  python3 - "$HEARTBEAT_FILE" "$status" "$message" "$pending_before" "$running_before" "$pending_after" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
status = sys.argv[2]
message = sys.argv[3]
pending_before = sys.argv[4]
running_before = sys.argv[5]
pending_after = sys.argv[6]

now = datetime.now(timezone.utc).isoformat()

try:
    data = json.loads(path.read_text()) if path.exists() else {}
except Exception:
    data = {}

data["updated_at"] = now
data["last_status"] = status
data["last_message"] = message

if status == "running":
    data["last_started_at"] = now
else:
    data["last_finished_at"] = now

if status == "success":
    data["last_success_at"] = now

if pending_before != "":
    data["pending_before"] = int(pending_before)

if running_before != "":
    data["running_before"] = int(running_before)

if pending_after != "":
    data["pending_after"] = int(pending_after)

path.write_text(json.dumps(data, indent=2, sort_keys=True))

if status != "running":
    history_path = path.with_name("collection_history.jsonl")

    started_at = data.get("last_started_at")
    finished_at = data.get("last_finished_at") or now

    duration_seconds = None
    try:
        start_dt = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        duration_seconds = round((finish_dt - start_dt).total_seconds(), 2)
    except Exception:
        pass

    event = {
        "timestamp": now,
        "status": status,
        "message": message,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "pending_before": int(pending_before) if pending_before != "" else None,
        "running_before": int(running_before) if running_before != "" else None,
        "pending_after": int(pending_after) if pending_after != "" else None,
    }

    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")

PY
}

queue_count() {
  local status="$1"

  docker compose -f "$COMPOSE_FILE" exec -T db \
    psql -U jobpulse_user -d jobpulse -tA -c "
      SELECT COUNT(*)
      FROM job_search_demand_queue
      WHERE status = '${status}';
    " 2>/dev/null | tr -d '[:space:]'
}

reset_recent_running() {
  local since_sql="$1"
  local reason="$2"

  log "Resetting recent running queue rows after failure. since=${since_sql}, reason=${reason}"

  docker compose -f "$COMPOSE_FILE" exec -T db \
    psql -U jobpulse_user -d jobpulse -c "
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
    " >> "$LOG_FILE" 2>&1 || true
}

log ""
log "===== SAFE COLLECTION CYCLE $(date -Is) ====="
write_heartbeat "running" "Collection cycle started. Running LinkedIn auth preflight."

log "Running LinkedIn auth preflight..."

if ! docker compose -f "$COMPOSE_FILE" exec -T api python -m scripts.linkedin_auth_preflight >> "$LOG_FILE" 2>&1; then
  log "SAFE_CYCLE_ABORTED: LinkedIn auth preflight failed. Skipping seed/process/reconcile."
  write_heartbeat "aborted_auth" "LinkedIn auth preflight failed. Collection skipped."
  python3 scripts/send_telegram_alerts.py >> /opt/jobpulse/logs/telegram_alerts.log 2>&1 || true
  exit 0
fi

log "LinkedIn auth preflight passed."

PENDING_COUNT="$(queue_count pending)"
RUNNING_COUNT="$(queue_count running)"

PENDING_COUNT="${PENDING_COUNT:-0}"
RUNNING_COUNT="${RUNNING_COUNT:-0}"

write_heartbeat "running" "LinkedIn auth passed. Checking backlog." "$PENDING_COUNT" "$RUNNING_COUNT"

log "Queue status before cycle: pending=${PENDING_COUNT}, running=${RUNNING_COUNT}, backlog_limit=${BACKLOG_LIMIT}, process_limit=${PROCESS_LIMIT}"

if [ "$RUNNING_COUNT" -gt 0 ]; then
  log "SAFE_CYCLE_ABORTED: ${RUNNING_COUNT} queue tasks are still running. Skipping this cycle."
  write_heartbeat "skipped_running" "${RUNNING_COUNT} queue tasks are still running. Cycle skipped." "$PENDING_COUNT" "$RUNNING_COUNT"
  exit 0
fi

if [ "$PENDING_COUNT" -ge "$BACKLOG_LIMIT" ]; then
  log "Backlog is high. Skipping seed and processing existing pending queue only."
else
  log "Backlog is acceptable. Seeding priority coverage tasks."

  if ! docker compose -f "$COMPOSE_FILE" exec -T api \
    python -m scripts.seed_priority_coverage_queue --limit "$SEED_LIMIT" --retry-after-hours 24 >> "$LOG_FILE" 2>&1; then
    log "SAFE_CYCLE_FAILED: seed_priority_coverage_queue failed."
    write_heartbeat "failed" "Seed priority coverage queue failed." "$PENDING_COUNT" "$RUNNING_COUNT"
    python3 scripts/send_telegram_alerts.py >> /opt/jobpulse/logs/telegram_alerts.log 2>&1 || true
    exit 0
  fi
fi

log "Processing demand queue."

PROCESS_STARTED_AT_SQL="$(date -u '+%Y-%m-%d %H:%M:%S')"

if ! docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.process_search_demand_queue --limit "$PROCESS_LIMIT" --workers 1 --skip-company-enrichment --skip-post-processing >> "$LOG_FILE" 2>&1; then
  log "SAFE_CYCLE_FAILED: process_search_demand_queue failed."
  reset_recent_running "$PROCESS_STARTED_AT_SQL" "reset after process_search_demand_queue failed"
  PENDING_AFTER="$(queue_count pending)"
  PENDING_AFTER="${PENDING_AFTER:-0}"
  write_heartbeat "failed" "Process search demand queue failed; recent running rows were reset." "$PENDING_COUNT" "$RUNNING_COUNT" "$PENDING_AFTER"
  python3 scripts/send_telegram_alerts.py >> /opt/jobpulse/logs/telegram_alerts.log 2>&1 || true
  exit 0
fi

log "Reconciling priority coverage."

if ! docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.reconcile_priority_coverage >> "$LOG_FILE" 2>&1; then
  log "SAFE_CYCLE_FAILED: reconcile_priority_coverage failed."
  write_heartbeat "failed" "Reconcile priority coverage failed." "$PENDING_COUNT" "$RUNNING_COUNT"
  python3 scripts/send_telegram_alerts.py >> /opt/jobpulse/logs/telegram_alerts.log 2>&1 || true
  exit 0
fi

PENDING_AFTER="$(queue_count pending)"
PENDING_AFTER="${PENDING_AFTER:-0}"

log "Queue status after cycle: pending=${PENDING_AFTER}"
log "SAFE_COLLECTION_CYCLE_DONE $(date -Is)"

write_heartbeat "success" "Collection cycle completed successfully." "$PENDING_COUNT" "$RUNNING_COUNT" "$PENDING_AFTER"
