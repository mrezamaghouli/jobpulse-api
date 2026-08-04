"""
Tests for scripts/collection_heartbeat.py: the atomic, locked,
run-identified heartbeat writer, including the phase 2A adversarial-pass
corrections:

  - owner_pid (stable, caller-supplied) vs writer_pid (the short-lived
    CLI helper's own os.getpid(), diagnostic only)
  - bounded, configurable lock-retry for transient contention

No real Docker, PostgreSQL, Telegram, LinkedIn, or production endpoint is
ever used -- everything here operates on temp files via tmp_path.
"""
import fcntl
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.collection_heartbeat as hb


OWNER_PID = 424242


@pytest.fixture
def paths(tmp_path):
    return {"state_path": tmp_path / "heartbeat.json", "lock_path": tmp_path / "heartbeat.lock"}


# =====================================================================
# start / progress / finish / fail basics
# =====================================================================

def test_start_creates_versioned_state_with_run_id(paths):
    state = hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    assert state["schema_version"] == hb.SCHEMA_VERSION
    assert state["run_id"] == "run-a"
    assert state["status"] == hb.STATUS_RUNNING
    assert state["progress_seq"] == 0
    assert state["owner_pid"] == OWNER_PID
    assert isinstance(state["writer_pid"], int)


def test_progress_increments_progress_seq(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    s1 = hb.progress("run-a", OWNER_PID, "stage_1", "doing stage 1", **paths)
    s2 = hb.progress("run-a", OWNER_PID, "stage_2", "doing stage 2", **paths)
    assert s1["progress_seq"] == 1
    assert s2["progress_seq"] == 2


def test_finish_records_terminal_state(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    state = hb.finish("run-a", OWNER_PID, "cycle_finished", "done", status=hb.STATUS_SUCCESS, **paths)
    assert state["status"] == hb.STATUS_SUCCESS
    assert state["finished_at"] is not None
    assert state["last_success_at"] is not None


def test_failure_records_sanitized_error_category(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    state = hb.fail("run-a", OWNER_PID, "process", "process step failed", error_category="persistence_error", **paths)
    assert state["status"] == hb.STATUS_FAILED
    assert state["error_category"] == "persistence_error"
    assert state["finished_at"] is not None


@pytest.mark.parametrize("status", [hb.STATUS_ABORTED_AUTH, hb.STATUS_SKIPPED_RUNNING])
def test_fail_supports_legacy_status_values_for_admin_status_compat(paths, status):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    state = hb.fail("run-a", OWNER_PID, "auth_preflight", "auth failed", status=status, **paths)
    assert state["status"] == status
    assert state["last_status"] == status


def test_legacy_last_status_and_last_message_mirror_new_fields(paths):
    state = hb.start("run-a", OWNER_PID, "cycle_started", "hello", **paths)
    assert state["last_status"] == state["status"]
    assert state["last_message"] == state["message"] == "hello"
    assert "updated_at" in state


# =====================================================================
# owner_pid vs writer_pid (item 6)
# =====================================================================

def test_owner_pid_is_stable_across_start_progress_finish(paths):
    s1 = hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    s2 = hb.progress("run-a", OWNER_PID, "s1", "m1", **paths)
    s3 = hb.progress("run-a", OWNER_PID, "s2", "m2", **paths)
    s4 = hb.finish("run-a", OWNER_PID, "cycle_finished", "done", **paths)

    assert s1["owner_pid"] == s2["owner_pid"] == s3["owner_pid"] == s4["owner_pid"] == OWNER_PID


def test_owner_pid_belongs_to_the_caller_supplied_value_not_the_process(paths):
    """owner_pid must be exactly the value the caller passed -- never
    substituted with this Python process's own os.getpid()."""
    import os
    state = hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    assert state["owner_pid"] == OWNER_PID
    assert state["owner_pid"] != os.getpid()


def test_writer_pid_is_the_current_process_and_differs_from_owner_pid(paths):
    import os
    state = hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    assert state["writer_pid"] == os.getpid()
    assert state["writer_pid"] != state["owner_pid"]


def test_helper_invocation_does_not_replace_owner_pid_with_its_own(paths):
    """Each of start/progress/finish/fail is modeled as a distinct
    short-lived helper invocation in production (one `python3 -m
    scripts.collection_heartbeat` process per call) -- writer_pid is free
    to be identical here (same test process), but owner_pid must still be
    exactly what the CALLER supplied on every single call, never anything
    derived from the helper's own identity."""
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    for i in range(3):
        state = hb.progress("run-a", OWNER_PID, f"s{i}", f"m{i}", **paths)
        assert state["owner_pid"] == OWNER_PID


def test_neither_pid_gates_a_stale_run_write_run_id_is_canonical(paths):
    """A write from a DIFFERENT run_id must be rejected regardless of
    whether it happens to claim the SAME owner_pid -- run_id, not PID, is
    the canonical ownership key."""
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    with pytest.raises(hb.StaleRunError):
        hb.progress("run-b", OWNER_PID, "hijack", "same pid, different run", **paths)


def test_different_owner_pid_with_same_run_id_still_accepted():
    """This module does not use PID as a liveness/identity gate at all --
    only run_id. A caller passing a different owner_pid for the SAME
    run_id is accepted (PID is descriptive, not authoritative); canonical
    identity is run_id/progress_seq/timestamps/stage, documented
    explicitly in the module docstring."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        paths = {"state_path": Path(td) / "s.json", "lock_path": Path(td) / "s.lock"}
        hb.start("run-a", 111, "cycle_started", "started", **paths)
        state = hb.progress("run-a", 222, "s1", "m1", **paths)
        assert state["run_id"] == "run-a"
        assert state["owner_pid"] == 222


# =====================================================================
# Atomicity
# =====================================================================

def test_atomic_writes_leave_valid_json(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    hb.progress("run-a", OWNER_PID, "s1", "m1", **paths)
    hb.finish("run-a", OWNER_PID, "s2", "m2", **paths)

    data = json.loads(paths["state_path"].read_text())
    assert data["run_id"] == "run-a"

    leftovers = [p for p in paths["state_path"].parent.iterdir() if p.name.startswith(".collection_heartbeat.")]
    assert leftovers == []


def test_atomic_write_uses_tempfile_and_os_replace_same_directory(paths, monkeypatch):
    calls = {}
    import os
    real_replace = os.replace

    def spy_replace(src, dst):
        calls["src_dir"] = os.path.dirname(src)
        calls["dst_dir"] = os.path.dirname(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(hb.os, "replace", spy_replace)
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    assert calls["src_dir"] == calls["dst_dir"] == str(paths["state_path"].parent)


# =====================================================================
# Malformed / missing state recovery
# =====================================================================

@pytest.mark.parametrize("content", ["not json", "[]", '"a string"', ""])
def test_malformed_state_recovers_safely(paths, content):
    paths["state_path"].write_text(content)
    state = hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    assert state["run_id"] == "run-a"


def test_missing_state_file_recovers_safely(paths):
    assert not paths["state_path"].exists()
    state = hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    assert state["run_id"] == "run-a"


# =====================================================================
# Run-identity / stale-write protection
# =====================================================================

def test_older_run_cannot_overwrite_newer_runs_progress(paths):
    hb.start("run-newer", OWNER_PID, "cycle_started", "started", **paths)
    hb.progress("run-newer", OWNER_PID, "s1", "still going", **paths)

    with pytest.raises(hb.StaleRunError):
        hb.progress("run-older", OWNER_PID, "s1", "an older run trying to write", **paths)

    data = json.loads(paths["state_path"].read_text())
    assert data["run_id"] == "run-newer"
    assert data["stage"] == "s1"
    assert data["message"] == "still going"


def test_older_run_cannot_overwrite_newer_runs_terminal_state(paths):
    hb.start("run-newer", OWNER_PID, "cycle_started", "started", **paths)
    hb.finish("run-newer", OWNER_PID, "cycle_finished", "newer finished", **paths)

    with pytest.raises(hb.StaleRunError):
        hb.finish("run-older", OWNER_PID, "cycle_finished", "older trying to finish late", **paths)

    data = json.loads(paths["state_path"].read_text())
    assert data["message"] == "newer finished"


def test_new_start_can_claim_heartbeat_after_prior_run_terminal(paths):
    hb.start("run-1", OWNER_PID, "cycle_started", "started", **paths)
    hb.finish("run-1", OWNER_PID, "cycle_finished", "done", **paths)

    state = hb.start("run-2", OWNER_PID + 1, "cycle_started", "second cycle", **paths)
    assert state["run_id"] == "run-2"
    assert state["progress_seq"] == 0
    assert state["owner_pid"] == OWNER_PID + 1


def test_new_start_can_claim_heartbeat_even_while_prior_run_marker_says_running(paths):
    hb.start("run-dead", OWNER_PID, "cycle_started", "started", **paths)
    # Process "dies" here -- no further writes for run-dead.

    state = hb.start("run-alive", OWNER_PID + 1, "cycle_started", "new run after crash recovery", **paths)
    assert state["run_id"] == "run-alive"
    assert state["status"] == hb.STATUS_RUNNING


def test_current_run_can_always_write_its_own_progress(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    for i in range(5):
        state = hb.progress("run-a", OWNER_PID, f"stage_{i}", f"m{i}", **paths)
        assert state["run_id"] == "run-a"
        assert state["progress_seq"] == i + 1


def test_metrics_do_not_carry_forward_across_a_fresh_start(paths):
    hb.start("run-1", OWNER_PID, "cycle_started", "started", **paths)
    hb.progress("run-1", OWNER_PID, "s1", "m1", metrics={"pending_before": 5}, **paths)
    hb.finish("run-1", OWNER_PID, "cycle_finished", "done", metrics={"pending_after": 5}, **paths)

    state = hb.start("run-2", OWNER_PID, "cycle_started", "second cycle", **paths)
    assert state["current_metrics"] is None


def test_metrics_carry_forward_within_same_run_when_not_supplied(paths):
    hb.start("run-1", OWNER_PID, "cycle_started", "started", metrics={"pending_before": 7}, **paths)
    state = hb.progress("run-1", OWNER_PID, "s1", "m1", **paths)
    assert state["current_metrics"] == {"pending_before": 7}


def test_last_success_at_and_last_useful_ingestion_carry_forward_across_runs(paths):
    hb.start("run-1", OWNER_PID, "cycle_started", "started", **paths)
    hb.finish("run-1", OWNER_PID, "cycle_finished", "done", status=hb.STATUS_SUCCESS, useful_ingestion=True, **paths)

    state = hb.start("run-2", OWNER_PID, "cycle_started", "second cycle", **paths)
    assert state["last_success_at"] is not None
    assert state["last_useful_ingestion_at"] is not None

    state2 = hb.fail("run-2", OWNER_PID, "process", "boom", **paths)
    assert state2["last_success_at"] is not None
    assert state2["last_useful_ingestion_at"] is not None


# =====================================================================
# Locking
# =====================================================================

def test_lock_busy_raises_and_does_not_write(paths):
    lock_file = open(paths["lock_path"], "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(hb.LockBusyError):
            hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
        assert not paths["state_path"].exists()
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def test_concurrent_writers_do_not_corrupt_state(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)

    errors = []
    successes = []
    lock = threading.Lock()

    def worker(i):
        try:
            hb.progress("run-a", OWNER_PID, f"stage_{i}", f"m{i}", **paths)
            with lock:
                successes.append(i)
        except hb.LockBusyError as exc:
            with lock:
                errors.append(exc)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(isinstance(exc, hb.LockBusyError) for exc in errors)
    assert len(successes) >= 1

    data = json.loads(paths["state_path"].read_text())
    assert data["run_id"] == "run-a"
    assert 1 <= data["progress_seq"] <= 20


def test_sequential_retried_writers_all_eventually_succeed(paths):
    hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)

    for i in range(10):
        for _ in range(50):
            try:
                state = hb.progress("run-a", OWNER_PID, f"stage_{i}", f"m{i}", **paths)
                break
            except hb.LockBusyError:
                time.sleep(0.001)
        else:
            pytest.fail("writer never acquired the lock after 50 retries")
        assert state["progress_seq"] == i + 1

    data = json.loads(paths["state_path"].read_text())
    assert data["progress_seq"] == 10


# =====================================================================
# Bounded lock-retry (item 7)
# =====================================================================

def test_lock_retry_succeeds_once_contention_clears(paths):
    """update_heartbeat() with lock_retry_attempts > 1 must retry and
    eventually succeed once a transient holder releases the lock --
    without needing the CALLER to implement any retry loop itself."""
    lock_file = open(paths["lock_path"], "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    result = {}

    def release_after_delay():
        time.sleep(0.15)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()

    t = threading.Thread(target=release_after_delay)
    t.start()

    state = hb.start(
        "run-a", OWNER_PID, "cycle_started", "started",
        lock_retry_attempts=20, lock_retry_delay_seconds=0.05, **paths,
    )
    t.join()

    assert state["run_id"] == "run-a"


def test_lock_retry_exhausted_raises_lock_busy_error(paths):
    lock_file = open(paths["lock_path"], "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(hb.LockBusyError):
            hb.start(
                "run-a", OWNER_PID, "cycle_started", "started",
                lock_retry_attempts=3, lock_retry_delay_seconds=0.01, **paths,
            )
        assert not paths["state_path"].exists()
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def test_default_lock_retry_attempts_is_one_no_retry_for_direct_library_callers(paths):
    """The library default (no explicit lock_retry_attempts) must remain
    fail-fast for direct callers -- retry behavior is opt-in, and the CLI
    (main()) is what supplies a higher default for the real wrapper."""
    lock_file = open(paths["lock_path"], "a+")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    try:
        with pytest.raises(hb.LockBusyError):
            hb.start("run-a", OWNER_PID, "cycle_started", "started", **paths)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    assert time.monotonic() - started < 0.5  # fails immediately, no retry delay


# =====================================================================
# No secrets
# =====================================================================

def test_no_secret_or_raw_payload_fields_in_schema(paths):
    state = hb.start("run-a", OWNER_PID, "cycle_started", "started", metrics={"pending_before": 3}, **paths)
    raw = json.dumps(state)
    for forbidden in ("password", "token", "api_key", "TELEGRAM", "ADMIN_TOKEN"):
        assert forbidden not in raw


# =====================================================================
# CLI
# =====================================================================

def test_cli_start_then_progress_then_finish(tmp_path):
    state_path = tmp_path / "s.json"
    lock_path = tmp_path / "s.lock"
    py = sys.executable

    def run_cli(*args):
        return subprocess.run(
            [py, "-m", "scripts.collection_heartbeat", "--state-path", str(state_path), "--lock-path", str(lock_path), *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15,
        )

    r1 = run_cli("start", "--run-id", "cli-run", "--owner-pid", "999", "--stage", "cycle_started", "--message", "hi")
    assert r1.returncode == 0, r1.stderr

    r2 = run_cli("progress", "--run-id", "cli-run", "--owner-pid", "999", "--stage", "s1", "--message", "m1", "--metrics-json", '{"pending_before": 2}')
    assert r2.returncode == 0, r2.stderr

    r3 = run_cli("finish", "--run-id", "cli-run", "--owner-pid", "999", "--stage", "done", "--message", "done", "--status", "success", "--useful-ingestion")
    assert r3.returncode == 0, r3.stderr

    data = json.loads(state_path.read_text())
    assert data["status"] == "success"
    assert data["last_useful_ingestion_at"] is not None
    assert data["owner_pid"] == 999


def test_cli_stale_run_exits_nonzero(tmp_path):
    state_path = tmp_path / "s.json"
    lock_path = tmp_path / "s.lock"
    py = sys.executable

    def run_cli(*args):
        return subprocess.run(
            [py, "-m", "scripts.collection_heartbeat", "--state-path", str(state_path), "--lock-path", str(lock_path), *args],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15,
        )

    run_cli("start", "--run-id", "run-1", "--owner-pid", "1", "--stage", "cycle_started", "--message", "hi")
    result = run_cli("progress", "--run-id", "run-2", "--owner-pid", "2", "--stage", "hijack", "--message", "nope")
    assert result.returncode != 0


def test_cli_invalid_metrics_json_exits_nonzero(tmp_path):
    state_path = tmp_path / "s.json"
    lock_path = tmp_path / "s.lock"
    py = sys.executable

    result = subprocess.run(
        [py, "-m", "scripts.collection_heartbeat", "--state-path", str(state_path), "--lock-path", str(lock_path),
         "start", "--run-id", "run-1", "--owner-pid", "1", "--stage", "cycle_started", "--message", "hi", "--metrics-json", "not json"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert not state_path.exists()


def test_cli_missing_owner_pid_is_rejected(tmp_path):
    state_path = tmp_path / "s.json"
    lock_path = tmp_path / "s.lock"
    py = sys.executable

    result = subprocess.run(
        [py, "-m", "scripts.collection_heartbeat", "--state-path", str(state_path), "--lock-path", str(lock_path),
         "start", "--run-id", "run-1", "--stage", "cycle_started", "--message", "hi"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0


def test_cli_lock_retry_flags_and_malformed_env_fallback(tmp_path, monkeypatch):
    """Malformed retry configuration (env or flag) must fall back to the
    documented default rather than crashing bash arithmetic or the CLI
    itself."""
    state_path = tmp_path / "s.json"
    lock_path = tmp_path / "s.lock"
    py = sys.executable
    env = dict(**{"PATH": "/usr/bin:/bin"})
    env["JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_ATTEMPTS"] = "not-a-number"
    env["JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_DELAY_SECONDS"] = "NaN"

    result = subprocess.run(
        [py, "-m", "scripts.collection_heartbeat", "--state-path", str(state_path), "--lock-path", str(lock_path),
         "start", "--run-id", "run-1", "--owner-pid", "1", "--stage", "cycle_started", "--message", "hi"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=15, env={**env, **{"PYTHONPATH": str(REPO_ROOT)}},
    )
    assert result.returncode == 0, result.stderr
    assert state_path.exists()


@pytest.mark.parametrize("raw,expected", [
    (None, hb.DEFAULT_LOCK_RETRY_ATTEMPTS),
    ("", hb.DEFAULT_LOCK_RETRY_ATTEMPTS),
    ("not-a-number", hb.DEFAULT_LOCK_RETRY_ATTEMPTS),
    ("0", hb.DEFAULT_LOCK_RETRY_ATTEMPTS),  # below minimum (1)
    ("-5", hb.DEFAULT_LOCK_RETRY_ATTEMPTS),
    (str(hb.MAX_LOCK_RETRY_ATTEMPTS + 1), hb.DEFAULT_LOCK_RETRY_ATTEMPTS),
    ("7", 7),
])
def test_parse_bounded_int_fallback_and_validation(raw, expected):
    assert hb._parse_bounded_int(raw, hb.DEFAULT_LOCK_RETRY_ATTEMPTS, 1, hb.MAX_LOCK_RETRY_ATTEMPTS) == expected


@pytest.mark.parametrize("raw", [None, "", "not-a-number", "-1", "NaN", "Infinity", "inf", str(hb.MAX_LOCK_RETRY_DELAY_SECONDS + 100)])
def test_parse_bounded_float_falls_back_on_malformed_or_out_of_range(raw):
    assert hb._parse_bounded_float(raw, hb.DEFAULT_LOCK_RETRY_DELAY_SECONDS, 0.0, hb.MAX_LOCK_RETRY_DELAY_SECONDS) == hb.DEFAULT_LOCK_RETRY_DELAY_SECONDS


def test_parse_bounded_float_accepts_valid_value():
    assert hb._parse_bounded_float("1.5", hb.DEFAULT_LOCK_RETRY_DELAY_SECONDS, 0.0, hb.MAX_LOCK_RETRY_DELAY_SECONDS) == 1.5
