#!/usr/bin/env bash
# Production Tor dark-launch procedure (Phase 3).
#
# Factored out of .github/workflows/tor-dark-launch.yml so the actual
# operational logic is version-controlled and directly testable (see
# tests/test_tor_dark_launch_script.py, which runs this EXACT file with
# fake docker/curl on PATH) rather than living only inside a GitHub
# Actions heredoc that YAML/static analysis alone cannot exercise.
#
# The workflow's job is limited to: validate the operator-supplied image
# reference, establish SSH, and invoke this exact, already-deployed file
# on the remote host (/opt/jobpulse/scripts/tor/production_dark_launch.sh
# -- put there by the normal deploy pipeline, same as every other
# scripts/ file) rather than piping the script body over stdin -- this
# frees the ssh session's stdin to carry GHCR_TOKEN_B64 instead of an ssh
# argv assignment, so the GHCR token is never a process argv substring on
# the remote host. See tests/test_tor_dark_launch_script.py, which
# executes this file directly (never a duplicated/simplified copy).
#
# Expected inputs (environment variables), all provided by the caller:
#   TOR_IMAGE_B64   (required) base64-encoded exact immutable image
#                   reference, e.g. ghcr.io/mrezamaghouli/jobpulse-tor:<40-hex-sha>.
#                   Revalidated here even though the workflow already
#                   validated the raw input -- this script must be safe
#                   to invoke on its own, never merely trusting a caller.
#   GHCR_TOKEN_B64  (optional) base64-encoded GHCR token. Empty/unset
#                   skips `docker login` entirely (e.g. if the jobpulse-tor
#                   package is public). Never printed, never logged.
#   JOBPULSE_DARK_LAUNCH_DIR (optional, TEST-ONLY) overrides the working
#                   directory this script cd's into. Defaults to
#                   /opt/jobpulse -- the real production workflow never
#                   sets this, so production always uses the default.
#                   Exists solely so tests/test_tor_dark_launch_script.py
#                   can run this exact script against a disposable temp
#                   directory instead of the real host path.
#   JOBPULSE_DARK_LAUNCH_HEALTH_RETRIES / _SLEEP_SECONDS (optional,
#                   TEST-ONLY) override the Tor healthcheck poll loop's
#                   retry count/sleep interval (defaults: 40 retries * 3s
#                   = up to 120s, matching docker-compose.prod.tor.yml's
#                   own bounded Tor healthcheck). Production never sets
#                   these; only the test harness shortens them so the
#                   tor-unhealthy failure path doesn't take two real
#                   minutes to exercise.
#
# Guarantees enforced below, in order:
#   - SECRET_FILE is a fixed, locally-defined path -- /opt/jobpulse/.tor_control_password
#     (or, in tests only, $JOBPULSE_DARK_LAUNCH_DIR/.tor_control_password) --
#     NEVER derived from TOR_IMAGE_B64 or any other untrusted input. It
#     must be a regular file, never a symlink, mode exactly 600, and
#     owned by the user executing this script.
#   - ALL mutating steps (GHCR login, image pull, `docker compose up`) are
#     preceded by a full API preflight -- health, baseline snapshot, and
#     every dark-launch invariant, including the RUNNING api container's
#     own TOR_ENABLED value -- so an already-unhealthy or invariant-
#     violating API aborts this script before any GHCR/Tor mutation.
#   - JOBPULSE_TOR_IMAGE is exported exactly once, immediately after
#     decode+validation, before ANY docker compose invocation that
#     references docker-compose.prod.tor.yml -- every such invocation in
#     this single bash process inherits that one exported value.
#   - The image is pulled and its post-start identity verified against
#     both the exact requested tag reference AND the exact local image ID
#     captured right after the pull -- never merely trusted.
#   - A SINGLE, CENTRALIZED cleanup mechanism guards every failure once a
#     Tor start has been ATTEMPTED: any non-zero exit once
#     TOR_START_ATTEMPTED=1 (set immediately before `docker compose up`
#     is invoked, not only after it succeeds -- compose can partially
#     create/start the container and still return non-zero) stops/removes
#     ONLY the `tor` service, via an EXIT trap -- not a per-call-site
#     repeated rollback -- so a failure mode nobody explicitly anticipated
#     (an unexpected `docker inspect`/`config` error, or `up` itself
#     failing partway through, for instance) is covered
#     exactly the same as a named one. db/api/frontend are never stopped,
#     restarted, recreated, or otherwise mutated by this script, under any
#     failure mode.
#   - No secret value (ControlPort password, GHCR token) is ever printed,
#     echoed, or placed in a docker-inspectable environment variable by
#     this script.
set -Eeuo pipefail

