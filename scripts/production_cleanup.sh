#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/jobpulse}"
LOG_DIR="${PROJECT_DIR}/logs"
BACKUP_DIR="${BACKUP_DIR:-/opt/jobpulse_backups}"
DRY_RUN="false"

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="true"
fi

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(timestamp) $*"
}

run() {
  log "RUN $*"
  if [[ "$DRY_RUN" != "true" ]]; then
    "$@"
  fi
}

delete_old_files() {
  local dir="$1"
  local pattern="$2"
  local days="$3"

  if [[ ! -d "$dir" ]]; then
    log "SKIP missing_dir=$dir"
    return 0
  fi

  log "cleanup_old_files dir=$dir pattern=$pattern older_than_days=$days"

  if [[ "$DRY_RUN" == "true" ]]; then
    find "$dir" -type f -name "$pattern" -mtime +"$days" -print
  else
    find "$dir" -type f -name "$pattern" -mtime +"$days" -print -delete
  fi
}

truncate_large_logs() {
  mkdir -p "$LOG_DIR"

  log "truncate_large_logs dir=$LOG_DIR threshold=100M keep_last_lines=20000"

  find "$LOG_DIR" -type f -name "*.log" -size +100M -print | while read -r file; do
    log "large_log_found file=$file"
    if [[ "$DRY_RUN" != "true" ]]; then
      tail -n 20000 "$file" > "${file}.tmp"
      cat "${file}.tmp" > "$file"
      rm -f "${file}.tmp"
      log "large_log_truncated file=$file"
    fi
  done
}

cleanup_python_cache() {
  log "cleanup_python_cache"

  if [[ "$DRY_RUN" == "true" ]]; then
    find "$PROJECT_DIR" -type d -name "__pycache__" -print
    find "$PROJECT_DIR" -type f -name "*.pyc" -print
  else
    find "$PROJECT_DIR" -type d -name "__pycache__" -prune -print -exec rm -rf {} +
    find "$PROJECT_DIR" -type f -name "*.pyc" -print -delete
  fi
}

cleanup_debug_files() {
  log "cleanup_debug_files"

  for dir in \
    "${PROJECT_DIR}/sample_output" \
    "${PROJECT_DIR}/debug_screenshots" \
    "${PROJECT_DIR}/screenshots" \
    "${PROJECT_DIR}/tmp"
  do
    if [[ -d "$dir" ]]; then
      delete_old_files "$dir" "*" 7
    fi
  done
}

cleanup_backups() {
  log "cleanup_backups"

  delete_old_files "${BACKUP_DIR}/releases" "*.tar.gz" 14
  delete_old_files "${BACKUP_DIR}/db" "*.dump" 14
  delete_old_files "${BACKUP_DIR}/db" "*.sql" 14
  delete_old_files "${BACKUP_DIR}/db" "*.gz" 14
}

cleanup_docker() {
  log "cleanup_docker"

  if ! command -v docker >/dev/null 2>&1; then
    log "SKIP docker_not_found"
    return 0
  fi

  # Safe cleanup: no volume pruning.
  run docker container prune -f --filter "until=168h"
  run docker image prune -af --filter "until=168h"
  run docker builder prune -af --filter "until=168h"
}

disk_report() {
  log "disk_report"
  df -h / || true
  docker system df || true
}

main() {
  log "production_cleanup_started dry_run=$DRY_RUN"

  mkdir -p "$LOG_DIR"

  disk_report
  truncate_large_logs
  cleanup_python_cache
  cleanup_debug_files
  cleanup_backups
  cleanup_docker
  disk_report

  log "production_cleanup_finished"
}

main "$@"
