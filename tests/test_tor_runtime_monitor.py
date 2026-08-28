"""Phase 3.4D -- Tor Runtime Monitor: design + tests.

Two kinds of coverage against .github/workflows/tor-runtime-monitor.yml:

1. Static source/YAML audit (same discipline as
   tests/test_production_runtime_diagnostic.py): trigger shape,
   permissions, secrets usage, forbidden-command/forbidden-Tor-token
   absence, metadata-only docker inspect templates, bash -n syntax on
   every embedded script block.

2. Behavioral audit of the classification decision table: the
   `classify_evidence` bash function is extracted verbatim from the
   workflow file and actually EXECUTED (via `bash -c`) with fabricated
   evidence environment variables for each of the 20 required scenarios.
   No Docker daemon, no SSH, no Tor daemon, no network is involved --
   the function only ever reads shell variables that this test sets
   directly, exactly as production evidence-parsing would set them from
   the (separately, statically audited) SSH evidence-gathering step.

This mirrors the black-box execution style of
tests/test_tor_dark_launch_script.py (which runs the real
production_dark_launch.sh with fake docker/curl on PATH) without needing
any fake binaries here at all, since classify_evidence performs no I/O of
its own.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "tor-runtime-monitor.yml"
DOCKER_BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
TOR_IMAGE_BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "tor-image-build.yml"
DEPLOY_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

EXPECTED_PRODUCTION_SHA = "148bd362b37c82c92737382d181fbdeac4d2187b"
EXPECTED_API_IMAGE = "ghcr.io/mrezamaghouli/jobpulse-api:148bd362b37c82c92737382d181fbdeac4d2187b"
EXPECTED_TOR_IMAGE = "ghcr.io/mrezamaghouli/jobpulse-tor:5dffbd669eec52f5283503bb6409a430509175a0"

CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PHASE_3_4D_CHANGED_FILES = [
    ".github/workflows/tor-runtime-monitor.yml",
    ".github/workflows/ci.yml",
    "tests/test_tor_runtime_monitor.py",
    "tests/test_tor_production_dark_launch.py",
]


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _on_block(data: dict):
    if "on" in data:
        return data["on"]
    return data[True]


def _code_only(source: str) -> str:
    return "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )


# =====================================================================
# Structure / trigger / permissions / secrets
# =====================================================================
def test_workflow_file_exists_and_parses_as_yaml():
    assert WORKFLOW_PATH.is_file()
    data = _load_workflow(WORKFLOW_PATH)
    assert data["name"] == "Tor Runtime Monitor"


def test_workflow_dispatch_remains_enabled():
    """Phase 3.4I adds `schedule:` alongside -- not instead of --
    `workflow_dispatch`, so manual runs remain possible."""
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert "workflow_dispatch" in on_block
    # No inputs were ever defined and none are added by this phase.
    assert on_block["workflow_dispatch"] is None


def test_workflow_source_has_no_disallowed_trigger_keys():
    """Checks executable YAML only. `schedule:` is now legitimate (Phase
    3.4I); push/pull_request/workflow_run remain forbidden -- adding this
    workflow must never be reachable from an ordinary push/PR/other
    workflow completion."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("push:", "pull_request:", "workflow_run:"):
        assert forbidden not in code, forbidden


def test_workflow_permissions_are_read_only():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["permissions"] == {"contents": "read"}


def test_workflow_concurrency_is_manual_and_non_cancelling():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["concurrency"] == {
        "group": "jobpulse-tor-runtime-monitor",
        "cancel-in-progress": False,
    }


def test_workflow_uses_only_vm_ssh_key_secret():
    source = WORKFLOW_PATH.read_text()
    referenced_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", source))
    assert referenced_secrets == {"VM_SSH_KEY"}


def test_no_notification_or_issue_secrets_or_integrations():
    """Phase 3.4D is a monitoring execution surface only -- no alerting
    integration exists yet."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("SLACK", "WEBHOOK", "issues.create", "gh issue create",
                       "smtp", "sendmail", "createComment"):
        assert forbidden.lower() not in code.lower(), forbidden


def test_workflow_has_no_hardcoded_password_or_secret_material():
    source = WORKFLOW_PATH.read_text()
    for forbidden in ("TOR_CONTROL_PASSWORD", "password:", "PASSWORD:"):
        assert forbidden not in source, forbidden


# =====================================================================
# No mutation commands anywhere in the workflow
# =====================================================================
FORBIDDEN_MUTATION_COMMANDS = (
    "docker compose up",
    "docker compose down",
    "docker start",
    "docker stop",
    "docker restart",
    "docker rm",
    "docker run",
    "docker create",
    "docker pull",
    "docker push",
    "git reset",
    "git checkout",
    "git pull",
    "git fetch",
    "chown",
)


def test_workflow_contains_no_mutation_commands():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in FORBIDDEN_MUTATION_COMMANDS:
        assert forbidden not in code, forbidden


def test_no_remediation_language_or_action_anywhere():
    """Belt-and-suspenders: no code path may ever attempt to fix what it
    finds -- detection/reporting only, exit-code signaling only."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("docker compose restart", "systemctl restart",
                       "kill -", "docker kill"):
        assert forbidden not in code, forbidden


# =====================================================================
# No secret reads / Tor operation / environment inspection
# =====================================================================
FORBIDDEN_SECRET_AND_TOR_TOKENS = (
    ".tor_control_password",
    "/run/secrets/tor_control_password",
    "NEWNYM",
    "request_new_identity",
    "AUTHENTICATE",
    "SETEVENTS",
    "SOCKS",
    "ControlPort",
    "9050",
    "9051",
)


def test_workflow_never_reads_secrets_or_operates_tor():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in FORBIDDEN_SECRET_AND_TOR_TOKENS:
        assert forbidden not in code, forbidden


def test_no_docker_logs():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "docker logs" not in code


def test_no_external_http_url_introduced():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("check.torproject.org", "linkedin.com", "://api.linkedin"):
        assert forbidden.lower() not in code.lower(), forbidden

    urls = re.findall(r"https?://[^\s'\"]+", code)
    assert urls, "expected at least the two localhost probes"
    for url in urls:
        assert url.startswith("http://127.0.0.1"), url


def test_existing_localhost_probes_present():
    source = WORKFLOW_PATH.read_text()
    assert "http://127.0.0.1:8000/health" in source
    assert "http://127.0.0.1/" in source


