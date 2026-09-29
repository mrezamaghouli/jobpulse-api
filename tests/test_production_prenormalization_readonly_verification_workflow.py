"""Structural regression guard for
.github/workflows/production-prenormalization-readonly-verification.yml
(Phase 4M).

Context: PR #37 (merged) added
production-api-reference-normalization.yml, a workflow_dispatch-only
runner that recreates ONLY the production api container. This companion
workflow is a dedicated, read-only tool an operator dispatches BEFORE
deciding whether to run that mutating runner: it proves the exact
preconditions the mutating runner assumes, and its own source contains
no production-mutation path at all -- not a "dry-run" flag on a mutating
runner, but a runner that never invokes any mutating command in the
first place.

These tests never SSH, use no real Docker, and make no network calls.
Static checks parse the committed YAML/shell as text and prove the
absence of every forbidden command/pattern; a handful of tests diff
specific sections against the already-reviewed corresponding sections of
production-api-reference-normalization.yml to prove genuine reuse rather
than a re-implementation that could silently drift.
"""
import re
import subprocess as sp
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-prenormalization-readonly-verification.yml"
NORMALIZATION_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-api-reference-normalization.yml"
DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

REMOTE_HEREDOC_START = "<<'REMOTE_SCRIPT'"
REMOTE_HEREDOC_END = "REMOTE_SCRIPT"

EXPECTED_PRODUCTION_SHA = "f4765f857355c6543f68cea0e481b7f20a917147"
EXPECTED_CURRENT_CONFIG_IMAGE = "jobpulse-api-rollback:bccbbd997ee8-1427495"
EXPECTED_IMAGE_ID = "sha256:7739628f61c3bba88ff4e395c0f0a0ce3bea3e3abb40956207b495f9d909b753"
EXPECTED_API_CONTAINER_ID = "ac66a4acef51c8ac3dc27b38a5559c5aa056ec033ceef43c9f8a0e4a8cbd3682"
EXPECTED_DB_CONTAINER_ID = "98f20abb30f4fdfe9473431370c3f6db29c39183d67f31d49638a493a1f605e9"
EXPECTED_FRONTEND_CONTAINER_ID = "fb949b779e80a53f4a9542d2eaa30945d1abbf357fbb2391b2422238371b7dd8"
EXPECTED_TOR_CONTAINER_ID = "e2210c3c47f7a73fca1d5b2faeb62b2eeec89526a56f5da4cb1a00a3298b1612"
CANONICAL_API_REFERENCE = "ghcr.io/mrezamaghouli/jobpulse-api:f4765f857355c6543f68cea0e481b7f20a917147"
SUCCESS_MARKER = "PHASE_4M_PRENORMALIZATION_READONLY_VERIFIED"


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
    return workflow["jobs"]["verify"]


@pytest.fixture(scope="module")
def steps(job) -> list:
    return job["steps"]


def _step_by_name(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


@pytest.fixture(scope="module")
def verify_step(steps):
    return _step_by_name(steps, "Run pre-normalization read-only production verification")


@pytest.fixture(scope="module")
def full_run_text(verify_step) -> str:
    return verify_step["run"]


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


def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines)


def _normalization_remote_script() -> str:
    text = NORMALIZATION_WORKFLOW_PATH.read_text()
    wf = yaml.safe_load(text)
    step = next(
        s
        for s in wf["jobs"]["normalize"]["steps"]
        if s.get("name") == "Run production API reference normalization"
    )
    full_run = step["run"]
    start = full_run.index(REMOTE_HEREDOC_START) + len(REMOTE_HEREDOC_START)
    rest = full_run[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == REMOTE_HEREDOC_END)
    return "\n".join(lines[:end_idx])


def _extract_env_parity_python_source(remote_script: str) -> str:
    start_marker = "<<'ENV_PARITY_PY'"
    start = remote_script.index(start_marker) + len(start_marker)
    rest = remote_script[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == "ENV_PARITY_PY")
    return "\n".join(lines[1:end_idx]) if lines and lines[0] == "" else "\n".join(lines[:end_idx])


IMAGE_PROVENANCE_STEP_NAME = "Derive immutable f476 image Config.Env from GHCR by digest (read-only, tag-independent)"


@pytest.fixture(scope="module")
def image_provenance_step(steps):
    return _step_by_name(steps, IMAGE_PROVENANCE_STEP_NAME)


@pytest.fixture(scope="module")
def image_provenance_step_text(image_provenance_step) -> str:
    return image_provenance_step["run"]


def _extract_image_provenance_python_source(step_text: str) -> str:
    start_marker = "<<'IMAGE_PROVENANCE_PY'"
    start = step_text.index(start_marker) + len(start_marker)
    rest = step_text[start:]
    lines = rest.splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == "IMAGE_PROVENANCE_PY")
    return "\n".join(lines[1:end_idx]) if lines and lines[0] == "" else "\n".join(lines[:end_idx])


@pytest.fixture(scope="module")
def image_provenance_py(image_provenance_step_text) -> str:
    return _extract_image_provenance_python_source(image_provenance_step_text)


@pytest.fixture(scope="module")
def image_provenance_ns(image_provenance_py):
    """Exec the extracted, syntax-checked provenance module in an
    isolated namespace so behavioral tests can call its pure helper
    functions directly (digest verification, platform selection, Env
    extraction) against fabricated data -- without making any real
    network call. `main()`/network I/O are never invoked by these tests;
    only the pure functions are exercised."""
    ns: dict = {"__name__": "image_provenance_under_test"}
    exec(compile(image_provenance_py, "<image-provenance>", "exec"), ns)
    return ns


# =====================================================================
# 1. Trigger, permissions, concurrency, structure
# =====================================================================

def test_workflow_file_exists_and_parses(workflow):
    assert workflow["name"] == "Production Pre-Normalization Read-Only Verification"


def test_trigger_is_workflow_dispatch_only(workflow):
    triggers = _triggers(workflow)
    assert set(triggers.keys()) == {"workflow_dispatch"}


def test_no_other_trigger_keys_in_source(workflow_text):
    for forbidden in ("\npush:", "\npull_request:", "\nschedule:", "\nworkflow_run:", "\nrepository_dispatch:"):
        assert forbidden not in workflow_text


def test_permissions_contents_read_only(workflow):
    assert workflow["permissions"] == {"contents": "read"}


def test_own_dedicated_concurrency_group(workflow):
    assert workflow["concurrency"]["group"] == "jobpulse-production-prenormalization-readonly-verification"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_require_dispatch_from_main(steps):
    step = _step_by_name(steps, "Require dispatch from main")
    assert "refs/heads/main" in step["run"]


def test_job_has_finite_timeout(job):
    assert job["timeout-minutes"] <= 15


def test_no_workflow_inputs_required(workflow):
    """This tool is read-only, so unlike the mutating runners it needs no
    typed confirmation token -- matching production-runtime-diagnostic.yml's
    established precedent for read-only tools."""
    triggers = _triggers(workflow)
    assert triggers.get("workflow_dispatch") in (None, {})


# =====================================================================
# 2. Pinned constants exact match
# =====================================================================

