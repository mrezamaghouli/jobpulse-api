"""
Structural regression guard for
.github/workflows/production-direct-runtime-upgrade.yml (Phase 3.4N-B).

This workflow SSHes to the production VM and can move the production
`api` runtime from one exact reviewed commit to another, keeping
collection transport DIRECT throughout. It is manual-only
(workflow_dispatch) and must never be reachable through any automatic
trigger. These tests are network-free and never dispatch the workflow --
they only parse the committed YAML and its embedded shell scripts as
text/structure, the same way tests/test_production_neutral_canary_workflow.py
already does for production-neutral-canary.yml.

The single "Run the immutable direct-runtime upgrade on production" step
contains two shell contexts:

- a RUNNER script: everything that executes on the GitHub-hosted runner
  itself (SSH/SCP setup, writing the remote script to a local temp file,
  transferring it, invoking it over SSH).
- a REMOTE script: the body of the `cat > "$REMOTE_SCRIPT_LOCAL"
  <<'REMOTE_SCRIPT' ... REMOTE_SCRIPT` heredoc, which is written to a
  local file, scp'd to the VM, and only then executed there.

Several checks are deliberately scoped to only one of the two -- e.g.
`git fetch origin main` is expected on the runner (to validate image_sha
against origin/main) AND on the remote script (to re-validate the target
SHA immediately before the git reset that actually moves the production
checkout).
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-direct-runtime-upgrade.yml"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

REMOTE_HEREDOC_START = "<<'REMOTE_SCRIPT'"
REMOTE_HEREDOC_END = "REMOTE_SCRIPT"

REVIEWED_TARGET_SHA = "f4765f857355c6543f68cea0e481b7f20a917147"
EXPECTED_CURRENT_SHA = "148bd362b37c82c92737382d181fbdeac4d2187b"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text()


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    return yaml.safe_load(workflow_text)


def _triggers(workflow: dict) -> dict:
    """PyYAML's default (YAML 1.1) resolver parses the bare `on:` key as
    the boolean True rather than the string "on"."""
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


@pytest.fixture(scope="module")
def job(workflow) -> dict:
    return workflow["jobs"]["direct-runtime-upgrade"]


@pytest.fixture(scope="module")
def steps(job) -> list:
    return job["steps"]


def _step_by_name(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


@pytest.fixture(scope="module")
def upgrade_step(steps):
    return _step_by_name(steps, "Run the immutable direct-runtime upgrade on production")


@pytest.fixture(scope="module")
def full_run_text(upgrade_step) -> str:
    return upgrade_step["run"]


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
    return subprocess.run(
        ["bash", "-n"], input=script, capture_output=True, text=True, timeout=10
    )


# =====================================================================
# 1. YAML / structural validity
# =====================================================================

def test_workflow_exists_and_is_valid_yaml_with_expected_job(workflow):
    assert isinstance(workflow, dict)
    assert isinstance(workflow.get("jobs"), dict)
    assert "direct-runtime-upgrade" in workflow["jobs"]


def test_workflow_name_is_exact(workflow):
    assert workflow["name"] == "Production Direct Runtime Upgrade"


def test_workflow_yaml_has_no_duplicate_mapping_keys(workflow_text):
    class UniqueKeyLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            seen = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise ValueError(f"Duplicate key {key!r} found in YAML mapping")
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    yaml.load(workflow_text, Loader=UniqueKeyLoader)  # raises on duplicate keys


def test_full_upgrade_step_run_text_is_valid_bash_syntax(full_run_text):
    result = _run_bash_n(full_run_text)
    assert result.returncode == 0, result.stderr


def test_runner_script_is_valid_bash_syntax(runner_script):
    result = _run_bash_n(runner_script)
    assert result.returncode == 0, result.stderr


def test_remote_script_is_valid_bash_syntax(remote_script):
    result = _run_bash_n(remote_script)
    assert result.returncode == 0, result.stderr


def test_exactly_one_remote_script_heredoc_pair(full_run_text):
    assert full_run_text.count(REMOTE_HEREDOC_START) == 1
    lines = full_run_text.splitlines()
    end_count = sum(1 for line in lines if line.strip() == REMOTE_HEREDOC_END)
    assert end_count == 1


def test_all_other_run_steps_are_valid_bash_syntax(steps):
    for step in steps:
        if "run" not in step:
            continue
        if step is _step_by_name(steps, "Run the immutable direct-runtime upgrade on production"):
            continue
        if step.get("name") == "Verify target compose invariants (api stays direct, no Tor wiring)":
            # This step's body is a `python3 - <<'PY' ... PY` heredoc --
            # bash -n on the outer text is still valid (heredoc body is
            # opaque to bash syntax checking), and separately verified
            # for Python syntax below.
            pass
        result = _run_bash_n(step["run"])
        assert result.returncode == 0, f"{step.get('name')}: {result.stderr}"


def test_compose_invariants_step_python_body_is_valid_syntax(steps):
    import ast

    step = _step_by_name(steps, "Verify target compose invariants (api stays direct, no Tor wiring)")
    text = step["run"]
    start = text.index("<<'PY'") + len("<<'PY'")
    rest = text[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == "PY")
    body = "\n".join(lines[:end_idx])
    ast.parse(body)  # raises SyntaxError on failure


# =====================================================================
# 2. Trigger shape
# =====================================================================

def test_trigger_is_workflow_dispatch_only(workflow):
    triggers = _triggers(workflow)
    assert set(triggers.keys()) == {"workflow_dispatch"}


def test_no_push_pull_request_workflow_run_schedule_trigger(workflow_text):
    triggers_block = workflow_text.split("on:", 1)[1].split("permissions:", 1)[0]
    for forbidden in ("push:", "pull_request:", "workflow_run:", "schedule:", "repository_dispatch:"):
        assert forbidden not in triggers_block, f"forbidden trigger {forbidden!r} present"


def test_required_image_sha_and_confirmation_inputs(workflow):
    inputs = workflow["jobs"]  # noop to keep var used below deterministically
    triggers = _triggers(workflow)
    dispatch_inputs = triggers["workflow_dispatch"]["inputs"]
    assert "image_sha" in dispatch_inputs
    assert "confirmation" in dispatch_inputs
    assert dispatch_inputs["image_sha"]["required"] is True
    assert dispatch_inputs["confirmation"]["required"] is True
    assert dispatch_inputs["image_sha"]["type"] == "string"
    assert dispatch_inputs["confirmation"]["type"] == "string"


# =====================================================================
# 3. Permissions / concurrency / timeout
# =====================================================================

def test_permissions_contents_read_only(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_concurrency_group_and_cancel_in_progress(workflow):
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "jobpulse-production-direct-runtime-upgrade"
    assert concurrency["cancel-in-progress"] is False


def test_job_timeout_minutes_is_fifteen(job):
    assert job["timeout-minutes"] == 15


def test_runs_on_ubuntu_latest(job):
    assert job["runs-on"] == "ubuntu-latest"


# =====================================================================
# 4. Confirmation token / target SHA pinning
# =====================================================================

def test_confirmation_token_is_exact(workflow_text):
    assert '"$CONFIRMATION" != "UPGRADE_DIRECT_RUNTIME"' in workflow_text


def test_reviewed_target_sha_hardcoded(workflow):
    env = workflow["jobs"]["direct-runtime-upgrade"]["env"] if "env" in workflow["jobs"]["direct-runtime-upgrade"] else workflow["env"]
    assert workflow["env"]["REVIEWED_TARGET_SHA"] == REVIEWED_TARGET_SHA


def test_expected_current_sha_hardcoded(workflow):
    assert workflow["env"]["EXPECTED_CURRENT_SHA"] == EXPECTED_CURRENT_SHA


def test_image_sha_input_validated_against_reviewed_constant(workflow_text):
    assert '"$IMAGE_SHA" != "$REVIEWED_TARGET_SHA"' in workflow_text


def test_image_sha_shape_validated_as_40_hex(workflow_text):
    assert re.search(r'\^\[0-9a-f\]\{40\}\$', workflow_text)


def test_expected_current_sha_hardcoded_in_remote_script(remote_script):
    assert f'EXPECTED_CURRENT_SHA="{EXPECTED_CURRENT_SHA}"' in remote_script


def test_no_arbitrary_target_sha_accepted(workflow_text):
    # image_sha must be validated against the hardcoded constant, not
    # merely checked for ancestry-of-main (which would accept any
    # ancestor, not just the specifically-reviewed one).
    assert "REVIEWED_TARGET_SHA" in workflow_text
    assert 'if [ "$IMAGE_SHA" != "$REVIEWED_TARGET_SHA" ]' in workflow_text


# =====================================================================
# 5. Main-ref guard
# =====================================================================

def test_require_dispatch_from_main_step_exists_and_is_first(steps):
    assert steps[0]["name"] == "Require dispatch from main"
    assert "refs/heads/main" in steps[0]["run"]


def test_main_ref_guard_precedes_ssh_key_material(steps):
    step_names = [s.get("name") for s in steps]
    guard_index = step_names.index("Require dispatch from main")
    ssh_index = step_names.index("Run the immutable direct-runtime upgrade on production")
    assert guard_index < ssh_index


# =====================================================================
# 6. Ancestor-of-main / required-files proof (runner side)
# =====================================================================

def test_runner_proves_target_is_ancestor_of_main(steps):
    step = _step_by_name(steps, "Prove target SHA is a reviewed ancestor of main and extract its compose file")
    text = step["run"]
    assert "git fetch origin main" in text
    assert 'git cat-file -e "${IMAGE_SHA}^{commit}"' in text
    assert 'git merge-base --is-ancestor "$IMAGE_SHA" origin/main' in text


def test_runner_proves_required_target_files_exist(steps):
    step = _step_by_name(steps, "Prove target SHA is a reviewed ancestor of main and extract its compose file")
    text = step["run"]
    for required_path in (
        "app/config.py",
        "scripts/search_transport/transport.py",
        "scripts/search_transport/executor.py",
        "scripts/providers/linkedin_browser_provider.py",
        "scripts/repair_jobpulse_schema.py",
    ):
        assert f'git show "${{IMAGE_SHA}}:{required_path}"' in text, required_path


def test_target_compose_extracted_to_fixed_path_not_from_working_tree(steps):
    step = _step_by_name(steps, "Prove target SHA is a reviewed ancestor of main and extract its compose file")
    text = step["run"]
    assert 'git show "${IMAGE_SHA}:docker-compose.prod.yml" > /tmp/target-compose.yml' in text


# =====================================================================
# 7. Target compose invariants step
# =====================================================================

def test_compose_invariants_step_checks_required_properties(steps):
    step = _step_by_name(steps, "Verify target compose invariants (api stays direct, no Tor wiring)")
    text = step["run"]
    for expected in (
        "JOBPULSE_API_IMAGE",
        'TOR_ENABLED"',
        "TOR_SOCKS_HOST",
        "TOR_SOCKS_PORT",
        "SEARCH_TRANSPORT",
        "depends_on",
        "scripts.repair_jobpulse_schema",
        "uvicorn",
    ):
        assert expected in text, expected


def test_compose_invariants_step_runs_against_real_target_compose():
    """End-to-end sanity: the actual check logic, run against the real
    f476 docker-compose.prod.yml pulled from git history, must pass
    without needing Docker/SSH -- proving the assertions are correct,
    not merely present as text."""
    import subprocess as sp
    import sys

    result = sp.run(
        ["git", "show", f"{REVIEWED_TARGET_SHA}:docker-compose.prod.yml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    compose_text = result.stdout

    script = """
import sys
import yaml

compose = yaml.safe_load(sys.stdin.read())
api = compose["services"]["api"]
errors = []

image = api.get("image", "")
if "JOBPULSE_API_IMAGE" not in image:
    errors.append("image")

env = api.get("environment", {}) or {}
if env.get("TOR_ENABLED") != "false":
    errors.append("TOR_ENABLED")
if "TOR_SOCKS_HOST" in env:
    errors.append("TOR_SOCKS_HOST")
