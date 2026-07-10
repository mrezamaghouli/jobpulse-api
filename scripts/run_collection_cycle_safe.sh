#!/usr/bin/env bash
set -u

cd /opt/jobpulse

COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"
LOG_FILE="/opt/jobpulse/logs/collection_cycle.log"

SEED_LIMIT="${SEED_LIMIT:-10}"
PROCESS_LIMIT="${PROCESS_LIMIT:-10}"
BACKLOG_LIMIT="${BACKLOG_LIMIT:-50}"

mkdir -p /opt/jobpulse/logs

log() {
  echo "$1" >> "$LOG_FILE"
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

log ""
log "===== SAFE COLLECTION CYCLE $(date -Is) ====="
log "Running LinkedIn auth preflight..."

if ! docker compose -f "$COMPOSE_FILE" exec -T api python -m scripts.linkedin_auth_preflight >> "$LOG_FILE" 2>&1; then
  log "SAFE_CYCLE_ABORTED: LinkedIn auth preflight failed. Skipping seed/process/reconcile."

  python3 scripts/send_telegram_alerts.py >> /opt/jobpulse/logs/telegram_alerts.log 2>&1 || true

  exit 0
fi

log "LinkedIn auth preflight passed."

PENDING_COUNT="$(queue_count pending)"
RUNNING_COUNT="$(queue_count running)"

PENDING_COUNT="${PENDING_COUNT:-0}"
RUNNING_COUNT="${RUNNING_COUNT:-0}"

log "Queue status before cycle: pending=${PENDING_COUNT}, running=${RUNNING_COUNT}, backlog_limit=${BACKLOG_LIMIT}"

if [ "$RUNNING_COUNT" -gt 0 ]; then
  log "SAFE_CYCLE_ABORTED: ${RUNNING_COUNT} queue tasks are still running. Skipping this cycle."
  exit 0
fi

if [ "$PENDING_COUNT" -ge "$BACKLOG_LIMIT" ]; then
  log "Backlog is high. Skipping seed and processing existing pending queue only."
else
  log "Backlog is acceptable. Seeding priority coverage tasks."

  docker compose -f "$COMPOSE_FILE" exec -T api \
    python -m scripts.seed_priority_coverage_queue --limit "$SEED_LIMIT" --retry-after-hours 24 >> "$LOG_FILE" 2>&1
fi

log "Processing demand queue."

docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.process_search_demand_queue --limit "$PROCESS_LIMIT" --workers 1 --skip-company-enrichment >> "$LOG_FILE" 2>&1

log "Reconciling priority coverage."

docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.reconcile_priority_coverage >> "$LOG_FILE" 2>&1

PENDING_AFTER="$(queue_count pending)"
PENDING_AFTER="${PENDING_AFTER:-0}"

log "Queue status after cycle: pending=${PENDING_AFTER}"
log "SAFE_COLLECTION_CYCLE_DONE $(date -Is)"
