#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/jobpulse}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BASE_URL="${BASE_URL:-http://localhost/api}"
LOG_FILE="${PROJECT_DIR}/logs/production_smoke_test.log"

mkdir -p "${PROJECT_DIR}/logs"

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(timestamp) $*" | tee -a "$LOG_FILE"
}

fail() {
  log "FAIL $*"
  exit 1
}

pass() {
  log "PASS $*"
}

load_env() {
  if [[ -f "${PROJECT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${PROJECT_DIR}/.env"
    set +a
  fi
}

curl_status() {
  local url="$1"
  curl -sS -o /tmp/jobpulse_smoke_body.out -w "%{http_code}" "$url"
}

curl_header_status() {
  local url="$1"
  curl -sS -i "$url" -o /tmp/jobpulse_smoke_headers.out -w "%{http_code}"
}

require_status() {
  local name="$1"
  local expected="$2"
  local url="$3"

  local status
  status="$(curl_status "$url" || true)"

  if [[ "$status" != "$expected" ]]; then
    log "URL=$url"
    log "BODY=$(cat /tmp/jobpulse_smoke_body.out 2>/dev/null || true)"
    fail "$name expected_http=$expected actual_http=$status"
  fi

  pass "$name http=$status"
}

require_json_contains() {
  local name="$1"
  local needle="$2"

  if ! grep -q "$needle" /tmp/jobpulse_smoke_body.out; then
    log "BODY=$(cat /tmp/jobpulse_smoke_body.out 2>/dev/null || true)"
    fail "$name missing=$needle"
  fi

  pass "$name contains=$needle"
}

main() {
  cd "$PROJECT_DIR"
  load_env

  log "production_smoke_test_started base_url=${BASE_URL}"

  docker compose -f "$COMPOSE_FILE" ps | tee -a "$LOG_FILE"

  require_status "health" "200" "${BASE_URL}/health"
  require_json_contains "health_database_connected" '"database":"connected"'

  require_status "jobs_search" "200" "${BASE_URL}/jobs?query=python%20backend%20remote&limit=5&page=1"
  require_json_contains "jobs_search_has_title" '"title"'
  require_json_contains "jobs_search_has_quality_score" '"quality_score"'

  require_status "api_guard_limit_validation" "400" "${BASE_URL}/jobs?query=backend&limit=999&page=1"
  require_json_contains "api_guard_limit_error" 'limit must be between'

  local long_query
  long_query="$(python3 - <<'PY'
print("a" * 150)
PY
)"
  require_status "api_guard_query_length_validation" "400" "${BASE_URL}/jobs?query=${long_query}&limit=5&page=1"

  local first_cache_status second_cache_status
  first_cache_status="$(curl_header_status "${BASE_URL}/jobs?query=smoke%20python%20backend%20remote&limit=3&page=1" || true)"
  grep -i "x-jobpulse-cache" /tmp/jobpulse_smoke_headers.out | tee -a "$LOG_FILE" || true

  second_cache_status="$(curl_header_status "${BASE_URL}/jobs?query=smoke%20python%20backend%20remote&limit=3&page=1" || true)"
  grep -i "x-jobpulse-cache" /tmp/jobpulse_smoke_headers.out | tee -a "$LOG_FILE" || true

  if [[ "$first_cache_status" != "200" || "$second_cache_status" != "200" ]]; then
    fail "cache_test expected 200/200 got ${first_cache_status}/${second_cache_status}"
  fi

  if grep -qi "x-jobpulse-cache: HIT" /tmp/jobpulse_smoke_headers.out; then
    pass "api_cache_hit"
  else
    log "WARN cache HIT header not observed; continuing"
  fi

  require_status "admin_without_key_blocked" "401" "${BASE_URL}/admin/summary"

  if [[ -z "${ADMIN_API_KEY:-}" ]]; then
    fail "ADMIN_API_KEY missing from .env"
  fi

  local admin_status
  admin_status="$(
    curl -sS \
      -H "X-Admin-Key: ${ADMIN_API_KEY}" \
      -o /tmp/jobpulse_smoke_body.out \
      -w "%{http_code}" \
      "${BASE_URL}/admin/summary" || true
  )"

  if [[ "$admin_status" != "200" ]]; then
    log "BODY=$(cat /tmp/jobpulse_smoke_body.out 2>/dev/null || true)"
    fail "admin_with_key expected_http=200 actual_http=$admin_status"
  fi

  require_json_contains "admin_summary_has_jobs" '"jobs"'
  pass "admin_with_key"

  local jobs_count
  jobs_count="$(
    docker compose -f "$COMPOSE_FILE" exec -T db \
      psql -U jobpulse_user -d jobpulse -Atc "SELECT COUNT(*) FROM jobs;" \
      | tr -d '[:space:]'
  )"

  if [[ -z "$jobs_count" || "$jobs_count" == "0" ]]; then
    fail "jobs_count_invalid count=${jobs_count:-empty}"
  fi

  pass "jobs_count count=$jobs_count"

  docker compose -f "$COMPOSE_FILE" exec -T api \
    python -m scripts.collection_cycle_report --limit 3 \
    | tee -a "$LOG_FILE"

  docker compose -f "$COMPOSE_FILE" exec -T api \
    python -m scripts.migrate_database \
    | tee -a "$LOG_FILE"

  log "production_smoke_test_finished status=OK"
}

main "$@"