def test_one_time_pins_exact(workflow):
    env = workflow["env"]
    assert env["EXPECTED_PRODUCTION_SHA"] == EXPECTED_PRODUCTION_SHA
    assert env["EXPECTED_CURRENT_CONFIG_IMAGE"] == EXPECTED_CURRENT_CONFIG_IMAGE
    assert env["EXPECTED_IMAGE_ID"] == EXPECTED_IMAGE_ID
    assert env["EXPECTED_API_CONTAINER_ID"] == EXPECTED_API_CONTAINER_ID
    assert env["EXPECTED_DB_CONTAINER_ID"] == EXPECTED_DB_CONTAINER_ID
    assert env["EXPECTED_FRONTEND_CONTAINER_ID"] == EXPECTED_FRONTEND_CONTAINER_ID
    assert env["EXPECTED_TOR_CONTAINER_ID"] == EXPECTED_TOR_CONTAINER_ID
    assert env["CANONICAL_API_REFERENCE"] == CANONICAL_API_REFERENCE


def test_remote_script_pins_match_workflow_env(remote_script):
    for needle in (
        f'EXPECTED_PRODUCTION_SHA="{EXPECTED_PRODUCTION_SHA}"',
        f'EXPECTED_CURRENT_CONFIG_IMAGE="{EXPECTED_CURRENT_CONFIG_IMAGE}"',
        f'EXPECTED_IMAGE_ID="{EXPECTED_IMAGE_ID}"',
        f'EXPECTED_API_CONTAINER_ID="{EXPECTED_API_CONTAINER_ID}"',
        f'EXPECTED_DB_CONTAINER_ID="{EXPECTED_DB_CONTAINER_ID}"',
        f'EXPECTED_FRONTEND_CONTAINER_ID="{EXPECTED_FRONTEND_CONTAINER_ID}"',
        f'EXPECTED_TOR_CONTAINER_ID="{EXPECTED_TOR_CONTAINER_ID}"',
        f'CANONICAL_API_REFERENCE="{CANONICAL_API_REFERENCE}"',
    ):
        assert needle in remote_script, needle


def test_pins_match_merged_normalization_workflow(remote_script):
    """These constants must describe the SAME production preconditions
    the merged mutating runner assumes -- pull them from that file's own
    pins rather than hand-copying, so a future repin of one is caught if
    the other is forgotten."""
    norm_remote = _normalization_remote_script()
    for needle in (
        f'EXPECTED_PRODUCTION_SHA="{EXPECTED_PRODUCTION_SHA}"',
        f'EXPECTED_CURRENT_CONFIG_IMAGE="{EXPECTED_CURRENT_CONFIG_IMAGE}"',
        f'EXPECTED_IMAGE_ID="{EXPECTED_IMAGE_ID}"',
        f'EXPECTED_API_CONTAINER_ID="{EXPECTED_API_CONTAINER_ID}"',
        f'EXPECTED_DB_CONTAINER_ID="{EXPECTED_DB_CONTAINER_ID}"',
        f'EXPECTED_FRONTEND_CONTAINER_ID="{EXPECTED_FRONTEND_CONTAINER_ID}"',
        f'EXPECTED_TOR_CONTAINER_ID="{EXPECTED_TOR_CONTAINER_ID}"',
    ):
        assert needle in norm_remote, needle


# =====================================================================
# 3. No mutation path anywhere in the source -- the core safety property
# =====================================================================

_FORBIDDEN_DOCKER_MUTATIONS = (
    "docker compose up",
    "docker compose down",
    "docker compose create",
    "docker restart",
    "docker stop",
    "docker start",
    "docker rm",
    "docker tag",
    "docker pull",
    "docker push",
    "docker kill",
    "docker exec -it",
)

_FORBIDDEN_GIT_MUTATIONS = (
    "git reset",
    "git checkout",
    "git switch",
    "git pull",
    "git merge",
    "git clean",
    "git stash",
    "git commit",
    "git push",
    "git rebase",
    "git cherry-pick",
)


@pytest.mark.parametrize("forbidden", _FORBIDDEN_DOCKER_MUTATIONS)
def test_no_docker_mutation_command(remote_script, runner_script, forbidden):
    assert forbidden not in _code_only(remote_script)
    assert forbidden not in _code_only(runner_script)


@pytest.mark.parametrize("forbidden", _FORBIDDEN_GIT_MUTATIONS)
def test_no_git_mutation_command(remote_script, forbidden):
    assert forbidden not in _code_only(remote_script)


def test_no_docker_compose_up_down_create_anywhere(remote_script):
    """Belt-and-suspenders beyond the exact-phrase check above: no
    `docker compose ... up|down|create` regardless of intervening flags."""
    for line in _code_only(remote_script).splitlines():
        if "docker compose" not in line:
            continue
        assert not re.search(r"\bup\b", line), line
        assert not re.search(r"\bdown\b", line), line
        assert not re.search(r"\bcreate\b", line), line
        assert "--force-recreate" not in line


def test_docker_compose_config_never_followed_by_mutating_subcommand(remote_script):
    for line in _code_only(remote_script).splitlines():
        if "docker compose -p" not in line:
            continue
        assert " config" in line, line


def test_only_read_only_git_commands_used(remote_script):
    git_commands = set()
    for line in _code_only(remote_script).splitlines():
        stripped = line.strip()
        if stripped.startswith("echo ") or stripped.startswith('"') or stripped.startswith("fail "):
            continue
        git_commands.update(re.findall(r"(?<![\"'\w-])git\s+([a-z-]+)", line))
    assert git_commands <= {"rev-parse", "status", "cat-file", "fetch", "show"}, git_commands


def test_no_production_file_write(remote_script):
    """No redirect-to-a-production-path write anywhere. The
    environment-parity proof streams its three JSON documents through
    an in-memory pipe (see the dedicated mktemp/temp-file regression
    tests below) rather than writing them to any path, and the remote
    script itself is piped over stdin, never scp'd."""
    for line in _code_only(remote_script).splitlines():
        if ">" not in line or ">&" in line and "2>&1" in line and ">>" not in line and " > " not in line:
            pass
    # Narrow, explicit checks instead of a blanket redirect scan (a
    # blanket scan would also flag e.g. `2>&1` merges):
    assert "> /opt/jobpulse" not in remote_script
    assert ">> /opt/jobpulse" not in remote_script
    assert "docker-compose.prod.yml" not in _code_only(remote_script).replace(
        'COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"', ""
    )
    assert "mktemp" not in remote_script


def test_no_scp_or_remote_file_write_in_runner(runner_script):
    """Unlike the mutating runners (which scp a temp script to
    production and clean it up), this workflow pipes its script over SSH
    stdin -- there is no production-side file to write or remove."""
    assert "scp " not in runner_script
    assert "REMOTE_SCRIPT_PATH" not in runner_script


def test_no_collector_queue_or_collection_cycle_execution(remote_script):
    for forbidden in (
        "run_collection_cycle_safe.sh\"",
        "seed_priority_coverage_queue.py",
        "process_search_demand_queue.py",
        "linkedin_plan_collect",
    ):
        # These patterns are only ever grepped-for as literal strings, or
        # assigned into a constant compared against later (scheduler
        # drift checks / active collector check) -- never executed.
        # Executing one would look like `./scripts/foo.sh` or
        # `python -m scripts.foo` as the command itself, not inside a
        # quoted comparison string.
        for line in _code_only(remote_script).splitlines():
            stripped = line.strip()
            if forbidden not in stripped:
                continue
            is_data = (
                "grep" in stripped
                or "fail" in stripped
                or "echo" in stripped
                or stripped.startswith("for pattern in")
                or re.match(r"^[A-Z_][A-Z0-9_]*=", stripped)
            )
            assert is_data, stripped