def test_workflow_never_inspects_docker_environment_or_config():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert ".Config.Env" not in code
    assert "docker inspect" in code
    for line in code.splitlines():
        if "docker inspect" not in line:
            continue
        assert "--format" in line, line


def test_no_forbidden_fields_anywhere_in_inspect_templates():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden_field in ("Mounts", "Binds", "Secrets", "HostConfig"):
        assert forbidden_field not in code, forbidden_field


def test_docker_exec_appears_exactly_once_and_is_narrowly_scoped():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert code.count("docker exec") == 1
    assert "docker exec jobpulse-api-prod printenv TOR_ENABLED" in WORKFLOW_PATH.read_text()
    assert re.search(r"docker exec\s+\S+\s+env\b", code) is None
    assert re.search(r"printenv\s*(\n|$|['\"])", code) is None


def test_docker_exec_never_targets_the_tor_container():
    code = _code_only(WORKFLOW_PATH.read_text())
    for line in code.splitlines():
        if "docker exec" not in line:
            continue
        assert "jobpulse-tor-prod" not in line, line


def test_docker_port_command_present():
    assert "docker port jobpulse-tor-prod" in WORKFLOW_PATH.read_text()


def test_metadata_observation_covers_exactly_api_and_tor_containers():
    """Unlike production-runtime-diagnostic.yml (which inspects all four
    application containers for general observability), the monitor's
    classification inputs are scoped to exactly the two containers whose
    state feeds the severity model: jobpulse-api-prod (image drift,
    TOR_ENABLED) and jobpulse-tor-prod (presence/status/health/restart
    count/image/ports). Postgres/frontend metadata is not part of this
    Phase 3.4D decision table."""
    source = WORKFLOW_PATH.read_text()
    assert "jobpulse-api-prod" in source
    assert "jobpulse-tor-prod" in source


# =====================================================================
# Expected constants
# =====================================================================
def test_expected_production_sha_literal_is_exact():
    source = WORKFLOW_PATH.read_text()
    assert f'EXPECTED_PRODUCTION_SHA="{EXPECTED_PRODUCTION_SHA}"' in source


def test_expected_api_image_literal_is_exact():
    source = WORKFLOW_PATH.read_text()
    assert f'EXPECTED_API_IMAGE="{EXPECTED_API_IMAGE}"' in source


def test_expected_tor_image_literal_is_exact():
    source = WORKFLOW_PATH.read_text()
    assert f'EXPECTED_TOR_IMAGE="{EXPECTED_TOR_IMAGE}"' in source


def test_reference_tor_restart_count_is_fixed_baseline_not_hard_requirement():
    source = WORKFLOW_PATH.read_text()
    assert 'REFERENCE_TOR_RESTART_COUNT="0"' in source
    # Must never be a workflow_dispatch input (typo-safety discipline) --
    # workflow_dispatch carries no `inputs:` block, regardless of whether
    # `schedule:` is also present alongside it.
    data = _load_workflow(WORKFLOW_PATH)
    assert _on_block(data)["workflow_dispatch"] is None


def test_none_of_the_expected_constants_derived_from_github_sha():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "github.sha" not in code


# =====================================================================
# Exit-code behavior
# =====================================================================
def test_critical_returns_nonzero_ok_and_warning_return_zero():
    source = WORKFLOW_PATH.read_text()
    assert 'return 2' in source
    assert 'return 0' in source
    # The final step must propagate classify_evidence's own exit code,
    # not swallow it.
    assert 'exit "$MONITOR_EXIT"' in source


# =====================================================================
# bash -n syntax sanity for every embedded script block
# =====================================================================
def _extract_remote_heredoc(source: str) -> str:
    m = re.search(r"<<'REMOTE'[^\n]*\n(.*?)\n\s*REMOTE\n", source, re.DOTALL)
    assert m, "expected a <<'REMOTE' ... REMOTE heredoc block"
    return m.group(1)


def _extract_step_block(source: str, step_name: str, next_marker: str) -> str:
    # `.*?` (DOTALL, non-greedy) rather than requiring an immediate blank
    # line before next_marker -- a step's run block may be followed by an
    # explanatory comment block (itself valid bash, since every line
    # starts with `#`) before the next step's `- name:` line.
    pattern = rf"- name: {re.escape(step_name)}.*?\n(\s+run: \|\n)(.*?)\n\s*{re.escape(next_marker)}"
    m = re.search(pattern, source, re.DOTALL)
    assert m, f"expected to find step {step_name!r}"
    block = m.group(2)
    lines = block.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    return "\n".join(l[indent:] if l.startswith(" " * indent) else l for l in lines)


def _extract_last_step_block(source: str, step_name: str) -> str:
    idx = source.index(f"- name: {step_name}")
    tail = source[idx:]
    m = re.search(r"run: \|\n(.*)", tail, re.DOTALL)
    assert m
    block = m.group(1)
    lines = block.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    return "\n".join(l[indent:] if l.startswith(" " * indent) else l for l in lines)


def test_remote_heredoc_passes_bash_syntax_check(tmp_path):
    source = WORKFLOW_PATH.read_text()
    remote_script = _extract_remote_heredoc(source)
    script_path = tmp_path / "remote.sh"
    script_path.write_text(remote_script)
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_evidence_step_passes_bash_syntax_check(tmp_path):
    source = WORKFLOW_PATH.read_text()
    script = _extract_step_block(
        source,
        "Gather production runtime evidence (read-only)",
        "- name: Classify",
    )
    script_path = tmp_path / "evidence.sh"
    script_path.write_text(script)
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_classify_step_passes_bash_syntax_check(tmp_path):
    source = WORKFLOW_PATH.read_text()
    script = _extract_step_block(
        source,
        "Classify evidence (deterministic, local -- no SSH, no Docker, no Tor)",
        "- name: Report execution status",
    )
    # Substitute the GitHub Actions expression for a literal path so bash
    # -n can parse the file exactly as it will be interpolated at runtime.
    script = script.replace(
        '${{ steps.evidence.outputs.output_file }}', '/tmp/placeholder'
    )
    script_path = tmp_path / "classify.sh"
    script_path.write_text(script)
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_final_step_passes_bash_syntax_check(tmp_path):
    source = WORKFLOW_PATH.read_text()
    script = _extract_final_step_script(source)
    script_path = tmp_path / "final_step.sh"
    script_path.write_text(script)
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_remote_block_never_redirects_output_into_a_new_file():
    source = WORKFLOW_PATH.read_text()
    remote_code = _code_only(_extract_remote_heredoc(source))
    for line in remote_code.splitlines():
        stripped = re.sub(r"\d?>&\d", "", line)
        stripped = re.sub(r">\s*/dev/null", "", stripped)
        assert not re.search(r"[^|]>\s*\S", stripped), line


