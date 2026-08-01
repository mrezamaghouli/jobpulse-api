#!/usr/bin/env bash
# Infrastructure health check + Telegram delivery.
#
# Reliability contract (see docs/PRODUCTION_RUNBOOK.md for the operator
# summary):
#   - The cooldown timestamp records the last CONFIRMED-delivered
#     failure alert, never a bare attempt. A failed delivery leaves it
#     untouched, so the next scheduled poll retries immediately.
#   - "Delivered" means: HTTP request succeeded AND the decoded JSON body
#     has "ok": true. Network errors, timeouts, non-2xx responses,
#     malformed JSON, and "ok": false are all failures.
#   - A recovery notification is only sent for an incident whose failure
#     alert was actually confirmed delivered; it clears the delivered
#     marker only after the recovery message itself is confirmed delivered.
#   - All state is one atomically-written JSON file; a single flock-based
#     lock serializes invocations so overlapping runs (e.g. an operator
#     manually running this while cron also fires) can't race the state
#     file or send duplicate alerts.
#
# This script does not, by itself, install any cron/systemd schedule.
# See docs/PRODUCTION_RUNBOOK.md for an example schedule an operator must
# install explicitly.
set -u

# validate_duration_seconds <raw_value> <default> <min> <max>
# Prints a safe, validated non-negative integer on stdout. ANY input that
# isn't a plain unsigned integer of at most 9 digits (avoiding bash
# arithmetic overflow on `-ge`/`-le` for absurdly long digit strings)
# within [min,max] falls back to <default>. The raw value is never
# printed or logged by this function -- callers must not echo it either
# -- so a malformed/garbage env var never ends up in a log line meant for
# a number. Zero is deliberately never valid on its own here: every
# caller passes min>=1, so "disable this timeout" is not an available
# configuration -- monitoring must never be silently turned off via a
# duration variable.
#
# Only LEADING and TRAILING whitespace is trimmed -- never internal
# whitespace. A prior version used `tr -d '[:space:]'`, which deletes
# whitespace anywhere in the string, so a malformed value like "1 5"
# silently became the valid-looking "15" instead of being rejected. The
# parameter-expansion trim below removes only a leading and a trailing
# run of whitespace; any internal space/tab/newline that survives still
# fails the `^[0-9]{1,9}$` check below and correctly falls back to
# <default>.
validate_duration_seconds() {
  local raw="$1" default="$2" min="$3" max="$4"
  local trimmed="$raw"
  trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
  trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"

  if [[ "$trimmed" =~ ^[0-9]{1,9}$ ]] && [ "$trimmed" -ge "$min" ] && [ "$trimmed" -le "$max" ]; then
    printf '%s' "$trimmed"
  else
    printf '%s' "$default"
  fi
}

# ---- configurable paths (env-overridable; safe production defaults) ----
ROOT="${JOBPULSE_ALERT_ROOT:-/opt/jobpulse}"
ENV_FILE="${JOBPULSE_ALERT_ENV_FILE:-$ROOT/.env}"
COMPOSE_FILE="${JOBPULSE_ALERT_COMPOSE_FILE:-$ROOT/docker-compose.prod.yml}"
ALERT_LOG="${JOBPULSE_ALERT_LOG:-$ROOT/logs/health_alert.log}"
STATE_DIR="${JOBPULSE_ALERT_STATE_DIR:-$ROOT/state}"
STATE_FILE="${JOBPULSE_ALERT_STATE_FILE:-$STATE_DIR/health_alert_state.json}"
LOCK_FILE="${JOBPULSE_ALERT_LOCK_FILE:-$STATE_DIR/health_alert.lock}"

# ---- validated, bounded durations (see docs/PRODUCTION_RUNBOOK.md for
# the exact defaults/ranges and the resulting documented maximum lock
# duration) ----
COOLDOWN_SECONDS="$(validate_duration_seconds "${HEALTH_ALERT_COOLDOWN_SECONDS:-}" 3600 1 86400)"
REQUEST_TIMEOUT_SECONDS="$(validate_duration_seconds "${HEALTH_ALERT_REQUEST_TIMEOUT_SECONDS:-}" 20 1 120)"
API_HEALTH_TIMEOUT_SECONDS="$(validate_duration_seconds "${HEALTH_ALERT_API_TIMEOUT_SECONDS:-}" 10 1 120)"
HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS="$(validate_duration_seconds "${HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS:-}" 15 1 120)"
HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS="$(validate_duration_seconds "${HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS:-}" 15 1 120)"
HEALTH_ALERT_KILL_AFTER_SECONDS="$(validate_duration_seconds "${HEALTH_ALERT_KILL_AFTER_SECONDS:-}" 5 1 30)"

