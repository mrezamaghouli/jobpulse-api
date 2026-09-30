"""Regression guard for
.github/workflows/production-incident-259c507-image-build.yml.

Context: production runs f4765f857355c6543f68cea0e481b7f20a917147 (f476).
The LinkedIn header-artifact false-positive fix is
259c507e10def1cae5a4f10cd718811981233f0d, whose sole parent is f476. main
(and its :main image) additionally carries the Phase 4B poster-profile
provider change, so the incident needs its own exact-source image. This
workflow builds and pushes exactly that one image -- nothing else.

These tests never dispatch the workflow, never contact a registry, never
SSH, and never run Docker. Static checks parse the committed YAML/shell as
text; behavioral checks execute the workflow's own embedded shell and
Python (extracted verbatim) against local git objects and a fake registry.
"""
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess as sp
import urllib.error
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-incident-259c507-image-build.yml"

TARGET_SOURCE_SHA = "259c507e10def1cae5a4f10cd718811981233f0d"
TARGET_PARENT_SHA = "f4765f857355c6543f68cea0e481b7f20a917147"
MAIN_WITH_PHASE_4B_SHA = "b63cb54d64832c0ba47df4ebbef944995f285cca"
BASELINE_INDEX_DIGEST = "sha256:7739628f61c3bba88ff4e395c0f0a0ce3bea3e3abb40956207b495f9d909b753"
IMAGE_REPOSITORY = "ghcr.io/mrezamaghouli/jobpulse-api"
INCIDENT_IMAGE_TAG = f"incident-{TARGET_SOURCE_SHA}"
CONFIRMATION_TOKEN = "BUILD_259C507_INCIDENT_IMAGE"

VERIFY_CHECKOUT_STEP = "Verify exact checkout and parent"
SOURCE_DELTA_STEP = "Verify source delta from f476 is exactly the incident allowlist"
BEHAVIOR_STEP = "Verify incident predicate behavior (differential, f476 vs 259c507)"
TESTS_STEP = "Run incident regression tests from the exact incident source"
REGISTRY_HELPER_STEP = "Write registry provenance helper"
TAG_ABSENT_STEP = "Refuse to overwrite an existing incident tag"
BUILD_STEP = "Build and push incident image (linux/amd64 only)"
PROVENANCE_STEP = "Verify pushed image provenance and compare with f476"
UPLOAD_STEP = "Upload provenance record"


# =====================================================================
# Fixtures / helpers
# =====================================================================
@pytest.fixture(scope="module")
def workflow_text() -> str:
    return WORKFLOW_PATH.read_text()


@pytest.fixture(scope="module")
def workflow(workflow_text) -> dict:
    return yaml.safe_load(workflow_text)


def _triggers(workflow: dict):
    # PyYAML parses a bare `on:` key as boolean True.
    return workflow["on"] if "on" in workflow else workflow[True]


def _steps(workflow, job):
    return workflow["jobs"][job]["steps"]


def _step(workflow, job, name):
    for step in _steps(workflow, job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found in job {job!r}")


def _step_index(workflow, job, name):
    return [s.get("name") for s in _steps(workflow, job)].index(name)


def _all_run_scripts(workflow):
    for job_name, job in workflow["jobs"].items():
        for step in job["steps"]:
            if "run" in step:
                yield job_name, step.get("name"), step["run"]


def _heredoc(run_text, marker):
    start = f"<<'{marker}'\n"
    assert start in run_text, f"heredoc {marker} not found"
    body = run_text.split(start, 1)[1]
    lines = body.split("\n")
    end = lines.index(marker)
    return "\n".join(lines[:end]) + "\n"


def _git(*args, cwd=REPO_ROOT, check=True):
    return sp.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _have_commits(*shas):
    return all(
        _git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0 for sha in shas
    )


requires_incident_commits = pytest.mark.skipif(
    not _have_commits(TARGET_SOURCE_SHA, TARGET_PARENT_SHA),
    reason="incident commits not present in this clone (needs full history)",
)


@pytest.fixture(scope="module")
def registry_module(workflow, tmp_path_factory):
    run_text = _step(workflow, "build-and-push", REGISTRY_HELPER_STEP)["run"]
    source = _heredoc(run_text, "REGISTRY_PY")
    path = tmp_path_factory.mktemp("registry") / "incident_registry.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("incident_registry_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def incident_clone(tmp_path_factory):
    """A throwaway local clone checked out at exactly 259c507, with an
    origin/main ref that contains it (mirrors actions/checkout's state)."""
    if not _have_commits(TARGET_SOURCE_SHA, TARGET_PARENT_SHA):
        pytest.skip("incident commits not present in this clone")
    clone = tmp_path_factory.mktemp("clone") / "repo"
    sp.run(["git", "clone", "-q", "--no-checkout", "--shared", str(REPO_ROOT), str(clone)], check=True)
    _git("checkout", "-q", "--detach", TARGET_SOURCE_SHA, cwd=clone)
    main_ref = MAIN_WITH_PHASE_4B_SHA if _have_commits(MAIN_WITH_PHASE_4B_SHA) else TARGET_SOURCE_SHA
    _git("update-ref", "refs/remotes/origin/main", main_ref, cwd=clone)
    return clone


def _run_step_script(run_text, cwd, env_overrides, tmp_path):
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "GITHUB_OUTPUT": str(output_file),
        "RUNNER_TEMP": str(runner_temp),
        "TARGET_SOURCE_SHA": TARGET_SOURCE_SHA,
        "TARGET_PARENT_SHA": TARGET_PARENT_SHA,
    }
    env.update(env_overrides)
    result = sp.run(["bash", "-c", run_text], cwd=cwd, env=env, capture_output=True, text=True)
    outputs = dict(
        line.split("=", 1) for line in output_file.read_text().splitlines() if "=" in line
    )
    return result, outputs


