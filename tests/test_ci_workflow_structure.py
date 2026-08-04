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


def test_deploy_job_condition_mentions_dispatch_and_successful_workflow_run(deploy_workflow):
    condition = deploy_workflow["jobs"]["deploy"].get("if", "")
    assert "workflow_dispatch" in condition
    assert "workflow_run" in condition
    assert "conclusion" in condition and "success" in condition
    assert "'push'" not in condition and '"push"' not in condition


def _condition_as_python(condition: str) -> str:
    """Translate the small subset of GitHub Actions expression syntax
    used by the deploy job's `if:` into an evaluable Python expression,
    so the actual condition string (not a hand-copied stand-in) is what
    gets exercised below."""
    py = condition
    py = py.replace("github.event.workflow_run.conclusion", "event['workflow_run']['conclusion']")
    py = py.replace("github.event_name", "github['event_name']")
    py = py.replace("||", " or ")
    py = py.replace("&&", " and ")
    return py


@pytest.mark.parametrize(
    "event_name,conclusion,should_deploy",
    [
        ("workflow_run", "success", True),
        ("workflow_run", "failure", False),
        ("workflow_run", "cancelled", False),
        ("workflow_run", "skipped", False),
        ("workflow_dispatch", None, True),
        ("push", None, False),
    ],
)
def test_deploy_condition_only_fires_on_dispatch_or_successful_build(
    deploy_workflow, event_name, conclusion, should_deploy
):
    condition = deploy_workflow["jobs"]["deploy"]["if"]
    py_expr = _condition_as_python(condition)

    github = {"event_name": event_name}
    event = {"workflow_run": {"conclusion": conclusion}}

    result = eval(py_expr, {"__builtins__": {}}, {"github": github, "event": event})
    assert result is should_deploy, (
        f"event_name={event_name!r} conclusion={conclusion!r}: "
        f"expected should_deploy={should_deploy}, condition evaluated to {result!r}"
    )
