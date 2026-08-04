"""
Tests for scripts/run_collection_cycle_safe.sh (phase 2A, adversarially
hardened, twice):

  - every external Docker/PostgreSQL/alert-sender call is bounded by
    GNU `timeout`, with no leftover child process and the cycle lock
    released afterward
  - every in-container collector invocation ALSO carries its own
    process-group-scoped deadline (scripts/run_with_deadline.py) -- a
    SECOND, independent bound, not just the host-side `docker compose
    exec` timeout
  - every bounded PostgreSQL query also carries a server-side
    `SET statement_timeout`
  - queue-count failures/timeouts/malformed output are never treated as
    "0 pending"
  - truthful, non-zero exit codes on every real failure -- including a
    structurally-VALID but non-success process summary (partial_failure),
    which must NEVER be reported as a successful terminal cycle just
    because some real work happened (exit code 8, distinct from every
    other failure branch)
  - owner_pid stability, progress-write-failure-is-fatal, terminal
    heartbeat/history persistence guarantees
  - cycle-level classification sourced from the real
    scripts/process_summary.py contract (including its own canonical
    outcome recomputation), and confirmed visible to the EXISTING,
    unmodified app/admin_status.py alert/performance readers

Exercised via subprocess against a fake `docker` binary placed first on
PATH and a fake send_telegram_alerts.py stand-in -- no real Docker,
PostgreSQL, Telegram, or LinkedIn is ever contacted. `flock`, `timeout`,
`python3` (the real venv interpreter), and
scripts/collection_heartbeat.py / scripts/process_summary.py all run for
real against temp state/lock/summary paths.

IMPORTANT SCOPE NOTE: the fake `docker` here proves the HOST-side
`docker compose exec` invocation was constructed correctly (including
embedding the in-container `run_with_deadline` wrapper and the SQL
`SET statement_timeout` clause) and that a hung *host-side* fake process
is killed. It does NOT and CANNOT prove that a real container process
started by a real `docker compose exec` stops when the host client is
killed -- that claim is validated separately and directly (real
subprocess, real process groups, no Docker) in
tests/test_run_with_deadline.py. Until a real disposable-container
integration test exists, the in-container half of the dual-deadline
design is structurally protected and unit-tested at the process-group
level, but not Docker-runtime integration-tested -- see
docs/PRODUCTION_RUNBOOK.md.
"""
import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WRAPPER = SCRIPTS / "run_collection_cycle_safe.sh"

sys.path.insert(0, str(REPO_ROOT))
import scripts.process_summary as ps  # noqa: E402


def test_wrapper_passes_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_wrapper_is_executable_on_disk():
    mode = WRAPPER.stat().st_mode
    assert mode & stat.S_IXUSR, f"{WRAPPER} is missing the owner-executable bit"


# =====================================================================
# Fake `docker` -- no real Docker involved. Controlled entirely via env
# vars so each test can script exactly which step fails, hangs, or what
# a query returns. Writes a `.called_<step>` marker file for each step it
# actually reaches (so tests can prove a LATER step never ran), AND a
# `.cmdline_<step>` file containing the FULL argv it was invoked with (so
# tests can prove the dual-timeout / statement_timeout wiring is actually
# present in what the wrapper constructs at runtime, not just in the
# source text).
# =====================================================================