if "TOR_SOCKS_PORT" in env:
    errors.append("TOR_SOCKS_PORT")
if env.get("SEARCH_TRANSPORT") == "proxy":
    errors.append("SEARCH_TRANSPORT")

depends_on = api.get("depends_on", {}) or {}
if "tor" in depends_on:
    errors.append("depends_on tor")

command = api.get("command", "")
if "scripts.repair_jobpulse_schema" not in command or "uvicorn" not in command:
    errors.append("command")
if command.find("scripts.repair_jobpulse_schema") > command.find("uvicorn"):
    errors.append("command order")

sys.exit(1 if errors else 0)
"""
    proc = sp.run(
        [sys.executable, "-c", script], input=compose_text, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_ci_verify_job_checks_out_full_history_for_historical_target_git_show():
    """test_compose_invariants_step_runs_against_real_target_compose (above)
    runs `git show <REVIEWED_TARGET_SHA>:docker-compose.prod.yml` against
    the actual reviewed historical commit. A default `actions/checkout@v4`
    only fetches a depth-1 shallow clone, which does not contain that
    commit and makes the `git show` fail in CI even though the test is
    correct and passes locally against a full clone. The `verify` job in
    ci.yml must therefore explicitly request full history so this
    structural regression guard can actually run in CI."""
    ci_text = CI_WORKFLOW_PATH.read_text()
    ci_workflow = yaml.safe_load(ci_text)

    verify_job = ci_workflow["jobs"]["verify"]
    checkout_step = _step_by_name(verify_job["steps"], "Checkout repository")

    assert checkout_step.get("uses") == "actions/checkout@v4"
    assert checkout_step.get("with", {}).get("fetch-depth") == 0


# =====================================================================
# 8. SSH bounding model (runner script)
# =====================================================================

def _executable_ssh_keyscan_lines(runner_script: str):
    return [line for line in _executable_lines(runner_script) if "ssh-keyscan" in line]


def test_exactly_one_bounded_ssh_keyscan(runner_script):
    lines = _executable_ssh_keyscan_lines(runner_script)
    assert len(lines) == 1
    assert "-T 10" in lines[0]


def test_ssh_opts_bounded_and_applied(runner_script):
    assert "SSH_OPTS=(" in runner_script
    for expected in (
        "ConnectTimeout=10",
        "ConnectionAttempts=1",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=4",
        "BatchMode=yes",
        "StrictHostKeyChecking=yes",
    ):
        assert expected in runner_script, expected


def test_ssh_opts_applied_to_both_scp_and_ssh(runner_script):
    scp_lines = [line for line in _executable_lines(runner_script) if line.startswith("scp ")]
    ssh_lines = [
        line
        for line in _executable_lines(runner_script)
        if "ssh " in line and "ssh-keyscan" not in line and not line.startswith("scp")
    ]
    assert scp_lines, "no scp invocation found"
    assert ssh_lines, "no ssh invocation found"
    for line in scp_lines + ssh_lines:
        assert '"${SSH_OPTS[@]}"' in line, line


# =====================================================================
# 9. Secret transport model
# =====================================================================

def test_no_ghcr_token_base64_anywhere(runner_script, remote_script):
    # "Base64" is permitted to appear in explanatory comments (describing
    # what is deliberately NOT done, matching the already-reviewed
    # Phase 3.4M canary's own comment style) -- only executable lines are
    # checked for an actual base64 transformation of the token.
    all_executable = list(_executable_lines(runner_script)) + list(_executable_lines(remote_script))
    for line in all_executable:
        assert "GHCR_TOKEN_B64" not in line, line
        if "GHCR_TOKEN" in line:
            assert "base64" not in line.lower(), line


def test_ghcr_token_never_in_remote_command_argv(runner_script):
    """GHCR_TOKEN must only appear on the LEFT side of the stdin pipe
    (`printf ... "${GHCR_TOKEN:-}" | ssh ...`) -- never inside the quoted
    remote command string that becomes the SSH command's argv."""
    ssh_command_line = next(
        line for line in runner_script.splitlines() if line.strip().startswith('"bash ')
    )
    assert "GHCR_TOKEN" not in ssh_command_line, ssh_command_line


def test_ghcr_token_piped_via_stdin_only(runner_script):
    assert 'printf \'%s\' "${GHCR_TOKEN:-}" | ssh' in runner_script


def test_docker_login_uses_password_stdin(remote_script):
    assert "docker login ghcr.io -u mrezamaghouli --password-stdin" in remote_script


def test_ghcr_comment_does_not_falsely_claim_stdin_is_conditionally_unread(runner_script):
    """A prior revision's comment claimed the remote script never reads
    the GHCR token stdin when the immutable image is already cached --
    false: the script pulls the exact tag every run, and reads/consumes
    this stdin via `docker login` whenever HAS_TOKEN=true regardless of
    local caching."""
    assert "the remote script never reads" not in runner_script
    assert "already present on the VM" not in runner_script


def test_ghcr_comment_accurately_describes_exact_pull_and_isolated_auth(runner_script):
    assert "exact immutable" in runner_script or "exact-tag" in runner_script
    assert "isolated temporary DOCKER_CONFIG" in runner_script


# =====================================================================
# 10. Isolated Docker auth
# =====================================================================

def test_isolated_docker_config_created_unconditionally(remote_script):
    assert 'TMP_DOCKER_CONFIG="$(mktemp -d)"' in remote_script
    # The mktemp call must occur before the conditional login, and the
    # pull must always use the isolated config regardless of HAS_TOKEN.
    mktemp_index = remote_script.index('TMP_DOCKER_CONFIG="$(mktemp -d)"')
    login_index = remote_script.index("docker login ghcr.io")
    pull_index = remote_script.index('docker pull "$TARGET_IMAGE"')
    assert mktemp_index < login_index < pull_index


def test_pull_always_uses_isolated_docker_config(remote_script):
    pull_line = next(
        line for line in _executable_lines(remote_script) if 'docker pull "$TARGET_IMAGE"' in line
    )
    assert pull_line.startswith('DOCKER_CONFIG="$TMP_DOCKER_CONFIG"')


def test_no_unscoped_docker_pull(remote_script):
    for line in _executable_lines(remote_script):
        if line.strip() == 'docker pull "$TARGET_IMAGE"':
            pytest.fail(f"unscoped docker pull found: {line!r}")


def test_tmp_docker_config_cleaned_up(remote_script):
    assert 'rm -rf "$TMP_DOCKER_CONFIG"' in remote_script


def test_no_default_docker_config_mutation(remote_script):
    assert "~/.docker/config.json" not in remote_script
    # bare/empty DOCKER_CONFIG assignment (not the TMP_DOCKER_CONFIG
    # variable declaration) would fall back to the production user's
    # real default config -- must never appear as its own assignment.
    assert re.search(r'(?<!TMP_)DOCKER_CONFIG=""', remote_script) is None
    assert 'DOCKER_CONFIG="${TMP_DOCKER_CONFIG:-}"' not in remote_script


# =====================================================================
# 11. Exact target image
# =====================================================================

def test_target_image_computed_from_image_sha_never_main_or_latest(runner_script):
    assert 'TARGET_IMAGE="ghcr.io/mrezamaghouli/jobpulse-api:${IMAGE_SHA}"' in runner_script
    for line in _executable_lines(runner_script):
        if "TARGET_IMAGE=" in line and ("main" in line.lower() or ":latest" in line):
            pytest.fail(f"mutable tag in TARGET_IMAGE assignment: {line!r}")


def test_no_main_or_latest_tag_anywhere_in_remote_script(remote_script):
    for line in _executable_lines(remote_script):
        if "docker pull" in line or "JOBPULSE_API_IMAGE=" in line:
            assert ":main" not in line, line
            assert ":latest" not in line, line


def test_target_image_id_captured_after_pull(remote_script):
    assert 'TARGET_IMAGE_ID="$(docker image inspect "$TARGET_IMAGE" --format' in remote_script


# =====================================================================
# 12. Current-live-runtime fail-closed checks
# =====================================================================

def test_current_checkout_sha_checked_before_any_mutation(remote_script):
    assert 'CURRENT_HEAD="$(git rev-parse HEAD)"' in remote_script
    assert f'"$CURRENT_HEAD" != "$EXPECTED_CURRENT_SHA"' in remote_script


def test_current_api_image_reference_checked(remote_script):
    assert 'EXPECTED_CURRENT_IMAGE="ghcr.io/mrezamaghouli/jobpulse-api:${EXPECTED_CURRENT_SHA}"' in remote_script
    assert '"$API_IMAGE_REF_BEFORE" != "$EXPECTED_CURRENT_IMAGE"' in remote_script


def test_tracked_worktree_cleanliness_checked_untracked_allowed(remote_script):
    assert "git status --porcelain | grep -v '^??'" in remote_script


def test_no_git_clean_anywhere(workflow_text):
    assert "git clean" not in workflow_text


def test_service_and_project_labels_checked(remote_script):
    assert 'com.docker.compose.service' in remote_script
    assert 'com.docker.compose.project' in remote_script
    assert '"$API_SERVICE_LABEL" != "api"' in remote_script
    assert '"$API_PROJECT_LABEL" != "$PRODUCTION_PROJECT"' in remote_script


def test_running_and_healthy_required_restart_count_not_required(remote_script):
    assert '"$API_RUNNING_BEFORE" != "true" ] || [ "$API_HEALTH_BEFORE" != "healthy"' in remote_script
    # restart count is captured but must not gate the precondition
    precondition_block = remote_script.split("--- capturing unrelated production containers")[0]
    assert "API_RESTARTS_BEFORE" in precondition_block
    assert '"$API_RESTARTS_BEFORE" !=' not in precondition_block


# =====================================================================
# 13. Direct-mode / env-file precondition
# =====================================================================

def test_search_transport_and_tor_enabled_precondition(remote_script):
    assert '"$API_SEARCH_TRANSPORT_BEFORE" != "direct"' in remote_script
    assert '"$API_TOR_ENABLED_BEFORE" != "false"' in remote_script


def test_narrow_env_file_inspection_no_secret_dump(remote_script):
    assert "for env_file in .api_keys.env /opt/jobpulse/.admin.env .env" in remote_script
    assert "grep -E '^SEARCH_TRANSPORT=' \"$env_file\"" in remote_script
    # must not cat/print full env file contents
    section = remote_script.split("narrow env-file inspection")[1].split("acquiring non-blocking")[0]
    assert "cat \"$env_file\"" not in section
    assert "cat $env_file" not in section


# =====================================================================
# 13b. Effective candidate Compose environment (resolved, before mutation)
# =====================================================================

def test_effective_candidate_compose_config_resolved_via_docker_compose_config(remote_script):
    """A narrow grep over individual env files is not a complete proof of
    Docker Compose's own env_file/environment precedence and
    interpolation semantics -- the candidate must never even briefly
    start in proxy mode and only be caught after the fact. This requires
    resolving the ACTUAL merged configuration via `docker compose
    ... config`."""
    assert 'docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" config' in remote_script
    assert "EFFECTIVE_CANDIDATE_SEARCH_TRANSPORT" in remote_script
    assert "EFFECTIVE_CANDIDATE_TOR_ENABLED" in remote_script


def test_effective_candidate_compose_env_checked_before_any_mutation(remote_script):
    config_index = remote_script.index('docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" config')
    check_index = remote_script.index('"$EFFECTIVE_CANDIDATE_SEARCH_TRANSPORT" != "direct"')
    mutation_index = remote_script.index("API_MUTATION_STARTED=true")
    pull_index = remote_script.index('docker pull "$TARGET_IMAGE"')
    assert config_index < check_index < pull_index < mutation_index


def test_effective_candidate_search_transport_and_tor_enabled_gated(remote_script):
    assert '"$EFFECTIVE_CANDIDATE_SEARCH_TRANSPORT" != "direct"' in remote_script
    assert '"$EFFECTIVE_CANDIDATE_TOR_ENABLED" != "false"' in remote_script