def test_remote_block_is_quoted_heredoc_not_expanded_locally():
    assert "<<'REMOTE'" in WORKFLOW_PATH.read_text()


def test_no_git_fetch_reset_checkout_or_pull_anywhere():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("git fetch", "git reset", "git checkout", "git pull"):
        assert forbidden not in code, forbidden


# =====================================================================
# No build/deploy trigger path is matched by this new file (real
# path-filter audit against the actual repository workflow files, not
# assumed).
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


def test_docker_build_paths_do_not_match_any_phase_3_4d_changed_file():
    patterns = _push_paths(DOCKER_BUILD_PATH)
    for changed_file in PHASE_3_4D_CHANGED_FILES:
        for pattern in patterns:
            assert not _path_matches_filter(changed_file, pattern), (changed_file, pattern)


def test_tor_image_build_paths_do_not_match_any_phase_3_4d_changed_file():
    patterns = _push_paths(TOR_IMAGE_BUILD_PATH)
    for changed_file in PHASE_3_4D_CHANGED_FILES:
        for pattern in patterns:
            assert not _path_matches_filter(changed_file, pattern), (changed_file, pattern)


def test_docker_build_and_tor_image_build_have_push_paths_defined():
    assert len(_push_paths(DOCKER_BUILD_PATH)) > 0
    assert len(_push_paths(TOR_IMAGE_BUILD_PATH)) > 0


def test_deploy_production_reacts_only_to_build_jobpulse_api_image():
    data = _load_workflow(DEPLOY_PATH)
    on_block = _on_block(data)
    assert set(on_block.keys()) <= {"workflow_run", "workflow_dispatch"}
    assert on_block["workflow_run"]["workflows"] == ["Build JobPulse API Image"]

    monitor_data = _load_workflow(WORKFLOW_PATH)
    assert monitor_data["name"] != "Build JobPulse API Image"


def test_new_workflow_name_is_distinct_from_other_workflows():
    data = _load_workflow(WORKFLOW_PATH)
    for other in WORKFLOW_PATH.parent.glob("*.yml"):
        if other == WORKFLOW_PATH:
            continue
        other_data = _load_workflow(other)
        assert other_data.get("name") != data["name"], other


def test_no_other_workflow_references_the_new_workflow_by_name():
    data = _load_workflow(WORKFLOW_PATH)
    for other in WORKFLOW_PATH.parent.glob("*.yml"):
        if other == WORKFLOW_PATH:
            continue
        other_data = _load_workflow(other)
        on_block = _on_block(other_data)
        if isinstance(on_block, dict) and "workflow_run" in on_block:
            workflows = on_block["workflow_run"].get("workflows", [])
            assert data["name"] not in workflows, other


# =====================================================================
# CI self-coverage: prevents this test file from silently falling out of
# the focused Tor suite in a future edit.
# =====================================================================
def test_ci_yml_runs_the_phase_3_4d_monitor_test_file():
    """Mirrors test_ci_yml_runs_the_phase_3_1a_test_file in
    tests/test_production_runtime_diagnostic.py: proves
    tests/test_tor_runtime_monitor.py is part of the actual focused Tor
    pytest command in ci.yml, not merely present in the repo unexercised.
    Checks the exact 'Run focused Tor unit suite' step's `run:` block
    specifically -- not just anywhere in the file -- so a stray mention
    elsewhere (e.g. a comment) cannot satisfy this guard."""
    source = CI_PATH.read_text()
    step_match = re.search(
        r"- name: Run focused Tor unit suite.*?\n\s+run: \|\n(.*?)\n\n\s*- name:",
        source, re.DOTALL,
    )
    assert step_match, "expected to find the 'Run focused Tor unit suite' step"
    pytest_command = step_match.group(1)
    assert "tests/test_tor_runtime_monitor.py" in pytest_command


def test_ci_yml_real_tor_gate_unchanged():
    """This hardening pass must not touch the existing manual gating of
    the real-Tor network job."""
    source = CI_PATH.read_text()
    assert "github.event_name == 'workflow_dispatch' && inputs.run_real_tor == true" in source


# =====================================================================
# Behavioral: extract classify_evidence() and execute it for real with
# fabricated evidence. No Docker, no SSH, no Tor, no network.
# =====================================================================
def _extract_classify_function(source: str) -> str:
    m = re.search(r"(classify_evidence\(\) \{.*?\} # end classify_evidence)", source, re.DOTALL)
    assert m, "expected classify_evidence() function with an `} # end classify_evidence` marker"
    return m.group(1)


BASE_EVIDENCE = {
    "TOR_CONTAINER_PRESENT": "yes",
    "TOR_STATUS": "running",
    "TOR_HEALTH": "healthy",
    "TOR_RESTART_COUNT": "0",
    "REFERENCE_TOR_RESTART_COUNT": "0",
    "TOR_IMAGE_MATCH": "yes",
    "TOR_PUBLISHED_PORTS": "NONE",
    "API_HEALTH_RC": "0",
    "API_HEALTH_BODY": '{"status":"ok","database":"connected"}',
    "API_IMAGE_MATCH": "yes",
    "API_TOR_ENABLED_ACTUAL": "false",
    "PRODUCTION_SHA_MATCH": "yes",
}


def _run_classify(tmp_path, overrides=None, source=None):
    """Extract classify_evidence() from the workflow (or from a caller-
    supplied source, for the no-remediation-command test) and execute it
    in a fresh bash process with BASE_EVIDENCE plus any overrides set as
    environment variables. Returns (stdout_dict, exit_code)."""
    source = source if source is not None else WORKFLOW_PATH.read_text()
    func_src = _extract_classify_function(source)

    evidence = dict(BASE_EVIDENCE)
    if overrides:
        evidence.update(overrides)

    script_path = tmp_path / "classify_fn.sh"
    script_path.write_text(func_src + "\nclassify_evidence\n")

    env = {"PATH": "/usr/bin:/bin"}
    env.update(evidence)

    result = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True, text=True, env=env,
    )
    fields = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k] = v
    return fields, result.returncode


