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

EXPECTED_PRODUCTION_SHA = "0b0290d5dedc9bfc9fba83a1a97f782a10890b06"
EXPECTED_API_IMAGE = "ghcr.io/mrezamaghouli/jobpulse-api:0b0290d5dedc9bfc9fba83a1a97f782a10890b06"
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


def test_workflow_is_workflow_dispatch_only():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert on_block == {"workflow_dispatch": None}


def test_workflow_source_has_no_other_trigger_keys():
    """Checks executable YAML only -- the header comment intentionally
    discusses `schedule:` in prose (explaining that Phase 3.4D does not
    add one yet), which must not trip this guard."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("push:", "pull_request:", "workflow_run:", "schedule:"):
        assert forbidden not in code, forbidden


def test_no_schedule_activated_in_phase_3_4d():
    """Explicit, separate guard beyond the generic trigger check: `cron:`
    (schedule's only key) must not appear in executable YAML/code yet.
    Checked via _code_only for the same reason as
    test_workflow_source_has_no_other_trigger_keys -- prose discussing a
    future `schedule:`/cron trigger is allowed in comments."""
    code = _code_only(WORKFLOW_PATH.read_text())
    assert "cron:" not in code


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
    # Must never be a workflow_dispatch input (typo-safety discipline).
    data = _load_workflow(WORKFLOW_PATH)
    assert _on_block(data) == {"workflow_dispatch": None}


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
    pattern = rf"- name: {re.escape(step_name)}.*?\n(\s+run: \|\n)(.*?)\n\n\s*{re.escape(next_marker)}"
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
    script = _extract_last_step_block(
        source, "Classify evidence (deterministic, local -- no SSH, no Docker, no Tor)"
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