def test_no_linkedin_access(remote_script, runner_script):
    for forbidden in ("linkedin.com", "LinkedIn", "www.linkedin"):
        assert forbidden not in remote_script
        assert forbidden not in runner_script


def test_no_tor_controlport_socks_or_newnym(remote_script):
    for forbidden in ("NEWNYM", "ControlPort", ":9051", "TOR_SOCKS_", "SEARCH_TRANSPORT=proxy", "check.torproject.org"):
        assert forbidden not in remote_script


def test_tor_only_ever_inspected_never_configured(remote_script):
    section = remote_script[remote_script.index("jobpulse-tor-prod") : remote_script.index("db_frontend_tor_identity=confirmed")]
    for forbidden in ("docker restart", "docker stop", "docker start", "docker rm", "docker exec jobpulse-tor-prod"):
        assert forbidden not in section


def test_no_runtime_diagnostic_or_normalization_dispatch(workflow_text, remote_script, runner_script):
    for forbidden in (
        "production-runtime-diagnostic",
        "production-api-reference-normalization.yml\"",
        "workflow_dispatch.yml",
        "gh workflow run",
        "gh api",
        "/actions/workflows/",
        "repository_dispatch",
    ):
        assert forbidden not in workflow_text
        assert forbidden not in remote_script
        assert forbidden not in runner_script


def test_no_other_production_workflow_dispatch(workflow_text):
    for forbidden in (
        "Production Direct Runtime Upgrade",
        "Production Split-Release Recovery",
        "Production Neutral Canary",
        "Deploy Production",
        "Build JobPulse API Image",
    ):
        assert forbidden not in workflow_text


def test_active_collector_check_never_kills(remote_script):
    section = remote_script[
        remote_script.index("active collector observation") : remote_script.index("safe localhost health observations")
    ]
    for forbidden in ("docker stop", "docker kill", "kill -", "docker rm"):
        assert forbidden not in section
    assert "docker top" in section


def test_active_collector_never_touches_lock_files(remote_script):
    section = remote_script[
        remote_script.index("active collector observation") : remote_script.index("safe localhost health observations")
    ]
    for forbidden in ("flock", "touch /tmp/jobpulse", "rm /tmp/jobpulse", "> /tmp/jobpulse"):
        assert forbidden not in section


def test_never_acquires_collection_locks(remote_script):
    """Unlike the mutating runners, this workflow only checks scheduler
    PROVENANCE -- it never opens/acquires the actual lock files. The one
    `flock -n` occurrence is inside CANONICAL_LAUNCHER_LINE, a constant
    string compared against crontab content, never executed."""
    for line in _code_only(remote_script).splitlines():
        stripped = line.strip()
        if "flock -n" in stripped:
            assert stripped.startswith("CANONICAL_LAUNCHER_LINE="), stripped
    assert "OUTER_LOCK_FD" not in remote_script
    assert "INTERNAL_LOCK_FD" not in remote_script
    assert 'exec {' not in _code_only(remote_script)


# =====================================================================
# 4. Static Compose provenance
# =====================================================================

def test_static_compose_hash_derived_from_immutable_git_commit_on_runner(steps):
    step = _step_by_name(steps, "Derive immutable Compose file hash from the pinned f476 commit")
    run_text = step["run"]
    assert 'git show "${EXPECTED_PRODUCTION_SHA}:docker-compose.prod.yml"' in run_text
    assert "GITHUB_ENV" in run_text


def test_static_compose_provenance_check_exists(remote_script):
    assert 'ACTUAL_COMPOSE_SHA256="$(sha256sum "$COMPOSE_FILE" | awk' in remote_script
    assert '[ "$ACTUAL_COMPOSE_SHA256" = "$COMPOSE_SHA256" ]' in remote_script


def test_static_compose_provenance_precedes_container_inspection(remote_script):
    hash_idx = remote_script.index('ACTUAL_COMPOSE_SHA256="$(sha256sum "$COMPOSE_FILE"')
    api_idx = remote_script.index("jobpulse-api-prod")
    assert hash_idx < api_idx


# =====================================================================
# 5. Direct / no-Tor checks
# =====================================================================

def test_direct_no_tor_runtime_check_exists(remote_script):
    assert 'docker exec jobpulse-api-prod printenv SEARCH_TRANSPORT' in remote_script
    assert 'docker exec jobpulse-api-prod printenv TOR_ENABLED' in remote_script
    assert '[ "$TOR_ENABLED_RUNTIME" = "false" ]' in remote_script


def test_no_full_environment_dump(remote_script):
    for forbidden in (
        "docker exec jobpulse-api-prod env",
        "docker exec jobpulse-api-prod printenv\n",
        "{{range .Config.Env}}",
        "docker inspect jobpulse-api-prod --format '{{.Config.Env}}'",
    ):
        assert forbidden not in remote_script


def test_only_two_narrow_docker_exec_calls(remote_script):
    exec_calls = [line for line in _code_only(remote_script).splitlines() if "docker exec" in line]
    assert len(exec_calls) == 2
    for line in exec_calls:
        assert "printenv" in line
        assert "SEARCH_TRANSPORT" in line or "TOR_ENABLED" in line


# =====================================================================
# 6. Effective CURRENT Compose validation (reused from the mutating runner)
# =====================================================================

def test_effective_current_config_rendered_read_only(remote_script):
    assert 'EFFECTIVE_CURRENT_CONFIG="$(JOBPULSE_API_IMAGE="$EXPECTED_CURRENT_CONFIG_IMAGE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" config)"' in remote_script


def test_effective_current_config_checks_image_search_transport_tor(remote_script):
    for needle in (
        '[ "$EFFECTIVE_CURRENT_API_IMAGE" = "$EXPECTED_CURRENT_CONFIG_IMAGE" ]',
        '[ -n "$EFFECTIVE_CURRENT_SEARCH_TRANSPORT" ] && [ "$EFFECTIVE_CURRENT_SEARCH_TRANSPORT" != "direct" ]',
        '[ "$EFFECTIVE_CURRENT_TOR_ENABLED" = "false" ]',
    ):
        assert needle in remote_script, needle


def test_effective_current_config_never_dumps_full_config(remote_script):
    lines = [line for line in remote_script.splitlines() if "EFFECTIVE_CURRENT_CONFIG" in line]
    for line in lines:
        assert not line.strip().startswith('echo "$EFFECTIVE_CURRENT_CONFIG"')


def test_base_service_topology_check_exists(remote_script):
    assert 'docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" config --services' in remote_script
    assert "printf 'api\\ndb\\nfrontend\\n'" in remote_script
    assert "base_service_set=confirmed_api_db_frontend" in remote_script


def test_effective_current_compose_checks_parity_with_normalization_workflow(remote_script):
    """Same api-block/env-block extraction model, byte-for-byte, as the
    already-reviewed mutating runner -- never a materially different
    parser."""
    norm_remote = _normalization_remote_script()

    def extract_block(script: str, start_needle: str, end_needle: str) -> str:
        s = script.index(start_needle)
        e = script.index(end_needle, s)
        return script[s:e]

    ours = extract_block(
        remote_script,
        'EFFECTIVE_CURRENT_API_BLOCK="$(printf',
        '[ "$EFFECTIVE_CURRENT_API_IMAGE" = "$EXPECTED_CURRENT_CONFIG_IMAGE" ]',
    )
    theirs = extract_block(
        norm_remote,
        'EFFECTIVE_CURRENT_API_BLOCK="$(printf',
        '[ "$EFFECTIVE_CURRENT_API_IMAGE" = "$EXPECTED_CURRENT_CONFIG_IMAGE" ]',
    )
    # The mutating runner nests this logic one level deeper (inside
    # prove_reference_only_inputs(), since it must call it twice); this
    # read-only tool runs it once at top level. Dedent both sides before
    # comparing so only the LOGIC is checked for parity, not incidental
    # function-nesting indentation.
    def dedent_normalize(text: str) -> str:
        return "\n".join(line.strip() for line in _normalize(text).splitlines())

    assert dedent_normalize(ours) == dedent_normalize(theirs)


