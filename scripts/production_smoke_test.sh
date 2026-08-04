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

SMOKE_TMP_DIR=""

cleanup() {
  local exit_code=$?
  if [[ -n "$SMOKE_TMP_DIR" ]]; then
    rm -rf "$SMOKE_TMP_DIR" || log "WARNING: failed to remove temporary directory $SMOKE_TMP_DIR"
  fi
  exit "$exit_code"
}
trap cleanup EXIT

# Never use predictable fixed /tmp output paths: a pre-existing file or
# symlink at a shared name could interfere with or hijack the redirection.
SMOKE_TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/jobpulse_smoke_test.XXXXXX")"
if [[ -z "$SMOKE_TMP_DIR" || ! -d "$SMOKE_TMP_DIR" ]]; then
  echo "ERROR: failed to create a temporary directory" >&2
  exit 1
fi
chmod 700 "$SMOKE_TMP_DIR"

BODY_FILE="${SMOKE_TMP_DIR}/body.out"
HEADERS_FILE="${SMOKE_TMP_DIR}/headers.out"

load_env_file() {
  local env_file="$1"
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
}

# Same effective precedence as docker-compose.prod.yml's api service
# env_file list: later files override earlier ones for overlapping keys.
load_env() {
  load_env_file "${PROJECT_DIR}/.api_keys.env"
  load_env_file "${PROJECT_DIR}/.admin.env"
  load_env_file "${PROJECT_DIR}/.env"
}

# Mirrors app/api_security.py's get_configured_public_api_keys(): split on
# commas, trim whitespace, skip empty entries, use the first usable key.
select_public_api_key() {
  local raw="${JOBPULSE_PUBLIC_API_KEYS:-}"
  local IFS=','
  local candidate
  for candidate in $raw; do
    candidate="$(printf '%s' "$candidate" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [[ -n "$candidate" ]]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

curl_status() {
  local url="$1"
  shift
  curl -sS -o "$BODY_FILE" -w "%{http_code}" "$@" "$url"
}

curl_header_status() {
  local url="$1"
  shift
  curl -sS -i -o "$HEADERS_FILE" -w "%{http_code}" "$@" "$url"
}

require_status() {
  local name="$1"
  local expected="$2"
  local url="$3"
  shift 3

  local status
  status="$(curl_status "$url" "$@" || true)"

  if [[ "$status" != "$expected" ]]; then
    log "URL=$url"
    log "BODY=$(cat "$BODY_FILE" 2>/dev/null || true)"
    fail "$name expected_http=$expected actual_http=$status"
  fi

  pass "$name http=$status"
}

require_json_contains() {
  local name="$1"
  local needle="$2"

  if ! grep -q "$needle" "$BODY_FILE"; then
    log "BODY=$(cat "$BODY_FILE" 2>/dev/null || true)"
    fail "$name missing=$needle"
  fi

  pass "$name contains=$needle"
}

# GET /jobs (response_model=list[Job]) legitimately returns [] when the
# selected query has no matching active jobs -- that is not a failure.
# Only the JSON shape is validated here, never row content.
require_jobs_array() {
  local name="$1"
  local error

  if ! error="$(python3 - "$BODY_FILE" <<'PY'
import json
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except json.JSONDecodeError as exc:
    print(f"invalid_json: {exc}")
    sys.exit(1)

if not isinstance(data, list):
    print(f"expected_top_level_array actual_type={type(data).__name__}")
    sys.exit(1)

for index, item in enumerate(data):
    if not isinstance(item, dict):
        print(f"element_{index}_not_an_object actual_type={type(item).__name__}")
        sys.exit(1)
PY
)"; then
    log "BODY=$(cat "$BODY_FILE" 2>/dev/null || true)"
    fail "$name ${error:-invalid_jobs_array}"
  fi

  pass "$name"
}

