#!/usr/bin/env bash
# Phase 3.1: production Tor ControlPort secret provisioning.
#
# Shipped fresh from the Actions runner's checkout over the SSH session's
# stdin on every dispatch (`ssh ... "bash -s" < this file`), unlike
# scripts/tor/production_dark_launch.sh which deliberately depends on
# already being deployed to /opt/jobpulse. That dependency would be a
# chicken-and-egg problem here: this phase must run while the VM is still
# pinned at a SHA that predates this file existing in any deploy. See
# tests/test_tor_secret_provision.py, which runs this EXACT file (never a
# duplicated/simplified copy) with fake docker/curl/stat/git on PATH.
#
# This script NEVER starts, stops, or mutates the api/db/frontend/tor
# containers. Its only possible filesystem mutation is creating exactly
# one new file: $ROOT/.tor_control_password -- and only when that path
# does not already exist. An existing secret is validated, never
# overwritten, rotated, or printed.
#
# Expected inputs (environment variables), all provided by the caller:
#   EXPECTED_PRODUCTION_SHA (required) the pinned 40-char lowercase hex
#                   commit SHA the production checkout must already be at.
#                   Not a secret -- deliberately NOT a workflow_dispatch
#                   input (an operator-editable string here would let a
#                   fat-fingered dispatch silently target the wrong
#                   commit); the calling workflow pins this literal value.
#                   Compared only against `git rev-parse HEAD` in $ROOT --
#                   never against origin/main, which will move as soon as
#                   this workflow file itself is merged.
#   TOR_SECRET_PROVISION_TEST_MODE (optional, TEST-ONLY) must be "1" to
#                   enable TOR_SECRET_PROVISION_TEST_ROOT below. Unset in
#                   every real production invocation.
#   TOR_SECRET_PROVISION_TEST_ROOT (optional, TEST-ONLY) overrides $ROOT.
#                   Only honored when TEST_MODE=1; if set without
#                   TEST_MODE=1 this script fails closed rather than
#                   silently ignoring it, so a misconfigured production
#                   invocation can never be quietly redirected off
#                   /opt/jobpulse.
#
# Guarantees enforced below, in order:
#   - Production SHA gate runs before anything else that could mutate
#     the filesystem.
#   - Every production invariant check (API health, TOR_ENABLED, Tor
#     container absence, api/db/frontend identity snapshots) runs both
#     BEFORE and AFTER the secret decision, and BEFORE/AFTER snapshots
#     must be byte-for-byte identical -- this script performs zero
#     container mutation, so any difference is itself a bug to fail
#     loudly on rather than silently accept.
#   - An existing secret is validated (regular file, not symlink, mode
#     600, owned by the executing user, non-empty, bounded size) and left
#     completely untouched -- this is intentionally STRICTER than the
#     consumer (scripts/tor/production_dark_launch.sh), which accepts the
#     same checks but has no reason to also bound the size; provisioning
#     adds the size bound as defense-in-depth against something other
#     than this script having written the file.
#   - First creation uses a same-directory mktemp, generates directly into
#     it, validates the temp file, then publishes via `ln` (hardlink) --
#     never `mv -f`/`cp`/`>` -- so a concurrent second creation loses the
#     `ln` race with EEXIST instead of silently clobbering.
#   - No secret value (the generated password, or its base64/hash/partial
#     form) is ever printed, echoed, or placed in a docker-inspectable
#     environment variable by this script.
set -Eeuo pipefail

report_err() {
  local rc=$?
  local line="$1"
  local cmd="$2"
  echo "TOR_SECRET_PROVISION_ERROR_TRACE: rc=${rc} line=${line} cmd=${cmd}" >&2
  return "$rc"
}
trap 'report_err "$LINENO" "$BASH_COMMAND"' ERR

fail() {
  echo "TOR_SECRET_PROVISION_FAILED: $*" >&2
  exit 1
}

