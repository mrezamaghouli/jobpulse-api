"""Executable test harness for scripts/tor/production_dark_launch.sh.

Static YAML parsing of the old tor-dark-launch.yml heredoc did NOT catch
the missing-SECRET_FILE-initialization / JOBPULSE_TOR_IMAGE-propagation
gaps found in review -- only actually EXECUTING the shell logic can. This
file runs the real, version-controlled script (never a simplified/faked
reimplementation) with fake `docker`/`curl`/`stat` binaries placed first
on PATH, driven by a small Python dispatcher that logs every invocation
and simulates a wide range of scenarios (success, every post-start
failure path, API-preflight failures before any mutation, and secret-file
hardening failures).

No network, no real Docker, no real SSH, no real secret. Fully
deterministic and fast.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "tor" / "production_dark_launch.sh"

VALID_IMAGE = "ghcr.io/mrezamaghouli/jobpulse-tor:" + "a" * 40

PULLED_IMAGE_ID = "sha256:" + "a" * 64
MISMATCHED_IMAGE_ID = "sha256:" + "0" * 64


FAKE_DOCKER = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_dir = Path(os.environ["FAKE_STATE_DIR"])
scenario = os.environ.get("FAKE_SCENARIO", "success")
expected_image = os.environ.get("EXPECTED_TOR_IMAGE", "")
pulled_image_id = os.environ.get("FAKE_PULLED_IMAGE_ID", "sha256:" + "a" * 64)
mismatched_image_id = os.environ.get("FAKE_MISMATCHED_IMAGE_ID", "sha256:" + "0" * 64)

with open(state_dir / "actions.log", "a") as f:
    f.write("docker " + " ".join(sys.argv[1:]) + "\\n")

args = sys.argv[1:]


def _next_count(name):
    counter_file = state_dir / name
    n = int(counter_file.read_text()) if counter_file.exists() else 0
    n += 1
    counter_file.write_text(str(n))
    return n


if args[:2] == ["login", "ghcr.io"]:
    sys.stdin.read()
    sys.exit(0)

if args[:2] == ["image", "inspect"]:
    fmt = None
    for j, a in enumerate(args):
        if a == "--format":
            fmt = args[j + 1]
    if fmt == "{{.Id}}":
        print(pulled_image_id)
        sys.exit(0)
    if fmt == "{{json .RepoDigests}}":
        print(json.dumps(["ghcr.io/mrezamaghouli/jobpulse-tor@sha256:" + "d" * 64]))
        sys.exit(0)
    print("unknown")
    sys.exit(0)

if args and args[0] == "pull":
    sys.exit(0)

if args and args[0] == "compose":
    rest = args[1:]
    i = 0
    while i < len(rest) and rest[i] == "-f":
        i += 2
    if i < len(rest) and rest[i] == "--profile":
        i += 2
    sub = rest[i]

    if sub == "config":
        n = _next_count("config_calls")

        tor_enabled_value = "false"
        depends_on = {"db": {}}

        if scenario == "api_tor_enabled_before_wrong" and n == 1:
            tor_enabled_value = "true"
        if scenario == "api_depends_on_tor_before" and n == 2:
            depends_on = {"db": {}, "tor": {}}
        if scenario == "api_tor_enabled_after_wrong" and n == 3:
            tor_enabled_value = "true"
        if scenario == "api_depends_on_tor_after" and n == 4:
            depends_on = {"db": {}, "tor": {}}

        config = {
            "services": {
                "api": {
                    "environment": {"TOR_ENABLED": tor_enabled_value},
                    "depends_on": depends_on,
                },
                "tor": {},
            }
        }
        print(json.dumps(config))
        sys.exit(0)

    if sub == "up":
        if scenario == "tor_up_partial_failure":
            sys.exit(1)
        sys.exit(0)

    if sub == "run":
        sys.exit(1 if scenario == "diagnostic_fail" else 0)

    if sub == "stop" or sub == "rm":
        sys.exit(0)

    if sub == "ps":
        if scenario == "unexpected_post_start_command_failure":
            sys.exit(1)
        print("(fake ps output)")
        sys.exit(0)

    sys.exit(0)

if args and args[0] == "inspect":
    fmt = None
    for j, a in enumerate(args):
        if a == "--format":
            fmt = args[j + 1]

    if fmt and "State.Health.Status" in fmt:
        if scenario == "tor_unhealthy":
            print("unhealthy")
        else:
            counter_file = state_dir / "health_calls"
            n = int(counter_file.read_text()) if counter_file.exists() else 0
            n += 1
            counter_file.write_text(str(n))
            print("healthy" if n >= 2 else "starting")
        sys.exit(0)

    if fmt and "Config.Image" in fmt:
        if scenario == "image_mismatch":
            print("ghcr.io/mrezamaghouli/jobpulse-tor:" + "0" * 40)
        else:
            print(expected_image)
        sys.exit(0)

    if fmt == "{{.Image}}":
        if scenario == "image_id_mismatch":
            print(mismatched_image_id)
        else:
            print(pulled_image_id)
        sys.exit(0)

    if fmt and fmt.count("|") == 4:
        n = _next_count("snapshot_calls")
        if scenario == "api_snapshot_changed" and n >= 2:
            print("changedid|changedimage|running|2024-01-01T00:00:01Z|1")
        else:
            snapshot_file = state_dir / "api_snapshot"
            if not snapshot_file.exists():
                snapshot_file.write_text("fixedid|fixedimage|running|2024-01-01T00:00:00Z|0")
            print(snapshot_file.read_text().strip())
        sys.exit(0)

    print("unknown")
    sys.exit(0)

if args and args[0] == "exec":
    n = _next_count("exec_calls")
    wrong = "true"
    correct = "false"

    if scenario == "running_api_tor_enabled_before_wrong" and n == 1:
        print(wrong)
    elif scenario == "running_api_tor_enabled_wrong" and n >= 2:
        print(wrong)
    else:
        print(correct)
    sys.exit(0)

sys.exit(0)
'''

FAKE_CURL = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

scenario = os.environ.get("FAKE_SCENARIO", "success")
state_dir = Path(os.environ["FAKE_STATE_DIR"])

with open(state_dir / "actions.log", "a") as f:
    f.write("curl " + " ".join(sys.argv[1:]) + "\\n")

counter_file = state_dir / "curl_calls"
n = int(counter_file.read_text()) if counter_file.exists() else 0
n += 1
counter_file.write_text(str(n))

if scenario == "api_health_before_fail" and n == 1:
    sys.exit(1)
if scenario == "api_health_after_fail" and n >= 2:
    sys.exit(1)

sys.exit(0)
'''

FAKE_STAT = '''#!/usr/bin/env python3
import os
import subprocess
import sys

scenario = os.environ.get("FAKE_SCENARIO", "success")
args = sys.argv[1:]

if scenario == "secret_wrong_owner" and args[:2] == ["-c", "%u"]:
    print("999999")
    sys.exit(0)

result = subprocess.run(["/usr/bin/stat", *args])
sys.exit(result.returncode)
'''


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def harness(tmp_path):
    """Sets up: a fake PATH with docker/curl/stat, a disposable
    JOBPULSE_DARK_LAUNCH_DIR containing the compose files + a valid
    mode-600 secret file, and returns a function that runs the real
    script with a given scenario."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", FAKE_DOCKER)
    _write_executable(bin_dir / "curl", FAKE_CURL)
    _write_executable(bin_dir / "stat", FAKE_STAT)

    launch_dir = tmp_path / "opt_jobpulse"
    launch_dir.mkdir()
    (launch_dir / "docker-compose.prod.yml").write_text("services: {}\n")
    (launch_dir / "docker-compose.prod.tor.yml").write_text("services: {}\n")

    secret_file = launch_dir / ".tor_control_password"
    secret_file.write_text("fake-password")
    secret_file.chmod(0o600)

    state_root = tmp_path / "state"
    state_root.mkdir()
    call_counter = {"n": 0}

    def run(scenario="success", image=VALID_IMAGE, ghcr_token="", extra_env=None,
            secret_mode=None, secret_exists=True, secret_symlink=False):
        # Each call gets its OWN fresh state dir -- the fake docker/curl
        # dispatchers keep call counters (health/config/exec/snapshot/curl
        # call numbers) inside FAKE_STATE_DIR to distinguish e.g. the
        # BEFORE vs AFTER invocation of the same check. Sharing one
        # state dir across multiple run() calls in the same test (as a
        # single fixture-wide directory would) would let counters leak
        # between unrelated scenarios and silently corrupt those checks.
        call_counter["n"] += 1
        state_dir = state_root / f"call_{call_counter['n']}"
        state_dir.mkdir()

        if secret_symlink:
            target = launch_dir / "real_secret_elsewhere"
            target.write_text("fake-password")
            target.chmod(0o600)
            secret_file.unlink()
            secret_file.symlink_to(target)
        if secret_mode is not None:
            secret_file.chmod(secret_mode)
        if not secret_exists:
            secret_file.unlink()

        image_b64 = subprocess.run(
            ["base64", "-w", "0"], input=image, capture_output=True, text=True, check=True,
        ).stdout
        token_b64 = subprocess.run(
            ["base64", "-w", "0"], input=ghcr_token, capture_output=True, text=True, check=True,
        ).stdout

        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "TOR_IMAGE_B64": image_b64,
            "GHCR_TOKEN_B64": token_b64,
            "JOBPULSE_DARK_LAUNCH_DIR": str(launch_dir),
            "JOBPULSE_DARK_LAUNCH_HEALTH_RETRIES": "5",
            "JOBPULSE_DARK_LAUNCH_HEALTH_SLEEP_SECONDS": "0",
            "FAKE_STATE_DIR": str(state_dir),
            "FAKE_SCENARIO": scenario,
            "EXPECTED_TOR_IMAGE": image,
            "FAKE_PULLED_IMAGE_ID": PULLED_IMAGE_ID,
            "FAKE_MISMATCHED_IMAGE_ID": MISMATCHED_IMAGE_ID,
        }
        if extra_env:
            env.update(extra_env)

        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True, text=True, timeout=30, env=env,
        )
        actions_log = (state_dir / "actions.log").read_text() if (state_dir / "actions.log").exists() else ""
        return result, actions_log

    return run