FAKE_DOCKER = r"""#!/usr/bin/env bash
marker_dir="${FAKE_DOCKER_MARKER_DIR:-/tmp}"

if [ "$1" = "compose" ]; then
  shift
  if [ "$1" = "-f" ]; then shift; shift; fi
  if [ "$1" = "exec" ]; then
    shift
    [ "$1" = "-T" ] && shift
    extra_env=""
    while [ "$1" = "-e" ]; do
      extra_env="$2"
      shift 2
    done
    service="$1"; shift
    printf '%s\n' "$*" > "$marker_dir/.cmdline_${service}_last"
    if [ "$service" = "db" ]; then
      touch "$marker_dir/.called_db"
      printf '%s\n' "$*" >> "$marker_dir/.cmdline_db_all"
      if [ "${FAKE_DB_HANG:-0}" = "1" ]; then
        sleep 600
      fi
      args="$*"
      if [ "${FAKE_DB_RC:-0}" != "0" ]; then
        exit "${FAKE_DB_RC}"
      fi
      if echo "$args" | grep -q "status = 'pending'"; then
        if [ -n "${FAKE_PENDING_OUTPUT+set}" ]; then
          printf '%s' "$FAKE_PENDING_OUTPUT"
        else
          printf '%s' "${FAKE_PENDING_COUNT:-0}"
        fi
        exit 0
      elif echo "$args" | grep -q "status = 'running'"; then
        if [ -n "${FAKE_RUNNING_OUTPUT+set}" ]; then
          printf '%s' "$FAKE_RUNNING_OUTPUT"
        else
          printf '%s' "${FAKE_RUNNING_COUNT:-0}"
        fi
        exit 0
      else
        exit 0
      fi
    elif [ "$service" = "api" ]; then
      case "$*" in
        *linkedin_auth_preflight*)
          touch "$marker_dir/.called_auth"
          printf '%s\n' "$*" > "$marker_dir/.cmdline_auth"
          [ "${FAKE_AUTH_HANG:-0}" = "1" ] && sleep 600
          exit "${FAKE_AUTH_RC:-0}" ;;
        *seed_priority_coverage_queue*)
          touch "$marker_dir/.called_seed"
          printf '%s\n' "$*" > "$marker_dir/.cmdline_seed"
          [ "${FAKE_SEED_HANG:-0}" = "1" ] && sleep 600
          exit "${FAKE_SEED_RC:-0}" ;;
        *process_search_demand_queue*)
          touch "$marker_dir/.called_process"
          printf '%s\n' "$*" > "$marker_dir/.cmdline_process"
          printf '%s\n' "$extra_env" > "$marker_dir/.extra_env_process"
          [ "${FAKE_PROCESS_HANG:-0}" = "1" ] && sleep 600
          if [ "${FAKE_PROCESS_RC:-0}" = "0" ] && [ "${FAKE_WRITE_SUMMARY:-1}" = "1" ]; then
            container_path="${extra_env#*JOBPULSE_PROCESS_SUMMARY_RESULT_PATH=}"
            host_path="${FAKE_LOGS_HOST_DIR}/$(basename "$container_path")"
            printf '%s' "${FAKE_SUMMARY_JSON}" > "$host_path"
          fi
          exit "${FAKE_PROCESS_RC:-0}" ;;
        *reconcile_priority_coverage*)
          touch "$marker_dir/.called_reconcile"
          printf '%s\n' "$*" > "$marker_dir/.cmdline_reconcile"
          [ "${FAKE_RECONCILE_HANG:-0}" = "1" ] && sleep 600
          exit "${FAKE_RECONCILE_RC:-0}" ;;
        *)
          exit 0 ;;
      esac
    fi
  fi
fi
exit 0
"""

FAKE_TELEGRAM_ALERTS = r"""#!/usr/bin/env python3
import os, sys, time
if os.environ.get("FAKE_TELEGRAM_HANG") == "1":
    time.sleep(600)
sys.exit(int(os.environ.get("FAKE_TELEGRAM_RC", "0")))
"""


def _write_executable(path: Path, content: str):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _agg(**overrides):
    d = {
        "jobs_discovered": 0, "jobs_valid": 0, "jobs_filtered_invalid": 0,
        "jobs_filtered_non_linkedin": 0, "jobs_filtered_header_artifact": 0,
        "jobs_filtered_missing_identifier": 0, "rows_inserted": 0,
        "rows_updated_existing": 0, "persistence_errors": 0,
    }
    d.update(overrides)
    return d


def _batch_report(total_queries=1, successful_queries=1, failed_queries=0,
                   useful_queries=0, zero_yield_queries=0, skipped_queries=0, **agg_overrides):
    return {
        "total_queries": total_queries,
        "successful_queries": successful_queries,
        "failed_queries": failed_queries,
        "useful_queries": useful_queries,
        "zero_yield_queries": zero_yield_queries,
        "skipped_queries": skipped_queries,
        "partial_failure": successful_queries > 0 and failed_queries > 0,
        "aggregate_collector_metrics": _agg(**agg_overrides),
    }


def _summary_json_from_batch(batch_report, had_pending_targets=True) -> str:
    """Builds a summary via the REAL scripts.process_summary.build_summary()
    -- guaranteed internally consistent by construction, unlike a
    hand-rolled dict, which is fragile against read_summary()'s strict
    (and now fully semantic, recompute-and-compare) validation."""
    summary = ps.build_summary(batch_report, had_pending_targets)
    return json.dumps(summary.to_dict())


# A useful_success default: one query, jobs_valid/discovered consistent
# with rows_inserted.
DEFAULT_SUMMARY = _summary_json_from_batch(
    _batch_report(useful_queries=1, jobs_discovered=5, jobs_valid=5, rows_inserted=3), True,
)


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", FAKE_DOCKER)

    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "logs").mkdir()

    telegram_script = tmp_path / "fake_send_telegram_alerts.py"
    _write_executable(telegram_script, FAKE_TELEGRAM_ALERTS)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["JOBPULSE_COLLECTION_ROOT"] = str(root_dir)
    env["JOBPULSE_PYTHON_BIN"] = sys.executable
    env["JOBPULSE_TELEGRAM_ALERTS_SCRIPT"] = str(telegram_script)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["FAKE_DOCKER_MARKER_DIR"] = str(root_dir)
    env["FAKE_LOGS_HOST_DIR"] = str(root_dir / "logs")
    env["FAKE_PENDING_COUNT"] = "3"
    env["FAKE_RUNNING_COUNT"] = "0"
    env["FAKE_TELEGRAM_RC"] = "0"
    env["FAKE_SUMMARY_JSON"] = DEFAULT_SUMMARY
    # Tight bounds so hang tests run fast; still well above what a
    # non-hanging fake command needs. The host backstop margin is ALSO
    # shrunk to its minimum (1s) here -- with the default 30s margin,
    # HOST_STEP_TIMEOUT_SECONDS (which is what actually bounds the fake
    # `docker` hang in these tests, since the inner run_with_deadline
    # wrapper is never interpreted by the fake) would be
    # 2 + 1 + 30 = 33s per hang test instead of 2 + 1 + 1 = 4s.
    env["JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS"] = "2"
    env["JOBPULSE_COLLECTION_DB_QUERY_TIMEOUT_SECONDS"] = "2"
    env["JOBPULSE_COLLECTION_ALERT_TIMEOUT_SECONDS"] = "2"
    env["JOBPULSE_COLLECTION_KILL_AFTER_SECONDS"] = "1"
    env["JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS"] = "1"

    return {"env": env, "root": root_dir}


