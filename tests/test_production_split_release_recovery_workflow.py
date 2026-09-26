"""Structural + behavioral regression guard for
.github/workflows/production-split-release-recovery.yml (Phase 4H).

Context: Production Direct Runtime Upgrade run 35247979950 (Phase 4E)
advanced production git to bccbbd997ee8... then failed live-frontend
marker validation and attempted an API rollback. The read-only
Production Runtime Diagnostic (run 35442611756) proved the rollback
actually succeeded (API healthy on the old f476 image bytes) but
`rollback_git` never ran -- production is split: git at bccbbd, API
runtime at f476 (CASE_B_GIT_TARGET_API_OLD).

This workflow is a ONE-TIME, incident-specific recovery runner. It is
NOT production-direct-runtime-upgrade.yml and NOT deploy.yml. Its ONLY
authorized production mutation is `git reset --hard` from the exact
pinned incident commit to the exact pinned known-good commit -- gated by
an exhaustive set of preconditions pinned to the diagnostic-proven
incident state, and it never recreates/restarts any container.

Like tests/test_production_direct_runtime_upgrade_workflow.py and
tests/test_production_runtime_diagnostic.py, these tests never SSH, use
no real Docker, and make no network calls. Static checks parse the
committed YAML/shell as text; behavioral checks run the ACTUAL embedded
remote shell script (extracted verbatim) via `bash -c` against fake
git/docker/curl/sha256sum/flock executables.
"""
import os
import re
import shutil
import subprocess as sp
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-split-release-recovery.yml"
UPGRADE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-direct-runtime-upgrade.yml"
DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CHECKPOINT_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-checkpoint.yml"

REMOTE_HEREDOC_START = "<<'REMOTE'"
REMOTE_HEREDOC_END = "REMOTE"

CONFIRMATION_TOKEN = "RECOVER_PHASE4E_SPLIT_RELEASE"
INCIDENT_GIT_SHA = "bccbbd997ee83ee9219d6051a808dae17f5cc933"
RECOVERY_TARGET_SHA = "f4765f857355c6543f68cea0e481b7f20a917147"
EXPECTED_API_IMAGE_ID = "sha256:7739628f61c3bba88ff4e395c0f0a0ce3bea3e3abb40956207b495f9d909b753"
EXPECTED_CURRENT_API_CONFIG_IMAGE = "jobpulse-api-rollback:bccbbd997ee8-1427495"
EXPECTED_API_CONTAINER_ID = "ac66a4acef51c8ac3dc27b38a5559c5aa056ec033ceef43c9f8a0e4a8cbd3682"
EXPECTED_DB_CONTAINER_ID = "98f20abb30f4fdfe9473431370c3f6db29c39183d67f31d49638a493a1f605e9"
EXPECTED_FRONTEND_CONTAINER_ID = "fb949b779e80a53f4a9542d2eaa30945d1abbf357fbb2391b2422238371b7dd8"
EXPECTED_TOR_CONTAINER_ID = "e2210c3c47f7a73fca1d5b2faeb62b2eeec89526a56f5da4cb1a00a3298b1612"


# =====================================================================
# Fixtures
# =====================================================================
@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text()


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    return yaml.safe_load(workflow_text)


def _triggers(workflow: dict):
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


@pytest.fixture(scope="module")
def job(workflow) -> dict:
    return workflow["jobs"]["recover"]


@pytest.fixture(scope="module")
def steps(job) -> list:
    return job["steps"]