# =====================================================================
# Trigger / input surface
# =====================================================================
def test_workflow_is_workflow_dispatch_only(workflow):
    triggers = _triggers(workflow)
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}


def test_only_input_is_the_confirmation_token(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"confirmation"}
    assert inputs["confirmation"]["required"] is True
    assert inputs["confirmation"]["type"] == "string"


def test_no_arbitrary_sha_or_ref_can_be_supplied(workflow, workflow_text):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    for name in inputs:
        assert not re.search(r"sha|ref|commit|image|tag", name, re.IGNORECASE)
    # The only inputs.* reference anywhere is the confirmation token, and it
    # is routed through env -- never interpolated into a script.
    assert re.findall(r"inputs\.[A-Za-z_]+", workflow_text) == ["inputs.confirmation"]
    for _, _, run_text in _all_run_scripts(workflow):
        assert "${{" not in run_text


def test_confirmation_token_is_required_first_in_every_job(workflow):
    for job in ("verify-source", "build-and-push"):
        first = _steps(workflow, job)[0]
        assert CONFIRMATION_TOKEN in first["run"]
        assert '"${CONFIRMATION:-}" != "BUILD_259C507_INCIDENT_IMAGE"' in first["run"]
        assert "exit 1" in first["run"]
    assert workflow["env"]["CONFIRMATION"] == "${{ inputs.confirmation }}"


@pytest.mark.parametrize("job", ["verify-source", "build-and-push"])
def test_every_job_requires_dispatch_from_main(workflow, job):
    guarded = [
        step for step in _steps(workflow, job)[:2]
        if '!= "refs/heads/main"' in step.get("run", "")
    ]
    assert guarded, f"{job} does not require refs/heads/main before checkout"


@pytest.mark.parametrize(
    "token,ref,should_pass",
    [
        (CONFIRMATION_TOKEN, "refs/heads/main", True),
        ("build_259c507_incident_image", "refs/heads/main", False),
        ("", "refs/heads/main", False),
        (CONFIRMATION_TOKEN, "refs/heads/feature", False),
    ],
)
def test_guard_script_behavior(workflow, tmp_path, token, ref, should_pass):
    run_text = _steps(workflow, "build-and-push")[0]["run"]
    result, _ = _run_step_script(
        run_text,
        tmp_path,
        {
            "CONFIRMATION": token,
            "GITHUB_REF": ref,
            "INCIDENT_IMAGE_TAG": INCIDENT_IMAGE_TAG,
        },
        tmp_path,
    )
    assert (result.returncode == 0) is should_pass, result.stderr


# =====================================================================
# Hard pins
# =====================================================================
def test_target_and_parent_are_hard_pinned(workflow):
    env = workflow["env"]
    assert env["TARGET_SOURCE_SHA"] == TARGET_SOURCE_SHA
    assert env["TARGET_PARENT_SHA"] == TARGET_PARENT_SHA
    assert env["BASELINE_INDEX_DIGEST"] == BASELINE_INDEX_DIGEST
    assert env["IMAGE_REPOSITORY"] == IMAGE_REPOSITORY


