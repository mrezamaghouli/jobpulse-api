#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="/opt/jobpulse/backups"
COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"
DB_SERVICE="db"
DB_USER="jobpulse_user"
DB_NAME="jobpulse"
TEST_DB="jobpulse_restore_test"

LATEST_BACKUP="$(ls -1t "$BACKUP_DIR"/jobpulse_*.sql 2>/dev/null | head -n 1 || true)"

if [ -z "$LATEST_BACKUP" ]; then
  echo "ERROR: No backup file found in $BACKUP_DIR"
  exit 1
fi

echo "Latest backup: $LATEST_BACKUP"
echo "Backup size:"
du -h "$LATEST_BACKUP"

cd /opt/jobpulse

echo "Dropping old test database if exists..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;"

echo "Creating test database..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d postgres -c "CREATE DATABASE $TEST_DB;"

echo "Restoring latest backup into test database..."
cat "$LATEST_BACKUP" | docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d "$TEST_DB" >/tmp/jobpulse_restore_test.log 2>& -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d "$TEST_DB" >/tmp1

echo "Checking restored data..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d "$TEST_DB" -c "
SELECT
  COUNT(*) AS restored_jobs,
  MAX(first_seen_at) AS newest_first_seen,
  MAX(last_seen_at) AS newest_last_seen
FROM jobs;
"

echo "Checking important tables..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d "$TEST_DB" -c "
SELECT table_name
FROM information_schema.tables
WHERE table_schema='public'
  AND table_name IN (
    'jobs',
    'job_search_demand_queue',
    'job_collection_coverage',
    'job_catalog_countries',
    'admin_action_runs'
  )
ORDER BY table_name;
"

echo "Cleaning up test database..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $TEST_DB;"

echo "OK: latest backup restore test passed ✅"
