"""Executable test harness for .github/scripts/tor/provision_production_secret.sh,
plus static audits of .github/workflows/tor-secret-provision.yml and the
Phase 3.1 trigger-regression claim against the existing build/deploy
workflows.

Follows the same discipline as tests/test_tor_dark_launch_script.py: this
file runs the real, version-controlled helper script (never a
simplified/faked reimplementation) with fake `docker`/`curl`/`stat`/`git`/`ln`
binaries placed first on PATH, driven by a small Python dispatcher that
logs every invocation. No network, no real Docker, no real SSH, no real
production secret. Fully deterministic and fast.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "tor" / "provision_production_secret.sh"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tor-secret-provision.yml"
DOCKER_BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
TOR_IMAGE_BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "tor-image-build.yml"
DEPLOY_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

TEST_SHA = "a" * 40
WRONG_SHA = "b" * 40

NEW_FILES = [
    ".github/workflows/tor-secret-provision.yml",
    ".github/scripts/tor/provision_production_secret.sh",
    "tests/test_tor_secret_provision.py",
    ".github/workflows/ci.yml",
    "tests/test_tor_production_dark_launch.py",
]


# =====================================================================
# Fake command dispatchers placed on PATH for the executable-harness
# tests below.
# =====================================================================
FAKE_DOCKER = '''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

state_dir = Path(os.environ["FAKE_STATE_DIR"])
scenario = os.environ.get("FAKE_SCENARIO", "success")

with open(state_dir / "actions.log", "a") as f:
    f.write("docker " + " ".join(sys.argv[1:]) + "\\n")

args = sys.argv[1:]


def next_count(name):
    p = state_dir / name
    n = int(p.read_text()) if p.exists() else 0
    n += 1
    p.write_text(str(n))
    return n


BASE_VALUES = {
    "jobpulse-api-prod": "apiid|apiimage|running|2024-01-01T00:00:00Z|0|healthy",
    "jobpulse-postgres-prod": "dbid|dbimage|running|2024-01-01T00:00:00Z|0|healthy",
    "jobpulse-frontend-prod": "frontendid|frontendimage|running|2024-01-01T00:00:00Z|0|n/a",
}

CHANGED_VALUES = {
    "jobpulse-api-prod": "changedid|apiimage|running|2024-01-01T00:00:01Z|1|healthy",
    "jobpulse-postgres-prod": "changedid|dbimage|running|2024-01-01T00:00:01Z|1|healthy",
    "jobpulse-frontend-prod": "changedid|frontendimage|running|2024-01-01T00:00:01Z|1|n/a",
}

SCENARIO_TO_CHANGED_NAME = {
    "api_snapshot_changed": "jobpulse-api-prod",
    "db_snapshot_changed": "jobpulse-postgres-prod",
    "frontend_snapshot_changed": "jobpulse-frontend-prod",
}

if args and args[0] == "inspect":
    name = args[1]
    fmt = args[3] if len(args) > 3 and args[2] == "--format" else None
    n = next_count(f"inspect_calls_{name}")
    value = BASE_VALUES.get(name, "unknown")
    changed_name = SCENARIO_TO_CHANGED_NAME.get(scenario)
    if changed_name == name and n == 2:
        value = CHANGED_VALUES[name]
    print(value)
    sys.exit(0)

if args and args[0] == "exec":
    n = next_count("exec_calls")
    wrong, correct = "true", "false"
    if scenario == "tor_enabled_wrong_before" and n == 1:
        print(wrong)
    elif scenario == "tor_enabled_wrong_after" and n == 2:
        print(wrong)
    else:
        print(correct)
    sys.exit(0)

if args and args[0] == "ps":
    n = next_count("ps_calls")
    if scenario == "tor_container_present_before" and n == 1:
        print("jobpulse-tor-prod")
    elif scenario == "tor_container_present_after" and n == 2:
        print("jobpulse-tor-prod")
    else:
        print("")
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

if scenario == "api_health_fail_before" and n == 1:
    sys.exit(1)
if scenario == "api_health_fail_after" and n >= 2:
    sys.exit(1)

sys.exit(0)
'''

FAKE_STAT = '''#!/usr/bin/env python3
import os
import subprocess
import sys

scenario = os.environ.get("FAKE_SCENARIO", "success")
args = sys.argv[1:]

if scenario == "existing_wrong_owner" and args[:2] == ["-c", "%u"]:
    print("999999")
    sys.exit(0)

result = subprocess.run(["/usr/bin/stat", *args])
sys.exit(result.returncode)
'''

FAKE_GIT = '''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:2] == ["rev-parse", "HEAD"]:
    print(os.environ.get("GIT_FAKE_HEAD_SHA", "0" * 40))
    sys.exit(0)
sys.exit(1)
'''

FAKE_LN = '''#!/usr/bin/env python3
import os
import subprocess
import sys

scenario = os.environ.get("FAKE_SCENARIO", "success")
args = sys.argv[1:]

if scenario == "concurrent_creation":
    dest = args[-1]
    if not os.path.exists(dest):
        with open(dest, "w") as f:
            f.write("race-winner-sentinel-value-not-a-real-secret\\n")
        os.chmod(dest, 0o600)
    sys.exit(1)

result = subprocess.run(["/usr/bin/ln", *args])
sys.exit(result.returncode)
'''


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def harness(tmp_path):
    """Fake PATH with docker/curl/stat/git/ln, plus a helper to run the
    real script against a disposable ROOT via TOR_SECRET_PROVISION_TEST_MODE."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", FAKE_DOCKER)
    _write_executable(bin_dir / "curl", FAKE_CURL)
    _write_executable(bin_dir / "stat", FAKE_STAT)
    _write_executable(bin_dir / "git", FAKE_GIT)
    _write_executable(bin_dir / "ln", FAKE_LN)

    state_root = tmp_path / "state"
    state_root.mkdir()
    call_counter = {"n": 0}

    class Harness:
        def new_root(self):
            call_counter["n"] += 1
            root = tmp_path / f"root_{call_counter['n']}"
            root.mkdir()
            return root

        def run(self, root, scenario="success", expected_sha=TEST_SHA,
                 git_head_sha=TEST_SHA, extra_env=None):
            call_counter["n"] += 1
            state_dir = state_root / f"call_{call_counter['n']}"
            state_dir.mkdir()

            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "TOR_SECRET_PROVISION_TEST_MODE": "1",
                "TOR_SECRET_PROVISION_TEST_ROOT": str(root),
                "EXPECTED_PRODUCTION_SHA": expected_sha,
                "GIT_FAKE_HEAD_SHA": git_head_sha,
                "FAKE_STATE_DIR": str(state_dir),
                "FAKE_SCENARIO": scenario,
                "HOME": str(tmp_path),
            }
            if extra_env:
                env.update(extra_env)

            result = subprocess.run(
                ["bash", str(SCRIPT_PATH)],
                capture_output=True, text=True, timeout=30, env=env,
            )
            actions_log = (state_dir / "actions.log").read_text() if (state_dir / "actions.log").exists() else ""
            return result, actions_log

    return Harness()