# =====================================================================
# 7. Runtime / effective-current environment semantic parity
# =====================================================================

def test_render_current_env_json_pair_exists_and_never_pulls(remote_script):
    func_body = _extract_bash_function(remote_script, "render_current_env_json_pair")
    assert "config --format json" in func_body
    assert "docker pull" not in func_body
    assert "docker image pull" not in func_body
    # Regression guard: the image default Env is no longer sourced via a
    # live production-side `docker image inspect` at all -- it is decoded
    # from a pre-verified value the runner-side GHCR digest-chain step
    # already supplied (see section 16 below).
    assert "docker image inspect" not in func_body
    assert "IMAGE_DEFAULT_ENV_JSON_B64" in func_body
    assert "base64 -d" in func_body


def test_compute_env_parity_hashes_semantic_model_matches_normalization_workflow(remote_script):
    """compute_env_parity_hashes() is intentionally NOT byte-identical to
    the mutating runner's version any more: that runner still captures
    its three JSON documents into production-side temporary files
    (acceptable there because it already mutates production under an
    explicit, separately-authorized dispatch), while this dedicated
    read-only runner streams the same documents through an in-memory
    pipe so it performs zero production filesystem writes. What must
    still match is the SEMANTIC comparison model: the same four output
    keys and the same fail-closed error reporting."""
    norm_remote = _normalization_remote_script()
    ours = _extract_bash_function(remote_script, "compute_env_parity_hashes")
    theirs = _extract_bash_function(norm_remote, "compute_env_parity_hashes")
    for needle in (
        "EXPECTED_RUNTIME_ENV_SHA256",
        "ACTUAL_RUNTIME_ENV_SHA256",
        "EXPECTED_ENV_KEY_COUNT",
        "ACTUAL_ENV_KEY_COUNT",
    ):
        assert needle in ours, needle
        assert needle in theirs, needle
    assert "FATAL: semantic environment comparison failed" in ours
    assert "FATAL: semantic environment comparison failed" in theirs


def test_embedded_python_semantic_model_matches_normalization_workflow(remote_script):
    """Same divergence rationale as
    test_compute_env_parity_hashes_semantic_model_matches_normalization_workflow:
    this runner's embedded python reads its three JSON documents from a
    bounded stdin framing protocol (read_record()/MAX_RECORD_BYTES)
    instead of file paths, so it is no longer byte-identical to the
    mutating runner's file-path-based version. The canonical-hash MODEL
    -- image-defaults-overlaid-by-Compose-environment "expected" vs.
    parsed "actual", both sort_keys/compact-separator JSON, both
    SHA-256 -- must still match exactly, reusing the same helper
    functions verbatim."""
    norm_remote = _normalization_remote_script()
    py_ours = _extract_env_parity_python_source(remote_script)
    py_theirs = _extract_env_parity_python_source(norm_remote)
    for shared_needle in (
        "def stringify(value, ctx):",
        "def parse_kv_list(entries, ctx):",
        "def canonical_hash(env_map):",
        'json.dumps(env_map, sort_keys=True, separators=(",", ":"), ensure_ascii=True)',
        'hashlib.sha256(canonical.encode("utf-8")).hexdigest()',
        "expected_env = dict(image_env)",
        "expected_env.update(compose_env)",
        'print(f"EXPECTED_RUNTIME_ENV_SHA256={canonical_hash(expected_env)}")',
        'print(f"ACTUAL_RUNTIME_ENV_SHA256={canonical_hash(runtime_env)}")',
        'print(f"EXPECTED_ENV_KEY_COUNT={len(expected_env)}")',
        'print(f"ACTUAL_ENV_KEY_COUNT={len(runtime_env)}")',
    ):
        assert shared_needle in py_ours, shared_needle
        assert shared_needle in py_theirs, shared_needle


def test_embedded_python_syntax_valid(remote_script):
    py_source = _extract_env_parity_python_source(remote_script)
    compile(py_source, "<embedded-env-parity>", "exec")


def test_embedded_python_never_prints_values(remote_script):
    py_source = _extract_env_parity_python_source(remote_script)
    assert "print(f\"EXPECTED_RUNTIME_ENV_SHA256=" in py_source
    assert "print(f\"ACTUAL_RUNTIME_ENV_SHA256=" in py_source
    # Only hash/count lines and a narrow error naming a key are ever
    # printed -- never a raw environment value.
    assert "for key, value in" in py_source
    assert 'print(f"{key}' not in py_source
    assert 'print(f"{value}' not in py_source


def test_environment_values_never_printed_in_bash(remote_script):
    parity_section = remote_script[
        remote_script.index("runtime vs effective-CURRENT semantic environment equivalence") : remote_script.index(
            "DB / frontend / Tor identity"
        )
    ]
    for varname in ("ENV_PARITY_COMPOSE_JSON", "ENV_PARITY_IMAGE_ENV_JSON", "RUNTIME_ENV_JSON"):
        for line in parity_section.splitlines():
            if varname in line:
                assert not line.strip().startswith(f'echo "${varname}"')
                assert not line.strip().startswith(f'cat "${varname}"')


def test_env_parity_json_variables_never_exported(remote_script):
    """The three captured JSON documents (which can transiently hold
    resolved secret values from env_file inputs) must live only as
    plain, never-exported shell variables -- never `export`ed into the
    process environment, where they would appear in a child process's
    /proc/<pid>/environ."""
    for varname in (
        "ENV_PARITY_COMPOSE_JSON",
        "ENV_PARITY_IMAGE_ENV_JSON",
        "RUNTIME_ENV_JSON",
        "compose_json",
        "image_env_json",
        "runtime_env_json",
    ):
        assert f"export {varname}" not in remote_script


def test_env_parity_values_never_passed_as_python_argv(remote_script):
    """Requirement: resolved environment values must never be passed as
    python argv arguments -- the embedded helper reads them exclusively
    from stdin via the bounded framing protocol."""
    py_source = _extract_env_parity_python_source(remote_script)
    assert "sys.argv" not in py_source
    assert "python3 -c \"$ENV_PARITY_PY_SOURCE\"" in remote_script
    # python3's own source is passed via -c; a heredoc-on-stdin
    # invocation (`python3 - <<...`) would collide with the framed
    # protocol's exclusive use of stdin.
    assert "python3 - " not in remote_script
    assert "python3 -" not in remote_script.replace("python3 -c", "")


def test_env_parity_never_uses_mktemp_or_temp_directory(remote_script):
    """Regression guard for the concrete blocker found in review: the
    environment-parity proof must never create a production-side
    temporary directory or file, under any name."""
    assert "mktemp" not in remote_script
    assert "ENV_PARITY_TMP_DIR" not in remote_script
    assert "final_cleanup_trap" not in remote_script