# 1. all healthy -> OK
def test_scenario_all_healthy_is_ok(tmp_path):
    fields, rc = _run_classify(tmp_path)
    assert fields["monitor_status"] == "OK"
    assert fields["warning_count"] == "0"
    assert fields["critical_count"] == "0"
    assert rc == 0


# 2. Tor missing -> CRITICAL
def test_scenario_tor_missing_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"TOR_CONTAINER_PRESENT": "no"})
    assert fields["monitor_status"] == "CRITICAL"
    assert int(fields["critical_count"]) >= 1
    assert rc == 2


# 3. Tor exited -> CRITICAL
def test_scenario_tor_exited_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"TOR_STATUS": "exited"})
    assert fields["monitor_status"] == "CRITICAL"
    assert rc == 2


# 4. Tor unhealthy -> CRITICAL
def test_scenario_tor_unhealthy_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"TOR_HEALTH": "unhealthy"})
    assert fields["monitor_status"] == "CRITICAL"
    assert rc == 2


# 5. restart_count 0 -> 1 -> WARNING
def test_scenario_restart_count_increase_is_warning(tmp_path):
    fields, rc = _run_classify(tmp_path, {"TOR_RESTART_COUNT": "1"})
    assert fields["tor_restart_count_changed"] == "yes"
    assert fields["monitor_status"] == "WARNING"
    assert int(fields["warning_count"]) >= 1
    assert fields["critical_count"] == "0"
    assert rc == 0


# 6. wrong Tor image -> WARNING
def test_scenario_wrong_tor_image_is_warning(tmp_path):
    fields, rc = _run_classify(tmp_path, {"TOR_IMAGE_MATCH": "no"})
    assert fields["monitor_status"] == "WARNING"
    assert rc == 0


# 7. Tor published host port -> CRITICAL
def test_scenario_tor_published_port_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"TOR_PUBLISHED_PORTS": "0.0.0.0:9050->9050/tcp"})
    assert fields["monitor_status"] == "CRITICAL"
    assert rc == 2


# 8. TOR_ENABLED=true -> CRITICAL
def test_scenario_tor_enabled_true_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"API_TOR_ENABLED_ACTUAL": "true"})
    assert fields["monitor_status"] == "CRITICAL"
    assert fields["api_tor_enabled_match"] == "no"
    assert rc == 2


# 9. TOR_ENABLED missing/UNKNOWN -> CRITICAL
def test_scenario_tor_enabled_unknown_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"API_TOR_ENABLED_ACTUAL": "UNKNOWN"})
    assert fields["monitor_status"] == "CRITICAL"
    assert rc == 2


# 10. API image drift -> WARNING
def test_scenario_api_image_drift_is_warning(tmp_path):
    fields, rc = _run_classify(tmp_path, {"API_IMAGE_MATCH": "no"})
    assert fields["monitor_status"] == "WARNING"
    assert rc == 0


# 11. production SHA drift -> WARNING
def test_scenario_production_sha_drift_is_warning(tmp_path):
    fields, rc = _run_classify(tmp_path, {"PRODUCTION_SHA_MATCH": "no"})
    assert fields["monitor_status"] == "WARNING"
    assert rc == 0


# 12. API health failure -> CRITICAL
def test_scenario_api_health_failure_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {"API_HEALTH_RC": "7", "API_HEALTH_BODY": "NONE"})
    assert fields["monitor_status"] == "CRITICAL"
    assert fields["api_health_ok"] == "no"
    assert rc == 2


# 13. database disconnected -> CRITICAL
def test_scenario_database_disconnected_is_critical(tmp_path):
    fields, rc = _run_classify(
        tmp_path, {"API_HEALTH_BODY": '{"status":"degraded","database":"disconnected"}'}
    )
    assert fields["database_connected"] == "no"
    assert fields["monitor_status"] == "CRITICAL"
    assert rc == 2


# 14. multiple warnings aggregate correctly
def test_scenario_multiple_warnings_aggregate(tmp_path):
    fields, rc = _run_classify(tmp_path, {
        "TOR_RESTART_COUNT": "1",
        "TOR_IMAGE_MATCH": "no",
        "API_IMAGE_MATCH": "no",
        "PRODUCTION_SHA_MATCH": "no",
    })
    assert fields["warning_count"] == "4"
    assert fields["critical_count"] == "0"
    assert fields["monitor_status"] == "WARNING"
    assert rc == 0


# 15. warning + critical -> CRITICAL
def test_scenario_warning_plus_critical_is_critical(tmp_path):
    fields, rc = _run_classify(tmp_path, {
        "TOR_IMAGE_MATCH": "no",          # warning
        "TOR_STATUS": "exited",           # critical
    })
    assert int(fields["warning_count"]) >= 1
    assert int(fields["critical_count"]) >= 1
    assert fields["monitor_status"] == "CRITICAL"
    assert rc == 2


# 16. no automatic remediation commands exist
def test_no_remediation_commands_in_source():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in FORBIDDEN_MUTATION_COMMANDS:
        assert forbidden not in code, forbidden


# 17. no ControlPort
def test_no_controlport_in_source():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "ControlPort" not in code


# 18. no SOCKS
def test_no_socks_in_source():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "SOCKS" not in code


# 19. no NEWNYM
def test_no_newnym_in_source():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "NEWNYM" not in code
    assert "request_new_identity" not in code


# 20. no external URLs
def test_no_external_urls_in_source():
    code = _code_only(WORKFLOW_PATH.read_text())
    urls = re.findall(r"https?://[^\s'\"]+", code)
    for url in urls:
        assert url.startswith("http://127.0.0.1"), url


# =====================================================================
# Extra: classify_evidence never touches the filesystem/network itself
# =====================================================================
def test_classify_function_body_contains_no_io_or_network_calls():
    """The function extracted and executed above must be pure -- no ssh,
    docker, curl, git, or file redirection anywhere inside its body. All
    I/O happens in the (separately, statically audited) evidence step,
    never inside classify_evidence itself."""
    func_src = _extract_classify_function(WORKFLOW_PATH.read_text())
    code = _code_only(func_src)
    for forbidden in ("ssh ", "docker ", "curl ", "git ", " > ", " >> "):
        assert forbidden not in code, forbidden