def test_effective_candidate_config_never_dumps_full_config(remote_script):
    """Only the two specific values needed are extracted -- the resolved
    config as a whole (which can include other env vars/secrets) is
    never echoed/printed."""
    section = remote_script.split("proving the effective candidate Compose environment")[1].split(
        "pinning immutable target API image"
    )[0]
    assert "echo \"$EFFECTIVE_CANDIDATE_CONFIG\"" not in section
    assert "cat <<<\"$EFFECTIVE_CANDIDATE_CONFIG\"" not in section


# =====================================================================
# 14. Collection-cycle lock
# =====================================================================

def test_cycle_lock_path_is_runtime_resolved_not_hardcoded_proven(remote_script, workflow_text):
    """Regression guard: the default path is not, on its own, provably
    the production wrapper's *effective* lock path -- it must only be
    computed inside resolve_final_lock_path(), never as a bare
    top-level constant, and no wording anywhere may claim it is already
    "proven"."""
    assert "resolve_final_lock_path() {" in remote_script
    func_body = remote_script.split("resolve_final_lock_path() {")[1].split("\n          }")[0]
    assert "$EFFECTIVE_COLLECTION_ROOT/state/run_collection_cycle.lock" in func_body
    assert "proven production" not in workflow_text
    assert "proven default" not in workflow_text
    assert "no override found anywhere in the codebase" not in workflow_text


def test_collection_env_parsed_as_data_never_sourced_or_evaled(remote_script):
    """Regression guard for the Final Scheduler-Proven Lock Hardening
    fix: sourcing a file (even inside a subshell) is NOT read-only -- it
    can execute commands, write files, or make network calls, since a
    subshell only isolates shell VARIABLES, never those side effects.
    The resolver must never source or eval .collection.env."""
    assert '. "$ROOT/.collection.env"' not in remote_script
    assert '. "$envfile"' not in remote_script
    executable_lines = list(_executable_lines(remote_script))
    assert not any(line == "eval" or line.startswith("eval ") for line in executable_lines)
    assert "set -a" not in remote_script
    # the even-older restricted-grep-only approach must not have returned either
    assert "grep -cE '^JOBPULSE_COLLECTION_CYCLE_LOCK_PATH='" not in remote_script


def test_collection_env_resolver_uses_restricted_parser_functions(remote_script):
    assert "resolve_collection_env_lock_override() {" in remote_script
    func_body = remote_script.split("resolve_collection_env_lock_override() {")[1].split("\n          }")[0]
    assert "parse_assignment_line" in func_body
    assert "extract_literal_value" in func_body
    assert '"shell"' in func_body


def test_collection_env_resolution_never_prints_file_contents(remote_script):
    """The file IS read as data (via `cat`, into a variable) -- that is
    the whole point of parsing it as data instead of sourcing it. What
    must never happen is echoing/logging that content wholesale."""
    func_body = remote_script.split("resolve_collection_env_lock_override() {")[1].split("\n          }")[0]
    assert 'timeout 5 cat "$envfile"' in func_body
    assert 'echo "$content"' not in func_body


def test_resolved_lock_path_relative_fails_closed(remote_script):
    func_body = remote_script.split("resolve_final_lock_path() {")[1].split("\n          }")[0]
    assert "case \"$CYCLE_LOCK_PATH\" in" in func_body
    assert "refusing to proceed" in func_body


def test_lock_path_resolved_before_flock(remote_script):
    resolve_index = remote_script.index("resolving effective production collection-cycle lock path")
    flock_index = remote_script.index('flock -n "$CYCLE_LOCK_FD"')
    assert resolve_index < flock_index


def test_flock_uses_resolved_lock_path(remote_script):
    assert 'exec {CYCLE_LOCK_FD}>"$CYCLE_LOCK_PATH"' in remote_script
    assert 'flock -n "$CYCLE_LOCK_FD"' in remote_script


# =====================================================================
# 14b. Restricted, non-executing parser primitives
# =====================================================================

def test_extract_literal_value_rejects_variable_and_command_expansion(remote_script):
    """The restricted value-extractor must never accept `$`, backticks,
    or `$(...)` inside a double-quoted or bare value -- those are real
    shell-expansion constructs this parser never evaluates."""
    func_body = remote_script.split("extract_literal_value() {")[1].split("\n          }")[0]
    assert "'$'" in func_body or '"$"' in func_body.replace("\\$", "$")
    assert '`' in func_body
    assert "^[A-Za-z0-9/._-]+$" in func_body, "bare unquoted values must use an allowlist, not a denylist"


def test_extract_literal_value_supports_quotes_and_export_and_comments(remote_script):
    assert "parse_assignment_line() {" in remote_script
    assign_body = remote_script.split("parse_assignment_line() {")[1].split("\n          }")[0]
    assert "export" in assign_body
    extract_body = remote_script.split("extract_literal_value() {")[1].split("\n          }")[0]
    assert '\\#' in extract_body or "#" in extract_body


def test_classify_wrapper_command_rejects_shell_wrapping(remote_script):
    func_body = remote_script.split("classify_wrapper_command() {")[1].split("\n          }")[0]
    for needle in ("bash", "sh", "env", "source", "&&", "||", "WRAPPER_UNSUPPORTED"):
        assert needle in func_body, needle


def test_parse_job_line_supports_user_and_system_crontab_formats(remote_script):
    func_body = remote_script.split("parse_job_line() {")[1].split("\n          }")[0]
    assert '"system"' in func_body
    assert "f6" in func_body, "system-mode (/etc/crontab, /etc/cron.d) must account for the extra user field"


# =====================================================================
# 14c. Order-aware cron scanning (scan_cron_source)
# =====================================================================

def test_scan_cron_source_is_order_aware_not_a_global_collector(remote_script):
    """Regression guard: cron variable-assignment semantics are ordered
    -- an assignment after a job line must not affect that earlier job.
    scan_cron_source must walk lines in order maintaining running
    cur_root/cur_lock state, never globally collect every assignment in
    the file irrespective of position."""
    func_body = remote_script.split("scan_cron_source() {")[1].split("\n          }")[0]
    assert "while IFS= read -r line" in func_body
    assert "cur_root=" in func_body
    assert "cur_lock=" in func_body
    assert 'DISCOVERED_LAUNCHERS+=("${cur_root}|${cur_lock}|${label}")' in func_body


def test_scan_cron_source_fails_closed_on_ambiguous_relevant_line(remote_script):
    func_body = remote_script.split("scan_cron_source() {")[1].split("\n          }")[0]
    assert "mentions_var" in func_body
    assert "mentions_script" in func_body
    assert "refusing to guess, failing closed" in func_body
    # a line mentioning neither must be safely skippable without
    # attempting to classify its shape at all
    assert 'if [ "$mentions_var" = "false" ] && [ "$mentions_script" = "false" ]' in func_body


def test_scan_cron_source_fails_closed_on_shell_wrapped_invocation(remote_script):
    func_body = remote_script.split("scan_cron_source() {")[1].split("\n          }")[0]
    assert "classify_wrapper_command" in func_body
    assert "WRAPPER_SIMPLE" in func_body


# =====================================================================
# 14d. Systemd effective-unit (base + drop-in) resolution
# =====================================================================

def test_systemd_scan_uses_effective_unit_view_not_raw_file_grep(remote_script):
    """Regression guard: a raw recursive grep over unit FILES misses
    drop-ins that override Environment= without repeating ExecStart= --
    `systemctl cat` returns the merged, effective configuration."""
    assert "scan_systemd_unit() {" in remote_script
    func_body = remote_script.split("scan_systemd_unit() {")[1].split("\n          }")[0]
    assert "systemctl cat" in func_body


def test_systemd_candidate_filter_checks_fragment_and_dropin_paths(remote_script):
    scan_launchers_body = remote_script.split("scan_launchers() {")[1].split("\n          }")[0]
    assert "FragmentPath" in scan_launchers_body
    assert "DropInPaths" in scan_launchers_body


def test_systemd_environment_file_indirection_fails_closed(remote_script):
    func_body = remote_script.split("scan_systemd_unit() {")[1].split("\n          }")[0]
    assert "EnvironmentFile=" in func_body
    assert "does not follow" in func_body


def test_systemd_unsupported_environment_form_fails_closed(remote_script):
    func_body = remote_script.split("scan_systemd_unit() {")[1].split("\n          }")[0]
    assert "unsupported Environment= form" in func_body
    assert "refusing to guess, failing closed" in func_body


def test_systemd_search_not_limited_to_two_hardcoded_directories(remote_script):
    """Regression guard: a prior revision only checked
    /etc/systemd/system and ~/.config/systemd/user directly, missing
    /run/systemd/system, vendor unit directories, and drop-ins located
    elsewhere -- using `systemctl`'s own enumeration/effective-unit
    view instead of manually walking directories covers all of them."""
    scan_launchers_body = remote_script.split("scan_launchers() {")[1].split("\n          }")[0]
    assert "systemctl list-unit-files" in scan_launchers_body
    assert "/etc/systemd/system\"" not in scan_launchers_body


# =====================================================================
# 14e. Launcher scan: cron sources, root crontab, bounding, reconciliation
# =====================================================================

def test_scan_launchers_covers_own_crontab_etc_crontab_and_cron_d(remote_script):
    func_body = remote_script.split("scan_launchers() {")[1].split("\n          reconcile_launchers() {")[0]
    assert "crontab -l" in func_body
    assert "/etc/crontab" in func_body
    assert "/etc/cron.d" in func_body


def test_scan_launchers_root_crontab_uses_bounded_non_interactive_sudo(remote_script):
    func_body = remote_script.split("scan_launchers() {")[1].split("\n          reconcile_launchers() {")[0]
    assert "sudo -n crontab -u root -l" in func_body
    assert "-i" not in func_body.split("sudo -n crontab -u root -l")[0][-20:]


def test_scan_launchers_fails_closed_when_root_crontab_unprovable(remote_script):
    func_body = remote_script.split("scan_launchers() {")[1].split("\n          reconcile_launchers() {")[0]
    assert "passwordless-sudo grant" in func_body
    assert "proven root-cron provenance" in func_body


def test_every_launcher_discovery_command_is_bounded_by_timeout(remote_script):
    """No individual launcher-discovery subprocess (crontab, sudo
    crontab, cat, find, systemctl) should be allowed to hang until the
    15-minute job timeout -- each is individually bounded."""
    for needle in (
        'timeout 5 crontab -l',
        'timeout 5 cat /etc/crontab',
        'timeout 5 find /etc/cron.d',
        'timeout 5 sudo -n crontab -u root -l',
        'timeout 5 systemctl list-unit-files',
        'timeout 5 systemctl show',
        'timeout 5 systemctl cat',
    ):
        assert needle in remote_script, needle


def test_timeout_availability_checked_before_launcher_scan(remote_script):
    check_index = remote_script.index("GNU coreutils 'timeout' is required")
    scan_index = remote_script.index("scan_launchers\n")
    assert check_index < scan_index


def test_reconcile_launchers_fails_closed_on_disagreement(remote_script):
    func_body = remote_script.split("reconcile_launchers() {")[1].split("\n          }")[0]
    assert '"$effective_lock" != "$LAUNCHER_LOCK_CONSENSUS"' in func_body
    assert "refusing to guess which is authoritative, failing closed" in func_body


def test_reconcile_launchers_fails_closed_on_root_mismatch(remote_script):
    func_body = remote_script.split("reconcile_launchers() {")[1].split("\n          }")[0]
    assert '"$effective_root" != "/opt/jobpulse"' in func_body


def test_no_launcher_case_uses_wrapper_default_only_after_full_scan(remote_script):
    func_body = remote_script.split("reconcile_launchers() {")[1].split("\n          }")[0]
    assert "No launcher of run_collection_cycle_safe.sh found in any inspected source" in func_body


