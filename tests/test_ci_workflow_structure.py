"""
Structural regression guard for .github/workflows/ci.yml.

Why this exists: YAML indentation mistakes are easy to introduce (e.g. an
extra two spaces silently nests a new job as a PROPERTY of the previous
job instead of a sibling under `jobs:`) and YAML parsing alone does not
validate GitHub Actions' own schema -- a document can be perfectly valid
YAML and still be a structurally broken workflow. This test asserts the
job hierarchy directly against the parsed document, the same way an
adversarial reviewer would, rather than trusting visual indentation.

No network access, no `actionlint` download -- if `actionlint` happens to
already be installed locally it is invoked as an extra check, otherwise
skipped (never a hard dependency of this test file).
"""
import base64
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text())


def test_workflow_is_valid_yaml_and_jobs_is_a_mapping(workflow):
    assert isinstance(workflow, dict)
    assert isinstance(workflow.get("jobs"), dict)
    assert len(workflow["jobs"]) >= 2


def test_postgres_upsert_integration_job_is_a_direct_sibling_of_jobs(workflow):
    jobs = workflow["jobs"]
    assert "postgres-upsert-integration" in jobs

    job = jobs["postgres-upsert-integration"]
    assert isinstance(job, dict)
    assert "runs-on" in job
    assert "services" in job
    assert "env" in job
    assert "steps" in job
    assert isinstance(job["steps"], list)
    assert job["steps"]


def test_postgres_upsert_integration_is_not_nested_inside_another_job(workflow):
    jobs = workflow["jobs"]
    for other_name, other_job in jobs.items():
        if other_name == "postgres-upsert-integration":
            continue
        assert isinstance(other_job, dict)
        assert "postgres-upsert-integration" not in other_job
        # Also guard against it being buried inside that job's own
        # `steps` list (a different, sneakier way to mis-nest content).
        for step in other_job.get("steps", []) or []:
            if isinstance(step, dict):
                assert "postgres-upsert-integration" not in step


def test_verify_job_still_exists_and_is_unaffected(workflow):
    jobs = workflow["jobs"]
    assert "verify" in jobs
    verify = jobs["verify"]
    assert isinstance(verify, dict)
    assert isinstance(verify.get("steps"), list)
    assert len(verify["steps"]) >= 5  # unchanged, sanity check only


def test_postgres_service_uses_postgres_16(workflow):
    job = workflow["jobs"]["postgres-upsert-integration"]
    postgres_service = job["services"]["postgres"]
    assert "postgres:16" in postgres_service["image"]


def test_postgres_job_installs_postgresql_client_not_only_libpq_dev(workflow):
    """Regression guard for the exact original defect: `libpq-dev` is C
    headers for compiling against libpq, not the `pg_isready`/`psql`
    client binaries (`postgresql-client`)."""
    job = workflow["jobs"]["postgres-upsert-integration"]
    install_steps = [s for s in job["steps"] if "Install system packages" in (s.get("name") or "")]
    assert install_steps
    run_text = install_steps[0]["run"]
    assert "postgresql-client" in run_text


def test_postgres_job_readiness_step_does_not_rely_solely_on_bare_pg_isready_loop(workflow):
    """The replaced readiness step must use a bounded retry that can
    actually fail (unlike the original `for ... pg_isready ... done`
    loop, which fell through successfully even when pg_isready was
    entirely missing or the database never became reachable)."""
    job = workflow["jobs"]["postgres-upsert-integration"]
    steps = job["steps"]
    readiness_steps = [s for s in steps if "reachable" in (s.get("name") or "").lower() or "preflight" in (s.get("name") or "").lower()]
    assert readiness_steps, "no preflight/readiness step found"
    run_text = readiness_steps[0]["run"]
    assert "sys.exit(1)" in run_text  # must be able to fail
    assert "JOBPULSE_TEST_POSTGRES_DSN" in run_text
    assert "psycopg2" in run_text


def test_postgres_job_fails_if_integration_test_is_skipped(workflow):
    job = workflow["jobs"]["postgres-upsert-integration"]
    run_steps = [s for s in job["steps"] if "run" in s]
    test_step_text = "\n".join(s["run"] for s in run_steps)
    assert "junit-xml" in test_step_text
    assert "skipped" in test_step_text
    assert "sys.exit(1)" in test_step_text


