#!/usr/bin/env bash
set -euo pipefail

cd /opt/jobpulse

echo "=============================="
echo "Docker services"
echo "=============================="
docker compose -f docker-compose.prod.yml ps

echo
echo "=============================="
echo "API health"
echo "=============================="
curl -fsS http://localhost/api/health || true
echo

echo
echo "=============================="
echo "Job stats"
echo "=============================="
docker compose -f docker-compose.prod.yml exec -T db psql -U jobpulse_user -d jobpulse -c "
SELECT
  COUNT(*) AS total_jobs,
  MAX(first_seen_at) AS newest_first_seen,
  MAX(last_seen_at) AS newest_last_seen,
  COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '1 hour') AS jobs_added_last_hour,
  COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '1 hour') AS jobs_seen_last_hour
FROM jobs;
"

echo
echo "=============================="
echo "Bad external apply check"
echo "=============================="
docker compose -f docker-compose.prod.yml exec -T db psql -U jobpulse_user -d jobpulse -c "
SELECT COUNT(*) AS bad_external_apply_count
FROM jobs
WHERE last_seen_at >= NOW() - INTERVAL '24 hours'
  AND apply_type = 'external'
  AND (
    apply_url IS NULL
    OR TRIM(apply_url) = ''
    OR apply_url ILIKE '%linkedin.com%'
    OR apply_url ILIKE '%lnkd.in%'
  );
"

echo
echo "=============================="
echo "Latest backups"
echo "=============================="
ls -lh /opt/jobpulse/backups | tail -n 5

echo
echo "=============================="
echo "Cron jobs"
echo "=============================="
crontab -l
