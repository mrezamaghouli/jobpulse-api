"""
Structural regression guard for
.github/workflows/production-neutral-canary.yml (Phase 3.4M-A).

This workflow SSHes to the production VM and can run a real one-shot Tor
canary. It is manual-only (workflow_dispatch) and must never be
reachable through any automatic trigger. These tests are network-free
and never dispatch the workflow -- they only parse the committed YAML
and its embedded shell scripts as text/structure, the same way
tests/test_ci_workflow_structure.py already does for ci.yml/deploy.yml.

The single "Run the one-shot neutral canary on production" step contains
two shell contexts:

- a RUNNER script: everything that executes on the GitHub-hosted runner
  itself (SSH/SCP setup, writing the remote script to a local temp file,
  transferring it, invoking it over SSH). This includes code both BEFORE
  and AFTER the embedded heredoc below, since runner-side cleanup/
  transfer/invocation commands continue after the heredoc closes.
- a REMOTE script: the body of the `cat > "$REMOTE_SCRIPT_LOCAL"
  <<'REMOTE_SCRIPT' ... REMOTE_SCRIPT` heredoc, which is written to a
  local file, scp'd to the VM, and only then executed there via `bash
  <path> <args...>`.

Several checks are deliberately scoped to only one of the two -- e.g.
`git fetch` is expected on the runner (to validate image_sha against
origin/main) but forbidden on the remote production script (which must
never fetch/pull/reset/checkout the production checkout).
"""
import ast
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-neutral-canary.yml"
THIS_TEST_FILE = Path(__file__).resolve()

REMOTE_HEREDOC_START = "<<'REMOTE_SCRIPT'"
REMOTE_HEREDOC_END = "REMOTE_SCRIPT"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text()


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    return yaml.safe_load(workflow_text)


def _triggers(workflow: dict) -> dict:
    """PyYAML's default (YAML 1.1) resolver parses the bare `on:` key as
    the boolean True rather than the string "on" -- fetch it either way
    (same quirk already handled in tests/test_ci_workflow_structure.py)."""
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


@pytest.fixture(scope="module")
def job(workflow) -> dict:
    return workflow["jobs"]["neutral-canary"]


@pytest.fixture(scope="module")
def steps(job) -> list:
    return job["steps"]