def _step_by_name(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


@pytest.fixture(scope="module")
def recovery_step(steps):
    return _step_by_name(steps, "Run production split-release recovery")


@pytest.fixture(scope="module")
def full_run_text(recovery_step) -> str:
    return recovery_step["run"]


def _split_remote_and_runner(full_run_text: str):
    start = full_run_text.index(REMOTE_HEREDOC_START) + len(REMOTE_HEREDOC_START)
    before = full_run_text[: full_run_text.index(REMOTE_HEREDOC_START)]
    rest = full_run_text[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == REMOTE_HEREDOC_END)
    remote = "\n".join(lines[:end_idx])
    after = "\n".join(lines[end_idx + 1 :])
    return before + after, remote


@pytest.fixture(scope="module")
def runner_script(full_run_text) -> str:
    runner, _ = _split_remote_and_runner(full_run_text)
    return runner


@pytest.fixture(scope="module")
def remote_script(full_run_text) -> str:
    _, remote = _split_remote_and_runner(full_run_text)
    return remote


def _executable_lines(script: str):
    for line in script.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


def _run_bash_n(script: str):
    return sp.run(["bash", "-n"], input=script, capture_output=True, text=True, timeout=10)


def _code_only(text: str) -> str:
    """Strip full-line `#` comments so substring checks don't false-positive
    on prose mentioning another workflow/command for context."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


# =====================================================================
# A/B. Trigger, confirmation token, permissions, concurrency
# =====================================================================
def test_workflow_file_exists_and_parses(workflow):
    assert workflow["name"] == "Production Split-Release Recovery"


def test_trigger_is_workflow_dispatch_only(workflow):
    on_block = _triggers(workflow)
    assert set(on_block.keys()) == {"workflow_dispatch"}


def test_no_other_trigger_keys_in_source(workflow_text):
    for forbidden in ("push:", "pull_request:", "schedule:", "workflow_run:", "repository_dispatch:"):
        assert forbidden not in workflow_text, forbidden


def test_confirmation_input_required_string(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert inputs["confirmation"]["required"] is True
    assert inputs["confirmation"]["type"] == "string"


def test_confirmation_token_is_exact(workflow_text):
    assert f'"$CONFIRMATION" != "{CONFIRMATION_TOKEN}"' in workflow_text


def test_permissions_contents_read_only(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_concurrency_group_and_no_cancel(workflow):
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "jobpulse-production-split-release-recovery"
    assert concurrency["cancel-in-progress"] is False


def test_require_dispatch_from_main(steps):
    step = _step_by_name(steps, "Require dispatch from main")
    assert "refs/heads/main" in step["run"]


def test_confirmation_check_precedes_ssh_key_material(steps):
    names = [s.get("name") for s in steps]
    assert names.index("Require exact confirmation token") < names.index(
        "Validate VM_SSH_KEY secret is present"
    )


# =====================================================================
# C. Exact recovery pins (section 7)
# =====================================================================
def test_incident_and_recovery_sha_pinned(workflow):
    env = workflow["env"]
    assert env["INCIDENT_GIT_SHA"] == INCIDENT_GIT_SHA
    assert env["RECOVERY_TARGET_SHA"] == RECOVERY_TARGET_SHA


def test_expected_api_pins(workflow):
    env = workflow["env"]
    assert env["EXPECTED_API_IMAGE_ID"] == EXPECTED_API_IMAGE_ID
    assert env["EXPECTED_CURRENT_API_CONFIG_IMAGE"] == EXPECTED_CURRENT_API_CONFIG_IMAGE
    assert env["EXPECTED_API_CONTAINER_ID"] == EXPECTED_API_CONTAINER_ID


def test_expected_service_container_id_pins(workflow):
    env = workflow["env"]
    assert env["EXPECTED_DB_CONTAINER_ID"] == EXPECTED_DB_CONTAINER_ID
    assert env["EXPECTED_FRONTEND_CONTAINER_ID"] == EXPECTED_FRONTEND_CONTAINER_ID
    assert env["EXPECTED_TOR_CONTAINER_ID"] == EXPECTED_TOR_CONTAINER_ID


def test_pins_are_40_or_64_or_sha256_prefixed_hex(workflow):
    env = workflow["env"]
    assert re.fullmatch(r"[0-9a-f]{40}", env["INCIDENT_GIT_SHA"])
    assert re.fullmatch(r"[0-9a-f]{40}", env["RECOVERY_TARGET_SHA"])
    for container_id in (
        env["EXPECTED_API_CONTAINER_ID"],
        env["EXPECTED_DB_CONTAINER_ID"],
        env["EXPECTED_FRONTEND_CONTAINER_ID"],
        env["EXPECTED_TOR_CONTAINER_ID"],
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", container_id)
    assert env["EXPECTED_API_IMAGE_ID"].startswith("sha256:")


# =====================================================================
# F/G. Hash derivation step (section 8) -- immutable git objects, compose parity gate
# =====================================================================
@pytest.fixture(scope="module")
def hash_step(steps):
    return _step_by_name(steps, "Derive immutable file hashes for incident and recovery commits")


def test_hash_step_uses_full_history_checkout(steps):
    checkout_step = _step_by_name(steps, "Checkout repository with full history")
    assert checkout_step["uses"].startswith("actions/checkout@")
    assert checkout_step["with"]["fetch-depth"] == 0


def test_hash_step_derives_from_git_show_not_working_tree(hash_step):
    run = hash_step["run"]
    for sha_var in ("INCIDENT_GIT_SHA", "RECOVERY_TARGET_SHA"):
        assert f'git show "${{{sha_var}}}:frontend/index.html"' in run
        assert f'git show "${{{sha_var}}}:scripts/providers/linkedin_browser_provider.py"' in run
        assert f'git show "${{{sha_var}}}:docker-compose.prod.yml"' in run


def test_hash_step_verifies_target_commit_objects_exist(hash_step):
    run = hash_step["run"]
    assert 'git cat-file -e "${INCIDENT_GIT_SHA}^{commit}"' in run
    assert 'git cat-file -e "${RECOVERY_TARGET_SHA}^{commit}"' in run


def test_compose_parity_required_before_ssh(hash_step, steps):
    run = hash_step["run"]
    assert '"$INCIDENT_COMPOSE_SHA256" != "$RECOVERY_COMPOSE_SHA256"' in run
    assert "exit 1" in run.split('"$INCIDENT_COMPOSE_SHA256" != "$RECOVERY_COMPOSE_SHA256"')[1].split("fi")[0]
    names = [s.get("name") for s in steps]
    assert names.index("Derive immutable file hashes for incident and recovery commits") < names.index(
        "Run production split-release recovery"
    )


def test_six_hashes_exported_to_github_env(hash_step):
    run = hash_step["run"]
    assert "GITHUB_ENV" in run
    for var in (
        "INCIDENT_FRONTEND_SHA256",
        "INCIDENT_PROVIDER_SHA256",
        "INCIDENT_COMPOSE_SHA256",
        "RECOVERY_FRONTEND_SHA256",
        "RECOVERY_PROVIDER_SHA256",
        "RECOVERY_COMPOSE_SHA256",
    ):
        assert var in run


# =====================================================================
# Do not reuse/repurpose other workflows (section 3, 9, 35, 39)
# =====================================================================
def test_this_is_a_new_dedicated_workflow_file():
    assert WORKFLOW_PATH.is_file()


def test_upgrade_workflow_not_modified_by_this_change():
    assert UPGRADE_WORKFLOW_PATH.is_file()
    text = UPGRADE_WORKFLOW_PATH.read_text()
    assert "Production Split-Release Recovery" not in text
    assert CONFIRMATION_TOKEN not in text


def test_deploy_workflow_not_referenced():
    assert DEPLOY_WORKFLOW_PATH.is_file()
    code_only = _code_only(WORKFLOW_PATH.read_text())
    assert "deploy.yml" not in code_only
    assert "deploy_prod_from_ghcr.sh" not in code_only
    assert "gh workflow run" not in code_only


def test_production_rollback_checkpoint_never_dispatched(workflow_text):
    code_only = _code_only(workflow_text)
    assert "Production Rollback Checkpoint" not in code_only
    assert "production-checkpoint.yml" not in code_only
    assert "pg_dump" not in code_only
    assert "pg_restore" not in code_only


# =====================================================================
# Mutation-command scope guard: only `git reset` on production, no
# container mutation anywhere in the whole workflow file
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
    "docker tag",
    "git checkout",
    "git switch",
    "git pull",
    "git clean",
)


def test_no_container_mutation_commands_anywhere(workflow_text):
    code_only = _code_only(workflow_text)
    for forbidden in FORBIDDEN_MUTATION_COMMANDS:
        if forbidden == "git pull":
            # Appears only as prose inside a fail() message ("refusing to
            # git pull, failing before mutation") -- assert it is never
            # invoked as an actual command (start of line, or after a
            # shell separator).
            assert re.search(r"(^|[;&|]\s*)git pull\b", code_only, re.MULTILINE) is None, forbidden
        else:
            assert forbidden not in code_only, forbidden


def test_exactly_one_git_reset_hard_and_it_targets_recovery_sha(remote_script):
    occurrences = [
        line for line in _executable_lines(remote_script) if line.startswith("git reset")
    ]
    assert len(occurrences) == 1, occurrences
    assert occurrences[0] == 'git reset --hard "$RECOVERY_TARGET_SHA"'


def test_no_reset_to_incident_sha_anywhere(remote_script):
    """Section 22: this recovery is monotonic. There must be no code
    path that resets git back to INCIDENT_GIT_SHA."""
    assert 'git reset --hard "$INCIDENT_GIT_SHA"' not in remote_script
    assert f"git reset --hard {INCIDENT_GIT_SHA}" not in remote_script
    assert "rollback_git" not in remote_script


def test_git_recovery_started_set_immediately_before_mutation(remote_script):
    idx_flag = remote_script.index("GIT_RECOVERY_STARTED=true")
    idx_reset = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    between = remote_script[idx_flag + len("GIT_RECOVERY_STARTED=true") : idx_reset]
    assert between.strip() == "" or between.strip() == ""  # nothing but whitespace/newline
    assert idx_flag < idx_reset


def test_no_linkedin_request_or_collector_execution(remote_script):
    for forbidden in (
        "python -m scripts.linkedin_plan_collect",
        "python -m scripts.process_search_demand_queue",
        "python -m scripts.collector_postgres",
        "python -m scripts.seed_priority_coverage_queue",
        "python -m scripts.reconcile_priority_coverage",
        "linkedin.com",
    ):
        assert forbidden not in remote_script


def test_no_tor_controlport_or_newnym(remote_script):
    assert "NEWNYM" not in remote_script
    assert "ControlPort" not in remote_script
    assert ":9051" not in remote_script
    assert "TOR_CONTROL_PASSWORD" not in remote_script
    assert "/run/secrets/tor_control_password" not in remote_script


def test_only_vm_ssh_key_secret_used(workflow_text):
    referenced_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", workflow_text))
    assert referenced_secrets == {"VM_SSH_KEY"}


def test_ssh_key_cleaned_up_in_runner(steps):
    cleanup = _step_by_name(steps, "Clean up local SSH key material")
    assert cleanup.get("if") == "always()"
    assert "jobpulse_recovery_key" in cleanup["run"]


def test_no_scp_no_file_transfer(runner_script):
    assert "scp " not in runner_script
    assert "scp\n" not in runner_script


def test_exactly_one_ssh_invocation(runner_script):
    ssh_invocations = [
        line for line in _executable_lines(runner_script) if line.startswith("ssh ") or line.startswith("ssh -")
    ]
    assert len(ssh_invocations) == 1, ssh_invocations


# =====================================================================
# Lock ordering (outer before internal) and active-collector fail-closed
# =====================================================================
def test_outer_lock_before_internal_lock(remote_script):
    outer_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    internal_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    assert outer_idx < internal_idx


def test_pinned_canonical_lock_paths_no_fuzzy_parser(remote_script):
    assert 'OUTER_LOCK_PATH="/tmp/jobpulse_collection_cycle.lock"' in remote_script
    assert 'INTERNAL_LOCK_PATH="/opt/jobpulse/state/run_collection_cycle.lock"' in remote_script
    # This runner DOES now inspect crontab/systemd (a narrow, evidence-
    # pinned provenance check -- see the scheduler-provenance tests
    # below), but it must never carry the full generic Phase 3.4O
    # cron/systemd grammar/parser (scan_launchers, classify_cron_job_command,
    # reconcile_launchers, etc.) from production-direct-runtime-upgrade.yml.
    assert "scan_launchers" not in remote_script
    assert "classify_cron_job_command" not in remote_script
    assert "classify_wrapper_command" not in remote_script
    assert "reconcile_launchers" not in remote_script
    assert "DISCOVERED_LAUNCHERS" not in remote_script


def test_locks_acquired_non_blocking(remote_script):
    assert 'flock -n "$OUTER_LOCK_FD"' in remote_script
    assert 'flock -n "$INTERNAL_LOCK_FD"' in remote_script


def test_locks_released_in_exit_trap_internal_then_outer(remote_script):
    func_body = remote_script.split("release_locks() {")[1].split("\n          }")[0]
    internal_idx = func_body.index("INTERNAL_LOCK_FD")
    outer_idx = func_body.index("OUTER_LOCK_FD")
    assert internal_idx < outer_idx
    assert "trap release_locks EXIT" in remote_script


def test_active_collector_check_before_mutation(remote_script):
    collector_idx = remote_script.index("docker top jobpulse-api-prod")
    reset_idx = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    assert collector_idx < reset_idx


def test_active_collector_patterns_match_known_scripts(remote_script):
    for pattern in (
        "linkedin_auth_preflight",
        "seed_priority_coverage_queue",
        "process_search_demand_queue",
        "linkedin_plan_collect",
        "collector_postgres",
        "reconcile_priority_coverage",
        "run_collection_cycle",
    ):
        assert pattern in remote_script


def test_no_kill_of_collector_processes(remote_script):
    assert "kill " not in remote_script
    assert "pkill" not in remote_script
    assert "docker kill" not in remote_script


def test_active_collector_check_never_kills(remote_script):
    start = remote_script.index("active collector check")
    end = remote_script.index("ONLY AUTHORIZED PRODUCTION MUTATION")
    block = remote_script[start:end]
    for forbidden in ("kill ", "pkill", "docker kill"):
        assert forbidden not in block, forbidden


# =====================================================================
# Scheduler provenance (Phase 4H correction).
#
# Historical, independently-verified LIVE production Actions log
# evidence (never inferred, never guessed):
#
#   - Production Direct Runtime Upgrade run 34479326097 failed at its
#     own scheduler-provenance stage with the exact real output:
#       "ERROR: this account's crontab invokes (or references)
#       run_collection_cycle_safe.sh in an unsupported/unrecognized
#       form -- refusing to guess, failing closed (*/30 * * * * cd
#       /opt/jobpulse && flock -n /tmp/jobpulse_collection_cycle.lock
#       ./scripts/run_collection_cycle_safe.sh)"
#     -- proving the canonical launcher lives in THIS SSH-authenticated
#     account's OWN crontab, not /etc/crontab, not root's crontab, not
#     a systemd unit.
#   - The later successful run 34679402811 proved exactly one launcher
#     exists anywhere:
#       "All 1 discovered launcher(s) agree: ROOT=/opt/jobpulse,
#       effective internal lock=/opt/jobpulse/state/run_collection_cycle.lock,
#       outer scheduler lock=/tmp/jobpulse_collection_cycle.lock"
# =====================================================================

CANONICAL_LAUNCHER_LINE = (
    "*/30 * * * * cd /opt/jobpulse && flock -n /tmp/jobpulse_collection_cycle.lock "
    "./scripts/run_collection_cycle_safe.sh"
)


def test_canonical_launcher_line_matches_historical_evidence(remote_script):
    assert f"CANONICAL_LAUNCHER_LINE='{CANONICAL_LAUNCHER_LINE}'" in remote_script


def test_own_crontab_read_is_bounded_by_timeout(remote_script):
    assert "timeout 5 crontab -l" in remote_script
    assert 'command -v timeout > /dev/null 2>&1 || fail' in remote_script
    assert 'command -v crontab > /dev/null 2>&1 || fail' in remote_script


def test_own_crontab_never_edited_or_installed(remote_script):
    assert "crontab -e" not in remote_script
    assert "crontab -r" not in remote_script
    assert re.search(r"crontab\s+-l\s*<", remote_script) is None  # never used to INSTALL (crontab < file)


def test_own_crontab_uses_exact_line_fixed_string_match(remote_script):
    # -F (fixed string) + -x (whole line) -- never a permissive regex.
    assert 'grep -Fxc "$CANONICAL_LAUNCHER_LINE"' in remote_script


def test_own_crontab_requires_exactly_one_canonical_match(remote_script):
    assert '[ "$CANONICAL_MATCH_COUNT" = "1" ]' in remote_script


def test_own_crontab_requires_exactly_one_wrapper_mention(remote_script):
    assert '[ "$WRAPPER_MENTION_COUNT" = "1" ]' in remote_script


def test_own_crontab_fails_closed_on_relevant_env_override(remote_script):
    assert '[ "$OWN_ROOT_VAR_COUNT" = "0" ]' in remote_script
    assert '[ "$OWN_LOCK_VAR_COUNT" = "0" ]' in remote_script


def test_no_crontab_state_is_a_failure_not_acceptable_empty(remote_script):
    section = remote_script[
        remote_script.index("scheduler provenance: this account's own crontab") : remote_script.index(
            "scheduler drift check: /etc/crontab"
        )
    ]
    assert 'if [ "$OWN_CRONTAB_STATUS" -ne 0 ]; then' in section
    assert "fail " in section


def test_etc_crontab_inspected_read_only_and_bounded(remote_script):
    section = remote_script[
        remote_script.index("scheduler drift check: /etc/crontab") : remote_script.index(
            "scheduler drift check: /etc/cron.d"
        )
    ]
    assert "timeout 5 cat /etc/crontab" in section
    assert "run_collection_cycle_safe.sh" in section
    assert ">" not in section.replace("2>&1", "").replace("2>/dev/null", "")  # never redirected TO the file (no write)


def test_etc_cron_d_inspected_read_only_and_bounded(remote_script):
    section = remote_script[
        remote_script.index("scheduler drift check: /etc/cron.d") : remote_script.index(
            "scheduler drift check: root crontab"
        )
    ]
    assert "timeout 5 find /etc/cron.d -maxdepth 1 -type f" in section
    assert "timeout 5 cat \"$cron_d_file\"" in section


def test_root_crontab_uses_bounded_non_interactive_sudo(remote_script):
    section = remote_script[
        remote_script.index("scheduler drift check: root crontab") : remote_script.index(
            "scheduler drift check: systemd"
        )
    ]
    assert "timeout 5 sudo -n crontab -u root -l" in section
    assert "no crontab for" in section


def test_systemd_drift_check_is_read_only(remote_script):
    section = remote_script[
        remote_script.index("scheduler drift check: systemd") : remote_script.index(
            "effective internal lock path provenance"
        )
    ]
    assert "systemctl list-unit-files" in section
    assert "systemctl show" in section
    for forbidden in ("systemctl start", "systemctl stop", "systemctl restart", "systemctl enable", "systemctl disable", "systemctl daemon-reload"):
        assert forbidden not in section


# =====================================================================
# Systemd fail-closed correction (second independent review pass).
# The systemd alternate-source drift check must never silently
# continue when systemd state cannot be established: `systemctl`
# absence, `systemctl list-unit-files` failure, and `systemctl show`
# failure must all fail closed, with no `|| true` able to mask any of
# them, and every enumerated unit must get a bounded effective-state
# inspection before mutation is possible.
# =====================================================================
def _systemd_section(remote_script: str) -> str:
    return remote_script[
        remote_script.index("scheduler drift check: systemd") : remote_script.index(
            "effective internal lock path provenance"
        )
    ]


def test_systemctl_presence_is_mandatory(remote_script):
    section = _systemd_section(remote_script)
    assert 'command -v systemctl > /dev/null 2>&1 || fail' in section


def test_systemctl_missing_fails_before_any_lock_or_mutation(remote_script):
    systemd_idx = remote_script.index("scheduler drift check: systemd")
    command_v_idx = remote_script.index('command -v systemctl > /dev/null 2>&1 || fail', systemd_idx)
    outer_lock_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    reset_idx = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    assert systemd_idx < command_v_idx < outer_lock_idx < reset_idx


def test_list_unit_files_is_bounded(remote_script):
    section = _systemd_section(remote_script)
    assert "timeout 5 systemctl list-unit-files --type=service --no-legend --no-pager" in section


def test_list_unit_files_failure_is_not_swallowed(remote_script):
    section = _systemd_section(remote_script)
    assert "UNIT_LIST_STATUS=$?" in section
    assert '[ "$UNIT_LIST_STATUS" -eq 0 ] || fail' in section


def test_no_double_pipe_true_can_mask_list_unit_files_failure(remote_script):
    section = _systemd_section(remote_script)
    list_unit_files_line = next(
        line for line in section.splitlines() if "systemctl list-unit-files" in line
    )
    assert "|| true" not in list_unit_files_line


def test_every_unit_gets_bounded_effective_state_inspection(remote_script):
    section = _systemd_section(remote_script)
    assert (
        'timeout 5 systemctl show "$unit" '
        "--property=ExecStart,Environment,EnvironmentFiles,FragmentPath,DropInPaths --no-pager"
        in section
    )


def test_systemctl_show_failure_is_not_swallowed(remote_script):
    section = _systemd_section(remote_script)
    assert "UNIT_SHOW_STATUS=$?" in section
    assert '[ "$UNIT_SHOW_STATUS" -eq 0 ] || fail' in section


def test_no_double_pipe_true_anywhere_in_systemd_section(remote_script):
    section = _systemd_section(remote_script)
    assert "|| true" not in section


def test_systemd_show_effective_execstart_checked_for_wrapper(remote_script):
    section = _systemd_section(remote_script)
    assert "grep -Fq 'run_collection_cycle_safe.sh' <<< \"$UNIT_SHOW_OUTPUT\"" in section


def test_systemd_show_effective_environment_checked_for_root_var(remote_script):
    section = _systemd_section(remote_script)
    assert "grep -Fq 'JOBPULSE_COLLECTION_ROOT' <<< \"$UNIT_SHOW_OUTPUT\"" in section


def test_systemd_show_effective_environment_checked_for_lock_var(remote_script):
    section = _systemd_section(remote_script)
    assert "grep -Fq 'JOBPULSE_COLLECTION_CYCLE_LOCK_PATH' <<< \"$UNIT_SHOW_OUTPUT\"" in section


def test_systemd_checks_finish_before_outer_flock(remote_script):
    systemd_idx = remote_script.index("scheduler drift check: systemd")
    systemd_end_idx = remote_script.index("effective internal lock path provenance")
    outer_lock_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    assert systemd_idx < systemd_end_idx <= outer_lock_idx


def test_outer_flock_remains_before_internal_flock_with_systemd_upstream(remote_script):
    outer_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    internal_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    systemd_idx = remote_script.index("scheduler drift check: systemd")
    assert systemd_idx < outer_idx < internal_idx


def test_all_systemd_checks_precede_docker_top_and_git_reset(remote_script):
    systemd_end_idx = remote_script.index("effective internal lock path provenance")
    docker_top_idx = remote_script.index("docker top jobpulse-api-prod")
    reset_idx = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    assert systemd_end_idx < docker_top_idx < reset_idx


def test_no_systemd_mutation_commands_in_systemd_section(remote_script):
    section = _systemd_section(remote_script)
    for forbidden in (
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "systemctl enable",
        "systemctl disable",
        "systemctl daemon-reload",
        "systemctl mask",
        "systemctl unmask",
        "systemctl kill",
    ):
        assert forbidden not in section, forbidden


def test_systemd_show_failure_does_not_print_raw_output_in_fail_message(remote_script):
    section = _systemd_section(remote_script)
    fail_line = next(
        line for line in section.splitlines() if "systemctl show failed" in line
    )
    assert "$UNIT_SHOW_OUTPUT" not in fail_line


def test_own_crontab_generic_failure_does_not_echo_raw_content(remote_script):
    section = remote_script[
        remote_script.index("scheduler provenance: this account's own crontab") : remote_script.index(
            "scheduler drift check: /etc/crontab"
        )
    ]
    fail_line = next(
        line for line in section.splitlines() if "cannot read this account's own crontab" in line
    )
    assert "$OWN_CRONTAB_OUTPUT" not in fail_line


def test_root_crontab_generic_failure_does_not_echo_raw_content(remote_script):
    section = remote_script[
        remote_script.index("scheduler drift check: root crontab") : remote_script.index(
            "scheduler drift check: systemd"
        )
    ]
    fail_line = next(
        line for line in section.splitlines() if "cannot establish root's crontab configuration" in line
    )
    assert "$ROOT_CRONTAB_OUTPUT" not in fail_line


def test_alternate_sources_never_mutate_scheduler_state(remote_script):
    section = remote_script[
        remote_script.index("scheduler drift check: /etc/crontab") : remote_script.index(
            "effective internal lock path provenance"
        )
    ]
    for forbidden in (
        "crontab -e",
        "crontab -r",
        "crontab -u root -e",
        "crontab -u root -r",
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "systemctl enable",
        "systemctl disable",
        "systemctl daemon-reload",
    ):
        assert forbidden not in section, forbidden
    assert re.search(r"(^|[;&|]\s*)service\s+\S+\s+(start|stop|restart|reload)", section, re.MULTILINE) is None


def test_collection_env_parsed_as_data_never_sourced(remote_script):
    section = remote_script[
        remote_script.index("effective internal lock path provenance") : remote_script.index(
            "collection scheduler safety"
        )
    ]
    assert re.search(r"(^|[;&|]\s*)source\s", section, re.MULTILINE) is None
    assert re.search(r"(^|[;&|]\s*)\.\s+/opt/jobpulse/\.collection\.env", section, re.MULTILINE) is None
    assert re.search(r"(^|[;&|(]\s*)eval\b", section, re.MULTILINE) is None
    assert "bash -c" not in section
    assert 'timeout 5 cat "$COLLECTION_ENV_PATH"' in section


def test_internal_lock_override_absent_or_exact_expected(remote_script):
    section = remote_script[
        remote_script.index("effective internal lock path provenance") : remote_script.index(
            "collection scheduler safety"
        )
    ]
    assert '[ "$LOCK_VALUE" != "$INTERNAL_LOCK_PATH" ]' in section
    assert 'LOCK_ASSIGNMENTS_FOUND" -gt 1' in section


def test_scheduler_provenance_precedes_outer_lock(remote_script):
    provenance_idx = remote_script.index("scheduler provenance: this account's own crontab")
    outer_lock_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    assert provenance_idx < outer_lock_idx


def test_all_scheduler_checks_precede_internal_lock(remote_script):
    collection_env_idx = remote_script.index("effective internal lock path provenance")
    internal_lock_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    assert collection_env_idx < internal_lock_idx


def test_all_scheduler_and_lock_checks_precede_docker_top(remote_script):
    provenance_idx = remote_script.index("scheduler provenance: this account's own crontab")
    internal_lock_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    docker_top_idx = remote_script.index("docker top jobpulse-api-prod")
    assert provenance_idx < internal_lock_idx < docker_top_idx


def test_all_scheduler_and_lock_checks_precede_git_reset(remote_script, reset_index):
    provenance_idx = remote_script.index("scheduler provenance: this account's own crontab")
    assert provenance_idx < reset_index


def test_scheduler_checks_never_execute_the_wrapper(remote_script):
    section = remote_script[
        remote_script.index("scheduler provenance: this account's own crontab") : remote_script.index(
            "collection scheduler safety"
        )
    ]
    assert "./scripts/run_collection_cycle_safe.sh" not in section.replace(
        f"CANONICAL_LAUNCHER_LINE='{CANONICAL_LAUNCHER_LINE}'", ""
    )
    assert "run_collection_cycle_safe.sh\"" not in section.replace(
        f"CANONICAL_LAUNCHER_LINE='{CANONICAL_LAUNCHER_LINE}'", ""
    ).replace("'run_collection_cycle_safe.sh'", "")


def test_scheduler_checks_never_dump_full_cron_contents(remote_script):
    section = remote_script[
        remote_script.index("scheduler provenance: this account's own crontab") : remote_script.index(
            "collection scheduler safety"
        )
    ]
    # Only counts/labels are ever echoed -- never the raw crontab/unit text itself.
    assert "echo \"$OWN_CRONTAB_OUTPUT\"" not in section
    assert "echo \"$ETC_CRONTAB_CONTENT\"" not in section
    assert "echo \"$ROOT_CRONTAB_OUTPUT\"" not in section
    assert "echo \"$EFFECTIVE_UNIT_CONFIG\"" not in section


# =====================================================================
# Preconditions before mutation (git, host files, API, DB/frontend/tor,
# bind mount) -- all must appear textually before the reset line
# =====================================================================
@pytest.fixture(scope="module")
def reset_index(remote_script) -> int:
    return remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')


def test_git_head_precondition_before_mutation(remote_script, reset_index):
    idx = remote_script.index('[ "$CURRENT_SHA" = "$INCIDENT_GIT_SHA" ]')
    assert idx < reset_index


def test_target_commit_object_check_before_mutation_no_pull(remote_script, reset_index):
    idx = remote_script.index("git cat-file -e \"${RECOVERY_TARGET_SHA}^{commit}\"")
    assert idx < reset_index
    # "git pull" appears only as prose inside a fail() message ("refusing
    # to git pull, failing before mutation") -- assert it is never
    # actually invoked as a command (start of line, or after a shell
    # separator).
    assert re.search(r"(^|[;&|]\s*)git pull\b", remote_script, re.MULTILINE) is None
    assert re.search(r"(^|[;&|]\s*)git fetch\b", remote_script, re.MULTILINE) is None


def test_host_file_provenance_checked_before_mutation(remote_script, reset_index):
    for var in ("INCIDENT_FRONTEND_SHA256", "INCIDENT_PROVIDER_SHA256", "INCIDENT_COMPOSE_SHA256"):
        idx = remote_script.index(f'"$ACTUAL_{var.split("INCIDENT_")[1]}" = "${var}"'.replace("ACTUAL_", "ACTUAL_"))
    # simpler direct assertions:
    assert remote_script.index('"$ACTUAL_FRONTEND_SHA256" = "$INCIDENT_FRONTEND_SHA256"') < reset_index
    assert remote_script.index('"$ACTUAL_PROVIDER_SHA256" = "$INCIDENT_PROVIDER_SHA256"') < reset_index
    assert remote_script.index('"$ACTUAL_COMPOSE_SHA256" = "$INCIDENT_COMPOSE_SHA256"') < reset_index


def test_api_precondition_checked_before_mutation(remote_script, reset_index):
    for needle in (
        '"$API_ID_BEFORE" = "$EXPECTED_API_CONTAINER_ID"',
        '"$API_CONFIG_IMAGE_BEFORE" = "$EXPECTED_CURRENT_API_CONFIG_IMAGE"',
        '"$API_IMAGE_ID_BEFORE" = "$EXPECTED_API_IMAGE_ID"',
        '"$API_RUNNING_BEFORE" = "true"',
        '"$API_HEALTH_BEFORE" = "healthy"',
        '"$API_RESTART_COUNT_BEFORE" = "0"',
    ):
        assert remote_script.index(needle) < reset_index


def test_api_never_recreated_only_inspected_and_exec(remote_script):
    for line in _executable_lines(remote_script):
        if "jobpulse-api-prod" not in line:
            continue
        assert (
            "docker inspect jobpulse-api-prod" in line
            or "docker exec jobpulse-api-prod" in line
            or "curl" in line
            or "docker top jobpulse-api-prod" in line
        ), line


def test_search_transport_and_tor_enabled_precondition(remote_script, reset_index):
    idx = remote_script.index('[ "$TOR_ENABLED_BEFORE" = "false" ]')
    assert idx < reset_index
    assert 'printenv SEARCH_TRANSPORT' in remote_script
    assert 'printenv TOR_ENABLED' in remote_script


def test_narrow_env_inspection_no_full_dump(remote_script):
    assert "printenv\n" not in remote_script
    assert re.search(r"docker exec [a-z-]+ env\b", remote_script) is None
    assert ".Config.Env" not in remote_script


def test_db_frontend_tor_container_id_precondition(remote_script, reset_index):
    for needle in (
        '"$DB_ID_BEFORE" = "$EXPECTED_DB_CONTAINER_ID"',
        '"$FRONTEND_ID_BEFORE" = "$EXPECTED_FRONTEND_CONTAINER_ID"',
        '"$TOR_ID_BEFORE" = "$EXPECTED_TOR_CONTAINER_ID"',
    ):
        assert remote_script.index(needle) < reset_index


def test_tor_published_ports_checked_none(remote_script):
    assert '[ -z "$TOR_PUBLISHED_PORTS_BEFORE" ]' in remote_script
    assert '[ -z "$TOR_PUBLISHED_PORTS_AFTER" ]' in remote_script


def test_frontend_never_restarted_or_recreated(remote_script):
    for line in _executable_lines(remote_script):
        if "frontend" not in line.lower():
            continue
        assert "docker restart" not in line
        assert "docker create" not in line
        assert not re.search(r"docker compose.*\bup\b", line)


def test_db_never_restarted_or_recreated(remote_script):
    for line in _executable_lines(remote_script):
        if "postgres" not in line.lower():
            continue
        assert "docker restart" not in line
        assert not re.search(r"docker compose.*\bup\b", line)


def test_tor_never_restarted_or_recreated(remote_script):
    for line in _executable_lines(remote_script):
        if "tor" not in line.lower():
            continue
        assert "docker restart" not in line
        assert not re.search(r"docker compose.*\bup\b", line)


def test_frontend_bind_mount_source_verified_before_mutation(remote_script, reset_index):
    idx = remote_script.index('"$MOUNT_SOURCE" = "/opt/jobpulse/frontend"')
    assert idx < reset_index
    assert '"$MOUNT_TYPE" = "bind"' in remote_script
    assert '"$MOUNT_RW" = "false"' in remote_script


# =====================================================================
# Post-reset validations, failure behavior (no reverse rollback)
# =====================================================================
def test_post_reset_git_validated_no_auto_revert(remote_script, reset_index):
    idx = remote_script.index('"$NEW_SHA" = "$RECOVERY_TARGET_SHA"')
    assert idx > reset_index
    fail_line = [l for l in remote_script.splitlines() if '"$NEW_SHA" = "$RECOVERY_TARGET_SHA"' in l][0]
    assert "NOT reverting" in fail_line or "NOT reverting" in remote_script


def test_post_reset_file_provenance_checked(remote_script, reset_index):
    for needle in (
        '"$POST_FRONTEND_SHA256" = "$RECOVERY_FRONTEND_SHA256"',
        '"$POST_PROVIDER_SHA256" = "$RECOVERY_PROVIDER_SHA256"',
        '"$POST_COMPOSE_SHA256" = "$RECOVERY_COMPOSE_SHA256"',
    ):
        assert remote_script.index(needle) > reset_index


def test_post_reset_container_mount_checked_no_restart(remote_script, reset_index):
    idx = remote_script.index('"$POST_CONTAINER_FRONTEND_SHA256" = "$RECOVERY_FRONTEND_SHA256"')
    assert idx > reset_index
    assert "docker exec jobpulse-frontend-prod sha256sum" in remote_script


def test_post_reset_http_sha256_checked(remote_script, reset_index):
    idx = remote_script.index('"$POST_LIVE_FRONTEND_SHA256" = "$RECOVERY_FRONTEND_SHA256"')
    assert idx > reset_index
    idx_status = remote_script.index('"$POST_LIVE_HTTP_STATUS" = "200"')
    assert idx_status > reset_index


def test_api_container_id_and_image_id_unchanged_after_reset(remote_script, reset_index):
    for needle in (
        '"$API_ID_AFTER" = "$API_ID_BEFORE"',
        '"$API_IMAGE_ID_AFTER" = "$API_IMAGE_ID_BEFORE"',
        '"$API_CONFIG_IMAGE_AFTER" = "$API_CONFIG_IMAGE_BEFORE"',
    ):
        assert remote_script.index(needle) > reset_index


def test_db_frontend_tor_ids_checked_unchanged_after_reset(remote_script, reset_index):
    for needle in (
        '"$DB_ID_AFTER" = "$DB_ID_BEFORE"',
        '"$FRONTEND_ID_AFTER" = "$FRONTEND_ID_BEFORE"',
        '"$TOR_ID_AFTER" = "$TOR_ID_BEFORE"',
    ):
        assert remote_script.index(needle) > reset_index


def test_failure_after_mutation_does_not_reset_git(remote_script):
    """Every fail() call after the reset line must not be followed by
    any git reset -- confirmed globally (only one git reset exists at
    all, verified in test_exactly_one_git_reset_hard_and_it_targets_recovery_sha)."""
    reset_index = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    after = remote_script[reset_index + len('git reset --hard "$RECOVERY_TARGET_SHA"') :]
    assert "git reset" not in after


# =====================================================================
# Success classification (section 30)
# =====================================================================
def test_success_classification_exact_string(remote_script):
    assert "FUNCTIONAL_F476_BASELINE_RESTORED_API_REFERENCE_ALIAS_REMAINS" in remote_script


def test_completion_marker_checked_by_runner(runner_script, remote_script):
    assert "PRODUCTION_SPLIT_RELEASE_RECOVERY_COMPLETE" in remote_script
    assert "PRODUCTION_SPLIT_RELEASE_RECOVERY_COMPLETE" in runner_script


def test_does_not_claim_config_image_normalized(remote_script, runner_script):
    combined = remote_script + runner_script
    assert "normalized" not in combined.lower() or "is left as-is" in combined.lower()


# =====================================================================
# Bash syntax validity
# =====================================================================
def test_remote_script_is_valid_bash_syntax(remote_script):
    result = _run_bash_n(remote_script)
    assert result.returncode == 0, result.stderr


def test_runner_script_is_valid_bash_syntax(runner_script):
    result = _run_bash_n(runner_script)
    assert result.returncode == 0, result.stderr


def test_all_run_steps_are_valid_bash_syntax(steps):
    for step in steps:
        if "run" in step:
            result = _run_bash_n(step["run"])
            assert result.returncode == 0, f"{step.get('name')}: {result.stderr}"


# =====================================================================
# Behavioral harness: run the ACTUAL extracted remote script (verbatim)
# against fake git/docker/curl/sha256sum/flock executables. No Python
# reimplementation of the recovery logic.
# =====================================================================
FAKE_LOCK_HELPER = "flock"

# Resolved once, from the unmodified test-process PATH, and used as an
# absolute-path shebang for every fake/shim executable below (instead
# of `#!/usr/bin/env bash`) so none of them depend on `env` resolving
# `bash` via PATH -- required for the `systemctl_available=False`
# scenario, which narrows a scenario's PATH to fake_bin alone. Also
# used to invoke bash directly in `_run_recovery`.
_BASH_EXECUTABLE = shutil.which("bash") or "/bin/bash"


def _write_fake_executable(bin_dir: Path, name: str, body: str) -> Path:
    path = bin_dir / name
    path.write_text(f"#!{_BASH_EXECUTABLE}\n" + body)
    path.chmod(0o755)
    return path


# Real coreutils the remote script shells out to but this harness never
# fakes (their genuine behavior -- especially `flock`'s real OS-level
# advisory locking and `timeout`'s real bounding -- is exactly what
# several tests are proving), plus `tee` (used by the runner script
# wrapping the remote heredoc). Passthrough shims (thin `exec
# <absolute real path> "$@"` wrappers) are installed into every
# scenario's fake_bin so PATH can be safely narrowed to fake_bin alone
# for the `systemctl_available=False` scenario without breaking any
# earlier step -- resolved from the unscrubbed test-process PATH, never
# from a scenario's own (possibly narrowed) PATH.
_PASSTHROUGH_COMMANDS = ("timeout", "awk", "cat", "cut", "find", "grep", "flock", "tee")


def _write_passthrough_shims(bin_dir: Path) -> None:
    import shlex

    for name in _PASSTHROUGH_COMMANDS:
        real = shutil.which(name)
        if real is None:
            continue
        _write_fake_executable(bin_dir, name, f"exec {shlex.quote(real)} \"$@\"\n")


class Scenario:
    """Baked-in test double state for one behavioral test run. All
    values default to the exact diagnostic-proven incident state
    (run 35442611756) so `Scenario()` alone is the happy path; a test
    overrides only the field(s) needed to induce its failure mode."""

    def __init__(self, tmp_path: Path, **overrides):
        self.tmp_path = tmp_path
        self.fake_root = tmp_path / "jobpulse_root"
        self.outer_lock_path = tmp_path / "outer.lock"
        self.marker = tmp_path / "git_reset_done"
        self.etc_crontab_path = tmp_path / "etc_crontab"
        self.etc_cron_d_path = tmp_path / "etc_cron_d"

        defaults = dict(
            current_git_sha=INCIDENT_GIT_SHA,
            post_reset_git_sha=RECOVERY_TARGET_SHA,
            git_cat_file_exit=0,
            git_reset_exit=0,
            git_status_lines=("?? .tor_control_password", "?? state/"),
            host_frontend_sha_before="1111111111111111111111111111111111111111111111111111111111111111",
            host_frontend_sha_after="2222222222222222222222222222222222222222222222222222222222222222",
            host_provider_sha_before="3333333333333333333333333333333333333333333333333333333333333333",
            host_provider_sha_after="4444444444444444444444444444444444444444444444444444444444444444",
            host_compose_sha_before="5555555555555555555555555555555555555555555555555555555555555555",
            host_compose_sha_after="5555555555555555555555555555555555555555555555555555555555555555",
            container_frontend_sha_before="1111111111111111111111111111111111111111111111111111111111111111",
            container_frontend_sha_after="2222222222222222222222222222222222222222222222222222222222222222",
            live_frontend_sha_before="1111111111111111111111111111111111111111111111111111111111111111",
            live_frontend_sha_after="2222222222222222222222222222222222222222222222222222222222222222",
            host_marker_before=True,
            host_marker_after=False,
            container_marker_before=True,
            container_marker_after=False,
            live_marker_before=True,
            live_marker_after=False,
            live_http_status_after="200",
            api_id=EXPECTED_API_CONTAINER_ID,
            api_config_image=EXPECTED_CURRENT_API_CONFIG_IMAGE,
            api_image_id=EXPECTED_API_IMAGE_ID,
            api_running="true",
            api_health="healthy",
            api_restart_count="0",
            api_direct_health_exit=0,
            api_nginx_health_exit=0,
            search_transport="",  # empty => printenv reports unset
            tor_enabled="false",
            db_id=EXPECTED_DB_CONTAINER_ID,
            db_image_id="sha256:dbimageid000000000000000000000000000000000000000000000000000",
            db_running="true",
            db_health="healthy",
            db_restart_count="0",
            frontend_id=EXPECTED_FRONTEND_CONTAINER_ID,
            frontend_image_id="sha256:feimageid000000000000000000000000000000000000000000000000000",
            frontend_running="true",
            frontend_restart_count="0",
            mount_type="bind",
            mount_source=None,  # filled in after fake_root is known unless overridden
            mount_rw="false",
            tor_id=EXPECTED_TOR_CONTAINER_ID,
            tor_image_id="sha256:torimageid00000000000000000000000000000000000000000000000000",
            tor_running="true",
            tor_health="healthy",
            tor_restart_count="0",
            tor_published_ports="",
            docker_top_output="PID   USER   TIME   COMMAND\n1     root   0:00   uvicorn app.main:app",
            outer_lock_busy=False,
            # Scheduler provenance (Phase 4H correction) -- defaults
            # reproduce the exact historically-proven state: run
            # 34679402811 proved exactly one launcher (this account's
            # own crontab, the canonical line) and no other source.
            own_crontab_lines_override=None,  # not None => REPLACES the entire own-crontab content verbatim
            own_crontab_root=None,  # None => matches fake_root (the script's own substituted expectation)
            own_crontab_outer_lock=None,  # None => matches outer_lock_path (the script's own substituted expectation)
            own_crontab_extra_lines=(),  # extra lines appended verbatim (e.g. a duplicate canonical line)
            own_crontab_exit=0,
            etc_crontab_content=None,  # None => /etc/crontab does not exist
            etc_cron_d_files=None,  # None => /etc/cron.d does not exist; else {filename: content}
            root_crontab_mode="no_crontab",  # "no_crontab" | "content" | "password_required" | "unavailable"
            root_crontab_content="",
            # Systemd fail-closed harness (Phase 4H second correction).
            # systemctl_available=False => `_run_recovery` narrows PATH
            # to fake_bin alone (which has passthrough shims for every
            # other real command the script needs -- see
            # `_write_passthrough_shims`) and no `systemctl` is ever
            # written into it, so this is a genuine "systemctl
            # unavailable" simulation regardless of what the host
            # running the tests has installed.
            systemctl_available=True,
            systemctl_list_exit=0,  # nonzero => `systemctl list-unit-files` fails
            # sequence of (unit_name, show_output, show_exit) -- show_output
            # is the verbatim stdout `systemctl show <unit> --property=...`
            # would produce for that unit; show_exit is its exit status.
            # `unit_name` here is always an ORDINARY (non-template) unit
            # file name with no "@" -- `list-unit-files` emits it as
            # "<unit_name>.service" and it is inspected via `systemctl show`.
            systemd_units=(),
            # Phase 4J: uninstantiated systemd TEMPLATE unit files, e.g.
            # the real apport-coredump-hook@.service that caused run
            # 35846112545 to fail closed. Sequence of
            # (template_name, cat_output, cat_exit) -- `list-unit-files`
            # emits it as "<template_name>@.service" and it is inspected
            # via `systemctl cat`, never `systemctl show`.
            systemd_template_units=(),
            # Phase 4J: exit status of `systemctl list-units --type=service
            # --all` (the separate LOADED-unit enumeration that catches an
            # instantiated template such as foo@instance.service).
            systemd_list_units_exit=0,
            # Phase 4J: sequence of (full_unit_name, show_output, show_exit)
            # for units returned by `systemctl list-units --type=service
            # --all` -- full_unit_name is used verbatim (e.g.
            # "jobpulse-worker@production.service") and is inspected via
            # `systemctl show`, exactly like an ordinary concrete unit.
            systemd_loaded_units=(),
            # Phase 4J correction: when set, this RAW text (one line per
            # physical line) is emitted verbatim as the `systemctl
            # list-units` stdout instead of the normal generated
            # "<unit> loaded active running <desc>" rows -- used only to
            # model a hypothetically malformed/decorated record (e.g. a
            # leading status-circle glyph) reaching the parser despite
            # `--plain`/`--full` having been correctly requested, proving
            # the `*.service` sanity check is a genuine independent
            # defense and not just reliance on the flags.
            systemd_loaded_units_list_raw_override=None,
            collection_env_content=None,  # None => file absent
        )
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, value)
        if self.mount_source is None:
            self.mount_source = str(self.fake_root / "frontend")

    def env(self) -> dict:
        return {
            "INCIDENT_GIT_SHA": INCIDENT_GIT_SHA,
            "RECOVERY_TARGET_SHA": RECOVERY_TARGET_SHA,
            "EXPECTED_API_IMAGE_ID": EXPECTED_API_IMAGE_ID,
            "EXPECTED_CURRENT_API_CONFIG_IMAGE": EXPECTED_CURRENT_API_CONFIG_IMAGE,
            "EXPECTED_API_CONTAINER_ID": EXPECTED_API_CONTAINER_ID,
            "EXPECTED_DB_CONTAINER_ID": EXPECTED_DB_CONTAINER_ID,
            "EXPECTED_FRONTEND_CONTAINER_ID": EXPECTED_FRONTEND_CONTAINER_ID,
            "EXPECTED_TOR_CONTAINER_ID": EXPECTED_TOR_CONTAINER_ID,
            "INCIDENT_FRONTEND_SHA256": self.host_frontend_sha_before,
            "INCIDENT_PROVIDER_SHA256": self.host_provider_sha_before,
            "INCIDENT_COMPOSE_SHA256": self.host_compose_sha_before,
            "RECOVERY_FRONTEND_SHA256": self.host_frontend_sha_after,
            "RECOVERY_PROVIDER_SHA256": self.host_provider_sha_after,
            "RECOVERY_COMPOSE_SHA256": self.host_compose_sha_after,
        }

    def build_fakes(self) -> Path:
        bin_dir = self.tmp_path / "fake_bin"
        bin_dir.mkdir(exist_ok=True)

        _write_passthrough_shims(bin_dir)
        _write_fake_executable(bin_dir, "hostname", "echo jobpulse-prod-test\n")

        # printf '%s\n' with each status line individually quoted
        git_status_printf = "\n".join(f"printf '%s\\n' {self._q(line)}" for line in self.git_status_lines)
        git_body = f'''MARKER={self._q(str(self.marker))}
case "$1" in
  cat-file) exit {self.git_cat_file_exit} ;;
  status)
{git_status_printf if git_status_printf else "true"}
    exit 0 ;;
  rev-parse)
    if [ -e "$MARKER" ]; then
      echo {self._q(self.post_reset_git_sha)}
    else
      echo {self._q(self.current_git_sha)}
    fi
    exit 0 ;;
  reset)
    touch "$MARKER"
    exit {self.git_reset_exit} ;;
  *) exit 1 ;;
esac
'''
        _write_fake_executable(bin_dir, "git", git_body)

        sha256_body = f'''MARKER={self._q(str(self.marker))}
if [ $# -eq 0 ]; then
  cat > /dev/null
  if [ -e "$MARKER" ]; then
    echo {self._q(self.live_frontend_sha_after)}"  -"
  else
    echo {self._q(self.live_frontend_sha_before)}"  -"
  fi
  exit 0
fi
if [ -e "$MARKER" ]; then
  FRONTEND={self._q(self.host_frontend_sha_after)}
  PROVIDER={self._q(self.host_provider_sha_after)}
  COMPOSE={self._q(self.host_compose_sha_after)}
else
  FRONTEND={self._q(self.host_frontend_sha_before)}
  PROVIDER={self._q(self.host_provider_sha_before)}
  COMPOSE={self._q(self.host_compose_sha_before)}
fi
case "$1" in
  frontend/index.html) echo "$FRONTEND  $1" ;;
  scripts/providers/linkedin_browser_provider.py) echo "$PROVIDER  $1" ;;
  docker-compose.prod.yml) echo "$COMPOSE  $1" ;;
  *) echo "0000000000000000000000000000000000000000000000000000000000000000  $1" ;;
esac
'''
        _write_fake_executable(bin_dir, "sha256sum", sha256_body)

        curl_body = f'''MARKER={self._q(str(self.marker))}
args=("$@")
url="${{args[-1]}}"
has_w=no
for a in "${{args[@]}}"; do
  [ "$a" = "-w" ] && has_w=yes
done
case "$url" in
  http://127.0.0.1:8000/health) exit {self.api_direct_health_exit} ;;
  http://127.0.0.1/api/health) exit {self.api_nginx_health_exit} ;;
  http://127.0.0.1/)
    if [ "$has_w" = "yes" ]; then
      printf '%s' {self._q(self.live_http_status_after)}
      exit 0
    fi
    if [ -e "$MARKER" ]; then
      if [ "{"1" if self.live_marker_after else "0"}" = "1" ]; then
        printf 'BODY WITH Open Poster Profile MARKER'
      else
        printf 'BODY WITHOUT THE MARKER'
      fi
    else
      if [ "{"1" if self.live_marker_before else "0"}" = "1" ]; then
        printf 'BODY WITH Open Poster Profile MARKER'
      else
        printf 'BODY WITHOUT THE MARKER'
      fi
    fi
    exit 0 ;;
  *) exit 1 ;;
esac
'''
        _write_fake_executable(bin_dir, "curl", curl_body)

        # No fake `flock` -- the real system binary correctly handles both
        # forms this test needs: `flock -n <fd>` (operates on the real
        # inherited file descriptor the script opened via
        # `exec {FD}>"$PATH"`, so genuine OS advisory-lock semantics
        # apply) and `flock <path> <cmd...>` (used by the test harness's
        # own lock-holder helper below). Faking it would defeat the
        # point of proving real non-blocking-lock behavior.

        mount_line = f"{self.mount_type}|{self.mount_source}|/usr/share/nginx/html|{self.mount_rw}"

        def health_tmpl(status):
            return f'{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}'

        docker_body = f'''
cmd="$1"; shift
case "$cmd" in
  inspect)
    name="$1"; shift
    fmt=""
    while [ $# -gt 0 ]; do
      if [ "$1" = "--format" ]; then fmt="$2"; shift 2; continue; fi
      shift
    done
    case "$name" in
      jobpulse-api-prod)
        case "$fmt" in
          '{{{{.Id}}}}') echo {self._q(self.api_id)} ;;
          '{{{{.Config.Image}}}}') echo {self._q(self.api_config_image)} ;;
          '{{{{.Image}}}}') echo {self._q(self.api_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.api_running)} ;;
          '{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}') echo {self._q(self.api_health)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.api_restart_count)} ;;
          *) echo "UNKNOWN_FORMAT_API:$fmt" >&2; exit 1 ;;
        esac ;;
      jobpulse-postgres-prod)
        case "$fmt" in
          '{{{{.Id}}}}') echo {self._q(self.db_id)} ;;
          '{{{{.Image}}}}') echo {self._q(self.db_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.db_running)} ;;
          '{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}') echo {self._q(self.db_health)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.db_restart_count)} ;;
          *) echo "UNKNOWN_FORMAT_DB:$fmt" >&2; exit 1 ;;
        esac ;;
      jobpulse-frontend-prod)
        case "$fmt" in
          '{{{{.Id}}}}') echo {self._q(self.frontend_id)} ;;
          '{{{{.Image}}}}') echo {self._q(self.frontend_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.frontend_running)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.frontend_restart_count)} ;;
          '{{{{range .Mounts}}}}{{{{if eq .Destination "/usr/share/nginx/html"}}}}{{{{.Type}}}}|{{{{.Source}}}}|{{{{.Destination}}}}|{{{{.RW}}}}{{{{end}}}}{{{{end}}}}') echo {self._q(mount_line)} ;;
          *) echo "UNKNOWN_FORMAT_FRONTEND:$fmt" >&2; exit 1 ;;
        esac ;;
      jobpulse-tor-prod)
        case "$fmt" in
          '{{{{.Id}}}}') echo {self._q(self.tor_id)} ;;
          '{{{{.Image}}}}') echo {self._q(self.tor_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.tor_running)} ;;
          '{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}') echo {self._q(self.tor_health)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.tor_restart_count)} ;;
          *) echo "UNKNOWN_FORMAT_TOR:$fmt" >&2; exit 1 ;;
        esac ;;
      *) exit 1 ;;
    esac
    ;;
  exec)
    name="$1"; shift
    case "$name" in
      jobpulse-api-prod)
        if [ "$1" = "printenv" ]; then
          var="$2"
          case "$var" in
            SEARCH_TRANSPORT)
              if [ -n "{self.search_transport}" ]; then echo {self._q(self.search_transport)}; exit 0; else exit 1; fi
              ;;
            TOR_ENABLED)
              echo {self._q(self.tor_enabled)}
              exit 0
              ;;
          esac
        fi
        exit 1
        ;;
      jobpulse-frontend-prod)
        MARKER={self._q(str(self.marker))}
        if [ "$1" = "sha256sum" ]; then
          if [ -e "$MARKER" ]; then
            echo {self._q(self.container_frontend_sha_after)}"  $2"
          else
            echo {self._q(self.container_frontend_sha_before)}"  $2"
          fi
          exit 0
        fi
        if [ "$1" = "grep" ]; then
          if [ -e "$MARKER" ]; then
            [ "{"1" if self.container_marker_after else "0"}" = "1" ] && exit 0 || exit 1
          else
            [ "{"1" if self.container_marker_before else "0"}" = "1" ] && exit 0 || exit 1
          fi
        fi
        exit 1
        ;;
      *) exit 1 ;;
    esac
    ;;
  top)
    printf '%s\\n' {self._q(self.docker_top_output)}
    exit 0
    ;;
  port)
    printf '%s\\n' {self._q(self.tor_published_ports)}
    exit 0
    ;;
  *) exit 1 ;;
esac
'''
        _write_fake_executable(bin_dir, "docker", docker_body)

        # --- Scheduler provenance fakes (Phase 4H correction) ---------
        if self.own_crontab_lines_override is not None:
            own_crontab_lines = list(self.own_crontab_lines_override)
        else:
            root_for_cron = self.own_crontab_root if self.own_crontab_root is not None else str(self.fake_root)
            outer_lock_for_cron = (
                self.own_crontab_outer_lock if self.own_crontab_outer_lock is not None else str(self.outer_lock_path)
            )
            canonical_line_for_cron = (
                f"*/30 * * * * cd {root_for_cron} && flock -n {outer_lock_for_cron} "
                "./scripts/run_collection_cycle_safe.sh"
            )
            own_crontab_lines = [canonical_line_for_cron, *self.own_crontab_extra_lines]

        own_crontab_printf = "\n".join(f"printf '%s\\n' {self._q(line)}" for line in own_crontab_lines)
        root_crontab_printf = "\n".join(f"printf '%s\\n' {self._q(line)}" for line in self.root_crontab_content.splitlines())

        crontab_body = f'''
if [ "$1" = "-u" ] && [ "$2" = "root" ] && [ "$3" = "-l" ]; then
  case {self._q(self.root_crontab_mode)} in
    content)
      {root_crontab_printf if root_crontab_printf else "true"}
      exit 0 ;;
    *)
      echo "no crontab for root" >&2
      exit 1 ;;
  esac
fi
if [ "$1" = "-l" ]; then
  if [ {int(self.own_crontab_exit)} -ne 0 ]; then
    echo "no crontab for tester" >&2
    exit {int(self.own_crontab_exit)}
  fi
  {own_crontab_printf if own_crontab_printf else "true"}
  exit 0
fi
exit 1
'''
        _write_fake_executable(bin_dir, "crontab", crontab_body)

        # Strips leading sudo options (only `-n` is ever used by the
        # remote script) then either simulates a sudo-level failure
        # (password required) or execs straight through to the fake
        # `crontab` above -- never a real privilege escalation.
        sudo_body = f'''
case {self._q(self.root_crontab_mode)} in
  password_required)
    echo "sudo: a password is required" >&2
    exit 1 ;;
  unavailable)
    echo "sudo: crontab: command not found" >&2
    exit 127 ;;
esac
while [[ "$1" == -* ]]; do shift; done
exec "$@"
'''
        _write_fake_executable(bin_dir, "sudo", sudo_body)

        # Configurable fail-closed systemd harness (Phase 4H second
        # correction). By default (systemd_units=(), all exits 0) this
        # reproduces the historically-proven real environment: zero
        # units discovered, proving the loop does not crash/hang/block
        # recovery when discovery finds nothing. Individual behavioral
        # tests override systemctl_available / systemctl_list_exit /
        # systemd_units to drive the ACTUAL extracted remote script down
        # every fail-closed path.
        if self.systemctl_available:
            # Persistent unit-file inventory (`systemctl list-unit-files`):
            # ordinary concrete names plus, separately, uninstantiated
            # template names (emitted with the "@.service" suffix).
            # Modeled as realistic multi-column `systemctl list-unit-files`
            # rows (UNIT FILE / STATE / VENDOR PRESET) -- the script must
            # still correctly extract only the first field.
            unit_list_printf = "\n".join(
                f"printf '%s\\n' {self._q(name + '.service enabled enabled')}" for name, _show_output, _show_exit in self.systemd_units
            )
            template_list_printf = "\n".join(
                f"printf '%s\\n' {self._q(name + '@.service static -')}" for name, _cat_output, _cat_exit in self.systemd_template_units
            )
            combined_unit_list_printf = "\n".join(p for p in (unit_list_printf, template_list_printf) if p)

            show_cases = "\n".join(
                f'''  {self._q(name + ".service")})
    printf '%s' {self._q(show_output)}
    exit {int(show_exit)} ;;'''
                for name, show_output, show_exit in self.systemd_units
            )
            loaded_show_cases = "\n".join(
                f'''  {self._q(full_name)})
    printf '%s' {self._q(show_output)}
    exit {int(show_exit)} ;;'''
                for full_name, show_output, show_exit in self.systemd_loaded_units
            )
            cat_cases = "\n".join(
                f'''  {self._q(name + "@.service")})
    printf '%s' {self._q(cat_output)}
    exit {int(cat_exit)} ;;'''
                for name, cat_output, cat_exit in self.systemd_template_units
            )

            # Loaded-unit inventory (`systemctl list-units --type=service
            # --all --plain --full`) -- this is what would surface an
            # INSTANTIATED copy of a template (e.g. foo@instance.service)
            # even though the persistent unit-file inventory only ever
            # contains the bare foo@.service template name. Modeled as
            # realistic `systemctl list-units --plain` rows (UNIT LOAD
            # ACTIVE SUB DESCRIPTION), including a unit whose SUB column
            # would render a status-circle decoration WITHOUT --plain --
            # the script must still correctly extract only the first
            # field.
            if self.systemd_loaded_units_list_raw_override is not None:
                loaded_unit_list_printf = "\n".join(
                    f"printf '%s\\n' {self._q(line)}"
                    for line in self.systemd_loaded_units_list_raw_override.splitlines()
                )
            else:
                loaded_unit_list_printf = "\n".join(
                    f"printf '%s\\n' {self._q(full_name + ' loaded active running Service')}"
                    for full_name, _show_output, _show_exit in self.systemd_loaded_units
                )

            systemctl_body = f'''
case "$1" in
  list-unit-files)
    if [ {int(self.systemctl_list_exit)} -ne 0 ]; then
      echo "simulated systemctl list-unit-files failure" >&2
      exit {int(self.systemctl_list_exit)}
    fi
    {combined_unit_list_printf if combined_unit_list_printf else "true"}
    exit 0 ;;
  list-units)
    # Phase 4J correction: require the caller to actually pass --plain
    # and --full -- without --plain, real systemctl would render a
    # status-circle glyph for a failed/degraded unit as a leading
    # pseudo-column, and without --full a long unit name could be
    # ellipsized. If a future refactor silently drops either flag, this
    # fake fails loudly instead of the tests continuing to pass against
    # an unrealistic happy-path shape.
    HAS_PLAIN=no
    HAS_FULL=no
    for a in "$@"; do
      case "$a" in
        --plain) HAS_PLAIN=yes ;;
        --full) HAS_FULL=yes ;;
      esac
    done
    if [ "$HAS_PLAIN" != "yes" ] || [ "$HAS_FULL" != "yes" ]; then
      echo "fake systemctl: list-units invoked without required --plain/--full flags" >&2
      exit 1
    fi
    if [ {int(self.systemd_list_units_exit)} -ne 0 ]; then
      echo "simulated systemctl list-units failure" >&2
      exit {int(self.systemd_list_units_exit)}
    fi
    {loaded_unit_list_printf if loaded_unit_list_printf else "true"}
    exit 0 ;;
  show)
    unit="$2"
    case "$unit" in
{show_cases}
{loaded_show_cases}
      *)
        printf 'ExecStart=\\nEnvironment=\\nEnvironmentFiles=\\nFragmentPath=\\nDropInPaths=\\n'
        exit 0 ;;
    esac
    ;;
  cat)
    unit="$2"
    case "$unit" in
{cat_cases}
      *)
        exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
'''
            _write_fake_executable(bin_dir, "systemctl", systemctl_body)
        # else: deliberately do not create a `systemctl` executable --
        # combined with the PATH scrubbing in `_run_recovery`, this
        # simulates genuine systemctl unavailability regardless of
        # whether the host running the tests actually has systemd.

        if self.etc_crontab_content is not None:
            self.etc_crontab_path.parent.mkdir(parents=True, exist_ok=True)
            self.etc_crontab_path.write_text(self.etc_crontab_content)

        if self.etc_cron_d_files is not None:
            self.etc_cron_d_path.mkdir(parents=True, exist_ok=True)
            for fname, fcontent in self.etc_cron_d_files.items():
                (self.etc_cron_d_path / fname).write_text(fcontent)

        # Host-file existence/content checks the real remote script runs
        # directly against the filesystem (grep -F on frontend/index.html,
        # `[ -e .tor_control_password ]`, `[ -d state ]`) -- these need
        # real files under fake_root since they are not shelled out to a
        # fake executable.
        self.fake_root.mkdir(parents=True, exist_ok=True)
        (self.fake_root / "frontend").mkdir(exist_ok=True)
        (self.fake_root / "scripts" / "providers").mkdir(parents=True, exist_ok=True)
        (self.fake_root / "state").mkdir(exist_ok=True)
        (self.fake_root / ".tor_control_password").write_text("x")
        self._write_frontend_index(self.host_marker_before)

        if self.collection_env_content is not None:
            (self.fake_root / ".collection.env").write_text(self.collection_env_content)

        return bin_dir

    def _write_frontend_index(self, marker_present: bool):
        content = "Open Poster Profile" if marker_present else "no marker here"
        (self.fake_root / "frontend" / "index.html").write_text(content)

    @staticmethod
    def _q(value) -> str:
        import shlex

        return shlex.quote(str(value))


def _run_recovery(scenario: Scenario, remote_script: str, *, hold_outer_lock=False):
    fake_bin = scenario.build_fakes()
    substituted = (
        remote_script.replace("/opt/jobpulse", str(scenario.fake_root))
        .replace("/tmp/jobpulse_collection_cycle.lock", str(scenario.outer_lock_path))
        .replace("/etc/crontab", str(scenario.etc_crontab_path))
        .replace("/etc/cron.d", str(scenario.etc_cron_d_path))
    )
    script = "#!/usr/bin/env bash\n" + substituted

    env = dict(os.environ)
    if getattr(scenario, "systemctl_available", True):
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    else:
        # fake_bin alone: every command the remote script needs is
        # either explicitly faked or a passthrough shim to the real
        # binary (see _write_passthrough_shims), so this reliably
        # simulates genuine systemctl unavailability without depending
        # on what directory layout the host running these tests has.
        env["PATH"] = str(fake_bin)
    env.update(scenario.env())

    holder = None
    if hold_outer_lock:
        holder = sp.Popen(["flock", str(scenario.outer_lock_path), "sleep", "5"])
        time.sleep(0.3)

    try:
        result = sp.run([_BASH_EXECUTABLE, "-c", script], capture_output=True, text=True, timeout=20, env=env)
    finally:
        if holder is not None:
            holder.kill()
            holder.wait()

    return result


# --- Scenario 1: exact incident state -> reset to f476 -> all validations pass
def test_behavior_happy_path_recovery_succeeds(remote_script, tmp_path):
    scenario = Scenario(tmp_path)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PRODUCTION_SPLIT_RELEASE_RECOVERY_COMPLETE" in result.stdout
    assert "FUNCTIONAL_F476_BASELINE_RESTORED_API_REFERENCE_ALIAS_REMAINS" in result.stdout
    assert (scenario.marker).exists()  # git reset actually ran


# --- Scenario 2: wrong current git SHA -> no mutation
def test_behavior_wrong_current_git_sha_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, current_git_sha="0" * 40)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "is not the expected incident SHA" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 3: wrong API image ID -> no mutation
def test_behavior_wrong_api_image_id_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, api_image_id="sha256:" + "9" * 64)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "API image ID" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 4: busy outer lock -> no mutation
def test_behavior_busy_outer_lock_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path)
    result = _run_recovery(scenario, remote_script, hold_outer_lock=True)
    assert result.returncode != 0
    assert "outer collection lock busy" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 5: active collector -> no mutation
def test_behavior_active_collector_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        docker_top_output="PID   USER   TIME   COMMAND\n42    root   0:05   python -m scripts.process_search_demand_queue",
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "active collector process detected" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 6: frontend bind mount source mismatch -> no mutation
def test_behavior_frontend_mount_source_mismatch_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, mount_source="/var/lib/some-other-path")
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "frontend mount source is not" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 7: after reset HTTP hash mismatch -> fails, does NOT reset back to bccbbd
def test_behavior_post_reset_http_hash_mismatch_fails_without_reverting(remote_script, tmp_path):
    scenario = Scenario(tmp_path, live_frontend_sha_after="deadbeef" * 8)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "post-reset live HTTP frontend SHA-256" in result.stderr
    # the mutation DID happen (git reset ran) -- the point of this test
    # is that failure afterward never triggers a reverse reset back to
    # the incident SHA. The fake git's `reset` subcommand only ever sets
    # the post-reset marker forward (it has no code path that could move
    # rev-parse HEAD back to INCIDENT_GIT_SHA), so asserting there is
    # exactly one `git reset` invocation in the whole run is sufficient
    # proof no reverse reset was attempted.
    assert scenario.marker.exists()
    reset_log_lines = [
        line for line in result.stdout.splitlines() if line.strip().startswith("git reset")
    ]
    assert len(reset_log_lines) == 0  # the actual `git reset` call itself is never echoed as a log line
    assert result.stdout.count("git_recovery_started=true") == 1


# --- Scenario 8: after reset API state drift -> fails without attempting container recreation
def test_behavior_post_reset_api_drift_fails_without_recreation(remote_script, tmp_path):
    scenario = Scenario(tmp_path)
    # Simulate API restart_count changing across the reset window by
    # using an api_field function whose RestartCount answer flips after
    # the marker exists -- achieved here via a second scenario variable
    # would require dynamic docker fake; simplest faithful simulation:
    # override restart_count to a value that will mismatch itself is not
    # possible statically, so instead we drift image ID after reset by
    # having the API's Config.Image differ between before/after through
    # the git marker (docker fake doesn't consult marker for API image,
    # so instead assert the drift path via an inconsistent RestartCount
    # baked directly as a literal mismatch check): use restart_count "1"
    # only for verification the invariant-check code path exists and
    # fails closed, not a full dynamic-drift simulation.
    scenario2 = Scenario(tmp_path, api_restart_count="0")
    fake_bin = scenario2.build_fakes()
    # Patch the docker fake in-place to report a DIFFERENT restart count
    # on the SECOND invocation (post-reset) than the first (pre-reset),
    # proving the post-reset invariant check actually fires.
    docker_path = fake_bin / "docker"
    original = docker_path.read_text()
    counter_file = tmp_path / "api_restartcount_calls"
    patched = original.replace(
        "'{{.RestartCount}}') echo " + Scenario._q("0") + " ;;\n          *) echo \"UNKNOWN_FORMAT_API:$fmt\" >&2; exit 1 ;;",
        (
            "'{{.RestartCount}}')\n"
            f"            COUNT_FILE={Scenario._q(str(counter_file))}\n"
            "            if [ -e \"$COUNT_FILE\" ]; then echo 1; else touch \"$COUNT_FILE\"; echo 0; fi\n"
            "            ;;\n"
            "          *) echo \"UNKNOWN_FORMAT_API:$fmt\" >&2; exit 1 ;;"
        ),
    )
    assert patched != original, "patch target string not found -- fixture drifted from workflow source"
    docker_path.write_text(patched)
    docker_path.chmod(0o755)

    substituted = (
        remote_script.replace("/opt/jobpulse", str(scenario2.fake_root))
        .replace("/tmp/jobpulse_collection_cycle.lock", str(scenario2.outer_lock_path))
        .replace("/etc/crontab", str(scenario2.etc_crontab_path))
        .replace("/etc/cron.d", str(scenario2.etc_cron_d_path))
    )
    script = "#!/usr/bin/env bash\n" + substituted
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env.update(scenario2.env())
    result = sp.run(["bash", "-c", script], capture_output=True, text=True, timeout=20, env=env)

    assert result.returncode != 0
    assert "API restart_count changed" in result.stderr
    # confirms failure never attempted any container recreation command
    assert "docker compose" not in result.stdout
    assert "docker create" not in result.stdout
    assert "docker restart" not in result.stdout


# =====================================================================
# Scheduler provenance behavioral scenarios (Phase 4H correction).
# Same discipline as the 8 scenarios above: the ACTUAL extracted remote
# script is executed verbatim via `bash -c` against fake
# crontab/sudo/systemctl/git/docker/curl/sha256sum -- no Python
# reimplementation of the recovery or provenance logic.
# =====================================================================

# --- Scenario 1 (repeat, explicit): exact canonical own crontab, no
# alternate sources, no internal override -> reaches recovery mutation.
def test_behavior_scheduler_provenance_exact_canonical_reaches_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path)  # defaults ARE the historically-proven state
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "own_crontab_provenance=confirmed_canonical_launcher" in result.stdout
    assert "etc_crontab_drift=none" in result.stdout
    assert "cron_d_drift=none" in result.stdout
    assert "root_crontab_drift=none" in result.stdout
    assert "systemd_drift=none" in result.stdout
    assert "effective_internal_lock_provenance=confirmed_or_absent_default" in result.stdout
    assert "PRODUCTION_SPLIT_RELEASE_RECOVERY_COMPLETE" in result.stdout
    assert scenario.marker.exists()


# --- Scenario 2: canonical own-crontab line absent -> fail before mutation
def test_behavior_canonical_own_crontab_line_absent_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        own_crontab_lines_override=["*/5 * * * * /usr/bin/true # unrelated job"],
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "does not contain exactly one occurrence of the historically proven canonical launcher line" in result.stderr
    assert not scenario.marker.exists()
    assert "docker compose" not in result.stdout
    assert "docker create" not in result.stdout


# --- Scenario 3: canonical launcher path (root) drift -> fail before mutation
def test_behavior_canonical_launcher_root_drift_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, own_crontab_root="/opt/some-other-jobpulse")
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "does not contain exactly one occurrence of the historically proven canonical launcher line" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 4: outer flock path drift -> fail before mutation
def test_behavior_outer_flock_path_drift_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, own_crontab_outer_lock="/tmp/some-other.lock")
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "does not contain exactly one occurrence of the historically proven canonical launcher line" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 5: duplicate launcher -> fail before mutation
def test_behavior_duplicate_launcher_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path)
    # Build a second, identical canonical line as an "extra" line so the
    # own crontab contains the canonical launcher twice.
    canonical = f"*/30 * * * * cd {scenario.fake_root} && flock -n {scenario.outer_lock_path} ./scripts/run_collection_cycle_safe.sh"
    scenario.own_crontab_extra_lines = (canonical,)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert (
        "does not contain exactly one occurrence of the historically proven canonical launcher line" in result.stderr
        or "mentions run_collection_cycle_safe.sh" in result.stderr
    )
    assert not scenario.marker.exists()


# --- Scenario 6: alternate launcher found in /etc/cron.d -> fail before mutation
def test_behavior_alternate_launcher_in_cron_d_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        etc_cron_d_files={"jobpulse-collection": "*/15 * * * * root cd /opt/jobpulse && ./scripts/run_collection_cycle_safe.sh\n"},
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "now references the collection wrapper or its launcher variables" in result.stderr
    assert not scenario.marker.exists()
    assert "docker compose" not in result.stdout


# --- Scenario 7: root crontab contains wrapper reference -> fail before mutation
def test_behavior_root_crontab_wrapper_reference_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        root_crontab_mode="content",
        root_crontab_content="*/30 * * * * cd /opt/jobpulse && ./scripts/run_collection_cycle_safe.sh\n",
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "root's crontab now references the collection wrapper or its launcher variables" in result.stderr
    assert not scenario.marker.exists()


# --- Scenario 8: .collection.env changes internal lock path -> fail before mutation
def test_behavior_collection_env_internal_lock_drift_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        collection_env_content="JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/opt/jobpulse/state/other.lock\n",
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "not the expected" in result.stderr
    assert not scenario.marker.exists()
    assert "docker compose" not in result.stdout


# --- Extra: root crontab in the historically-proven "no crontab for
# root" state must NOT block recovery (this is the documented, accepted
# empty state for root specifically -- unlike the account's own crontab).
def test_behavior_root_crontab_no_crontab_state_is_accepted(remote_script, tmp_path):
    scenario = Scenario(tmp_path, root_crontab_mode="no_crontab")
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert scenario.marker.exists()


# --- Extra: sudo requiring a password for the root-crontab read fails closed
def test_behavior_root_crontab_password_required_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, root_crontab_mode="password_required")
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "cannot establish root's crontab configuration read-only" in result.stderr
    assert not scenario.marker.exists()


# --- Extra: an unreadable own crontab (nonzero exit) is a failure, not
# an acceptable empty state (Section 3's explicit requirement).
def test_behavior_own_crontab_unreadable_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, own_crontab_exit=1)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "historical evidence proves a scheduled launcher exists here" in result.stderr
    assert not scenario.marker.exists()


# --- Extra: .collection.env absent is accepted (wrapper's own source default applies)
def test_behavior_collection_env_absent_is_accepted(remote_script, tmp_path):
    scenario = Scenario(tmp_path, collection_env_content=None)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert scenario.marker.exists()


# =====================================================================
# Systemd fail-closed behavioral scenarios (second independent review
# pass). Same discipline as every scenario above: the ACTUAL extracted
# remote script is executed verbatim via `bash -c` against a
# configurable fake `systemctl` (and, for Scenario A, a PATH actively
# scrubbed of any real `systemctl`) -- no Python reimplementation of
# the drift-check logic. Every failure scenario proves: no git-reset
# marker, no docker compose mutation, no container
# restart/create/recreate.
# =====================================================================
def _assert_no_mutation_occurred(result, scenario):
    assert not scenario.marker.exists()
    for forbidden in ("docker compose", "docker create", "docker restart", "docker start"):
        assert forbidden not in result.stdout


# --- Scenario A: systemctl unavailable -> failure, no git-reset marker
def test_behavior_systemd_scenario_a_systemctl_unavailable_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, systemctl_available=False)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "systemctl command unavailable" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Scenario B: systemctl list-unit-files returns nonzero -> failure, no mutation
def test_behavior_systemd_scenario_b_list_unit_files_failure_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, systemctl_list_exit=1)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "systemctl list-unit-files failed in a bounded read" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Scenario C: one service exists and `systemctl show` fails -> failure, no mutation
def test_behavior_systemd_scenario_c_show_failure_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_units=(("jobpulse-collector", "", 1),),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "systemctl show failed in a bounded read" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Scenario D: one service's effective ExecStart contains the wrapper -> failure before mutation
def test_behavior_systemd_scenario_d_effective_execstart_wrapper_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_units=(
            (
                "jobpulse-collector",
                "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/jobpulse-collector.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "effective configuration references the collection wrapper" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Scenario E: one service has effective Environment=JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=<different path> -> failure before mutation
def test_behavior_systemd_scenario_e_effective_environment_lock_var_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_units=(
            (
                "jobpulse-collector",
                "ExecStart=/usr/bin/true\nEnvironment=JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/different/path\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/jobpulse-collector.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "effective configuration references the collection wrapper or its launcher variables" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Scenario E2: effective Environment=JOBPULSE_COLLECTION_ROOT=<value> also fails closed
def test_behavior_systemd_scenario_e2_effective_environment_root_var_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_units=(
            (
                "jobpulse-collector",
                "ExecStart=/usr/bin/true\nEnvironment=JOBPULSE_COLLECTION_ROOT=/opt/some-other\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/jobpulse-collector.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "effective configuration references the collection wrapper or its launcher variables" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Scenario F: clean systemd enumeration with multiple irrelevant units -> provenance passes, reaches mutation
def test_behavior_systemd_scenario_f_clean_enumeration_reaches_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_units=(
            (
                "unrelated-a",
                "ExecStart=/usr/bin/true\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/unrelated-a.service\nDropInPaths=\n",
                0,
            ),
            (
                "unrelated-b",
                "ExecStart=/usr/sbin/nginx -g daemon off;\nEnvironment=SOME_OTHER_VAR=1\nEnvironmentFiles=/etc/default/unrelated-b\nFragmentPath=/etc/systemd/system/unrelated-b.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemd_drift=none" in result.stdout
    assert scenario.marker.exists()


# =====================================================================
# Phase 4J: systemd TEMPLATE unit handling.
#
# Context: real recovery run 35846112545 (run_attempt 1) failed closed
# (SAFE_PRE_MUTATION_RECOVERY_ABORT) because the live production host's
# persistent unit-file inventory includes the uninstantiated systemd
# template unit apport-coredump-hook@.service, and the Phase 4H scanner
# blindly ran `systemctl show apport-coredump-hook@.service ...` for
# every name `systemctl list-unit-files` returned -- an invalid
# operation against a bare template name, which exited 1. The safety
# policy (fail closed before mutation) was correct; the
# enumeration/inspection model was too naive. Phase 4J fixes this
# SEMANTICALLY (unit-name shape: `*@.service`), never by hardcoding
# apport-coredump-hook@.service as an allowlisted exception:
#   - uninstantiated template unit file -> `systemctl cat` (static
#     definition, read-only)
#   - every other persistent unit-file name, and every LOADED unit name
#     from a separate `systemctl list-units --type=service --all`
#     enumeration (which is how a currently-instantiated copy of a
#     template, e.g. foo@instance.service, is still caught) ->
#     `systemctl show` (effective runtime state), exactly as before.
# Same discipline as every other behavioral scenario in this file: the
# ACTUAL extracted remote script executes verbatim via `bash -c` against
# a configurable fake `systemctl` -- no Python reimplementation of the
# drift-check logic.
# =====================================================================
def _unit_file_loop_section(remote_script: str) -> str:
    return remote_script[
        remote_script.index('UNIT_NAMES="$(awk') : remote_script.index(
            'echo "systemd_unit_file_drift=none"'
        )
    ]


def _template_branch_section(remote_script: str) -> str:
    section = _unit_file_loop_section(remote_script)
    start = section.index("*@.service)")
    end = section.index("*)", start)
    return section[start:end]


def _loaded_unit_section(remote_script: str) -> str:
    return remote_script[
        remote_script.index("timeout 5 systemctl list-units --type=service --all") : remote_script.index(
            'echo "systemd_loaded_unit_drift=none"'
        )
    ]


# --- B: list-units --type=service --all is bounded and fail closed
def test_list_units_is_bounded_and_status_captured(remote_script):
    section = _loaded_unit_section(remote_script)
    assert "timeout 5 systemctl list-units --type=service --all --no-legend --no-pager" in section
    assert "LOADED_UNIT_LIST_STATUS=$?" in section
    assert '[ "$LOADED_UNIT_LIST_STATUS" -eq 0 ] || fail' in section
    assert "|| true" not in section


# --- C: template detection recognizes exactly the semantic form *@.service
def test_template_detection_uses_exact_semantic_glob(remote_script):
    section = _unit_file_loop_section(remote_script)
    assert "*@.service)" in section
    # Semantic (unit-name shape), never a hardcoded allowlist of a known
    # distro-specific unit name. A comment may reference the real
    # incident unit for context; the executable code must not.
    assert "apport-coredump-hook" not in _code_only(section)


# --- D: template unit definitions use systemctl cat
def test_template_branch_uses_systemctl_cat(remote_script):
    branch = _template_branch_section(remote_script)
    assert 'timeout 5 systemctl cat "$unit" --no-pager' in branch


# --- E: template definitions are NOT passed to systemctl show
def test_template_branch_never_calls_systemctl_show(remote_script):
    branch = _code_only(_template_branch_section(remote_script))
    assert "systemctl show" not in branch


# --- F: systemctl cat failure fails closed (structural)
def test_template_cat_failure_is_not_swallowed(remote_script):
    branch = _template_branch_section(remote_script)
    assert "TEMPLATE_CAT_STATUS=$?" in branch
    assert '[ "$TEMPLATE_CAT_STATUS" -eq 0 ] || fail' in branch
    cat_line = next(line for line in branch.splitlines() if "systemctl cat" in line and "timeout" in line)
    assert "|| true" not in cat_line


# --- G: empty template definition fails closed (structural)
def test_template_cat_empty_result_fails_closed(remote_script):
    branch = _template_branch_section(remote_script)
    assert '[ -n "$TEMPLATE_CAT_OUTPUT" ] || fail' in branch


# --- H/I/J: template definition scanned for wrapper + both launcher vars
def test_template_definition_scanned_for_wrapper_and_launcher_vars(remote_script):
    branch = _template_branch_section(remote_script)
    assert "grep -Fq 'run_collection_cycle_safe.sh' <<< \"$TEMPLATE_CAT_OUTPUT\"" in branch
    assert "grep -Fq 'JOBPULSE_COLLECTION_ROOT' <<< \"$TEMPLATE_CAT_OUTPUT\"" in branch
    assert "grep -Fq 'JOBPULSE_COLLECTION_CYCLE_LOCK_PATH' <<< \"$TEMPLATE_CAT_OUTPUT\"" in branch


# --- K: loaded instantiated units use systemctl show, never systemctl cat
def test_loaded_unit_section_uses_show_never_cat(remote_script):
    section = _loaded_unit_section(remote_script)
    assert (
        'timeout 5 systemctl show "$loaded_unit" '
        "--property=ExecStart,Environment,EnvironmentFiles,FragmentPath,DropInPaths --no-pager"
        in section
    )
    assert "systemctl cat" not in section


# --- L: loaded-unit show failure fails closed (structural)
def test_loaded_unit_show_failure_is_not_swallowed(remote_script):
    section = _loaded_unit_section(remote_script)
    assert "LOADED_UNIT_SHOW_STATUS=$?" in section
    assert '[ "$LOADED_UNIT_SHOW_STATUS" -eq 0 ] || fail' in section
    assert '[ -n "$LOADED_UNIT_SHOW_OUTPUT" ] || fail' in section
    assert "|| true" not in section


# --- N: loaded-unit effective wrapper/env references fail closed (structural)
def test_loaded_unit_scanned_for_wrapper_and_launcher_vars(remote_script):
    section = _loaded_unit_section(remote_script)
    assert "grep -Fq 'run_collection_cycle_safe.sh' <<< \"$LOADED_UNIT_SHOW_OUTPUT\"" in section
    assert "grep -Fq 'JOBPULSE_COLLECTION_ROOT' <<< \"$LOADED_UNIT_SHOW_OUTPUT\"" in section
    assert "grep -Fq 'JOBPULSE_COLLECTION_CYCLE_LOCK_PATH' <<< \"$LOADED_UNIT_SHOW_OUTPUT\"" in section


# --- O/P: all systemd checks (unit-file AND loaded-unit) remain before
# lock acquisition and before git reset
def test_loaded_unit_checks_precede_locks_and_git_reset(remote_script):
    loaded_end_idx = remote_script.index('echo "systemd_loaded_unit_drift=none"')
    outer_lock_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    internal_lock_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    reset_idx = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    assert loaded_end_idx < outer_lock_idx < internal_lock_idx < reset_idx


# --- Q: no raw systemd output (list-units, cat) is ever echoed
def test_no_raw_systemd_cat_or_list_units_output_echoed(remote_script):
    for var in ("TEMPLATE_CAT_OUTPUT", "LOADED_UNIT_LIST_OUTPUT", "LOADED_UNIT_SHOW_OUTPUT"):
        for line in remote_script.splitlines():
            if var in line:
                assert not line.strip().startswith("echo") or "fail" in line, line


# --- R: no systemd mutation exists in the new sections
def test_no_systemd_mutation_in_template_or_loaded_sections(remote_script):
    for section in (_unit_file_loop_section(remote_script), _loaded_unit_section(remote_script)):
        for forbidden in (
            "systemctl start",
            "systemctl stop",
            "systemctl restart",
            "systemctl enable",
            "systemctl disable",
            "systemctl daemon-reload",
            "systemctl mask",
            "systemctl unmask",
            "systemctl kill",
        ):
            assert forbidden not in section, forbidden


_BENIGN_TEMPLATE_DEFINITION = (
    "# /lib/systemd/system/apport-coredump-hook@.service\n"
    "[Unit]\n"
    "Description=Process error reports when systemd-coredump receives a coredump\n"
    "Documentation=man:core(5)\n"
    "DefaultDependencies=no\n"
    "[Service]\n"
    "Type=oneshot\n"
    "ExecStart=/bin/sh -c 'if [ -x /usr/share/apport/apport ]; then /usr/share/apport/apport %P %s %c %d %P %E; fi'\n"
    "TimeoutStartSec=30\n"
)


# --- T1: uninstantiated template with benign definition, no relevant
# loaded instance -> systemd provenance passes, recovery reaches mutation.
# Reproduces the real Phase 4I host shape that caused run 35846112545 to
# fail closed.
def test_behavior_systemd_t1_uninstantiated_template_reaches_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(("apport-coredump-hook", _BENIGN_TEMPLATE_DEFINITION, 0),),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemd_drift=none" in result.stdout
    assert scenario.marker.exists()


# --- T2: template definition references the collection wrapper -> failure before mutation
def test_behavior_systemd_t2_template_definition_wrapper_reference_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(
            (
                "jobpulse-worker",
                "[Service]\nExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh %i\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "template unit" in result.stderr
    assert "references the collection wrapper" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- T3: systemctl cat exits nonzero for the template -> failure before mutation
def test_behavior_systemd_t3_template_cat_failure_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(("apport-coredump-hook", "", 1),),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "systemctl cat failed in a bounded read" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- T4: systemctl cat returns an empty result for the template -> failure before mutation
def test_behavior_systemd_t4_template_cat_empty_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(("apport-coredump-hook", "", 0),),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "unusable (empty) result for template unit" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- T5: instantiated template (jobpulse-worker@production.service, found
# only via the LOADED-unit enumeration) has an effective wrapper reference
# -> failure before mutation, even though its template definition alone
# (jobpulse-worker@.service) is irrelevant.
def test_behavior_systemd_t5_instantiated_template_effective_wrapper_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(
            ("jobpulse-worker", "[Service]\nExecStart=/usr/bin/jobpulse-worker %i\n", 0),
        ),
        systemd_loaded_units=(
            (
                "jobpulse-worker@production.service",
                "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/jobpulse-worker@.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "loaded unit" in result.stderr
    assert "references the collection wrapper" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- T6: the loaded-unit enumeration itself (systemctl list-units) exits
# nonzero -> failure before mutation.
def test_behavior_systemd_t6_list_units_failure_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, systemd_list_units_exit=1)
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "systemctl list-units failed in a bounded read" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- T7: systemctl show for a loaded unit exits nonzero -> failure before mutation
def test_behavior_systemd_t7_loaded_unit_show_failure_blocks_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_loaded_units=(("unrelated-loaded.service", "", 1),),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "systemctl show failed in a bounded read" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- T8: clean mix of ordinary services, irrelevant template unit files,
# and irrelevant instantiated template services -> systemd_drift=none,
# recovery proceeds to mutation.
def test_behavior_systemd_t8_clean_mixed_inventory_reaches_mutation(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_units=(
            (
                "unrelated-ordinary",
                "ExecStart=/usr/sbin/nginx -g daemon off;\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/unrelated-ordinary.service\nDropInPaths=\n",
                0,
            ),
        ),
        systemd_template_units=(
            ("irrelevant-template", _BENIGN_TEMPLATE_DEFINITION, 0),
        ),
        systemd_loaded_units=(
            (
                "irrelevant-template@instance1.service",
                "ExecStart=/usr/bin/true\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/irrelevant-template@.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemd_drift=none" in result.stdout
    assert scenario.marker.exists()


# --- Named regression test for the exact real Phase 4I failure
# (run 35846112545): an irrelevant UNINSTANTIATED template
# (apport-coredump-hook@.service) must no longer cause the recovery
# runner to fail merely because `systemctl show <template>@.service`
# would be an invalid operation -- AND the template must still be
# genuinely inspected via `systemctl cat` (not silently skipped): a
# nonzero `systemctl cat` exit for that *same* unit name must still fail
# closed before mutation.
def test_regression_phase4i_apport_coredump_hook_template_no_longer_blocks_recovery(tmp_path_factory, remote_script):
    passthrough_scenario = Scenario(
        tmp_path_factory.mktemp("phase4j_regression_pass"),
        systemd_template_units=(("apport-coredump-hook", _BENIGN_TEMPLATE_DEFINITION, 0),),
    )
    pass_result = _run_recovery(passthrough_scenario, remote_script)
    assert pass_result.returncode == 0, pass_result.stdout + pass_result.stderr
    assert "systemd_drift=none" in pass_result.stdout
    assert passthrough_scenario.marker.exists()

    # Same exact unit name, isolated tmp dir -- proves the template is
    # still genuinely inspected via `systemctl cat` (not skipped): if the
    # runner ever stopped calling `systemctl cat` for this unit, this
    # nonzero-exit configuration would go unnoticed and recovery would
    # wrongly reach mutation.
    still_inspected_scenario = Scenario(
        tmp_path_factory.mktemp("phase4j_regression_inspected"),
        systemd_template_units=(("apport-coredump-hook", "irrelevant but present", 1),),
    )
    inspected_result = _run_recovery(still_inspected_scenario, remote_script)
    assert inspected_result.returncode != 0
    assert "systemctl cat failed in a bounded read" in inspected_result.stderr
    assert "unit=apport-coredump-hook@.service" in inspected_result.stderr
    _assert_no_mutation_occurred(inspected_result, still_inspected_scenario)


# =====================================================================
# Phase 4J correction (independent review): the loaded-unit enumeration
# (`systemctl list-units --type=service --all`) plus `awk '{print $1}'`
# is not machine-safe. Without `--plain`, systemctl can render a leading
# status-circle glyph for a failed/degraded unit (e.g.
# "* broken.service loaded failed failed ..."), which would shift $1
# away from the actual unit name -- the runner could then attempt
# `systemctl show "*"` (or the real glyph) and safe-abort for a parser
# artifact rather than genuine scheduler drift. Without `--full`, a long
# unit name can be ellipsized before either template detection or
# inspection.
#
# Fix: both enumerations now request `--full`; the loaded-unit
# enumeration also requests `--plain`. Parsing uses `awk 'NF {print $1}'`
# (never repairs decorated output after the fact). A defense-in-depth
# `*.service` sanity check on every parsed record refuses to guess and
# fails closed -- before any `systemctl show`/`cat` call -- if a record
# is ever malformed regardless of the flags requested.
# =====================================================================
def test_list_unit_files_requests_full(remote_script):
    section = _systemd_section(remote_script)
    assert "timeout 5 systemctl list-unit-files --type=service --no-legend --no-pager --full" in section


def test_list_units_requests_plain_and_full(remote_script):
    section = _loaded_unit_section(remote_script)
    assert "timeout 5 systemctl list-units --type=service --all --no-legend --no-pager --plain --full" in section


def test_unit_file_parsing_skips_blank_records(remote_script):
    section = _systemd_section(remote_script)
    assert "UNIT_NAMES=\"$(awk 'NF {print $1}' <<< \"$UNIT_LIST_OUTPUT\")\"" in section


def test_loaded_unit_parsing_skips_blank_records(remote_script):
    section = _loaded_unit_section(remote_script)
    assert "LOADED_UNIT_NAMES=\"$(awk 'NF {print $1}' <<< \"$LOADED_UNIT_LIST_OUTPUT\")\"" in section


def _unit_file_sanity_check_section(remote_script: str) -> str:
    section = _unit_file_loop_section(remote_script)
    start = section.index('[ -z "$unit" ] && continue')
    end = section.index("*@.service)")
    return section[start:end]


def _loaded_unit_sanity_check_section(remote_script: str) -> str:
    section = _loaded_unit_section(remote_script)
    start = section.index('[ -z "$loaded_unit" ] && continue')
    end = section.index('LOADED_UNIT_SHOW_STATUS=0')
    return section[start:end]


def test_unit_file_sanity_check_precedes_template_branch(remote_script):
    section = _code_only(_unit_file_sanity_check_section(remote_script))
    assert '*.service) ;;' in section
    assert 'fail "unexpected token while parsing systemctl list-unit-files output' in section
    assert "systemctl show" not in section
    assert "systemctl cat" not in section


def test_loaded_unit_sanity_check_precedes_show(remote_script):
    section = _code_only(_loaded_unit_sanity_check_section(remote_script))
    assert '*.service) ;;' in section
    assert 'fail "unexpected token while parsing systemctl list-units output' in section
    assert "systemctl show" not in section


def test_sanity_check_fail_messages_do_not_dump_full_list_output(remote_script):
    for var in ("UNIT_LIST_OUTPUT", "LOADED_UNIT_LIST_OUTPUT"):
        for line in remote_script.splitlines():
            if "unexpected token while parsing" in line:
                assert var not in line


# --- Structural: no relaxed error handling was introduced alongside the fix
def test_no_relaxed_error_handling_in_sanity_checks(remote_script):
    for section in (
        _unit_file_sanity_check_section(remote_script),
        _loaded_unit_sanity_check_section(remote_script),
    ):
        assert "|| true" not in section


def test_sanity_check_continue_only_used_for_blank_line_skip(remote_script):
    # The only `continue` statements in either loop are the pre-existing
    # blank-line skips (`[ -z "$unit" ] && continue` /
    # `[ -z "$loaded_unit" ] && continue`) -- the new sanity check must
    # `fail`, never silently `continue`/skip a malformed token.
    unit_section = _unit_file_loop_section(remote_script)
    loaded_section = _loaded_unit_section(remote_script)
    for section, var in ((unit_section, "unit"), (loaded_section, "loaded_unit")):
        continue_lines = [line for line in section.splitlines() if "continue" in line]
        assert len(continue_lines) == 1
        assert f'[ -z "${var}" ] && continue' in continue_lines[0]


# --- Behavioral: the dangerous non-plain shape, if it somehow still
# reaches the parser, is rejected by the sanity check before any
# systemctl show/cat call and before mutation.
def test_behavior_status_circle_decorated_line_fails_closed_before_show(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_loaded_units_list_raw_override="● broken.service loaded failed failed Broken_Service",
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "unexpected token while parsing systemctl list-units output" in result.stderr
    assert 'systemctl show "●"' not in result.stdout
    assert 'systemctl show "●"' not in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Behavioral: the normal plain-form row for the SAME unit parses
# correctly and is inspected normally via systemctl show.
def test_behavior_plain_form_loaded_unit_row_parsed_and_inspected_normally(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_loaded_units=(
            (
                "broken.service",
                "ExecStart=/usr/bin/true\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/broken.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemd_drift=none" in result.stdout
    assert scenario.marker.exists()


# --- Behavioral: same decorated-line defense, but proving the wrapper
# scan is never even reached for the malformed token (fails at the
# sanity check, not later at the drift grep).
def test_behavior_status_circle_line_never_reaches_wrapper_scan(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_loaded_units_list_raw_override="● broken.service loaded failed failed Broken_Service",
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "references the collection wrapper" not in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Behavioral: a long unit name survives the enumeration -> parsing ->
# inspection pipeline unchanged (proving `--full` matters end to end,
# independent of any real terminal width).
_LONG_UNIT_BASE = "jobpulse-" + ("x" * 180)


def test_behavior_long_unit_name_reaches_systemctl_show_unchanged(remote_script, tmp_path):
    long_name = _LONG_UNIT_BASE
    scenario = Scenario(
        tmp_path,
        systemd_units=(
            (
                long_name,
                "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/"
                + long_name
                + ".service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert f"systemd unit {long_name}.service effective configuration references the collection wrapper" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


def test_behavior_long_loaded_unit_name_reaches_systemctl_show_unchanged(remote_script, tmp_path):
    long_name = _LONG_UNIT_BASE + "@production.service"
    scenario = Scenario(
        tmp_path,
        systemd_loaded_units=(
            (
                long_name,
                "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/"
                + _LONG_UNIT_BASE
                + "@.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert f"systemd loaded unit {long_name} effective configuration references the collection wrapper" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


# --- Regression: fake systemctl enforces --plain/--full so a future
# refactor that silently drops either flag cannot pass tests unnoticed.
def test_fake_systemctl_rejects_list_units_missing_plain_or_full(tmp_path):
    scenario = Scenario(tmp_path)
    fake_bin = scenario.build_fakes()
    result = sp.run(
        [str(fake_bin / "systemctl"), "list-units", "--type=service", "--all", "--no-legend", "--no-pager"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    result_with_flags = sp.run(
        [
            str(fake_bin / "systemctl"),
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--no-pager",
            "--plain",
            "--full",
        ],
        capture_output=True,
        text=True,
    )
    assert result_with_flags.returncode == 0


# --- Template regression must still pass unchanged with the machine-safe
# enumeration in place (Phase 4J semantics preserved).
def test_regression_apport_template_still_uses_cat_not_show_after_machine_safety_fix(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(("apport-coredump-hook", _BENIGN_TEMPLATE_DEFINITION, 0),),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "systemd_drift=none" in result.stdout
    assert scenario.marker.exists()


# --- Loaded template instance regression must still pass unchanged.
def test_regression_loaded_template_instance_still_uses_show_after_machine_safety_fix(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        systemd_template_units=(
            ("jobpulse-worker", "[Service]\nExecStart=/usr/bin/jobpulse-worker %i\n", 0),
        ),
        systemd_loaded_units=(
            (
                "jobpulse-worker@production.service",
                "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\nEnvironment=\nEnvironmentFiles=\nFragmentPath=/etc/systemd/system/jobpulse-worker@.service\nDropInPaths=\n",
                0,
            ),
        ),
    )
    result = _run_recovery(scenario, remote_script)
    assert result.returncode != 0
    assert "loaded unit" in result.stderr
    assert "references the collection wrapper" in result.stderr
    _assert_no_mutation_occurred(result, scenario)


def test_structural_ordering_unaffected_by_machine_safety_fix(remote_script):
    systemd_idx = remote_script.index("scheduler drift check: systemd")
    loaded_end_idx = remote_script.index('echo "systemd_loaded_unit_drift=none"')
    outer_lock_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    internal_lock_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    docker_top_idx = remote_script.index("docker top jobpulse-api-prod")
    reset_idx = remote_script.index('git reset --hard "$RECOVERY_TARGET_SHA"')
    assert (
        systemd_idx
        < loaded_end_idx
        < outer_lock_idx
        < internal_lock_idx
        < docker_top_idx
        < reset_idx
    )