# =====================================================================
# Phase 3.4H -- unattended-run hardening: bounded execution time,
# operator-visible WARNING/CRITICAL annotations, job summary. (Phase 3.4H
# itself added no schedule; Phase 3.4I below activates one alongside
# workflow_dispatch -- see that section for the current trigger shape.)
# =====================================================================
def test_workflow_dispatch_still_present_after_hardening():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert "workflow_dispatch" in on_block
    assert on_block["workflow_dispatch"] is None


def test_monitor_job_has_five_minute_timeout():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["jobs"]["monitor"]["timeout-minutes"] == 5


def test_ssh_keyscan_has_finite_timeout():
    source = WORKFLOW_PATH.read_text()
    assert "ssh-keyscan -T 10" in source


def test_ssh_connection_has_batch_mode():
    source = WORKFLOW_PATH.read_text()
    assert "-o BatchMode=yes" in source


def test_ssh_connection_has_connect_timeout():
    source = WORKFLOW_PATH.read_text()
    assert "-o ConnectTimeout=10" in source


def test_ssh_connection_has_single_connection_attempt():
    source = WORKFLOW_PATH.read_text()
    assert "-o ConnectionAttempts=1" in source


def test_no_strict_host_key_checking_disabled():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "StrictHostKeyChecking=no" not in code
    assert "StrictHostKeyChecking no" not in code


def test_no_retry_loop_introduced():
    """No looping construct wraps the ssh/ssh-keyscan calls -- a single
    bounded attempt only, never a retry-until-success loop."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("while ", "until ", "for i in", "retry"):
        assert forbidden not in code.lower(), forbidden


def test_no_sleep_introduced():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "sleep " not in code
    assert "sleep\n" not in code


def test_no_notification_integration_introduced_by_hardening():
    """Phase 3.4H adds only GitHub Actions workflow-command annotations
    (::warning::/::error::) and $GITHUB_STEP_SUMMARY -- no Slack/email/
    Telegram/Discord/webhook/issue-creation/PagerDuty/SMS integration and
    no new secret."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("SLACK", "WEBHOOK", "TELEGRAM", "DISCORD", "PAGERDUTY",
                       "issues.create", "gh issue create", "smtp", "sendmail",
                       "createComment", "twilio"):
        assert forbidden.lower() not in code.lower(), forbidden
    secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", WORKFLOW_PATH.read_text()))
    assert secrets == {"VM_SSH_KEY"}


def test_github_step_summary_is_used():
    source = WORKFLOW_PATH.read_text()
    assert "$GITHUB_STEP_SUMMARY" in source


def test_warning_and_error_annotation_syntax_present():
    source = WORKFLOW_PATH.read_text()
    assert "::warning::" in source
    assert "::error::" in source


def test_summary_written_before_final_exit_propagation():
    """Structural ordering guard: the $GITHUB_STEP_SUMMARY write must
    appear before the final `exit "$MONITOR_EXIT"` in the classify step's
    source, so a CRITICAL run (exit 2) still produces its summary and
    annotation first -- `set -e` must never short-circuit past it."""
    source = WORKFLOW_PATH.read_text()
    summary_idx = source.index('>> "$GITHUB_STEP_SUMMARY"')
    exit_idx = source.rindex('exit "$MONITOR_EXIT"')
    assert summary_idx < exit_idx


def test_classify_evidence_output_still_piped_not_swallowed():
    """The annotation/summary logic must observe classify_evidence's real
    exit code via PIPESTATUS after piping through `tee`, not silently
    replace it with tee's own (always-zero) exit code."""
    source = WORKFLOW_PATH.read_text()
    assert "classify_evidence | tee" in source
    assert 'MONITOR_EXIT="${PIPESTATUS[0]}"' in source


# =====================================================================
# Phase 3.4H -- behavioral: execute the ENTIRE classify step (not just
# classify_evidence()) with fabricated evidence, a fake OUTPUT_FILE, and
# a fake $GITHUB_STEP_SUMMARY. No Docker, no SSH, no Tor, no network --
# GITHUB_STEP_SUMMARY is just a local temp file the same way the real
# runner provides one.
# =====================================================================
EVIDENCE_LINES_OK = """production_sha_expected=148bd362b37c82c92737382d181fbdeac4d2187b
production_sha_actual=148bd362b37c82c92737382d181fbdeac4d2187b
production_sha_match=yes
api_image_expected=ghcr.io/mrezamaghouli/jobpulse-api:148bd362b37c82c92737382d181fbdeac4d2187b
api_image_actual=ghcr.io/mrezamaghouli/jobpulse-api:148bd362b37c82c92737382d181fbdeac4d2187b
api_image_match=yes
tor_container_present=yes
tor_image_expected=ghcr.io/mrezamaghouli/jobpulse-tor:5dffbd669eec52f5283503bb6409a430509175a0
tor_image_actual=ghcr.io/mrezamaghouli/jobpulse-tor:5dffbd669eec52f5283503bb6409a430509175a0
tor_image_match=yes
tor_status=running
tor_health=healthy
tor_restart_count=0
tor_started_at=2026-08-27T08:58:13.353242516Z
tor_published_ports=NONE
api_tor_enabled_expected=false
api_tor_enabled_actual=false
api_tor_enabled_match=yes
api_health_rc=0
api_health_body={"status":"ok","database":"connected"}
frontend_http_status=200
frontend_curl_rc=0
"""


def _extract_classify_step_script(source: str) -> str:
    idx = source.index("- name: Classify evidence")
    tail = source[idx:]
    m = re.search(r"run: \|\n(.*)", tail, re.DOTALL)
    assert m
    block = m.group(1)
    lines = block.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    script = "\n".join(l[indent:] if l.startswith(" " * indent) else l for l in lines)
    # The real GitHub expression is interpolated by the Actions runner
    # before bash ever sees this file; substitute it for a shell
    # variable reference so the extracted script reads its evidence file
    # from an env var we control, exactly mirroring how
    # test_local_step_passes_bash_syntax_check-style extraction already
    # handles this workflow's other `${{ ... }}` usage.
    script = script.replace(
        '${{ steps.evidence.outputs.output_file }}', '"$OUTPUT_FILE_OVERRIDE"'
    )
    return script


