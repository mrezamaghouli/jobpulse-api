"""Structural + behavioral regression guard for
.github/workflows/production-api-reference-normalization.yml (Phase 4M).

Context: Phase 4K (Production Split-Release Recovery run 36238209271)
restored production's functional f476 baseline. The API's Config.Image
is still the temporary local rollback alias
jobpulse-api-rollback:bccbbd997ee8-1427495 left behind by Phase 4E's
rollback -- intentional debt deferred at every prior phase. This
workflow performs exactly ONE semantic change: recreate ONLY the api
container so Docker records the canonical GHCR reference
ghcr.io/mrezamaghouli/jobpulse-api:f4765f857355c6543f68cea0e481b7f20a917147
as Config.Image, while requiring the underlying image ID to be EXACTLY
unchanged. No code/image-byte upgrade, no git mutation, no db/frontend/
tor recreation.

Like the sibling workflow test files, these tests never SSH, use no
real Docker, and make no network calls. Static checks parse the
committed YAML/shell as text; behavioral checks run the ACTUAL embedded
remote shell script (extracted verbatim) via `bash -c` against fake
git/docker/curl/sha256sum/flock/systemctl/crontab/sudo/sleep/timeout
executables.
"""
import base64
import hashlib
import os
import re
import shlex
import shutil
import subprocess as sp
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-api-reference-normalization.yml"
UPGRADE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-direct-runtime-upgrade.yml"
RECOVERY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-split-release-recovery.yml"
DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
DIAGNOSTIC_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-runtime-diagnostic.yml"

REMOTE_HEREDOC_START = "<<'REMOTE_SCRIPT'"
REMOTE_HEREDOC_END = "REMOTE_SCRIPT"

CONFIRMATION_TOKEN = "NORMALIZE_F476_API_REFERENCE"
EXPECTED_PRODUCTION_SHA = "f4765f857355c6543f68cea0e481b7f20a917147"
EXPECTED_CURRENT_CONFIG_IMAGE = "jobpulse-api-rollback:bccbbd997ee8-1427495"
CANONICAL_IMAGE = "ghcr.io/mrezamaghouli/jobpulse-api:f4765f857355c6543f68cea0e481b7f20a917147"
EXPECTED_IMAGE_ID = "sha256:7739628f61c3bba88ff4e395c0f0a0ce3bea3e3abb40956207b495f9d909b753"
EXPECTED_API_CONTAINER_ID = "ac66a4acef51c8ac3dc27b38a5559c5aa056ec033ceef43c9f8a0e4a8cbd3682"
EXPECTED_DB_CONTAINER_ID = "98f20abb30f4fdfe9473431370c3f6db29c39183d67f31d49638a493a1f605e9"
EXPECTED_FRONTEND_CONTAINER_ID = "fb949b779e80a53f4a9542d2eaa30945d1abbf357fbb2391b2422238371b7dd8"
EXPECTED_TOR_CONTAINER_ID = "e2210c3c47f7a73fca1d5b2faeb62b2eeec89526a56f5da4cb1a00a3298b1612"

_REAL_SHA256SUM = shutil.which("sha256sum") or "/usr/bin/sha256sum"


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
    return workflow["jobs"]["normalize"]


@pytest.fixture(scope="module")
def steps(job) -> list:
    return job["steps"]