def test_both_checkouts_use_exactly_the_pinned_sha(workflow):
    for job in ("verify-source", "build-and-push"):
        checkouts = [s for s in _steps(workflow, job) if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert len(checkouts) == 1
        assert checkouts[0]["with"]["ref"] == "${{ env.TARGET_SOURCE_SHA }}"
        assert checkouts[0]["with"]["persist-credentials"] is False
        assert checkouts[0]["with"]["fetch-depth"] == 0


def test_workflow_never_references_main_head_sha(workflow_text):
    assert "github.sha" not in workflow_text
    assert "github.event.workflow_run" not in workflow_text


# =====================================================================
# Exact checkout verification (executed against a real local clone)
# =====================================================================
def test_checkout_verification_checks_head_and_single_parent(workflow):
    run_text = _step(workflow, "verify-source", VERIFY_CHECKOUT_STEP)["run"]
    assert 'head_sha="$(git rev-parse HEAD)"' in run_text
    assert '"$head_sha" != "$TARGET_SOURCE_SHA"' in run_text
    assert "git rev-list --parents -n 1 HEAD" in run_text
    assert '"$parents" != "$TARGET_PARENT_SHA"' in run_text
    assert "git merge-base --is-ancestor" in run_text
    assert "git status --porcelain" in run_text
    rerun = _step(workflow, "build-and-push", "Re-verify exact checkout and build inputs")["run"]
    assert '"$head_sha" != "$TARGET_SOURCE_SHA"' in rerun
    assert '"$parents" != "$TARGET_PARENT_SHA"' in rerun


def test_checkout_verification_passes_on_exact_incident_commit(workflow, incident_clone, tmp_path):
    run_text = _step(workflow, "verify-source", VERIFY_CHECKOUT_STEP)["run"]
    result, _ = _run_step_script(run_text, incident_clone, {}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"HEAD={TARGET_SOURCE_SHA}" in result.stdout


def test_checkout_verification_fails_when_head_is_not_the_pin(workflow, incident_clone, tmp_path):
    run_text = _step(workflow, "verify-source", VERIFY_CHECKOUT_STEP)["run"]
    _git("checkout", "-q", "--detach", TARGET_PARENT_SHA, cwd=incident_clone)
    try:
        result, _ = _run_step_script(run_text, incident_clone, {}, tmp_path)
    finally:
        _git("checkout", "-q", "--detach", TARGET_SOURCE_SHA, cwd=incident_clone)
    assert result.returncode != 0
    assert "expected exactly" in result.stderr


def test_checkout_verification_fails_on_wrong_parent(workflow, incident_clone, tmp_path):
    run_text = _step(workflow, "verify-source", VERIFY_CHECKOUT_STEP)["run"]
    wrong_parent = "0" * 40
    result, _ = _run_step_script(run_text, incident_clone, {"TARGET_PARENT_SHA": wrong_parent}, tmp_path)
    assert result.returncode != 0
    assert "parents are" in result.stderr


def test_checkout_verification_fails_on_dirty_worktree(workflow, incident_clone, tmp_path):
    run_text = _step(workflow, "verify-source", VERIFY_CHECKOUT_STEP)["run"]
    stray = incident_clone / "stray.txt"
    stray.write_text("x")
    try:
        result, _ = _run_step_script(run_text, incident_clone, {}, tmp_path)
    finally:
        stray.unlink()
    assert result.returncode != 0
    assert "not clean" in result.stderr


# =====================================================================
# Source-delta allowlist (executed against a real local clone)
# =====================================================================
def test_source_delta_allowlist_is_exact(workflow):
    run_text = _step(workflow, "verify-source", SOURCE_DELTA_STEP)["run"]
    assert "git diff --no-renames --name-status" in run_text
    assert (
        "expected=\"$(printf 'M\\tscripts/collector_postgres.py\\nM\\ttests/test_collector_outcomes.py')\""
        in run_text
    )
    for path in ("Dockerfile", "requirements.txt", "requirements.prod.txt", ".dockerignore"):
        assert path in run_text


def test_source_delta_passes_for_the_real_incident_commit(workflow, incident_clone, tmp_path):
    run_text = _step(workflow, "verify-source", SOURCE_DELTA_STEP)["run"]
    result, outputs = _run_step_script(run_text, incident_clone, {}, tmp_path)
    assert result.returncode == 0, result.stderr
    dockerfile = _git("show", f"{TARGET_SOURCE_SHA}:Dockerfile").stdout.encode()
    assert outputs["dockerfile_sha256"] == hashlib.sha256(dockerfile).hexdigest()
    for key in (
        "source_tree_sha",
        "collector_blob_sha",
        "dockerfile_sha256",
        "requirements_sha256",
        "requirements_prod_sha256",
        "dockerignore_sha256",
    ):
        assert outputs.get(key), key
    assert outputs["collector_blob_sha"] == _git("rev-parse", f"{TARGET_SOURCE_SHA}:scripts/collector_postgres.py").stdout.strip()


@pytest.mark.skipif(not _have_commits(MAIN_WITH_PHASE_4B_SHA), reason="b63cb54 not present")
def test_source_delta_rejects_main_with_phase_4b(workflow, incident_clone, tmp_path):
    """b63cb54 (main) vs f476 also changes the Phase 4B provider/frontend
    and many workflows -- it must never pass the incident allowlist."""
    run_text = _step(workflow, "verify-source", SOURCE_DELTA_STEP)["run"]
    result, _ = _run_step_script(
        run_text, incident_clone, {"TARGET_SOURCE_SHA": MAIN_WITH_PHASE_4B_SHA}, tmp_path
    )
    assert result.returncode != 0
    assert "not exactly the reviewed incident allowlist" in result.stderr
    assert "scripts/providers/linkedin_browser_provider.py" in result.stderr


def test_source_delta_rejects_empty_delta(workflow, incident_clone, tmp_path):
    run_text = _step(workflow, "verify-source", SOURCE_DELTA_STEP)["run"]
    result, _ = _run_step_script(
        run_text, incident_clone, {"TARGET_SOURCE_SHA": TARGET_PARENT_SHA}, tmp_path
    )
    assert result.returncode != 0


# =====================================================================
# Incident predicate behavior (differential, executed)
# =====================================================================
def _run_behavior(workflow, tmp_path, baseline_source, target_source):
    run_text = _step(workflow, "verify-source", BEHAVIOR_STEP)["run"]
    script = _heredoc(run_text, "BEHAVIOR_PY")
    baseline = tmp_path / "baseline.py"
    target = tmp_path / "target.py"
    baseline.write_text(baseline_source)
    target.write_text(target_source)
    return sp.run(
        ["python3", "-c", script],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "BASELINE_PATH": str(baseline), "TARGET_PATH": str(target)},
        capture_output=True,
        text=True,
    )


def _collector_source(sha):
    return _git("show", f"{sha}:scripts/collector_postgres.py").stdout


def test_behavior_step_uses_ast_extraction_of_both_sources(workflow):
    run_text = _step(workflow, "verify-source", BEHAVIOR_STEP)["run"]
    assert 'git show "${TARGET_PARENT_SHA}:scripts/collector_postgres.py"' in run_text
    script = _heredoc(run_text, "BEHAVIOR_PY")
    assert "ast.parse" in script
    assert "is_invalid_linkedin_search_header_job" in script
    assert "INCIDENT_PREDICATE_BEHAVIOR_VERIFIED" in script


@requires_incident_commits
def test_behavior_check_passes_for_real_f476_and_259c507(workflow, tmp_path):
    result = _run_behavior(
        workflow, tmp_path, _collector_source(TARGET_PARENT_SHA), _collector_source(TARGET_SOURCE_SHA)
    )
    assert result.returncode == 0, result.stderr
    assert "INCIDENT_PREDICATE_BEHAVIOR_VERIFIED" in result.stdout


@requires_incident_commits
def test_behavior_check_fails_if_target_still_has_unsafe_location_rejection(workflow, tmp_path):
    parent = _collector_source(TARGET_PARENT_SHA)
    result = _run_behavior(workflow, tmp_path, parent, parent)
    assert result.returncode != 0
    assert "BEHAVIOR_CHECK_FAILED" in result.stderr


@requires_incident_commits
def test_behavior_check_fails_if_title_header_filter_is_removed(workflow, tmp_path):
    target = _collector_source(TARGET_SOURCE_SHA)
    weakened = target.replace('if title.lower().endswith(" jobs in germany"):', "if False:")
    weakened = weakened.replace(
        'if re.match(r"^\\d+\\s+.+\\s+Jobs\\s+in\\s+.+$", title, re.IGNORECASE):', "if False:"
    )
    assert weakened != target
    result = _run_behavior(workflow, tmp_path, _collector_source(TARGET_PARENT_SHA), weakened)
    assert result.returncode != 0
    assert "does not reject header title" in result.stderr


@requires_incident_commits
def test_behavior_check_fails_if_location_reference_is_reintroduced(workflow, tmp_path):
    target = _collector_source(TARGET_SOURCE_SHA)
    anchor = 'if title.lower().endswith(" jobs in united kingdom"):\n        return True\n'
    assert target.count(anchor) == 1
    sneaky = target.replace(
        anchor,
        anchor + '\n    if str(job.get("location") or "") == "__never__":\n        return True\n',
    )
    assert sneaky != target
    result = _run_behavior(workflow, tmp_path, _collector_source(TARGET_PARENT_SHA), sneaky)
    assert result.returncode != 0
    assert "still references location/description" in result.stderr


def test_regression_tests_run_from_the_incident_checkout_only(workflow):
    steps = _steps(workflow, "verify-source")
    names = [s.get("name") for s in steps]
    run_text = _step(workflow, "verify-source", TESTS_STEP)["run"]
    assert "tests/test_collector_outcomes.py" in run_text
    for node in (
        "test_legitimate_job_missing_location_and_description_is_not_a_header_artifact",
        "test_legitimate_job_missing_location_and_description_reaches_insert_path",
        "test_genuine_header_style_titles_are_still_filtered",
        "test_legitimate_job_missing_location_and_description_does_not_increment_header_artifact_counter",
    ):
        assert f"tests/test_collector_outcomes.py::{node}" in run_text
    # Tests run after the pinned checkout and its verification, and there is
    # no second checkout in this job that could swap in main's tests.
    assert names.index(TESTS_STEP) > names.index(SOURCE_DELTA_STEP)
    assert sum(1 for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")) == 1
    install = _step(workflow, "verify-source", "Install test dependencies from the exact incident source")["run"]
    assert "pip install -r requirements.txt" in install


def test_named_incident_regressions_exist_at_the_incident_commit(workflow):
    if not _have_commits(TARGET_SOURCE_SHA):
        pytest.skip("incident commit not present")
    source = _git("show", f"{TARGET_SOURCE_SHA}:tests/test_collector_outcomes.py").stdout
    run_text = _step(workflow, "verify-source", TESTS_STEP)["run"]
    for node in re.findall(r"test_collector_outcomes\.py::(\w+)", run_text):
        assert f"def {node}(" in source, node


# =====================================================================
# Build: ordering, tags, platform
# =====================================================================
def test_build_job_depends_on_source_verification(workflow):
    assert workflow["jobs"]["build-and-push"]["needs"] == "verify-source"


def test_build_step_ordering_is_fail_closed(workflow):
    idx = lambda name: _step_index(workflow, "build-and-push", name)  # noqa: E731
    assert idx("Re-verify exact checkout and build inputs") < idx(TAG_ABSENT_STEP)
    assert idx(REGISTRY_HELPER_STEP) < idx(TAG_ABSENT_STEP) < idx(BUILD_STEP)
    assert idx(BUILD_STEP) < idx(PROVENANCE_STEP) < idx(UPLOAD_STEP)


def test_build_pushes_exactly_one_incident_specific_tag(workflow):
    build = _step(workflow, "build-and-push", BUILD_STEP)
    assert build["uses"].startswith("docker/build-push-action@")
    tags = [t for t in str(build["with"]["tags"]).splitlines() if t.strip()]
    assert tags == ["${{ env.IMAGE_REPOSITORY }}:${{ env.INCIDENT_IMAGE_TAG }}"]
    assert workflow["env"]["INCIDENT_IMAGE_TAG"] == INCIDENT_IMAGE_TAG
    assert build["with"]["push"] is True
    assert build["with"]["context"] == "."
    assert build["with"]["file"] == "./Dockerfile"


def test_build_is_linux_amd64_only_without_shared_cache(workflow):
    build = _step(workflow, "build-and-push", BUILD_STEP)["with"]
    assert build["platforms"] == "linux/amd64"
    assert build["no-cache"] is True
    assert "cache-from" not in build and "cache-to" not in build
    assert build["provenance"] == "mode=min"


def test_build_labels_record_source_and_parent(workflow):
    labels = _step(workflow, "build-and-push", BUILD_STEP)["with"]["labels"]
    assert "org.opencontainers.image.revision=${{ env.TARGET_SOURCE_SHA }}" in labels
    assert "io.jobpulse.incident.parent=${{ env.TARGET_PARENT_SHA }}" in labels


@pytest.mark.parametrize(
    "forbidden",
    [
        "jobpulse-api:main",
        ":main\n",
        "jobpulse-api:latest",
        ":latest",
        f"jobpulse-api:{TARGET_PARENT_SHA}",
        f"jobpulse-api:{TARGET_SOURCE_SHA}",
        f"jobpulse-api:{MAIN_WITH_PHASE_4B_SHA}",
        "type=gha",
    ],
)
def test_no_forbidden_tag_or_cache_anywhere(workflow_text, forbidden):
    code = "\n".join(
        line for line in workflow_text.splitlines() if not line.lstrip().startswith("#")
    ) + "\n"
    assert forbidden not in code


def test_incident_tag_guard_rejects_mismatched_tag(workflow, tmp_path):
    run_text = _steps(workflow, "build-and-push")[0]["run"]
    result, _ = _run_step_script(
        run_text,
        tmp_path,
        {"CONFIRMATION": CONFIRMATION_TOKEN, "GITHUB_REF": "refs/heads/main", "INCIDENT_IMAGE_TAG": "main"},
        tmp_path,
    )
    assert result.returncode != 0


# =====================================================================
# Forbidden actions: no production, no deploy, no dispatch, no collection
# =====================================================================
@pytest.mark.parametrize(
    "pattern",
    [
        r"\bssh\b",
        r"\bscp\b",
        r"ssh-keyscan",
        r"VM_HOST",
        r"VM_SSH_KEY",
        r"35\.192\.251\.190",
        r"/opt/jobpulse",
        r"docker[ -]compose",
        r"deploy_prod_from_ghcr",
        r"\bdocker (run|exec|pull|push|tag|rm|stop|restart)\b",
        r"gh workflow",
        r"workflow run",
        r"/dispatches",
        r"repository_dispatch",
        r"createWorkflowDispatch",
        r"production-api-reference-normalization",
        r"production-prenormalization-readonly-verification",
        r"NORMALIZE_F476_API_REFERENCE",
        r"production-direct-runtime-upgrade\.yml",
        r"run_collection_cycle",
        r"run_production_collection",
        r"linkedin_plan_collect",
        r"process_search_demand_queue",
        r"collection_heartbeat",
        r"psql\b",
        r"TOR_ENABLED",
        r"crontab",
        r"systemctl",
    ],
)
def test_no_forbidden_production_action(workflow_text, pattern):
    # Strip comments: prose may describe what the workflow never does.
    code = "\n".join(
        line for line in workflow_text.splitlines() if not line.lstrip().startswith("#")
    )
    assert not re.search(pattern, code), pattern


def test_permissions_are_minimal(workflow):
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["verify-source"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["build-and-push"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }


def test_no_repository_secrets_other_than_github_token(workflow_text):
    assert set(re.findall(r"secrets\.([A-Za-z_]+)", workflow_text)) == {"GITHUB_TOKEN"}


def test_concurrency_serializes_runs(workflow):
    assert workflow["concurrency"]["group"] == "jobpulse-incident-259c507-image-build"
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_every_run_script_is_strict_and_parses(workflow, tmp_path):
    for job, name, run_text in _all_run_scripts(workflow):
        assert run_text.lstrip().startswith("set -euo pipefail"), (job, name)
        script = tmp_path / "step.sh"
        script.write_text(run_text)
        result = sp.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, (job, name, result.stderr)


# =====================================================================
# Provenance outputs
# =====================================================================
PROVENANCE_ENV_KEYS = (
    "SOURCE_TREE_SHA",
    "COLLECTOR_BLOB_SHA",
    "DOCKERFILE_SHA256",
    "REQUIREMENTS_SHA256",
    "REQUIREMENTS_PROD_SHA256",
    "DOCKERIGNORE_SHA256",
)


def test_verify_source_exports_build_input_hashes(workflow):
    outputs = workflow["jobs"]["verify-source"]["outputs"]
    assert set(outputs) == {
        "source_tree_sha",
        "collector_blob_sha",
        "dockerfile_sha256",
        "requirements_sha256",
        "requirements_prod_sha256",
        "dockerignore_sha256",
    }


def test_provenance_step_receives_digests_and_source_hashes(workflow):
    step = _step(workflow, "build-and-push", PROVENANCE_STEP)
    env = step["env"]
    assert env["BUILT_INDEX_DIGEST"] == "${{ steps.build.outputs.digest }}"
    assert env["BUILT_IMAGE_ID"] == "${{ steps.build.outputs.imageid }}"
    for key in PROVENANCE_ENV_KEYS:
        assert env[key].startswith("${{ needs.verify-source.outputs."), key
    assert 'incident_registry.py" verify' in step["run"]
    assert "GITHUB_STEP_SUMMARY" in step["run"]
    assert "does NOT authorize deploying" in step["run"]


def test_provenance_record_is_uploaded(workflow):
    upload = _step(workflow, "build-and-push", UPLOAD_STEP)
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["if-no-files-found"] == "error"


# =====================================================================
# Registry helper (executed against a fake registry)
# =====================================================================
def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _dumps(obj) -> bytes:
    return json.dumps(obj, sort_keys=True).encode()


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def _make_image(store, *, labels, cmd=None, env=None, diff_ids=None, extra_entries=(), arch="amd64"):
    config = {
        "os": "linux",
        "architecture": arch,
        "created": "2026-10-01T00:00:00Z",
        "config": {
            "Cmd": cmd or ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"],
            "WorkingDir": "/app",
            "ExposedPorts": {"8000/tcp": {}},
            "Env": env or ["PATH=/usr/local/bin", "PYTHON_VERSION=3.12.11"],
            "Labels": labels,
        },
        "rootfs": {"type": "layers", "diff_ids": diff_ids or ["sha256:" + "a" * 64, "sha256:" + "b" * 64]},
    }
    config_body = _dumps(config)
    store[f"blobs/{_digest(config_body)}"] = (200, config_body)
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {"digest": _digest(config_body)},
        "layers": [{"digest": "sha256:" + "c" * 64}, {"digest": "sha256:" + "d" * 64}],
    }
    manifest_body = _dumps(manifest)
    manifest_digest = _digest(manifest_body)
    store[f"manifests/{manifest_digest}"] = (200, manifest_body)
    attestation_body = _dumps({"schemaVersion": 2, "mediaType": OCI_MANIFEST, "layers": []})
    store[f"manifests/{_digest(attestation_body)}"] = (200, attestation_body)
    entries = [
        {"mediaType": OCI_MANIFEST, "digest": manifest_digest, "platform": {"os": "linux", "architecture": "amd64"}},
        {
            "mediaType": OCI_MANIFEST,
            "digest": _digest(attestation_body),
            "platform": {"os": "unknown", "architecture": "unknown"},
            "annotations": {
                "vnd.docker.reference.type": "attestation-manifest",
                "vnd.docker.reference.digest": manifest_digest,
            },
        },
        *extra_entries,
    ]
    index_body = _dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries})
    index_digest = _digest(index_body)
    store[f"manifests/{index_digest}"] = (200, index_body)
    return index_digest, index_body


