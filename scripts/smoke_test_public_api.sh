#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${1:-http://localhost}"
API_KEYS_FILE="${JOBPULSE_API_KEYS_FILE:-/opt/jobpulse/.api_keys.env}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

pass() {
  echo "✅ PASS: $*"
}

fail() {
  echo "❌ FAIL: $*" >&2
  exit 1
}

info() {
  echo "ℹ️  $*"
}

require_status() {
  local name="$1"
  local url="$2"
  local expected="$3"
  local extra_header="${4:-}"

  local headers="$tmp_dir/${name}.headers"
  local body="$tmp_dir/${name}.body"

  local code
  if [[ -n "$extra_header" ]]; then
    code="$(curl -sS -D "$headers" -o "$body" -w "%{http_code}" -H "$extra_header" "$url")"
  else
    code="$(curl -sS -D "$headers" -o "$body" -w "%{http_code}" "$url")"
  fi

  if [[ "$code" != "$expected" ]]; then
    echo "URL: $url" >&2
    echo "Expected: $expected" >&2
    echo "Got: $code" >&2
    echo "Body:" >&2
    cat "$body" >&2 || true
    fail "$name returned unexpected status"
  fi

  pass "$name status $expected"
}

require_json_ok() {
  local name="$1"
  local url="$2"
  local extra_header="${3:-}"

  local body="$tmp_dir/${name}.json"
  local headers="$tmp_dir/${name}.headers"

  if [[ -n "$extra_header" ]]; then
    curl -fsS -D "$headers" -H "$extra_header" "$url" -o "$body"
  else
    curl -fsS -D "$headers" "$url" -o "$body"
  fi

  python3 -m json.tool "$body" >/dev/null

  pass "$name JSON valid"
}

extract_api_key() {
  if [[ ! -f "$API_KEYS_FILE" ]]; then
    fail "API keys file not found: $API_KEYS_FILE"
  fi

  local raw
  raw="$(
    grep -E '^JOBPULSE_PUBLIC_API_KEYS=' "$API_KEYS_FILE" \
      | tail -n 1 \
      | cut -d= -f2- \
      | sed 's/^["'\'']//; s/["'\'']$//' \
      | cut -d, -f1 \
      | xargs
  )"

  if [[ -z "$raw" ]]; then
    fail "No API key found in JOBPULSE_PUBLIC_API_KEYS"
  fi

  printf '%s' "$raw"
}

info "Base URL: $BASE_URL"

API_KEY="$(extract_api_key)"
AUTH_HEADER="X-API-Key: $API_KEY"

require_json_ok "health" "$BASE_URL/api/health"
require_json_ok "version" "$BASE_URL/api/version"
require_json_ok "docs_info" "$BASE_URL/api/docs-info"

require_status "api_docs_page" "$BASE_URL/api-docs.html" "200"

if ! curl -fsS "$BASE_URL/api-docs.html" | grep -q "JobPulse API Docs"; then
  fail "api-docs.html does not contain expected title"
fi
pass "api-docs.html content"

require_status "search_without_api_key" "$BASE_URL/api/jobs/search?query=data%20analyst&limit=1" "401"

headers="$tmp_dir/search.headers"
body="$tmp_dir/search.body"

code="$(
  curl -sS \
    -D "$headers" \
    -o "$body" \
    -w "%{http_code}" \
    -H "$AUTH_HEADER" \
    "$BASE_URL/api/jobs/search?query=data%20analyst&location=Germany&sort_by=relevance&sort_order=desc&limit=3"
)"

if [[ "$code" != "200" ]]; then
  echo "Body:" >&2
  cat "$body" >&2 || true
  fail "search_with_api_key returned $code instead of 200"
fi

python3 -m json.tool "$body" >/dev/null
pass "search_with_api_key status 200 and JSON valid"

if grep -qi "x-ratelimit" "$headers"; then
  pass "rate-limit headers present"
else
  echo "⚠️  WARN: rate-limit headers not found in response headers"
fi

python3 - <<PY
import json
from pathlib import Path

data = json.loads(Path("$body").read_text())
results = data.get("results", data if isinstance(data, list) else [])

print("ℹ️  Search result count:", len(results) if isinstance(results, list) else "unknown")

if isinstance(data, dict):
    print("ℹ️  Response keys:", ", ".join(sorted(data.keys())[:20]))
PY

echo
echo "🎉 Public API smoke test completed successfully for: $BASE_URL"