def _run_classify_step(tmp_path, evidence_text=EVIDENCE_LINES_OK):
    """Execute the full classify step (evidence-parsing + classify_evidence
    + annotation + summary + final exit) against fabricated evidence, with
    GITHUB_STEP_SUMMARY pointed at a local temp file. Returns
    (stdout, exit_code, summary_text)."""
    script = _extract_classify_step_script(WORKFLOW_PATH.read_text())
    script_path = tmp_path / "classify_step.sh"
    script_path.write_text(script)

    evidence_path = tmp_path / "evidence.txt"
    evidence_path.write_text(evidence_text)

    summary_path = tmp_path / "summary.md"
    summary_path.write_text("")

    env = {
        "PATH": "/usr/bin:/bin",
        "OUTPUT_FILE_OVERRIDE": str(evidence_path),
        "GITHUB_STEP_SUMMARY": str(summary_path),
    }
    result = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True, text=True, env=env,
    )
    return result.stdout, result.returncode, summary_path.read_text()


# 12. WARNING still exits 0 / 11. WARNING emits ::warning::
def test_full_step_warning_emits_annotation_and_exits_zero(tmp_path):
    evidence = EVIDENCE_LINES_OK.replace("tor_restart_count=0", "tor_restart_count=1")
    stdout, rc, summary = _run_classify_step(tmp_path, evidence)
    assert "::warning::" in stdout
    assert "::error::" not in stdout
    assert "monitor_status=WARNING" in stdout
    assert rc == 0


# 13. CRITICAL emits ::error:: / 14. CRITICAL still exits 2
def test_full_step_critical_emits_annotation_and_exits_two(tmp_path):
    evidence = EVIDENCE_LINES_OK.replace("tor_status=running", "tor_status=exited")
    stdout, rc, summary = _run_classify_step(tmp_path, evidence)
    assert "::error::" in stdout
    assert "monitor_status=CRITICAL" in stdout
    assert rc == 2


# 15. OK emits no warning/error annotation
def test_full_step_ok_emits_no_annotation(tmp_path):
    stdout, rc, summary = _run_classify_step(tmp_path, EVIDENCE_LINES_OK)
    assert "::warning::" not in stdout
    assert "::error::" not in stdout
    assert "monitor_status=OK" in stdout
    assert rc == 0


# 17-21: summary content fields
def test_full_step_summary_contains_required_fields(tmp_path):
    stdout, rc, summary = _run_classify_step(tmp_path, EVIDENCE_LINES_OK)
    for expected in (
        "Verdict: OK",
        "warning_count | 0",
        "critical_count | 0",
        "tor_status | running",
        "tor_health | healthy",
        "api_health_ok | yes",
        "database_connected | yes",
        "api_tor_enabled_match | yes",
    ):
        assert expected in summary, expected


def test_full_step_critical_summary_still_generated(tmp_path):
    """Even a CRITICAL run (non-zero exit) must have produced its summary
    -- proves the ordering guard behaviorally, not just structurally."""
    evidence = EVIDENCE_LINES_OK.replace("tor_status=running", "tor_status=exited")
    stdout, rc, summary = _run_classify_step(tmp_path, evidence)
    assert rc == 2
    assert "Verdict: CRITICAL" in summary
    assert "critical_count | 1" in summary


# 22. summary contains no secret values
def test_full_step_summary_contains_no_secret_material(tmp_path):
    stdout, rc, summary = _run_classify_step(tmp_path, EVIDENCE_LINES_OK)
    for forbidden in ("VM_SSH_KEY", "BEGIN OPENSSH", "BEGIN RSA", "PRIVATE KEY",
                       "tor_control_password", "GHCR_TOKEN"):
        assert forbidden not in summary, forbidden


# 25. classification behavior tests from Phase 3.4D still all pass
# unchanged -- covered by the unmodified test_scenario_* tests above,
# which still execute the real classify_evidence() function extracted
# from this same (now-hardened) workflow file.


# =====================================================================
# Phase 3.4H (final hardening pass) -- every run must leave a GitHub
# Actions summary, including one that fails before classification is
# ever reached (missing secret, ssh-keyscan failure, SSH connection
# failure, unreachable /opt/jobpulse, MONITOR_EVIDENCE_INCOMPLETE).
# =====================================================================
def test_stable_step_ids_present():
    data = _load_workflow(WORKFLOW_PATH)
    steps = data["jobs"]["monitor"]["steps"]
    ids_by_name = {s["name"]: s.get("id") for s in steps}
    assert ids_by_name["Validate secrets"] == "validate"
    assert ids_by_name["Gather production runtime evidence (read-only)"] == "evidence"
    assert ids_by_name["Classify evidence (deterministic, local -- no SSH, no Docker, no Tor)"] == "classify"


def test_final_reporting_step_exists_and_always_runs():
    data = _load_workflow(WORKFLOW_PATH)
    steps = data["jobs"]["monitor"]["steps"]
    final = steps[-1]
    assert "Report execution status" in final["name"]
    assert final.get("if") == "always()"


def test_final_step_references_only_safe_step_outcomes():
    source = WORKFLOW_PATH.read_text()
    for expr in (
        "${{ steps.validate.outcome }}",
        "${{ steps.evidence.outcome }}",
        "${{ steps.classify.outcome }}",
    ):
        assert expr in source, expr


def test_final_step_uses_github_step_summary():
    final_step = _extract_last_step_block(
        WORKFLOW_PATH.read_text(), "Report execution status (always runs -- no SSH, no Docker, no Tor, no network)"
    )
    assert "$GITHUB_STEP_SUMMARY" in final_step


def test_no_continue_on_error_anywhere():
    """validate/evidence/classify must never be able to mask their own
    failure -- a real failure in any of them must still fail the overall
    job. The final always() step's own success must never paper over
    that. Checked via _code_only since the explanatory comments above
    legitimately discuss why continue-on-error is absent."""
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "continue-on-error" not in code


def test_final_step_contains_no_ssh_docker_curl_or_secret_reads():
    final_step = _code_only(_extract_last_step_block(
        WORKFLOW_PATH.read_text(), "Report execution status (always runs -- no SSH, no Docker, no Tor, no network)"
    ))
    for forbidden in ("ssh ", "ssh-keyscan", "docker ", "curl ", "secrets.", "VM_SSH_KEY"):
        assert forbidden not in final_step, forbidden


# =====================================================================
# Exact-count SSH regression guards (protects against an old unbounded
# ssh/ssh-keyscan call being retained alongside the hardened one).
# =====================================================================
def test_exactly_one_ssh_keyscan_invocation():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert code.count("ssh-keyscan") == 1


