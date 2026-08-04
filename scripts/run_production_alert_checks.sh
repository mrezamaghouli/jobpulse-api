#!/usr/bin/env bash
# Independently schedulable entry point for BOTH production alert checks:
#
#   1. scripts/production_health_alert.sh  (infrastructure: Docker/API/DB/disk)
#   2. scripts/send_telegram_alerts.py     (collector heartbeat + admin-status
#                                            alerts, via /api/admin/status)
#
# This is the fix for the "collector heartbeat visibility" gap: previously,
# send_telegram_alerts.py was only ever invoked from inside
# run_collection_cycle_safe.sh's own failure/abort exit paths, so a
# successful-but-empty collection cycle, or the collector's cron entry
# disappearing entirely, never triggered a poll of /api/admin/status at
# all. Running this wrapper on its own independent schedule (see
# docs/PRODUCTION_RUNBOOK.md for an example -- NOT installed by this repo)
# polls both signal sources regardless of what the collector itself is
# doing.
#
# run_collection_cycle_safe.sh's own send_telegram_alerts.py calls on its
# failure paths are intentionally left in place in this phase -- this
# wrapper's independent schedule is additive, not a replacement. Both
# scripts already deduplicate on their own delivered-state, so overlapping
# invocations (this wrapper firing at the same time collection fails) are
# safe: at most one of the two callers actually delivers a new message for
# any given alert set.
set -u

# validate_duration_seconds <raw_value> <default> <min> <max>
# Same contract as the identically-named helper in
# production_health_alert.sh (duplicated here rather than sourced, since
# that script has top-level executable logic that would run if sourced):
# prints a safe validated non-negative integer, at most 9 digits (avoids
# bash arithmetic overflow), within [min,max], falling back to <default>
# for anything else. The raw value is never echoed/logged.
#
# Only LEADING and TRAILING whitespace is trimmed -- never internal
# whitespace. `tr -d '[:space:]'` would delete whitespace anywhere in the
# string, silently turning a malformed value like "1 5" into the
# valid-looking "15" instead of rejecting it; the parameter-expansion
# trim below only strips a leading/trailing run, so any internal
# whitespace still fails the digits-only check and falls back safely.
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

ROOT="${JOBPULSE_ALERT_ROOT:-/opt/jobpulse}"
STATE_DIR="${JOBPULSE_ALERT_STATE_DIR:-$ROOT/state}"
WRAPPER_LOCK_FILE="${JOBPULSE_ALERT_WRAPPER_LOCK_FILE:-$STATE_DIR/run_production_alert_checks.lock}"
LOG_FILE="${JOBPULSE_ALERT_WRAPPER_LOG:-$ROOT/logs/production_alert_checks.log}"

# Each child gets its OWN independent deadline (applied fresh per call in
# run_component_with_timeout below) -- not one shared budget for the
# whole wrapper run, which could let a hung first child consume the
# entire budget and starve the second. Default of 240s comfortably
# exceeds production_health_alert.sh's own documented worst-case internal
# bound (~190s with its defaults -- see docs/PRODUCTION_RUNBOOK.md); this
# is a backstop in case a sub-script hangs somewhere its own internal
# timeouts don't cover, not the primary bounding mechanism. Under normal
# operation the wrapper finishes well inside a 5-minute cron cadence, but
# the absolute worst case -- both components independently hitting their
# full deadline+kill-after grace, 2 x (240+10)s = 500s -- does NOT fit in
# 5 minutes. That is expected and safe, not a bug: see "Lock-busy exit
# semantics" in docs/PRODUCTION_RUNBOOK.md -- a next tick that fires
# while a pathological run is still finishing simply exits cleanly via
# this script's own top-level lock (`*_already_running`, exit 0) rather
# than overlapping or corrupting state.
JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS="$(validate_duration_seconds "${JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS:-}" 240 1 3600)"
JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS="$(validate_duration_seconds "${JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS:-}" 10 1 60)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEALTH_ALERT_SCRIPT="${JOBPULSE_HEALTH_ALERT_SCRIPT:-$SCRIPT_DIR/production_health_alert.sh}"
TELEGRAM_ALERTS_SCRIPT="${JOBPULSE_TELEGRAM_ALERTS_SCRIPT:-$SCRIPT_DIR/send_telegram_alerts.py}"
PYTHON_BIN="${JOBPULSE_PYTHON_BIN:-python3}"

# GNU coreutils `timeout` is required to bound each component below. If
# it's missing, both components fail clearly (see run_component_with_timeout)
# instead of running unbounded.
TIMEOUT_BIN="$(command -v timeout 2>/dev/null || true)"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR state_dir_create_failed dir=$STATE_DIR" >&2
  exit 1
fi

ts() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log() {
  echo "$(ts) $*" | tee -a "$LOG_FILE"
}