def test_env_parity_never_creates_named_json_files(remote_script):
    """Regression guard: these three filenames were the concrete
    production-filesystem-write blocker found in review -- they must
    never reappear as actual CODE, under any path. (They are named in
    an explanatory comment, which is fine -- documentation naming a
    forbidden pattern is not the pattern itself.)"""
    for forbidden in ("compose_current.json", "image_env.json", "runtime_env.json"):
        assert forbidden not in _code_only(remote_script)


def test_env_parity_never_writes_under_any_alternate_writable_path(remote_script):
    """The fix must not merely relocate the same file-based design to
    another writable production path -- /tmp, /var/tmp, /opt/jobpulse,
    $HOME, and /dev/shm are all still production filesystem writes and
    are all explicitly disallowed by the read-only contract."""
    parity_section = _code_only(
        remote_script[
            remote_script.index("Semantic environment-equivalence proof") : remote_script.index(
                "DB / frontend / Tor identity"
            )
        ]
    )
    for forbidden in ("/tmp/", "/var/tmp/", "/opt/jobpulse/", "$HOME/", "/dev/shm", "TMPDIR"):
        assert forbidden not in parity_section, forbidden


def test_env_parity_producers_never_redirected_to_a_file(remote_script):
    """The three docker producers must be captured into bash variables
    and streamed into python3's stdin via a pipe -- never redirected
    (`>`, `>>`, or `tee`) to a file path."""
    func_body = _extract_bash_function(remote_script, "render_current_env_json_pair")
    assert " > " not in func_body
    assert ">>" not in func_body
    assert "tee " not in func_body
    emit_body = _extract_bash_function(remote_script, "emit_env_parity_frame")
    assert " > " not in emit_body
    assert "tee " not in emit_body


def test_env_parity_framing_is_length_prefixed_not_newline_delimited(remote_script):
    """A length-prefixed frame cannot be confused by any byte sequence a
    JSON document could ever contain (embedded newlines, quotes, `=`,
    spaces, or multi-byte Unicode), unlike a bare newline-delimited
    scheme."""
    func_body = _extract_bash_function(remote_script, "emit_env_parity_frame")
    assert "wc -c" in func_body
    py_source = _extract_env_parity_python_source(remote_script)
    assert "def read_record(stream):" in py_source
    assert "MAX_RECORD_BYTES" in py_source


def test_env_parity_producer_status_checked_before_streaming(remote_script):
    """Each producer's exit/decode status must be checked, via an
    explicit `||`, immediately after it runs and BEFORE its output is
    ever fed into the pipe to python3 -- so a producer failure can never
    be masked by a later pipeline status. The image-default-Env producer
    is now a base64 decode of a runner-supplied, pre-verified value
    rather than a live `docker image inspect`, but the same
    checked-immediately discipline still applies to it."""
    func_body = _extract_bash_function(remote_script, "render_current_env_json_pair")
    assert func_body.count("|| {") == 2
    assert "docker compose -p" in func_body
    assert "base64 -d" in func_body
    assert "docker image inspect" not in func_body


def test_env_parity_pipe_status_explicitly_checked(remote_script):
    """Bash's `-e` does not abort inside a subshell after `set +e`, so
    python3's own exit status (PIPESTATUS[1], not the emitter's) is what
    positively determines success here -- neither command in the pipe
    can mask the other."""
    func_body = _extract_bash_function(remote_script, "compute_env_parity_hashes")
    assert "set +e" in func_body
    assert 'exit "${PIPESTATUS[1]}"' in func_body


def test_env_parity_status_checks_use_if_guard_not_bare_status_capture(remote_script):
    """`set -e` aborts immediately after a failing assignment or
    function call, before a following `x=$?` line would ever run -- so
    every env-parity status capture must use an `if ... ; else ... ; fi`
    guard instead of a bare `cmd; status=$?`."""
    assert 'if py_output="$(' in remote_script
    assert 'if compute_env_parity_hashes "$ENV_PARITY_COMPOSE_JSON"' in remote_script


def test_no_env_parity_tmp_dir_or_cleanup_trap_remains(remote_script):
    """Regression guard: ENV_PARITY_TMP_DIR and its EXIT-trap cleanup
    function existed only to remove the production-side temp directory
    this fix eliminates -- there is nothing left to chmod or clean up."""
    assert "ENV_PARITY_TMP_DIR" not in remote_script
    assert "final_cleanup_trap" not in remote_script
    func_body = _extract_bash_function(remote_script, "render_current_env_json_pair")
    assert "chmod" not in func_body


def test_env_parity_reports_only_hashes_and_counts(remote_script):
    for needle in (
        "expected_environment_sha256=$EXPECTED_RUNTIME_ENV_SHA256",
        "actual_environment_sha256=$ACTUAL_RUNTIME_ENV_SHA256",
        "expected_environment_key_count=$EXPECTED_ENV_KEY_COUNT",
        "actual_environment_key_count=$ACTUAL_ENV_KEY_COUNT",
    ):
        assert needle in remote_script, needle


def test_env_parity_requires_exact_equality(remote_script):
    assert '[ "$EXPECTED_RUNTIME_ENV_SHA256" != "$ACTUAL_RUNTIME_ENV_SHA256" ]' in remote_script
    assert '[ "$EXPECTED_ENV_KEY_COUNT" != "$ACTUAL_ENV_KEY_COUNT" ]' in remote_script


def test_python3_required_before_env_parity_check(remote_script):
    idx = remote_script.index("command -v python3")
    parity_idx = remote_script.index("render_current_env_json_pair || exit 1")
    assert idx < parity_idx


# =====================================================================
# 8. Scheduler provenance -- fail closed, reused as literally as
#    practical from the merged normalization workflow.
# =====================================================================

def _scheduler_provenance_section(script: str) -> str:
    start = script.index("scheduler provenance: this account's own crontab")
    end = script.index("effective_internal_lock_provenance=confirmed_or_absent_default") + len(
        "effective_internal_lock_provenance=confirmed_or_absent_default"
    )
    return script[start:end]


def test_scheduler_provenance_parity_with_normalization_workflow(remote_script):
    """The safety-critical scheduler-discovery logic must be reused as
    literally as practical from the already-reviewed
    production-api-reference-normalization.yml -- not reinvented.
    Compares the two sections with only cosmetic whitespace normalized."""
    this_section = _scheduler_provenance_section(remote_script)
    norm_section = _scheduler_provenance_section(_normalization_remote_script())
    assert _normalize(this_section) == _normalize(norm_section)


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


def test_scheduler_provenance_is_fail_closed(remote_script):
    """Every drift/enumeration check either fails or explicitly reports a
    bounded 'none'/'ok' classification -- there is no silent skip."""
    section = _scheduler_provenance_section(remote_script)
    assert section.count("fail ") >= 15
    assert "|| true" in section  # only for count derivations, never for a hard requirement


def test_collection_env_lock_provenance_reused(remote_script):
    assert "effective internal lock path provenance" in remote_script
    assert "parsed as data only, never sourced/eval'd" in remote_script
    assert '. "/opt/jobpulse/.collection.env"' not in _code_only(remote_script)


def test_scheduler_provenance_never_writes_to_collection_env(remote_script):
    section = _scheduler_provenance_section(remote_script)
    assert "> \"$COLLECTION_ENV_PATH\"" not in section
    assert ">> \"$COLLECTION_ENV_PATH\"" not in section


# =====================================================================
# 9. Active collector observation is read-only
# =====================================================================