class FakeRegistry:
    def __init__(self, store):
        self.store = store

    def get(self, path):
        return self.store.get(path, (404, b""))


def _provenance_env(index_digest, baseline_digest):
    return {
        "TARGET_SOURCE_SHA": TARGET_SOURCE_SHA,
        "TARGET_PARENT_SHA": TARGET_PARENT_SHA,
        "INCIDENT_IMAGE_TAG": INCIDENT_IMAGE_TAG,
        "IMAGE_REPOSITORY": IMAGE_REPOSITORY,
        "BUILT_INDEX_DIGEST": index_digest,
        "BUILT_IMAGE_ID": "sha256:" + "e" * 64,
        "BASELINE_INDEX_DIGEST": baseline_digest,
        "SOURCE_TREE_SHA": "1" * 40,
        "COLLECTOR_BLOB_SHA": "2" * 40,
        "DOCKERFILE_SHA256": "3" * 64,
        "REQUIREMENTS_SHA256": "4" * 64,
        "REQUIREMENTS_PROD_SHA256": "5" * 64,
        "DOCKERIGNORE_SHA256": "6" * 64,
        "GITHUB_RUN_ID": "123",
    }


INCIDENT_LABELS = {
    "org.opencontainers.image.revision": TARGET_SOURCE_SHA,
    "io.jobpulse.incident.parent": TARGET_PARENT_SHA,
}