def test_launcher_scan_precedes_collection_env_resolution(remote_script):
    launcher_index = remote_script.index("proving scheduler-level launcher provenance")
    resolve_index = remote_script.index("resolving effective production collection-cycle lock path")
    assert launcher_index < resolve_index


def test_launcher_scan_precedes_pull_and_mutation(remote_script):
    launcher_index = remote_script.index("proving scheduler-level launcher provenance")
    pull_index = remote_script.index('docker pull "$TARGET_IMAGE"')
    mutation_index = remote_script.index("API_MUTATION_STARTED=true")
    assert launcher_index < pull_index < mutation_index


def test_lock_acquired_non_blocking_and_fails_closed(remote_script):
    assert 'exec {CYCLE_LOCK_FD}>"$CYCLE_LOCK_PATH"' in remote_script
    assert 'flock -n "$CYCLE_LOCK_FD"' in remote_script
    assert "lock $CYCLE_LOCK_PATH is busy" in remote_script


def test_lock_held_across_entire_operation_released_only_in_exit_trap(remote_script):
    # the only fd-close for CYCLE_LOCK_FD must be inside the exit trap,
    # not anywhere in the main body
    main_body = remote_script.split("final_exit_trap()")[1].split("trap final_exit_trap EXIT")[1]
    assert 'CYCLE_LOCK_FD}>&-' not in main_body


def test_no_kill_or_stop_of_collection_processes(remote_script):
    # The phrase "read-only, no kill" appears in one explanatory echo
    # string describing what is deliberately NOT done -- strip that one
    # known phrase, then require zero remaining occurrences of "kill".
    sanitized = remote_script.replace("read-only, no kill", "")
    assert "docker kill" not in sanitized
    assert "kill -9" not in sanitized
    assert re.search(r"\bkill\b", sanitized) is None, "unexpected kill reference found"


# =====================================================================
# 15. Active-collector precondition
# =====================================================================

def test_active_collector_check_uses_docker_top_read_only(remote_script):
    assert "docker top jobpulse-api-prod" in remote_script
    # the detector uses `grep -E`, so the module paths appear with an
    # escaped dot (scripts\.process_search_demand_queue) in the pattern.
    # These are the REAL module names invoked by the actual production
    # wrapper (scripts/run_collection_cycle_safe.sh) -- auth preflight,
    # seed, process (which itself subprocess-invokes linkedin_plan_collect,
    # which itself subprocess-invokes collector_postgres), and the
    # reconciliation step -- not merely a subset recalled from memory.
    for needle in (
        r"scripts\.linkedin_auth_preflight",
        r"scripts\.seed_priority_coverage_queue",
        r"scripts\.process_search_demand_queue",
        r"scripts\.linkedin_plan_collect",
        r"scripts\.collector_postgres",
        r"scripts\.reconcile_priority_coverage",
    ):
        assert needle in remote_script, needle


def test_active_collector_check_matches_real_wrapper_module_invocations():
    """The detector's module names must be a superset of every
    `python -m scripts.<name>` (or `-m scripts.<name>`, matching how the
    wrapper actually invokes it with a leading `python` on its own line)
    invocation the real production wrapper
    (scripts/run_collection_cycle_safe.sh) performs, sourced from that
    file directly rather than trusted from memory."""
    wrapper_path = REPO_ROOT / "scripts" / "run_collection_cycle_safe.sh"
    wrapper_text = wrapper_path.read_text()
    invoked_modules = set(re.findall(r"(?:^|\s)python -m (scripts\.[a-zA-Z_]+)", wrapper_text, re.MULTILINE))
    # run_with_deadline is a transient exec wrapper immediately replaced
    # by the real target command it wraps, not itself a collection
    # operation worth detecting as a long-running process.
    invoked_modules.discard("scripts.run_with_deadline")
    assert invoked_modules, "expected at least one scripts.* module invocation in the wrapper"

    detection_line = next(
        line
        for line in WORKFLOW_PATH.read_text().splitlines()
        if "docker top jobpulse-api-prod" in line and "grep -E" in line
    )
    for module in invoked_modules:
        escaped = module.replace(".", r"\.")
        assert escaped in detection_line, f"{module} invoked by the real wrapper but missing from the detector"


def test_active_collector_check_precedes_pull_and_recreation(remote_script):
    collector_index = remote_script.index("docker top jobpulse-api-prod")
    pull_index = remote_script.index('docker pull "$TARGET_IMAGE"')
    recreate_index = remote_script.index('JOBPULSE_API_IMAGE="$TARGET_IMAGE" docker compose')
    assert collector_index < pull_index < recreate_index


# =====================================================================
# 16. Compose-hash equality gate
# =====================================================================

def test_compose_hash_equality_checked_before_recreation(remote_script):
    assert 'CURRENT_COMPOSE_SHA256="$(sha256sum "$COMPOSE_FILE"' in remote_script
    assert '"$CURRENT_COMPOSE_SHA256" != "$TARGET_COMPOSE_SHA256"' in remote_script
    hash_check_index = remote_script.index('"$CURRENT_COMPOSE_SHA256" != "$TARGET_COMPOSE_SHA256"')
    recreate_index = remote_script.index('JOBPULSE_API_IMAGE="$TARGET_IMAGE" docker compose')
    assert hash_check_index < recreate_index


def test_final_compose_hash_reverified_after_git_reset(remote_script):
    assert 'FINAL_COMPOSE_SHA256="$(sha256sum "$COMPOSE_FILE"' in remote_script
    assert '"$FINAL_COMPOSE_SHA256" != "$TARGET_COMPOSE_SHA256"' in remote_script
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')
    final_hash_index = remote_script.index('FINAL_COMPOSE_SHA256=')
    assert reset_index < final_hash_index


# =====================================================================
# 17. Rollback tag uniqueness / candidate SHA tag never overwritten
# =====================================================================

def test_rollback_tag_is_unique_and_run_scoped(remote_script):
    assert 'ROLLBACK_TAG="jobpulse-api-rollback:' in remote_script
    assert "docker image inspect \"$ROLLBACK_TAG\"" in remote_script
    assert "already exists -- refusing to reuse it" in remote_script


def test_rollback_tag_built_from_previous_image_id_not_target(remote_script):
    tag_line = next(
        line for line in _executable_lines(remote_script) if line.startswith('docker tag "$API_IMAGE_ID_BEFORE"')
    )
    assert "$ROLLBACK_TAG" in tag_line


def test_target_immutable_tag_never_retagged(remote_script):
    for line in _executable_lines(remote_script):
        if line.startswith("docker tag"):
            assert '"$TARGET_IMAGE"' not in line, f"target SHA tag retagged: {line!r}"


def test_rollback_tag_cleaned_up(remote_script):
    assert 'docker rmi "$ROLLBACK_TAG"' in remote_script


# =====================================================================
# 18. API-only reconciliation
# =====================================================================

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


def test_exactly_two_api_recreation_invocations_candidate_and_rollback(remote_script):
    """Exactly one candidate recreation (using $TARGET_IMAGE) and exactly
    one rollback recreation (using $ROLLBACK_TAG) -- two distinct code
    paths, never a third/stale invocation."""
    lines = _generic_api_recreation_lines(remote_script)
    assert len(lines) == 2, lines

    candidate_lines = [line for line in lines if "$TARGET_IMAGE" in line]
    rollback_lines = [line for line in lines if "$ROLLBACK_TAG" in line]
    assert len(candidate_lines) == 1, candidate_lines
    assert len(rollback_lines) == 1, rollback_lines


def test_every_api_recreation_uses_no_build_no_deps(remote_script):
    for line in _generic_api_recreation_lines(remote_script):
        assert "--no-build" in line, line
        assert "--no-deps" in line, line


def test_no_db_frontend_tor_reconciliation(remote_script):
    for line in _executable_lines(remote_script):
        if "docker compose" not in line:
            continue
        if not re.search(r"(?:^|\s)up(?:\s|$)", line):
            continue
        assert re.search(r"\bdb\b", line) is None, line
        assert re.search(r"\bfrontend\b", line) is None, line
        assert re.search(r"\btor\b", line) is None, line


def test_no_pull_of_db_frontend_tor(remote_script):
    for line in _executable_lines(remote_script):
        if "docker compose" in line and re.search(r"(?:^|\s)pull(?:\s|$)", line):
            assert re.search(r"\bdb\b", line) is None, line
            assert re.search(r"\bfrontend\b", line) is None, line
            assert re.search(r"\btor\b", line) is None, line


def test_no_restart_of_db_frontend_tor(remote_script):
    assert "docker restart" not in remote_script
    assert "docker compose restart" not in remote_script


# =====================================================================
# 19. Bounded health checks
# =====================================================================

def _executable_curl_lines(script: str):
    return [line for line in _executable_lines(script) if line.startswith("curl") or " curl " in line or line.strip().startswith("if curl")]


def test_every_health_curl_has_finite_connect_and_max_time(remote_script):
    curl_lines = [line for line in remote_script.splitlines() if "curl " in line and "health" in line]
    assert curl_lines, "no health curl lines found"
    for line in curl_lines:
        assert "--connect-timeout" in line, line
        assert "--max-time" in line, line


def test_direct_and_nginx_health_paths_both_checked(remote_script):
    assert "http://127.0.0.1:8000/health" in remote_script
    assert "http://127.0.0.1/api/health" in remote_script


def test_bounded_attempt_loops_use_seq_not_infinite(remote_script):
    health_check_blocks = re.findall(r"for attempt in \$\(seq 1 (\d+)\); do", remote_script)
    assert health_check_blocks, "no bounded attempt loops found"
    for bound in health_check_blocks:
        assert int(bound) <= 30


def test_health_checks_occur_both_pre_and_post_git_reset(remote_script):
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')
    pre_reset = remote_script[:reset_index]
    post_reset = remote_script[reset_index:]
    assert "http://127.0.0.1:8000/health" in pre_reset
    assert "http://127.0.0.1:8000/health" in post_reset
    assert "http://127.0.0.1/api/health" in pre_reset
    assert "http://127.0.0.1/api/health" in post_reset


# =====================================================================
# 20. Actual transport-mode validation (not absence-only)
# =====================================================================

def test_get_search_transport_mode_actually_invoked_in_container(remote_script):
    assert "from app.config import get_search_transport_mode" in remote_script
    assert "docker exec jobpulse-api-prod python -c" in remote_script


def test_transport_mode_check_requires_direct(remote_script):
    assert '"$TRANSPORT_MODE" != "direct"' in remote_script
    assert '"$FINAL_TRANSPORT_MODE" != "direct"' in remote_script


def test_tor_enabled_checked_via_printenv_not_full_dump(remote_script):
    assert "docker exec jobpulse-api-prod printenv TOR_ENABLED" in remote_script


def test_no_proxy_mode_ever_invoked(remote_script):
    assert 'get_search_transport("proxy")' not in remote_script
    assert "get_search_transport('proxy')" not in remote_script
    assert "SEARCH_TRANSPORT=proxy" not in remote_script
    assert "SEARCH_TRANSPORT: proxy" not in remote_script


# =====================================================================
# 21. DB / frontend / Tor pre/post invariants
# =====================================================================

def test_unrelated_container_states_captured_and_compared(remote_script):
    assert "capture_container_state jobpulse-postgres-prod" in remote_script
    assert "capture_container_state jobpulse-frontend-prod" in remote_script
    assert "capture_container_state jobpulse-tor-prod" in remote_script
    assert '"$DB_STATE_AFTER" != "$DB_STATE_BEFORE"' in remote_script
    assert '"$FRONTEND_STATE_AFTER" != "$FRONTEND_STATE_BEFORE"' in remote_script
    assert '"$TOR_STATE_AFTER" != "$TOR_STATE_BEFORE"' in remote_script