report_err() {
  local rc=$?
  local line="$1"
  local cmd="$2"
  echo "TOR_DARK_LAUNCH_ERROR_TRACE: rc=${rc} line=${line} cmd=${cmd}" >&2
  return "$rc"
}
trap 'report_err "$LINENO" "$BASH_COMMAND"' ERR

fail() {
  echo "TOR_DARK_LAUNCH_FAILED: $*" >&2
  exit 1
}

JOBPULSE_DARK_LAUNCH_DIR="${JOBPULSE_DARK_LAUNCH_DIR:-/opt/jobpulse}"
cd "$JOBPULSE_DARK_LAUNCH_DIR" || fail "cannot cd to $JOBPULSE_DARK_LAUNCH_DIR"

BASE_COMPOSE="docker-compose.prod.yml"
TOR_COMPOSE="docker-compose.prod.tor.yml"

[ -f "$TOR_COMPOSE" ] || fail "missing $TOR_COMPOSE in $JOBPULSE_DARK_LAUNCH_DIR -- deploy the Phase 3 code first"

# --- Decode + STRICTLY revalidate the image reference (defense in depth:
# the calling workflow already validated the raw input, but this script
# must never merely trust that a caller did so correctly). Exactly one
# immutable format is accepted; latest/main/short-SHA/foreign registry or
# repository/shell metacharacters/whitespace/empty all fail closed. ---
[ -n "${TOR_IMAGE_B64:-}" ] || fail "TOR_IMAGE_B64 is required"

TOR_IMAGE="$(printf '%s' "$TOR_IMAGE_B64" | base64 -d 2>/dev/null || true)"

if ! [[ "$TOR_IMAGE" =~ ^ghcr\.io/mrezamaghouli/jobpulse-tor:[0-9a-f]{40}$ ]]; then
  fail "decoded TOR_IMAGE failed strict validation (must be ghcr.io/mrezamaghouli/jobpulse-tor:<40-hex-sha>)"
fi

# Exported EXACTLY ONCE, before any docker compose invocation that
# includes $TOR_COMPOSE -- every subsequent `docker compose` call in this
# same bash process inherits it automatically via the process environment;
# no per-call re-export is needed or performed.
export JOBPULSE_TOR_IMAGE="$TOR_IMAGE"

echo "== production Tor dark launch: image=$JOBPULSE_TOR_IMAGE =="

# --- Secret file hardening: fixed, locally-defined path -- NEVER derived
# from TOR_IMAGE_B64/GHCR_TOKEN_B64 or any other input. Read-only checks --
# never creates, modifies, or prints its contents. Every check below runs
# BEFORE any GHCR/Tor mutation. ---
SECRET_FILE="$JOBPULSE_DARK_LAUNCH_DIR/.tor_control_password"

if [ -L "$SECRET_FILE" ]; then
  fail "production secret $SECRET_FILE must not be a symlink"
fi
[ -f "$SECRET_FILE" ] || fail "production secret missing: $SECRET_FILE (create it manually first, mode 600, before dispatching this workflow)"

SECRET_MODE="$(stat -c '%a' "$SECRET_FILE")"
[ "$SECRET_MODE" = "600" ] || fail "production secret $SECRET_FILE must be mode 600 (found: $SECRET_MODE)"