def test_actionlint_if_available():
    """Never downloads or installs actionlint -- only runs it if it is
    ALREADY present on PATH."""
    binary = shutil.which("actionlint")
    if not binary:
        pytest.skip("actionlint is not installed locally -- not downloading it per this pass's constraints")

    result = subprocess.run([binary, str(WORKFLOW_PATH)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


# --- Deploy workflow: no duplicate automatic production deploys ----------
#
# Before this fix, deploy.yml had both a direct `push` trigger (for
# frontend/docs/README changes) AND a `workflow_run` trigger that fires
# after "Build JobPulse API Image" succeeds. A single commit touching both
# an API-build path (e.g. scripts/**) and a push-trigger path (e.g.
# docs/**) could enqueue two separate automatic production deploys, and
# `cancel-in-progress: false` meant both could run sequentially. The fix
# removes the direct `push` trigger so automatic deploys only follow a
# successful API image build; other changes require `workflow_dispatch`.

DEPLOY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"


@pytest.fixture(scope="module")
def deploy_workflow() -> dict:
    return yaml.safe_load(DEPLOY_WORKFLOW_PATH.read_text())


def _triggers(workflow: dict) -> dict:
    """PyYAML's default (YAML 1.1) resolver parses the bare `on:` key as
    the boolean True rather than the string "on" -- fetch it either way
    so this test isn't silently broken by that quirk."""
    if "on" in workflow:
        return workflow["on"]
    return workflow[True]


def test_deploy_workflow_is_valid_yaml_with_a_deploy_job(deploy_workflow):
    assert isinstance(deploy_workflow, dict)
    assert isinstance(deploy_workflow.get("jobs"), dict)
    assert "deploy" in deploy_workflow["jobs"]


def test_deploy_workflow_has_no_direct_push_trigger(deploy_workflow):
    triggers = _triggers(deploy_workflow)
    assert "push" not in triggers, (
        "a direct push trigger can race with the workflow_run deploy and "
        "produce duplicate automatic production deploys"
    )


def test_deploy_workflow_retains_workflow_run_trigger_from_build(deploy_workflow):
    triggers = _triggers(deploy_workflow)
    assert "workflow_run" in triggers
    workflow_run = triggers["workflow_run"]
    assert workflow_run["workflows"] == ["Build JobPulse API Image"]
    assert workflow_run["types"] == ["completed"]
    assert workflow_run["branches"] == ["main"], (
        "a manually dispatched or non-main build must not be able to "
        "arm the automatic deploy trigger"
    )


def test_deploy_workflow_retains_workflow_dispatch_trigger(deploy_workflow):
    triggers = _triggers(deploy_workflow)
    assert "workflow_dispatch" in triggers


def test_deploy_workflow_concurrency_group_still_configured(deploy_workflow):
    concurrency = deploy_workflow.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("group") == "jobpulse-production-deploy"
    assert concurrency.get("cancel-in-progress") is False, (
        "cancel-in-progress must stay false so an in-flight deploy is "
        "never killed mid-way; duplicate runs are prevented at the "
        "trigger level instead"
    )


def test_deploy_job_condition_mentions_dispatch_and_successful_main_push_build(deploy_workflow):
    condition = deploy_workflow["jobs"]["deploy"].get("if", "")
    assert "workflow_dispatch" in condition
    assert "workflow_run" in condition
    assert "conclusion" in condition and "success" in condition
    assert "head_branch" in condition and "main" in condition
    assert "workflow_run.event ==" in condition and "'push'" in condition, (
        "'push' must only appear as the required github.event.workflow_run.event "
        "value (the build's own trigger), never as a direct top-level push trigger"
    )


def _condition_as_python(condition: str) -> str:
    """Translate the small subset of GitHub Actions expression syntax
    used by the deploy job's `if:` into an evaluable Python expression,
    so the actual condition string (not a hand-copied stand-in) is what
    gets exercised below."""
    py = condition
    py = py.replace("github.event.workflow_run.conclusion", "event['workflow_run']['conclusion']")
    py = py.replace("github.event.workflow_run.head_branch", "event['workflow_run']['head_branch']")
    py = py.replace("github.event.workflow_run.event", "event['workflow_run']['event']")
    py = py.replace("github.event_name", "github['event_name']")
    py = py.replace("||", " or ")
    py = py.replace("&&", " and ")
    return py


@pytest.mark.parametrize(
    "event_name,conclusion,head_branch,wr_event,should_deploy,case_id",
    [
        ("workflow_dispatch", None, None, None, True, "deploy-workflow_dispatch"),
        ("workflow_run", "success", "main", "push", True, "success-push-main"),
        ("workflow_run", "failure", "main", "push", False, "failure-push-main"),
        ("workflow_run", "cancelled", "main", "push", False, "cancelled-push-main"),
        ("workflow_run", "skipped", "main", "push", False, "skipped-push-main"),
        ("workflow_run", "success", "feature-branch", "push", False, "success-push-feature"),
        ("workflow_run", "success", "main", "workflow_dispatch", False, "success-dispatch-main"),
        ("workflow_run", "success", "feature-branch", "workflow_dispatch", False, "success-dispatch-feature"),
        ("push", None, None, None, False, "unrelated-push-event"),
    ],
    ids=[
        "deploy-workflow_dispatch",
        "success-push-main",
        "failure-push-main",
        "cancelled-push-main",
        "skipped-push-main",
        "success-push-feature",
        "success-dispatch-main",
        "success-dispatch-feature",
        "unrelated-push-event",
    ],
)
def test_deploy_condition_only_fires_on_dispatch_or_successful_main_push_build(
    deploy_workflow, event_name, conclusion, head_branch, wr_event, should_deploy, case_id
):
    condition = deploy_workflow["jobs"]["deploy"]["if"]
    py_expr = _condition_as_python(condition)

    github = {"event_name": event_name}
    event = {"workflow_run": {"conclusion": conclusion, "head_branch": head_branch, "event": wr_event}}

    result = eval(py_expr, {"__builtins__": {}}, {"github": github, "event": event})
    assert result is should_deploy, (
        f"[{case_id}] event_name={event_name!r} conclusion={conclusion!r} "
        f"head_branch={head_branch!r} workflow_run.event={wr_event!r}: "
        f"expected should_deploy={should_deploy}, condition evaluated to {result!r}"
    )


# --- Build workflow: reject manually dispatched non-main builds ----------
#
# docker-build.yml supports workflow_dispatch, which can be run against any
# selected branch and would otherwise publish the mutable `:main` tag from
# that branch's code. A job-level guard restricts build-api to the existing
# push-to-main trigger or a workflow_dispatch explicitly run against main.

BUILD_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"


@pytest.fixture(scope="module")
def build_workflow() -> dict:
    return yaml.safe_load(BUILD_WORKFLOW_PATH.read_text())


def test_build_workflow_push_trigger_still_targets_main(build_workflow):
    triggers = _triggers(build_workflow)
    assert triggers["push"]["branches"] == ["main"]


def test_build_workflow_retains_workflow_dispatch(build_workflow):
    triggers = _triggers(build_workflow)
    assert "workflow_dispatch" in triggers


def test_build_job_has_a_branch_guard_condition(build_workflow):
    condition = build_workflow["jobs"]["build-api"].get("if", "")
    assert "push" in condition
    assert "refs/heads/main" in condition


def test_build_step_still_publishes_main_and_sha_tags(build_workflow):
    steps = build_workflow["jobs"]["build-api"]["steps"]
    build_step = next(s for s in steps if s.get("name") == "Build and push API image")
    tags_text = build_step["with"]["tags"]
    assert ":main" in tags_text
    assert "${{ github.sha }}" in tags_text


def _build_condition_as_python(condition: str) -> str:
    py = condition
    py = py.replace("github.event_name", "github['event_name']")
    py = py.replace("github.ref", "github['ref']")
    py = py.replace("||", " or ")
    py = py.replace("&&", " and ")
    return py


@pytest.mark.parametrize(
    "event_name,ref,should_build",
    [
        ("push", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/main", True),
        ("workflow_dispatch", "refs/heads/feature-x", False),
    ],
    ids=["push-main", "dispatch-main", "dispatch-feature"],
)
def test_build_job_only_runs_for_push_or_main_dispatch(build_workflow, event_name, ref, should_build):
    condition = build_workflow["jobs"]["build-api"]["if"]
    py_expr = _build_condition_as_python(condition)

    github = {"event_name": event_name, "ref": ref}

    result = eval(py_expr, {"__builtins__": {}}, {"github": github})
    assert result is should_build, (
        f"event_name={event_name!r} ref={ref!r}: expected should_build={should_build}, "
        f"condition evaluated to {result!r}"
    )


# --- Production compose: image pinned via JOBPULSE_API_IMAGE -------------

COMPOSE_PROD_PATH = REPO_ROOT / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def compose_prod() -> dict:
    return yaml.safe_load(COMPOSE_PROD_PATH.read_text())


def test_compose_prod_is_valid_yaml_with_an_api_service(compose_prod):
    assert isinstance(compose_prod, dict)
    assert isinstance(compose_prod.get("services"), dict)
    assert "api" in compose_prod["services"]


def test_compose_prod_api_image_uses_env_var_with_main_fallback(compose_prod):
    image = compose_prod["services"]["api"]["image"]
    assert image == "${JOBPULSE_API_IMAGE:-ghcr.io/mrezamaghouli/jobpulse-api:main}", (
        "the api service image must be overridable via JOBPULSE_API_IMAGE so "
        "the deploy workflow can pin it to an immutable build SHA, while "
        "still defaulting to :main for existing manual/operator workflows"
    )


def test_compose_prod_only_api_image_line_changed(compose_prod):
    """Regression guard: db/frontend images, and everything else about the
    api service, must be untouched by the image-pinning change."""
    assert compose_prod["services"]["db"]["image"] == "postgres:16-alpine"
    assert compose_prod["services"]["frontend"]["image"] == "nginx:alpine"
    api = compose_prod["services"]["api"]
    assert api["container_name"] == "jobpulse-api-prod"
    assert api["ports"] == ["127.0.0.1:8000:8000"]


# --- Deploy workflow: pin automatic deploys to the exact build SHA -------
#
# Automatic deploys previously always pulled the mutable `:main` tag, so a
# later build could silently change what an already-triggered deployment
# actually installs. The deploy job now resolves an immutable image before
# ever opening the SSH connection: workflow_run deployments are pinned to
# `ghcr.io/mrezamaghouli/jobpulse-api:<workflow_run.head_sha>` (after
# validating head_sha is exactly a 40-character lowercase hex SHA), while
# manual workflow_dispatch deploys continue to use `:main`. The resolved
# image is base64-encoded and passed to the remote shell the same way
# GHCR_TOKEN_B64 already is, decoded there, and exported as
# JOBPULSE_API_IMAGE for scripts/deploy_prod_from_ghcr.sh (which already
# reads that variable).


def _resolve_image_step(deploy_workflow):
    steps = deploy_workflow["jobs"]["deploy"]["steps"]
    matches = [s for s in steps if s.get("id") == "resolve_image"]
    assert matches, "expected a deploy step with id: resolve_image"
    return matches[0]


def _deploy_ssh_step(deploy_workflow):
    steps = deploy_workflow["jobs"]["deploy"]["steps"]
    matches = [s for s in steps if s.get("name") == "Deploy from GHCR to production VM"]
    assert matches, "expected the 'Deploy from GHCR to production VM' step"
    return matches[0]


def test_resolve_image_step_runs_before_the_ssh_deploy_step(deploy_workflow):
    steps = deploy_workflow["jobs"]["deploy"]["steps"]
    ids_and_names = [s.get("id") or s.get("name") for s in steps]
    assert ids_and_names.index("resolve_image") < ids_and_names.index(
        "Deploy from GHCR to production VM"
    )


def test_resolve_image_step_validates_head_sha_format(deploy_workflow):
    run_text = _resolve_image_step(deploy_workflow)["run"]
    assert "workflow_run.head_sha" in run_text
    assert "[0-9a-f]{40}" in run_text, "head_sha must be validated as exactly 40 lowercase hex characters"


def test_resolve_image_step_never_falls_back_to_main_for_automatic_deploys(deploy_workflow):
    run_text = _resolve_image_step(deploy_workflow)["run"]
    assert "workflow_dispatch" in run_text
    assert 'DEPLOY_IMAGE="ghcr.io/mrezamaghouli/jobpulse-api:main"' in run_text
    assert "${HEAD_SHA}" in run_text


def test_deploy_step_base64_passes_resolved_image_and_exports_it_remotely(deploy_workflow):
    resolve_run_text = _resolve_image_step(deploy_workflow)["run"]
    assert "base64" in resolve_run_text
    assert "GITHUB_OUTPUT" in resolve_run_text

    ssh_step = _deploy_ssh_step(deploy_workflow)
    assert ssh_step.get("env", {}).get("DEPLOY_IMAGE_B64") == "${{ steps.resolve_image.outputs.deploy_image_b64 }}"

    ssh_run_text = ssh_step["run"]
    assert "DEPLOY_IMAGE_B64" in ssh_run_text
    assert "export JOBPULSE_API_IMAGE=" in ssh_run_text
    assert "./scripts/deploy_prod_from_ghcr.sh" in ssh_run_text
    # the resolved image must reach the remote host before the deploy script runs
    assert ssh_run_text.index("export JOBPULSE_API_IMAGE=") < ssh_run_text.index(
        "./scripts/deploy_prod_from_ghcr.sh"
    )


def test_deploy_ssh_step_never_prints_the_decoded_ghcr_token(deploy_workflow):
    ssh_run_text = _deploy_ssh_step(deploy_workflow)["run"]
    assert "echo \"$GHCR_TOKEN\"" not in ssh_run_text
    assert "echo $GHCR_TOKEN" not in ssh_run_text
    assert "GHCR_TOKEN_B64\" | base64 -d | docker login" in ssh_run_text, (
        "the decoded GHCR token must be piped straight into docker login, "
        "never echoed to the log"
    )


def _run_resolve_image_script(run_text: str, event_name: str, head_sha: str, tmp_path):
    """Simulate the GitHub Actions templating step (literal ${{ }}
    substitution happens before the shell ever sees the script) and then
    actually execute the real resolve_image step's bash, so this proves
    the shipped script's behavior rather than a hand-copied stand-in."""
    script = run_text.replace("${{ github.event_name }}", event_name)
    script = script.replace("${{ github.event.workflow_run.head_sha }}", head_sha)

    output_file = tmp_path / "github_output"
    output_file.write_text("")

    env = dict(os.environ)
    env["GITHUB_OUTPUT"] = str(output_file)

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )

    resolved_image = None
    resolved_git_ref = None
    for line in output_file.read_text().splitlines():
        if line.startswith("deploy_image_b64="):
            resolved_image = base64.b64decode(line.split("=", 1)[1]).decode()
        elif line.startswith("deploy_git_ref_b64="):
            resolved_git_ref = base64.b64decode(line.split("=", 1)[1]).decode()

    return result, resolved_image, resolved_git_ref


def test_resolve_image_script_resolves_main_for_manual_dispatch(deploy_workflow, tmp_path):
    run_text = _resolve_image_step(deploy_workflow)["run"]
    result, image, git_ref = _run_resolve_image_script(run_text, "workflow_dispatch", "0" * 40, tmp_path)
    assert result.returncode == 0, result.stderr
    assert image == "ghcr.io/mrezamaghouli/jobpulse-api:main"
    assert git_ref == "origin/main", "manual deploys must check out the latest origin/main"


def test_resolve_image_script_pins_to_head_sha_for_automatic_deploy(deploy_workflow, tmp_path):
    run_text = _resolve_image_step(deploy_workflow)["run"]
    sha = "a" * 40
    result, image, git_ref = _run_resolve_image_script(run_text, "workflow_run", sha, tmp_path)
    assert result.returncode == 0, result.stderr
    assert image == f"ghcr.io/mrezamaghouli/jobpulse-api:{sha}"
    assert git_ref == sha
    assert image.rsplit(":", 1)[1] == git_ref, (
        "the deployed image tag and the VM git checkout must pin to the exact same commit SHA"
    )
    assert ":main" not in image, "automatic deploys must never resolve to the mutable :main tag"


@pytest.mark.parametrize(
    "bad_sha",
    ["", "not-a-sha", "A" * 40, "a" * 39, "a" * 41, "g" * 40],
    ids=["empty", "not-hex", "uppercase", "too-short", "too-long", "invalid-hex-char"],
)
def test_resolve_image_script_rejects_invalid_head_sha(deploy_workflow, tmp_path, bad_sha):
    run_text = _resolve_image_step(deploy_workflow)["run"]
    result, image, git_ref = _run_resolve_image_script(run_text, "workflow_run", bad_sha, tmp_path)
    assert result.returncode != 0, f"expected failure for head_sha={bad_sha!r}"
    assert image is None
    assert git_ref is None


def test_deploy_step_passes_both_resolved_values_to_remote_shell(deploy_workflow):
    ssh_step = _deploy_ssh_step(deploy_workflow)
    assert ssh_step["env"]["DEPLOY_IMAGE_B64"] == "${{ steps.resolve_image.outputs.deploy_image_b64 }}"
    assert ssh_step["env"]["DEPLOY_GIT_REF_B64"] == "${{ steps.resolve_image.outputs.deploy_git_ref_b64 }}"

    ssh_invocation = next(
        line for line in ssh_step["run"].splitlines() if line.strip().startswith("ssh -i")
    )
    assert "GHCR_TOKEN_B64='$GHCR_TOKEN_B64'" in ssh_invocation
    assert "DEPLOY_IMAGE_B64='$DEPLOY_IMAGE_B64'" in ssh_invocation
    assert "DEPLOY_GIT_REF_B64='$DEPLOY_GIT_REF_B64'" in ssh_invocation


def test_remote_checkout_script_validates_commit_and_ancestry(deploy_workflow):
    ssh_run_text = _deploy_ssh_step(deploy_workflow)["run"]
    assert 'git cat-file -e "${DEPLOY_GIT_REF}^{commit}"' in ssh_run_text, (
        "automatic checkout must verify the triggering commit actually exists locally"
    )
    assert 'git merge-base --is-ancestor "$DEPLOY_GIT_REF" origin/main' in ssh_run_text, (
        "automatic checkout must verify the triggering commit is an ancestor of origin/main"
    )
    assert 'git reset --hard "$DEPLOY_GIT_REF"' in ssh_run_text
    assert 'git reset --hard origin/main' in ssh_run_text


def test_git_checkout_happens_before_deploy_script(deploy_workflow):
    ssh_run_text = _deploy_ssh_step(deploy_workflow)["run"]
    assert ssh_run_text.index('git reset --hard') < ssh_run_text.index(
        "./scripts/deploy_prod_from_ghcr.sh"
    ), "the repository checkout must land before the deploy script runs"


def test_remote_script_rejects_ref_that_is_neither_origin_main_nor_a_sha(deploy_workflow):
    ssh_run_text = _deploy_ssh_step(deploy_workflow)["run"]
    assert 'if [ "$DEPLOY_GIT_REF" != "origin/main" ] && ! [[ "$DEPLOY_GIT_REF" =~ ^[0-9a-f]{40}$ ]]; then' in ssh_run_text
    assert "decoded deployment git ref is invalid" in ssh_run_text


# --- Remote checkout logic exercised against a real, disposable local git repo ---
#
# These tests extract the actual heredoc body sent to the VM (up to, but
# not including, the docker-login/deploy-script tail, which needs Docker
# and real registry credentials and must not run here) and execute it
# against a throwaway local git remote + working checkout that stands in
# for /opt/jobpulse -- entirely local, no network, no real repository or
# production host touched.


def _extract_remote_heredoc(ssh_run_text: str) -> str:
    lines = ssh_run_text.splitlines()
    start = next(i for i, l in enumerate(lines) if "<<'REMOTE'" in l) + 1
    end = next(i for i, l in enumerate(lines) if l.strip() == "REMOTE" and i > start)
    return "\n".join(lines[start:end])


def _remote_checkout_only_script(ssh_run_text: str) -> str:
    heredoc = _extract_remote_heredoc(ssh_run_text)
    marker = 'if [ -n "${GHCR_TOKEN_B64:-}" ]; then'
    return heredoc[: heredoc.index(marker)]


def _run_git(args, cwd, check=True):
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
    )
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check)


