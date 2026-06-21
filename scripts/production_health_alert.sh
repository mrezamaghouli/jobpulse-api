#!/usr/bin/env bash
set -u

ROOT="/opt/jobpulse"
ENV_FILE="$ROOT/.env"
COMPOSE_FILE="$ROOT/docker-compose.prod.yml"
ALERT_LOG="$ROOT/logs/health_alert.log"
STATE_FILE="/tmp/jobpulse_health_state"
LAST_ALERT_FILE="/tmp/jobpulse_health_last_alert"
COOLDOWN_SECONDS="${HEALTH_ALERT_COOLDOWN_SECONDS:-3600}"

mkdir -p "$ROOT/logs"

ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(ts) $*" | tee -a "$ALERT_LOG"
}

send_telegram() {
  local message="$1"

  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    log "telegram_not_configured"
    return 1
  fi

  curl -sS --max-time 20 \
    -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    >/tmp/jobpulse_telegram_response.out 2>/tmp/jobpulse_telegram_response.err || {
      log "telegram_send_failed error=$(cat /tmp/jobpulse_telegram_response.err)"
      return 1
    }

  log "telegram_send_ok response=$(cat /tmp/jobpulse_telegram_response.out)"
}

if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
else
  log "ERROR env_file_missing path=$ENV_FILE"
fi

if [ "${1:-}" = "--test" ]; then
  send_telegram "✅ JobPulse test alert OK

Server: $(hostname)
Time: $(ts)
API: http://35.192.251.190"
  exit 0
fi

fail=0
cd "$ROOT" || exit 1

log "health_alert_check_started"

for svc in db api frontend; do
  cid=$(docker compose -f "$COMPOSE_FILE" ps -q "$svc" 2>/dev/null || true)

  if [ -z "$cid" ]; then
    log "ERROR service_missing service=$svc"
    fail=1
    continue
  fi

  state=$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo "missing")
  log "service_status service=$svc state=$state"

  if [ "$state" != "running" ]; then
    fail=1
  fi
done

if curl -fsS --max-time 10 http://localhost/api/health >/tmp/jobpulse_api_health.out 2>/tmp/jobpulse_api_health.err; then
  log "api_health_ok response=$(cat /tmp/jobpulse_api_health.out)"
else
  log "ERROR api_health_failed error=$(cat /tmp/jobpulse_api_health.err)"
  fail=1
fi

jobs_count=$(docker compose -f "$COMPOSE_FILE" exec -T db psql -U jobpulse_user -d jobpulse -tAc "SELECT COUNT(*) FROM jobs;" 2>/tmp/jobpulse_jobs_count.err || true)

if [ -n "$jobs_count" ]; then
  log "jobs_count=$jobs_count"
else
  log "ERROR jobs_count_failed error=$(cat /tmp/jobpulse_jobs_count.err)"
  fail=1
fi

disk_pct=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
log "disk_usage_root=${disk_pct}%"

if [ "$disk_pct" -ge 97 ]; then
  log "ERROR disk_usage_critical=${disk_pct}%"
  fail=1
fi

previous_status="UNKNOWN"
if [ -f "$STATE_FILE" ]; then
  previous_status="$(cat "$STATE_FILE" 2>/dev/null || echo UNKNOWN)"
fi

if [ "$fail" -eq 0 ]; then
  echo "OK" > "$STATE_FILE"
  log "health_alert_check_finished status=OK"

  if [ "$previous_status" = "FAILED" ]; then
    send_telegram "✅ JobPulse recovered

Server: $(hostname)
Time: $(ts)

API health is OK again."
  fi

  exit 0
fi

echo "FAILED" > "$STATE_FILE"

now_epoch="$(date +%s)"
last_alert_epoch="0"

if [ -f "$LAST_ALERT_FILE" ]; then
  last_alert_epoch="$(cat "$LAST_ALERT_FILE" 2>/dev/null || echo 0)"
fi

elapsed=$((now_epoch - last_alert_epoch))

if [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
  log "health_failed_but_alert_in_cooldown elapsed=${elapsed}s cooldown=${COOLDOWN_SECONDS}s"
  exit 1
fi

echo "$now_epoch" > "$LAST_ALERT_FILE"

docker_status="$(docker compose -f "$COMPOSE_FILE" ps 2>&1 | tail -n 30)"
recent_log="$(tail -n 50 "$ALERT_LOG" 2>/dev/null || true)"
disk_status="$(df -h / 2>/dev/null || true)"

send_telegram "🚨 JobPulse health check FAILED

Server: $(hostname)
Time: $(ts)
Public API: http://35.192.251.190

Docker:
${docker_status}

Disk:
${disk_status}

Recent log:
${recent_log}"

log "health_alert_check_finished status=FAILED"
exit 1
