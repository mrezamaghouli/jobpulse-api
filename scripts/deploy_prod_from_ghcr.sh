#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
IMAGE="${JOBPULSE_API_IMAGE:-ghcr.io/mrezamaghouli/jobpulse-api:main}"
STATUS_FILE="${JOBPULSE_DEPLOY_STATUS_FILE:-/opt/jobpulse/logs/deploy_status.json}"
LOG_FILE="${JOBPULSE_DEPLOY_LOG:-/opt/jobpulse/logs/deploy_prod_from_ghcr.log}"

mkdir -p "$(dirname "$STATUS_FILE")" "$(dirname "$LOG_FILE")"

log() {
  echo "$(date -u +"%Y-%m-%dT%H:%M:%SZ") $*" | tee -a "$LOG_FILE"
}

write_status() {
  local status="$1"
  local message="$2"
  local previous_image_id="${3:-}"
  local current_image_id="${4:-}"

  cat > "$STATUS_FILE" <<JSON
{
  "status": "$status",
  "message": "$message",
  "image": "$IMAGE",
  "previous_image_id": "$previous_image_id",
  "current_image_id": "$current_image_id",
  "updated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
JSON
}

health_check() {
  for i in $(seq 1 30); do
    if curl -fsS http://localhost/api/health >/tmp/jobpulse_health.json 2>/dev/null; then
      cat /tmp/jobpulse_health.json
      echo
      return 0
    fi

    sleep 2
  done

  return 1
}

ensure_no_api_build() {
  if docker compose -f "$COMPOSE_FILE" config | awk '
    $1 == "api:" { in_api=1; next }
    /^[a-zA-Z0-9_-]+:/ && $1 != "api:" { in_api=0 }
    in_api && $1 == "build:" { found=1 }
    END { exit found ? 0 : 1 }
  '; then
    log "ERROR: api service still has build: in $COMPOSE_FILE. Refusing production deploy."
    write_status "failed" "api service still has build configured"
    exit 1
  fi
}

log "deploy_start image=$IMAGE"

ensure_no_api_build

previous_image_id="$(
  docker inspect --format '{{.Image}}' jobpulse-api-prod 2>/dev/null || true
)"

log "previous_image_id=${previous_image_id:-none}"

docker compose -f "$COMPOSE_FILE" pull api

pulled_image_id="$(
  docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || true
)"

log "pulled_image_id=${pulled_image_id:-unknown}"

docker compose -f "$COMPOSE_FILE" up -d --no-build db api frontend

log "waiting_for_health"

if health_check; then
  current_image_id="$(
    docker inspect --format '{{.Image}}' jobpulse-api-prod 2>/dev/null || true
  )"

  log "deploy_ok current_image_id=${current_image_id:-unknown}"
  write_status "success" "Deploy completed successfully" "$previous_image_id" "$current_image_id"
  exit 0
fi

log "deploy_health_failed"

if [[ -n "$previous_image_id" ]]; then
  log "rollback_start previous_image_id=$previous_image_id"

  docker tag "$previous_image_id" "$IMAGE"

  docker compose -f "$COMPOSE_FILE" up -d --no-build api

  if health_check; then
    current_image_id="$(
      docker inspect --format '{{.Image}}' jobpulse-api-prod 2>/dev/null || true
    )"

    log "rollback_ok current_image_id=${current_image_id:-unknown}"
    write_status "rolled_back" "Deploy failed health check; rolled back to previous image" "$previous_image_id" "$current_image_id"
    exit 1
  fi

  log "rollback_failed"
  write_status "failed" "Deploy failed and rollback health check failed" "$previous_image_id" ""
else
  log "rollback_unavailable previous_image_id_missing"
  write_status "failed" "Deploy failed and no previous image was available for rollback" "" ""
fi

docker compose -f "$COMPOSE_FILE" ps | tee -a "$LOG_FILE"
docker compose -f "$COMPOSE_FILE" logs --tail=120 api | tee -a "$LOG_FILE"

exit 1