@pytest.fixture
def fake_images():
    store = {}
    baseline_digest, _ = _make_image(store, labels=None, env=["PATH=/usr/local/bin", "PYTHON_VERSION=3.12.10"])
    target_digest, target_body = _make_image(store, labels=INCIDENT_LABELS)
    store[f"manifests/{INCIDENT_IMAGE_TAG}"] = (200, target_body)
    return store, target_digest, baseline_digest


def test_registry_helper_happy_path_records_full_provenance(registry_module, fake_images):
    store, target, baseline = fake_images
    record = registry_module.build_provenance(FakeRegistry(store), _provenance_env(target, baseline))
    assert record["source_commit"] == TARGET_SOURCE_SHA
    assert record["source_parent"] == TARGET_PARENT_SHA
    assert record["oci_index_digest"] == target
    assert record["image_digest_reference"] == f"{IMAGE_REPOSITORY}@{target}"
    assert record["image_tag_reference"] == f"{IMAGE_REPOSITORY}:{INCIDENT_IMAGE_TAG}"
    assert record["linux_amd64_manifest_digest"].startswith("sha256:")
    assert record["image_config_digest"].startswith("sha256:")
    assert len(record["attestation_manifest_digests"]) == 1
    for key in (
        "dockerfile_sha256",
        "requirements_txt_sha256",
        "requirements_prod_txt_sha256",
        "dockerignore_sha256",
        "source_tree",
        "collector_postgres_blob",
        "image_id_note",
    ):
        assert record[key]
    assert record["deployment_authorized"] is False
    assert "NOT claimed to be byte-identical" in record["reproducibility"]
    comparison = record["comparison_with_f476"]
    assert comparison["byte_identical_to_baseline"] is False
    assert comparison["fatal_differences"] == []
    fields = {d["field"] for d in comparison["informational_differences"]}
    assert {"Env.PYTHON_VERSION", "Labels"} <= fields
    assert record["baseline"]["oci_index_digest"] == baseline