def _mutation_lines(actions_log):
    return [
        line for line in actions_log.splitlines()
        if line.startswith("docker compose")
        and (" up " in f" {line} " or " stop " in f" {line} " or " rm " in f" {line} ")
    ] + [
        line for line in actions_log.splitlines()
        if line.startswith("docker login") or line.startswith("docker pull")
    ]


def test_secret_file_initialized_before_use(harness):
    """SECRET_FILE must be checked (existence + mode) using the fixed,
    locally-defined path -- proven here by making the file mode-644
    instead of 600 and requiring the script to fail closed on that exact
    precondition, before ever starting Tor."""
    result, actions_log = harness(secret_mode=0o644)

    assert result.returncode != 0
    assert "must be mode 600" in result.stderr
    assert "up" not in actions_log


def test_secret_file_missing_fails_closed(harness):
    result, actions_log = harness(secret_exists=False)

    assert result.returncode != 0
    assert "production secret missing" in result.stderr
    assert "up" not in actions_log


def test_secret_file_symlink_rejected(harness):
    result, actions_log = harness(secret_symlink=True)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert not _mutation_lines(actions_log), actions_log


def test_secret_file_wrong_owner_rejected(harness):
    result, actions_log = harness(scenario="secret_wrong_owner")

    assert result.returncode != 0
    assert "must be owned by the user executing this script" in result.stderr
    assert not _mutation_lines(actions_log), actions_log


