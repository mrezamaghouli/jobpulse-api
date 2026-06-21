#!/usr/bin/env bash
set -u

ROOT="/opt/jobpulse"
COMPOSE_FILE="$ROOT/docker-compose.prod.yml"
LOG="$ROOT/logs/health_check.log"

mkdir -p "$ROOT/logs"

ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(ts) $*" | tee -a "$LOG"
}

fail=0

cd "$ROOT" || exit 1

log "=============================="
log "health_check_started"

# 1) Docker compose services
for svc in db api frontend; do
  cid=$(docker compose -f "$COMPOSE_FILE" ps -q "$svc" 2>/dev/null || true)

  if [ -z "$cid" ]; then
    log "ERROR service_missing service=$svc"
    fail=1
    continue
  fi

  state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo "missing")
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || echo "missing")

  log "service_status service=$svc state=$state health=$health"

  if [ "$state" != "running" ]; then
    log "ERROR service_not_running service=$svc"
    fail=1
  fi
done

# 2) API health
if curl -fsS --max-time 10 http://localhost/api/health >/tmp/jobpulse_api_health.out 2>/tmp/jobpulse_api_health.err; then
  log "api_health_ok response=$(cat /tmp/jobpulse_api_health.out)"
else
  log "ERROR api_health_failed error=$(cat /tmp/jobpulse_api_health.err)"
  fail=1
fi

# 3) DB readiness
if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U jobpulse_user -d jobpulse >/tmp/jobpulse_db_ready.out 2>/tmp/jobpulse_db_ready.err; then
  log "db_ready_ok response=$(cat /tmp/jobpulse_db_ready.out)"
else
  log "ERROR db_ready_failed error=$(cat /tmp/jobpulse_db_ready.err)"
  fail=1
fi

# 4) Jobs count
jobs_count=$(docker compose -f "$COMPOSE_FILE" exec -T db psql -U jobpulse_user -d jobpulse -tAc "SELECT COUNT(*) FROM jobs;" 2>/tmp/jobpulse_jobs_count.err || true)

if [ -n "$jobs_count" ]; then
  log "jobs_count=$jobs_count"
else
  log "ERROR jobs_count_failed error=$(cat /tmp/jobpulse_jobs_count.err)"
  fail=1
fi

# 5) Disk usage
disk_pct=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
log "disk_usage_root=${disk_pct}%"

if [ "$disk_pct" -ge 90 ]; then
  log "WARNING disk_usage_high=${disk_pct}%"
fi

if [ "$disk_pct" -ge 97 ]; then
  log "ERROR disk_usage_critical=${disk_pct}%"
  fail=1
fi

# 6) Cron check
if crontab -l 2>/dev/null | grep -q "seed_autonomous_search_queue"; then
  log "cron_autonomous_collector_ok"
else
  log "WARNING cron_autonomous_collector_missing"
fi

if crontab -l 2>/dev/null | grep -q "process_search_demand_queue"; then
  log "cron_demand_processor_ok"
else
  log "WARNING cron_demand_processor_missing"
fi

if [ "$fail" -eq 0 ]; then
  log "health_check_finished status=OK"
  exit 0
else
  log "health_check_finished status=FAILED"
  exit 1
fi
