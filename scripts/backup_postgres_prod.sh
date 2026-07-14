#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DB_SERVICE="${JOBPULSE_DB_SERVICE:-db}"
DB_NAME="${POSTGRES_DB:-jobpulse}"
DB_USER="${POSTGRES_USER:-jobpulse_user}"
BACKUP_DIR="${JOBPULSE_BACKUP_DIR:-/opt/jobpulse/backups/postgres}"
LOG_FILE="${JOBPULSE_BACKUP_LOG:-/opt/jobpulse/logs/postgres_backup.log}"
RETENTION_DAYS="${JOBPULSE_BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR" "$(dirname "$LOG_FILE")"

ts="$(date -u +"%Y%m%dT%H%M%SZ")"
tmp_file="$BACKUP_DIR/.${DB_NAME}_${ts}.dump.tmp"
dump_file="$BACKUP_DIR/${DB_NAME}_${ts}.dump"
sha_file="$dump_file.sha256"
list_file="$dump_file.list"
manifest_file="$dump_file.json"

log() {
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") $*" | tee -a "$LOG_FILE"
}

cleanup_on_error() {
  rm -f "$tmp_file"
}
trap cleanup_on_error ERR

log "backup_start db=$DB_NAME dir=$BACKUP_DIR"

docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  pg_dump \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -Fc \
    --no-owner \
    --no-privileges \
  > "$tmp_file"

test -s "$tmp_file"

mv "$tmp_file" "$dump_file"

sha256sum "$dump_file" > "$sha_file"

cat "$dump_file" | docker compose -f "$COMPOSE_FILE" exec -T "$DB_SERVICE" \
  pg_restore -l > "$list_file"

bytes="$(stat -c%s "$dump_file")"
sha="$(cut -d' ' -f1 "$sha_file")"

cat > "$manifest_file" <<JSON
{
  "created_at_utc": "$ts",
  "db_name": "$DB_NAME",
  "db_user": "$DB_USER",
  "file": "$dump_file",
  "bytes": $bytes,
  "sha256": "$sha",
  "format": "pg_dump custom",
  "retention_days": $RETENTION_DAYS
}
JSON

find "$BACKUP_DIR" -type f \( -name "*.dump" -o -name "*.dump.sha256" -o -name "*.dump.list" -o -name "*.dump.json" \) -mtime +"$RETENTION_DAYS" -delete

log "backup_ok file=$dump_file bytes=$bytes sha256=$sha"
