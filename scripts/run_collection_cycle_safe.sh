#!/usr/bin/env bash
set -u

cd /opt/jobpulse

COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"
LOG_FILE="/opt/jobpulse/logs/collection_cycle.log"

mkdir -p /opt/jobpulse/logs

echo "" >> "$LOG_FILE"
echo "===== SAFE COLLECTION CYCLE $(date -Is) =====" >> "$LOG_FILE"

echo "Running LinkedIn auth preflight..." >> "$LOG_FILE"

if ! docker compose -f "$COMPOSE_FILE" exec -T api python -m scripts.linkedin_auth_preflight >> "$LOG_FILE" 2>&1; then
  echo "SAFE_CYCLE_ABORTED: LinkedIn auth preflight failed. Skipping seed/process/reconcile." >> "$LOG_FILE"

  # Trigger Telegram alert if configured
  python3 scripts/send_telegram_alerts.py >> /opt/jobpulse/logs/telegram_alerts.log 2>&1 || true

  exit 0
fi

echo "LinkedIn auth preflight passed. Starting collection..." >> "$LOG_FILE"

docker0
fi

echo "LinkedIn auth preflight passed. Starting collection..." >> "$LOG_FILE"

docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.seed_priority_coverage_queue --limit 10 --retry-after-hours 24 >> "$LOG_FILE" 2>&1

docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.process_search_demand_queue --limit 10 --workers 1 --skip-company-enrichment >> "$LOG_FILE" 2>&1

docker compose -f "$COMPOSE_FILE" exec -T api \
  python -m scripts.reconcile_priority_coverage >> "$LOG_FILE" 2>&1

echo "SAFE_COLLECTION_CYCLE_DONE $(date -Is)" >> "$LOG_FILE"