def test_active_collector_observation_read_only_and_reported(remote_script):
    assert 'TOP_OUTPUT="$(docker top jobpulse-api-prod)"' in remote_script
    assert 'echo "active_collector=$ACTIVE_COLLECTOR"' in remote_script
    assert '[ "$ACTIVE_COLLECTOR" = "no" ] || fail' in remote_script


def test_active_collector_patterns_match_normalization_workflow(remote_script):
    norm_remote = _normalization_remote_script()
    ours_line = next(line for line in remote_script.splitlines() if line.strip().startswith("for pattern in"))
    theirs_line = next(line for line in norm_remote.splitlines() if line.strip().startswith("for pattern in"))
    assert ours_line.strip() == theirs_line.strip()


# =====================================================================
# 10. Canonical image presence check cannot pull
# =====================================================================

def test_canonical_reference_presence_check_never_pulls(remote_script):
    section = remote_script[
        remote_script.index("canonical reference local presence") : remote_script.index("zero-mutation accounting")
    ]
    assert "docker pull" not in section
    assert "docker image pull" not in section
    assert "docker image inspect \"$CANONICAL_API_REFERENCE\"" in section


def test_canonical_reference_absence_is_non_blocking(remote_script):
    section = remote_script[
        remote_script.index("canonical reference local presence") : remote_script.index("zero-mutation accounting")
    ]
    assert "fail " not in _code_only(section)


# =====================================================================
# 11. Success marker only after every required invariant
# =====================================================================

def test_success_marker_is_last_line_of_remote_script(remote_script):
    non_empty_lines = [line for line in remote_script.splitlines() if line.strip()]
    assert non_empty_lines[-1].strip() == f'echo "{SUCCESS_MARKER}"'


def test_success_marker_exact(remote_script):
    assert SUCCESS_MARKER in remote_script
    assert remote_script.count(SUCCESS_MARKER) == 1


def test_every_required_check_precedes_success_marker(remote_script):
    success_idx = remote_script.index(SUCCESS_MARKER)
    for needle in (
        '[ "$CURRENT_SHA" = "$EXPECTED_PRODUCTION_SHA" ]',
        '[ "$ACTUAL_COMPOSE_SHA256" = "$COMPOSE_SHA256" ]',
        '[ "$API_ID" = "$EXPECTED_API_CONTAINER_ID" ]',
        '[ "$TOR_ENABLED_RUNTIME" = "false" ]',
        '[ "$EFFECTIVE_CURRENT_API_IMAGE" = "$EXPECTED_CURRENT_CONFIG_IMAGE" ]',
        '[ "$EXPECTED_RUNTIME_ENV_SHA256" != "$ACTUAL_RUNTIME_ENV_SHA256" ]',
        '[ "$DB_ID" = "$EXPECTED_DB_CONTAINER_ID" ]',
        '[ "$FRONTEND_ID" = "$EXPECTED_FRONTEND_CONTAINER_ID" ]',
        '[ "$TOR_ID" = "$EXPECTED_TOR_CONTAINER_ID" ]',
        '[ "$ACTIVE_COLLECTOR" = "no" ]',
    ):
        idx = remote_script.index(needle)
        assert idx < success_idx, needle


def test_fail_always_exits_nonzero_before_success_marker(remote_script):
    fail_func = _extract_bash_function(remote_script, "fail")
    assert "exit 1" in fail_func


def test_failures_cannot_trigger_repair_or_mutation(remote_script):
    """No automated retry LOOP, self-heal branch, or fallback mutation
    anywhere in the script -- a failure only ever reports and exits.
    (A human-facing suggestion to re-dispatch this read-only workflow
    later, e.g. after an active collector finishes, is not automated
    repair and is explicitly allowed.)"""
    for forbidden in ("repair", "self-heal", "self_heal", "fix_", "remediate"):
        assert forbidden not in remote_script.lower()


def test_no_retry_wrapper_around_any_check(remote_script):
    assert "while true" not in _code_only(remote_script)
    assert "until [" not in _code_only(remote_script)


# =====================================================================
# 12. Zero-mutation accounting output
# =====================================================================

def test_zero_mutation_accounting_present(remote_script):
    for needle in (
        "production_git_mutations=0",
        "container_recreations=0",
        "container_restarts=0",
        "container_stops_starts=0",
        "docker_pulls=0",
        "docker_tags=0",
        "production_file_writes=0",
        "cron_systemd_mutations=0",
        "database_mutations=0",
        "linkedin_requests=0",
        "tor_controlport_requests=0",
    ):
        assert needle in remote_script, needle


def test_zero_mutation_accounting_precedes_success_marker(remote_script):
    accounting_idx = remote_script.index("zero-mutation accounting")
    success_idx = remote_script.index(SUCCESS_MARKER)
    assert accounting_idx < success_idx


# =====================================================================
# 13. SSH hygiene / secret handling
# =====================================================================

def test_strict_ssh_options(runner_script):
    for needle in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ConnectTimeout=10", "ConnectionAttempts=1"):
        assert needle in runner_script


def test_ssh_key_cleaned_up_via_trap(runner_script):
    assert "trap cleanup_runner EXIT" in runner_script
    assert "rm -f ~/.ssh/jobpulse_prenorm_verify_key" in runner_script


def test_no_ghcr_or_vm_ssh_key_printed(remote_script, runner_script):
    for forbidden in ("VM_SSH_KEY", "GHCR_TOKEN", "ghcr.io/mrezamaghouli/jobpulse-api:f4765f857355c6543f68cea0e481b7f20a917147\" | echo"):
        assert forbidden not in remote_script
    assert "echo \"$VM_SSH_KEY\"" not in runner_script


def test_no_ghcr_login_or_token_usage(remote_script):
    assert "docker login" not in remote_script
    assert "GHCR_TOKEN" not in remote_script


def test_remote_script_uses_strict_bash_mode(remote_script):
    assert "set -Eeuo pipefail" in remote_script


def test_error_trap_never_leaks_secret_values(remote_script):
    trap_line = next(line for line in remote_script.splitlines() if "trap 'report_err" in line)
    assert "$BASH_COMMAND" in trap_line
    # BASH_COMMAND reflects unexpanded command source text (variable
    # NAMES, never their expanded values) -- consistent with every other
    # reviewed Phase 4M workflow.


# =====================================================================
# 16. Runner-side GHCR digest-chain image provenance
#
# The image default Env is no longer read via `docker image inspect` on
# production. Instead, a dedicated runner-side step fetches it from GHCR
# through a fully content-addressed chain -- EXPECTED_IMAGE_ID (hardcoded)
# -> verified OCI index -> verified linux/amd64 manifest -> verified
# config blob -> Config.Env -- each hop re-hashed against its
# parent-declared digest, never the mutable CANONICAL_API_REFERENCE tag
# and never a server-reported digest header alone. Structural tests parse
# the embedded python as text; behavioral tests exec the extracted,
# syntax-checked module and call its pure helper functions directly
# against fabricated (including deliberately tampered) data -- no real
# network call is ever made by these tests.
# =====================================================================

def test_image_provenance_step_exists_before_ssh_key_validation(steps):
    names = [s.get("name") for s in steps]
    prov_idx = names.index(IMAGE_PROVENANCE_STEP_NAME)
    ssh_idx = names.index("Validate VM_SSH_KEY secret is present")
    verify_idx = names.index("Run pre-normalization read-only production verification")
    assert prov_idx < ssh_idx < verify_idx


def test_image_provenance_step_needs_no_secret(image_provenance_step):
    assert "env" not in image_provenance_step or not image_provenance_step["env"]


