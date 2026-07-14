#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

REQ_DIR="/opt/jobpulse/logs/admin_requests"
LOG_DIR="/opt/jobpulse/logs"

BACKUP_REQ="$REQ_DIR/postgres_backup_now.request"
VERIFY_REQ="$REQ_DIR/postgres_restore_verify_now.request"

mkdir -p "$REQ_DIR" "$LOG_DIR"

log() {
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") $*"
}

write_result() {
  local name="$1"
  local status="$2"
 M:%SZ") $*"
}

write_result() {
  local name="$1"
  local status="$2"
  local message="$3"
  local file="$REQ_DIR/${name}_last_result.json"

  cat > "$file" <<JSON
{
  "action": "$name",
  "status": "$status",
  "message": "$message",
  "finished_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
JSON
}

run_backup_now() {
  local running="$REQ_DIR/postgres_backup_now.running"

  mv "$BACKUP_REQ" "$running"

  log "postgres_backup_request_start"

  if ./scripts/backup_postgres_prod.sh >> "$LOG_DIR/postgres_backup.manual.log" 2>&1; then
    ./scripts/check_postgres_backups.py >> "$LOG_DIR/postgres_backup_monitor.manual.log" 2>&1 || true
    write_result "postgres_backup" "success" "PostgreSQL backup completed successfully."
    log "postgres_backup_request_ok"
  else
    write_result "postgres_backup" "failed" "PostgreSQL backup failed. Check logs/postgres_backup.manual.log"
    log "postgres_backup_request_failed"
  fi

  rm -f "$running"
}

run_restore_verify_now() {
  local running="$REQ_DIR/postgres_restore_verify_now.running"

  mv "$VERIFY_REQ" "$running"

  log "postgres_restore_verify_request_start"

  if ./scripts/restore_verify_postgres_prod.sh >> "$LOG_DIR/postgres_restore_verify.manual.log" 2>&1; then
    ./scripts/check_postgres_backups.py >> "$LOG_DIR/postgres_backup_monitor.manual.log" 2>&1 || true
    write_result "postgres_restore_verify" "success" "PostgreSQL restore verification completed successfully."
    log "postgres_restore_verify_request_ok"
  else
    write_result "postgres_restore_verify" "failed" "PostgreSQL restore verification failed. Check logs/postgres_restore_verify.manual.log"
    log "postgres_restore_verify_request_failed"
  fi

  rm -f "$running"
}

if [[ -f "$BACKUP_REQ" ]]; then
  run_backup_now
fi

if [[ -f "$VERIFY_REQ" ]]; then
  run_restore_verify_now
fi