def test_exactly_one_remote_ssh_invocation():
    """Counts lines that invoke the `ssh` binary itself (not
    ssh-keyscan, not the key-material path jobpulse_monitor_key, not the
    private-key file name)."""
    code = _code_only(WORKFLOW_PATH.read_text())
    ssh_invocations = [
        line for line in code.splitlines()
        if re.match(r"\s*ssh\s", line) and "ssh-keyscan" not in line
    ]
    assert len(ssh_invocations) == 1, ssh_invocations
    assert "-o BatchMode=yes" in ssh_invocations[0]
    assert "-o ConnectTimeout=10" in ssh_invocations[0]
    assert "-o ConnectionAttempts=1" in ssh_invocations[0]


def test_single_ssh_keyscan_has_finite_timeout():
    source = WORKFLOW_PATH.read_text()
    keyscan_lines = [l for l in _code_only(source).splitlines() if "ssh-keyscan" in l]
    assert len(keyscan_lines) == 1
    assert "-T 10" in keyscan_lines[0]


# =====================================================================
# Behavioral: execute the final always() step directly with fabricated
# step outcomes. No SSH, no Docker, no Tor, no network -- it never makes
# any of those calls, verified statically above and structurally here.
# =====================================================================
def _extract_final_step_script(source: str) -> str:
    idx = source.index("- name: Report execution status")
    tail = source[idx:]
    m = re.search(r"run: \|\n(.*)", tail, re.DOTALL)
    assert m
    block = m.group(1)
    lines = block.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    script = "\n".join(l[indent:] if l.startswith(" " * indent) else l for l in lines)
    script = script.replace('${{ steps.validate.outcome }}', '"$VALIDATE_OUTCOME_OVERRIDE"')
    script = script.replace('${{ steps.evidence.outcome }}', '"$EVIDENCE_OUTCOME_OVERRIDE"')
    script = script.replace('${{ steps.classify.outcome }}', '"$CLASSIFY_OUTCOME_OVERRIDE"')
    return script


def _run_final_step(tmp_path, validate="success", evidence="success", classify="success"):
    script = _extract_final_step_script(WORKFLOW_PATH.read_text())
    script_path = tmp_path / "final_step.sh"
    script_path.write_text(script)

    summary_path = tmp_path / "summary.md"

    env = {
        "PATH": "/usr/bin:/bin",
        "VALIDATE_OUTCOME_OVERRIDE": validate,
        "EVIDENCE_OUTCOME_OVERRIDE": evidence,
        "CLASSIFY_OUTCOME_OVERRIDE": classify,
        "GITHUB_STEP_SUMMARY": str(summary_path),
    }
    result = subprocess.run(
        ["bash", str(script_path)],
        capture_output=True, text=True, env=env,
    )
    summary = summary_path.read_text() if summary_path.exists() else ""
    return result.stdout, result.returncode, summary


def test_final_step_pre_classification_failure_emits_error_and_fallback_summary(tmp_path):
    """Missing-secret scenario: validate failed, evidence and classify
    both skipped."""
    stdout, rc, summary = _run_final_step(tmp_path, validate="failure", evidence="skipped", classify="skipped")
    assert "::error::" in stdout
    assert "could not complete runtime evidence gathering" in stdout
    assert rc == 0  # this step's own exit never masks the already-failed prior steps
    assert "Execution status: INCOMPLETE / FAILED" in summary
    assert "| Validate secrets | failure |" in summary
    assert "| Gather production runtime evidence | skipped |" in summary
    assert "| Classify evidence | skipped |" in summary


def test_final_step_evidence_transport_failure_emits_error_and_fallback_summary(tmp_path):
    """SSH/evidence-gathering failure scenario: validate succeeded,
    evidence failed, classify skipped."""
    stdout, rc, summary = _run_final_step(tmp_path, validate="success", evidence="failure", classify="skipped")
    assert "::error::" in stdout
    assert "Execution status: INCOMPLETE / FAILED" in summary
    assert "| Gather production runtime evidence | failure |" in summary


def test_final_step_pre_classification_failure_not_labeled_critical(tmp_path):
    stdout, rc, summary = _run_final_step(tmp_path, validate="failure", evidence="skipped", classify="skipped")
    assert "monitor_status=CRITICAL" not in stdout
    assert "monitor_status=CRITICAL" not in summary
    assert "Verdict: CRITICAL" not in summary  # fallback path never claims the deterministic CRITICAL verdict


def test_final_step_noop_when_classification_was_reached_success(tmp_path):
    stdout, rc, summary = _run_final_step(tmp_path, validate="success", evidence="success", classify="success")
    assert "::error::" not in stdout
    assert summary == ""
    assert rc == 0


def test_final_step_noop_when_classification_was_reached_critical(tmp_path):
    """When classify itself failed (outcome=failure, i.e. the CRITICAL
    exit-2 path), the final step must NOT write a second summary or
    duplicate/reinterpret the annotation -- the Classify evidence step
    already did both."""
    stdout, rc, summary = _run_final_step(tmp_path, validate="success", evidence="success", classify="failure")
    assert "::error::" not in stdout
    assert summary == ""
    assert rc == 0


# Full end-to-end proof that the Classify evidence step's own CRITICAL
# handling is untouched by this phase: emits its own ::error::, writes
# its own detailed summary, and exits 2 -- exactly as before.
def test_classify_step_critical_path_still_self_contained(tmp_path):
    evidence = EVIDENCE_LINES_OK.replace("tor_status=running", "tor_status=exited")
    stdout, rc, summary = _run_classify_step(tmp_path, evidence)
    assert "::error::" in stdout
    assert rc == 2
    assert "Verdict: CRITICAL" in summary


# =====================================================================
# Phase 3.4I -- schedule activation readiness: adds `schedule:` alongside
# `workflow_dispatch` for unattended hourly execution. No runtime logic
# (evidence gathering, classify_evidence(), SSH hardening, timeout,
# final if: always() fallback, secrets, permissions, concurrency, image/
# SHA pins) changes in this phase -- only the trigger block and its
# surrounding comments.
# =====================================================================
def test_schedule_trigger_exists():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert "schedule" in on_block


def test_exactly_one_cron_entry():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert isinstance(on_block["schedule"], list)
    assert len(on_block["schedule"]) == 1


def test_cron_is_exactly_17_past_every_hour():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert on_block["schedule"][0] == {"cron": "17 * * * *"}


def test_no_minute_zero_schedule():
    """Guards against ever reverting to the documented GitHub Actions
    high-load top-of-the-hour slot."""
    source = WORKFLOW_PATH.read_text()
    assert '"0 * * * *"' not in source
    assert "'0 * * * *'" not in source