def _secret_path(root: Path) -> Path:
    return root / ".tor_control_password"


# =====================================================================
# SHA gate
# =====================================================================
def test_wrong_production_sha_fails_before_any_secret_check(harness):
    root = harness.new_root()
    result, actions_log = harness.run(root, git_head_sha=WRONG_SHA)

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_SHA_MISMATCH" in result.stderr
    assert not _secret_path(root).exists()
    assert actions_log == ""  # no docker/curl invocation happened at all


def test_missing_expected_sha_fails(harness):
    root = harness.new_root()
    result, _ = harness.run(root, expected_sha="")

    assert result.returncode != 0
    assert "EXPECTED_PRODUCTION_SHA is required" in result.stderr


def test_malformed_expected_sha_fails(harness):
    root = harness.new_root()
    result, _ = harness.run(root, expected_sha="not-a-sha")

    assert result.returncode != 0
    assert "must be exactly 40 lowercase hex characters" in result.stderr


# =====================================================================
# Test-mode root guard
# =====================================================================
def test_custom_root_rejected_outside_test_mode(tmp_path):
    root = tmp_path / "sneaky_root"
    root.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "TOR_SECRET_PROVISION_TEST_ROOT": str(root),
        "EXPECTED_PRODUCTION_SHA": TEST_SHA,
        "HOME": str(tmp_path),
    }
    result = subprocess.run(["bash", str(SCRIPT_PATH)], capture_output=True, text=True, timeout=30, env=env)

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_TEST_ROOT must not be set outside test mode" in result.stderr