def test_capture_container_state_includes_all_required_fields(remote_script):
    # Naive split on the first "}" breaks on the Go template braces
    # (`{{.Id}}`) inside the function body -- bound the extraction to the
    # next top-level function definition instead.
    after_start = remote_script.split("capture_container_state() {", 1)[1]
    func_body = after_start.split("final_exit_trap() {", 1)[0]
    # Health is looked up via the missing-key-safe `index .State "Health"`
    # form (see test_capture_container_state_uses_missing_key_safe_health_lookup
    # below) rather than direct `.State.Health` field access, so it is
    # checked separately from the other plain-field-access assertions.
    for field in (".Id", ".RestartCount", ".State.Running", ".Image", ".Config.Image"):
        assert field in func_body, field
    assert 'index .State "Health"' in func_body


def test_db_and_frontend_required_present_and_running_before_mutation():
    """An ABSENT before-snapshot must not be allowed to silently compare
    equal to another ABSENT after-snapshot -- DB and frontend are
    required to already exist and be running (healthy, where a
    healthcheck is defined) before anything is touched. Tor stays
    optional: its absence is not itself an error, only a state change is."""
    text = WORKFLOW_PATH.read_text()
    assert '"$DB_STATE_BEFORE" = "ABSENT"' in text
    assert '"$FRONTEND_STATE_BEFORE" = "ABSENT"' in text
    assert '"$TOR_STATE_BEFORE" = "ABSENT"' not in text
    assert 'DB_RUNNING_BEFORE="$(docker inspect -f \'{{.State.Running}}\' jobpulse-postgres-prod)"' in text
    assert 'FRONTEND_RUNNING_BEFORE="$(docker inspect -f \'{{.State.Running}}\' jobpulse-frontend-prod)"' in text
    assert '"$DB_RUNNING_BEFORE" != "true"' in text
    assert '"$FRONTEND_RUNNING_BEFORE" != "true"' in text


def test_db_precondition_checked_before_lock_and_mutation(remote_script):
    db_check_index = remote_script.index('"$DB_STATE_BEFORE" = "ABSENT"')
    lock_index = remote_script.index('exec {CYCLE_LOCK_FD}>"$CYCLE_LOCK_PATH"')
    mutation_index = remote_script.index("API_MUTATION_STARTED=true")
    assert db_check_index < lock_index < mutation_index


# =====================================================================
# 22. Ordering: lock < pull < recreate < validate < git reset < final
# =====================================================================

def test_ordering_lock_before_pull_before_recreate_before_reset(remote_script):
    lock_index = remote_script.index('exec {CYCLE_LOCK_FD}>"$CYCLE_LOCK_PATH"')
    pull_index = remote_script.index('docker pull "$TARGET_IMAGE"')
    recreate_index = remote_script.index('JOBPULSE_API_IMAGE="$TARGET_IMAGE" docker compose')
    candidate_recreated_flag_index = remote_script.index("CANDIDATE_API_RECREATED=true")
    transport_check_index = remote_script.index("TRANSPORT_MODE=\"$(docker exec")
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')
    reset_flag_index = remote_script.index("GIT_RESET_DONE=true")
    final_transport_check_index = remote_script.index("FINAL_TRANSPORT_MODE=")
    success_index = remote_script.index("WORKFLOW_SUCCESS=true")

    assert (
        lock_index
        < pull_index
        < recreate_index
        < candidate_recreated_flag_index
        < transport_check_index
        < reset_index
        < reset_flag_index
        < final_transport_check_index
        < success_index
    ), "critical ordering invariant violated: a future change moved a step out of the required sequence"


def test_candidate_validation_precedes_git_reset_specifically(remote_script):
    """Regression guard: catches a future change that moves
    `git reset --hard $TARGET_SHA` earlier, before the candidate API has
    been proven healthy/direct/topology-unchanged."""
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')

    required_before_reset = [
        'CANDIDATE_HEALTHY=false',
        '"$API_IMAGE_REF_AFTER" != "$TARGET_IMAGE"',
        '"$API_IMAGE_ID_AFTER" != "$TARGET_IMAGE_ID"',
        "TRANSPORT_MODE=\"$(docker exec",
        '"$DB_STATE_AFTER" != "$DB_STATE_BEFORE"',
    ]
    for marker in required_before_reset:
        marker_index = remote_script.index(marker)
        assert marker_index < reset_index, f"{marker!r} must precede git reset but does not"


def test_git_reset_only_reachable_after_candidate_recreated_flag_set(remote_script):
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')
    flag_index = remote_script.index("CANDIDATE_API_RECREATED=true")
    assert flag_index < reset_index


# =====================================================================
# 22b. Partial-mutation rollback-eligibility flags (Phase 3.4N-B)
# =====================================================================

def test_api_mutation_started_flag_set_immediately_before_candidate_up(remote_script):
    """Regression guard: `docker compose ... up` can itself partially
    recreate/change the container and then still exit non-zero. The
    mutation-started flag must be set BEFORE that command, not after it
    returns, and no other executable line may sit between the flag
    assignment and the mutating command."""
    flag_index = remote_script.index("API_MUTATION_STARTED=true")
    up_index = remote_script.index('JOBPULSE_API_IMAGE="$TARGET_IMAGE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" up -d --no-build --no-deps api')
    assert flag_index < up_index

    between = remote_script[flag_index + len("API_MUTATION_STARTED=true") : up_index]
    for line in _executable_lines(between):
        pytest.fail(f"unexpected executable line between API_MUTATION_STARTED=true and the candidate up command: {line!r}")


def test_git_mutation_started_flag_set_immediately_before_target_reset(remote_script):
    """Same partial-mutation guard as API_MUTATION_STARTED, for the git
    checkout mutation: `git reset --hard` can itself partially apply and
    then still exit non-zero."""
    flag_index = remote_script.index("GIT_MUTATION_STARTED=true")
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')
    assert flag_index < reset_index

    between = remote_script[flag_index + len("GIT_MUTATION_STARTED=true") : reset_index]
    for line in _executable_lines(between):
        pytest.fail(f"unexpected executable line between GIT_MUTATION_STARTED=true and the target git reset: {line!r}")


def test_mutation_started_flags_initialized_false(remote_script):
    init_block = remote_script.split("rollback_api() {")[0]
    assert "API_MUTATION_STARTED=false" in init_block
    assert "GIT_MUTATION_STARTED=false" in init_block


# =====================================================================
# 23. Failure-path structure: candidate failure -> rollback API -> restore git
# =====================================================================

def test_exit_trap_performs_rollback_only_on_non_success(remote_script):
    trap_body = remote_script.split("final_exit_trap() {")[1].split("trap final_exit_trap EXIT")[0]
    assert 'if [ "$WORKFLOW_SUCCESS" != "true" ]; then' in trap_body


def test_exit_trap_calls_rollback_api_before_rollback_git(remote_script):
    trap_body = remote_script.split("final_exit_trap() {")[1].split("trap final_exit_trap EXIT")[0]
    rollback_api_call_index = trap_body.index("rollback_api")
    rollback_git_call_index = trap_body.index("rollback_git")
    assert rollback_api_call_index < rollback_git_call_index


def test_trap_keys_api_rollback_eligibility_off_mutation_started_not_success_flag(remote_script):
    """Regression guard for the Phase 3.4N-B partial-mutation fix: the
    trap must decide API rollback eligibility from API_MUTATION_STARTED
    (set before the mutating command), never from the post-success-only
    CANDIDATE_API_RECREATED flag -- a future change that swaps this back
    would reintroduce the gap where a `docker compose up` that partially
    applies and then exits non-zero is treated as "nothing to roll
    back"."""
    trap_body = remote_script.split("final_exit_trap() {")[1].split("trap final_exit_trap EXIT")[0]
    assert 'if [ "$API_MUTATION_STARTED" = "true" ]; then' in trap_body
    assert 'if [ "$CANDIDATE_API_RECREATED" = "true" ]; then' not in trap_body


def test_trap_keys_git_recovery_eligibility_off_mutation_started_not_reset_done_flag(remote_script):
    """Same regression guard as the API one, for the git-recovery branch:
    the trap must key off GIT_MUTATION_STARTED (set before `git reset
    --hard`), never off the post-success-only GIT_RESET_DONE flag."""
    trap_body = remote_script.split("final_exit_trap() {")[1].split("trap final_exit_trap EXIT")[0]
    assert 'if [ "$GIT_MUTATION_STARTED" = "true" ]; then' in trap_body
    assert 'if [ "$GIT_RESET_DONE" = "true" ]; then' not in trap_body


def test_exit_code_captured_before_rollback_commands_run(remote_script):
    trap_body = remote_script.split("final_exit_trap() {")[1]
    local_ec_index = trap_body.index("local ec=$?")
    rollback_call_index = trap_body.index("rollback_api")
    exit_ec_index = trap_body.rindex('exit "$ec"')
    assert local_ec_index < rollback_call_index < exit_ec_index


def test_rollback_git_targets_expected_current_sha(remote_script):
    func_body = remote_script.split("rollback_git() {")[1].split("\n          }")[0]
    assert 'git reset --hard "$EXPECTED_CURRENT_SHA"' in func_body


def test_rollback_api_verifies_image_id_matches_previous(remote_script):
    func_body = remote_script.split("rollback_api() {")[1].split("\n          }")[0]
    assert '"$rb_image_id" != "$API_IMAGE_ID_BEFORE"' in func_body


def test_rollback_api_reproves_direct_and_no_tor_invariant(remote_script):
    """The old (pre-upgrade) image predates the reviewed SearchTransport
    abstraction and cannot be trusted to expose
    get_search_transport_mode() -- so rollback must instead re-prove the
    same direct/no-Tor invariant via the same narrow env-inspection
    technique used everywhere else in this runner, never a full env
    dump, and never by assuming get_search_transport_mode() exists in
    the old image."""
    assert "rb_search_transport=" in remote_script
    assert "rb_tor_enabled=" in remote_script
    assert '"$rb_search_transport" != "direct"' in remote_script
    assert '"$rb_tor_enabled" != "false"' in remote_script
    rollback_index = remote_script.index("rollback_api() {")
    search_transport_index = remote_script.index("rb_search_transport=")
    tor_enabled_index = remote_script.index("rb_tor_enabled=")
    assert rollback_index < search_transport_index
    assert rollback_index < tor_enabled_index


def test_get_search_transport_mode_never_invoked_for_rollback():
    """get_search_transport_mode() must only ever be called against the
    already-reviewed candidate/final image, never against the old
    148bd-era rollback image -- exactly two invocations (the pre-reset
    candidate check and the final post-reset re-check), never a third
    one added for rollback verification."""
    text = WORKFLOW_PATH.read_text()
    assert text.count("from app.config import get_search_transport_mode") == 2


def test_rollback_api_includes_bounded_nginx_health_check(remote_script):
    func_body = remote_script.split("rollback_api() {")[1].split("\n          }")[0]
    assert "http://127.0.0.1/api/health" in func_body
    assert "rb_nginx_healthy" in func_body
    assert '"$rb_nginx_healthy" != "true"' in func_body


def test_no_retry_of_candidate_no_second_deployment_attempt(remote_script):
    assert "for attempt in" in remote_script  # bounded health polling exists
    # but there must be no loop that re-invokes the candidate recreation
    # or the pull itself
    recreate_lines = _generic_api_recreation_lines(remote_script)
    assert len(recreate_lines) == 2  # candidate + rollback, never more
    pull_count = sum(1 for line in _executable_lines(remote_script) if 'docker pull "$TARGET_IMAGE"' in line)
    assert pull_count == 1


# =====================================================================
# 24. Untracked artifact existence invariant (before/after, not "must exist")
# =====================================================================

def test_untracked_artifact_existence_captured_before_any_mutation(remote_script):
    """Regression guard: an artifact that was already absent before this
    run ever started must not become a late post-candidate failure just
    because it is still absent -- so existence is captured as a
    before-snapshot, not asserted unconditionally."""
    assert "TOR_SECRET_PATH_EXISTED_BEFORE=false" in remote_script
    assert "STATE_PATH_EXISTED_BEFORE=false" in remote_script
    before_index = remote_script.index("TOR_SECRET_PATH_EXISTED_BEFORE=false")
    mutation_index = remote_script.index("API_MUTATION_STARTED=true")
    assert before_index < mutation_index