API_HEALTH_URL="${JOBPULSE_API_HEALTH_URL:-http://localhost/api/health}"
PYTHON_BIN="${JOBPULSE_PYTHON_BIN:-python3}"

# GNU coreutils `timeout` is required to bound every Docker/DB command
# below. Resolved once, up front: if it's missing, every Docker-related
# check fails clearly (see run_with_timeout) instead of silently running
# unbounded.
TIMEOUT_BIN="$(command -v timeout 2>/dev/null || true)"

mkdir -p "$(dirname "$ALERT_LOG")" 2>/dev/null || true

if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR state_dir_create_failed dir=$STATE_DIR" >&2
  exit 1
fi

ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(ts) $*" | tee -a "$ALERT_LOG"
}

# Strips every configured secret (bot token, chat id) out of any text
# before it is ever logged. Both are replaced -- not just the bot token --
# since either could in principle be reflected back in a curl error
# string or an unexpected API response.
redact() {
  local text="$1"
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    text="${text//$TELEGRAM_BOT_TOKEN/***REDACTED***}"
  fi
  if [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    text="${text//$TELEGRAM_CHAT_ID/***REDACTED***}"
  fi
  printf '%s' "$text"
}

truncate_text() {
  local text="$1"
  local max="${2:-300}"
  printf '%s' "${text:0:$max}"
}

# ---- top-level, non-blocking inter-process lock ----
# Tied to the file descriptor: bash closes it on any exit path (normal,
# error, most signals), which always releases the flock -- there is no
# separate cleanup step that can leave it permanently held.
exec {LOCK_FD}>"$LOCK_FILE" || {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR lock_file_open_failed path=$LOCK_FILE" >&2
  exit 1
}

# Exit 0 here is a deliberate choice, not an oversight: it means "another
# invocation of this exact check is already in flight," not "monitoring
# status is unknown." Every external call this script makes is now bounded
# by an explicit timeout -- curl's own --max-time for the two HTTP calls
# (REQUEST_TIMEOUT_SECONDS, API_HEALTH_TIMEOUT_SECONDS), and GNU `timeout`
# for every Docker/DB command (HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS,
# HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS via run_with_timeout, below) -- so
# the lock has a documented, finite maximum hold time (see "Lock-busy exit
# semantics" in docs/PRODUCTION_RUNBOOK.md for the exact figure). Under
# normal operation the lock is held only for the few seconds a single run
# takes, so contention usually means an operator manually running this
# while cron also fires; reporting that as a check *failure* would be a
# false positive, since the check is actively being performed by the
# other invocation -- and is now guaranteed to finish within the
# documented maximum, not run forever.
if ! flock -n "$LOCK_FD"; then
  log "health_alert_already_running lock=$LOCK_FILE"
  exit 0
fi

# run_with_timeout <duration_seconds> <kill_after_seconds> <cmd...>
# Runs <cmd...> under GNU coreutils `timeout`, bounding it to
# <duration_seconds> and escalating to SIGKILL after an additional
# <kill_after_seconds> grace period if it ignores SIGTERM. Deliberately
# does NOT pass --foreground: without it, `timeout` puts <cmd...> in its
# own new process group and signals the WHOLE group on timeout, so any
# subprocess the command itself forks (as `docker` sometimes does) is
# killed too. This was verified empirically during the adversarial-pass
# reproduction: --foreground left a forked grandchild process running
# after `timeout` reaped only the direct child, which would violate "no
# child process may remain running."
#
# Sets TIMEOUT_LAST_CATEGORY to "" on a normal (bounded) run -- regardless
# of the wrapped command's own exit code -- or "timeout" if the bound was
# hit (exit 124/137/143), or "timeout_utility_missing" if GNU `timeout`
# isn't installed. Callers check this AFTER checking the return code to
# tell "the command ran and failed" apart from "the command never
# finished" or "we refused to run it unbounded."
#
# Must be called directly in the current shell (redirect its own stdout
# to a file), never inside `$(...)` -- command substitution runs in a
# subshell, and TIMEOUT_LAST_CATEGORY set there would be invisible to the
# caller once the subshell exits.
TIMEOUT_LAST_CATEGORY=""

run_with_timeout() {
  local duration="$1" kill_after="$2"
  shift 2
  TIMEOUT_LAST_CATEGORY=""

  if [ -z "$TIMEOUT_BIN" ]; then
    TIMEOUT_LAST_CATEGORY="timeout_utility_missing"
    return 1
  fi

  "$TIMEOUT_BIN" -k "${kill_after}" "${duration}" "$@"
  local rc=$?

  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] || [ "$rc" -eq 143 ]; then
    TIMEOUT_LAST_CATEGORY="timeout"
  fi

  return "$rc"
}