def run_wrapper(sandbox, timeout=30, **env_overrides):
    env = dict(sandbox["env"])
    env.update({k: str(v) for k, v in env_overrides.items()})
    return subprocess.run(
        ["bash", str(WRAPPER)], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=timeout,
    )


def heartbeat_state(sandbox) -> dict:
    path = sandbox["root"] / "logs" / "collection_heartbeat.json"
    return json.loads(path.read_text())


def history_events(sandbox) -> list[dict]:
    path = sandbox["root"] / "logs" / "collection_history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def marker_exists(sandbox, name) -> bool:
    return (sandbox["root"] / f".called_{name}").exists()


def cmdline_for(sandbox, name) -> str:
    path = sandbox["root"] / f".cmdline_{name}"
    return path.read_text() if path.exists() else ""


def fresh_admin_status_module(history_path: Path, monkeypatch):
    """Loads (and re-executes) the REAL, unmodified app/admin_status.py
    module with JOBPULSE_COLLECTION_HISTORY pointed at the given file --
    the exact module the existing, unmodified production alert path
    (commit 83d7b00) uses."""
    monkeypatch.setenv("JOBPULSE_COLLECTION_HISTORY", str(history_path))
    import importlib
    import app.admin_status as admin_status
    importlib.reload(admin_status)
    return admin_status


# =====================================================================
# Happy path
# =====================================================================