def test_jobpulse_tor_image_equals_exact_validated_input(harness):
    """The script must export JOBPULSE_TOR_IMAGE equal to the exact
    decoded, validated input -- proven end to end via the success path,
    where the fake docker inspect Config.Image check (which compares
    against EXPECTED_TOR_IMAGE, itself set to the same `image` argument)
    must match, and the overall run must reach the final success marker."""
    result, _ = harness(scenario="success")

    assert "PRODUCTION_TOR_DARK_LAUNCH_READY" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0


def test_compose_config_calls_receive_jobpulse_tor_image_env(harness, monkeypatch):
    """Directly proves JOBPULSE_TOR_IMAGE is present in the environment
    at the moment `docker compose ... config` runs -- the fake docker
    dispatcher only ever emits valid config JSON; a bash script that
    forgot to export JOBPULSE_TOR_IMAGE before invoking
    docker-compose.prod.tor.yml (which requires it via `:?`) would, in
    the REAL compose CLI, fail immediately with a "required variable"
    error. This test additionally inspects the script's own source to
    pin that the export happens textually before the first `docker
    compose ... -f "$TOR_COMPOSE"` call, which is what actually
    guarantees this for the real (non-faked) Compose CLI."""
    source = SCRIPT_PATH.read_text()
    export_index = source.index('export JOBPULSE_TOR_IMAGE="$TOR_IMAGE"')
    first_tor_compose_call = source.index('docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE"')

    assert export_index < first_tor_compose_call, (
        "JOBPULSE_TOR_IMAGE must be exported before the first docker compose "
        "invocation that references $TOR_COMPOSE"
    )

    result, _ = harness(scenario="success")
    assert result.returncode == 0