def test_registry_helper_rejects_runtime_contract_drift(registry_module, fake_images):
    store, _, baseline = fake_images
    drifted, body = _make_image(store, labels=INCIDENT_LABELS, cmd=["python", "other.py"])
    store[f"manifests/{INCIDENT_IMAGE_TAG}"] = (200, body)
    with pytest.raises(registry_module.ProvenanceError, match="runtime contract differs"):
        registry_module.build_provenance(FakeRegistry(store), _provenance_env(drifted, baseline))


def test_registry_helper_rejects_tag_resolving_elsewhere(registry_module, fake_images):
    store, target, baseline = fake_images
    store[f"manifests/{INCIDENT_IMAGE_TAG}"] = (200, b"{}")
    with pytest.raises(registry_module.ProvenanceError, match="resolves to"):
        registry_module.build_provenance(FakeRegistry(store), _provenance_env(target, baseline))


def test_registry_helper_rejects_wrong_revision_label(registry_module, fake_images):
    store, _, baseline = fake_images
    wrong, body = _make_image(
        store, labels={**INCIDENT_LABELS, "org.opencontainers.image.revision": MAIN_WITH_PHASE_4B_SHA}
    )
    store[f"manifests/{INCIDENT_IMAGE_TAG}"] = (200, body)
    with pytest.raises(registry_module.ProvenanceError, match="revision label"):
        registry_module.build_provenance(FakeRegistry(store), _provenance_env(wrong, baseline))