@pytest.fixture
def git_sandbox(tmp_path):
    """A disposable local git remote ('origin.git') plus a working clone
    ('opt_jobpulse') standing in for the production VM's /opt/jobpulse
    checkout. `old_sha` is an ancestor of the current main tip (`tip_sha`);
    `stray_sha` is a real, locally-present commit that was never merged
    into main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _run_git(["init", "-q", "-b", "main"], seed)
    (seed / "f.txt").write_text("1")
    _run_git(["add", "."], seed)
    _run_git(["commit", "-q", "-m", "c1"], seed)
    old_sha = _run_git(["rev-parse", "HEAD"], seed).stdout.strip()

    (seed / "f.txt").write_text("2")
    _run_git(["add", "."], seed)
    _run_git(["commit", "-q", "-m", "c2"], seed)
    tip_sha = _run_git(["rev-parse", "HEAD"], seed).stdout.strip()

    _run_git(["checkout", "-q", "-b", "stray"], seed)
    (seed / "f.txt").write_text("3")
    _run_git(["add", "."], seed)
    _run_git(["commit", "-q", "-m", "stray"], seed)
    stray_sha = _run_git(["rev-parse", "HEAD"], seed).stdout.strip()
    _run_git(["checkout", "-q", "main"], seed)

    origin_dir = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin_dir)],
        check=True, capture_output=True, text=True,
    )

    deploy_dir = tmp_path / "opt_jobpulse"
    subprocess.run(
        ["git", "clone", "-q", str(origin_dir), str(deploy_dir)],
        check=True, capture_output=True, text=True,
    )

    return {
        "deploy_dir": deploy_dir,
        "old_sha": old_sha,
        "tip_sha": tip_sha,
        "stray_sha": stray_sha,
    }


def _head_sha(repo_dir):
    return _run_git(["rev-parse", "HEAD"], repo_dir).stdout.strip()


def _run_remote_checkout(deploy_workflow, git_sandbox, deploy_git_ref: str):
    ssh_run_text = _deploy_ssh_step(deploy_workflow)["run"]
    script = _remote_checkout_only_script(ssh_run_text)
    script = script.replace(
        "cd /opt/jobpulse", f"cd {shlex.quote(str(git_sandbox['deploy_dir']))}"
    )

    env = dict(os.environ)
    env["DEPLOY_IMAGE_B64"] = base64.b64encode(b"ghcr.io/mrezamaghouli/jobpulse-api:main").decode()
    env["DEPLOY_GIT_REF_B64"] = base64.b64encode(deploy_git_ref.encode()).decode()

    return subprocess.run(
        ["bash", "-c", script],
        cwd=git_sandbox["deploy_dir"],
        env=env,
        capture_output=True,
        text=True,
    )


def test_remote_checkout_manual_resets_to_origin_main(deploy_workflow, git_sandbox):
    result = _run_remote_checkout(deploy_workflow, git_sandbox, "origin/main")
    assert result.returncode == 0, result.stderr
    assert _head_sha(git_sandbox["deploy_dir"]) == git_sandbox["tip_sha"]


def test_remote_checkout_automatic_resets_to_exact_triggering_sha(deploy_workflow, git_sandbox):
    result = _run_remote_checkout(deploy_workflow, git_sandbox, git_sandbox["old_sha"])
    assert result.returncode == 0, result.stderr
    assert _head_sha(git_sandbox["deploy_dir"]) == git_sandbox["old_sha"]


def test_remote_checkout_rejects_commit_not_an_ancestor_of_main(deploy_workflow, git_sandbox):
    result = _run_remote_checkout(deploy_workflow, git_sandbox, git_sandbox["stray_sha"])
    assert result.returncode != 0
    assert _head_sha(git_sandbox["deploy_dir"]) == git_sandbox["tip_sha"], (
        "a rejected checkout must never move HEAD"
    )


def test_remote_checkout_rejects_unavailable_commit(deploy_workflow, git_sandbox):
    missing_sha = "d" * 40
    result = _run_remote_checkout(deploy_workflow, git_sandbox, missing_sha)
    assert result.returncode != 0
    assert _head_sha(git_sandbox["deploy_dir"]) == git_sandbox["tip_sha"]


@pytest.mark.parametrize(
    "bad_ref",
    ["main", "origin/develop", "not-a-sha", "A" * 40, "a" * 39, ""],
    ids=["bare-main", "other-remote-branch", "not-hex", "uppercase", "too-short", "empty"],
)
def test_remote_checkout_rejects_arbitrary_decoded_refs(deploy_workflow, git_sandbox, bad_ref):
    result = _run_remote_checkout(deploy_workflow, git_sandbox, bad_ref)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "decoded deployment git ref is invalid" in combined or "DEPLOY_GIT_REF_B64 is missing" in combined
    assert _head_sha(git_sandbox["deploy_dir"]) == git_sandbox["tip_sha"]