def test_tor_only_start_used_not_full_stack(harness):
    result, actions_log = harness(scenario="success")

    assert result.returncode == 0
    up_lines = [line for line in actions_log.splitlines() if " up " in f" {line} "]
    assert up_lines, actions_log
    for line in up_lines:
        assert line.strip().endswith(" tor"), f"up command did not target only tor: {line}"


def test_no_api_db_frontend_restart_on_success(harness):
    _, actions_log = harness(scenario="success")

    forbidden_substrings = ["restart api", "restart db", "restart frontend", "rm -f api", "rm -f db", "rm -f frontend", "stop api", "stop db", "stop frontend"]
    for forbidden in forbidden_substrings:
        assert forbidden not in actions_log, f"forbidden action found: {forbidden}"


def _assert_rolled_back_only_tor(actions_log):
    stop_rm_lines = [l for l in actions_log.splitlines() if l.startswith("docker compose") and (" stop " in f" {l} " or " rm " in f" {l} ")]
    assert stop_rm_lines, actions_log
    for line in stop_rm_lines:
        assert line.strip().endswith(" tor"), f"rollback touched more than tor: {line}"

    forbidden_substrings = ["restart api", "restart db", "restart frontend", "stop api", "stop db", "stop frontend", "up api", "up db", "up frontend"]
    for forbidden in forbidden_substrings:
        assert forbidden not in actions_log, f"forbidden action found: {forbidden}"


def test_tor_unhealthy_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="tor_unhealthy")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_TOR_UNHEALTHY" in result.stderr
    assert "TOR_DARK_LAUNCH_CLEANUP_TRAP" in result.stderr
    _assert_rolled_back_only_tor(actions_log)
    assert "PRODUCTION_TOR_DARK_LAUNCH_READY" not in result.stdout


def test_tor_up_partial_failure_still_rolls_back_only_tor(harness):
    """Compose can partially create/start the tor container and still
    return non-zero -- proves TOR_START_ATTEMPTED (armed immediately
    BEFORE the `up` call, not only after it succeeds) still triggers the
    centralized cleanup trap in exactly that case, not just when `up`
    itself reports success. The fake docker dispatcher fails the `up ...
    tor` call outright for this scenario; the mutation is still recorded
    in actions_log (every docker invocation is logged unconditionally,
    win or lose) and cleanup must attempt stop/rm on tor anyway."""
    result, actions_log = harness(scenario="tor_up_partial_failure")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_CLEANUP_TRAP" in result.stderr

    up_lines = [line for line in actions_log.splitlines() if " up " in f" {line} " and line.strip().endswith(" tor")]
    assert up_lines, actions_log

    _assert_rolled_back_only_tor(actions_log)
    assert "PRODUCTION_TOR_DARK_LAUNCH_READY" not in result.stdout


