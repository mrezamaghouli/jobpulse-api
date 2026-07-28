#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DB_SERVICE="${JOBPULSE_DB_SERVICE:-db}"
DB_USER="${POSTGRES_USER:-jobpulse_user}"
BACKUP_DIR="${JOBPULSE_BACKUP_DIR:-/opt/jobpulse/backups/postgres}"
TEST_DB="${JOBPULSE_VERIFY_DB:-jobpulse_restore_test}"

LATEST_BACKUP="$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | head -n 1 || true)"

if [ -z "$LATEST_BACKUP" ]; then
  echo "ERROR: No .dump backup found in $BACKUP_DIR"
  exit 1
fi

echo "Latest backup: $LATEST_BACKUP"
echo "Backup size:"
du -h "$LATEST_BACKUP"

cleanup() {
  docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
    dropdb -U "$DB_USER" --if-exists "$TEST_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Dropping old test database if exists..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  dropdb -U "$DB_USER" --if-exists "$TEST_DB" >/dev/null 2>&1 || true

echo "Creating test database..."
docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  createdb -U "$DB_USER" "$TEST_DB"

echo "Restoring latest backup into test database..."
cat "$LATEST_BACKUP" | docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  pg_restore -U "$DB_USER" -d "$TEST_DB" --no-owner --no-privileges --exit-on-error

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

echo "OK: latest backup restore test passed"