def test_registry_helper_rejects_non_incident_tag(registry_module, fake_images):
    store, target, baseline = fake_images
    env = _provenance_env(target, baseline)
    env["INCIDENT_IMAGE_TAG"] = "main"
    with pytest.raises(registry_module.ProvenanceError, match="incident tag"):
        registry_module.build_provenance(FakeRegistry(store), env)


def test_registry_helper_rejects_built_digest_equal_to_baseline(registry_module, fake_images):
    store, _, baseline = fake_images
    with pytest.raises(registry_module.ProvenanceError, match="equals the f476 baseline"):
        registry_module.build_provenance(FakeRegistry(store), _provenance_env(baseline, baseline))


@pytest.mark.parametrize("bad", ["", "latest", "sha256:abc", None])
def test_registry_helper_rejects_malformed_built_digest(registry_module, fake_images, bad):
    store, _, baseline = fake_images
    env = _provenance_env("x", baseline)
    env["BUILT_INDEX_DIGEST"] = bad
    with pytest.raises(registry_module.ProvenanceError):
        registry_module.build_provenance(FakeRegistry(store), env)


def test_fetch_verified_rejects_tampered_content(registry_module, fake_images):
    store, target, _ = fake_images
    store[f"manifests/{target}"] = (200, store[f"manifests/{target}"][1] + b" ")
    with pytest.raises(registry_module.ProvenanceError, match="content hashes to"):
        registry_module.fetch_verified(FakeRegistry(store), "manifests", target)