def _step_by_name(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


@pytest.fixture(scope="module")
def canary_step(steps):
    return _step_by_name(steps, "Run the one-shot neutral canary on production")


@pytest.fixture(scope="module")
def full_run_text(canary_step) -> str:
    return canary_step["run"]


def _split_remote_and_runner(full_run_text: str):
    start = full_run_text.index(REMOTE_HEREDOC_START) + len(REMOTE_HEREDOC_START)
    before = full_run_text[:full_run_text.index(REMOTE_HEREDOC_START)]
    rest = full_run_text[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line == REMOTE_HEREDOC_END)
    remote = "\n".join(lines[:end_idx])
    after = "\n".join(lines[end_idx + 1:])
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


def _generic_canary_run_lines(remote_script: str):
    """Executable lines that semantically invoke the canary: a `docker
    compose` command with a `run` subcommand targeting
    `search-transport-canary`. Deliberately broader than counting one
    exact literal string, so this also catches a stale/old invocation
    form (e.g. missing `-T`) surviving alongside the new one. Comments
    are excluded by `_executable_lines`; `config --services`/
    `--profiles` validation, `grep`/filter references, and
    leftover-container label filters are excluded because none of them
    combine `docker compose` + a `run` subcommand + the service name on
    one executable line."""
    import re

    matches = []
    for line in _executable_lines(remote_script):
        if "docker compose" not in line:
            continue
        if "search-transport-canary" not in line:
            continue
        if not re.search(r"(?:^|\s)run(?:\s|$)", line):
            continue
        matches.append(line)
    return matches


def _executable_ssh_keyscan_lines(runner_script: str):
    return [line for line in _executable_lines(runner_script) if "ssh-keyscan" in line]


def _executable_health_curl_lines(remote_script: str):
    return [
        line
        for line in _executable_lines(remote_script)
        if "curl" in line and "http://127.0.0.1:8000/health" in line
    ]


def _image_pull_branches(remote_script: str):
    """Split the image-pull `if docker image inspect ... then / else /
    fi` block into its two branch bodies, by locating the specific bare
    `else`/`fi` lines that belong to it (not the unrelated `else` in the
    output-cap-exceeded block, nor any of the `fi`s closing other `if`
    blocks in the script)."""
    present_start = remote_script.index('if docker image inspect "$API_IMAGE" > /dev/null 2>&1; then')
    else_index = remote_script.index("\nelse\n", present_start)
    api_image_id_index = remote_script.index(
        'API_IMAGE_ID="$(docker image inspect "$API_IMAGE" --format', else_index
    )
    present_branch = remote_script[present_start:else_index]
    absent_branch = remote_script[else_index:api_image_id_index]
    return present_branch, absent_branch


# --- 1. workflow exists / valid YAML ----------------------------------------

def test_workflow_exists_and_is_valid_yaml_with_expected_job(workflow):
    assert isinstance(workflow, dict)
    assert isinstance(workflow.get("jobs"), dict)
    assert "neutral-canary" in workflow["jobs"]


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


def test_full_canary_step_run_text_is_valid_bash_syntax(full_run_text):
    result = subprocess.run(
        ["bash", "-n"], input=full_run_text, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_remote_script_in_isolation_is_valid_bash_syntax(remote_script):
    result = subprocess.run(
        ["bash", "-n"], input=remote_script, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_exactly_one_remote_heredoc_in_canary_step(full_run_text):
    assert full_run_text.count(REMOTE_HEREDOC_START) == 1
    # The closing marker line must appear exactly once as its own bare line.
    closing_lines = [line for line in full_run_text.splitlines() if line == REMOTE_HEREDOC_END]
    assert len(closing_lines) == 1


# --- 2-6. trigger model: workflow_dispatch only -----------------------------

def test_only_workflow_dispatch_trigger(workflow):
    triggers = _triggers(workflow)
    assert isinstance(triggers, dict)
    assert set(triggers.keys()) == {"workflow_dispatch"}


@pytest.mark.parametrize("forbidden_trigger", ["push", "pull_request", "workflow_run", "schedule", "repository_dispatch"])
def test_forbidden_automatic_triggers_absent(workflow, forbidden_trigger):
    triggers = _triggers(workflow)
    assert forbidden_trigger not in triggers


# --- 7. permissions contents: read only -------------------------------------

def test_permissions_are_contents_read_only(workflow):
    permissions = workflow.get("permissions")
    assert permissions == {"contents": "read"}


# --- 8. concurrency exists / cancel-in-progress false -----------------------

def test_concurrency_group_and_cancel_in_progress_false(workflow):
    concurrency = workflow.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("group") == "jobpulse-production-neutral-canary"
    assert concurrency.get("cancel-in-progress") is False


# --- job-level timeout ceiling -----------------------------------------------

def test_job_has_a_finite_reasonably_bounded_timeout(job):
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int)
    assert 10 <= timeout <= 15


# --- 9-10. required inputs ---------------------------------------------------

def test_image_sha_input_is_required(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert "image_sha" in inputs
    assert inputs["image_sha"]["required"] is True


def test_confirmation_input_is_required(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert "confirmation" in inputs
    assert inputs["confirmation"]["required"] is True


def test_input_access_uses_established_repo_convention(workflow_text):
    # tor-secret-provision.yml / tor-dark-launch.yml / ci.yml all use the
    # modern `inputs.*` context rather than `github.event.inputs.*` --
    # match that existing convention.
    assert "inputs.image_sha" in workflow_text
    assert "inputs.confirmation" in workflow_text
    assert "github.event.inputs" not in workflow_text


# --- 11. confirmation token exact --------------------------------------------

def test_confirmation_token_exact_match_enforced(steps):
    step = _step_by_name(steps, "Require exact confirmation token")
    run_text = step["run"]
    assert 'CONFIRMATION" != "RUN_NEUTRAL_CANARY"' in run_text
    assert "exit 1" in run_text


# --- 12. main-ref guard exists and runs before SSH ---------------------------

def test_main_ref_guard_exists(steps):
    step = _step_by_name(steps, "Require dispatch from main")
    run_text = step["run"]
    assert "github.ref" in run_text
    assert "refs/heads/main" in run_text
    assert "exit 1" in run_text


def test_main_ref_guard_and_confirmation_precede_the_ssh_step(steps):
    names = [s.get("name") for s in steps]
    ssh_index = names.index("Run the one-shot neutral canary on production")
    assert names.index("Require dispatch from main") < ssh_index
    assert names.index("Require exact confirmation token") < ssh_index
    assert names.index("Validate VM_SSH_KEY secret is present") < ssh_index
    sha_step_name = next(n for n in names if n and n.startswith("Validate image_sha shape"))
    assert names.index(sha_step_name) < ssh_index


def test_main_ref_guard_is_the_first_step(steps):
    assert steps[0]["name"] == "Require dispatch from main"


# --- 13. SHA regex validation -------------------------------------------------

def test_image_sha_regex_validation_exists(steps):
    step = next(s for s in steps if s.get("name", "").startswith("Validate image_sha shape"))
    run_text = step["run"]
    assert "^[0-9a-f]{40}$" in run_text
    assert "exit 1" in run_text


# --- exact reviewed SHA pin ---------------------------------------------------

def test_reviewed_image_sha_constant_matches_phase_3_4l_merge_sha(workflow):
    env = workflow.get("env", {})
    assert env.get("REVIEWED_IMAGE_SHA") == "f4765f857355c6543f68cea0e481b7f20a917147"


def test_image_sha_is_pinned_to_reviewed_sha_before_ssh(steps):
    step = next(s for s in steps if s.get("name", "").startswith("Validate image_sha shape"))
    run_text = step["run"]
    assert '"$IMAGE_SHA" != "$REVIEWED_IMAGE_SHA"' in run_text
    assert "exit 1" in run_text
    names = [s.get("name") for s in steps]
    ssh_index = names.index("Run the one-shot neutral canary on production")
    assert names.index(step["name"]) < ssh_index


# --- 14. ancestor-of-main validation ------------------------------------------

def test_ancestor_of_main_validation_exists(steps):
    step = _step_by_name(steps, "Prove image_sha is a reviewed ancestor of main and extract its overlay")
    run_text = step["run"]
    assert "git merge-base --is-ancestor" in run_text
    assert "origin/main" in run_text
    assert "git cat-file -e" in run_text


# --- 15. overlay extracted from image_sha, not workflow branch ---------------

def test_overlay_and_canary_source_extracted_from_image_sha_commit(steps):
    step = _step_by_name(steps, "Prove image_sha is a reviewed ancestor of main and extract its overlay")
    run_text = step["run"]
    assert 'git show "${IMAGE_SHA}:scripts/search_transport/canary.py"' in run_text
    assert 'git show "${IMAGE_SHA}:docker-compose.prod.tor.yml"' in run_text
    assert "search-transport-canary:" in run_text


# --- 16. overlay SHA-256 checked remotely -------------------------------------

def test_overlay_sha256_computed_on_runner(steps):
    step = _step_by_name(steps, "Prove image_sha is a reviewed ancestor of main and extract its overlay")
    assert "sha256sum /tmp/reviewed-overlay.yml" in step["run"]


def test_overlay_sha256_checked_remotely_before_canary(runner_script, remote_script):
    assert "LOCAL_OVERLAY_SHA256=" in runner_script
    assert "REMOTE_OVERLAY_SHA256=" in remote_script
    assert '"$REMOTE_OVERLAY_SHA256" != "$LOCAL_OVERLAY_SHA256"' in remote_script
    checksum_index = remote_script.index('"$REMOTE_OVERLAY_SHA256" != "$LOCAL_OVERLAY_SHA256"')
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    assert checksum_index < canary_run_index


# --- 17-18. secrets ------------------------------------------------------------

def test_vm_ssh_key_secret_is_used(workflow_text):
    assert "secrets.VM_SSH_KEY" in workflow_text


def test_no_new_secret_names_introduced(workflow_text):
    import re
    used_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", workflow_text))
    assert used_secrets <= {"VM_SSH_KEY", "GHCR_TOKEN"}
    assert used_secrets, "expected at least VM_SSH_KEY to be referenced"


def test_secret_values_never_echoed_directly(workflow_text):
    assert "echo \"$VM_SSH_KEY\"" not in workflow_text
    assert "echo $VM_SSH_KEY" not in workflow_text
    assert "echo \"$GHCR_TOKEN\"" not in workflow_text
    assert "echo $GHCR_TOKEN" not in workflow_text


# --- GHCR token transport: stdin only, never argv, never base64 -------------

def test_ghcr_token_never_base64_encoded(workflow_text):
    assert "base64" not in workflow_text


def test_ghcr_token_travels_via_stdin_pipe_into_ssh(runner_script):
    assert 'printf \'%s\' "${GHCR_TOKEN:-}" | ssh' in runner_script


def test_ghcr_token_variable_never_appears_inside_an_ssh_command_string_argument(runner_script):
    # The ssh/scp invocations build a quoted remote command string; GHCR_TOKEN
    # itself must never be interpolated into that string (only HAS_TOKEN, a
    # non-secret boolean, may be).
    for line in runner_script.splitlines():
        if "ssh " in line or "scp " in line:
            assert "$GHCR_TOKEN" not in line
            assert "GHCR_TOKEN_B64" not in line


def test_remote_script_reads_ghcr_token_from_its_own_stdin(remote_script):
    assert "docker login ghcr.io -u mrezamaghouli --password-stdin" in remote_script
    # No GHCR_TOKEN variable exists inside the remote script at all -- it
    # only ever sees the non-secret HAS_TOKEN flag and reads the actual
    # secret bytes implicitly via its own stdin.
    assert "GHCR_TOKEN" not in remote_script
    assert "HAS_TOKEN" in remote_script


def test_ghcr_auth_skipped_when_image_already_present(remote_script):
    already_present_index = remote_script.index("Immutable API image already present locally")
    login_index = remote_script.index("docker login ghcr.io")
    inspect_check_index = remote_script.index('docker image inspect "$API_IMAGE" > /dev/null 2>&1')
    assert inspect_check_index < already_present_index < login_index


def test_ghcr_auth_only_attempted_when_has_token_true(remote_script):
    if_index = remote_script.index('if [ "$HAS_TOKEN" = "true" ]; then')
    login_index = remote_script.index("docker login ghcr.io")
    assert if_index < login_index
    # No `fi` closing that if-block appears between the condition and the
    # login call -- i.e. the login is still inside that guarded scope.
    between = remote_script[if_index:login_index]
    assert "\nfi" not in between and not between.rstrip().endswith("fi")


# --- Docker-config isolation: prove per-branch semantics, not just the -------
# --- absence of one literal string ------------------------------------------

def test_image_present_branch_never_logs_in_or_pulls(remote_script):
    present_branch, _absent_branch = _image_pull_branches(remote_script)
    assert "docker login" not in present_branch
    assert "docker pull" not in present_branch
    assert "mktemp -d" not in present_branch


def test_image_absent_branch_creates_temp_docker_config_unconditionally_before_optional_login(remote_script):
    _present_branch, absent_branch = _image_pull_branches(remote_script)
    mktemp_index = absent_branch.index('TMP_DOCKER_CONFIG="$(mktemp -d)"')
    has_token_if_index = absent_branch.index('if [ "$HAS_TOKEN" = "true" ]; then')
    login_index = absent_branch.index("docker login ghcr.io")
    pull_index = absent_branch.index('docker pull "$API_IMAGE"')
    # Created before the HAS_TOKEN branch even exists, so both the
    # (optional) login and the (mandatory) pull can rely on it.
    assert mktemp_index < has_token_if_index < login_index < pull_index


def test_image_absent_branch_pull_is_unconditional_and_outside_has_token_guard(remote_script):
    _present_branch, absent_branch = _image_pull_branches(remote_script)
    has_token_if_index = absent_branch.index('if [ "$HAS_TOKEN" = "true" ]; then')
    fi_index = absent_branch.index("\n  fi\n", has_token_if_index)
    pull_index = absent_branch.index('docker pull "$API_IMAGE"')
    # The pull line must come AFTER the `fi` closing the HAS_TOKEN guard,
    # i.e. it always runs regardless of HAS_TOKEN -- not only inside the
    # token-present branch.
    assert fi_index < pull_index


def test_image_absent_branch_login_and_pull_use_the_same_isolated_temp_config(remote_script):
    _present_branch, absent_branch = _image_pull_branches(remote_script)
    assert 'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker login ghcr.io' in absent_branch
    assert 'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker pull "$API_IMAGE"' in absent_branch


# --- 19. no target URL input --------------------------------------------------

def test_no_target_url_or_arbitrary_command_inputs(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert set(inputs.keys()) == {"image_sha", "confirmation"}
    forbidden_substrings = (
        "target", "url", "tor_host", "socks", "host", "command",
        "ssh", "compose_project", "controlport", "retry",
    )
    for key in inputs:
        lowered = key.lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in lowered, f"unexpected input key {key!r}"


# --- 20. neutral endpoint hard-coded ------------------------------------------

def test_neutral_endpoint_is_hardcoded_not_input_derived(workflow):
    env = workflow.get("env", {})
    assert env.get("NEUTRAL_TARGET_URL") == "https://check.torproject.org/api/ip"


def test_neutral_endpoint_env_does_not_reference_inputs(workflow_text):
    import re
    match = re.search(r'NEUTRAL_TARGET_URL:\s*"([^"]+)"', workflow_text)
    assert match
    assert "inputs." not in match.group(0)
    assert match.group(1) == "https://check.torproject.org/api/ip"


# --- 21-22. immutable image construction --------------------------------------

def test_immutable_ghcr_sha_image_construction(runner_script):
    assert 'API_IMAGE="ghcr.io/mrezamaghouli/jobpulse-api:${IMAGE_SHA}"' in runner_script


def test_no_main_or_latest_runtime_fallback(workflow_text):
    assert "jobpulse-api:main" not in workflow_text
    assert "jobpulse-api:latest" not in workflow_text


# --- 23. existing Tor required healthy ----------------------------------------

def test_tor_healthy_precondition_required_before_canary(remote_script):
    precondition_index = remote_script.index('"$TOR_RUNNING_BEFORE" != "true"')
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    assert precondition_index < canary_run_index
    assert '"$TOR_HEALTH_BEFORE" != "healthy"' in remote_script


# --- 24. existing API health required, bounded, no retry ---------------------

def test_api_health_check_required_before_canary(remote_script):
    first_health_check = remote_script.index("curl --connect-timeout 5 --max-time 10 -fsS http://127.0.0.1:8000/health")
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    assert first_health_check < canary_run_index


def test_api_health_checks_are_bounded_both_pre_and_post_run(remote_script):
    occurrences = remote_script.count("curl --connect-timeout 5 --max-time 10 -fsS http://127.0.0.1:8000/health")
    assert occurrences == 2


def test_generic_health_curl_count_is_exactly_two(remote_script):
    # Identifies ANY executable curl request to the API health endpoint
    # by semantics (curl + the health URL), not just the exact bounded
    # literal -- this fails if an old plain `curl -fsS ...` survived
    # alongside the bounded calls.
    matches = _executable_health_curl_lines(remote_script)
    assert len(matches) == 2, f"expected exactly 2 health curl calls (pre+post), found {len(matches)}: {matches}"


def test_every_generic_health_curl_is_bounded(remote_script):
    matches = _executable_health_curl_lines(remote_script)
    for line in matches:
        for required in ("--connect-timeout 5", "--max-time 10", "-fsS"):
            assert required in line, f"health curl missing {required!r}: {line}"
        assert "--retry" not in line


def test_no_curl_retry_flag_anywhere(workflow_text):
    assert "--retry" not in workflow_text


# --- 25. compose project derived from container label, cross-checked --------

def test_compose_project_derived_and_cross_checked_between_api_and_tor(remote_script):
    assert 'API_PROJECT="$(docker inspect' in remote_script
    assert 'TOR_PROJECT="$(docker inspect' in remote_script
    assert '"$API_PROJECT" != "$TOR_PROJECT"' in remote_script


def test_compose_service_labels_verified_for_both_containers(remote_script):
    assert 'API_SERVICE_LABEL="$(docker inspect' in remote_script
    assert 'TOR_SERVICE_LABEL="$(docker inspect' in remote_script
    assert '"$API_SERVICE_LABEL" != "api"' in remote_script
    assert '"$TOR_SERVICE_LABEL" != "tor"' in remote_script


# --- network/alias discovery, hardened for ambiguity -------------------------

def test_network_discovery_requires_exactly_one_alias_match(remote_script):
    assert '"${#MATCHING_NETWORKS[@]}" -eq 0' in remote_script
    assert '"${#MATCHING_NETWORKS[@]}" -gt 1' in remote_script


def test_network_project_label_cross_checked(remote_script):
    assert "docker network inspect" in remote_script
    assert "com.docker.compose.project" in remote_script
    assert '"$NETWORK_PROJECT_LABEL" != "$PRODUCTION_PROJECT"' in remote_script


def test_api_container_confirmed_on_same_network_as_tor(remote_script):
    assert 'API_NETWORKS=' in remote_script
    assert '*" $NETWORK_NAME "*' in remote_script


# --- 26. existing running Tor image reused, never pulled ---------------------

def test_tor_image_reused_from_running_container_never_pulled(remote_script):
    assert 'TOR_IMAGE_REF_BEFORE="$(docker inspect' in remote_script
    assert "JOBPULSE_TOR_IMAGE" in remote_script


def test_only_api_image_is_pulled_not_tor(remote_script):
    pull_lines = [line for line in _executable_lines(remote_script) if "docker pull" in line]
    assert pull_lines
    for line in pull_lines:
        assert "API_IMAGE" in line
        assert "TOR_IMAGE" not in line


# --- 27. --no-deps mandatory --------------------------------------------------

def test_no_deps_flag_present_in_canary_invocation(remote_script):
    assert "run --rm --no-deps -T search-transport-canary" in remote_script


# --- 28-29. exactly one canary invocation, no retry loop ---------------------

def test_exactly_one_canary_invocation(remote_script):
    run_occurrences = remote_script.count("run --rm --no-deps -T search-transport-canary")
    assert run_occurrences == 1


def test_generic_canary_run_count_is_exactly_one(remote_script):
    # Broader than the exact-literal-string count above: this identifies
    # ANY executable `docker compose ... run ... search-transport-canary`
    # line by semantics (docker compose + a run subcommand + the service
    # name), so it still fails if a stale old-style invocation (e.g.
    # missing `-T`) survived alongside the new one -- something the
    # exact-string count above cannot detect on its own.
    matches = _generic_canary_run_lines(remote_script)
    assert len(matches) == 1, f"expected exactly one canary run invocation, found {len(matches)}: {matches}"


def test_the_one_generic_canary_invocation_is_the_bounded_new_form(remote_script):
    matches = _generic_canary_run_lines(remote_script)
    line = matches[0]
    for required in ("--rm", "--no-deps", "-T", "< /dev/null"):
        assert required in line, f"canary invocation missing {required!r}: {line}"


def test_no_historical_command_substitution_canary_form(remote_script, workflow_text):
    # The previously-reviewed transcript briefly showed both
    # `CANARY_OUTPUT="$(docker compose ... run --rm --no-deps
    # search-transport-canary)"` (old, unbounded command substitution,
    # no -T) and the new `-T ... < /dev/null > file` form side by side.
    # Prove the old form is gone from the actual committed file, not
    # merely absent from a transcript.
    assert 'CANARY_OUTPUT="$(docker compose' not in remote_script
    assert 'CANARY_OUTPUT="$(docker compose' not in workflow_text


def test_no_retry_loop_or_second_invocation(remote_script):
    # No loop/retry construct wraps the canary invocation itself -- the
    # legitimate `while read` loop earlier in the script (network alias
    # parsing) is unrelated and must not trip this check, so scope it to
    # the region immediately around the single `run --rm` call.
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    vicinity = remote_script[max(0, canary_run_index - 500):canary_run_index + 500]
    for forbidden in ("for i in", "while ", "until ", "continue-on-error"):
        assert forbidden not in vicinity.lower()
    assert remote_script.count("run --rm --no-deps -T search-transport-canary") == 1
    assert len(_generic_canary_run_lines(remote_script)) == 1


def test_canary_command_not_overridden(remote_script):
    run_line_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    tail = remote_script[run_line_index:run_line_index + 200]
    first_line = tail.splitlines()[0]
    remainder = first_line.strip()[len("run --rm --no-deps -T search-transport-canary"):]
    assert remainder.strip('> "$CANARY_OUTPUT_FILE" 2>&1/dev/null< ') == "" or remainder.strip().startswith("< /dev/null >")


def test_canary_stdin_detached_and_output_capture_bounded(remote_script):
    assert "< /dev/null >" in remote_script
    assert 'CANARY_OUTPUT_CAP=1048576' in remote_script
    assert '"$CANARY_OUTPUT_BYTES" -gt "$CANARY_OUTPUT_CAP"' in remote_script


def test_output_cap_exceeded_fails_workflow_not_silently_truncated(remote_script):
    assert 'OUTPUT_CAP_EXCEEDED=true' in remote_script
    assert 'OUTPUT_CAP_EXCEEDED" = "true"' in remote_script
    cap_fail_index = remote_script.index('FATAL: canary output exceeded the safety cap')
    exit_index = remote_script[cap_fail_index:].index("exit 1")
    assert exit_index >= 0


# --- 30. no ControlPort / NEWNYM / rotation, executable-only -----------------

@pytest.mark.parametrize("forbidden", [
    "9051", "TOR_CONTROL_HOST", "TOR_CONTROL_PORT", "TOR_CONTROL_PASSWORD",
    "NEWNYM", "rotate_circuit", "request_new_identity",
])
def test_no_control_plane_or_rotation_tokens_in_executable_shell(runner_script, remote_script, forbidden):
    assert forbidden not in runner_script
    assert forbidden not in remote_script


def test_no_executable_controlport_usage(runner_script, remote_script):
    for script in (runner_script, remote_script):
        for line in _executable_lines(script):
            assert "ControlPort" not in line


def test_no_stem_controller_usage(workflow_text):
    assert "import stem" not in workflow_text
    assert "stem.control" not in workflow_text
    assert "Controller(" not in workflow_text


# --- 31. no LinkedIn target ----------------------------------------------------

def test_no_linkedin_target_anywhere_in_workflow(workflow_text, runner_script, remote_script):
    assert "linkedin.com" not in workflow_text.lower()
    assert "linkedin" not in runner_script.lower()
    assert "linkedin" not in remote_script.lower()


# --- 32. no production git mutation in remote script --------------------------

@pytest.mark.parametrize("forbidden", ["git fetch", "git pull", "git checkout", "git reset"])
def test_no_production_git_mutation_commands_in_remote_script(remote_script, forbidden):
    assert forbidden not in remote_script


def test_workflow_level_runner_git_fetch_exists_for_sha_validation(steps):
    step = _step_by_name(steps, "Prove image_sha is a reviewed ancestor of main and extract its overlay")
    assert "git fetch origin main" in step["run"]


def test_remote_production_checkout_is_read_only(remote_script):
    assert "git -C /opt/jobpulse rev-parse HEAD" in remote_script
    assert "git -C /opt/jobpulse status --short" in remote_script


# --- 33. no Tor/API lifecycle commands ----------------------------------------

@pytest.mark.parametrize("forbidden", [
    "docker restart", "docker start ", "docker compose up", "docker compose down",
    "docker compose restart", "docker compose create",
])
def test_no_lifecycle_mutation_commands_anywhere(workflow_text, forbidden):
    assert forbidden not in workflow_text


# --- 34. post-run container-ID/restart/image invariant checks ----------------

def test_post_run_invariant_checks_exist_and_follow_canary_invocation(remote_script):
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    post_run_index = remote_script.index("post-run production invariant check")
    assert post_run_index > canary_run_index
    tail = remote_script[post_run_index:]
    assert "API_ID_AFTER" in tail and "API_ID_BEFORE" in tail
    assert "API_RESTARTS_AFTER" in tail and "API_RESTARTS_BEFORE" in tail
    assert "TOR_ID_AFTER" in tail and "TOR_ID_BEFORE" in tail
    assert "TOR_RESTARTS_AFTER" in tail and "TOR_RESTARTS_BEFORE" in tail


def test_post_run_image_reference_invariants_exist(remote_script):
    for var in (
        "API_IMAGE_ID_BEFORE", "API_IMAGE_ID_AFTER", "API_IMAGE_REF_BEFORE", "API_IMAGE_REF_AFTER",
        "TOR_IMAGE_ID_BEFORE", "TOR_IMAGE_ID_AFTER",
        "TOR_IMAGE_REF_BEFORE", "TOR_IMAGE_REF_AFTER",
    ):
        assert var in remote_script
    assert '"$API_IMAGE_ID_AFTER" = "$API_IMAGE_ID_BEFORE"' in remote_script
    assert '"$API_IMAGE_REF_AFTER" = "$API_IMAGE_REF_BEFORE"' in remote_script
    assert '"$TOR_IMAGE_ID_AFTER" = "$TOR_IMAGE_ID_BEFORE"' in remote_script


def test_tor_image_reference_compared_after_run_symmetric_with_api(remote_script):
    # Tor's `.Config.Image` reference must be captured and compared
    # after the run too, not merely its image ID -- symmetric with the
    # API's own before/after image-ID-and-reference protection.
    assert 'TOR_IMAGE_REF_BEFORE="$(docker inspect' in remote_script
    assert 'TOR_IMAGE_REF_AFTER="$(docker inspect' in remote_script
    assert '"$TOR_IMAGE_REF_AFTER" = "$TOR_IMAGE_REF_BEFORE"' in remote_script
    ref_after_index = remote_script.index('TOR_IMAGE_REF_AFTER="$(docker inspect')
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    assert ref_after_index > canary_run_index


def test_post_run_invariants_run_even_if_canary_fails(remote_script):
    canary_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    before = remote_script[:canary_index]
    after = remote_script[canary_index:]
    assert "set +e" in before
    assert "set -e" in after
    post_run_index = remote_script.index("post-run production invariant check")
    canary_exit_check_index = remote_script.index('"$CANARY_EXIT" -ne 0')
    assert post_run_index < canary_exit_check_index, (
        "invariant checks must run before the workflow decides to fail on canary exit code"
    )


def test_invariant_failure_reported_before_canary_exit_code_decision(remote_script):
    # Post-run invariant failure must be checked (and can exit) before the
    # script ever gets to deciding based on CANARY_EXIT -- this proves
    # invariants are never skipped by an early return on canary failure,
    # for ANY canary exit code (2/3/4/5/6/124/etc all flow through the
    # same unconditional post-run block below the run --rm invocation,
    # since there is no early `exit`/`return` between the invocation and
    # the invariant checks).
    canary_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    post_run_index = remote_script.index("post-run production invariant check")
    between = remote_script[canary_index:post_run_index]
    # No exit/return statement is allowed between the invocation and the
    # start of the (always-executed) post-run invariant section.
    for line in _executable_lines(between):
        assert not line.startswith("exit ")
        assert not line.startswith("return ")


def test_no_leftover_canary_container_checks_are_project_scoped(remote_script):
    assert "com.docker.compose.service=search-transport-canary" in remote_script
    assert "PRE_LEFTOVER" in remote_script
    assert "POST_LEFTOVER" in remote_script
    for var in ("PRE_LEFTOVER", "POST_LEFTOVER"):
        line = next(l for l in remote_script.splitlines() if f'{var}="$(docker ps' in l)
        assert 'label=com.docker.compose.project=${PRODUCTION_PROJECT}' in line


def test_pre_run_leftover_guard_runs_before_canary_invocation(remote_script):
    pre_leftover_index = remote_script.index('PRE_LEFTOVER="$(docker ps')
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    assert pre_leftover_index < canary_run_index


# --- 35. API SEARCH_TRANSPORT precondition + post-check, stricter -----------

def test_api_search_transport_precondition_rejects_anything_but_unset_or_direct(remote_script):
    assert '"$API_SEARCH_TRANSPORT_VALUE_BEFORE" != "direct"' in remote_script
    guard_index = remote_script.index('"$API_SEARCH_TRANSPORT_VALUE_BEFORE" != "direct"')
    assert "refusing to proceed" in remote_script[guard_index:guard_index + 200]


def test_api_search_transport_post_check_exists(remote_script):
    assert "API_SEARCH_TRANSPORT_VALUE_BEFORE" in remote_script
    assert "API_SEARCH_TRANSPORT_VALUE_AFTER" in remote_script
    assert '"${API_SEARCH_TRANSPORT_VALUE_AFTER:-}" != "${API_SEARCH_TRANSPORT_VALUE_BEFORE:-}"' in remote_script


# --- 36. TOR_ENABLED precondition + post-check, absence is now a failure ----

def test_api_tor_enabled_precondition_requires_exactly_false(remote_script):
    assert '"$API_TOR_ENABLED_VALUE_BEFORE" != "false"' in remote_script
    guard_index = remote_script.index('"$API_TOR_ENABLED_VALUE_BEFORE" != "false"')
    assert "refusing to proceed" in remote_script[guard_index:guard_index + 200]


def test_api_tor_enabled_post_check_exists(remote_script):
    assert "API_TOR_ENABLED_VALUE_BEFORE" in remote_script
    assert "API_TOR_ENABLED_VALUE_AFTER" in remote_script
    assert '"${API_TOR_ENABLED_VALUE_AFTER:-}" != "${API_TOR_ENABLED_VALUE_BEFORE:-}"' in remote_script


# --- 37. temporary artifact cleanup -------------------------------------------

def test_runner_temporary_artifact_cleanup_exists(runner_script):
    assert "trap cleanup_runner EXIT" in runner_script
    assert "/tmp/reviewed-overlay.yml" in runner_script
    assert '"$REMOTE_SCRIPT_LOCAL"' in runner_script


def test_remote_temporary_artifact_cleanup_exists(remote_script):
    assert "trap cleanup_remote EXIT" in remote_script
    assert 'rm -f "$REMOTE_OVERLAY_PATH"' in remote_script
    assert 'rm -f "$REMOTE_SCRIPT_SELF"' in remote_script


def test_temporary_docker_config_cleanup_exists(remote_script):
    assert "TMP_DOCKER_CONFIG" in remote_script
    assert "rm -rf" in remote_script


def test_no_blank_or_unset_docker_config_used_for_any_docker_command(remote_script):
    # The historical bug: DOCKER_CONFIG="${TMP_DOCKER_CONFIG:-}" (or an
    # explicit empty string) is indistinguishable to the Docker CLI from
    # DOCKER_CONFIG being unset entirely, so it silently falls back to
    # the production user's real ~/.docker/config.json instead of an
    # isolated temp dir. Neither form may appear anywhere in the script.
    assert 'DOCKER_CONFIG="${TMP_DOCKER_CONFIG:-}"' not in remote_script
    assert 'DOCKER_CONFIG=""' not in remote_script


def test_no_unscoped_docker_pull_of_api_image(remote_script):
    for line in _executable_lines(remote_script):
        if line.startswith("docker pull "):
            pytest.fail(
                f"found a docker pull with no DOCKER_CONFIG= prefix on the same "
                f"line (falls back to the default Docker config): {line!r}"
            )


def test_cleanup_traps_are_registered_before_any_failure_prone_operation(remote_script):
    # The trap must be set up early, before the checksum check / preflight
    # / pull / canary invocation that could each fail and trigger it.
    trap_index = remote_script.index("trap cleanup_remote EXIT")
    checksum_index = remote_script.index('"$REMOTE_OVERLAY_SHA256" != "$LOCAL_OVERLAY_SHA256"')
    canary_run_index = remote_script.index("run --rm --no-deps -T search-transport-canary")
    assert trap_index < checksum_index < canary_run_index


# --- SSH/SCP bounded connection options ---------------------------------------

def test_ssh_keyscan_uses_bounded_timeout(runner_script):
    assert "ssh-keyscan -T 10" in runner_script


def test_generic_executable_ssh_keyscan_count_is_exactly_one(runner_script):
    # Counts ANY executable ssh-keyscan invocation, not just the exact
    # bounded literal -- this fails if an old unbounded `ssh-keyscan -p
    # ...` were added alongside the bounded one.
    matches = _executable_ssh_keyscan_lines(runner_script)
    assert len(matches) == 1, f"expected exactly one ssh-keyscan invocation, found {len(matches)}: {matches}"


def test_the_one_generic_ssh_keyscan_is_bounded(runner_script):
    matches = _executable_ssh_keyscan_lines(runner_script)
    assert "-T 10" in matches[0], f"the sole ssh-keyscan invocation is not bounded: {matches[0]}"


def test_ssh_and_scp_use_bounded_options_with_single_connection_attempt(runner_script):
    assert "ConnectTimeout=10" in runner_script
    assert "ConnectionAttempts=1" in runner_script
    assert "ServerAliveInterval=" in runner_script
    assert "ServerAliveCountMax=" in runner_script
    assert "BatchMode=yes" in runner_script
    assert "StrictHostKeyChecking=yes" in runner_script


def test_ssh_opts_array_reused_by_both_scp_and_ssh_invocations(runner_script):
    ssh_opts_usages = runner_script.count('"${SSH_OPTS[@]}"')
    # two scp calls + one ssh call = 3 usages of the shared bounded options
    assert ssh_opts_usages == 3


# --- 38. no deployment script invocation --------------------------------------

def test_no_deploy_script_invocation(workflow_text):
    assert "deploy_prod_from_ghcr" not in workflow_text


# --- 39. no automatic workflow trigger (covered above) + no GitHub state mutation

@pytest.mark.parametrize("forbidden", [
    "gh variable set", "gh secret set", "git push", "gh pr merge", "workflow_dispatch\":",
])
def test_no_github_state_mutation_commands(workflow_text, forbidden):
    assert forbidden not in workflow_text


def test_no_docker_login_persists_into_default_docker_config(remote_script):
    assert "DOCKER_CONFIG=\"$TMP_DOCKER_CONFIG\"" in remote_script
    # No EXECUTABLE line references the default config path (an
    # explanatory comment describing the historical bug is fine and
    # expected -- only executable code matters here). Absence of the
    # literal path string alone doesn't prove isolation, since Docker
    # falls back to that default path implicitly whenever DOCKER_CONFIG
    # is unset/blank -- so also trace the actual branch semantics (see
    # test_image_absent_branch_* and
    # test_no_blank_or_unset_docker_config_used_for_any_docker_command
    # above, and test_no_unscoped_docker_pull_of_api_image below).
    for line in _executable_lines(remote_script):
        assert "~/.docker/config.json" not in line
    _present_branch, absent_branch = _image_pull_branches(remote_script)
    assert 'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker pull "$API_IMAGE"' in absent_branch


# --- deploy.yml and runtime code must remain completely untouched ------------

DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


def test_deploy_workflow_file_still_exists_unmodified_by_this_task():
    assert DEPLOY_WORKFLOW_PATH.exists()
    text = DEPLOY_WORKFLOW_PATH.read_text()
    assert "name: Deploy Production" in text


# --- meta: this test file itself is clean (no duplicate/placeholder tests) ---

def test_this_test_file_has_no_duplicate_test_function_names():
    tree = ast.parse(THIS_TEST_FILE.read_text())
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    ]
    duplicates = {name for name in names if names.count(name) > 1}
    assert not duplicates, f"duplicate test function names: {duplicates}"


def test_this_test_file_has_no_placeholder_or_pass_only_tests():
    tree = ast.parse(THIS_TEST_FILE.read_text())
    empty = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name.startswith("test_")):
            continue
        body = node.body
        # Skip a leading docstring when judging emptiness.
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if not body or all(isinstance(stmt, ast.Pass) for stmt in body):
            empty.append(node.name)
    assert not empty, f"empty/pass-only test functions: {empty}"


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint not installed locally")
def test_actionlint_if_available():
    result = subprocess.run(
        ["actionlint", str(WORKFLOW_PATH)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