def test_no_duplicate_schedule_trigger():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert code.count("- cron:") == 1


def test_workflow_dispatch_and_schedule_both_present_no_other_triggers():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert set(on_block.keys()) == {"workflow_dispatch", "schedule"}


def test_timeout_minutes_unchanged_by_schedule_activation():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["jobs"]["monitor"]["timeout-minutes"] == 5


def test_stable_step_ids_unchanged_by_schedule_activation():
    data = _load_workflow(WORKFLOW_PATH)
    steps = data["jobs"]["monitor"]["steps"]
    ids_by_name = {s["name"]: s.get("id") for s in steps}
    assert ids_by_name["Validate secrets"] == "validate"
    assert ids_by_name["Gather production runtime evidence (read-only)"] == "evidence"
    assert ids_by_name["Classify evidence (deterministic, local -- no SSH, no Docker, no Tor)"] == "classify"


def test_final_always_step_unchanged_by_schedule_activation():
    data = _load_workflow(WORKFLOW_PATH)
    steps = data["jobs"]["monitor"]["steps"]
    final = steps[-1]
    assert "Report execution status" in final["name"]
    assert final.get("if") == "always()"


def test_concurrency_unchanged_and_still_serializes_runs():
    """The existing fixed-group, cancel-in-progress: false setting is
    already correct for hourly scheduling: it was deliberately left
    unchanged rather than weakened.

    Actual GitHub Actions semantics (not an unlimited FIFO queue): with a
    fixed, non-templated group name, at most one run in this group is
    ever executing, and at most one additional run is held pending.
    cancel-in-progress: false means a newly arriving run never cancels
    the currently *running* one -- it never kills a run mid-SSH-session.
    But if a run is already pending when yet another trigger arrives, the
    newer pending run replaces the older pending one rather than joining
    a queue behind it.

    That "replace the stale pending run" behavior is what we actually
    want here: this monitor reports on production's state *right now*,
    not a durable log, so if multiple triggers stack up (e.g. several
    manual dispatches during an incident), running the newest one instead
    of a now-stale queued one is more useful, not less. No queue: max is
    added -- a deeper backlog of pending runs would only mean acting on
    older, less relevant evidence."""
    data = _load_workflow(WORKFLOW_PATH)
    assert data["concurrency"] == {
        "group": "jobpulse-tor-runtime-monitor",
        "cancel-in-progress": False,
    }
    # A dynamic/templated group (e.g. including github.run_id) would
    # defeat serialization -- guard against that regression explicitly.
    assert "${{" not in data["concurrency"]["group"]


def test_no_additional_secrets_from_schedule_activation():
    secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", WORKFLOW_PATH.read_text()))
    assert secrets == {"VM_SSH_KEY"}


def test_no_new_permissions_from_schedule_activation():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["permissions"] == {"contents": "read"}


def test_expected_constants_unchanged_by_schedule_activation():
    source = WORKFLOW_PATH.read_text()
    assert f'EXPECTED_PRODUCTION_SHA="{EXPECTED_PRODUCTION_SHA}"' in source
    assert f'EXPECTED_API_IMAGE="{EXPECTED_API_IMAGE}"' in source
    assert f'EXPECTED_TOR_IMAGE="{EXPECTED_TOR_IMAGE}"' in source


def test_warning_critical_ok_exit_codes_unchanged_by_schedule_activation(tmp_path):
    """Re-proves the exit-code contract end-to-end (real bash execution,
    not just static text) after the trigger-block edit, guarding against
    any accidental collateral change to the classify step."""
    ok_stdout, ok_rc, _ = _run_classify_step(tmp_path, EVIDENCE_LINES_OK)
    assert "monitor_status=OK" in ok_stdout
    assert ok_rc == 0

    warning_evidence = EVIDENCE_LINES_OK.replace("tor_restart_count=0", "tor_restart_count=1")
    warning_stdout, warning_rc, _ = _run_classify_step(tmp_path, warning_evidence)
    assert "monitor_status=WARNING" in warning_stdout
    assert warning_rc == 0

    critical_evidence = EVIDENCE_LINES_OK.replace("tor_status=running", "tor_status=exited")
    critical_stdout, critical_rc, _ = _run_classify_step(tmp_path, critical_evidence)
    assert "monitor_status=CRITICAL" in critical_stdout
    assert critical_rc == 2


def test_still_no_continue_on_error_after_schedule_activation():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "continue-on-error" not in code


def test_still_no_tor_operation_after_schedule_activation():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("ControlPort", "SOCKS", "9050", "9051", "AUTHENTICATE",
                       "SETEVENTS", "NEWNYM", "request_new_identity", "docker logs"):
        assert forbidden not in code, forbidden


def test_still_no_linkedin_or_external_url_after_schedule_activation():
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "linkedin" not in code.lower()
    urls = re.findall(r"https?://[^\s'\"]+", code)
    for url in urls:
        assert url.startswith("http://127.0.0.1"), url


def test_still_no_mutation_or_remediation_commands_after_schedule_activation():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in FORBIDDEN_MUTATION_COMMANDS:
        assert forbidden not in code, forbidden


def test_scheduled_runs_share_the_same_single_job_as_manual_runs():
    """Guards against a second, schedule-specific job or code path: there
    must be exactly one job (`monitor`), and nothing in the workflow
    branches on `github.event_name`/`github.event.schedule` to give a
    scheduled run different evidence-gathering or classification logic
    than a workflow_dispatch run."""
    data = _load_workflow(WORKFLOW_PATH)
    assert list(data["jobs"].keys()) == ["monitor"]
    source = WORKFLOW_PATH.read_text()
    assert "github.event_name" not in source
    assert "github.event.schedule" not in source


def test_build_and_tor_image_paths_still_do_not_match_after_schedule_activation():
    """Re-audits both build workflows against the current, unchanged
    Phase 3.4D file scope -- adding `schedule:` to tor-runtime-monitor.yml
    itself doesn't add a new file to that scope, so this is a stability
    guard, not a new path-filter claim."""
    for build_path in (DOCKER_BUILD_PATH, TOR_IMAGE_BUILD_PATH):
        patterns = _push_paths(build_path)
        for changed_file in PHASE_3_4D_CHANGED_FILES:
            for pattern in patterns:
                assert not _path_matches_filter(changed_file, pattern), (changed_file, pattern)
