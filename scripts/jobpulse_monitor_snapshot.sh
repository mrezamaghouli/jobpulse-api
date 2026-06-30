#!/usr/bin/env bash
set -uo pipefail

cd /opt/jobpulse

mkdir -p /opt/jobpulse/logs

TS="$(date -Is)"
HEALTH="fail"

if curl -fsS http://localhost/api/health >/tmp/jobpulse_api_health.json 2>/dev/null; then
  HEALTH="ok"
fi

STATS="$(
docker compose -f docker-compose.prod.yml exec -T db psql -U jobpulse_user -d jobpulse -At -F '|' -c "
SELECT
  COUNT(*),
  COALESCE(MAX(first_seen_at)::text, ''),
  COALESCE(MAX(last_seen_at)::text, ''),
  COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '1 hour'),
  COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '1 hour'),
  COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '24 hours'),
  COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '24 hours')
FROM jobs;
" 2>/dev/null
)"

IFS='|' read -r TOTAL_JOBS NEWEST_FIRST NEWEST_LAST ADDED_1H SEEN_1H ADDED_24H SEEN_24H <<< "$STATS"

BAD_APPLY="$(
docker compose -f docker-compose.prod.yml exec -T db psql -U jobpulse_user -d jobpulse -At -c "
SELECT COUNT(*)
FROM jobs
WHERE last_seen_at >= NOW() - INTERVAL '24 hours'
  AND apply_type = 'external'
  AND (
    apply_url IS NULL
    OR TRIM(apply_url) = ''
    OR apply_url ILIKE '%linkedin.com%'
    OR apply_url ILIKE '%lnkd.in%'
  );
" 2>/dev/null
)"

BACKUPS_24H="$(find /opt/jobpulse/backups -name 'jobpulse_*.sql' -type f -mtime -1 2>/dev/null | wc -l | tr -d ' ')"

LINE="$TS health=$HEALTH total_jobs=${TOTAL_JOBS:-unknown} added_1h=${ADDED_1H:-unknown} seen_1h=${SEEN_1H:-unknown} added_24h=${ADDED_24H:-unknown} seen_24h=${SEEN_24H:-unknown} bad_apply=${BAD_APPLY:-unknown} backups_24h=${BACKUPS_24H:-0} newest_first=${NEWEST_FIRST:-unknown} newest_last=${NEWEST_LAST:-unknown}"

echo "$LINE" >> /opt/jobpulse/logs/status_snapshots.log
echo "$LINE"

ALERTS=0

if [ "$HEALTH" != "ok" ]; then
  echo "$TS ALERT api_health_failed" >> /opt/jobpulse/logs/status_alerts.log
  ALERTS=$((ALERTS + 1))
fi

if [ "${BAD_APPLY:-0}" != "0" ]; then
  echo "$TS ALERT bad_external_apply_count=${BAD_APPLY}" >> /opt/jobpulse/logs/status_alerts.log
  ALERTS=$((ALERTS + 1))
fi

if [ "${SEEN_24H:-0}" = "0" ]; then
  echo "$TS ALERT no_jobs_seen_last_24h" >> /opt/jobpulse/logs/status_alerts.log
  ALERTS=$((ALERTS + 1))
fi

if [ "${BACKUPS_24H:-0}" = "0" ]; then
  echo "$TS ALERT no_backup_last_24h" >> /opt/jobpulse/logs/status_alerts.log
  ALERTS=$((ALERTS + 1))
fi

echo "$TS alerts=$ALERTS" >> /opt/jobpulse/logs/status_snapshots.log