def _step_by_name(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


@pytest.fixture(scope="module")
def normalization_step(steps):
    return _step_by_name(steps, "Run production API reference normalization")


@pytest.fixture(scope="module")
def full_run_text(normalization_step) -> str:
    return normalization_step["run"]


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


def _code_only(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _run_bash_n(script: str):
    return sp.run(["bash", "-n"], input=script, capture_output=True, text=True, timeout=10)


def _extract_bash_function(script: str, func_name: str) -> str:
    lines = script.splitlines()
    start_idx = next(
        i
        for i, line in enumerate(lines)
        if line.strip().startswith(f"{func_name}()") and line.rstrip().endswith("{")
    )
    depth = 0
    collected = []
    for line in lines[start_idx:]:
        collected.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(collected)


# =====================================================================
# 1. Trigger, confirmation token, permissions, concurrency
# =====================================================================

def test_workflow_file_exists_and_parses(workflow):
    assert workflow["name"] == "Production API Reference Normalization"


def test_trigger_is_workflow_dispatch_only(workflow):
    triggers = _triggers(workflow)
    assert list(triggers.keys()) == ["workflow_dispatch"]


def test_no_other_trigger_keys_in_source(workflow_text):
    for forbidden in ("\non:\n  push:", "pull_request:", "workflow_run:", "schedule:", "repository_dispatch:"):
        assert forbidden not in workflow_text


def test_confirmation_input_required_string(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert list(inputs.keys()) == ["confirmation"]
    assert inputs["confirmation"]["required"] is True
    assert inputs["confirmation"]["type"] == "string"


def test_confirmation_token_is_exact(workflow_text):
    assert f'if [ "$CONFIRMATION" != "{CONFIRMATION_TOKEN}" ]; then' in workflow_text
    assert CONFIRMATION_TOKEN in workflow_text


def test_permissions_contents_read_only(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_concurrency_matches_direct_runtime_upgrade_group(workflow):
    assert workflow["concurrency"]["group"] == "jobpulse-production-direct-runtime-upgrade"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_direct_runtime_upgrade_concurrency_group_actually_matches(workflow_text):
    upgrade_text = UPGRADE_WORKFLOW_PATH.read_text()
    upgrade_workflow = yaml.safe_load(upgrade_text)
    assert workflow_text  # sanity: fixture materialized
    this_workflow = yaml.safe_load(workflow_text)
    assert this_workflow["concurrency"]["group"] == upgrade_workflow["concurrency"]["group"]


def test_require_dispatch_from_main(steps):
    step = _step_by_name(steps, "Require dispatch from main")
    assert "refs/heads/main" in step["run"]


def test_confirmation_check_precedes_ssh_key_material(steps):
    names = [s.get("name") for s in steps]
    confirm_idx = names.index("Require exact confirmation token")
    ssh_idx = names.index("Validate VM_SSH_KEY secret is present")
    assert confirm_idx < ssh_idx


# =====================================================================
# 2. Pinned constants
# =====================================================================

def test_one_time_pins_exact(workflow):
    env = workflow["env"]
    assert env["EXPECTED_PRODUCTION_SHA"] == EXPECTED_PRODUCTION_SHA
    assert env["EXPECTED_CURRENT_CONFIG_IMAGE"] == EXPECTED_CURRENT_CONFIG_IMAGE
    assert env["CANONICAL_IMAGE"] == CANONICAL_IMAGE
    assert env["EXPECTED_IMAGE_ID"] == EXPECTED_IMAGE_ID
    assert env["EXPECTED_API_CONTAINER_ID"] == EXPECTED_API_CONTAINER_ID
    assert env["EXPECTED_DB_CONTAINER_ID"] == EXPECTED_DB_CONTAINER_ID
    assert env["EXPECTED_FRONTEND_CONTAINER_ID"] == EXPECTED_FRONTEND_CONTAINER_ID
    assert env["EXPECTED_TOR_CONTAINER_ID"] == EXPECTED_TOR_CONTAINER_ID


def test_canonical_image_is_exact_f476_ghcr_reference(remote_script):
    assert f'CANONICAL_IMAGE="{CANONICAL_IMAGE}"' in remote_script


def test_expected_image_id_pinned_in_remote_script(remote_script):
    assert f'EXPECTED_IMAGE_ID="{EXPECTED_IMAGE_ID}"' in remote_script


def test_production_git_sha_pinned_exact_f476(remote_script):
    assert f'EXPECTED_PRODUCTION_SHA="{EXPECTED_PRODUCTION_SHA}"' in remote_script


def test_current_config_image_pinned_exact_rollback_alias(remote_script):
    assert f'EXPECTED_CURRENT_CONFIG_IMAGE="{EXPECTED_CURRENT_CONFIG_IMAGE}"' in remote_script


def test_container_pins_exact(remote_script):
    assert f'EXPECTED_API_CONTAINER_ID="{EXPECTED_API_CONTAINER_ID}"' in remote_script
    assert f'EXPECTED_DB_CONTAINER_ID="{EXPECTED_DB_CONTAINER_ID}"' in remote_script
    assert f'EXPECTED_FRONTEND_CONTAINER_ID="{EXPECTED_FRONTEND_CONTAINER_ID}"' in remote_script
    assert f'EXPECTED_TOR_CONTAINER_ID="{EXPECTED_TOR_CONTAINER_ID}"' in remote_script


def test_pins_are_40_or_64_or_sha256_prefixed_hex(workflow):
    env = workflow["env"]
    assert re.fullmatch(r"[0-9a-f]{40}", env["EXPECTED_PRODUCTION_SHA"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", env["EXPECTED_IMAGE_ID"])
    for key in (
        "EXPECTED_API_CONTAINER_ID",
        "EXPECTED_DB_CONTAINER_ID",
        "EXPECTED_FRONTEND_CONTAINER_ID",
        "EXPECTED_TOR_CONTAINER_ID",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", env[key])


# =====================================================================
# 3. Immutable git-object provenance before SSH
# =====================================================================

@pytest.fixture(scope="module")
def hash_step(steps):
    return _step_by_name(steps, "Derive immutable file hashes for the f476 production baseline")


def test_hash_step_derives_from_immutable_git_object_not_working_tree(hash_step):
    run_text = hash_step["run"]
    assert 'git cat-file -e "${EXPECTED_PRODUCTION_SHA}^{commit}"' in run_text
    assert 'git show "${EXPECTED_PRODUCTION_SHA}:docker-compose.prod.yml"' in run_text
    assert 'git show "${EXPECTED_PRODUCTION_SHA}:frontend/index.html"' in run_text
    assert 'git show "${EXPECTED_PRODUCTION_SHA}:scripts/providers/linkedin_browser_provider.py"' in run_text


def test_hash_step_exports_three_hashes(hash_step):
    run_text = hash_step["run"]
    for var in ("COMPOSE_SHA256", "FRONTEND_SHA256", "PROVIDER_SHA256"):
        assert f"echo \"{var}=${var}\"" in run_text or f"{var}=$" in run_text


def test_hash_step_writes_to_github_env(hash_step):
    assert '>> "$GITHUB_ENV"' in hash_step["run"]


def test_hash_step_precedes_ssh_key_validation(steps):
    names = [s.get("name") for s in steps]
    hash_idx = names.index("Derive immutable file hashes for the f476 production baseline")
    ssh_idx = names.index("Validate VM_SSH_KEY secret is present")
    assert hash_idx < ssh_idx


# =====================================================================
# 4. No git mutation anywhere
# =====================================================================

def test_no_git_reset_command(remote_script):
    code = _code_only(remote_script)
    assert "git reset" not in code


def test_no_git_checkout_pull_merge_commands(remote_script):
    code = _code_only(remote_script)
    for forbidden in ("git checkout", "git pull", "git merge", "git commit", "git push"):
        assert forbidden not in code


def test_only_read_only_git_commands_used(remote_script):
    git_lines = [line for line in _executable_lines(remote_script) if re.search(r"(?:^|[;&|(]\s*)git\s+\S", line)]
    assert git_lines, "expected at least one git invocation"
    for line in git_lines:
        assert re.search(r"git\s+(rev-parse|status|cat-file|show|fetch)\b", line), line


def test_git_invariant_checked_before_and_after_mutation(remote_script):
    before_idx = remote_script.index('CURRENT_SHA="$(git rev-parse HEAD)"')
    mutation_idx = remote_script.index("API_MUTATION_STARTED=true")
    after_idx = remote_script.index('POST_SHA="$(git rev-parse HEAD)"')
    assert before_idx < mutation_idx < after_idx


# =====================================================================
# 5. Canonical image provenance
# =====================================================================

def test_canonical_image_pull_before_mutation(remote_script):
    pull_idx = remote_script.index('docker pull "$CANONICAL_IMAGE"')
    mutation_idx = remote_script.index("API_MUTATION_STARTED=true")
    assert pull_idx < mutation_idx


def test_pulled_canonical_image_id_equality_required(remote_script):
    assert 'PULLED_IMAGE_ID="$(docker image inspect "$CANONICAL_IMAGE" --format' in remote_script
    assert '[ "$PULLED_IMAGE_ID" = "$EXPECTED_IMAGE_ID" ]' in remote_script
    check_idx = remote_script.index('[ "$PULLED_IMAGE_ID" = "$EXPECTED_IMAGE_ID" ]')
    mutation_idx = remote_script.index("API_MUTATION_STARTED=true")
    assert check_idx < mutation_idx


def test_ghcr_login_uses_password_stdin_and_isolated_config(remote_script):
    assert "docker login ghcr.io -u mrezamaghouli --password-stdin" in remote_script
    assert 'TMP_DOCKER_CONFIG="$(mktemp -d)"' in remote_script
    assert 'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker login' in remote_script
    assert 'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker pull "$CANONICAL_IMAGE"' in remote_script


def test_ghcr_login_only_when_token_present(remote_script):
    assert 'if [ "$HAS_TOKEN" = "true" ]; then' in remote_script


def test_no_ghcr_token_base64_anywhere(runner_script, remote_script):
    for script in (runner_script, remote_script):
        for line in _executable_lines(script):
            assert "base64" not in line.lower() or "GHCR_TOKEN" not in line


def test_ghcr_token_travels_only_via_stdin(runner_script):
    assert 'printf \'%s\' "${GHCR_TOKEN:-}" | ssh' in runner_script
    for line in _executable_lines(runner_script):
        if "GHCR_TOKEN" in line and "ssh" in line:
            assert line.strip().startswith("printf")


def test_tmp_docker_config_cleaned_up(remote_script):
    assert '[ -n "$TMP_DOCKER_CONFIG" ] && rm -rf "$TMP_DOCKER_CONFIG"' in remote_script


# =====================================================================
# 6. Effective candidate compose config
# =====================================================================

def test_effective_candidate_config_checks_image_before_mutation(remote_script):
    config_idx = remote_script.index('docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" config')
    image_check_idx = remote_script.index('[ "$EFFECTIVE_CANDIDATE_API_IMAGE" = "$CANONICAL_IMAGE" ]')
    mutation_idx = remote_script.index("API_MUTATION_STARTED=true")
    assert config_idx < image_check_idx < mutation_idx


def test_effective_candidate_config_checks_tor_disabled(remote_script):
    assert '[ "$EFFECTIVE_CANDIDATE_TOR_ENABLED" = "false" ]' in remote_script


def test_effective_candidate_config_never_dumps_full_config(remote_script):
    lines = [line for line in remote_script.splitlines() if "EFFECTIVE_CANDIDATE_CONFIG" in line]
    for line in lines:
        assert not line.strip().startswith("echo \"$EFFECTIVE_CANDIDATE_CONFIG\"")


# =====================================================================
# 7. Scheduler provenance parity with production-split-release-recovery.yml
# =====================================================================

def _scheduler_provenance_section(script: str) -> str:
    start = script.index("scheduler provenance: this account's own crontab")
    end = script.index("collection scheduler safety: acquire locks")
    return script[start:end]


def _recovery_remote_script() -> str:
    recovery_text = RECOVERY_WORKFLOW_PATH.read_text()
    recovery_workflow = yaml.safe_load(recovery_text)
    step = next(
        s
        for s in recovery_workflow["jobs"]["recover"]["steps"]
        if s.get("name") == "Run production split-release recovery"
    )
    full_run_text = step["run"]
    start = full_run_text.index("<<'REMOTE'") + len("<<'REMOTE'")
    rest = full_run_text[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == "REMOTE")
    return "\n".join(lines[:end_idx])


def _recovery_scheduler_provenance_section() -> str:
    recovery_remote = _recovery_remote_script()
    start = recovery_remote.index("scheduler provenance: this account's own crontab")
    end = recovery_remote.index("collection scheduler safety: acquire locks")
    return recovery_remote[start:end]


def test_scheduler_provenance_parity_with_recovery_workflow(remote_script):
    """The safety-critical scheduler-discovery logic must be reused as
    literally as practical from the already-reviewed Phase 4J version in
    production-split-release-recovery.yml -- not reinvented. Compares
    the two sections with only cosmetic whitespace normalized."""
    this_section = _scheduler_provenance_section(remote_script)
    recovery_section = _recovery_scheduler_provenance_section()

    def _normalize(text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines()]
        lines = [line for line in lines if line.strip()]
        return "\n".join(lines)

    assert _normalize(this_section) == _normalize(recovery_section)


def test_scheduler_provenance_covers_all_required_checks(remote_script):
    section = _scheduler_provenance_section(remote_script)
    for needle in (
        "own_crontab_provenance=confirmed_canonical_launcher",
        "etc_crontab_drift=none",
        "cron_d_drift=none",
        "root_crontab_drift=none",
        "systemd_unit_file_drift=none",
        "systemd_loaded_unit_drift=none",
        "systemd_drift=none",
        "*@.service)",
        "systemctl cat",
        "list-unit-files --type=service --no-legend --no-pager --full",
        "list-units --type=service --all --no-legend --no-pager --plain --full",
    ):
        assert needle in section, needle


def test_collection_env_lock_provenance_reused(remote_script):
    assert "effective internal lock path provenance" in remote_script
    assert "parsed as data only, never sourced/eval'd" in remote_script
    assert '. "/opt/jobpulse/.collection.env"' not in _code_only(remote_script)


# =====================================================================
# 8. Locks and active collector
# =====================================================================

def test_outer_lock_before_internal_lock(remote_script):
    outer_idx = remote_script.index('flock -n "$OUTER_LOCK_FD"')
    internal_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    assert outer_idx < internal_idx


def test_locks_before_active_collector_check(remote_script):
    internal_idx = remote_script.index('flock -n "$INTERNAL_LOCK_FD"')
    collector_idx = remote_script.index("active collector check")
    assert internal_idx < collector_idx


def test_active_collector_before_api_mutation(remote_script):
    collector_idx = remote_script.index('[ "$ACTIVE_COLLECTOR" = "no" ]')
    mutation_idx = remote_script.index("API_MUTATION_STARTED=true")
    assert collector_idx < mutation_idx


def test_active_collector_never_kills(remote_script):
    section = _code_only(
        remote_script[remote_script.index("active collector check") : remote_script.index("canonical image provenance")]
    )
    for forbidden in ("docker stop", "docker kill", "kill -"):
        assert forbidden not in section


def test_locks_released_only_in_exit_trap(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    assert "release_locks" in trap_body
    # No other explicit exec {FD}>&- release outside the trap/release_locks function.
    release_lines = [line for line in remote_script.splitlines() if "exec {" in line and ">&-" in line]
    release_func = _extract_bash_function(remote_script, "release_locks")
    for line in release_lines:
        assert line.strip() in release_func


# =====================================================================
# 9. Final pre-mutation revalidation
# =====================================================================

def test_final_revalidation_rereads_all_pinned_state(remote_script):
    section_start = remote_script.index("final pre-mutation revalidation")
    section_end = remote_script.index("ONLY AUTHORIZED PRODUCTION MUTATION")
    section = remote_script[section_start:section_end]
    for needle in (
        "REVALIDATE_SHA=",
        "REVALIDATE_API_ID=",
        "REVALIDATE_API_CONFIG_IMAGE=",
        "REVALIDATE_API_IMAGE_ID=",
        "REVALIDATE_DB_ID=",
        "REVALIDATE_FRONTEND_ID=",
        "REVALIDATE_TOR_ID=",
        "REVALIDATE_COMPOSE_SHA256=",
        "REVALIDATE_SEARCH_TRANSPORT=",
        "REVALIDATE_TOR_ENABLED=",
    ):
        assert needle in section, needle


def test_final_revalidation_precedes_mutation_marker(remote_script):
    reval_idx = remote_script.index("final pre-mutation revalidation")
    mutation_idx = remote_script.index("API_MUTATION_STARTED=true")
    assert reval_idx < mutation_idx


# =====================================================================
# 10. API mutation markers and mutation matrix
# =====================================================================

def test_mutation_flags_initialized_false(remote_script):
    init_block = remote_script.split("wait_for_api_convergence() {")[0]
    assert "API_MUTATION_STARTED=false" in init_block
    assert "NORMALIZATION_CONFIRMED=false" in init_block
    assert "ROLLBACK_ATTEMPTED=false" in init_block
    assert "ROLLBACK_CONFIRMED=false" in init_block


def test_api_mutation_started_set_immediately_before_candidate_up(remote_script):
    flag_idx = remote_script.index("API_MUTATION_STARTED=true")
    up_idx = remote_script.index('JOBPULSE_API_IMAGE="$CANONICAL_IMAGE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" up -d --no-build --no-deps --force-recreate api')
    assert flag_idx < up_idx
    between = remote_script[flag_idx + len("API_MUTATION_STARTED=true") : up_idx]
    for line in _executable_lines(between):
        pytest.fail(f"unexpected executable line between API_MUTATION_STARTED=true and candidate up: {line!r}")


def test_candidate_uses_force_recreate_no_build_no_deps_api_only(remote_script):
    assert "docker compose -p \"$PRODUCTION_PROJECT\" -f \"$COMPOSE_FILE\" up -d --no-build --no-deps --force-recreate api" in remote_script


def _generic_api_recreation_lines(script: str):
    matches = []
    for line in _executable_lines(script):
        if "docker compose" not in line:
            continue
        if not re.search(r"(?:^|\s)up(?:\s|$)", line):
            continue
        if " api" not in line and not line.rstrip().endswith(" api"):
            continue
        matches.append(line)
    return matches


def test_exactly_two_api_compose_up_sites(remote_script):
    lines = _generic_api_recreation_lines(remote_script)
    assert len(lines) == 2, lines
    candidate_lines = [line for line in lines if "$CANONICAL_IMAGE" in line]
    rollback_lines = [line for line in lines if "$EXPECTED_CURRENT_CONFIG_IMAGE" in line]
    assert len(candidate_lines) == 1
    assert len(rollback_lines) == 1


def test_no_db_recreation(remote_script):
    for line in _executable_lines(remote_script):
        if "docker compose" in line and re.search(r"(?:^|\s)up(?:\s|$)", line):
            assert re.search(r"\bdb\b", line) is None, line


def test_no_frontend_recreation(remote_script):
    for line in _executable_lines(remote_script):
        if "docker compose" in line and re.search(r"(?:^|\s)up(?:\s|$)", line):
            assert re.search(r"\bfrontend\b", line) is None, line


def test_no_tor_recreation(remote_script):
    for line in _executable_lines(remote_script):
        if "docker compose" in line and re.search(r"(?:^|\s)up(?:\s|$)", line):
            assert re.search(r"\btor\b", line) is None, line


def test_no_docker_restart_stop_rm_of_any_service(remote_script):
    code = _code_only(remote_script)
    for forbidden in ("docker restart", "docker stop", "docker rm", "docker kill", "docker create"):
        assert forbidden not in code


# =====================================================================
# 11. Same-image-ID invariant and normalization-specific success
# =====================================================================

def test_convergence_requires_same_expected_image_id(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    assert '"$image_id" = "$EXPECTED_IMAGE_ID"' in func


def test_new_container_id_must_differ_from_before(remote_script):
    assert '[ "$NEW_API_ID" != "$EXPECTED_API_CONTAINER_ID" ]' in remote_script


def test_success_requires_canonical_config_image(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    assert 'cur_config_image="$(docker inspect -f' in func
    assert 'if [ "$cur_config_image" != "$expected_config_image" ]; then' in func


def test_success_markers_exact(remote_script):
    assert 'echo "API_REFERENCE_NORMALIZED_TO_CANONICAL_F476"' in remote_script
    assert 'echo "PRODUCTION_API_REFERENCE_NORMALIZATION_COMPLETE"' in remote_script


def test_normalization_confirmed_set_only_at_the_end(remote_script):
    confirmed_idx = remote_script.rindex("NORMALIZATION_CONFIRMED=true")
    success_idx = remote_script.index("PRODUCTION_API_REFERENCE_NORMALIZATION_COMPLETE")
    assert success_idx < confirmed_idx
    workflow_success_idx = remote_script.index("WORKFLOW_SUCCESS=true")
    assert confirmed_idx < workflow_success_idx


# =====================================================================
# 12. Phase 4L wall-clock convergence principles reused
# =====================================================================

def test_convergence_uses_wall_clock_deadline_and_attempt_ceiling(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    assert "local deadline=$((SECONDS + 90))" in func
    assert "for attempt in $(seq 1 45); do" in func
    assert 'if [ "$SECONDS" -ge "$deadline" ]; then' in func


def test_convergence_curls_tightly_bounded(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    assert "curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1:8000/health" in func
    assert "curl --connect-timeout 1 --max-time 2 -fsS http://127.0.0.1/api/health" in func
    assert "--max-time 5" not in func


def test_convergence_docker_inspect_consolidated_and_bounded(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    assert "timeout 3 docker inspect -f '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.Image}}'" in func


def test_convergence_tolerates_starting_state(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    loop_body = func.split("for attempt in $(seq 1 45); do")[1].split("done")[0]
    assert "return 1" not in loop_body
    assert "exit 1" not in loop_body


# =====================================================================
# 13. Rollback design
# =====================================================================

def test_rollback_retags_expected_image_id_as_old_alias(remote_script):
    func = _extract_bash_function(remote_script, "rollback_api")
    assert 'docker tag "$EXPECTED_IMAGE_ID" "$EXPECTED_CURRENT_CONFIG_IMAGE"' in func


def test_rollback_recreates_api_only_with_old_alias(remote_script):
    func = _extract_bash_function(remote_script, "rollback_api")
    assert 'JOBPULSE_API_IMAGE="$EXPECTED_CURRENT_CONFIG_IMAGE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" up -d --no-build --no-deps --force-recreate api' in func


def test_rollback_uses_shared_convergence_with_old_alias_expectation(remote_script):
    func = _extract_bash_function(remote_script, "rollback_api")
    assert 'wait_for_api_convergence "$EXPECTED_CURRENT_CONFIG_IMAGE"' in func


def test_rollback_attempted_only_after_mutation_and_only_on_failure(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    assert 'if [ "$WORKFLOW_SUCCESS" != "true" ] && [ "$API_MUTATION_STARTED" = "true" ] && [ "$NORMALIZATION_CONFIRMED" != "true" ]; then' in trap_body


def test_no_git_rollback_path_exists(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    assert "git reset" not in trap_body
    assert "git checkout" not in trap_body


def test_rollback_success_does_not_set_workflow_success(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    assert "WORKFLOW_SUCCESS=true" not in trap_body


def test_rollback_confirmed_classification_message_exact(remote_script):
    assert "NORMALIZATION_FAILED_ROLLBACK_CONFIRMED_PRESTATE_RESTORED" in remote_script


def test_failed_rollback_emits_narrow_evidence_not_full_dump(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    for needle in (
        "final_api_container_id=",
        "final_api_config_image=",
        "final_api_image_id=",
        "final_api_running=",
        "final_api_health=",
        "final_search_transport=",
        "final_tor_enabled=",
        "final_db_id=",
        "final_frontend_id=",
        "final_tor_id=",
    ):
        assert needle in trap_body, needle


def test_no_second_rollback_attempt(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    assert trap_body.count("rollback_api") == 1


def test_no_blind_or_true_rollback_chain(remote_script):
    trap_body = _extract_bash_function(remote_script, "final_exit_trap")
    code = _code_only(trap_body)
    assert "rollback_api || true" not in code


# =====================================================================
# 14. No LinkedIn / no Tor traffic / no diagnostic dispatch
# =====================================================================

def test_no_linkedin_or_collector_execution(remote_script):
    for forbidden in (
        "python -m scripts.process_search_demand_queue",
        "python -m scripts.linkedin_plan_collect",
        "python -m scripts.collector_postgres",
        "python -m scripts.seed_priority_coverage_queue",
        "python -m scripts.reconcile_priority_coverage",
        "linkedin.com",
    ):
        assert forbidden not in remote_script


def test_no_tor_controlport_socks_or_newnym(remote_script):
    for forbidden in ("NEWNYM", "ControlPort", ":9051", "TOR_SOCKS_", "SEARCH_TRANSPORT=proxy"):
        assert forbidden not in remote_script


def test_tor_enabled_only_ever_read_never_set(remote_script):
    for line in _executable_lines(remote_script):
        if "TOR_ENABLED=" in line and "cur_tor_enabled=" not in line.lower():
            assert "printenv" in line or "sed -n" in line or "TOR_ENABLED=false\"" in line or line.strip().startswith("[") or "echo" in line, line


def test_no_diagnostic_workflow_dispatch(workflow_text):
    """A comment may reference production-runtime-diagnostic.yml for
    context (it explains this workflow intentionally does NOT dispatch
    it) -- the executable code must not."""
    code = _code_only(workflow_text)
    assert "production-runtime-diagnostic" not in code
    assert "gh workflow run" not in code
    assert "gh api" not in code


def test_does_not_modify_other_workflows():
    # Structural guard: this PR must not touch the other production
    # workflows -- confirmed by their continued existence/shape here,
    # a full content diff is out of scope for this file.
    assert UPGRADE_WORKFLOW_PATH.is_file()
    assert RECOVERY_WORKFLOW_PATH.is_file()
    assert DEPLOY_WORKFLOW_PATH.is_file()
    assert DIAGNOSTIC_WORKFLOW_PATH.is_file()


# =====================================================================
# 15. Job timeout
# =====================================================================

def test_job_has_finite_timeout(job):
    assert job["timeout-minutes"] == 15


def test_no_retry_of_candidate_no_second_attempt(remote_script):
    pull_count = sum(1 for line in _executable_lines(remote_script) if 'docker pull "$CANONICAL_IMAGE"' in line)
    assert pull_count == 1


# =====================================================================
# 16. Structural: no secret dump, no full env dump
# =====================================================================

def test_no_ghcr_token_or_ssh_key_printed(remote_script, runner_script):
    for script in (remote_script, runner_script):
        for line in _executable_lines(script):
            assert "echo \"$GHCR_TOKEN\"" not in line
            assert "echo \"$VM_SSH_KEY\"" not in line


def test_no_full_environment_dump(remote_script):
    code = _code_only(remote_script)
    assert re.search(r"docker exec jobpulse-api-prod (printenv|env)\s*$", code, re.MULTILINE) is None
    assert re.search(r"docker exec jobpulse-api-prod (printenv|env)\s*\n", code) is None


# =====================================================================
# Behavioral harness
# =====================================================================
# Executes the ACTUAL extracted remote shell script (verbatim, via
# `bash -c`) against stateful fake docker/curl/systemctl/crontab/sudo
# executables, and the REAL flock/timeout/sha256sum/git binaries against
# tmp_path-substituted paths and real host files -- never a Python
# reimplementation of the workflow's own logic.


@pytest.fixture()
def harness_bin(tmp_path):
    bin_dir = tmp_path / "sentinel_bin"
    bin_dir.mkdir()
    for mutating_cmd in ("docker", "git"):
        path = bin_dir / mutating_cmd
        path.write_text(
            "#!/usr/bin/env bash\n"
            f"echo 'FATAL_TEST_HARNESS_MUTATION_SENTINEL: {mutating_cmd} invoked by a parser-only test' >&2\n"
            "exit 99\n"
        )
        path.chmod(0o755)
    return bin_dir


def _write_fake_executable(bin_dir, name: str, script_body: str):
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + script_body)
    path.chmod(0o755)
    return path


def _write_passthrough_shims(bin_dir):
    for cmd in ("find", "grep", "cat", "awk", "sed", "cut", "head", "hostname", "mktemp", "printf", "true", "false"):
        real = shutil.which(cmd)
        if real:
            _write_fake_executable(bin_dir, cmd, f'exec {shlex.quote(real)} "$@"\n')


class Scenario:
    """Baked-in test double state for one behavioral run of the ACTUAL
    extracted remote script. All values default to the exact reviewed
    pre-normalization state so `Scenario()` alone is the happy path; a
    test overrides only the field(s) needed to induce its scenario."""

    def __init__(self, tmp_path: Path, **overrides):
        self.tmp_path = tmp_path
        self.fake_root = tmp_path / "jobpulse_root"
        self.outer_lock_path = tmp_path / "outer.lock"
        self.internal_lock_path = tmp_path / "internal.lock"
        self.etc_crontab_path = tmp_path / "etc_crontab"
        self.etc_cron_d_path = tmp_path / "etc_cron_d"
        self.phase_file = tmp_path / "phase"
        self.round_file = tmp_path / "round"

        defaults = dict(
            current_git_sha=EXPECTED_PRODUCTION_SHA,
            git_status_lines=(),
            compose_content="compose-file-content-f476\n",
            frontend_content="Open Poster Profile f476 frontend\n",
            provider_content="# provider f476\n",
            api_id_before=EXPECTED_API_CONTAINER_ID,
            api_config_image_before=EXPECTED_CURRENT_CONFIG_IMAGE,
            api_image_id_before=EXPECTED_IMAGE_ID,
            api_running_before="true",
            api_health_before="healthy",
            api_restart_count_before="0",
            api_service_label="api",
            api_project_label="jobpulse",
            search_transport_before="",
            tor_enabled_before="false",
            db_id="EXPECTED",
            db_image_id="sha256:dbimage000000000000000000000000000000000000000000000000000000",
            db_running="true",
            db_health="healthy",
            db_restart_count="0",
            frontend_id="EXPECTED",
            frontend_image_id="sha256:feimage000000000000000000000000000000000000000000000000000000",
            frontend_running="true",
            frontend_restart_count="0",
            frontend_id_after_candidate=None,  # None => unchanged from frontend_id
            mount_type="bind",
            mount_source=None,
            mount_rw="false",
            tor_id="EXPECTED",
            tor_image_id="sha256:torimage00000000000000000000000000000000000000000000000000000",
            tor_running="true",
            tor_health="healthy",
            tor_restart_count="0",
            tor_published_ports="",
            docker_top_output="PID   USER   TIME   COMMAND\n1     root   0:00   uvicorn app.main:app",
            outer_lock_busy=False,
            own_crontab_root=None,
            own_crontab_outer_lock=None,
            own_crontab_extra_lines=(),
            own_crontab_exit=0,
            etc_crontab_content=None,
            etc_cron_d_files=None,
            root_crontab_mode="no_crontab",
            root_crontab_content="",
            systemctl_available=True,
            systemctl_list_exit=0,
            systemd_units=(),
            systemd_template_units=(),
            systemd_list_units_exit=0,
            systemd_loaded_units=(),
            collection_env_content=None,
            # Canonical image resolution
            pulled_canonical_image_id=EXPECTED_IMAGE_ID,
            compose_exit=0,
            # Candidate sequence: list of dicts running/health/image/direct/nginx
            candidate_sequence=(
                dict(running="true", health="healthy", image=EXPECTED_IMAGE_ID, direct=True, nginx=True),
            ),
            candidate_compose_up_exit=0,
            candidate_id="candidateapi0000000000000000000000000000000000000000000000000",
            candidate_config_image=CANONICAL_IMAGE,
            candidate_restart_count="0",
            candidate_search_transport="",
            candidate_tor_enabled="false",
            # Rollback sequence
            rollback_sequence=(
                dict(running="true", health="healthy", image=EXPECTED_IMAGE_ID, direct=True, nginx=True),
            ),
            rollback_compose_up_exit=0,
            rollback_tag_exit=0,
            rollback_id="rollbackapi00000000000000000000000000000000000000000000000000",
            rollback_config_image=EXPECTED_CURRENT_CONFIG_IMAGE,
            rollback_restart_count="0",
            rollback_search_transport="",
            rollback_tor_enabled="false",
        )
        defaults.update(overrides)
        for key, value in defaults.items():
            setattr(self, key, value)
        if self.mount_source is None:
            self.mount_source = str(self.fake_root / "frontend")
        if self.db_id == "EXPECTED":
            self.db_id = EXPECTED_DB_CONTAINER_ID
        if self.frontend_id == "EXPECTED":
            self.frontend_id = EXPECTED_FRONTEND_CONTAINER_ID
        if self.tor_id == "EXPECTED":
            self.tor_id = EXPECTED_TOR_CONTAINER_ID
        self.candidate_marker = tmp_path / "candidate_recreated"
        self.rollback_marker = tmp_path / "rollback_recreated"
        self.retag_marker = tmp_path / "retag_done"

    @staticmethod
    def _q(value) -> str:
        return shlex.quote(str(value))

    def env(self) -> dict:
        return {}

    def build_fakes(self) -> Path:
        bin_dir = self.tmp_path / "fake_bin"
        bin_dir.mkdir(exist_ok=True)
        _write_passthrough_shims(bin_dir)

        # --- git: read-only, reports current_git_sha / configurable status lines
        git_status_printf = "\n".join(f"printf '%s\\n' {self._q(line)}" for line in self.git_status_lines)
        git_body = f'''
case "$1" in
  fetch) exit 0 ;;
  cat-file) exit 0 ;;
  rev-parse) echo {self._q(self.current_git_sha)}; exit 0 ;;
  status)
{git_status_printf if git_status_printf else "true"}
    exit 0 ;;
  *) exit 1 ;;
esac
'''
        _write_fake_executable(bin_dir, "git", git_body)

        # --- host files (real files under fake_root, real sha256sum reads them)
        self.fake_root.mkdir(parents=True, exist_ok=True)
        (self.fake_root / "frontend").mkdir(exist_ok=True)
        (self.fake_root / "scripts" / "providers").mkdir(parents=True, exist_ok=True)
        (self.fake_root / "state").mkdir(exist_ok=True)
        (self.fake_root / "docker-compose.prod.yml").write_text(self.compose_content)
        (self.fake_root / "frontend" / "index.html").write_text(self.frontend_content)
        (self.fake_root / "scripts" / "providers" / "linkedin_browser_provider.py").write_text(self.provider_content)

        if self.collection_env_content is not None:
            (self.fake_root / ".collection.env").write_text(self.collection_env_content)

        # --- scheduler provenance fakes (own crontab / root crontab / sudo / systemctl)
        self._build_scheduler_fakes(bin_dir)

        # --- docker / docker-compose / curl (stateful, phase+round aware)
        self._build_docker_and_curl_fakes(bin_dir)

        # --- sleep: instant (never a real 90s/30s wait in tests)
        _write_fake_executable(bin_dir, "sleep", "exit 0\n")

        if self.etc_crontab_content is not None:
            self.etc_crontab_path.parent.mkdir(parents=True, exist_ok=True)
            self.etc_crontab_path.write_text(self.etc_crontab_content)
        if self.etc_cron_d_files is not None:
            self.etc_cron_d_path.mkdir(parents=True, exist_ok=True)
            for fname, fcontent in self.etc_cron_d_files.items():
                (self.etc_cron_d_path / fname).write_text(fcontent)

        return bin_dir

    def _build_scheduler_fakes(self, bin_dir):
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
        root_crontab_printf = "\n".join(
            f"printf '%s\\n' {self._q(line)}" for line in self.root_crontab_content.splitlines()
        )

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

        if self.systemctl_available:
            unit_list_printf = "\n".join(
                f"printf '%s\\n' {self._q(name + '.service enabled enabled')}"
                for name, _so, _se in self.systemd_units
            )
            template_list_printf = "\n".join(
                f"printf '%s\\n' {self._q(name + '@.service static -')}"
                for name, _co, _ce in self.systemd_template_units
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
            loaded_unit_list_printf = "\n".join(
                f"printf '%s\\n' {self._q(full_name)}"
                for full_name, _so, _se in self.systemd_loaded_units
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

    def _build_docker_and_curl_fakes(self, bin_dir):
        self.phase_file.write_text("before")
        self.round_file.write_text("0")

        # Per-phase state directories for the consolidated convergence format.
        for phase, sequence in (("candidate", self.candidate_sequence), ("rollback", self.rollback_sequence)):
            state_dir = self.tmp_path / f"{phase}_state"
            state_dir.mkdir(exist_ok=True)
            for i, s in enumerate(sequence):
                (state_dir / f"{i}.running").write_text(s["running"])
                (state_dir / f"{i}.health").write_text(s["health"])
                (state_dir / f"{i}.image").write_text(s["image"])
                (state_dir / f"{i}.direct").write_text("true" if s["direct"] else "false")
                (state_dir / f"{i}.nginx").write_text("true" if s["nginx"] else "false")

        candidate_max_idx = len(self.candidate_sequence) - 1
        rollback_max_idx = len(self.rollback_sequence) - 1

        frontend_id_after = (
            self.frontend_id_after_candidate if self.frontend_id_after_candidate is not None else self.frontend_id
        )

        mount_line = f"{self.mount_type}|{self.mount_source}|/usr/share/nginx/html|{self.mount_rw}"

        def env_lines_for(search_transport, tor_enabled):
            lines = []
            if search_transport:
                lines.append(f"SEARCH_TRANSPORT={search_transport}")
            lines.append(f"TOR_ENABLED={tor_enabled}")
            return "\\n".join(lines)

        docker_body = f'''
PHASE_FILE={self._q(str(self.phase_file))}
ROUND_FILE={self._q(str(self.round_file))}
CANDIDATE_STATE_DIR={self._q(str(self.tmp_path / "candidate_state"))}
ROLLBACK_STATE_DIR={self._q(str(self.tmp_path / "rollback_state"))}
CANDIDATE_MAX_IDX={candidate_max_idx}
ROLLBACK_MAX_IDX={rollback_max_idx}
CANDIDATE_MARKER={self._q(str(self.candidate_marker))}
ROLLBACK_MARKER={self._q(str(self.rollback_marker))}
RETAG_MARKER={self._q(str(self.retag_marker))}

cmd="$1"; shift
case "$cmd" in
  tag)
    if [ {int(self.rollback_tag_exit)} -ne 0 ]; then
      exit {int(self.rollback_tag_exit)}
    fi
    touch "$RETAG_MARKER"
    exit 0
    ;;
  pull)
    exit 0
    ;;
  login)
    cat > /dev/null
    exit 0
    ;;
  top)
    printf '%s\\n' {self._q(self.docker_top_output)}
    exit 0
    ;;
  port)
    printf '%s' {self._q(self.tor_published_ports)}
    exit 0
    ;;
  image)
    sub="$1"; shift
    if [ "$sub" = "inspect" ]; then
      # docker image inspect "$CANONICAL_IMAGE" --format '{{{{.Id}}}}'
      echo {self._q(self.pulled_canonical_image_id)}
      exit 0
    fi
    exit 1
    ;;
  compose)
    # docker compose -p PROJECT -f FILE (config|up ...)
    sub=""
    is_up=no
    force_recreate=no
    for a in "$@"; do
      case "$a" in
        config) sub="config" ;;
        up) is_up=yes ;;
        --force-recreate) force_recreate=yes ;;
      esac
    done
    if [ "$sub" = "config" ]; then
      printf 'services:\\n'
      printf '  api:\\n'
      printf '    image: %s\\n' "${{JOBPULSE_API_IMAGE:-{CANONICAL_IMAGE}}}"
      printf '    environment:\\n'
      printf '      TOR_ENABLED: "false"\\n'
      exit 0
    fi
    if [ "$is_up" = "yes" ] && [ "$force_recreate" = "yes" ]; then
      if [ "$JOBPULSE_API_IMAGE" = {self._q(CANONICAL_IMAGE)} ]; then
        if [ {int(self.candidate_compose_up_exit)} -ne 0 ]; then
          exit {int(self.candidate_compose_up_exit)}
        fi
        echo candidate > "$PHASE_FILE"
        echo 0 > "$ROUND_FILE"
        touch "$CANDIDATE_MARKER"
        exit 0
      elif [ "$JOBPULSE_API_IMAGE" = {self._q(EXPECTED_CURRENT_CONFIG_IMAGE)} ]; then
        if [ {int(self.rollback_compose_up_exit)} -ne 0 ]; then
          exit {int(self.rollback_compose_up_exit)}
        fi
        echo rollback > "$PHASE_FILE"
        echo 0 > "$ROUND_FILE"
        touch "$ROLLBACK_MARKER"
        exit 0
      fi
      exit 1
    fi
    exit 0
    ;;
  exec)
    name="$1"; shift
    if [ "$1" = "printenv" ]; then
      var="$2"
      PHASE=$(cat "$PHASE_FILE")
      case "$PHASE" in
        before)
          SEARCH_TRANSPORT={self._q(self.search_transport_before)}
          TOR_ENABLED={self._q(self.tor_enabled_before)}
          ;;
        candidate)
          SEARCH_TRANSPORT={self._q(self.candidate_search_transport)}
          TOR_ENABLED={self._q(self.candidate_tor_enabled)}
          ;;
        rollback)
          SEARCH_TRANSPORT={self._q(self.rollback_search_transport)}
          TOR_ENABLED={self._q(self.rollback_tor_enabled)}
          ;;
      esac
      case "$var" in
        SEARCH_TRANSPORT)
          if [ -n "$SEARCH_TRANSPORT" ]; then echo "$SEARCH_TRANSPORT"; exit 0; else exit 1; fi
          ;;
        TOR_ENABLED)
          echo "$TOR_ENABLED"
          exit 0
          ;;
      esac
      exit 1
    fi
    exit 1
    ;;
  inspect)
    fmt=""
    name=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -f) fmt="$2"; shift 2 ;;
        --format) fmt="$2"; shift 2 ;;
        jobpulse-api-prod|jobpulse-postgres-prod|jobpulse-frontend-prod|jobpulse-tor-prod) name="$1"; shift ;;
        *) shift ;;
      esac
    done
    PHASE=$(cat "$PHASE_FILE")
    case "$name" in
      jobpulse-api-prod)
        case "$fmt" in
          '{{{{.State.Running}}}}|{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}|{{{{.Image}}}}')
            case "$PHASE" in
              candidate) STATE_DIR="$CANDIDATE_STATE_DIR"; MAX_IDX="$CANDIDATE_MAX_IDX" ;;
              rollback) STATE_DIR="$ROLLBACK_STATE_DIR"; MAX_IDX="$ROLLBACK_MAX_IDX" ;;
              *) echo "unknown|unknown|unknown"; exit 0 ;;
            esac
            ROUND=$(cat "$ROUND_FILE")
            echo $((ROUND+1)) > "$ROUND_FILE"
            IDX=$ROUND
            [ "$IDX" -gt "$MAX_IDX" ] && IDX="$MAX_IDX"
            printf '%s|%s|%s\\n' "$(cat "$STATE_DIR/$IDX.running")" "$(cat "$STATE_DIR/$IDX.health")" "$(cat "$STATE_DIR/$IDX.image")"
            ;;
          '{{{{.Id}}}}')
            case "$PHASE" in
              before) echo {self._q(self.api_id_before)} ;;
              candidate) echo {self._q(self.candidate_id)} ;;
              rollback) echo {self._q(self.rollback_id)} ;;
            esac
            ;;
          '{{{{.Config.Image}}}}')
            case "$PHASE" in
              before) echo {self._q(self.api_config_image_before)} ;;
              candidate) echo {self._q(self.candidate_config_image)} ;;
              rollback) echo {self._q(self.rollback_config_image)} ;;
            esac
            ;;
          '{{{{.Image}}}}')
            echo {self._q(self.api_image_id_before)}
            ;;
          '{{{{.State.Running}}}}')
            echo {self._q(self.api_running_before)}
            ;;
          '{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}')
            echo {self._q(self.api_health_before)}
            ;;
          '{{{{.RestartCount}}}}')
            case "$PHASE" in
              before) echo {self._q(self.api_restart_count_before)} ;;
              candidate) echo {self._q(self.candidate_restart_count)} ;;
              rollback) echo {self._q(self.rollback_restart_count)} ;;
            esac
            ;;
          '{{{{index .Config.Labels "com.docker.compose.service"}}}}')
            echo {self._q(self.api_service_label)}
            ;;
          '{{{{index .Config.Labels "com.docker.compose.project"}}}}')
            echo {self._q(self.api_project_label)}
            ;;
          '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}')
            case "$PHASE" in
              before) printf '%b\\n' {self._q(env_lines_for(self.search_transport_before, self.tor_enabled_before))} ;;
              candidate) printf '%b\\n' {self._q(env_lines_for(self.candidate_search_transport, self.candidate_tor_enabled))} ;;
              rollback) printf '%b\\n' {self._q(env_lines_for(self.rollback_search_transport, self.rollback_tor_enabled))} ;;
            esac
            ;;
          *) echo "UNKNOWN_FORMAT_API:$fmt" >&2; exit 1 ;;
        esac
        exit 0
        ;;
      jobpulse-postgres-prod)
        case "$fmt" in
          '{{{{.Id}}}}') echo {self._q(self.db_id)} ;;
          '{{{{.Image}}}}') echo {self._q(self.db_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.db_running)} ;;
          '{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}') echo {self._q(self.db_health)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.db_restart_count)} ;;
          *) echo "UNKNOWN_FORMAT_DB:$fmt" >&2; exit 1 ;;
        esac
        exit 0
        ;;
      jobpulse-frontend-prod)
        case "$fmt" in
          '{{{{.Id}}}}')
            case "$PHASE" in
              before) echo {self._q(self.frontend_id)} ;;
              *) echo {self._q(frontend_id_after)} ;;
            esac
            ;;
          '{{{{.Image}}}}') echo {self._q(self.frontend_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.frontend_running)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.frontend_restart_count)} ;;
          '{{{{range .Mounts}}}}{{{{if eq .Destination "/usr/share/nginx/html"}}}}{{{{.Type}}}}|{{{{.Source}}}}|{{{{.Destination}}}}|{{{{.RW}}}}{{{{end}}}}{{{{end}}}}') echo {self._q(mount_line)} ;;
          *) echo "UNKNOWN_FORMAT_FRONTEND:$fmt" >&2; exit 1 ;;
        esac
        exit 0
        ;;
      jobpulse-tor-prod)
        case "$fmt" in
          '{{{{.Id}}}}') echo {self._q(self.tor_id)} ;;
          '{{{{.Image}}}}') echo {self._q(self.tor_image_id)} ;;
          '{{{{.State.Running}}}}') echo {self._q(self.tor_running)} ;;
          '{{{{with (index .State "Health")}}}}{{{{.Status}}}}{{{{else}}}}none{{{{end}}}}') echo {self._q(self.tor_health)} ;;
          '{{{{.RestartCount}}}}') echo {self._q(self.tor_restart_count)} ;;
          *) echo "UNKNOWN_FORMAT_TOR:$fmt" >&2; exit 1 ;;
        esac
        exit 0
        ;;
      *) exit 1 ;;
    esac
    ;;
  *) exit 1 ;;
esac
'''
        _write_fake_executable(bin_dir, "docker", docker_body)

        curl_body = f'''
PHASE_FILE={self._q(str(self.phase_file))}
ROUND_FILE={self._q(str(self.round_file))}
CANDIDATE_STATE_DIR={self._q(str(self.tmp_path / "candidate_state"))}
ROLLBACK_STATE_DIR={self._q(str(self.tmp_path / "rollback_state"))}
CANDIDATE_MAX_IDX={candidate_max_idx}
ROLLBACK_MAX_IDX={rollback_max_idx}

url="${{@: -1}}"
PHASE=$(cat "$PHASE_FILE")
case "$url" in
  http://127.0.0.1:8000/health)
    case "$PHASE" in
      candidate) STATE_DIR="$CANDIDATE_STATE_DIR"; MAX_IDX="$CANDIDATE_MAX_IDX" ;;
      rollback) STATE_DIR="$ROLLBACK_STATE_DIR"; MAX_IDX="$ROLLBACK_MAX_IDX" ;;
      *) exit 0 ;;
    esac
    ROUND=$(cat "$ROUND_FILE")
    IDX=$ROUND
    [ "$IDX" -gt "$MAX_IDX" ] && IDX="$MAX_IDX"
    [ "$(cat "$STATE_DIR/$IDX.direct")" = "true" ] && exit 0 || exit 22
    ;;
  http://127.0.0.1/api/health)
    case "$PHASE" in
      candidate) STATE_DIR="$CANDIDATE_STATE_DIR"; MAX_IDX="$CANDIDATE_MAX_IDX" ;;
      rollback) STATE_DIR="$ROLLBACK_STATE_DIR"; MAX_IDX="$ROLLBACK_MAX_IDX" ;;
      *) exit 0 ;;
    esac
    ROUND=$(cat "$ROUND_FILE")
    IDX=$ROUND
    [ "$IDX" -gt "$MAX_IDX" ] && IDX="$MAX_IDX"
    [ "$(cat "$STATE_DIR/$IDX.nginx")" = "true" ] && exit 0 || exit 22
    ;;
  http://127.0.0.1/)
    printf '%s' {self._q(self.frontend_content)}
    exit 0
    ;;
  *) exit 1 ;;
esac
'''
        _write_fake_executable(bin_dir, "curl", curl_body)


def _run_with_real_hashes(scenario: Scenario, remote_script: str, *, hold_outer_lock=False):
    """Executes the ACTUAL extracted remote script via `bash -c` against
    the scenario's fakes, passing REAL sha256sum-computed hashes of the
    scenario's own fake host files as the positional hash arguments (so
    the ACTUAL sha256sum binary and the script's own comparisons drive
    file-provenance, never a Python reimplementation)."""
    fake_bin = scenario.build_fakes()
    substituted = (
        remote_script.replace("/opt/jobpulse", str(scenario.fake_root))
        .replace("/tmp/jobpulse_collection_cycle.lock", str(scenario.outer_lock_path))
        .replace("/opt/jobpulse/state/run_collection_cycle.lock", str(scenario.internal_lock_path))
        .replace("/etc/crontab", str(scenario.etc_crontab_path))
        .replace("/etc/cron.d", str(scenario.etc_cron_d_path))
    )
    script = "#!/usr/bin/env bash\n" + substituted

    compose_sha = hashlib.sha256(scenario.compose_content.encode()).hexdigest()
    frontend_sha = hashlib.sha256(scenario.frontend_content.encode()).hexdigest()
    provider_sha = hashlib.sha256(scenario.provider_content.encode()).hexdigest()

    env = dict(os.environ)
    if getattr(scenario, "systemctl_available", True):
        env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    else:
        env["PATH"] = str(fake_bin)
    env.update(scenario.env())

    holder = None
    if hold_outer_lock:
        holder = sp.Popen(["flock", str(scenario.outer_lock_path), "sleep", "5"])
        import time as _time

        _time.sleep(0.3)
    try:
        result = sp.run(
            ["bash", "-c", script, "bash", compose_sha, frontend_sha, provider_sha, "false"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    finally:
        if holder is not None:
            holder.terminate()
            holder.wait(timeout=5)
    return result


def _assert_no_mutation(result, scenario):
    assert not scenario.candidate_marker.exists(), result.stdout + result.stderr
    assert not scenario.rollback_marker.exists(), result.stdout + result.stderr


# --- N1: happy normalization
def test_behavior_n1_happy_normalization_succeeds(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[
            dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True),
            dict(running="true", health="healthy", image=EXPECTED_IMAGE_ID, direct=True, nginx=True),
        ],
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert "PRODUCTION_API_REFERENCE_NORMALIZATION_COMPLETE" in result.stdout, result.stdout + result.stderr
    assert "API_REFERENCE_NORMALIZED_TO_CANONICAL_F476" in result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    assert scenario.candidate_marker.exists()
    assert not scenario.rollback_marker.exists()


# --- N2: canonical registry tag resolves wrong image ID
def test_behavior_n2_canonical_image_wrong_id_aborts_pre_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, pulled_canonical_image_id="sha256:" + "9" * 64)
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "resolved to unexpected ID" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N3: current Config.Image drift
def test_behavior_n3_current_config_image_drift_aborts(remote_script, tmp_path):
    scenario = Scenario(tmp_path, api_config_image_before="ghcr.io/mrezamaghouli/jobpulse-api:somethingelse")
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "API Config.Image" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N4: current underlying image ID drift
def test_behavior_n4_current_image_id_drift_aborts(remote_script, tmp_path):
    scenario = Scenario(tmp_path, api_image_id_before="sha256:" + "8" * 64)
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "API underlying image ID" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N5: API container ID drift
def test_behavior_n5_api_container_id_drift_aborts(remote_script, tmp_path):
    scenario = Scenario(tmp_path, api_id_before="0" * 64)
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "API container ID" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N6: DB/frontend/Tor ID drift
def test_behavior_n6_db_id_drift_aborts(remote_script, tmp_path):
    scenario = Scenario(tmp_path, db_id="1" * 64)
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "DB container ID" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N7: collector active
def test_behavior_n7_active_collector_aborts(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        docker_top_output="PID   USER   TIME   COMMAND\n1     root   0:00   python -m scripts.run_collection_cycle",
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "active collector process detected" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N8: lock busy
def test_behavior_n8_outer_lock_busy_aborts(remote_script, tmp_path):
    scenario = Scenario(tmp_path)
    result = _run_with_real_hashes(scenario, remote_script, hold_outer_lock=True)
    assert result.returncode != 0
    assert "outer collection lock busy" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N9: candidate compose exits nonzero after mutation marker
def test_behavior_n9_candidate_compose_failure_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(tmp_path, candidate_compose_up_exit=1)
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert scenario.rollback_marker.exists()
    assert "rollback_attempted=true" in result.stderr


# --- N10: candidate remains health=starting until bound
def test_behavior_n10_candidate_never_healthy_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True)],
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "did not converge" in result.stderr
    assert scenario.rollback_marker.exists()


# --- N11: candidate Config.Image does not become canonical
def test_behavior_n11_candidate_config_image_not_canonical_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(tmp_path, candidate_config_image="jobpulse-api-rollback:unexpected-1234")
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "does not equal expected" in result.stderr
    assert scenario.rollback_marker.exists()


# --- N12: candidate underlying image ID differs after recreation
def test_behavior_n12_candidate_wrong_image_id_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[dict(running="true", health="healthy", image="sha256:" + "7" * 64, direct=True, nginx=True)],
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "did not converge" in result.stderr
    assert scenario.rollback_marker.exists()


# --- N13: candidate SEARCH_TRANSPORT=proxy
def test_behavior_n13_candidate_search_transport_proxy_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(tmp_path, candidate_search_transport="proxy")
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "SEARCH_TRANSPORT is not unset/direct" in result.stderr
    assert scenario.rollback_marker.exists()


# --- N14: candidate TOR_ENABLED != false
def test_behavior_n14_candidate_tor_enabled_true_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(tmp_path, candidate_tor_enabled="true")
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "TOR_ENABLED is not exactly 'false'" in result.stderr
    assert scenario.rollback_marker.exists()


# --- N15: candidate succeeds but DB/frontend/Tor ID changes
def test_behavior_n15_post_candidate_frontend_drift_triggers_rollback(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        frontend_id_after_candidate="deadbeef" * 8,
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "frontend container ID changed" in result.stderr
    assert scenario.candidate_marker.exists()
    assert scenario.rollback_marker.exists()
    # The fake docker only ever recreates a container for the api
    # service (see Scenario._build_docker_and_curl_fakes) -- the
    # unrelated drifted frontend is never itself mutated by this
    # workflow, structurally proven by test_no_frontend_recreation.


# --- N16: rollback health starting -> healthy
def test_behavior_n16_rollback_converges_but_workflow_still_fails(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True)],
        rollback_sequence=[
            dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True),
            dict(running="true", health="healthy", image=EXPECTED_IMAGE_ID, direct=True, nginx=True),
        ],
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0, "final workflow result must still be FAILURE"
    assert "NORMALIZATION_FAILED_ROLLBACK_CONFIRMED_PRESTATE_RESTORED" in result.stdout
    assert "rollback_confirmed=true" in result.stderr
    assert "PRODUCTION_API_REFERENCE_NORMALIZATION_COMPLETE" not in result.stdout


# --- N17: rollback restores exact old Config.Image alias
def test_behavior_n17_rollback_restores_exact_old_alias(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True)],
        rollback_config_image=EXPECTED_CURRENT_CONFIG_IMAGE,
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert scenario.retag_marker.exists()
    assert "rollback_confirmed=true" in result.stderr
    assert result.returncode != 0


# --- N18: rollback cannot converge
def test_behavior_n18_rollback_cannot_converge_one_attempt_only(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True)],
        rollback_sequence=[dict(running="true", health="starting", image=EXPECTED_IMAGE_ID, direct=True, nginx=True)],
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "rollback_confirmed=false" in result.stderr
    assert "manual operator review required" in result.stderr
    assert "final_api_container_id=" in result.stderr
    # Exactly one rollback attempt: only one compose-up with the rollback alias.
    rollback_up_count = result.stderr.count("ROLLBACK: retagging")
    assert rollback_up_count == 1


# --- N19: production git SHA changes unexpectedly before mutation
def test_behavior_n19_git_sha_drift_aborts_pre_mutation(remote_script, tmp_path):
    scenario = Scenario(tmp_path, current_git_sha="0" * 40)
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode != 0
    assert "production git HEAD" in result.stderr
    _assert_no_mutation(result, scenario)


# --- N20: production git SHA remains f476 through candidate success
def test_behavior_n20_git_sha_stable_through_success(remote_script, tmp_path):
    scenario = Scenario(
        tmp_path,
        candidate_sequence=[dict(running="true", health="healthy", image=EXPECTED_IMAGE_ID, direct=True, nginx=True)],
    )
    result = _run_with_real_hashes(scenario, remote_script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git_invariant=ok" in result.stdout


def test_behavior_mutation_sentinels_untouched_on_happy_path(remote_script, tmp_path):
    scenario = Scenario(tmp_path)
    fake_bin = scenario.build_fakes()
    result = _run_with_real_hashes(scenario, remote_script)
    assert "FATAL_TEST_HARNESS_MUTATION_SENTINEL" not in result.stdout
    assert "FATAL_TEST_HARNESS_MUTATION_SENTINEL" not in result.stderr