def test_untracked_artifact_existence_compared_after_git_reset(remote_script):
    assert "TOR_SECRET_PATH_EXISTS_AFTER=false" in remote_script
    assert "STATE_PATH_EXISTS_AFTER=false" in remote_script
    reset_index = remote_script.index('git reset --hard "$TARGET_SHA"')
    after_index = remote_script.index("TOR_SECRET_PATH_EXISTS_AFTER=false")
    assert reset_index < after_index
    assert '"$TOR_SECRET_PATH_EXISTS_AFTER" != "$TOR_SECRET_PATH_EXISTED_BEFORE"' in remote_script
    assert '"$STATE_PATH_EXISTS_AFTER" != "$STATE_PATH_EXISTED_BEFORE"' in remote_script


def test_no_unconditional_untracked_artifact_existence_requirement(remote_script):
    """A prior revision unconditionally required these paths to exist
    after reset, which would incorrectly fail if one was already absent
    before this run started. That unconditional form must not return."""
    assert 'for artifact in .tor_control_password state; do' not in remote_script


def test_no_secret_file_contents_read(remote_script):
    assert "cat /opt/jobpulse/.tor_control_password" not in remote_script
    assert "cat .tor_control_password" not in remote_script


# =====================================================================
# 25. No LinkedIn / no collector execution
# =====================================================================

def test_no_linkedin_target_anywhere(workflow_text):
    assert "linkedin.com" not in workflow_text.lower()


def test_no_collector_module_invocation(remote_script):
    # the module names may appear as detection-target strings (grep -E
    # patterns) but never as an executable invocation (python -m ...)
    for line in _executable_lines(remote_script):
        for forbidden_invocation in (
            "python -m scripts.collector_postgres",
            "python -m scripts.linkedin_plan_collect",
            "python -m scripts.process_search_demand_queue",
            "python -m scripts.run_collection_cycle",
        ):
            assert forbidden_invocation not in line, line


def test_collector_strings_only_appear_in_detection_pattern(remote_script):
    detection_line = next(
        line for line in remote_script.splitlines() if "process_search_demand_queue" in line and "grep -E" in line
    )
    assert r"scripts\.linkedin_auth_preflight" in detection_line
    assert r"scripts\.seed_priority_coverage_queue" in detection_line
    assert r"scripts\.linkedin_plan_collect" in detection_line
    assert r"scripts\.collector_postgres" in detection_line
    assert r"scripts\.reconcile_priority_coverage" in detection_line
    assert r"scripts\.run_collection_cycle" in detection_line


# =====================================================================
# 26. No proxy / Tor-control operations
# =====================================================================

def test_no_control_plane_operations(runner_script, remote_script):
    # "ControlPort" is permitted in one explanatory comment (listing what
    # is deliberately never transferred), matching the already-reviewed
    # Phase 3.4M canary's own comment style -- only executable lines
    # (never comments) are checked for actual operational use.
    executable = list(_executable_lines(runner_script)) + list(_executable_lines(remote_script))
    for line in executable:
        for forbidden in (
            "9050",
            "9051",
            "NEWNYM",
            "ControlPort",
            "rotate_circuit",
            "request_new_identity",
            "TOR_CONTROL_HOST",
            "TOR_CONTROL_PORT",
        ):
            assert forbidden not in line, f"{forbidden!r} in executable line: {line!r}"


def test_no_tor_socks_config_set(remote_script, runner_script):
    # TOR_SOCKS_HOST/PORT legitimately appear as literal strings inside
    # the *forbidden-value detector* in the separate "Verify target
    # compose invariants" step (checking these are absent from the
    # target compose's api service) -- that step is not part of
    # remote_script/runner_script, so neither should ever mention them.
    assert "TOR_SOCKS_HOST" not in remote_script
    assert "TOR_SOCKS_PORT" not in remote_script
    assert "TOR_SOCKS_HOST" not in runner_script
    assert "TOR_SOCKS_PORT" not in runner_script


def test_only_tor_enabled_read_never_set_true(remote_script):
    assert "TOR_ENABLED=true" not in remote_script
    assert 'TOR_ENABLED: "true"' not in remote_script


# =====================================================================
# 27. Zero diff to existing deploy workflow / script
# =====================================================================

def test_deploy_workflow_and_script_not_touched_by_this_test_file():
    """This test file itself must never reference/modify the existing
    generic deploy path -- a structural reminder, not a git-diff check
    (git-diff verification is performed by the file-scope audit outside
    pytest)."""
    this_file_text = Path(__file__).read_text()
    assert "deploy_prod_from_ghcr.sh" not in this_file_text.split("Structural regression guard")[1].split("This workflow")[0]


# =====================================================================
# 28. Runbook coverage
# =====================================================================

def test_runbook_has_phase_3_4n_b_section():
    runbook = (REPO_ROOT / "docs" / "PRODUCTION_RUNBOOK.md").read_text()
    assert "Phase 3.4N-B" in runbook
    assert REVIEWED_TARGET_SHA in runbook
    assert EXPECTED_CURRENT_SHA in runbook
    assert "UPGRADE_DIRECT_RUNTIME" in runbook


def test_runbook_documents_remote_temp_file_cleanup_limitation():
    """The remote script deletes itself in its own EXIT trap only AFTER
    it starts -- if SCP succeeds but SSH execution fails before the
    remote script starts (or the job is forcibly terminated before the
    trap runs), the RUN_ID-scoped remote script can remain under /tmp.
    This must be documented, not silently assumed away."""
    runbook = (REPO_ROOT / "docs" / "PRODUCTION_RUNBOOK.md").read_text()
    section = runbook.split("Phase 3.4N-B", 1)[1]
    assert "/tmp/jobpulse-direct-runtime-upgrade-remote-" in section
    assert "RUN_ID" in section
    assert "no secret material" in section or "contains no secret" in section
    # must not overclaim cleanup as unconditionally guaranteed
    assert "cleanup is guaranteed in every" not in section


def test_runbook_lock_path_wording_is_not_overclaimed():
    runbook = (REPO_ROOT / "docs" / "PRODUCTION_RUNBOOK.md").read_text()
    assert "proven production default" not in runbook
    assert "no override found anywhere in the codebase" not in runbook
    # a prior revision claimed sourcing .collection.env in a subshell
    # was read-only -- it is not, and the runbook must not repeat that claim
    assert "read-only subshell" not in runbook


def test_runbook_documents_non_executing_parser_and_order_awareness():
    runbook = (REPO_ROOT / "docs" / "PRODUCTION_RUNBOOK.md").read_text()
    section = runbook.split("Phase 3.4N-B", 1)[1]
    assert "never sourced" in section or "never sourced or" in section
    assert "order-aware" in section
    assert "sudo -n crontab -u root -l" in section
    assert "passwordless-sudo grant" in section
    assert "systemctl cat" in section


def test_runbook_documents_root_cron_privilege_requirement_honestly():
    """Root-crontab provenance requires a privilege that does not exist
    in this repo's evidence -- the runbook must say so plainly, not
    claim the check is complete while quietly assuming the grant."""
    runbook = (REPO_ROOT / "docs" / "PRODUCTION_RUNBOOK.md").read_text()
    section = runbook.split("Phase 3.4N-B", 1)[1]
    assert "does not currently exist" in section
    assert "fail-closed outcome, not a defect" in section or "not a defect" in section


# =====================================================================
# 29. TOR_ENABLED allowlist narrowness
# =====================================================================

def test_tor_enabled_allowlist_entry_is_narrow():
    dark_launch_test = (REPO_ROOT / "tests" / "test_tor_production_dark_launch.py").read_text()
    assert ".github/workflows/production-direct-runtime-upgrade.yml" in dark_launch_test
    # ensure it's a single, specific path entry, not a wildcard
    allowlist_block = dark_launch_test.split("_ALLOWED_TOR_ENABLED_SETTERS = {")[1].split("}")[0]
    assert "*" not in allowlist_block


# =====================================================================
# 29b. Behavioral (executable) regression tests
#
# Everything above this point is STRUCTURAL: it parses the committed
# YAML/bash as text and proves the right code, ordering, and wording
# exist. It cannot catch a regression in the actual *runtime behavior*
# of the scheduler-provenance parser -- e.g. cron order-awareness
# silently degrading back to a global union, or a hostile
# `.collection.env` value silently being accepted.
#
# These tests extract the ACTUAL function definitions (parse_assignment_line
# through resolve_final_lock_path) verbatim from the real remote heredoc
# and execute them in isolated bash subprocesses against fixture inputs
# -- no separate Python reimplementation of the parsing semantics. Every
# subprocess runs with a PATH restricted to a per-test temp bin directory
# (sentinel `docker`/`git`/`flock` that abort loudly if reached, plus any
# fixture `crontab`/`sudo`/`systemctl` a test supplies) prepended to the
# real PATH -- fully offline, no SSH/sudo/network/production contact.
# =====================================================================

import os
import shlex
import subprocess as sp


@pytest.fixture(scope="module")
def extracted_functions_script(remote_script) -> str:
    """The real parser/scanner function DEFINITIONS, verbatim, from
    `parse_assignment_line() {` through the end of
    `resolve_final_lock_path() {...}` -- immediately before
    `final_exit_trap() {` begins. No reimplementation."""
    start = remote_script.index("parse_assignment_line() {")
    end = remote_script.index("final_exit_trap() {")
    return remote_script[start:end]


@pytest.fixture()
def harness_bin(tmp_path):
    """Sentinel executables for every mutating command a parser-only
    test must never reach. Any invocation prints a distinctive FATAL
    marker and exits 99, so `_assert_mutation_sentinels_untouched` can
    catch an extraction mistake that accidentally pulls in mutation
    code."""
    bin_dir = tmp_path / "sentinel_bin"
    bin_dir.mkdir()
    for mutating_cmd in ("docker", "git", "flock"):
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


def _run_harness(extracted_functions_script, harness_bin, body: str, extra_path_dirs=(), extra_env=None):
    """Writes <extracted_functions_script> + <body> to a script and runs
    it via `bash -c` in a subprocess. PATH is extra_path_dirs (fixture
    commands, checked first) then harness_bin (mutation sentinels) then
    the real PATH (for ordinary utilities: cat/find/grep/awk/timeout) --
    real system crontab/sudo/systemctl are only reached if a test does
    NOT supply a fixture replacement, which no test here relies on for
    its actual assertions."""
    script = "#!/usr/bin/env bash\nset -euo pipefail\n" + extracted_functions_script + "\n" + body + "\n"
    path_dirs = ":".join(str(p) for p in (*extra_path_dirs, harness_bin))
    env = dict(os.environ)
    env["PATH"] = f"{path_dirs}:{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    result = sp.run(["bash", "-c", script], capture_output=True, text=True, timeout=15, env=env)
    return result


def _assert_mutation_sentinels_untouched(result):
    assert "FATAL_TEST_HARNESS_MUTATION_SENTINEL" not in result.stdout, result.stdout
    assert "FATAL_TEST_HARNESS_MUTATION_SENTINEL" not in result.stderr, result.stderr


def _parse_kv_lines(stdout: str) -> dict:
    values = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


# ---------------------------------------------------------------------
# Cases A/B/C/D/E: order-aware cron scanning (scan_cron_source) and
# multi-launcher reconciliation (reconcile_launchers)
# ---------------------------------------------------------------------

def _scan_cron_source_body(content_file, label="fixture-source", mode="user"):
    return f'''
DISCOVERED_LAUNCHERS=()
content="$(cat {shlex.quote(str(content_file))})"
scan_cron_source {shlex.quote(label)} {shlex.quote(mode)} "$content"
printf 'COUNT=%s\\n' "${{#DISCOVERED_LAUNCHERS[@]}}"
for entry in "${{DISCOVERED_LAUNCHERS[@]}}"; do
  printf 'ENTRY=%s\\n' "$entry"
done
'''