# Populates DOCKER_STATUS_CONTEXT with a bounded `docker compose ps`
# snapshot for inclusion in outgoing alert/recovery messages. This is
# contextual detail, not the primary health signal, so it never blocks
# message construction indefinitely -- a timeout/failure here falls back
# to a placeholder string (with a sanitized category) instead.
DOCKER_STATUS_CONTEXT=""

get_docker_status_context() {
  local out_file
  out_file="$(mktemp)" || {
    DOCKER_STATUS_CONTEXT="(docker compose ps unavailable: category=temp_file_failed)"
    return
  }

  run_with_timeout "$HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS" "$HEALTH_ALERT_KILL_AFTER_SECONDS" \
    docker compose -f "$COMPOSE_FILE" ps >"$out_file" 2>&1
  local rc=$?

  if [ "$rc" -ne 0 ]; then
    DOCKER_STATUS_CONTEXT="(docker compose ps unavailable: category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit})"
    log "ERROR docker_ps_context_failed category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
  else
    DOCKER_STATUS_CONTEXT="$(tail -n 30 "$out_file")"
  fi

  rm -f "$out_file"
}

# ---- offline capability check: no network call, just curl's own --help ----
CURL_SUPPORTS_FAIL_WITH_BODY="0"
if curl --help all 2>/dev/null | grep -q -- '--fail-with-body'; then
  CURL_SUPPORTS_FAIL_WITH_BODY="1"
fi

# Prints "true"/"false" for whether $1 (a file path) contains a JSON object
# with "ok": true. Never raises; malformed/non-JSON content is "false",
# never misclassified via a plain grep substring match.
telegram_response_is_ok() {
  "$PYTHON_BIN" -c "
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    print('false')
    raise SystemExit(0)
print('true' if isinstance(data, dict) and data.get('ok') is True else 'false')
" "$1" 2>/dev/null
}

# Extracts ONLY a short, human-readable diagnostic from a Telegram response
# body -- never the raw/full body. On a failure response Telegram's body
# looks like {"ok":false,"description":"Bad Request: chat not found"}; only
# that bounded "description" string (already tightly length-capped here,
# and still passed through redact() by the caller) is ever surfaced. A
# non-JSON or unexpected-shape body never has its raw content logged at
# all -- just a fixed marker -- so this can never leak request payloads,
# chat identifiers, or arbitrary response content into logs.
telegram_response_description() {
  "$PYTHON_BIN" -c "
import json, sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    print('unparseable_response_body')
    raise SystemExit(0)
if isinstance(data, dict) and isinstance(data.get('description'), str):
    print(data['description'][:120])
else:
    print('no_description_field')
" "$1" 2>/dev/null
}

# Set by send_telegram() on return; avoids using command substitution to
# get the outcome (which would capture log()'s stdout output too).
TELEGRAM_LAST_CATEGORY=""