# =====================================================================
# First creation / idempotency
# =====================================================================
def test_first_creation_success(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="success")

    assert result.returncode == 0, result.stderr
    assert "TOR_SECRET_STATUS=created" in result.stdout
    assert "TOR_SECRET_PROVISION_COMPLETE" in result.stdout

    secret_file = _secret_path(root)
    assert secret_file.is_file()
    assert not secret_file.is_symlink()
    mode = stat.S_IMODE(secret_file.stat().st_mode)
    assert oct(mode) == "0o600"
    assert secret_file.stat().st_uid == os.getuid()


def test_generated_secret_is_exactly_64_lowercase_hex_chars(harness):
    root = harness.new_root()
    harness.run(root, scenario="success")

    secret_file = _secret_path(root)
    raw_bytes = secret_file.read_bytes()
    assert len(raw_bytes) == 65  # 64 hex chars + exactly one trailing newline

    content = secret_file.read_text()
    assert content.rstrip("\n") != ""
    stripped = content.rstrip("\n")
    assert len(stripped) == 64
    assert all(c in "0123456789abcdef" for c in stripped)


def test_second_execution_reports_already_present_valid_and_file_unchanged(harness):
    root = harness.new_root()
    harness.run(root, scenario="success")

    secret_file = _secret_path(root)
    first_inode = secret_file.stat().st_ino
    first_content = secret_file.read_bytes()

    result2, _ = harness.run(root, scenario="success")

    assert result2.returncode == 0, result2.stderr
    assert "TOR_SECRET_STATUS=already_present_valid" in result2.stdout
    assert secret_file.stat().st_ino == first_inode
    assert secret_file.read_bytes() == first_content


# =====================================================================
# Existing-secret validation failures -- must never modify the file
# =====================================================================
def test_symlink_secret_rejected_without_modification(harness):
    root = harness.new_root()
    target = root / "real_secret_elsewhere"
    target.write_text("a" * 64 + "\n")
    target.chmod(0o600)
    secret_file = _secret_path(root)
    secret_file.symlink_to(target)

    result, actions_log = harness.run(root)

    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert secret_file.is_symlink()
    assert secret_file.resolve() == target.resolve()  # untouched, still points at target
    # BEFORE invariants (read-only) run ahead of the secret decision, but
    # no mutation of any kind happens -- only inspect/exec/ps.
    docker_subcommands = {
        line.split(" ", 2)[1] for line in actions_log.splitlines() if line.startswith("docker ")
    }
    assert docker_subcommands <= {"inspect", "exec", "ps"}, docker_subcommands


def test_non_regular_target_rejected(harness):
    root = harness.new_root()
    _secret_path(root).mkdir()

    result, _ = harness.run(root)

    assert result.returncode != 0
    assert "is not a regular file" in result.stderr


def test_wrong_mode_rejected_without_modification(harness):
    root = harness.new_root()
    secret_file = _secret_path(root)
    secret_file.write_text("a" * 64 + "\n")
    secret_file.chmod(0o644)

    result, _ = harness.run(root)

    assert result.returncode != 0
    assert "must be mode 600" in result.stderr
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o644  # unchanged
    assert secret_file.read_text() == "a" * 64 + "\n"


def test_empty_existing_file_rejected_without_modification(harness):
    root = harness.new_root()
    secret_file = _secret_path(root)
    secret_file.write_text("")
    secret_file.chmod(0o600)

    result, _ = harness.run(root)

    assert result.returncode != 0
    assert "is empty" in result.stderr
    assert secret_file.read_text() == ""


def test_unreasonable_size_rejected(harness):
    root = harness.new_root()
    secret_file = _secret_path(root)
    secret_file.write_text("a" * 5000)
    secret_file.chmod(0o600)

    result, _ = harness.run(root)

    assert result.returncode != 0
    assert "larger than expected" in result.stderr
    assert len(secret_file.read_text()) == 5000  # unchanged