def test_behavior_case_a_assignment_before_job_applies(extracted_functions_script, harness_bin, tmp_path):
    """Case A: an assignment BEFORE the wrapper job line applies to it."""
    content_file = tmp_path / "cron_a.txt"
    content_file.write_text(
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/a.lock\n"
        "*/5 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, _scan_cron_source_body(content_file))
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    entries = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("ENTRY=")]
    assert entries == ["|/a.lock|fixture-source"]


def test_behavior_case_b_assignment_after_job_does_not_retroactively_apply(
    extracted_functions_script, harness_bin, tmp_path
):
    """Case B: an assignment AFTER the wrapper job line must NOT affect
    that earlier job -- this is the specific regression a global-union
    (rather than order-aware) parser would introduce."""
    content_file = tmp_path / "cron_b.txt"
    content_file.write_text(
        "*/5 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/b.lock\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, _scan_cron_source_body(content_file))
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    entries = [line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("ENTRY=")]
    # empty root, empty lock: the job was recorded with whatever was in
    # scope AT THAT LINE, which was nothing yet -- /b.lock must not appear
    assert entries == ["||fixture-source"]
    assert "/b.lock" not in result.stdout


def test_behavior_case_c_differing_launcher_locks_fail_reconciliation(
    extracted_functions_script, harness_bin, tmp_path
):
    """Case C: two wrapper jobs in the same source resolve to different
    effective lock paths -- scan_cron_source itself succeeds (each job
    is individually well-formed), but reconcile_launchers must fail
    closed rather than picking a winner."""
    content_file = tmp_path / "cron_c.txt"
    content_file.write_text(
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/a.lock\n"
        "*/5 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/b.lock\n"
        "*/10 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
    )
    body = _scan_cron_source_body(content_file) + "\nreconcile_launchers\nprintf 'RECONCILE_SUCCEEDED\\n'\n"
    result = _run_harness(extracted_functions_script, harness_bin, body)
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "COUNT=2" in result.stdout, "order-aware scan must still discover both distinct launchers"
    assert "RECONCILE_SUCCEEDED" not in result.stdout
    assert "refusing to guess which is authoritative" in result.stderr


def test_behavior_case_d_same_effective_lock_reconciliation_succeeds(
    extracted_functions_script, harness_bin, tmp_path
):
    """Case D: two wrapper jobs with the SAME effective ROOT/lock are an
    acceptable, non-ambiguous reconciliation."""
    content_file = tmp_path / "cron_d.txt"
    content_file.write_text(
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/a.lock\n"
        "*/5 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
        "*/10 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
    )
    body = (
        _scan_cron_source_body(content_file)
        + "\nreconcile_launchers\nprintf 'RECONCILE_SUCCEEDED\\n'\nprintf 'CONSENSUS=%s\\n' \"$LAUNCHER_LOCK_CONSENSUS\"\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, body)
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert "COUNT=2" in result.stdout
    assert "RECONCILE_SUCCEEDED" in result.stdout
    assert "CONSENSUS=/a.lock" in result.stdout


@pytest.mark.parametrize(
    "job_line",
    [
        "*/5 * * * * bash -c '/opt/jobpulse/scripts/run_collection_cycle_safe.sh'",
        "*/5 * * * * sh -c '/opt/jobpulse/scripts/run_collection_cycle_safe.sh'",
        "*/5 * * * * env FOO=bar /opt/jobpulse/scripts/run_collection_cycle_safe.sh",
        "*/5 * * * * /opt/jobpulse/scripts/run_collection_cycle_safe.sh && echo done",
    ],
    ids=["bash-c", "sh-c", "env-prefixed", "chained-with-and"],
)
def test_behavior_case_e_unsupported_shell_wrapped_invocation_fails_closed(
    extracted_functions_script, harness_bin, tmp_path, job_line
):
    """Case E: the wrapper hidden behind a shell-wrapped/chained
    invocation the restricted grammar cannot safely understand must
    fail closed, never be guessed at."""
    content_file = tmp_path / "cron_e.txt"
    content_file.write_text(job_line + "\n")
    result = _run_harness(extracted_functions_script, harness_bin, _scan_cron_source_body(content_file))
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "refusing to guess" in result.stderr


# ---------------------------------------------------------------------
# Required-source-failure: an unreadable/unprovable source must fail
# closed, never be interpreted as "no launcher"
# ---------------------------------------------------------------------

def test_behavior_root_crontab_unavailable_fails_closed_before_lock_resolution(
    extracted_functions_script, harness_bin, tmp_path
):
    """Simulates the realistic production case: no passwordless-sudo
    grant exists for `sudo -n crontab -u root -l`. scan_launchers must
    fail closed strictly INSIDE the launcher scan -- never reachable to
    reconcile_launchers, let alone lock resolution -- and must not
    silently interpret "could not check root's crontab" as "root has no
    crontab"."""
    fixture_bin = tmp_path / "fixture_bin"
    fixture_bin.mkdir()
    _write_fake_executable(
        fixture_bin,
        "crontab",
        # this account's own crontab: deterministic "none" regardless of
        # whatever the real test-runner host happens to have configured
        'echo "no crontab for $(whoami)" >&2\nexit 1\n',
    )
    _write_fake_executable(
        fixture_bin,
        "sudo",
        # non-interactive sudo fails because no NOPASSWD rule exists --
        # never prompts, never actually attempts privilege escalation
        'if [ "$1" = "-n" ]; then\n  echo "sudo: a password is required" >&2\n  exit 1\nfi\nexit 1\n',
    )
    body = (
        "scan_launchers\n"
        "printf 'SCAN_LAUNCHERS_SUCCEEDED\\n'\n"
        "reconcile_launchers\n"
        "printf 'RECONCILE_SUCCEEDED\\n'\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, body, extra_path_dirs=(fixture_bin,))
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "SCAN_LAUNCHERS_SUCCEEDED" not in result.stdout
    assert "RECONCILE_SUCCEEDED" not in result.stdout
    assert "passwordless-sudo grant" in result.stderr
    assert "proven root-cron provenance" in result.stderr


# ---------------------------------------------------------------------
# Systemd effective-unit / drop-in scanning (scan_systemd_unit)
# ---------------------------------------------------------------------

def test_behavior_systemd_drop_in_environment_override_is_observed(
    extracted_functions_script, harness_bin, tmp_path
):
    """A drop-in need not itself contain the wrapper script name to
    affect the base service -- `systemctl cat` returns the merged,
    effective view, and this must be what the scanner reads, not a raw
    grep of individual unit files."""
    fixture_bin = tmp_path / "fixture_bin"
    fixture_bin.mkdir()
    unit_fixture = tmp_path / "unit_effective.txt"
    unit_fixture.write_text(
        "# /etc/systemd/system/jobpulse-collection.service\n"
        "[Service]\n"
        "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
        "# /etc/systemd/system/jobpulse-collection.service.d/override.conf\n"
        "Environment=JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/override.lock\n"
    )
    _write_fake_executable(
        fixture_bin,
        "systemctl",
        f'if [ "$1" = "cat" ]; then\n  cat {shlex.quote(str(unit_fixture))}\nfi\n',
    )
    body = (
        "DISCOVERED_LAUNCHERS=()\n"
        "scan_systemd_unit jobpulse-collection.service\n"
        'printf \'ENTRY=%s\\n\' "${DISCOVERED_LAUNCHERS[0]}"\n'
    )
    result = _run_harness(extracted_functions_script, harness_bin, body, extra_path_dirs=(fixture_bin,))
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert "ENTRY=|/override.lock|systemd:jobpulse-collection.service" in result.stdout


def test_behavior_systemd_environment_file_fails_closed(extracted_functions_script, harness_bin, tmp_path):
    """EnvironmentFile= indirection must never be sourced/eval'd as
    another config file -- the scanner has no supported, non-executing
    parse for it, so it fails closed."""
    fixture_bin = tmp_path / "fixture_bin"
    fixture_bin.mkdir()
    unit_fixture = tmp_path / "unit_effective.txt"
    unit_fixture.write_text(
        "[Service]\n"
        "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
        "EnvironmentFile=/opt/jobpulse/.collection.env\n"
    )
    _write_fake_executable(
        fixture_bin,
        "systemctl",
        f'if [ "$1" = "cat" ]; then\n  cat {shlex.quote(str(unit_fixture))}\nfi\n',
    )
    body = "DISCOVERED_LAUNCHERS=()\nscan_systemd_unit jobpulse-collection.service\n"
    result = _run_harness(extracted_functions_script, harness_bin, body, extra_path_dirs=(fixture_bin,))
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "EnvironmentFile=" in result.stderr
    assert "does not follow" in result.stderr


def test_behavior_systemd_unsupported_multi_variable_environment_line_fails_closed(
    extracted_functions_script, harness_bin, tmp_path
):
    fixture_bin = tmp_path / "fixture_bin"
    fixture_bin.mkdir()
    unit_fixture = tmp_path / "unit_effective.txt"
    unit_fixture.write_text(
        "[Service]\n"
        "ExecStart=/opt/jobpulse/scripts/run_collection_cycle_safe.sh\n"
        "Environment=JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/a.lock FOO=bar\n"
    )
    _write_fake_executable(
        fixture_bin,
        "systemctl",
        f'if [ "$1" = "cat" ]; then\n  cat {shlex.quote(str(unit_fixture))}\nfi\n',
    )
    body = "DISCOVERED_LAUNCHERS=()\nscan_systemd_unit jobpulse-collection.service\n"
    result = _run_harness(extracted_functions_script, harness_bin, body, extra_path_dirs=(fixture_bin,))
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "unsupported Environment= form" in result.stderr


# ---------------------------------------------------------------------
# .collection.env: non-executing parser, hostile fixtures, supported
# literals, ROOT-inertness
# ---------------------------------------------------------------------

def _resolve_collection_env_body(root_dir, print_root_after=False):
    extra = '\nprintf \'ROOT_AFTER=%s\\n\' "$EFFECTIVE_COLLECTION_ROOT"\n' if print_root_after else ""
    return (
        f"EFFECTIVE_COLLECTION_ROOT={shlex.quote(str(root_dir))}\n"
        "resolve_collection_env_lock_override\n"
        "printf 'OVERRIDE=%s\\n' \"${COLLECTION_ENV_LOCK_OVERRIDE:-<unset>}\"\n" + extra
    )


@pytest.mark.parametrize(
    "hostile_line_template",
    [
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=$(touch {marker}; echo /x.lock)",
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=`touch {marker}`",
        'JOBPULSE_COLLECTION_CYCLE_LOCK_PATH="$ROOT/custom.lock"',
    ],
    ids=["command-substitution", "backtick-substitution", "variable-expansion"],
)
def test_behavior_hostile_collection_env_never_executed(
    extracted_functions_script, harness_bin, tmp_path, hostile_line_template
):
    """Hostile fixtures that would execute a command if this file were
    ever sourced/eval'd. The restricted parser must reject them as
    unsupported syntax (never silently treat them as unset) and, above
    all, must never actually run the embedded command."""
    marker = tmp_path / "should-never-run"
    root_dir = tmp_path / "collection_root"
    root_dir.mkdir()
    (root_dir / ".collection.env").write_text(hostile_line_template.format(marker=shlex.quote(str(marker))) + "\n")

    result = _run_harness(extracted_functions_script, harness_bin, _resolve_collection_env_body(root_dir))

    assert not marker.exists(), "hostile .collection.env content was EXECUTED -- non-execution guarantee violated"
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "unsupported form" in result.stderr


def test_behavior_hostile_collection_env_shell_function_syntax_is_inert_not_executed(
    extracted_functions_script, harness_bin, tmp_path
):
    """A line that doesn't even look like an assignment to either
    relevant variable (e.g. a shell function definition) must be
    treated as pure inert data -- never executed -- while a genuine,
    separate, well-formed assignment elsewhere in the same file is
    still correctly extracted."""
    marker = tmp_path / "should-never-run"
    root_dir = tmp_path / "collection_root"
    root_dir.mkdir()
    (root_dir / ".collection.env").write_text(
        f"malicious() {{ touch {shlex.quote(str(marker))}; }}\n"
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/safe.lock\n"
    )

    result = _run_harness(extracted_functions_script, harness_bin, _resolve_collection_env_body(root_dir))

    assert not marker.exists(), "shell function syntax in .collection.env was EXECUTED"
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert "OVERRIDE=/safe.lock" in result.stdout


@pytest.mark.parametrize(
    "line,expected",
    [
        ("JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/plain.lock", "/plain.lock"),
        ("export JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/exported.lock", "/exported.lock"),
        ("   JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/ws.lock", "/ws.lock"),
        ("JOBPULSE_COLLECTION_CYCLE_LOCK_PATH='/quoted.lock'", "/quoted.lock"),
        ('JOBPULSE_COLLECTION_CYCLE_LOCK_PATH="/dquoted.lock"', "/dquoted.lock"),
        ("JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/commented.lock   # trailing note", "/commented.lock"),
    ],
    ids=["bare", "export", "leading-whitespace", "single-quoted", "double-quoted", "trailing-comment"],
)
def test_behavior_supported_collection_env_literal_forms_parsed_exactly(
    extracted_functions_script, harness_bin, tmp_path, line, expected
):
    root_dir = tmp_path / "collection_root"
    root_dir.mkdir()
    (root_dir / ".collection.env").write_text(line + "\n")

    result = _run_harness(extracted_functions_script, harness_bin, _resolve_collection_env_body(root_dir))

    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert f"OVERRIDE={expected}" in result.stdout


def test_behavior_root_assignment_in_collection_env_is_inert_not_retroactive(
    extracted_functions_script, harness_bin, tmp_path
):
    """The real wrapper computes ROOT from the environment BEFORE
    sourcing .collection.env -- an assignment to JOBPULSE_COLLECTION_ROOT
    inside that file must never retroactively change the already-
    established EFFECTIVE_COLLECTION_ROOT, even though it is a
    perfectly well-formed literal on its own."""
    root_dir = tmp_path / "collection_root"
    root_dir.mkdir()
    (root_dir / ".collection.env").write_text(
        "JOBPULSE_COLLECTION_ROOT=/some/other/root\n"
        "JOBPULSE_COLLECTION_CYCLE_LOCK_PATH=/x.lock\n"
    )

    result = _run_harness(
        extracted_functions_script, harness_bin, _resolve_collection_env_body(root_dir, print_root_after=True)
    )

    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert "OVERRIDE=/x.lock" in result.stdout
    assert f"ROOT_AFTER={root_dir}" in result.stdout, "JOBPULSE_COLLECTION_ROOT inside .collection.env must not mutate EFFECTIVE_COLLECTION_ROOT"


def test_behavior_root_reference_in_lock_value_is_unsupported_ambiguity(
    extracted_functions_script, harness_bin, tmp_path
):
    """If a value tries to depend on another variable via expansion (the
    only way ROOT semantics COULD create ambiguity here), the restricted
    parser cannot safely resolve it and must fail closed rather than
    guess."""
    root_dir = tmp_path / "collection_root"
    root_dir.mkdir()
    (root_dir / ".collection.env").write_text(
        'JOBPULSE_COLLECTION_CYCLE_LOCK_PATH="$JOBPULSE_COLLECTION_ROOT/custom.lock"\n'
    )

    result = _run_harness(extracted_functions_script, harness_bin, _resolve_collection_env_body(root_dir))

    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "unsupported form" in result.stderr


# ---------------------------------------------------------------------
# Multi-launcher reconciliation (reconcile_launchers), seeded directly
# ---------------------------------------------------------------------

def test_behavior_reconcile_launchers_same_root_and_lock_succeeds(extracted_functions_script, harness_bin):
    body = (
        'DISCOVERED_LAUNCHERS=("|/a.lock|src1" "|/a.lock|src2")\n'
        "reconcile_launchers\n"
        "printf 'RECONCILE_SUCCEEDED\\n'\n"
        "printf 'CONSENSUS=%s\\n' \"$LAUNCHER_LOCK_CONSENSUS\"\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, body)
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert "RECONCILE_SUCCEEDED" in result.stdout
    assert "CONSENSUS=/a.lock" in result.stdout


def test_behavior_reconcile_launchers_different_lock_fails(extracted_functions_script, harness_bin):
    body = (
        'DISCOVERED_LAUNCHERS=("|/a.lock|src1" "|/b.lock|src2")\n'
        "reconcile_launchers\n"
        "printf 'RECONCILE_SUCCEEDED\\n'\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, body)
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "RECONCILE_SUCCEEDED" not in result.stdout
    assert "refusing to guess which is authoritative" in result.stderr


def test_behavior_reconcile_launchers_different_root_fails(extracted_functions_script, harness_bin):
    body = (
        'DISCOVERED_LAUNCHERS=("/opt/jobpulse|/a.lock|src1" "/some/other/root|/a.lock|src2")\n'
        "reconcile_launchers\n"
        "printf 'RECONCILE_SUCCEEDED\\n'\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, body)
    assert result.returncode != 0
    _assert_mutation_sentinels_untouched(result)
    assert "RECONCILE_SUCCEEDED" not in result.stdout
    assert "not /opt/jobpulse" in result.stderr


def test_behavior_reconcile_launchers_no_launchers_uses_default(extracted_functions_script, harness_bin):
    """The "no launcher" default is only reachable by actually running
    reconcile_launchers to completion over an empty array -- proving the
    no-launcher path is a real, exercised code path, not merely absence
    of a crash."""
    body = (
        "DISCOVERED_LAUNCHERS=()\n"
        "reconcile_launchers\n"
        "printf 'ROOT=%s\\n' \"$EFFECTIVE_COLLECTION_ROOT\"\n"
        "printf 'CONSENSUS=%s\\n' \"${LAUNCHER_LOCK_CONSENSUS:-<empty>}\"\n"
    )
    result = _run_harness(extracted_functions_script, harness_bin, body)
    assert result.returncode == 0, result.stderr
    _assert_mutation_sentinels_untouched(result)
    assert "ROOT=/opt/jobpulse" in result.stdout
    assert "CONSENSUS=<empty>" in result.stdout


# =====================================================================
# 30. Healthless container health-lookup safety (Phase 3.4N regression)
#
# Production runtime-upgrade run 34347072679 aborted with a Go template
# error ("map has no entry for key \"Health\"") while capturing the
# unrelated db/frontend/tor container state, because the format string
# used `{{if .State.Health}}`: direct field access on a map that omits
# the "Health" key entirely (observed against jobpulse-frontend-prod, an
# nginx:alpine container with no HEALTHCHECK) errors instead of treating
# the field as absent/falsy. The safe replacement is
# `{{with (index .State "Health")}}...{{else}}none{{end}}` -- `index`
# returns the zero value for a missing map key instead of erroring, the
# same pattern already proven in production-runtime-diagnostic.yml.
#
# capture_container_state() is used for db/frontend/tor -- all
# health-optional -- and DB_HEALTH_BEFORE explicitly accepts "no
# healthcheck defined". The three jobpulse-api-prod-only health lookups
# (rollback_api's rb_health, API_HEALTH_BEFORE, and the candidate
# container-health poll) are deliberately left on the strict
# `{{if .State.Health}}` form: the api service always carries a compose
# HEALTHCHECK, and those checks intentionally require "healthy" (a
# missing Health key there would itself be a real, correctly-surfaced
# failure, not a false negative to paper over).
# =====================================================================


def _extract_bash_function(script: str, func_name: str) -> str:
    """Extracts `<func_name>() { ... }` verbatim by tracking brace depth
    across the whole line (works here because every Go `{{ }}` template
    pair inside the function body is balanced on its own line)."""
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


@pytest.fixture(scope="module")
def capture_container_state_source(remote_script) -> str:
    return _extract_bash_function(remote_script, "capture_container_state")


def _db_health_before_line(remote_script: str) -> str:
    for line in remote_script.splitlines():
        if line.strip().startswith("DB_HEALTH_BEFORE="):
            return line
    raise AssertionError("DB_HEALTH_BEFORE assignment not found")


def test_capture_container_state_uses_missing_key_safe_health_lookup(
    capture_container_state_source,
):
    code_lines = [
        line
        for line in capture_container_state_source.splitlines()
        if not line.strip().startswith("#")
    ]
    code_text = "\n".join(code_lines)
    assert 'index .State "Health"' in code_text
    assert "{{if .State.Health}}" not in code_text
    assert ".State.Health.Status" not in code_text


def test_db_health_before_uses_missing_key_safe_health_lookup(remote_script):
    line = _db_health_before_line(remote_script)
    assert 'index .State "Health"' in line
    assert "{{if .State.Health}}" not in line
    assert ".State.Health.Status" not in line


def test_api_only_health_lookups_remain_intentionally_strict(remote_script):
    """The three jobpulse-api-prod-only health checks (rollback, before,
    candidate) were deliberately left unchanged -- the api service always
    declares a compose HEALTHCHECK, so requiring the strict `healthy`
    template there is correct, not a bug."""
    strict_snippet = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    occurrences = remote_script.count(strict_snippet)
    assert occurrences == 3, occurrences


def _run_capture_container_state(capture_container_state_source, harness_bin, tmp_path, mode: str):
    fake_docker_bin = tmp_path / "fake_docker_bin"
    fake_docker_bin.mkdir()
    if mode == "present_no_healthcheck":
        docker_body = (
            'if [ "$1" = "inspect" ]; then\n'
            '  shift\n'
            '  if [ "$1" = "-f" ]; then\n'
            "    echo 'container-id|0|true|none|sha256:image|nginx:alpine'\n"
            "    exit 0\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
    elif mode == "absent":
        docker_body = 'if [ "$1" = "inspect" ]; then\n  exit 1\nfi\nexit 1\n'
    else:
        raise ValueError(mode)
    _write_fake_executable(fake_docker_bin, "docker", docker_body)

    body = 'STATE="$(capture_container_state jobpulse-frontend-prod)"\nprintf \'STATE=%s\\n\' "$STATE"\n'
    return _run_harness(
        capture_container_state_source,
        harness_bin,
        body,
        extra_path_dirs=(fake_docker_bin,),
    )


def test_behavior_capture_container_state_healthless_container(
    capture_container_state_source, harness_bin, tmp_path
):
    """Case A: the container exists but has no HEALTHCHECK (Docker omits
    the "Health" key from .State entirely). Runs the ACTUAL extracted
    capture_container_state() against a fake docker -- not a Python
    reimplementation -- and requires it complete successfully with the
    health field resolved to "none"."""
    result = _run_capture_container_state(
        capture_container_state_source, harness_bin, tmp_path, "present_no_healthcheck"
    )
    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    state = values["STATE"]
    fields = state.split("|")
    assert len(fields) == 6, state
    assert fields[3] == "none", state


def test_behavior_capture_container_state_absent_container(
    capture_container_state_source, harness_bin, tmp_path
):
    """Case B: `docker inspect <name>` itself fails (container does not
    exist) -- capture_container_state must report exactly ABSENT and
    must not attempt the -f format lookup at all."""
    result = _run_capture_container_state(
        capture_container_state_source, harness_bin, tmp_path, "absent"
    )
    assert result.returncode == 0, result.stderr
    values = _parse_kv_lines(result.stdout)
    assert values["STATE"] == "ABSENT"


# =====================================================================
# 31. Meta: this test file itself is clean
# =====================================================================

def test_this_test_file_has_no_duplicate_test_function_names():
    import ast

    tree = ast.parse(Path(__file__).read_text())
    names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"duplicate test function names: {duplicates}"


def test_this_test_file_has_no_empty_or_pass_only_tests():
    import ast

    tree = ast.parse(Path(__file__).read_text())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]
        assert body, f"{node.name} has an empty body"
        assert not (len(body) == 1 and isinstance(body[0], ast.Pass)), f"{node.name} is pass-only"
