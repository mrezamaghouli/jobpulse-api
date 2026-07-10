#!/usr/bin/env bash
set -u

cd /opt/jobpulse

COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"
LOG_FILE="/opt/jobpulse/logs/collection_maintenance.log"

mkdir -p /opt/jobpulse/logs

echo "" >> "$LOG_FILE"
echo "===== COLLECTION MAINTENANCE $(date -Is) =====" >> "$LOG_FILE"

run_step() {
  local name="$1"
  shift

  echo "" >> "$LOG_FILE"
  echo "Running maintenance step: ${name}" >> "$LOG_FILE"

  if ! docker compose -f "$COMPOSE_FILE" exec -T api "$@" >> "$LOG_FILE" 2>&1; then
    echo "MAINTENANCE_STEP_FAILED: ${name}" >> "$LOG_FILE"
    exit 0
  fi
}

run_step "backfill_companies_from_jobs" python -m scripts.backfill_companies_from_jobs
run_step "sync_job_company_logos" python -m scripts.sync_job_company_logos
run_step "build_job_search_embeddings" python -m scripts.build_job_search_embeddings

echo "COLLECTION_MAINTENANCE_DONE $(date -Is)" >> "$LOG_FILE"