send_telegram() {
  local message="$1"
  TELEGRAM_LAST_CATEGORY=""

  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    log "telegram_not_configured"
    TELEGRAM_LAST_CATEGORY="not_configured"
    return 1
  fi

  local url="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
  local body_file err_file
  body_file="$(mktemp)" || { log "ERROR telegram_temp_file_failed"; TELEGRAM_LAST_CATEGORY="unknown"; return 1; }
  err_file="$(mktemp)" || { log "ERROR telegram_temp_file_failed"; rm -f "$body_file"; TELEGRAM_LAST_CATEGORY="unknown"; return 1; }

  local curl_extra=()
  if [ "$CURL_SUPPORTS_FAIL_WITH_BODY" = "1" ]; then
    curl_extra+=(--fail-with-body)
  fi

  local http_code
  http_code=$(curl -sS --max-time "$REQUEST_TIMEOUT_SECONDS" "${curl_extra[@]}" \
    -o "$body_file" -w '%{http_code}' \
    -X POST "$url" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${message}" \
    2>"$err_file")
  local curl_rc=$?

  local category="" ok="false"

  # Success requires ALL THREE: curl itself exits 0, HTTP status is 2xx,
  # and the decoded body is a JSON object with "ok": true. A 2xx-looking
  # status code or an "ok": true body is never sufficient on its own --
  # curl can print a status code and still report a non-zero exit (e.g.
  # 23: local write failure; 18: partial/truncated transfer), meaning the
  # delivery was never actually confirmed even though the body it managed
  # to write looks successful. Classification order matters: curl_rc==28
  # (timeout) and a non-2xx http_code are checked FIRST and always win
  # their category, even if curl_rc is also non-zero for another reason
  # (e.g. --fail-with-body causes curl to exit non-zero on a non-2xx
  # response while still writing the body -- that stays "http_status",
  # not a separate transport category, and still never reaches the "ok"
  # branch below). Only after ruling those out does a lingering non-zero
  # curl_rc against an apparently-2xx response get its own category
  # ("transport_error"), so a contradictory "200 but curl failed" result
  # can never fall through to being treated as a success.
  if [ "$curl_rc" -eq 28 ]; then
    category="timeout"
    # curl's own diagnostic text (e.g. "Operation timed out ..."). Never
    # the request URL or payload -- just curl's transport-level error --
    # and still redacted and tightly bounded as defense in depth.
    log "telegram_send_failed category=$category error=$(redact "$(truncate_text "$(cat "$err_file" 2>/dev/null)" 200)")"
  elif [ -z "$http_code" ] || [ "$http_code" = "000" ]; then
    category="network"
    log "telegram_send_failed category=$category error=$(redact "$(truncate_text "$(cat "$err_file" 2>/dev/null)" 200)")"
  elif ! [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    category="http_status"
    log "telegram_send_failed category=$category http_code=$http_code description=$(redact "$(telegram_response_description "$body_file")")"
  elif [ "$curl_rc" -ne 0 ]; then
    # http_code looks like 2xx, but curl did not report success. Never
    # logs body/description content here -- the body is not trustworthy
    # given the contradictory transport-level failure, so only the
    # non-sensitive http_code and curl's own exit code are recorded.
    category="transport_error"
    log "telegram_send_failed category=$category http_code=$http_code curl_exit=$curl_rc"
  elif [ "$(telegram_response_is_ok "$body_file")" = "true" ]; then
    ok="true"
    log "telegram_send_ok http_code=$http_code"
  else
    category="invalid_response"
    log "telegram_send_failed category=$category http_code=$http_code description=$(redact "$(telegram_response_description "$body_file")")"
  fi

  rm -f "$body_file" "$err_file"

  if [ "$ok" = "true" ]; then
    TELEGRAM_LAST_CATEGORY="ok"
    return 0
  fi

  TELEGRAM_LAST_CATEGORY="$category"
  return 1
}

# ---- atomic JSON state helpers (python3: temp file + fsync + os.replace) ----
# STATE_READ_PY emits five NUL-terminated fields, in fixed order, on stdout.
# This (and the read -d '' loop in load_state() below) is deliberate: the
# state file is data, written from a JSON patch, and must never be treated
# as a source of shell syntax. Earlier this was `eval "$(load_state)"` over
# KEY=value lines -- a string field like `FAILED; rm -rf /` or containing
# `$(...)`/backticks was executed as shell code on the very next read. NUL
# delimiting plus direct array assignment means the field content is never
# parsed by the shell at all, however many quotes, `$`, backticks, `;`, or
# newlines it contains. Each field is also type-validated in Python before
# being stringified: a field whose JSON type doesn't match what's expected
# (e.g. last_success_epoch holding a string or a list) falls back to its
# safe default instead of being passed through.
STATE_READ_PY='
import json, sys

path = sys.argv[1]
default = {
    "last_detected_status": None,
    "failure_delivered": False,
    "last_success_epoch": None,
    "last_attempt_epoch": None,
    "last_failure_category": None,
}

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
except Exception:
    data = {}

merged = dict(default)
for key in default:
    if key in data:
        merged[key] = data[key]

def as_str_field(value):
    # Only a real string survives; anything else (wrong type, or absent)
    # falls back to the safe default of "no value".
    if isinstance(value, str):
        return value.replace("\x00", "")
    return ""

def as_bool_field(value):
    # Only the exact boolean True counts as set; any other JSON type
    # (including truthy strings/numbers) is treated as false.
    return "1" if value is True else "0"

def as_int_field(value):
    # bool is a subclass of int in Python -- explicitly excluded so a
    # stray `true`/`false` in an epoch field can never be misread as 1/0.
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""

fields = [
    as_str_field(merged["last_detected_status"]),
    as_bool_field(merged["failure_delivered"]),
    as_int_field(merged["last_success_epoch"]),
    as_int_field(merged["last_attempt_epoch"]),
    as_str_field(merged["last_failure_category"]),
]

out = sys.stdout.buffer
for field in fields:
    out.write(field.encode("utf-8", errors="replace"))
    out.write(b"\x00")
out.flush()
'

STATE_WRITE_PY='
import json, os, sys, tempfile

path = sys.argv[1]
patch = json.loads(sys.argv[2])

default = {
    "last_detected_status": None,
    "failure_delivered": False,
    "last_success_epoch": None,
    "last_attempt_epoch": None,
    "last_failure_category": None,
}

try:
    with open(path, "r", encoding="utf-8") as f:
        current = json.load(f)
    if not isinstance(current, dict):
        current = {}
except Exception:
    current = {}

merged = dict(default)
for key in default:
    if key in current:
        merged[key] = current[key]
for key, value in patch.items():
    if key in default:
        merged[key] = value

directory = os.path.dirname(path) or "."
os.makedirs(directory, exist_ok=True)

fd, tmp_path = tempfile.mkstemp(prefix=".health_alert_state.", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(merged, f, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)
except BaseException:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise
'

# load_state: populates ST_* globals directly from NUL-delimited fields.
# Uses process substitution (not a pipe) so the `read` loop runs in the
# current shell, and `read -d ''` (not `$(...)`) so embedded NUL bytes
# can't silently truncate a field the way command substitution would.
# Never eval'd -- field content is assigned to variables verbatim, never
# parsed as shell source, regardless of what characters it contains.
load_state() {
  ST_LAST_DETECTED_STATUS=""
  ST_FAILURE_DELIVERED="0"
  ST_LAST_SUCCESS_EPOCH=""
  ST_LAST_ATTEMPT_EPOCH=""
  ST_LAST_FAILURE_CATEGORY=""

  local -a fields=()
  local field
  while IFS= read -r -d '' field; do
    fields+=("$field")
  done < <("$PYTHON_BIN" -c "$STATE_READ_PY" "$STATE_FILE" 2>/dev/null)

  ST_LAST_DETECTED_STATUS="${fields[0]:-}"
  ST_FAILURE_DELIVERED="${fields[1]:-0}"
  ST_LAST_SUCCESS_EPOCH="${fields[2]:-}"
  ST_LAST_ATTEMPT_EPOCH="${fields[3]:-}"
  ST_LAST_FAILURE_CATEGORY="${fields[4]:-}"
}

# write_state '<json object>' -- returns non-zero on any write failure.
# Callers MUST check the return code: a write failure must never be
# treated as successfully persisted delivery state.
write_state() {
  "$PYTHON_BIN" -c "$STATE_WRITE_PY" "$STATE_FILE" "$1" 2>>"$ALERT_LOG"
}

patch_json() {
  # patch_json key type value [key type value ...]
  # type is "str", "bool", "int", or "null".
  "$PYTHON_BIN" -c "
import json, sys
args = sys.argv[1:]
patch = {}
for i in range(0, len(args), 3):
    key, kind, raw = args[i], args[i + 1], args[i + 2]
    if kind == 'bool':
        value = raw == '1'
    elif kind == 'int':
        value = int(raw)
    elif kind == 'null':
        value = None
    else:
        value = raw
    patch[key] = value
print(json.dumps(patch))
" "$@"
}

if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
else
  log "ERROR env_file_missing path=$ENV_FILE"
fi

if [ "${1:-}" = "--test" ]; then
  if send_telegram "✅ JobPulse test alert OK

Server: $(hostname)
Time: $(ts)
API: http://35.192.251.190"; then
    log "test_alert_delivered"
    exit 0
  fi

  log "test_alert_failed category=$TELEGRAM_LAST_CATEGORY"
  exit 1
fi

fail=0
cd "$ROOT" || exit 1

log "health_alert_check_started"

for svc in db api frontend; do
  docker_ps_out="$(mktemp)"
  run_with_timeout "$HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS" "$HEALTH_ALERT_KILL_AFTER_SECONDS" \
    docker compose -f "$COMPOSE_FILE" ps -q "$svc" >"$docker_ps_out" 2>/dev/null
  docker_ps_rc=$?
  cid="$(cat "$docker_ps_out" 2>/dev/null || true)"
  rm -f "$docker_ps_out"

  if [ "$docker_ps_rc" -ne 0 ]; then
    log "ERROR docker_ps_q_failed service=$svc category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
    fail=1
    continue
  fi

  if [ -z "$cid" ]; then
    log "ERROR service_missing service=$svc"
    fail=1
    continue
  fi

  docker_inspect_out="$(mktemp)"
  run_with_timeout "$HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS" "$HEALTH_ALERT_KILL_AFTER_SECONDS" \
    docker inspect -f '{{.State.Status}}' "$cid" >"$docker_inspect_out" 2>/dev/null
  docker_inspect_rc=$?
  state="$(cat "$docker_inspect_out" 2>/dev/null || true)"
  rm -f "$docker_inspect_out"

  if [ "$docker_inspect_rc" -ne 0 ] || [ -z "$state" ]; then
    if [ "$docker_inspect_rc" -ne 0 ]; then
      log "ERROR docker_inspect_failed service=$svc category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit}"
    fi
    state="missing"
  fi
  log "service_status service=$svc state=$state"

  if [ "$state" != "running" ]; then
    fail=1
  fi
done

api_health_out="$(mktemp)"
api_health_err="$(mktemp)"

if curl -fsS --max-time "$API_HEALTH_TIMEOUT_SECONDS" "$API_HEALTH_URL" >"$api_health_out" 2>"$api_health_err"; then
  log "api_health_ok response=$(truncate_text "$(cat "$api_health_out")")"
else
  log "ERROR api_health_failed error=$(truncate_text "$(cat "$api_health_err")")"
  fail=1
fi

rm -f "$api_health_out" "$api_health_err"

jobs_count_out="$(mktemp)"
jobs_count_err="$(mktemp)"
run_with_timeout "$HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS" "$HEALTH_ALERT_KILL_AFTER_SECONDS" \
  docker compose -f "$COMPOSE_FILE" exec -T db psql -U jobpulse_user -d jobpulse -tAc "SELECT COUNT(*) FROM jobs;" \
  >"$jobs_count_out" 2>"$jobs_count_err"
jobs_count_rc=$?
jobs_count="$(cat "$jobs_count_out" 2>/dev/null || true)"

if [ "$jobs_count_rc" -ne 0 ]; then
  log "ERROR jobs_count_failed category=${TIMEOUT_LAST_CATEGORY:-non_zero_exit} error=$(truncate_text "$(cat "$jobs_count_err" 2>/dev/null)" 200)"
  fail=1
elif [ -n "$jobs_count" ]; then
  log "jobs_count=$jobs_count"
else
  log "ERROR jobs_count_failed category=empty_output error=$(truncate_text "$(cat "$jobs_count_err" 2>/dev/null)" 200)"
  fail=1
fi

rm -f "$jobs_count_out" "$jobs_count_err"

disk_pct=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
log "disk_usage_root=${disk_pct}%"

if [ "$disk_pct" -ge 97 ]; then
  log "ERROR disk_usage_critical=${disk_pct}%"
  fail=1
fi

load_state

# Guard against a corrupt/non-numeric stored epoch before it ever reaches
# bash arithmetic -- a hand-corrupted or partially-written state value
# must fall back safely, not crash the script.
if ! [[ "$ST_LAST_SUCCESS_EPOCH" =~ ^[0-9]+$ ]]; then
  ST_LAST_SUCCESS_EPOCH=0
fi

now_epoch="$(date +%s)"

if [ "$fail" -eq 0 ]; then
  # --- Healthy right now ---
  # failure_delivered is the recovery-pending marker: it means "a failure
  # alert was confirmed delivered and no recovery has been confirmed
  # delivered since." It is checked alone, NOT in conjunction with
  # last_detected_status -- a failed recovery attempt still leaves
  # last_detected_status=OK (that's the real, current status) while
  # failure_delivered stays 1, and this check must keep firing on every
  # subsequent healthy poll until a recovery is actually confirmed
  # delivered. Requiring last_detected_status=FAILED here previously
  # caused a failed recovery to permanently suppress all later retries.
  if [ "$ST_FAILURE_DELIVERED" = "1" ]; then
    get_docker_status_context
    docker_status="$DOCKER_STATUS_CONTEXT"

    if send_telegram "✅ JobPulse recovered

Server: $(hostname)
Time: $(ts)

API health is OK again.

Docker:
${docker_status}"; then
      patch="$(patch_json last_detected_status str OK failure_delivered bool 0 last_failure_category null "")"

      if ! write_state "$patch"; then
        log "ERROR state_write_failed path=$STATE_FILE detail=recovery_delivered"
        exit 1
      fi

      log "health_alert_check_finished status=OK recovery_delivered=true"
      exit 0
    fi

    log "recovery_delivery_failed category=$TELEGRAM_LAST_CATEGORY"
    patch="$(patch_json last_detected_status str OK last_attempt_epoch int "$now_epoch" last_failure_category str "$TELEGRAM_LAST_CATEGORY")"

    if ! write_state "$patch"; then
      log "ERROR state_write_failed path=$STATE_FILE detail=recovery_attempt_failed"
      exit 1
    fi

    log "health_alert_check_finished status=OK recovery_delivered=false"
    exit 1
  fi

  patch="$(patch_json last_detected_status str OK)"

  if ! write_state "$patch"; then
    log "ERROR state_write_failed path=$STATE_FILE detail=detected_ok"
    exit 1
  fi

  log "health_alert_check_finished status=OK"
  exit 0
fi

# --- Failing right now ---
# A fresh OK->FAILED (or first-ever) transition starts a new incident:
# any stale delivered/cooldown bookkeeping from a previous, unrelated
# incident must not suppress this one's first attempt.
if [ "$ST_LAST_DETECTED_STATUS" != "FAILED" ]; then
  failure_delivered="0"
  last_success_epoch="0"
else
  failure_delivered="$ST_FAILURE_DELIVERED"
  last_success_epoch="$ST_LAST_SUCCESS_EPOCH"
fi

elapsed=$((now_epoch - last_success_epoch))

if [ "$failure_delivered" = "1" ] && [ "$elapsed" -lt "$COOLDOWN_SECONDS" ]; then
  log "health_failed_but_alert_in_cooldown elapsed=${elapsed}s cooldown=${COOLDOWN_SECONDS}s"
  patch="$(patch_json last_detected_status str FAILED)"
  write_state "$patch" || log "ERROR state_write_failed path=$STATE_FILE detail=cooldown_status_refresh"
  exit 1
fi

get_docker_status_context
docker_status="$DOCKER_STATUS_CONTEXT"
recent_log="$(tail -n 50 "$ALERT_LOG" 2>/dev/null || true)"
disk_status="$(df -h / 2>/dev/null || true)"

if send_telegram "🚨 JobPulse health check FAILED

Server: $(hostname)
Time: $(ts)
Public API: http://35.192.251.190

Docker:
${docker_status}

Disk:
${disk_status}

Recent log:
${recent_log}"; then
  patch="$(patch_json last_detected_status str FAILED failure_delivered bool 1 last_success_epoch int "$now_epoch" last_attempt_epoch int "$now_epoch" last_failure_category null "")"

  if ! write_state "$patch"; then
    log "ERROR state_write_failed path=$STATE_FILE detail=failure_delivered"
    exit 1
  fi

  log "health_alert_check_finished status=FAILED delivered=true"
  exit 1
fi

log "health_alert_delivery_failed category=$TELEGRAM_LAST_CATEGORY"
patch="$(patch_json last_detected_status str FAILED last_attempt_epoch int "$now_epoch" last_failure_category str "$TELEGRAM_LAST_CATEGORY")"

if ! write_state "$patch"; then
  log "ERROR state_write_failed path=$STATE_FILE detail=failure_attempt_failed"
  exit 1
fi

log "health_alert_check_finished status=FAILED delivered=false"
exit 1