# Own top-level, non-blocking lock -- distinct from either sub-script's own
# internal lock file, so this only guards against two overlapping wrapper
# invocations (e.g. a slow cron tick overlapping the next one); it does not
# interfere with, or double-lock, the sub-scripts' own concurrency guards.
exec {WRAPPER_LOCK_FD}>"$WRAPPER_LOCK_FILE" || {
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ERROR wrapper_lock_open_failed path=$WRAPPER_LOCK_FILE" >&2
  exit 1
}

# Exit 0 here is deliberate: it means "another wrapper invocation is
# already running both checks," not "monitoring status is unknown." Both
# sub-scripts now bound EVERY external call they make with an explicit
# timeout (production_health_alert.sh: curl --max-time plus GNU `timeout`
# around every Docker/DB command; send_telegram_alerts.py: its one HTTP
# call), and this wrapper additionally bounds each child's total runtime
# with its own component deadline below -- so a full wrapper run has a
# documented, finite maximum duration (see "Lock-busy exit semantics" in
# docs/PRODUCTION_RUNBOOK.md for the exact figure), not an unbounded one.
# Contention here means a concurrent duplicate run that will finish within
# that documented maximum, not a skipped or permanently stuck check.
if ! flock -n "$WRAPPER_LOCK_FD"; then
  log "run_production_alert_checks_already_running lock=$WRAPPER_LOCK_FILE"
  exit 0
fi

# run_component_with_timeout <duration_seconds> <kill_after_seconds> <cmd...>
# Bounds one child component to <duration_seconds>, escalating to SIGKILL
# after <kill_after_seconds> if it ignores SIGTERM. Deliberately does NOT
# pass --foreground: without it, `timeout` puts <cmd...> in its own new
# process group and signals the WHOLE group on timeout, so every
# grandchild the component itself spawns (docker, curl, python3
# subprocesses) is killed too when the deadline hits -- verified
# empirically during the adversarial-pass reproduction that --foreground
# leaves such grandchildren orphaned. This is what guarantees "no child
# process remains running after the wrapper exits," including for a
# component that hangs somewhere its own internal timeouts don't cover.
#
# Sets COMPONENT_LAST_CATEGORY to "" on a normal (bounded) run --
# regardless of the component's own exit code -- "timeout" if the
# deadline was hit (exit 124/137/143), or "timeout_utility_missing" if
# GNU `timeout` isn't installed (in which case the component is never
# run unbounded; it fails immediately and clearly instead).
COMPONENT_LAST_CATEGORY=""

run_component_with_timeout() {
  local duration="$1" kill_after="$2"
  shift 2
  COMPONENT_LAST_CATEGORY=""

  if [ -z "$TIMEOUT_BIN" ]; then
    COMPONENT_LAST_CATEGORY="timeout_utility_missing"
    return 1
  fi

  "$TIMEOUT_BIN" -k "${kill_after}" "${duration}" "$@"
  local rc=$?

  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ] || [ "$rc" -eq 143 ]; then
    COMPONENT_LAST_CATEGORY="timeout"
  fi

  return "$rc"
}

log "run_production_alert_checks_started"

run_component_with_timeout "$JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS" "$JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS" \
  bash "$HEALTH_ALERT_SCRIPT"
health_rc=$?
if [ -n "$COMPONENT_LAST_CATEGORY" ]; then
  log "health_alert_check_exit_code=$health_rc category=$COMPONENT_LAST_CATEGORY"
else
  log "health_alert_check_exit_code=$health_rc"
fi

# Run unconditionally -- the second check must execute even when the
# first fails OR TIMES OUT, so a broken or stuck infrastructure check can
# never mask a broken collector-alert check (or vice versa). This is
# exactly why each component gets its own FRESH, independent deadline
# (run_component_with_timeout is invoked separately for each) instead of
# one shared deadline for the whole wrapper run: a shared deadline could
# be entirely consumed by a hung first child, leaving none for the
# second -- which was reproduced and confirmed during the adversarial
# pass before this fix (a hung health-check child blocked the wrapper
# forever and the telegram/admin-status check never ran even once).
run_component_with_timeout "$JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS" "$JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS" \
  "$PYTHON_BIN" "$TELEGRAM_ALERTS_SCRIPT"
telegram_rc=$?
if [ -n "$COMPONENT_LAST_CATEGORY" ]; then
  log "telegram_alert_check_exit_code=$telegram_rc category=$COMPONENT_LAST_CATEGORY"
else
  log "telegram_alert_check_exit_code=$telegram_rc"
fi

combined_rc=0
if [ "$health_rc" -ne 0 ] || [ "$telegram_rc" -ne 0 ]; then
  combined_rc=1
fi

log "run_production_alert_checks_finished health_rc=$health_rc telegram_rc=$telegram_rc combined_rc=$combined_rc"
exit "$combined_rc"