def test_image_provenance_python_syntax_valid(image_provenance_py):
    compile(image_provenance_py, "<image-provenance>", "exec")


def test_image_provenance_starts_from_expected_image_id_not_tag(image_provenance_py):
    assert f'EXPECTED_IMAGE_ID = "{EXPECTED_IMAGE_ID}"' in image_provenance_py
    # The mutable git-SHA tag must never appear in this provenance chain
    # at all -- not as a manifest path, not as a fallback.
    assert EXPECTED_PRODUCTION_SHA not in image_provenance_py
    assert "CANONICAL_API_REFERENCE" not in image_provenance_py
    assert 'fetch_manifest_by_digest(EXPECTED_IMAGE_ID, token, INDEX_MEDIA_TYPES' in image_provenance_py


def test_image_provenance_never_trusts_content_digest_header_or_status_alone(image_provenance_py):
    # The code itself must never read a server-reported digest header --
    # digest equality is established exclusively by recomputing SHA-256
    # over the exact response bytes.
    assert "resp.headers" not in image_provenance_py
    assert "getheader(" not in image_provenance_py
    assert "def require_digest_equals" in image_provenance_py
    assert "hashlib.sha256(data).hexdigest()" in image_provenance_py


def test_image_provenance_manifest_fetch_verifies_digest_before_parsing(image_provenance_py):
    # fetch_manifest_by_digest must call require_digest_equals BEFORE
    # json.loads -- ordering, not just presence.
    start = image_provenance_py.index("def fetch_manifest_by_digest")
    end = image_provenance_py.index("def fetch_blob_by_digest")
    body = image_provenance_py[start:end]
    require_idx = body.index("require_digest_equals(body, digest, ctx)")
    parse_idx = body.index("json.loads(body.decode")
    assert require_idx < parse_idx


def test_image_provenance_blob_fetch_verifies_digest(image_provenance_py):
    start = image_provenance_py.index("def fetch_blob_by_digest")
    end = image_provenance_py.index("def select_amd64_manifest_digest")
    body = image_provenance_py[start:end]
    assert "require_digest_equals(body, digest, ctx)" in body


def test_image_provenance_excludes_attestation_manifest_explicitly(image_provenance_py):
    assert 'annotations.get("vnd.docker.reference.type") == "attestation-manifest"' in image_provenance_py


def test_image_provenance_requires_exactly_one_amd64_candidate(image_provenance_py):
    assert "no linux/amd64 image manifest entry found" in image_provenance_py
    assert "ambiguous platform selection" in image_provenance_py
    assert "len(candidates) == 0" in image_provenance_py
    assert "len(candidates) > 1" in image_provenance_py


def test_image_provenance_config_digest_sourced_only_from_verified_manifest(image_provenance_py):
    start = image_provenance_py.index("def extract_config_digest")
    end = image_provenance_py.index("def extract_env_list")
    body = image_provenance_py[start:end]
    assert 'config_ref = manifest.get("config")' in body
    assert 'digest = config_ref.get("digest")' in body


def test_image_provenance_main_chain_ordering(image_provenance_py):
    """Config.Env must not be consumed before every hop of the digest
    chain has verified: index fetch, amd64 selection, manifest fetch,
    config-digest extraction, blob fetch, THEN env extraction -- in that
    exact order, all inside main()."""
    start = image_provenance_py.index("def main():")
    body = image_provenance_py[start:]
    idx_index = body.index("fetch_manifest_by_digest(EXPECTED_IMAGE_ID")
    idx_select = body.index("select_amd64_manifest_digest(index)")
    idx_manifest = body.index("fetch_manifest_by_digest(amd64_digest")
    idx_config_digest = body.index("extract_config_digest(manifest)")
    idx_blob = body.index("fetch_blob_by_digest(config_digest")
    idx_env = body.index("extract_env_list(config)")
    assert idx_index < idx_select < idx_manifest < idx_config_digest < idx_blob < idx_env


def test_image_provenance_bounded_response_size(image_provenance_py):
    assert "MAX_RESPONSE_BYTES" in image_provenance_py
    assert "len(body) > MAX_RESPONSE_BYTES" in image_provenance_py
    assert "fail(" in image_provenance_py


def test_image_provenance_empty_response_fails_closed(image_provenance_py):
    assert "len(body) == 0" in image_provenance_py
    assert "returned an empty response body" in image_provenance_py


def test_image_provenance_redirect_bounded_and_drops_sensitive_headers(image_provenance_py):
    assert "class BoundedRedirectHandler" in image_provenance_py
    assert "self.hops > 1" in image_provenance_py
    # Only Accept is forwarded across a redirect hop -- not Authorization,
    # not Host (see the regression note in the handler's own docstring:
    # forwarding Host caused a live, reproduced 421 Misdirected Request).
    assert 'new_req.add_header("Accept", accept)' in image_provenance_py
    assert "req.header_items()" not in image_provenance_py


def test_image_provenance_no_docker_cli_usage(image_provenance_step_text):
    # Registry access is raw HTTP (urllib) only -- no `docker` CLI
    # invocation anywhere in this step's actual code (comments may
    # legitimately name forbidden docker subcommands to explain what is
    # NOT done, so only non-comment lines are checked).
    code = _code_only(image_provenance_step_text)
    for forbidden in ("docker pull", "docker image pull", "docker login", "docker image inspect", "docker compose", "docker "):
        assert forbidden not in code, forbidden


def test_image_provenance_no_new_secret_or_permission(image_provenance_step, workflow):
    assert "secrets." not in image_provenance_step["run"]
    assert workflow["permissions"] == {"contents": "read"}


def test_image_provenance_only_canonical_json_on_stdout_never_logged(image_provenance_step_text, image_provenance_py):
    # The step captures stdout via command substitution -- never echoed
    # directly to the step's own log output.
    assert 'IMAGE_DEFAULT_ENV_JSON="$(python3 -c "$IMAGE_PROVENANCE_PY_SOURCE")"' in image_provenance_step_text
    # Diagnostics (hashes/counts/digests) go to stderr; only the final
    # canonical Env JSON is ever printed to stdout, and only once.
    assert image_provenance_py.count("print(canonical)") == 1
    stdout_prints = [
        line.strip() for line in image_provenance_py.splitlines()
        if line.strip().startswith("print(") and "file=sys.stderr" not in line
    ]
    assert stdout_prints == ["print(canonical)"]


def test_image_provenance_b64_transport_never_echoed(image_provenance_step_text, runner_script, remote_script):
    for script in (image_provenance_step_text, runner_script, remote_script):
        assert 'echo "$IMAGE_DEFAULT_ENV_JSON"' not in script
        assert 'echo "$IMAGE_DEFAULT_ENV_JSON_B64"' not in script


def test_image_provenance_b64_env_written_to_github_env(image_provenance_step_text):
    assert 'echo "IMAGE_DEFAULT_ENV_JSON_B64=$IMAGE_DEFAULT_ENV_JSON_B64" >> "$GITHUB_ENV"' in image_provenance_step_text


def test_image_provenance_ssh_invocation_passes_b64_positionally(remote_script, runner_script):
    assert 'bash -s -- "$COMPOSE_SHA256" "$IMAGE_DEFAULT_ENV_JSON_B64"' in runner_script
    assert 'IMAGE_DEFAULT_ENV_JSON_B64="${2:?required}"' in remote_script


# --- Behavioral tests: exec the extracted module, call pure functions ---