SECRET_OWNER_UID="$(stat -c '%u' "$SECRET_FILE")"
CURRENT_UID="$(id -u)"
[ "$SECRET_OWNER_UID" = "$CURRENT_UID" ] || fail "production secret $SECRET_FILE must be owned by the user executing this script (uid $CURRENT_UID), found owner uid $SECRET_OWNER_UID"

echo "checkpoint_stage=secret_precondition_ok"
# NOTE: never cat/echo/print $SECRET_FILE's contents anywhere in this script.

snapshot_container() {
  local name="$1"
  docker inspect "$name" --format \
    '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}' \
    2>/dev/null || fail "cannot inspect container: $name"
}

merged_config_json() {
  docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" config --format json
}

check_api_tor_enabled_in_merged_config() {
  local when="$1"
  local marker="$2"
  local value
  value="$(merged_config_json | python3 -c "import json,sys; print(json.load(sys.stdin)['services']['api']['environment'].get('TOR_ENABLED'))")"
  [ "$value" = "false" ] || fail "${marker}: api TOR_ENABLED is not 'false' in merged config $when (got: $value) -- aborting"
}

check_api_no_depends_on_tor_in_merged_config() {
  local when="$1"
  local marker="$2"
  local value
  value="$(merged_config_json | python3 -c "import json,sys; c=json.load(sys.stdin); print('tor' in c['services']['api'].get('depends_on', {}))")"
  [ "$value" = "False" ] || fail "${marker}: api has a depends_on: tor entry in merged config $when -- aborting, this must never be true"
}

check_running_api_tor_enabled() {
  local when="$1"
  local marker="$2"
  local value
  value="$(docker exec jobpulse-api-prod printenv TOR_ENABLED 2>/dev/null || echo unknown)"
  [ "$value" = "false" ] || fail "${marker}: the RUNNING jobpulse-api-prod container's own TOR_ENABLED is not 'false' $when (got: $value)"
}

# =====================================================================
# API PREFLIGHT -- runs BEFORE any GHCR login, image pull, or Tor
# mutation. If the API is already unhealthy or violates any dark-launch
# invariant, this script exits here: no docker login, no docker pull, no
# docker compose up, nothing to roll back.
# =====================================================================
curl -fsS http://127.0.0.1:8000/health >/dev/null || fail "API health check failed BEFORE Tor launch -- aborting, no Tor changes made"
echo "checkpoint_stage=api_health_before_ok"

API_BASELINE="$(snapshot_container jobpulse-api-prod)"

check_api_tor_enabled_in_merged_config "BEFORE launch" "TOR_DARK_LAUNCH_API_TOR_ENABLED_BEFORE_WRONG"
check_api_no_depends_on_tor_in_merged_config "BEFORE launch" "TOR_DARK_LAUNCH_API_DEPENDS_ON_TOR_BEFORE"
check_running_api_tor_enabled "BEFORE launch" "TOR_DARK_LAUNCH_RUNNING_API_TOR_ENABLED_BEFORE_WRONG"
echo "checkpoint_stage=dark_launch_invariants_before_ok"

# =====================================================================
# From here on, GHCR/Tor mutation may occur. tor_only_rollback() and the
# centralized EXIT trap are installed BEFORE any of it, guarded by
# TOR_START_ATTEMPTED so they are a strict no-op until this script is
# actually about to invoke `up -d tor`.
#
# TOR_START_ATTEMPTED is set immediately BEFORE that `up` call, not after
# it succeeds -- `docker compose up` can partially create/start the tor
# container and still return non-zero (e.g. it creates the container but
# the subsequent attach/wait step fails), which would otherwise leave
# orphaned Tor state that nothing ever cleans up if the flag were only
# set on success. `stop tor`/`rm -f tor` are safe no-ops (each `||
# true`-guarded) even when compose failed before creating anything at
# all, so arming cleanup slightly early costs nothing. TOR_STARTED is
# kept as a separate, narrower flag (set only once `up` actually
# succeeds) purely for its own checkpoint/reporting meaning elsewhere in
# this script -- cleanup itself must never depend on it.
# =====================================================================
TOR_START_ATTEMPTED=0
TOR_STARTED=0

