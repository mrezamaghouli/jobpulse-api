#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DB_SERVICE="${JOBPULSE_DB_SERVICE:-db}"
DB_USER="${POSTGRES_USER:-jobpulse_user}"
BACKUP_DIR="${JOBPULSE_BACKUP_DIR:-/opt/jobpulse/backups/postgres}"
VERIFY_DB="${JOBPULSE_VERIFY_DB:-jobpulse_restore_verify}"
LOG_FILE="${JOBPULSE_RESTORE_VERIFY_LOG:-/opt/jobpulse/logs/postgres_restore_verify.log}"

mkdir -p "$(dirname "$LOG_FILE")"

backup_file="${1:-}"
if [[ -z "$backup_file" ]]; then
  backup_file="$(ls -t "$BACKUP_DIR"/*.dump 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "$backup_file" || ! -f "$backup_file" ]]; then
  echo "No backup dump found in $BACKUP_DIR" | tee -a "$LOG_FILE"
  exit 1
fi

log() {
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") $*" | tee -a "$LOG_FILE"
}

cleanup() {
  if [[ "${JOBPULSE_KEEP_VERIFY_DB:-0}" != "1" ]]; then
    docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
      dropdb -U "$DB_USER" --if-exists "$VERIFY_DB" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

log "restore_verify_start backup=$backup_file verify_db=$VERIFY_DB"

docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  dropdb -U "$DB_USER" --if-exists "$VERIFY_DB" >/dev/null 2>&1 || true

docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  createdb -U "$DB_USER" "$VERIFY_DB"

cat "$backup_file" | docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  pg_restore \
    -U "$DB_USER" \
    -d "$VERIFY_DB" \
    --no-owner \
    --no-privileges \
    --exit-on-error

tables_count="$(
  docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
    psql -U "$DB_USER" -d "$VERIFY_DB" -tAc \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';"
)"

jobs_count="$(
  docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
    psql -U "$DB_USER" -d "$VERIFY_DB" -tAc \
    "SELECT CASE WHEN to_regclass('public.jobs') IS NULL THEN -1 ELSE (SELECT COUNT(*) FROM public.jobs) END;"
)"

if [[ "$tables_count" -le 0 ]]; then
  log "restore_verify_failed reason=no_public_tables backup=$backup_file"
  exit 1
fi

if [[ "$jobs_count" -lt 0 ]]; then
  log "restore_verify_failed reason=jobs_table_missing backup=$backup_file tables=$tables_count"
  exit 1
fi

log "restore_verify_ok backup=$backup_file tables=$tables_count jobs=$jobs_count"