def test_wrong_owner_rejected(harness):
    root = harness.new_root()
    secret_file = _secret_path(root)
    secret_file.write_text("a" * 64 + "\n")
    secret_file.chmod(0o600)

    result, _ = harness.run(root, scenario="existing_wrong_owner")

    assert result.returncode != 0
    assert "must be owned by the user executing this script" in result.stderr


# =====================================================================
# TOCTOU / no-clobber
# =====================================================================
def test_concurrent_target_appearance_fails_without_overwrite(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="concurrent_creation")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_CONCURRENT_CREATION" in result.stderr

    secret_file = _secret_path(root)
    assert secret_file.read_text() == "race-winner-sentinel-value-not-a-real-secret\n"
    # no leftover temp file
    assert not list(root.glob(".tor_control_password.tmp.*"))


# =====================================================================
# Production invariants
# =====================================================================
def test_api_health_fail_before_aborts_with_no_mutation(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="api_health_fail_before")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_API_HEALTH_FAILED" in result.stderr
    assert not _secret_path(root).exists()


def test_api_health_fail_after_reported_even_though_secret_already_created(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="api_health_fail_after")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_API_HEALTH_FAILED" in result.stderr
    # secret creation itself is not rolled back -- it already succeeded
    # before the AFTER invariant ran
    assert _secret_path(root).exists()


def test_tor_enabled_wrong_before_aborts_with_no_mutation(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="tor_enabled_wrong_before")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_TOR_ENABLED_WRONG" in result.stderr
    assert not _secret_path(root).exists()


def test_tor_container_present_before_aborts_with_no_mutation(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="tor_container_present_before")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_TOR_CONTAINER_PRESENT" in result.stderr
    assert not _secret_path(root).exists()


def test_tor_container_present_after_fails(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="tor_container_present_after")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_TOR_CONTAINER_PRESENT" in result.stderr


def test_api_snapshot_changed_fails(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="api_snapshot_changed")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_API_SNAPSHOT_CHANGED" in result.stderr


def test_db_snapshot_changed_fails(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="db_snapshot_changed")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_DB_SNAPSHOT_CHANGED" in result.stderr


def test_frontend_snapshot_changed_fails(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="frontend_snapshot_changed")

    assert result.returncode != 0
    assert "TOR_SECRET_PROVISION_FRONTEND_SNAPSHOT_CHANGED" in result.stderr


def test_success_path_reports_no_docker_mutation_only_reads(harness):
    root = harness.new_root()
    result, actions_log = harness.run(root, scenario="success")

    assert result.returncode == 0, result.stderr
    docker_subcommands = {
        line.split(" ", 2)[1]
        for line in actions_log.splitlines()
        if line.startswith("docker ")
    }
    assert docker_subcommands <= {"inspect", "exec", "ps"}, docker_subcommands


# =====================================================================
# Redaction / logging discipline
# =====================================================================
def test_no_secret_value_in_stdout_or_stderr(harness):
    root = harness.new_root()
    result, actions_log = harness.run(root, scenario="success")

    secret_value = _secret_path(root).read_text().rstrip("\n")
    assert secret_value not in result.stdout
    assert secret_value not in result.stderr
    assert secret_value not in actions_log


def test_no_shell_trace_output(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="success")

    assert not any(line.startswith("+ ") for line in result.stderr.splitlines())


def test_report_status_includes_only_metadata_fields(harness):
    root = harness.new_root()
    result, _ = harness.run(root, scenario="success")

    status_lines = [l for l in result.stdout.splitlines() if l.startswith("path=")]
    assert status_lines, result.stdout
    line = status_lines[0]
    for field in ("path=", "owner_uid=", "owner_name=", "mode=", "bytes="):
        assert field in line
    assert "600" in line


def _code_only(source: str) -> str:
    """Strip full-line `#` comments so static substring checks don't false-
    positive on prose (e.g. a comment explaining why `mv -f` is NOT used).
    Inline trailing comments are left alone -- none of the forbidden tokens
    below are expected to appear there either."""
    return "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )


# =====================================================================
# Static source-level checks (script)
# =====================================================================
def test_script_has_no_set_x():
    source = SCRIPT_PATH.read_text()
    assert "set -x" not in source


def test_script_never_dumps_secret_via_forbidden_commands():
    import re
    code = _code_only(SCRIPT_PATH.read_text())
    for forbidden in ("cat \"$SECRET_FILE\"", "cat $SECRET_FILE",
                       "cat \"$TMP_FILE\"", "cat $TMP_FILE",
                       "head ", "tail ", "xxd ", "sha256sum"):
        assert forbidden not in code, forbidden
    # Broader than the two exact forms above: no `cat` invocation of any
    # kind against either secret path should exist anywhere in the
    # script -- the temp-secret content is read via the `read` builtin
    # instead (see test_generated_value_read_via_builtin_not_cat).
    assert not re.search(r"(?<![A-Za-z])cat(?![A-Za-z])", code)
    # word-boundary match for `od` -- a plain substring check false-
    # positives on "...-prod " (every container name here ends in "prod")
    assert not re.search(r"(?<![A-Za-z])od(?![A-Za-z])", code)


def test_generated_value_read_via_builtin_not_cat():
    code = _code_only(SCRIPT_PATH.read_text())
    assert 'read -r GENERATED_VALUE < "$TMP_FILE"' in code
    assert "TMP_BYTES\" -eq 65" in code


def test_script_never_invokes_tor_control_or_mutation_commands():
    code = _code_only(SCRIPT_PATH.read_text())
    for forbidden in ("docker compose up", "docker run", "docker start",
                       "docker restart", "docker rm", "docker stop",
                       "NEWNYM", "AUTHENTICATE", "SETEVENTS",
                       "SOCKS", "9050", "9051"):
        assert forbidden not in code, forbidden


def test_script_health_safe_inspect_template_present():
    """Section 11: frontend containers without a healthcheck must be
    reported as n/a instead of causing a Go-template failure."""
    source = SCRIPT_PATH.read_text()
    assert "{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}" in source


def test_script_uses_hardlink_not_mv_or_cp_to_publish():
    code = _code_only(SCRIPT_PATH.read_text())
    assert 'ln "$TMP_FILE" "$SECRET_FILE"' in code
    assert "mv -f" not in code
    assert "mv \"$TMP_FILE\"" not in code


# =====================================================================
# Static YAML checks (workflow)
# =====================================================================
def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _on_block(data: dict):
    # PyYAML (1.1 resolver) parses the bare `on:` key as boolean True.
    if "on" in data:
        return data["on"]
    return data[True]


def test_workflow_is_workflow_dispatch_only():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert set(on_block.keys()) == {"workflow_dispatch"}


def test_workflow_source_has_no_other_trigger_keys():
    source = WORKFLOW_PATH.read_text()
    for forbidden in ("push:", "pull_request:", "workflow_run:", "schedule:"):
        assert forbidden not in source, forbidden


def test_workflow_requires_confirmation_input():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    inputs = on_block["workflow_dispatch"]["inputs"]
    assert "confirm" in inputs
    assert inputs["confirm"]["required"] is True
    assert inputs["confirm"]["type"] == "string"


def test_workflow_confirmation_is_validated_against_exact_string():
    source = WORKFLOW_PATH.read_text()
    assert 'CONFIRM_INPUT" != "PROVISION_TOR_SECRET"' in source


def test_workflow_uses_only_vm_ssh_key_secret():
    data = _load_workflow(WORKFLOW_PATH)
    source = WORKFLOW_PATH.read_text()
    referenced_secrets = set(__import__("re").findall(r"secrets\.([A-Za-z0-9_]+)", source))
    assert referenced_secrets == {"VM_SSH_KEY"}


def test_workflow_has_no_password_secret_or_env():
    source = WORKFLOW_PATH.read_text()
    for forbidden in ("TOR_CONTROL_PASSWORD", "password:", "PASSWORD:"):
        assert forbidden not in source, forbidden