def test_diagnostic_failure_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="diagnostic_fail")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_DIAGNOSTIC_FAILED" in result.stderr
    assert "TOR_DARK_LAUNCH_CLEANUP_TRAP" in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_image_mismatch_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="image_mismatch")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_IMAGE_MISMATCH" in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_image_id_mismatch_rolls_back_only_tor(harness):
    """A tag string alone is not cryptographically immutable -- this
    proves the SEPARATE, authoritative image-ID comparison (running
    container's .Image vs the exact ID captured right after `docker
    pull`) also triggers a Tor-only rollback when it disagrees, even
    though the tag-based check above it passed."""
    result, actions_log = harness(scenario="image_id_mismatch")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_IMAGE_ID_MISMATCH" in result.stderr
    assert PULLED_IMAGE_ID in result.stderr
    assert MISMATCHED_IMAGE_ID in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_api_health_after_fail_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="api_health_after_fail")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_API_HEALTH_AFTER_FAILED" in result.stderr
    assert "TOR_DARK_LAUNCH_CLEANUP_TRAP" in result.stderr
    _assert_rolled_back_only_tor(actions_log)
    assert "PRODUCTION_TOR_DARK_LAUNCH_READY" not in result.stdout


def test_api_snapshot_changed_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="api_snapshot_changed")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_API_SNAPSHOT_CHANGED" in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_api_tor_enabled_after_wrong_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="api_tor_enabled_after_wrong")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_API_TOR_ENABLED_AFTER_WRONG" in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_api_depends_on_tor_after_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="api_depends_on_tor_after")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_API_DEPENDS_ON_TOR_AFTER" in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_running_api_tor_enabled_wrong_after_rolls_back_only_tor(harness):
    result, actions_log = harness(scenario="running_api_tor_enabled_wrong")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_RUNNING_API_TOR_ENABLED_WRONG" in result.stderr
    _assert_rolled_back_only_tor(actions_log)


def test_unexpected_post_start_command_failure_rolls_back_only_tor(harness):
    """Proves the CENTRALIZED cleanup trap, not a per-call-site rollback:
    the final `docker compose ... ps` call is not wrapped in any explicit
    tor_only_rollback() call in the script's source, yet an unexpected
    failure there (under `set -e`) must still trigger exactly the same
    Tor-only rollback via the EXIT trap."""
    result, actions_log = harness(scenario="unexpected_post_start_command_failure")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_CLEANUP_TRAP" in result.stderr
    _assert_rolled_back_only_tor(actions_log)
    assert "PRODUCTION_TOR_DARK_LAUNCH_READY" not in result.stdout


def test_api_health_failure_before_launch_performs_no_tor_mutation(harness):
    """If the API is already unhealthy BEFORE any Tor action, the script
    must abort with ZERO Tor-related mutation -- no pull-then-rollback,
    no `up`, no `stop`/`rm` of tor either (there is nothing to roll back
    because nothing was ever started), and no GHCR login/pull either."""
    result, actions_log = harness(scenario="api_health_before_fail")

    assert result.returncode != 0
    assert "API health check failed BEFORE Tor launch" in result.stderr
    assert not _mutation_lines(actions_log), actions_log


def test_api_tor_enabled_before_wrong_performs_no_mutation(harness):
    result, actions_log = harness(scenario="api_tor_enabled_before_wrong")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_API_TOR_ENABLED_BEFORE_WRONG" in result.stderr
    assert not _mutation_lines(actions_log), actions_log


def test_api_depends_on_tor_before_performs_no_mutation(harness):
    result, actions_log = harness(scenario="api_depends_on_tor_before")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_API_DEPENDS_ON_TOR_BEFORE" in result.stderr
    assert not _mutation_lines(actions_log), actions_log


def test_running_api_tor_enabled_before_wrong_performs_no_mutation(harness):
    """Section 4: the RUNNING api container's own TOR_ENABLED must be
    checked BEFORE Tor start, not merely the resolved compose config --
    proven by making the (faked) running container report TOR_ENABLED=true
    on the very first `docker exec` check and requiring zero GHCR/Tor
    mutation as a result."""
    result, actions_log = harness(scenario="running_api_tor_enabled_before_wrong")

    assert result.returncode != 0
    assert "TOR_DARK_LAUNCH_RUNNING_API_TOR_ENABLED_BEFORE_WRONG" in result.stderr
    assert not _mutation_lines(actions_log), actions_log


