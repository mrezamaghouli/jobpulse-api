"""
Tests for scripts/run_with_deadline.py: the in-container deadline runner
that gives every collector module invocation a SECOND, independent
deadline enforced from inside the process itself (via process-group
signaling), separate from the host-side `timeout` around `docker compose
exec` in scripts/run_collection_cycle_safe.sh.

These tests run the REAL module as a real subprocess -- no Docker, no
PostgreSQL, no mocking of the deadline logic itself. This is what proves
process-group-kill actually terminates a forked grandchild, which a
fake-`docker`-based wrapper test cannot prove (see
tests/test_collection_cycle_wrapper.py's own docstring for why that
distinction matters).

Orphan checks use EXACT recorded PIDs (via a written pidfile + `os.kill(pid, 0)`),
never a broad `pgrep -f "sleep 600"` -- a broad pattern match can collide
with the test shell's own command line or an unrelated process on a busy
CI runner. See docs/PRODUCTION_RUNBOOK.md for why this distinction
matters.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGET_SCRIPT = r"""
import os, subprocess, sys, time
child_pid_file, grandchild_pid_file = sys.argv[1], sys.argv[2]
with open(child_pid_file, "w") as f:
    f.write(str(os.getpid()))
subprocess.Popen([sys.executable, "-c", (
    "import os, time, sys\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "time.sleep(600)\n"
), grandchild_pid_file])
time.sleep(600)
"""


def run_deadline(*args, timeout=15):
    return subprocess.run(
        [sys.executable, "-m", "scripts.run_with_deadline", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout,
    )


def pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal -- still "alive"
    return True


def wait_for_pid_files(*paths: Path, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(p.exists() and p.stat().st_size > 0 for p in paths):
            return
        time.sleep(0.05)
    raise TimeoutError(f"pid files never appeared: {paths}")


def test_normal_command_exit_code_is_propagated_unchanged():
    result = run_deadline("--seconds", "5", "--kill-after", "1", "--", sys.executable, "-c", "import sys; sys.exit(7)")
    assert result.returncode == 7


def test_normal_command_stdout_is_not_swallowed():
    result = run_deadline("--seconds", "5", "--kill-after", "1", "--", sys.executable, "-c", "print('hello-from-child')")
    assert "hello-from-child" in result.stdout


def test_hanging_command_is_killed_within_the_deadline():
    started = time.monotonic()
    result = run_deadline("--seconds", "1", "--kill-after", "1", "--", sys.executable, "-c", "import time; time.sleep(600)")
    elapsed = time.monotonic() - started
    assert result.returncode == 124  # TIMEOUT_EXIT_CODE, matching GNU coreutils `timeout`'s own convention
    assert elapsed < 10


def test_hanging_command_that_forks_a_grandchild_leaves_no_pid_specific_orphan(tmp_path):
    """The core claim this module exists to prove: killing the WHOLE
    process group (not just the direct child) stops a forked grandchild
    too -- verified against the EXACT recorded child/grandchild PIDs, not
    a broad pgrep pattern."""
    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"

    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.run_with_deadline", "--seconds", "2", "--kill-after", "1",
         "--", sys.executable, "-c", TARGET_SCRIPT, str(child_pid_file), str(grandchild_pid_file)],
        cwd=str(REPO_ROOT),
    )
    try:
        wait_for_pid_files(child_pid_file, grandchild_pid_file)
        child_pid = int(child_pid_file.read_text())
        grandchild_pid = int(grandchild_pid_file.read_text())
        assert pid_is_alive(child_pid)
        assert pid_is_alive(grandchild_pid)

        rc = proc.wait(timeout=15)
        assert rc == 124
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    time.sleep(0.5)
    assert not pid_is_alive(child_pid), "target child survived deadline-triggered cleanup"
    assert not pid_is_alive(grandchild_pid), "forked grandchild survived deadline-triggered cleanup"


def test_command_finishing_just_before_the_deadline_is_not_killed():
    result = run_deadline("--seconds", "5", "--kill-after", "1", "--", sys.executable, "-c", "import sys; sys.exit(0)")
    assert result.returncode == 0


def test_missing_command_after_double_dash_is_a_usage_error():
    result = run_deadline("--seconds", "5", "--kill-after", "1", "--")
    assert result.returncode == 2


def test_no_command_at_all_is_a_usage_error():
    result = run_deadline("--seconds", "5", "--kill-after", "1")
    assert result.returncode == 2


def test_kill_after_zero_means_immediate_sigkill_after_sigterm_grace():
    """kill_after=0 is a valid (if aggressive) configuration -- must not
    crash or hang the runner itself."""
    result = run_deadline("--seconds", "1", "--kill-after", "0", "--", sys.executable, "-c", "import time; time.sleep(600)")
    assert result.returncode == 124


# =====================================================================
# Strict argument validation (item 5)
# =====================================================================

@pytest.mark.parametrize("seconds", ["nan", "NaN", "inf", "Infinity", "-inf", "-Infinity"])
def test_seconds_rejects_nan_and_infinite_values(seconds):
    result = run_deadline("--seconds", seconds, "--kill-after", "1", "--", "true")
    assert result.returncode == 2


@pytest.mark.parametrize("kill_after", ["nan", "inf", "-inf"])
def test_kill_after_rejects_nan_and_infinite_values(kill_after):
    result = run_deadline("--seconds", "5", "--kill-after", kill_after, "--", "true")
    assert result.returncode == 2


def test_seconds_zero_is_rejected():
    result = run_deadline("--seconds", "0", "--kill-after", "1", "--", "true")
    assert result.returncode == 2


def test_seconds_negative_is_rejected():
    result = run_deadline("--seconds", "-1", "--kill-after", "1", "--", "true")
    assert result.returncode == 2


def test_kill_after_negative_is_rejected():
    result = run_deadline("--seconds", "5", "--kill-after", "-1", "--", "true")
    assert result.returncode == 2


def test_kill_after_zero_itself_is_valid_not_rejected():
    """kill_after == 0 is a valid (inclusive) minimum -- distinct from
    seconds, whose minimum is exclusive (> 0)."""
    result = run_deadline("--seconds", "5", "--kill-after", "0", "--", sys.executable, "-c", "import sys; sys.exit(0)")
    assert result.returncode == 0


def test_seconds_excessively_large_is_rejected():
    import scripts.run_with_deadline as rwd
    result = run_deadline("--seconds", str(rwd.MAX_SECONDS + 1), "--kill-after", "1", "--", "true")
    assert result.returncode == 2


def test_kill_after_excessively_large_is_rejected():
    import scripts.run_with_deadline as rwd
    result = run_deadline("--seconds", "5", "--kill-after", str(rwd.MAX_KILL_AFTER + 1), "--", "true")
    assert result.returncode == 2


@pytest.mark.parametrize("raw", ["abc", "", "5.5.5", "five", "1_000_000_000_000_000_000_000"])
def test_malformed_seconds_strings_are_rejected(raw):
    result = run_deadline("--seconds", raw, "--kill-after", "1", "--", "true")
    assert result.returncode == 2


def test_boundary_values_are_accepted():
    """Values exactly at the documented bounds must be accepted, not
    off-by-one rejected."""
    import scripts.run_with_deadline as rwd
    result = run_deadline("--seconds", "0.001", "--kill-after", str(rwd.MAX_KILL_AFTER), "--", sys.executable, "-c", "import sys; sys.exit(0)")
    # A very short deadline may or may not let `python -c` finish first --
    # either a clean exit (0) or the deadline firing (124) is acceptable;
    # what matters is the args themselves were not rejected (exit 2).
    assert result.returncode in (0, 124)


# =====================================================================
# External termination signal forwarding (item 3/4) -- SIGTERM, SIGINT,
# SIGHUP delivered to the RUNNER process itself must still terminate the
# target's whole process group, including a forked grandchild.
# =====================================================================

@pytest.mark.parametrize("sig,expected_rc", [
    (signal.SIGTERM, 128 + signal.SIGTERM),
    (signal.SIGINT, 128 + signal.SIGINT),
    (signal.SIGHUP, 128 + signal.SIGHUP),
])
def test_external_signal_to_runner_terminates_target_process_group(tmp_path, sig, expected_rc):
    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"

    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.run_with_deadline", "--seconds", "60", "--kill-after", "2",
         "--", sys.executable, "-c", TARGET_SCRIPT, str(child_pid_file), str(grandchild_pid_file)],
        cwd=str(REPO_ROOT),
    )
    try:
        wait_for_pid_files(child_pid_file, grandchild_pid_file)
        child_pid = int(child_pid_file.read_text())
        grandchild_pid = int(grandchild_pid_file.read_text())
        assert pid_is_alive(child_pid)
        assert pid_is_alive(grandchild_pid)

        started = time.monotonic()
        proc.send_signal(sig)
        rc = proc.wait(timeout=15)
        elapsed = time.monotonic() - started

        assert rc == expected_rc, f"expected exit {expected_rc} (128+{sig}) for signal {sig}, got {rc}"
        assert elapsed < 10, "cleanup took far longer than the configured 2s kill-after grace"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    time.sleep(0.5)
    assert not pid_is_alive(child_pid), f"target child survived {sig!r}-triggered cleanup"
    assert not pid_is_alive(grandchild_pid), f"grandchild survived {sig!r}-triggered cleanup"


def test_signal_arriving_well_before_the_deadline_does_not_wait_for_it():
    """A signal must interrupt the poll loop promptly -- it must not
    require waiting out the remaining --seconds deadline first."""
    child_pid_file_holder = {}

    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.run_with_deadline", "--seconds", "120", "--kill-after", "1",
         "--", sys.executable, "-c", "import time; time.sleep(600)"],
        cwd=str(REPO_ROOT),
    )
    try:
        time.sleep(0.3)  # let the child actually start
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert rc == 128 + signal.SIGTERM
    assert elapsed < 5, f"signal took {elapsed}s to take effect against a 120s deadline -- not promptly interrupted"


def test_normal_completion_does_not_send_any_signal_to_a_reused_pid():
    """Regression guard: on the ordinary (non-timeout, non-signaled) exit
    path, run_with_deadline must never call killpg/terminate at all --
    verified indirectly by confirming a fast, clean exit with the exact
    propagated exit code and no lingering delay that a spurious
    SIGTERM-then-wait cycle would introduce."""
    started = time.monotonic()
    result = run_deadline("--seconds", "30", "--kill-after", "5", "--", sys.executable, "-c", "import sys; sys.exit(3)")
    elapsed = time.monotonic() - started
    assert result.returncode == 3
    assert elapsed < 3  # no grace-period wait was ever triggered