tor_only_rollback() {
  echo "$1: stopping/removing ONLY tor, leaving api/db/frontend untouched" >&2
  docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" stop tor || true
  docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" rm -f tor || true
}

# Single centralized cleanup point for EVERY failure once a `tor` start
# has been ATTEMPTED -- named failure paths below (unhealthy, image
# mismatch, diagnostic, post-launch API checks), a partial/failed
# `docker compose up` itself, AND any unanticipated failure (an
# unexpected docker/python error under `set -e`) alike. Deliberately
# does not re-derive $? beyond capturing it once, and every mutating
# command inside tor_only_rollback is `|| true`-guarded, so this handler
# can never itself trigger a recursive failure/trap re-entry. Never
# touches api/db/frontend.
cleanup_on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "$TOR_START_ATTEMPTED" -eq 1 ]; then
    tor_only_rollback "TOR_DARK_LAUNCH_CLEANUP_TRAP rc=${rc}"
  fi
}
trap cleanup_on_exit EXIT

# --- Optional GHCR authentication -- reuses the exact model already
# established in .github/workflows/deploy.yml: password-stdin, never
# printed, never placed in docker-compose.yml. Skipped entirely (not a
# hard requirement) when GHCR_TOKEN_B64 is empty/unset, e.g. if the
# jobpulse-tor GHCR package is public and anonymous pull already works. ---
if [ -n "${GHCR_TOKEN_B64:-}" ]; then
  printf '%s' "$GHCR_TOKEN_B64" | base64 -d | docker login ghcr.io -u mrezamaghouli --password-stdin
  echo "checkpoint_stage=ghcr_login_ok"
else
  echo "checkpoint_stage=ghcr_login_skipped_no_token"
fi

# --- Explicitly ensure the exact immutable image exists locally BEFORE
# any container is started from it -- never merely assume `up -d` will
# succeed at pulling it. Bounded: a hung pull must not hang this script
# forever. Touches ONLY the Tor image -- never the API image/container. ---
timeout 180 docker pull "$JOBPULSE_TOR_IMAGE" || fail "docker pull of $JOBPULSE_TOR_IMAGE failed or timed out"
echo "checkpoint_stage=tor_image_pulled"

# --- Capture the exact local image ID (and RepoDigest, when reported) of
# what was just pulled -- this, not the mutable tag string, is what the
# post-start identity check below authoritatively compares against. ---
PULLED_TOR_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$JOBPULSE_TOR_IMAGE")"
PULLED_TOR_IMAGE_REPO_DIGESTS="$(docker image inspect --format '{{json .RepoDigests}}' "$JOBPULSE_TOR_IMAGE" 2>/dev/null || echo '[]')"
echo "checkpoint_stage=tor_image_id_captured pulled_image_id=$PULLED_TOR_IMAGE_ID"
echo "tor_image_repo_digests=$PULLED_TOR_IMAGE_REPO_DIGESTS"

# --- Start ONLY the tor service ---
TOR_START_ATTEMPTED=1
docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" up -d --no-build tor
TOR_STARTED=1
echo "checkpoint_stage=tor_service_started"

# HEALTH_CHECK_RETRIES/_SLEEP_SECONDS default to real production timing
# (40 retries * 3s = up to 120s, matching the bounded retries/interval
# already used by docker-compose.prod.tor.yml's own Tor healthcheck).
# Overridable ONLY for tests/test_tor_dark_launch_script.py, which needs
# the tor-unhealthy path to fail in well under a second rather than two
# real minutes -- production never sets these, so production always uses
# the defaults below.
HEALTH_CHECK_RETRIES="${JOBPULSE_DARK_LAUNCH_HEALTH_RETRIES:-40}"
HEALTH_CHECK_SLEEP_SECONDS="${JOBPULSE_DARK_LAUNCH_HEALTH_SLEEP_SECONDS:-3}"

TOR_HEALTHY=0
for _ in $(seq 1 "$HEALTH_CHECK_RETRIES"); do
  STATUS="$(docker inspect --format '{{.State.Health.Status}}' jobpulse-tor-prod 2>/dev/null || echo unknown)"
  if [ "$STATUS" = "healthy" ]; then
    TOR_HEALTHY=1
    break
  fi
  sleep "$HEALTH_CHECK_SLEEP_SECONDS"