def test_workflow_pins_expected_production_sha_not_as_input():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    inputs = on_block["workflow_dispatch"]["inputs"]
    assert "expected_production_sha" not in {k.lower() for k in inputs}
    assert "sha" not in {k.lower() for k in inputs}

    source = WORKFLOW_PATH.read_text()
    assert "EXPECTED_PRODUCTION_SHA: \"5dffbd669eec52f5283503bb6409a430509175a0\"" in source


def test_workflow_executes_exact_checked_out_helper_via_stdin():
    source = WORKFLOW_PATH.read_text()
    assert "< .github/scripts/tor/provision_production_secret.sh" in source
    assert "actions/checkout@v4" in source


def test_workflow_permissions_are_read_only():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["permissions"] == {"contents": "read"}


def test_workflow_never_invokes_tor_control_commands():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("docker compose up", "docker run", "NEWNYM",
                       "AUTHENTICATE", "SETEVENTS", "SOCKS", "9050", "9051"):
        assert forbidden not in code, forbidden


# =====================================================================
# Trigger-regression audit (Section 16)
# =====================================================================
def _path_matches_filter(rel_path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return rel_path == prefix or rel_path.startswith(prefix + "/")
    return rel_path == pattern


def _push_paths(workflow_path: Path):
    data = _load_workflow(workflow_path)
    on_block = _on_block(data)
    return on_block["push"]["paths"]


def test_docker_build_paths_do_not_match_new_files():
    patterns = _push_paths(DOCKER_BUILD_PATH)
    for new_file in NEW_FILES:
        for pattern in patterns:
            assert not _path_matches_filter(new_file, pattern), (new_file, pattern)


def test_tor_image_build_paths_do_not_match_new_files():
    patterns = _push_paths(TOR_IMAGE_BUILD_PATH)
    for new_file in NEW_FILES:
        for pattern in patterns:
            assert not _path_matches_filter(new_file, pattern), (new_file, pattern)


def test_deploy_workflow_run_targets_only_api_image_build_name():
    data = _load_workflow(DEPLOY_PATH)
    on_block = _on_block(data)
    assert on_block["workflow_run"]["workflows"] == ["Build JobPulse API Image"]

    provision_data = _load_workflow(WORKFLOW_PATH)
    assert provision_data["name"] == "Provision Production Tor Secret"
    assert provision_data["name"] != "Build JobPulse API Image"


def test_new_workflow_name_is_distinct_from_build_and_dark_launch_workflows():
    provision_data = _load_workflow(WORKFLOW_PATH)
    docker_build_data = _load_workflow(DOCKER_BUILD_PATH)
    tor_image_build_data = _load_workflow(TOR_IMAGE_BUILD_PATH)

    names = {docker_build_data["name"], tor_image_build_data["name"]}
    assert provision_data["name"] not in names


def test_docker_build_paths_do_not_match_ci_yml():
    """ci.yml is now also part of the Phase 3.1 file scope -- it must not
    accidentally fall under docker-build.yml's / tor-image-build.yml's own
    push-path filters either."""
    for workflow_path in (DOCKER_BUILD_PATH, TOR_IMAGE_BUILD_PATH):
        patterns = _push_paths(workflow_path)
        for pattern in patterns:
            assert not _path_matches_filter(".github/workflows/ci.yml", pattern), pattern


def test_ci_yml_runs_the_new_test_file():
    """Proves tests/test_tor_secret_provision.py is now part of ordinary
    deterministic CI (the focused, network-free Tor unit suite), not left
    unexercised by normal push/PR CI."""
    source = CI_PATH.read_text()
    assert "tests/test_tor_secret_provision.py" in source


def test_ci_yml_triggers_on_push_and_pull_request_to_main():
    data = _load_workflow(CI_PATH)
    on_block = _on_block(data)
    assert "push" in on_block and on_block["push"]["branches"] == ["main"]
    assert "pull_request" in on_block and on_block["pull_request"]["branches"] == ["main"]


def test_ci_yml_real_tor_job_gating_unchanged():
    """Section 5: this correction pass must not touch the existing manual
    gating of the real-Tor network job."""
    source = CI_PATH.read_text()
    assert "run_real_tor" in source
    assert "tests/test_tor_real_integration.py" in source


# =====================================================================
# bash -n syntax sanity (also run standalone per Section 17)
# =====================================================================
def test_script_passes_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