# GET /jobs/search has a stable response envelope regardless of how many
# (if any) rows match: results, count, page, limit, total_pages. This is
# the endpoint to probe for structural contract stability, since /jobs
# itself carries no such envelope.
require_jobs_search_envelope() {
  local name="$1"
  local error

  if ! error="$(python3 - "$BODY_FILE" <<'PY'
import json
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except json.JSONDecodeError as exc:
    print(f"invalid_json: {exc}")
    sys.exit(1)

if not isinstance(data, dict):
    print(f"expected_top_level_object actual_type={type(data).__name__}")
    sys.exit(1)

required_keys = ("results", "count", "page", "limit", "total_pages")
missing = [key for key in required_keys if key not in data]
if missing:
    print(f"missing_keys={','.join(missing)}")
    sys.exit(1)

if not isinstance(data["results"], list):
    print(f"results_not_a_list actual_type={type(data['results']).__name__}")
    sys.exit(1)

for key in ("count", "page", "limit", "total_pages"):
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        print(f"{key}_not_a_non_negative_integer value={value!r}")
        sys.exit(1)
PY
)"; then
    log "BODY=$(cat "$BODY_FILE" 2>/dev/null || true)"
    fail "$name ${error:-invalid_jobs_search_envelope}"
  fi

  pass "$name"
}

main() {
  cd "$PROJECT_DIR"
  load_env

  log "production_smoke_test_started base_url=${BASE_URL}"

  docker compose -f "$COMPOSE_FILE" ps | tee -a "$LOG_FILE"

  local public_api_key
  if ! public_api_key="$(select_public_api_key)"; then
    fail "no usable public API key found in JOBPULSE_PUBLIC_API_KEYS"
  fi

  require_status "health" "200" "${BASE_URL}/health"
  require_json_contains "health_database_connected" '"database":"connected"'

  require_status "jobs_without_key_blocked" "401" "${BASE_URL}/jobs?query=python%20backend%20remote&limit=5&page=1"

  require_status "jobs_search" "200" "${BASE_URL}/jobs?query=python%20backend%20remote&limit=5&page=1" \
    -H "X-API-Key: ${public_api_key}"
  require_jobs_array "jobs_search_is_valid_json_array"

  require_status "jobs_search_endpoint" "200" "${BASE_URL}/jobs/search?query=python%20backend%20remote&limit=5&page=1" \
    -H "X-API-Key: ${public_api_key}"
  require_jobs_search_envelope "jobs_search_endpoint_envelope"

  require_status "api_guard_limit_validation" "400" "${BASE_URL}/jobs?query=backend&limit=999&page=1" \
    -H "X-API-Key: ${public_api_key}"
  require_json_contains "api_guard_limit_error" 'limit must be between'

  local long_query
  long_query="$(python3 - <<'PY'
print("a" * 150)
PY
)"
  require_status "api_guard_query_length_validation" "400" "${BASE_URL}/jobs?query=${long_query}&limit=5&page=1" \
    -H "X-API-Key: ${public_api_key}"

  local first_cache_status second_cache_status
  first_cache_status="$(curl_header_status "${BASE_URL}/jobs?query=smoke%20python%20backend%20remote&limit=3&page=1" -H "X-API-Key: ${public_api_key}" || true)"
  grep -i "x-jobpulse-cache" "$HEADERS_FILE" | tee -a "$LOG_FILE" || true

  second_cache_status="$(curl_header_status "${BASE_URL}/jobs?query=smoke%20python%20backend%20remote&limit=3&page=1" -H "X-API-Key: ${public_api_key}" || true)"
  grep -i "x-jobpulse-cache" "$HEADERS_FILE" | tee -a "$LOG_FILE" || true

  if [[ "$first_cache_status" != "200" || "$second_cache_status" != "200" ]]; then
    fail "cache_test expected 200/200 got ${first_cache_status}/${second_cache_status}"
  fi

  if grep -qi "x-jobpulse-cache: HIT" "$HEADERS_FILE"; then
    pass "api_cache_hit"
  else
    log "WARN cache HIT header not observed; continuing"
  fi

  require_status "admin_without_key_blocked" "401" "${BASE_URL}/admin/summary"

  if [[ -z "${ADMIN_API_KEY:-}" ]]; then
    fail "ADMIN_API_KEY missing from .admin.env/.env"
  fi

  local admin_status
  admin_status="$(
    curl -sS \
      -H "X-Admin-Key: ${ADMIN_API_KEY}" \
      -o "$BODY_FILE" \
      -w "%{http_code}" \
      "${BASE_URL}/admin/summary" || true
  )"

  if [[ "$admin_status" != "200" ]]; then
    log "BODY=$(cat "$BODY_FILE" 2>/dev/null || true)"
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