done

[ "$TOR_HEALTHY" -eq 1 ] || fail "TOR_DARK_LAUNCH_TOR_UNHEALTHY: Tor container never reported healthy -- rolled back Tor only, no database/API changes made"
echo "checkpoint_stage=tor_healthy"

# --- Post-start image identity verification: never merely trust that
# `up -d` used the requested reference -- ask the running container. Two
# independent checks: the tag it reports AND the exact local image ID it
# was created from (a tag alone is not cryptographically immutable). ---
RUNNING_TOR_IMAGE_TAG="$(docker inspect --format '{{.Config.Image}}' jobpulse-tor-prod 2>/dev/null || echo unknown)"
if [ "$RUNNING_TOR_IMAGE_TAG" != "$JOBPULSE_TOR_IMAGE" ]; then
  fail "TOR_DARK_LAUNCH_IMAGE_MISMATCH: running jobpulse-tor-prod image ($RUNNING_TOR_IMAGE_TAG) does not match the requested exact reference ($JOBPULSE_TOR_IMAGE)"
fi

RUNNING_TOR_IMAGE_ID="$(docker inspect --format '{{.Image}}' jobpulse-tor-prod 2>/dev/null || echo unknown)"
if [ "$RUNNING_TOR_IMAGE_ID" != "$PULLED_TOR_IMAGE_ID" ]; then
  fail "TOR_DARK_LAUNCH_IMAGE_ID_MISMATCH: running jobpulse-tor-prod image ID ($RUNNING_TOR_IMAGE_ID) does not match the exact locally-pulled image ID ($PULLED_TOR_IMAGE_ID) for $JOBPULSE_TOR_IMAGE"
fi
echo "checkpoint_stage=tor_image_identity_verified"

# --- Operator-only diagnostic (profile tor-ops; never starts otherwise) ---
set +e
docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" --profile tor-ops run --rm tor-diagnostic
DIAGNOSTIC_RC=$?
set -e

[ "$DIAGNOSTIC_RC" -eq 0 ] || fail "TOR_DARK_LAUNCH_DIAGNOSTIC_FAILED rc=$DIAGNOSTIC_RC: Tor diagnostic failed -- rolled back Tor only; database untouched, API left running (never restarted for this)"
echo "checkpoint_stage=diagnostic_ok"

# --- API health + dark-launch invariants + snapshot comparison: AFTER ---
curl -fsS http://127.0.0.1:8000/health >/dev/null || fail "TOR_DARK_LAUNCH_API_HEALTH_AFTER_FAILED: API health check failed AFTER Tor launch"
echo "checkpoint_stage=api_health_after_ok"

API_AFTER="$(snapshot_container jobpulse-api-prod)"
[ "$API_BASELINE" = "$API_AFTER" ] || fail "TOR_DARK_LAUNCH_API_SNAPSHOT_CHANGED: jobpulse-api-prod container identity/state changed during the Tor dark launch (before=$API_BASELINE after=$API_AFTER) -- the API must never be touched by this procedure"
echo "checkpoint_stage=api_container_unchanged_ok"

check_api_tor_enabled_in_merged_config "AFTER launch" "TOR_DARK_LAUNCH_API_TOR_ENABLED_AFTER_WRONG"
check_api_no_depends_on_tor_in_merged_config "AFTER launch" "TOR_DARK_LAUNCH_API_DEPENDS_ON_TOR_AFTER"
check_running_api_tor_enabled "AFTER launch" "TOR_DARK_LAUNCH_RUNNING_API_TOR_ENABLED_WRONG"
echo "checkpoint_stage=dark_launch_invariants_after_ok"

docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" ps

# Disarm the cleanup trap only now, immediately before announcing
# success -- every prior line remains protected by it.
trap - EXIT

echo "PRODUCTION_TOR_DARK_LAUNCH_READY"