def test_behavioral_digest_mismatch_fails_closed(image_provenance_ns):
    require_digest_equals = image_provenance_ns["require_digest_equals"]
    data = b'{"some": "bytes"}'
    wrong_digest = "sha256:" + ("0" * 64)
    with pytest.raises(SystemExit):
        require_digest_equals(data, wrong_digest, "test ctx")


def test_behavioral_digest_match_passes(image_provenance_ns):
    import hashlib as _hashlib

    require_digest_equals = image_provenance_ns["require_digest_equals"]
    data = b'{"some": "bytes"}'
    correct_digest = f"sha256:{_hashlib.sha256(data).hexdigest()}"
    require_digest_equals(data, correct_digest, "test ctx")  # must not raise


def test_behavioral_malformed_expected_digest_fails_closed(image_provenance_ns):
    require_digest_equals = image_provenance_ns["require_digest_equals"]
    with pytest.raises(SystemExit):
        require_digest_equals(b"anything", "not-a-real-digest", "test ctx")


def test_behavioral_select_amd64_excludes_attestation_and_requires_exactly_one(image_provenance_ns):
    select_amd64_manifest_digest = image_provenance_ns["select_amd64_manifest_digest"]
    MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
    good_digest = "sha256:" + ("a" * 64)
    attestation_digest = "sha256:" + ("b" * 64)
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"mediaType": MANIFEST_MEDIA_TYPE, "digest": good_digest, "platform": {"architecture": "amd64", "os": "linux"}},
            {
                "mediaType": MANIFEST_MEDIA_TYPE,
                "digest": attestation_digest,
                "platform": {"architecture": "amd64", "os": "linux"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            },
        ],
    }
    assert select_amd64_manifest_digest(index) == good_digest


def test_behavioral_select_amd64_zero_candidates_fails_closed(image_provenance_ns):
    select_amd64_manifest_digest = image_provenance_ns["select_amd64_manifest_digest"]
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": "sha256:" + ("c" * 64), "platform": {"architecture": "arm64", "os": "linux"}},
        ],
    }
    with pytest.raises(SystemExit):
        select_amd64_manifest_digest(index)


def test_behavioral_select_amd64_ambiguous_candidates_fails_closed(image_provenance_ns):
    select_amd64_manifest_digest = image_provenance_ns["select_amd64_manifest_digest"]
    MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {"mediaType": MANIFEST_MEDIA_TYPE, "digest": "sha256:" + ("d" * 64), "platform": {"architecture": "amd64", "os": "linux"}},
            {"mediaType": MANIFEST_MEDIA_TYPE, "digest": "sha256:" + ("e" * 64), "platform": {"architecture": "amd64", "os": "linux"}},
        ],
    }
    with pytest.raises(SystemExit):
        select_amd64_manifest_digest(index)


def test_behavioral_malformed_index_fails_closed(image_provenance_ns):
    select_amd64_manifest_digest = image_provenance_ns["select_amd64_manifest_digest"]
    with pytest.raises(SystemExit):
        select_amd64_manifest_digest({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []})
    with pytest.raises(SystemExit):
        select_amd64_manifest_digest({"schemaVersion": 1, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [{}]})


def test_behavioral_missing_config_env_fails_closed(image_provenance_ns):
    extract_env_list = image_provenance_ns["extract_env_list"]
    with pytest.raises(SystemExit):
        extract_env_list({"config": {}})
    with pytest.raises(SystemExit):
        extract_env_list({"config": {"Env": "not-a-list"}})
    with pytest.raises(SystemExit):
        extract_env_list({})


def test_behavioral_extract_env_list_happy_path(image_provenance_ns):
    extract_env_list = image_provenance_ns["extract_env_list"]
    assert extract_env_list({"config": {"Env": ["A=1", "B=2"]}}) == ["A=1", "B=2"]


def test_behavioral_extract_config_digest_validates_media_type_and_digest(image_provenance_ns):
    extract_config_digest = image_provenance_ns["extract_config_digest"]
    good = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "sha256:" + ("f" * 64)},
    }
    assert extract_config_digest(good) == "sha256:" + ("f" * 64)

    bad_media = dict(good, config={"mediaType": "text/plain", "digest": "sha256:" + ("f" * 64)})
    with pytest.raises(SystemExit):
        extract_config_digest(bad_media)

    bad_digest = dict(good, config={"mediaType": "application/vnd.oci.image.config.v1+json", "digest": "not-a-digest"})
    with pytest.raises(SystemExit):
        extract_config_digest(bad_digest)


def test_behavioral_bounded_fetch_rejects_oversized_response(image_provenance_ns, monkeypatch):
    import io

    bounded_fetch = image_provenance_ns["bounded_fetch"]
    MAX_RESPONSE_BYTES = image_provenance_ns["MAX_RESPONSE_BYTES"]
    oversized = b"x" * (MAX_RESPONSE_BYTES + 10)

    class FakeResp:
        status = 200

        def read(self, n):
            return oversized[:n]

        def close(self):
            pass

    class FakeOpener:
        def open(self, req, timeout):
            return FakeResp()

    image_provenance_ns["_OPENER"] = FakeOpener()
    with pytest.raises(SystemExit):
        bounded_fetch("https://example.invalid/x", {}, "test ctx")


def test_behavioral_bounded_fetch_rejects_empty_response(image_provenance_ns):
    bounded_fetch = image_provenance_ns["bounded_fetch"]

    class FakeResp:
        status = 200

        def read(self, n):
            return b""

        def close(self):
            pass

    class FakeOpener:
        def open(self, req, timeout):
            return FakeResp()

    image_provenance_ns["_OPENER"] = FakeOpener()
    with pytest.raises(SystemExit):
        bounded_fetch("https://example.invalid/x", {}, "test ctx")


def test_behavioral_bounded_fetch_rejects_non_200(image_provenance_ns):
    import urllib.error

    bounded_fetch = image_provenance_ns["bounded_fetch"]

    class FakeOpener:
        def open(self, req, timeout):
            raise urllib.error.HTTPError("https://example.invalid/x", 404, "Not Found", {}, None)

    image_provenance_ns["_OPENER"] = FakeOpener()
    with pytest.raises(SystemExit):
        bounded_fetch("https://example.invalid/x", {}, "test ctx")


# =====================================================================
# 14. Diff scope / does-not-modify-other-workflows
# =====================================================================

def test_does_not_modify_other_workflows():
    result = sp.run(
        ["git", "diff", "--name-only", "origin/main...HEAD", "--", ".github/workflows/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("git diff against origin/main not available in this environment")
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    changed.discard(".github/workflows/production-prenormalization-readonly-verification.yml")
    assert not changed, f"unexpected workflow files changed: {changed}"


# =====================================================================
# 15. YAML / bash syntax
# =====================================================================

def test_yaml_parses(workflow):
    assert workflow is not None


def test_remote_script_bash_n_succeeds(remote_script):
    script = "#!/usr/bin/env bash\n" + remote_script
    result = _run_bash_n(script)
    assert result.returncode == 0, result.stderr


def test_runner_script_bash_n_succeeds(runner_script, full_run_text):
    # The runner script (outside the heredoc) is only valid when spliced
    # back with a placeholder for the heredoc body -- reconstruct exactly
    # what the "Run pre-normalization..." step's `run:` block contains.
    result = _run_bash_n("#!/usr/bin/env bash\n" + full_run_text)
    assert result.returncode == 0, result.stderr
