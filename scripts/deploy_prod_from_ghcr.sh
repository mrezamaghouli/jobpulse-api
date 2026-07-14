#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
IMAGE="${JOBPULSE_API_IMAGE:-ghcr.io/mrezamaghouli/jobpulse-api:main}"

echo "Deploying JobPulse API from $IMAGE"

docker compose -f "$COMPOSE_FILE" pull api

docker compose -f "$COMPOSE_FILE" up -d --no-build db api frontend

echo "Waiting for API health..."
for i in $(seq 1 30); do
  if curl -fsS http://localhost/api/health >/tmp/jobpulse_health.json 2>/dev/null; then
    cat /tmp/jobpulse_health.json
    echo
    echo "Deploy OK"
    exit 0
  fi

  sleep 2
done

echo "Deploy failed: API health check did not pass."
docker compose -f "$COMPOSE_FILE" ps
docker compose -f "$COMPOSE_FILE" logs --tail=80 api
exit 1