def test_preflight_runs_before_any_ghcr_or_pull(harness):
    """Section 3: API preflight (health, baseline, all invariants) must
    run BEFORE `docker login`/`docker pull`/`docker compose up` -- proven
    by pinning the textual order in the script's own source (the same
    discipline used above for JOBPULSE_TOR_IMAGE export ordering)."""
    source = SCRIPT_PATH.read_text()

    preflight_marker = source.index("dark_launch_invariants_before_ok")
    login_marker = source.index("docker login ghcr.io")
    pull_marker = source.index("timeout 180 docker pull")
    up_marker = source.index('docker compose -f "$BASE_COMPOSE" -f "$TOR_COMPOSE" up -d --no-build tor')

    assert preflight_marker < login_marker < pull_marker < up_marker


def test_no_secret_contents_in_stdout_or_stderr(harness):
    result, actions_log = harness(scenario="success", ghcr_token="super-secret-ghcr-token-value")

    assert "super-secret-ghcr-token-value" not in result.stdout
    assert "super-secret-ghcr-token-value" not in result.stderr
    assert "super-secret-ghcr-token-value" not in actions_log
    assert "fake-password" not in result.stdout
    assert "fake-password" not in result.stderr


def test_ready_marker_appears_only_after_every_gate_succeeds(harness):
    failing_scenarios = (
        "tor_unhealthy", "diagnostic_fail", "image_mismatch", "image_id_mismatch",
        "tor_up_partial_failure",
        "api_health_before_fail", "api_health_after_fail", "api_snapshot_changed",
        "api_tor_enabled_before_wrong", "api_tor_enabled_after_wrong",
        "api_depends_on_tor_before", "api_depends_on_tor_after",
        "running_api_tor_enabled_before_wrong", "running_api_tor_enabled_wrong",
        "unexpected_post_start_command_failure",
    )
    for scenario in failing_scenarios:
        result, _ = harness(scenario=scenario)
        assert "PRODUCTION_TOR_DARK_LAUNCH_READY" not in result.stdout, (
            f"ready marker must never appear for a failing scenario ({scenario})"
        )

    success_result, _ = harness(scenario="success")
    assert "PRODUCTION_TOR_DARK_LAUNCH_READY" in success_result.stdout
    assert success_result.returncode == 0


def test_success_path_captures_pulled_image_id_and_repo_digest(harness):
    result, _ = harness(scenario="success")

    assert result.returncode == 0
    assert f"pulled_image_id={PULLED_IMAGE_ID}" in result.stdout
    assert "tor_image_repo_digests=" in result.stdout
    assert "sha256:" in result.stdout


def test_cleanup_trap_disarmed_only_on_final_success(harness):
    """The script must remove its own EXIT trap (`trap - EXIT`) only
    immediately before the final success marker -- pinned textually so a
    future edit can't accidentally disarm it earlier (which would defeat
    the centralized cleanup guarantee for a late-stage failure)."""
    source = SCRIPT_PATH.read_text()

    disarm_index = source.index("trap - EXIT")
    ready_index = source.index("PRODUCTION_TOR_DARK_LAUNCH_READY")
    invariants_after_index = source.index("dark_launch_invariants_after_ok")

    assert invariants_after_index < disarm_index < ready_index


def test_rejects_invalid_image_reference_even_if_workflow_gate_is_bypassed(harness):
    """Defense in depth: the script itself must reject a bad image even
    though the workflow's own separate validation step should already
    have caught it -- this script must be safe to invoke on its own."""
    for bad_image in ("ghcr.io/mrezamaghouli/jobpulse-tor:latest",
                       "ghcr.io/mrezamaghouli/jobpulse-tor:main",
                       "ghcr.io/mrezamaghouli/jobpulse-tor:abc123",
                       "docker.io/mrezamaghouli/jobpulse-tor:" + "a" * 40,
                       "ghcr.io/someoneelse/jobpulse-tor:" + "a" * 40,
                       ""):
        result, actions_log = harness(scenario="success", image=bad_image or "x")
        if bad_image == "":
            continue
        assert result.returncode != 0, f"expected rejection for image {bad_image!r}"
        assert "failed strict validation" in result.stderr
        assert "up" not in actions_log