# --- Resolve ROOT. Production never sets TEST_MODE/TEST_ROOT; a custom
# root is refused outright unless TEST_MODE=1, so a misconfigured
# production dispatch can never be silently redirected off /opt/jobpulse. ---
TOR_SECRET_PROVISION_TEST_MODE="${TOR_SECRET_PROVISION_TEST_MODE:-0}"
if [ "$TOR_SECRET_PROVISION_TEST_MODE" = "1" ]; then
  ROOT="${TOR_SECRET_PROVISION_TEST_ROOT:?TOR_SECRET_PROVISION_TEST_ROOT is required when TOR_SECRET_PROVISION_TEST_MODE=1}"
else
  if [ -n "${TOR_SECRET_PROVISION_TEST_ROOT:-}" ]; then
    fail "TOR_SECRET_PROVISION_TEST_ROOT must not be set outside test mode"
  fi
  ROOT="/opt/jobpulse"
fi

cd "$ROOT" || fail "cannot cd to $ROOT"

# --- Production SHA gate -- runs before ANY filesystem mutation. Compared
# only against the pinned literal the calling workflow provides, never
# against origin/main (which moves as soon as this workflow is merged). ---
[ -n "${EXPECTED_PRODUCTION_SHA:-}" ] || fail "EXPECTED_PRODUCTION_SHA is required"
if ! [[ "$EXPECTED_PRODUCTION_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  fail "EXPECTED_PRODUCTION_SHA must be exactly 40 lowercase hex characters"
fi

CURRENT_SHA="$(git rev-parse HEAD)"
[ "$CURRENT_SHA" = "$EXPECTED_PRODUCTION_SHA" ] || fail "TOR_SECRET_PROVISION_SHA_MISMATCH: $ROOT HEAD is $CURRENT_SHA, expected pinned production SHA $EXPECTED_PRODUCTION_SHA -- refusing to proceed"
echo "checkpoint_stage=production_sha_ok sha=$CURRENT_SHA"

# =====================================================================
# Production invariants -- read-only, never mutate a container. Run in
# full both BEFORE and AFTER the secret decision below.
# =====================================================================
snapshot_container() {
  local name="$1"
  # `{{with (index .State "Health")}}` looks up "Health" as a map key and
  # only enters the block if the key is present -- unlike
  # `{{if .State.Health}}`, which errors ("map has no entry for key
  # Health") on Docker versions where a container with no HEALTHCHECK
  # omits the key entirely rather than setting it to a zero value.
  docker inspect "$name" --format \
    '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{with (index .State "Health")}}{{.Status}}{{else}}n/a{{end}}' \
    2>/dev/null || fail "cannot inspect container: $name"
}

check_api_health() {
  local when="$1"
  curl -fsS http://127.0.0.1:8000/health >/dev/null || fail "TOR_SECRET_PROVISION_API_HEALTH_FAILED: API health check failed $when"
}

check_running_api_tor_enabled() {
  local when="$1"
  local value
  value="$(docker exec jobpulse-api-prod printenv TOR_ENABLED 2>/dev/null || echo unknown)"
  [ "$value" = "false" ] || fail "TOR_SECRET_PROVISION_TOR_ENABLED_WRONG: the running jobpulse-api-prod container's TOR_ENABLED is not 'false' $when (got: $value)"
}

check_tor_container_absent() {
  local when="$1"
  local out
  out="$(docker ps -a --filter "name=^/jobpulse-tor-prod$" --format '{{.Names}}' 2>/dev/null || true)"
  [ -z "$out" ] || fail "TOR_SECRET_PROVISION_TOR_CONTAINER_PRESENT: found existing jobpulse-tor-prod container(s) $when: $out -- this phase must not run while any such container (running or stopped) exists, and never removes it automatically"
}

run_invariants() {
  local when="$1"
  check_api_health "$when"
  check_running_api_tor_enabled "$when"
  check_tor_container_absent "$when"
}

run_invariants "BEFORE secret provisioning"
API_BASELINE="$(snapshot_container jobpulse-api-prod)"
DB_BASELINE="$(snapshot_container jobpulse-postgres-prod)"
FRONTEND_BASELINE="$(snapshot_container jobpulse-frontend-prod)"
echo "checkpoint_stage=invariants_before_ok"

# =====================================================================
# Secret decision -- the ONLY possible filesystem mutation in this
# script, and only in the "does not exist" branch.
# =====================================================================
SECRET_FILE="$ROOT/.tor_control_password"

if [ -L "$SECRET_FILE" ]; then
  fail "production secret $SECRET_FILE must not be a symlink"
fi

report_status() {
  local status="$1"
  local mode owner_uid owner_name bytes
  mode="$(stat -c '%a' "$SECRET_FILE")"
  owner_uid="$(stat -c '%u' "$SECRET_FILE")"
  owner_name="$(stat -c '%U' "$SECRET_FILE" 2>/dev/null || echo unknown)"
  bytes="$(stat -c '%s' "$SECRET_FILE")"
  echo "TOR_SECRET_STATUS=$status"
  echo "path=$SECRET_FILE owner_uid=$owner_uid owner_name=$owner_name mode=$mode bytes=$bytes"
}

if [ -e "$SECRET_FILE" ]; then
  # --- Existing secret: VALIDATE ONLY. Never overwrite, rotate, chmod,
  # chown, or print its contents -- stricter than the consumer script's
  # own checks (same regular-file/symlink/mode/owner rules, plus a bound
  # on size here) but never less permissive of an already-valid file. ---
  [ -f "$SECRET_FILE" ] || fail "production secret $SECRET_FILE exists but is not a regular file"

  SECRET_MODE="$(stat -c '%a' "$SECRET_FILE")"
  [ "$SECRET_MODE" = "600" ] || fail "production secret $SECRET_FILE must be mode 600 (found: $SECRET_MODE)"

  SECRET_OWNER_UID="$(stat -c '%u' "$SECRET_FILE")"
  CURRENT_UID="$(id -u)"
  [ "$SECRET_OWNER_UID" = "$CURRENT_UID" ] || fail "production secret $SECRET_FILE must be owned by the user executing this script (uid $CURRENT_UID), found owner uid $SECRET_OWNER_UID"

  SECRET_BYTES="$(stat -c '%s' "$SECRET_FILE")"
  [ "$SECRET_BYTES" -gt 0 ] || fail "production secret $SECRET_FILE is empty"
  [ "$SECRET_BYTES" -le 4096 ] || fail "production secret $SECRET_FILE is larger than expected ($SECRET_BYTES bytes) -- refusing to treat as valid"

  report_status "already_present_valid"
else
  # --- First creation: same-directory mktemp, generate directly into it,
  # validate the temp file WITHOUT printing its contents, publish via
  # `ln` (hardlink) so a concurrent creation loses the race with EEXIST
  # instead of silently clobbering. Never `mv -f`/`cp`/`>` onto
  # $SECRET_FILE. ---
  umask 077
  TMP_FILE="$(mktemp "$ROOT/.tor_control_password.tmp.XXXXXX")"
  trap 'rm -f "$TMP_FILE"' EXIT

  openssl rand -hex 32 > "$TMP_FILE"
  chmod 600 "$TMP_FILE"

  [ -f "$TMP_FILE" ] && [ ! -L "$TMP_FILE" ] || fail "temporary secret file $TMP_FILE is not a regular file"
  TMP_MODE="$(stat -c '%a' "$TMP_FILE")"
  [ "$TMP_MODE" = "600" ] || fail "temporary secret file $TMP_FILE must be mode 600 (found: $TMP_MODE)"
  TMP_OWNER_UID="$(stat -c '%u' "$TMP_FILE")"
  [ "$TMP_OWNER_UID" = "$(id -u)" ] || fail "temporary secret file $TMP_FILE has unexpected owner uid $TMP_OWNER_UID"
  TMP_BYTES="$(stat -c '%s' "$TMP_FILE")"
  # openssl rand -hex 32 always produces exactly 64 lowercase hex
  # characters plus its normal trailing newline == 65 bytes, never more,
  # never fewer -- an exact-size check here also rules out any extra
  # line/trailing data without needing to inspect the content for that.
  [ "$TMP_BYTES" -eq 65 ] || fail "temporary secret file $TMP_FILE has unexpected size ($TMP_BYTES bytes, expected exactly 65) for openssl rand -hex 32"

  # Read the logical first line via the `read` builtin -- never `cat`,
  # `head`, or any other external command against the secret. Never
  # echoed/printed; unset immediately after the shape check.
  GENERATED_VALUE=""
  IFS= read -r GENERATED_VALUE < "$TMP_FILE"
  if ! [[ "$GENERATED_VALUE" =~ ^[0-9a-f]{64}$ ]]; then
    fail "generated secret does not resolve to exactly 64 lowercase hex characters"
  fi
  unset GENERATED_VALUE

  if ! ln "$TMP_FILE" "$SECRET_FILE" 2>/dev/null; then
    fail "TOR_SECRET_PROVISION_CONCURRENT_CREATION: $SECRET_FILE appeared concurrently -- refusing to overwrite, temp file discarded"
  fi
  rm -f "$TMP_FILE"
  trap - EXIT

  [ -f "$SECRET_FILE" ] && [ ! -L "$SECRET_FILE" ] || fail "post-creation verification failed: $SECRET_FILE is not a regular file"
  FINAL_MODE="$(stat -c '%a' "$SECRET_FILE")"
  [ "$FINAL_MODE" = "600" ] || fail "post-creation verification failed: $SECRET_FILE mode is $FINAL_MODE, expected 600"
  FINAL_OWNER_UID="$(stat -c '%u' "$SECRET_FILE")"
  [ "$FINAL_OWNER_UID" = "$(id -u)" ] || fail "post-creation verification failed: $SECRET_FILE owner uid $FINAL_OWNER_UID"
  FINAL_BYTES="$(stat -c '%s' "$SECRET_FILE")"
  [ "$FINAL_BYTES" -gt 0 ] || fail "post-creation verification failed: $SECRET_FILE is empty"

  report_status "created"
fi

# =====================================================================
# Production invariants -- AFTER. Must be identical to the BEFORE
# snapshots: this script performs zero container mutation, so any
# difference here is a bug, not an expected side effect.
# =====================================================================
run_invariants "AFTER secret provisioning"

API_AFTER="$(snapshot_container jobpulse-api-prod)"
[ "$API_BASELINE" = "$API_AFTER" ] || fail "TOR_SECRET_PROVISION_API_SNAPSHOT_CHANGED: jobpulse-api-prod identity/state changed (before=$API_BASELINE after=$API_AFTER) -- this script must never mutate it"

DB_AFTER="$(snapshot_container jobpulse-postgres-prod)"
[ "$DB_BASELINE" = "$DB_AFTER" ] || fail "TOR_SECRET_PROVISION_DB_SNAPSHOT_CHANGED: jobpulse-postgres-prod identity/state changed (before=$DB_BASELINE after=$DB_AFTER) -- this script must never mutate it"

FRONTEND_AFTER="$(snapshot_container jobpulse-frontend-prod)"
[ "$FRONTEND_BASELINE" = "$FRONTEND_AFTER" ] || fail "TOR_SECRET_PROVISION_FRONTEND_SNAPSHOT_CHANGED: jobpulse-frontend-prod identity/state changed (before=$FRONTEND_BASELINE after=$FRONTEND_AFTER) -- this script must never mutate it"

echo "checkpoint_stage=invariants_after_ok"
echo "TOR_SECRET_PROVISION_COMPLETE"