def test_successful_cycle_exits_zero_and_records_useful_success(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    state = heartbeat_state(sandbox)
    assert state["status"] == "success"
    assert state["final_outcome"] == "useful_success"
    assert state["last_useful_ingestion_at"] is not None
    assert state["run_id"]

    events = history_events(sandbox)
    assert events[-1]["status"] == "success"


def test_history_file_is_compatible_with_the_real_admin_status_parser(sandbox, monkeypatch):
    result = run_wrapper(sandbox)
    assert result.returncode == 0

    history_path = sandbox["root"] / "logs" / "collection_history.jsonl"
    admin_status = fresh_admin_status_module(history_path, monkeypatch)

    perf = admin_status.get_collection_performance()
    assert perf["exists"] is True
    assert perf["success_count"] == 1
    assert perf["failure_count"] == 0
    assert perf["avg_duration_minutes"] is not None
    assert perf["recent"][0]["status"] == "success"
    assert perf["recent"][0]["pending_before"] == 3


# =====================================================================
# Failure branches: every one exits non-zero
# =====================================================================

def test_auth_preflight_failure_exits_nonzero(sandbox):
    result = run_wrapper(sandbox, FAKE_AUTH_RC=1)
    assert result.returncode == 2
    state = heartbeat_state(sandbox)
    assert state["status"] == "aborted_auth"


def test_seed_failure_exits_nonzero(sandbox):
    result = run_wrapper(sandbox, FAKE_SEED_RC=1)
    assert result.returncode == 4
    state = heartbeat_state(sandbox)
    assert state["status"] == "failed"
    assert state["stage"] == "seed"


def test_queue_processing_failure_exits_nonzero(sandbox):
    result = run_wrapper(sandbox, FAKE_PROCESS_RC=1)
    assert result.returncode == 5
    state = heartbeat_state(sandbox)
    assert state["status"] == "failed"
    assert state["stage"] == "process"


def test_reconciliation_failure_exits_nonzero(sandbox):
    result = run_wrapper(sandbox, FAKE_RECONCILE_RC=1)
    assert result.returncode == 6
    state = heartbeat_state(sandbox)
    assert state["status"] == "failed"
    assert state["stage"] == "reconcile"


def test_no_failure_branch_ever_exits_zero(sandbox):
    for override in (
        {"FAKE_AUTH_RC": 1}, {"FAKE_SEED_RC": 1}, {"FAKE_PROCESS_RC": 1}, {"FAKE_RECONCILE_RC": 1},
    ):
        result = run_wrapper(sandbox, **override)
        assert result.returncode != 0, f"{override} unexpectedly exited 0"


# =====================================================================
# Backlog skip: zero-yield/no-op cycle, exit 0, explicitly classified
# =====================================================================

def test_backlog_running_skip_exits_zero_but_is_explicitly_classified(sandbox):
    result = run_wrapper(sandbox, FAKE_RUNNING_COUNT=2)
    assert result.returncode == 0
    state = heartbeat_state(sandbox)
    assert state["status"] == "skipped_running"
    assert "2 queue tasks are still running" in state["message"]

    events = history_events(sandbox)
    assert events[-1]["status"] == "skipped_running"


# =====================================================================
# External call bounds: hanging steps are killed, categorized, and leave
# no lingering process or lock (HOST-side fake docker layer)
# =====================================================================

@pytest.mark.parametrize("hang_var,expected_rc,expected_stage", [
    ("FAKE_AUTH_HANG", 2, "auth_preflight"),
    ("FAKE_SEED_HANG", 4, "seed"),
    ("FAKE_PROCESS_HANG", 5, "process"),
    ("FAKE_RECONCILE_HANG", 6, "reconcile"),
])
def test_hanging_docker_step_is_killed_and_categorized(sandbox, hang_var, expected_rc, expected_stage):
    result = run_wrapper(sandbox, timeout=30, **{hang_var: "1"})
    assert result.returncode == expected_rc
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "timeout"
    assert state["stage"] == expected_stage


def test_hanging_queue_count_query_is_killed_and_exits_nonzero(sandbox):
    result = run_wrapper(sandbox, timeout=30, FAKE_DB_HANG="1")
    assert result.returncode == 7
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "timeout"


def test_hanging_alert_sender_is_killed_and_does_not_block_the_cycle(sandbox):
    started = time.monotonic()
    result = run_wrapper(sandbox, timeout=30, FAKE_SEED_RC=1, FAKE_TELEGRAM_HANG="1")
    elapsed = time.monotonic() - started
    assert result.returncode == 4
    assert elapsed < 20
    assert "ALERT_SENDER_FAILED" in (sandbox["root"] / "logs" / "collection_cycle.log").read_text()


def test_no_child_process_remains_after_a_timeout(sandbox):
    result = run_wrapper(sandbox, timeout=30, FAKE_PROCESS_HANG="1")
    assert result.returncode == 5
    time.sleep(0.3)
    proc = subprocess.run(["pgrep", "-f", "sleep 600"], capture_output=True, text=True)
    assert proc.stdout.strip() == "", f"leftover process(es) found: {proc.stdout}"


def test_lock_releases_after_a_timeout_and_next_cycle_succeeds(sandbox):
    result1 = run_wrapper(sandbox, timeout=30, FAKE_PROCESS_HANG="1")
    assert result1.returncode == 5

    lock_path = sandbox["root"] / "state" / "run_collection_cycle.lock"
    assert lock_path.exists()
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    result2 = run_wrapper(sandbox)
    assert result2.returncode == 0


def test_timeout_utility_missing_fails_clearly(sandbox, tmp_path):
    restricted_bin = tmp_path / "restricted_bin"
    restricted_bin.mkdir()
    for tool in ("bash", "docker", "flock", "python3", "cat", "mkdir", "rm", "date", "tr", "grep", "mktemp", "printf", "tail", "pgrep"):
        found = None
        for d in os.environ.get("PATH", "").split(":"):
            candidate = Path(d) / tool
            if candidate.exists():
                found = candidate
                break
        if found:
            (restricted_bin / tool).symlink_to(found)
    (restricted_bin / "docker").unlink()
    _write_executable(restricted_bin / "docker", FAKE_DOCKER)
    (restricted_bin / "python3").unlink(missing_ok=True)
    (restricted_bin / "python3").symlink_to(sys.executable)

    env = dict(sandbox["env"])
    env["PATH"] = str(restricted_bin)

    result = subprocess.run(["bash", str(WRAPPER)], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 2
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "timeout_utility_missing"


# =====================================================================
# Dual host/in-container timeout wiring (item 5) -- proves the ACTUAL
# generated docker-compose-exec command embeds the in-container deadline
# runner and, for DB commands, a server-side statement_timeout. This is
# the host-side half of the proof; the in-container half (real
# process-group kill) is proven directly in tests/test_run_with_deadline.py.
# =====================================================================

def test_auth_step_command_includes_in_container_deadline_runner(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0
    cmdline = cmdline_for(sandbox, "auth")
    assert "scripts.run_with_deadline" in cmdline
    assert "--seconds" in cmdline
    assert "--kill-after" in cmdline
    assert "scripts.linkedin_auth_preflight" in cmdline


def test_seed_step_command_includes_in_container_deadline_runner(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0
    cmdline = cmdline_for(sandbox, "seed")
    assert "scripts.run_with_deadline" in cmdline
    assert "scripts.seed_priority_coverage_queue" in cmdline


def test_process_step_command_includes_in_container_deadline_runner(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0
    cmdline = cmdline_for(sandbox, "process")
    assert "scripts.run_with_deadline" in cmdline
    assert "scripts.process_search_demand_queue" in cmdline


def test_reconcile_step_command_includes_in_container_deadline_runner(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0
    cmdline = cmdline_for(sandbox, "reconcile")
    assert "scripts.run_with_deadline" in cmdline
    assert "scripts.reconcile_priority_coverage" in cmdline


def test_deadline_runner_seconds_matches_the_configured_step_timeout(sandbox):
    result = run_wrapper(sandbox, JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS="17")
    assert result.returncode == 0
    cmdline = cmdline_for(sandbox, "auth")
    assert "--seconds 17" in cmdline


def test_db_queries_include_server_side_statement_timeout(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0
    all_db_cmdlines = (sandbox["root"] / ".cmdline_db_all").read_text()
    assert "statement_timeout" in all_db_cmdlines
    assert "SET statement_timeout" in all_db_cmdlines


def test_statement_timeout_value_matches_configured_db_query_timeout(sandbox):
    result = run_wrapper(sandbox, JOBPULSE_COLLECTION_DB_QUERY_TIMEOUT_SECONDS="11")
    assert result.returncode == 0
    all_db_cmdlines = (sandbox["root"] / ".cmdline_db_all").read_text()
    assert "statement_timeout = '11s'" in all_db_cmdlines


def test_reset_recent_running_query_also_includes_statement_timeout(sandbox):
    """reset_recent_running() only runs on a process-step failure --
    verify its own psql invocation (not just queue_count()'s) carries the
    same server-side deadline."""
    result = run_wrapper(sandbox, FAKE_PROCESS_RC=1)
    assert result.returncode == 5
    all_db_cmdlines = (sandbox["root"] / ".cmdline_db_all").read_text()
    assert all_db_cmdlines.count("SET statement_timeout") >= 2  # at least one queue_count + the reset itself


# =====================================================================
# Host-versus-inner deadline ORDERING (item 6/7): the wrapper logs its
# fully-computed deadline configuration once per cycle (DEADLINE_CONFIG),
# which is what lets these tests assert the actual numeric relationship
# the running script used -- not just that both values exist somewhere.
# =====================================================================

def _deadline_config(sandbox) -> dict:
    log_text = (sandbox["root"] / "logs" / "collection_cycle.log").read_text()
    line = next(line for line in log_text.splitlines() if "DEADLINE_CONFIG" in line)
    fields = {}
    for token in line.split("DEADLINE_CONFIG", 1)[1].strip().split():
        key, value = token.split("=", 1)
        fields[key] = int(value)
    return fields


def test_inner_step_timeout_receives_the_configured_value(sandbox):
    result = run_wrapper(sandbox, JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS="45", JOBPULSE_COLLECTION_KILL_AFTER_SECONDS="3")
    assert result.returncode == 0
    cfg = _deadline_config(sandbox)
    assert cfg["inner_step_timeout"] == 45
    assert cfg["inner_kill_after"] == 3


def test_host_step_timeout_is_strictly_larger_than_inner_timeout_plus_grace(sandbox):
    result = run_wrapper(
        sandbox, JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS="45", JOBPULSE_COLLECTION_KILL_AFTER_SECONDS="3",
        JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS="7",
    )
    assert result.returncode == 0
    cfg = _deadline_config(sandbox)
    assert cfg["host_step_timeout"] > cfg["inner_step_timeout"] + cfg["inner_kill_after"]
    assert cfg["host_step_timeout"] == cfg["inner_step_timeout"] + cfg["inner_kill_after"] + cfg["margin"]
    assert cfg["margin"] == 7


def test_host_db_query_timeout_is_strictly_larger_than_server_statement_timeout(sandbox):
    result = run_wrapper(sandbox, JOBPULSE_COLLECTION_DB_QUERY_TIMEOUT_SECONDS="20", JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS="5")
    assert result.returncode == 0
    cfg = _deadline_config(sandbox)
    assert cfg["host_db_query_timeout"] > cfg["db_query_timeout"]
    assert cfg["host_db_query_timeout"] == cfg["db_query_timeout"] + cfg["margin"]


def test_malformed_step_timeout_config_cannot_invert_the_ordering(sandbox):
    """A malformed override falls back to the safe default BEFORE the
    host backstop is computed from it -- the ordering invariant holds
    regardless of what garbage was supplied."""
    result = run_wrapper(sandbox, JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS="not-a-number; rm -rf /")
    assert result.returncode == 0
    cfg = _deadline_config(sandbox)
    assert cfg["inner_step_timeout"] == 1800  # documented default
    assert cfg["host_step_timeout"] > cfg["inner_step_timeout"] + cfg["inner_kill_after"]


def test_malformed_margin_config_falls_back_to_default_and_stays_positive(sandbox):
    result = run_wrapper(sandbox, JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS="-999")
    assert result.returncode == 0
    cfg = _deadline_config(sandbox)
    assert cfg["margin"] > 0
    assert cfg["host_step_timeout"] > cfg["inner_step_timeout"] + cfg["inner_kill_after"]


def test_margin_is_never_zero_or_negative_across_a_range_of_inputs(sandbox):
    for raw in ("0", "-1", "abc", "NaN", ""):
        result = run_wrapper(sandbox, JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS=raw)
        assert result.returncode == 0
        cfg = _deadline_config(sandbox)
        assert cfg["margin"] >= 1, f"margin={cfg['margin']} for raw override {raw!r} -- must stay strictly positive"


# =====================================================================
# Queue-count truthfulness (item 2)
# =====================================================================

def test_true_zero_pending_and_running_is_accepted(sandbox):
    result = run_wrapper(sandbox, FAKE_PENDING_COUNT="0", FAKE_RUNNING_COUNT="0")
    assert result.returncode == 0


def test_queue_count_command_failure_is_rejected_not_treated_as_zero(sandbox):
    result = run_wrapper(sandbox, FAKE_DB_RC="1")
    assert result.returncode == 7
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "non_zero_exit"
    assert state["stage"].startswith("queue_count")


def test_queue_count_empty_output_is_rejected(sandbox):
    result = run_wrapper(sandbox, FAKE_PENDING_OUTPUT="")
    assert result.returncode == 7
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "malformed_output"


def test_queue_count_whitespace_only_output_is_rejected(sandbox):
    result = run_wrapper(sandbox, FAKE_PENDING_OUTPUT="   ")
    assert result.returncode == 7


def test_queue_count_negative_output_is_rejected(sandbox):
    result = run_wrapper(sandbox, FAKE_PENDING_OUTPUT="-5")
    assert result.returncode == 7
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "malformed_output"


def test_queue_count_non_numeric_output_is_rejected(sandbox):
    result = run_wrapper(sandbox, FAKE_PENDING_OUTPUT="ERROR: relation does not exist")
    assert result.returncode == 7


def test_queue_count_internal_whitespace_output_is_rejected(sandbox):
    result = run_wrapper(sandbox, FAKE_PENDING_OUTPUT="1 5")
    assert result.returncode == 7
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "malformed_output"


def test_queue_count_failure_never_silently_becomes_zero_pending(sandbox):
    result = run_wrapper(sandbox, FAKE_DB_RC="1")
    assert result.returncode == 7
    state = heartbeat_state(sandbox)
    assert state.get("current_metrics") is None or "pending_before" not in (state.get("current_metrics") or {})


# =====================================================================
# Alert-sender failure does not hide the original collector failure
# =====================================================================

def test_alert_sender_failure_does_not_mask_original_failure_code(sandbox):
    result = run_wrapper(sandbox, FAKE_SEED_RC=1, FAKE_TELEGRAM_RC=1)
    assert result.returncode == 4
    assert "ALERT_SENDER_FAILED" in (sandbox["root"] / "logs" / "collection_cycle.log").read_text()


# =====================================================================
# Lock-busy: documented duplicate-run status
# =====================================================================

def test_overlapping_invocation_exits_via_lock_busy_path_without_touching_heartbeat(sandbox):
    root = sandbox["root"]
    (root / "state").mkdir(parents=True, exist_ok=True)
    lock_path = root / "state" / "run_collection_cycle.lock"
    lock_file = open(lock_path, "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(sandbox)
        assert result.returncode == 0
        assert "run_collection_cycle_already_running" in result.stdout
        assert not (root / "logs" / "collection_heartbeat.json").exists()
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


# =====================================================================
# owner_pid stability (item 6)
# =====================================================================

def test_owner_pid_is_stable_across_the_whole_cycle_and_differs_from_writer_pid(sandbox):
    result = run_wrapper(sandbox)
    assert result.returncode == 0
    state = heartbeat_state(sandbox)
    assert isinstance(state["owner_pid"], int)
    assert isinstance(state["writer_pid"], int)
    assert state["owner_pid"] != state["writer_pid"]


# =====================================================================
# Persistent progress-write failure prevents the next collector stage
# =====================================================================

def test_persistent_progress_heartbeat_failure_prevents_next_stage(sandbox):
    root = sandbox["root"]
    heartbeat_lock_path = root / "logs" / "collection_heartbeat.lock"
    heartbeat_lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(heartbeat_lock_path, "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(
            sandbox, timeout=30,
            JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_ATTEMPTS="2",
            JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_DELAY_SECONDS="0.05",
        )
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    assert result.returncode == 3
    assert not marker_exists(sandbox, "seed")
    assert not marker_exists(sandbox, "process")
    assert not marker_exists(sandbox, "reconcile")


# =====================================================================
# Terminal heartbeat/history persistence guarantees
# =====================================================================

def test_heartbeat_persistence_failure_exits_nonzero(sandbox):
    root = sandbox["root"]
    lock_path = root / "logs" / "collection_heartbeat.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(sandbox)
        assert result.returncode == 3
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def test_history_persistence_failure_after_success_still_forces_nonzero(sandbox):
    root = sandbox["root"]
    history_lock_path = root / "logs" / "collection_history.lock"
    history_lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(history_lock_path, "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = run_wrapper(sandbox)
        assert result.returncode == 3
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


# =====================================================================
# Cycle-level classification: success outcomes
# =====================================================================

def test_useful_success_sets_last_useful_ingestion_at(sandbox):
    summary = _summary_json_from_batch(
        _batch_report(useful_queries=1, jobs_discovered=4, jobs_valid=4, rows_inserted=2), True,
    )
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=summary)
    assert result.returncode == 0
    state = heartbeat_state(sandbox)
    assert state["final_outcome"] == "useful_success"
    assert state["last_useful_ingestion_at"] is not None
    assert state["status"] == "success"


@pytest.mark.parametrize("outcome,batch_kwargs", [
    ("technical_success_no_results", {}),
    ("technical_success_filtered_all", {"jobs_discovered": 5, "jobs_valid": 5, "jobs_filtered_header_artifact": 5}),
    ("technical_success_no_new_rows", {"jobs_discovered": 5, "jobs_valid": 5, "rows_updated_existing": 5}),
])
def test_zero_yield_classifications_do_not_set_last_useful_ingestion_at(sandbox, outcome, batch_kwargs):
    summary = _summary_json_from_batch(_batch_report(zero_yield_queries=1, **batch_kwargs), True)
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=summary)
    assert result.returncode == 0
    state = heartbeat_state(sandbox)
    assert state["final_outcome"] == outcome
    assert state["last_useful_ingestion_at"] is None
    assert state["status"] == "success"


# =====================================================================
# Cycle-level classification: PARTIAL FAILURE (item 1 -- the primary
# focus of this adversarial pass)
# =====================================================================

def _partial_failure_summary(useful=False):
    agg_overrides = {"jobs_discovered": 5, "jobs_valid": 5, "rows_inserted": 2} if useful else {}
    return _summary_json_from_batch(
        _batch_report(total_queries=2, successful_queries=1, failed_queries=1, useful_queries=1 if useful else 0, **agg_overrides),
        True,
    )


def test_partial_failure_wrapper_exit_is_the_dedicated_nonzero_code(sandbox):
    """Corrected contract: exit 8 is distinct from every other failure
    branch -- the docker-exec step itself succeeded, but the batch was
    only partially successful."""
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    assert result.returncode == 8


def test_partial_failure_heartbeat_status_is_not_success(sandbox):
    run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    state = heartbeat_state(sandbox)
    assert state["status"] == "failed"
    assert state["status"] != "success"


def test_partial_failure_legacy_last_status_is_not_success(sandbox):
    """app/admin_status.py's build_alerts() (unmodified) only ever reads
    last_status -- this is what makes the incident observable WITHOUT any
    phase-2B rewrite."""
    run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    state = heartbeat_state(sandbox)
    assert state["last_status"] == "failed"
    assert state["last_status"] != "success"


def test_partial_failure_does_not_advance_last_success_at(sandbox):
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    assert result.returncode == 8
    state = heartbeat_state(sandbox)
    assert state["last_success_at"] is None


def test_partial_failure_final_outcome_remains_partial_failure(sandbox):
    run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    state = heartbeat_state(sandbox)
    assert state["final_outcome"] == "partial_failure"


def test_partial_failure_error_category_is_stable(sandbox):
    run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "partial_failure"


def test_partial_failure_useful_ingestion_can_advance_without_making_it_success(sandbox):
    """last_useful_ingestion_at MAY advance if at least one row was
    genuinely proven inserted, even inside a partially-failed batch --
    but this must never flip status/history/final_outcome to success."""
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary(useful=True))
    assert result.returncode == 8
    state = heartbeat_state(sandbox)
    assert state["last_useful_ingestion_at"] is not None
    assert state["status"] == "failed"
    assert state["final_outcome"] == "partial_failure"
    assert state["last_success_at"] is None


def test_partial_failure_without_any_proven_insert_does_not_advance_useful_ingestion(sandbox):
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary(useful=False))
    assert result.returncode == 8
    state = heartbeat_state(sandbox)
    assert state["last_useful_ingestion_at"] is None


def test_partial_failure_preserves_successful_query_and_insert_metrics(sandbox):
    """The original successful-query/insert evidence is not discarded
    just because the overall cycle is reported as a failure."""
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary(useful=True))
    assert result.returncode == 8
    state = heartbeat_state(sandbox)
    metrics = state["current_metrics"]
    assert metrics["successful_queries"] == 1
    assert metrics["failed_queries"] == 1
    assert metrics["rows_inserted"] == 2


def test_partial_failure_history_status_is_not_success(sandbox):
    run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    events = history_events(sandbox)
    assert events[-1]["status"] != "success"
    assert events[-1]["status"] == "failed"


def test_partial_failure_get_collection_performance_does_not_count_it_as_success(sandbox, monkeypatch):
    """Direct proof against the REAL, unmodified
    app.admin_status.get_collection_performance() parser."""
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    assert result.returncode == 8

    history_path = sandbox["root"] / "logs" / "collection_history.jsonl"
    admin_status = fresh_admin_status_module(history_path, monkeypatch)

    perf = admin_status.get_collection_performance()
    assert perf["success_count"] == 0
    assert perf["failure_count"] == 1


def test_partial_failure_existing_admin_alert_builder_exposes_the_incident(sandbox, monkeypatch):
    """Direct proof against the REAL, unmodified
    app.admin_status.build_alerts(): with heartbeat.last_status="failed"
    and non-zero age, the existing (83d7b00-hardened) alert construction
    must raise a collection_failed-style alert -- no phase-2B admin
    rewrite required for this incident to be observable."""
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    assert result.returncode == 8

    state = heartbeat_state(sandbox)

    admin_status = fresh_admin_status_module(sandbox["root"] / "logs" / "collection_history.jsonl", monkeypatch)

    collection_heartbeat = {
        "exists": True,
        "last_status": state["last_status"],
        "last_message": state["last_message"],
        "age_minutes": 1.0,
    }

    alerts = admin_status.build_alerts(
        job_stats={}, bad_apply={}, demand_queue=[], coverage=[], backups=[Path("/fake/backup.sql")],
        disk_usage={"used_percent": 10, "free_gb": 100},
        linkedin_auth={"exists": True, "age_hours": 1},
        collection_heartbeat=collection_heartbeat,
    )
    codes = [a["code"] for a in alerts]
    assert "collection_failed" in codes


def test_partial_failure_run_telegram_alerts_is_invoked(sandbox):
    """Matches the treatment of every other real failure branch: the
    best-effort alert sender is invoked so an operator gets notified."""
    marker = sandbox["root"] / "telegram_invoked_marker"
    telegram_script = Path(sandbox["env"]["JOBPULSE_TELEGRAM_ALERTS_SCRIPT"])
    telegram_script.write_text(
        "#!/usr/bin/env python3\nimport pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('called')\nsys.exit(0)\n"
    )
    telegram_script.chmod(telegram_script.stat().st_mode | stat.S_IEXEC)

    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    assert result.returncode == 8
    assert marker.exists()


def test_partial_failure_is_distinguishable_from_missing_summary_failure(sandbox):
    """Exit 8 (structurally-valid non-success summary) must be a
    DIFFERENT code from exit 5 (missing/malformed summary) -- they are
    different failure modes with different remediation."""
    partial_result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    missing_result = run_wrapper(sandbox, FAKE_WRITE_SUMMARY="0")
    assert partial_result.returncode == 8
    assert missing_result.returncode == 5
    assert partial_result.returncode != missing_result.returncode


def test_missing_process_summary_after_successful_exec_is_a_cycle_failure(sandbox):
    result = run_wrapper(sandbox, FAKE_WRITE_SUMMARY="0")
    assert result.returncode == 5
    state = heartbeat_state(sandbox)
    assert state["stage"] == "process_summary"
    assert state["error_category"] == "missing_result"


def test_malformed_process_summary_after_successful_exec_is_a_cycle_failure(sandbox):
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON="{not valid json")
    assert result.returncode == 5
    state = heartbeat_state(sandbox)
    assert state["stage"] == "process_summary"
    assert state["error_category"] == "invalid_result"


def test_semantically_contradictory_summary_is_also_rejected(sandbox):
    """A structurally well-formed but semantically self-contradictory
    summary (persisted outcome doesn't match its own recomputed outcome)
    must be rejected the same way a malformed one is -- proves the
    wrapper benefits from process_summary.py's fully semantic validation,
    not just structural checks."""
    doc = json.loads(DEFAULT_SUMMARY)
    doc["outcome"] = ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS  # contradicts rows_inserted > 0
    result = run_wrapper(sandbox, FAKE_SUMMARY_JSON=json.dumps(doc))
    assert result.returncode == 5
    state = heartbeat_state(sandbox)
    assert state["error_category"] == "invalid_result"


# =====================================================================
# No leftover temp files
# =====================================================================

def test_no_leftover_temp_files_after_successful_run(sandbox):
    run_wrapper(sandbox)
    logs_dir = sandbox["root"] / "logs"
    leftovers = [p for p in logs_dir.iterdir() if p.name.startswith(".collection_heartbeat.") or p.name.startswith(".process_summary_")]
    assert leftovers == []


def test_no_leftover_temp_files_after_failed_run(sandbox):
    run_wrapper(sandbox, FAKE_PROCESS_RC=1)
    logs_dir = sandbox["root"] / "logs"
    leftovers = [p for p in logs_dir.iterdir() if p.name.startswith(".collection_heartbeat.") or p.name.startswith(".process_summary_")]
    assert leftovers == []


def test_no_leftover_temp_files_after_partial_failure_run(sandbox):
    run_wrapper(sandbox, FAKE_SUMMARY_JSON=_partial_failure_summary())
    logs_dir = sandbox["root"] / "logs"
    leftovers = [p for p in logs_dir.iterdir() if p.name.startswith(".collection_heartbeat.") or p.name.startswith(".process_summary_")]
    assert leftovers == []


# =====================================================================
# Run identity
# =====================================================================

def test_each_invocation_gets_a_distinct_run_id(sandbox):
    run_wrapper(sandbox)
    state1 = heartbeat_state(sandbox)

    result2 = run_wrapper(sandbox)
    assert result2.returncode == 0
    state2 = heartbeat_state(sandbox)

    assert state1["run_id"] != state2["run_id"]