def _index(entries, media_type=OCI_INDEX):
    return {"schemaVersion": 2, "mediaType": media_type, "manifests": entries}


AMD64 = {"mediaType": OCI_MANIFEST, "digest": "sha256:" + "1" * 64, "platform": {"os": "linux", "architecture": "amd64"}}
ATTESTATION = {
    "mediaType": OCI_MANIFEST,
    "digest": "sha256:" + "2" * 64,
    "platform": {"os": "unknown", "architecture": "unknown"},
    "annotations": {"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": AMD64["digest"]},
}


def test_select_runtime_manifest_picks_the_single_amd64_entry(registry_module):
    selected, attestations = registry_module.select_runtime_manifest(_index([AMD64, ATTESTATION]))
    assert selected == AMD64
    assert attestations == [ATTESTATION]


@pytest.mark.parametrize(
    "entries,media_type,message",
    [
        ([AMD64, dict(AMD64, digest="sha256:" + "3" * 64)], OCI_INDEX, "exactly one linux/amd64"),
        ([ATTESTATION], OCI_INDEX, "exactly one linux/amd64"),
        ([AMD64, dict(AMD64, digest="sha256:" + "4" * 64, platform={"os": "linux", "architecture": "arm64"})], OCI_INDEX, "unexpected image index entry"),
        ([AMD64, dict(ATTESTATION, annotations={"vnd.docker.reference.type": "sbom"})], OCI_INDEX, "unexpected index reference type"),
        (
            [AMD64, dict(ATTESTATION, annotations={"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": "sha256:" + "9" * 64})],
            OCI_INDEX,
            "does not reference the runtime manifest",
        ),
        ([AMD64], OCI_MANIFEST, "not an image index"),
        ([], OCI_INDEX, "no manifests"),
    ],
)
def test_select_runtime_manifest_fails_closed(registry_module, entries, media_type, message):
    with pytest.raises(registry_module.ProvenanceError, match=message):
        registry_module.select_runtime_manifest(_index(entries, media_type))


def test_describe_image_rejects_non_amd64_config(registry_module):
    store = {}
    digest, _ = _make_image(store, labels=None, arch="arm64")
    with pytest.raises(registry_module.ProvenanceError, match="not linux/amd64"):
        registry_module.describe_image(FakeRegistry(store), digest)


@pytest.mark.parametrize("status,ok", [(404, True), (200, False), (401, False), (500, False)])
def test_check_tag_absent_refuses_overwrite_and_unknown_state(registry_module, status, ok):
    registry = FakeRegistry({f"manifests/{INCIDENT_IMAGE_TAG}": (status, b"{}")} if status != 404 else {})
    if ok:
        registry_module.check_tag_absent(registry, INCIDENT_IMAGE_TAG)
    else:
        with pytest.raises(registry_module.ProvenanceError):
            registry_module.check_tag_absent(registry, INCIDENT_IMAGE_TAG)


class _FakeResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self, _n):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    def __init__(self, location):
        self.location = location
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        if len(self.requests) == 1:
            raise urllib.error.HTTPError(request.full_url, 307, "redirect", {"Location": self.location}, None)
        return _FakeResponse(b"blob")


def test_registry_redirect_drops_authorization_and_requires_https(registry_module):
    registry = registry_module.Registry("secret-token")
    registry.opener = _RecordingOpener("https://pkg-containers.githubusercontent.com/blob")
    status, body = registry.get("blobs/sha256:" + "0" * 64)
    assert (status, body) == (200, b"blob")
    first, second = registry.opener.requests
    assert first.get_header("Authorization") == "Bearer secret-token"
    assert second.get_header("Authorization") is None

    registry.opener = _RecordingOpener("http://insecure.example/blob")
    with pytest.raises(registry_module.ProvenanceError, match="not https"):
        registry.get("blobs/sha256:" + "0" * 64)


def test_registry_helper_never_pushes_or_deletes(registry_module):
    source = Path(registry_module.__file__).read_text()
    assert "method=" not in source
    for verb in ("PUT", "POST", "DELETE", "PATCH"):
        assert f'"{verb}"' not in source


def test_registry_helper_cli_is_restricted_to_two_read_only_commands(registry_module):
    with pytest.raises(registry_module.ProvenanceError, match="usage"):
        registry_module.main(["incident_registry.py", "push"])


# =====================================================================
# Local tooling sanity
# =====================================================================
def test_actionlint_if_available():
    actionlint = shutil.which("actionlint")
    if not actionlint:
        pytest.skip("actionlint not installed")
    result = sp.run([actionlint, str(WORKFLOW_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
