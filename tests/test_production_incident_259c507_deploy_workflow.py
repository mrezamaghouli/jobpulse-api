"""Structural + behavioral regression guard for
.github/workflows/production-incident-259c507-deploy.yml.

The workflow is the one-time, API-only production deployment of the
already-built immutable incident image (source 259c507, parent f476,
build run 36858099909). These tests never SSH, never dispatch anything,
use no real Docker, and make no network calls:

- static checks parse the committed YAML/shell as text;
- the runner-side GHCR verifier is imported from the workflow and run
  against an in-memory fake registry;
- the ACTUAL embedded remote shell script (extracted verbatim) is run via
  `bash -c` against fake docker/git/curl/crontab/sudo/systemctl/sleep
  executables (real flock/timeout/sha256sum/python3), proving preflight
  fail-closed behavior, API-only mutation, and rollback outcomes.
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess as sp
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW_PATH = WORKFLOWS / "production-incident-259c507-deploy.yml"
DOCKER_BUILD_WORKFLOW_PATH = WORKFLOWS / "docker-build.yml"
DEPLOY_WORKFLOW_PATH = WORKFLOWS / "deploy.yml"
NORMALIZATION_WORKFLOW_PATH = WORKFLOWS / "production-api-reference-normalization.yml"
UPGRADE_WORKFLOW_PATH = WORKFLOWS / "production-direct-runtime-upgrade.yml"
CI_WORKFLOW_PATH = WORKFLOWS / "ci.yml"
RUNBOOK_PATH = REPO_ROOT / "docs" / "PRODUCTION_RUNBOOK.md"

CONFIRMATION_TOKEN = "DEPLOY_259C507_INCIDENT_IMAGE"
TARGET_SOURCE_SHA = "259c507e10def1cae5a4f10cd718811981233f0d"
EXPECTED_PRODUCTION_GIT_SHA = "f4765f857355c6543f68cea0e481b7f20a917147"
INCIDENT_IMAGE_REFERENCE = "ghcr.io/mrezamaghouli/jobpulse-api:incident-259c507e10def1cae5a4f10cd718811981233f0d"
INCIDENT_IMAGE_INDEX_DIGEST = "sha256:91cd873be81d013c16503fb7b5ccec3790946203ec1c22ade31fef1882e8e5c4"
INCIDENT_IMAGE_AMD64_MANIFEST_DIGEST = "sha256:24d18483e82bac1932bf4ddd6eaf3d1f7cca321e0247bba6fe06220e09e07316"
INCIDENT_IMAGE_CONFIG_DIGEST = "sha256:b905231fe790bc21c12c89303986c76a94c329b5d1b53b54d49265a62b08d1f7"
INCIDENT_BUILD_RUN_ID = "36858099909"
EXPECTED_F476_IMAGE_ID = "sha256:7739628f61c3bba88ff4e395c0f0a0ce3bea3e3abb40956207b495f9d909b753"
EXPECTED_F476_AMD64_MANIFEST_DIGEST = "sha256:75207004ec746504940d6bf1b61dd6a86901c5d6ecd2499987c5bc1220580907"
EXPECTED_F476_CONFIG_DIGEST = "sha256:fd9f1eedc8924304aba851770b56f17c8b3dde30a6ecf246dee79129073a9801"
INCIDENT_PURPOSE_LABEL = "linkedin-header-artifact-false-positive-fix"
INCIDENT_IMAGE_DIGEST_REFERENCE = f"ghcr.io/mrezamaghouli/jobpulse-api@{INCIDENT_IMAGE_INDEX_DIGEST}"
KNOWN_F476_ROLLBACK_ALIAS = "jobpulse-api-rollback:bccbbd997ee8-1427495"
CANONICAL_F476_REFERENCE = "ghcr.io/mrezamaghouli/jobpulse-api:f4765f857355c6543f68cea0e481b7f20a917147"
F476_IMAGE_DIGEST_REFERENCE = f"ghcr.io/mrezamaghouli/jobpulse-api@{'sha256:7739628f61c3bba88ff4e395c0f0a0ce3bea3e3abb40956207b495f9d909b753'}"
MAIN_B63_SHA = "b63cb54d64832c0ba47df4ebbef944995f285cca"
MAIN_370_SHA = "370562b6ac241aa4530f3cd2ad2299c3cb73b06f"

REMOTE_HEREDOC_START = "<<'REMOTE_SCRIPT'"
REMOTE_HEREDOC_END = "REMOTE_SCRIPT"
DEPLOY_STEP = "Run incident 259c507 API-only production deployment"
REGISTRY_STEP = "Write registry digest-chain verifier"
DERIVE_STEP = "Derive immutable source provenance from pinned git objects"

_REAL = {name: shutil.which(name) for name in ("sha256sum", "flock", "timeout", "python3")}


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
    return workflow["on"] if "on" in workflow else workflow[True]


@pytest.fixture(scope="module")
def steps(workflow) -> list:
    return workflow["jobs"]["deploy"]["steps"]


def _step(steps, name):
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"step {name!r} not found")


def _step_index(steps, name):
    return next(i for i, s in enumerate(steps) if s.get("name") == name)


def _split_remote_and_runner(run_text: str):
    start = run_text.index(REMOTE_HEREDOC_START) + len(REMOTE_HEREDOC_START)
    before = run_text[: run_text.index(REMOTE_HEREDOC_START)]
    lines = run_text[start:].splitlines()
    end_idx = next(i for i, line in enumerate(lines) if line.strip() == REMOTE_HEREDOC_END)
    return before + "\n".join(lines[end_idx + 1 :]), "\n".join(lines[:end_idx])


@pytest.fixture(scope="module")
def runner_script(steps) -> str:
    return _split_remote_and_runner(_step(steps, DEPLOY_STEP)["run"])[0]


@pytest.fixture(scope="module")
def remote_script(steps) -> str:
    return _split_remote_and_runner(_step(steps, DEPLOY_STEP)["run"])[1]


def _code_only(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _extract_bash_function(script: str, func_name: str) -> str:
    lines = script.splitlines()
    start_idx = next(
        i for i, line in enumerate(lines)
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


def _main_body(remote_script: str) -> str:
    """The remote script's top-level flow, after all function definitions."""
    return remote_script[remote_script.index("trap final_exit_trap EXIT") :]


def _heredoc(run_text: str, marker: str) -> str:
    lines = run_text.splitlines()
    start = next(i for i, line in enumerate(lines) if f"<<'{marker}'" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == marker)
    return "\n".join(lines[start + 1 : end])


# =====================================================================
# 1. Manual only, confirmation gate, no arbitrary inputs
# =====================================================================
def test_trigger_is_workflow_dispatch_only(workflow):
    triggers = _triggers(workflow)
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}


@pytest.mark.parametrize("key", ["push:", "pull_request", "schedule:", "workflow_run", "repository_dispatch", "workflow_call"])
def test_no_other_trigger_in_source(workflow_text, key):
    assert key not in workflow_text


def test_only_input_is_the_confirmation_token(workflow):
    inputs = _triggers(workflow)["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"confirmation"}
    assert inputs["confirmation"]["required"] is True
    assert inputs["confirmation"]["type"] == "string"


def test_no_operator_supplied_image_sha_ref_or_target(workflow_text):
    referenced_inputs = set(re.findall(r"inputs\.([A-Za-z0-9_]+)", workflow_text))
    assert referenced_inputs == {"confirmation"}
    assert "github.event.inputs" not in workflow_text


def test_confirmation_token_is_exact_and_unique_to_this_deployment(workflow_text):
    assert f'!= "{CONFIRMATION_TOKEN}"' in workflow_text
    for other in ("UPGRADE_DIRECT_RUNTIME", "NORMALIZE_F476_API_REFERENCE", "BUILD_259C507_INCIDENT_IMAGE"):
        assert other not in workflow_text


@pytest.mark.parametrize(
    "token,ref,ok",
    [
        (CONFIRMATION_TOKEN, "refs/heads/main", True),
        (CONFIRMATION_TOKEN.lower(), "refs/heads/main", False),
        (CONFIRMATION_TOKEN + " ", "refs/heads/main", False),
        ("", "refs/heads/main", False),
        ("UPGRADE_DIRECT_RUNTIME", "refs/heads/main", False),
        (CONFIRMATION_TOKEN, "refs/heads/feature", False),
    ],
)
def test_guard_steps_behavior(steps, token, ref, ok):
    main_guard = _step(steps, "Require dispatch from main")["run"].replace("${{ github.ref }}", ref)
    token_guard = _step(steps, "Require exact confirmation token")["run"]
    env = dict(os.environ, CONFIRMATION=token)
    results = [sp.run(["bash", "-c", s], env=env, capture_output=True, text=True) for s in (main_guard, token_guard)]
    assert all(r.returncode == 0 for r in results) is ok


def test_guards_run_first_before_any_checkout_or_secret(steps):
    names = [s.get("name") for s in steps]
    assert names[0] == "Require dispatch from main"
    assert names[1] == "Require exact confirmation token"
    for i, s in enumerate(steps):
        if "secrets." in json.dumps(s) or s.get("uses", "").startswith("actions/checkout"):
            assert i > 1, s.get("name")


def test_permissions_minimal(workflow):
    assert workflow["permissions"] == {"contents": "read", "packages": "read"}


def test_concurrency_shared_with_other_api_mutating_runners(workflow):
    group = workflow["concurrency"]["group"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    for other in (UPGRADE_WORKFLOW_PATH, NORMALIZATION_WORKFLOW_PATH):
        assert yaml.safe_load(other.read_text())["concurrency"]["group"] == group


def test_job_has_finite_timeout(workflow):
    assert 0 < workflow["jobs"]["deploy"]["timeout-minutes"] <= 30


def test_workflow_name_is_unique_and_not_referenced_by_workflow_run(workflow):
    for other in WORKFLOWS.glob("*.yml"):
        if other == WORKFLOW_PATH:
            continue
        data = yaml.safe_load(other.read_text())
        assert data.get("name") != workflow["name"], other
        assert workflow["name"] not in other.read_text(), other


# =====================================================================
# 2. Exact pins (workflow env, remote script, registry verifier)
# =====================================================================
PINS = {
    "TARGET_SOURCE_SHA": TARGET_SOURCE_SHA,
    "EXPECTED_PRODUCTION_GIT_SHA": EXPECTED_PRODUCTION_GIT_SHA,
    "INCIDENT_IMAGE_REFERENCE": INCIDENT_IMAGE_REFERENCE,
    "INCIDENT_IMAGE_INDEX_DIGEST": INCIDENT_IMAGE_INDEX_DIGEST,
    "INCIDENT_IMAGE_AMD64_MANIFEST_DIGEST": INCIDENT_IMAGE_AMD64_MANIFEST_DIGEST,
    "INCIDENT_IMAGE_CONFIG_DIGEST": INCIDENT_IMAGE_CONFIG_DIGEST,
    "INCIDENT_BUILD_RUN_ID": INCIDENT_BUILD_RUN_ID,
    "EXPECTED_F476_IMAGE_ID": EXPECTED_F476_IMAGE_ID,
    "EXPECTED_F476_AMD64_MANIFEST_DIGEST": EXPECTED_F476_AMD64_MANIFEST_DIGEST,
    "EXPECTED_F476_CONFIG_DIGEST": EXPECTED_F476_CONFIG_DIGEST,
    "INCIDENT_PURPOSE_LABEL": INCIDENT_PURPOSE_LABEL,
    "F476_IMAGE_DIGEST_REFERENCE": F476_IMAGE_DIGEST_REFERENCE,
}
ENV_ONLY_PINS = {
    "TARGET_PARENT_SHA": EXPECTED_PRODUCTION_GIT_SHA,
    "INCIDENT_IMAGE": INCIDENT_IMAGE_DIGEST_REFERENCE,
}


@pytest.mark.parametrize("name,value", sorted({**PINS, **ENV_ONLY_PINS}.items()))
def test_workflow_env_pins_exact(workflow, name, value):
    assert workflow["env"][name] == value


@pytest.mark.parametrize("name,value", sorted(PINS.items()))
def test_remote_script_pins_exact(remote_script, name, value):
    assert f'\n{name}="{value}"\n' in "\n" + remote_script + "\n"
    assert len(re.findall(rf"^{name}=", remote_script, re.MULTILINE)) == 1


def test_remote_script_extra_pins_exact(remote_script):
    assert f'INCIDENT_IMAGE_DIGEST_REFERENCE="{INCIDENT_IMAGE_DIGEST_REFERENCE}"' in remote_script
    assert f'EXPECTED_INCIDENT_IMAGE_ID="{INCIDENT_IMAGE_INDEX_DIGEST}"' in remote_script
    assert f'KNOWN_F476_ROLLBACK_ALIAS="{KNOWN_F476_ROLLBACK_ALIAS}"' in remote_script
    assert f'CANONICAL_F476_REFERENCE="{CANONICAL_F476_REFERENCE}"' in remote_script
    assert 'PRODUCTION_PROJECT="jobpulse"' in remote_script
    assert 'COMPOSE_FILE="/opt/jobpulse/docker-compose.prod.yml"' in remote_script


def test_registry_verifier_pins_exact(steps):
    source = _heredoc(_step(steps, REGISTRY_STEP)["run"], "REGISTRY_PY")
    assert f'INCIDENT_TAG = "incident-{TARGET_SOURCE_SHA}"' in source
    assert f'INCIDENT_INDEX_DIGEST = "{INCIDENT_IMAGE_INDEX_DIGEST}"' in source
    assert f'INCIDENT_AMD64_MANIFEST_DIGEST = "{INCIDENT_IMAGE_AMD64_MANIFEST_DIGEST}"' in source
    assert f'INCIDENT_CONFIG_DIGEST = "{INCIDENT_IMAGE_CONFIG_DIGEST}"' in source
    assert f'F476_INDEX_DIGEST = "{EXPECTED_F476_IMAGE_ID}"' in source
    assert f'F476_AMD64_MANIFEST_DIGEST = "{EXPECTED_F476_AMD64_MANIFEST_DIGEST}"' in source
    assert f'F476_CONFIG_DIGEST = "{EXPECTED_F476_CONFIG_DIGEST}"' in source
    assert f'"io.jobpulse.incident.purpose": "{INCIDENT_PURPOSE_LABEL}"' in source
    assert f'"org.opencontainers.image.revision": "{TARGET_SOURCE_SHA}"' in source
    assert f'"io.jobpulse.incident.parent": "{EXPECTED_PRODUCTION_GIT_SHA}"' in source
    assert f'"io.jobpulse.incident.build-run-id": "{INCIDENT_BUILD_RUN_ID}"' in source


def test_digest_reference_is_derived_from_the_pinned_index_digest():
    assert INCIDENT_IMAGE_DIGEST_REFERENCE.endswith("@" + INCIDENT_IMAGE_INDEX_DIGEST)
    assert INCIDENT_IMAGE_REFERENCE.endswith(":incident-" + TARGET_SOURCE_SHA)


@pytest.mark.parametrize(
    "forbidden",
    [":main", ":latest", MAIN_B63_SHA, MAIN_370_SHA, "0ee9a54a5997706fab2a1bec6b23e53aa5c7d336a76e3e2f1a6685fe5493c144"],
)
def test_never_references_floating_or_other_images_in_code(workflow_text, forbidden):
    assert forbidden not in _code_only(workflow_text)


def test_deployment_target_never_derived_from_github_sha_or_inputs(workflow_text):
    for forbidden in ("github.sha", "GITHUB_SHA", "github.event.after", "github.head_ref", "inputs.image", "inputs.sha"):
        assert forbidden not in workflow_text, forbidden


def test_never_dispatches_build_or_deploy_workflows(workflow_text):
    code = _code_only(workflow_text)
    for forbidden in ("Build JobPulse API Image", "Deploy Production", "docker-build.yml", "deploy.yml",
                      "gh workflow", "gh run", "/dispatches", "repository_dispatch", "workflow_call", "uses: ./"):
        assert forbidden not in code, forbidden
    for other in (DOCKER_BUILD_WORKFLOW_PATH, DEPLOY_WORKFLOW_PATH):
        assert "Production Incident 259c507 Deploy" not in other.read_text()


# production-direct-runtime-upgrade.yml is intentionally not listed: its
# post-incident current-runtime gate is a separately reviewed change.
@pytest.mark.parametrize("path", [".github/workflows/docker-build.yml", ".github/workflows/deploy.yml",
                                  ".github/workflows/production-api-reference-normalization.yml",
                                  ".github/workflows/production-incident-259c507-image-build.yml"])
def test_other_production_workflows_untouched_vs_main(path):
    if sp.run(["git", "cat-file", "-e", "origin/main"], cwd=REPO_ROOT, capture_output=True).returncode != 0:
        pytest.skip("origin/main not available")
    diff = sp.run(["git", "diff", "--quiet", "origin/main", "--", path], cwd=REPO_ROOT)
    assert diff.returncode == 0, path


def test_production_never_pulls_or_deploys_by_tag(remote_script):
    code = _code_only(remote_script)
    pulls = [line.strip() for line in code.splitlines() if re.search(r"\bdocker pull\b", line)]
    assert pulls == [
        'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker pull "$INCIDENT_IMAGE_DIGEST_REFERENCE"',
        'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker pull "$F476_IMAGE_DIGEST_REFERENCE" || fail "could not pull the f476 rollback artifact $F476_IMAGE_DIGEST_REFERENCE -- rollback cannot be prepared, no mutation"',
    ]
    assert "$INCIDENT_IMAGE_REFERENCE" not in code


# =====================================================================
# 3. Runner-side provenance ordering
# =====================================================================
def test_verification_steps_precede_ssh(steps):
    ssh_idx = _step_index(steps, "Validate VM_SSH_KEY secret is present")
    for name in (DERIVE_STEP, REGISTRY_STEP, "Verify incident image digest chain on GHCR (tag -> index -> amd64 manifest -> config -> labels)"):
        assert _step_index(steps, name) < ssh_idx
    assert _step_index(steps, DEPLOY_STEP) > ssh_idx


def test_deploy_step_refuses_without_derived_values(runner_script):
    for name in ("COMPOSE_SHA256", "FRONTEND_SHA256", "F476_COLLECTOR_SHA256", "INCIDENT_COLLECTOR_SHA256",
                 "F476_IMAGE_ENV_JSON_B64", "INCIDENT_IMAGE_ENV_JSON_B64"):
        assert name in runner_script.split("for required in", 1)[1].split("\n", 1)[0]
    assert runner_script.index("for required in") < runner_script.index("VM_SSH_KEY")


def test_ghcr_token_only_via_stdin(runner_script, remote_script):
    assert 'printf \'%s\' "${GHCR_TOKEN:-}" | ssh' in runner_script
    assert "base64" not in _code_only(runner_script)
    assert "--password-stdin" in remote_script
    assert 'DOCKER_CONFIG="$TMP_DOCKER_CONFIG" docker login' in remote_script


def _have_commits(*shas):
    return all(
        sp.run(["git", "cat-file", "-e", f"{s}^{{commit}}"], cwd=REPO_ROOT, capture_output=True).returncode == 0
        for s in shas
    )


@pytest.fixture(scope="module")
def derive_clone(tmp_path_factory):
    if not _have_commits(TARGET_SOURCE_SHA, EXPECTED_PRODUCTION_GIT_SHA):
        pytest.skip("incident commits not present in this clone")
    clone = tmp_path_factory.mktemp("derive") / "repo"
    sp.run(["git", "clone", "-q", "--no-checkout", "--shared", str(REPO_ROOT), str(clone)], check=True)
    sp.run(["git", "-C", str(clone), "checkout", "-q", "--detach", TARGET_SOURCE_SHA], check=True)
    # origin/main must contain 259c507 (mirrors actions/checkout on main).
    sp.run(["git", "-C", str(clone), "update-ref", "refs/remotes/origin/main", TARGET_SOURCE_SHA], check=True)
    return clone


def _run_derive(steps, clone, tmp_path, **env_overrides):
    script = _step(steps, DERIVE_STEP)["run"].replace("git fetch origin main", ": skipped fetch in test")
    github_env = tmp_path / "github_env"
    github_env.write_text("")
    env = dict(os.environ, GITHUB_ENV=str(github_env), TARGET_SOURCE_SHA=TARGET_SOURCE_SHA,
               EXPECTED_PRODUCTION_GIT_SHA=EXPECTED_PRODUCTION_GIT_SHA, TARGET_PARENT_SHA=EXPECTED_PRODUCTION_GIT_SHA,
               INCIDENT_IMAGE=INCIDENT_IMAGE_DIGEST_REFERENCE, INCIDENT_IMAGE_INDEX_DIGEST=INCIDENT_IMAGE_INDEX_DIGEST,
               F476_IMAGE_DIGEST_REFERENCE=F476_IMAGE_DIGEST_REFERENCE, EXPECTED_F476_IMAGE_ID=EXPECTED_F476_IMAGE_ID)
    env.update(env_overrides)
    result = sp.run(["bash", "-c", script], cwd=clone, env=env, capture_output=True, text=True)
    return result, github_env.read_text()


def test_derive_step_on_real_commits(steps, derive_clone, tmp_path):
    result, exported = _run_derive(steps, derive_clone, tmp_path)
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in exported.splitlines())
    show = lambda rev: sp.run(["git", "show", rev], cwd=REPO_ROOT, capture_output=True, check=True).stdout
    assert values["COMPOSE_SHA256"] == hashlib.sha256(show(f"{EXPECTED_PRODUCTION_GIT_SHA}:docker-compose.prod.yml")).hexdigest()
    assert values["F476_COLLECTOR_SHA256"] == hashlib.sha256(show(f"{EXPECTED_PRODUCTION_GIT_SHA}:scripts/collector_postgres.py")).hexdigest()
    assert values["INCIDENT_COLLECTOR_SHA256"] == hashlib.sha256(show(f"{TARGET_SOURCE_SHA}:scripts/collector_postgres.py")).hexdigest()
    assert values["F476_COLLECTOR_SHA256"] != values["INCIDENT_COLLECTOR_SHA256"]
    assert values["F476_PROVIDER_SHA256"] == hashlib.sha256(show(f"{TARGET_SOURCE_SHA}:scripts/providers/linkedin_browser_provider.py")).hexdigest()


def test_derive_step_rejects_inconsistent_f476_digest_pin(steps, derive_clone, tmp_path):
    result, exported = _run_derive(steps, derive_clone, tmp_path,
                                   F476_IMAGE_DIGEST_REFERENCE="ghcr.io/mrezamaghouli/jobpulse-api:f4765f857355c6543f68cea0e481b7f20a917147")
    assert result.returncode != 0 and exported == ""
    assert "F476_IMAGE_DIGEST_REFERENCE" in result.stderr


def test_derive_step_rejects_inconsistent_image_pin(steps, derive_clone, tmp_path):
    result, exported = _run_derive(steps, derive_clone, tmp_path, TARGET_PARENT_SHA=EXPECTED_PRODUCTION_GIT_SHA,
                                   INCIDENT_IMAGE="ghcr.io/mrezamaghouli/jobpulse-api:main",
                                   INCIDENT_IMAGE_INDEX_DIGEST=INCIDENT_IMAGE_INDEX_DIGEST)
    assert result.returncode != 0 and exported == ""


def test_derive_step_rejects_wrong_parent(steps, derive_clone, tmp_path):
    # Pretend the production baseline were 259c507's grandparent: parent check must fail.
    grandparent = sp.run(["git", "rev-parse", f"{EXPECTED_PRODUCTION_GIT_SHA}^"], cwd=REPO_ROOT,
                         capture_output=True, text=True).stdout.strip()
    result, exported = _run_derive(steps, derive_clone, tmp_path, EXPECTED_PRODUCTION_GIT_SHA=grandparent,
                                   TARGET_PARENT_SHA=grandparent)
    assert result.returncode != 0
    assert "parents are" in result.stderr
    assert exported == ""


def test_derive_step_rejects_unmerged_source(steps, derive_clone, tmp_path):
    sp.run(["git", "-C", str(derive_clone), "update-ref", "refs/remotes/origin/main", EXPECTED_PRODUCTION_GIT_SHA], check=True)
    try:
        result, exported = _run_derive(steps, derive_clone, tmp_path)
    finally:
        sp.run(["git", "-C", str(derive_clone), "update-ref", "refs/remotes/origin/main", TARGET_SOURCE_SHA], check=True)
    assert result.returncode != 0
    assert exported == ""


# =====================================================================
# 4. Immutable digest chain (runner-side GHCR verifier, fake registry)
# =====================================================================
@pytest.fixture(scope="module")
def registry_module(steps, tmp_path_factory):
    source = _heredoc(_step(steps, REGISTRY_STEP)["run"], "REGISTRY_PY")
    path = tmp_path_factory.mktemp("registry") / "incident_deploy_registry.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("incident_deploy_registry_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _dumps(obj) -> bytes:
    return json.dumps(obj, sort_keys=True).encode()


OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
BASE_ENV = ["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.12.11"]
GOOD_LABELS = {
    "org.opencontainers.image.revision": TARGET_SOURCE_SHA,
    "io.jobpulse.incident.parent": EXPECTED_PRODUCTION_GIT_SHA,
    "io.jobpulse.incident.build-run-id": INCIDENT_BUILD_RUN_ID,
    "io.jobpulse.incident.purpose": "linkedin-header-artifact-false-positive-fix",
}


def _make_image(store, *, labels, env=None, cmd=None, arch="amd64", extra_entries=()):
    config = {
        "os": "linux",
        "architecture": arch,
        "config": {
            "Cmd": cmd or ["python", "-m", "uvicorn", "app.main:app"],
            "WorkingDir": "/app",
            "ExposedPorts": {"8000/tcp": {}},
            "Env": list(env if env is not None else BASE_ENV),
            "Labels": labels,
        },
    }
    config_body = _dumps(config)
    store[f"blobs/{_digest(config_body)}"] = (200, config_body)
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {"mediaType": OCI_CONFIG, "digest": _digest(config_body)},
        "layers": [{"digest": "sha256:" + "c" * 64}],
    }
    manifest_body = _dumps(manifest)
    store[f"manifests/{_digest(manifest_body)}"] = (200, manifest_body)
    entries = [
        {"mediaType": OCI_MANIFEST, "digest": _digest(manifest_body), "platform": {"os": "linux", "architecture": "amd64"}},
        {
            "mediaType": OCI_MANIFEST,
            "digest": "sha256:" + "e" * 64,
            "platform": {"os": "unknown", "architecture": "unknown"},
            "annotations": {"vnd.docker.reference.type": "attestation-manifest",
                            "vnd.docker.reference.digest": _digest(manifest_body)},
        },
        *extra_entries,
    ]
    index_body = _dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries})
    store[f"manifests/{_digest(index_body)}"] = (200, index_body)
    return {"index": _digest(index_body), "index_body": index_body,
            "manifest": _digest(manifest_body), "config": _digest(config_body)}


class FakeRegistry:
    def __init__(self, store):
        self.store = store
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        return self.store.get(path, (404, b""))


@pytest.fixture
def chain(registry_module, monkeypatch):
    """A self-consistent fake registry; the module's pins are re-pointed at
    the fake digests (the real pins are asserted separately above)."""

    def build(*, incident_labels=None, incident_env=None, incident_cmd=None, f476_env=None, tag_target=None):
        store = {}
        f476 = _make_image(store, labels=None, env=f476_env)
        incident = _make_image(store, labels=GOOD_LABELS if incident_labels is None else incident_labels,
                               env=incident_env, cmd=incident_cmd)
        store[f"manifests/{registry_module.INCIDENT_TAG}"] = (200, (tag_target or incident)["index_body"])
        monkeypatch.setattr(registry_module, "INCIDENT_INDEX_DIGEST", incident["index"])
        monkeypatch.setattr(registry_module, "INCIDENT_AMD64_MANIFEST_DIGEST", incident["manifest"])
        monkeypatch.setattr(registry_module, "INCIDENT_CONFIG_DIGEST", incident["config"])
        monkeypatch.setattr(registry_module, "F476_INDEX_DIGEST", f476["index"])
        monkeypatch.setattr(registry_module, "F476_AMD64_MANIFEST_DIGEST", f476["manifest"])
        monkeypatch.setattr(registry_module, "F476_CONFIG_DIGEST", f476["config"])
        return FakeRegistry(store), incident, f476

    return build


def test_registry_happy_path(registry_module, chain):
    registry, incident, _ = chain()
    result = registry_module.verify(registry)
    assert result["incident_env_list"] == BASE_ENV
    assert result["f476_env_list"] == BASE_ENV
    assert result["image_default_env_keys_changed_vs_f476"] == []
    assert registry.paths[0] == f"manifests/{registry_module.INCIDENT_TAG}"
    # Every later fetch is by digest, never by tag.
    assert all("@" not in p and ("sha256:" in p) for p in registry.paths[1:])


def test_registry_rejects_tag_resolving_elsewhere(registry_module, chain):
    store_registry, _, f476 = chain()
    registry, _, f476 = chain(tag_target=f476)
    with pytest.raises(registry_module.ProvenanceError, match="resolves to"):
        registry_module.verify(registry)


def test_registry_rejects_unexpected_amd64_manifest(registry_module, chain, monkeypatch):
    registry, _, _ = chain()
    monkeypatch.setattr(registry_module, "INCIDENT_AMD64_MANIFEST_DIGEST", "sha256:" + "1" * 64)
    with pytest.raises(registry_module.ProvenanceError, match="linux/amd64 manifest"):
        registry_module.verify(registry)


def test_registry_rejects_unexpected_config_digest(registry_module, chain, monkeypatch):
    registry, _, _ = chain()
    monkeypatch.setattr(registry_module, "INCIDENT_CONFIG_DIGEST", "sha256:" + "2" * 64)
    with pytest.raises(registry_module.ProvenanceError, match="config"):
        registry_module.verify(registry)


@pytest.mark.parametrize("label", ["org.opencontainers.image.revision", "io.jobpulse.incident.parent", "io.jobpulse.incident.purpose", "io.jobpulse.incident.build-run-id"])
def test_registry_rejects_wrong_or_missing_required_label(registry_module, chain, label):
    for bad in ("wrong", None):
        labels = dict(GOOD_LABELS)
        if bad is None:
            labels.pop(label)
        else:
            labels[label] = bad
        registry, _, _ = chain(incident_labels=labels)
        with pytest.raises(registry_module.ProvenanceError, match=re.escape(label)):
            registry_module.verify(registry)


def test_registry_rejects_tampered_content(registry_module, chain):
    registry, incident, _ = chain()
    status, body = registry.store[f"blobs/{incident['config']}"]
    registry.store[f"blobs/{incident['config']}"] = (status, body.replace(b"/app", b"/tmp"))
    with pytest.raises(registry_module.ProvenanceError, match="content hashes to"):
        registry_module.verify(registry)


@pytest.mark.parametrize("pin,match", [("F476_AMD64_MANIFEST_DIGEST", "f476 index resolves"), ("F476_CONFIG_DIGEST", "f476 manifest resolves")])
def test_registry_rejects_unexpected_f476_baseline_chain(registry_module, chain, monkeypatch, pin, match):
    registry, _, _ = chain()
    monkeypatch.setattr(registry_module, pin, "sha256:" + "4" * 64)
    with pytest.raises(registry_module.ProvenanceError, match=match):
        registry_module.verify(registry)


def test_registry_rejects_runtime_contract_drift(registry_module, chain):
    registry, _, _ = chain(incident_cmd=["python", "other.py"])
    with pytest.raises(registry_module.ProvenanceError, match="runtime contract key Cmd"):
        registry_module.verify(registry)


@pytest.mark.parametrize("entry", ["SEARCH_TRANSPORT=proxy", "TOR_ENABLED=true", "HTTPS_PROXY=http://x:1", "all_proxy=socks5://x"])
def test_registry_rejects_transport_sensitive_env_drift(registry_module, chain, entry):
    registry, _, _ = chain(incident_env=BASE_ENV + [entry])
    with pytest.raises(registry_module.ProvenanceError, match="transport-sensitive"):
        registry_module.verify(registry)


def test_registry_reports_benign_env_key_changes_by_name_only(registry_module, chain):
    registry, _, _ = chain(incident_env=["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.12.12"])
    assert registry_module.verify(registry)["image_default_env_keys_changed_vs_f476"] == ["PYTHON_VERSION"]


def test_registry_rejects_ambiguous_platform(registry_module, chain):
    extra = ({"mediaType": OCI_MANIFEST, "digest": "sha256:" + "3" * 64, "platform": {"os": "linux", "architecture": "arm64"}},)
    store = {}
    _make_image(store, labels=GOOD_LABELS, extra_entries=extra)
    index = registry_module.parse_json_object(
        next(b for k, (s, b) in store.items() if k.startswith("manifests/") and b"manifests" in b), "index")
    with pytest.raises(registry_module.ProvenanceError, match="unexpected image index entry"):
        registry_module.select_runtime_manifest(index)


def test_registry_main_writes_only_env_lists_to_github_env(registry_module, chain, monkeypatch, tmp_path, capsys):
    registry, _, _ = chain()
    github_env = tmp_path / "github_env"
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setenv("GHCR_USERNAME", "u")
    monkeypatch.setenv("GHCR_PASSWORD", "p")
    monkeypatch.setattr(registry_module, "pull_token", lambda u, p: "tok")
    monkeypatch.setattr(registry_module, "Registry", lambda token: registry)
    assert registry_module.main(["x", "verify"]) == 0
    out = capsys.readouterr().out
    assert registry_module.SUCCESS_MARKER in out
    assert "PYTHON_VERSION=" not in out  # never prints Env values
    lines = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert set(lines) == {"F476_IMAGE_ENV_JSON_B64", "INCIDENT_IMAGE_ENV_JSON_B64"}
    assert json.loads(base64.b64decode(lines["INCIDENT_IMAGE_ENV_JSON_B64"])) == BASE_ENV


def test_registry_verifier_is_read_only(steps):
    source = _code_only(_heredoc(_step(steps, REGISTRY_STEP)["run"], "REGISTRY_PY"))
    for forbidden in ("method=\"PUT\"", "method=\"DELETE\"", "method=\"POST\"", "/blobs/uploads", "docker "):
        assert forbidden not in source


# =====================================================================
# 5. Production baseline, API-only mutation, forbidden actions
# =====================================================================
def test_known_rollback_alias_accepted_only_by_image_id(remote_script):
    body = _main_body(remote_script)
    id_check = body.index('[ "$API_IMAGE_ID_BEFORE" = "$EXPECTED_F476_IMAGE_ID" ]')
    case_stmt = body.index('case "$API_CONFIG_IMAGE_BEFORE" in')
    assert id_check < case_stmt
    case_block = body[case_stmt : body.index("esac", case_stmt)]
    assert '"$KNOWN_F476_ROLLBACK_ALIAS")' in case_block
    assert '"$CANONICAL_F476_REFERENCE")' in case_block
    assert "*)" in case_block and "fail" in case_block.split("*)")[1]


def test_exactly_two_api_compose_up_sites(remote_script):
    ups = [line.strip() for line in _code_only(remote_script).splitlines() if re.search(r"docker compose .*\bup\b", line)]
    assert len(ups) == 2, ups
    assert ups[0].startswith('JOBPULSE_API_IMAGE="$F476_IMAGE_DIGEST_REFERENCE" docker compose')  # rollback (function defined first)
    assert ups[1] == ('JOBPULSE_API_IMAGE="$INCIDENT_IMAGE_DIGEST_REFERENCE" docker compose -p "$PRODUCTION_PROJECT" '
                      '-f "$COMPOSE_FILE" up -d --no-build --no-deps --force-recreate api')
    for up in ups:
        assert re.search(r"up -d --no-build --no-deps --force-recreate api( \|\| \{)?$", up), up


@pytest.mark.parametrize(
    "pattern",
    [
        r"docker compose[^\n]*(?<![-\w])(down|restart|stop|start|rm|kill|exec|run|pull|create|build)\b",
        r"docker (restart|stop|start|rm|rmi|kill|system|volume|network|container|commit|push|build)\b",
        r"\bup\b[^\n]*\b(db|frontend|tor|tor-diagnostic)\s*$",
        r"jobpulse-(postgres|frontend|tor)-prod[^\n]*\b(restart|stop|start|rm|kill)\b",
    ],
)
def test_no_db_frontend_tor_or_other_container_mutation(remote_script, pattern):
    assert not re.search(pattern, _code_only(remote_script), re.MULTILINE), pattern


def test_no_tagging_and_no_image_removal_anywhere(remote_script):
    """K: the runner never tags (the vanished alias is not recreated) and
    never removes an image, so the f476 rollback artifact stays locally
    available after the run for operator recovery."""
    code = _code_only(remote_script)
    for forbidden in (r"\bdocker tag\b", r"\bdocker rmi\b", r"\bdocker image (rm|remove|prune)\b", r"\bdocker (system|builder) prune\b"):
        assert not re.search(forbidden, code), forbidden
    trap = _extract_bash_function(remote_script, "final_exit_trap")
    assert "F476_IMAGE_DIGEST_REFERENCE" not in trap and "INCIDENT_IMAGE_DIGEST_REFERENCE" not in trap


def test_f476_rollback_artifact_pinned_by_digest_and_pulled_only_by_digest(remote_script):
    """B: the rollback artifact is the constant f476 digest reference; it is
    pulled only by that digest, after the incident pull, and never by tag."""
    assert f'F476_IMAGE_DIGEST_REFERENCE="{F476_IMAGE_DIGEST_REFERENCE}"' in remote_script
    code = _code_only(remote_script)
    assert code.count('docker pull "$F476_IMAGE_DIGEST_REFERENCE"') == 1
    assert '"$CANONICAL_F476_REFERENCE"' not in code.replace('"$CANONICAL_F476_REFERENCE")', "")
    assert 'docker pull "$KNOWN_F476_ROLLBACK_ALIAS"' not in code and 'docker pull "$API_CONFIG_IMAGE_BEFORE"' not in code


def test_old_alias_is_never_required_to_resolve_or_used_for_rollback(remote_script):
    """F/H: Config.Image is observational only."""
    code = _code_only(remote_script)
    assert 'docker image inspect "$API_CONFIG_IMAGE_BEFORE"' not in code
    assert 'JOBPULSE_API_IMAGE="$API_CONFIG_IMAGE_BEFORE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" up' not in code
    func = _extract_bash_function(remote_script, "rollback_api")
    assert "API_CONFIG_IMAGE_BEFORE" not in _code_only(func).replace('pre-deployment Config.Image was $API_CONFIG_IMAGE_BEFORE', "")


def test_f476_pull_follows_identity_and_idle_checks_and_precedes_mutation(remote_script):
    """G + ordering: the running API's f476 identity, runtime source,
    transport, health, scheduler and idle checks all happen before the f476
    pull, and the pull (plus its ID/platform verification) precedes the
    deploy-inputs proof and the mutation marker."""
    body = _main_body(remote_script)
    pull = body.index('docker pull "$F476_IMAGE_DIGEST_REFERENCE"')
    for earlier in ('[ "$API_IMAGE_ID_BEFORE" = "$EXPECTED_F476_IMAGE_ID" ]', '[ "$COLLECTOR_SHA_BEFORE" = "$F476_COLLECTOR_SHA256" ]',
                    '[ "$PROVIDER_SHA_BEFORE" = "$F476_PROVIDER_SHA256" ]', '[ "$TOR_ENABLED_BEFORE" = "false" ]',
                    "own_crontab_provenance=confirmed_canonical_launcher", 'flock -n "$INTERNAL_LOCK_FD"',
                    '[ "$ACTIVE_COLLECTOR" = "no" ]', 'docker pull "$INCIDENT_IMAGE_DIGEST_REFERENCE"'):
        assert body.index(earlier) < pull, earlier
    for later in ('[ "$F476_PULLED_ID" = "$EXPECTED_F476_IMAGE_ID" ]', '[ "$F476_PULLED_PLATFORM" = "linux/amd64" ]',
                  '[ "$F476_PULLED_ID" = "$API_IMAGE_ID_BEFORE" ]'):
        assert pull < body.index(later) < body.index("prove_deploy_inputs") < body.index("API_MUTATION_STARTED=true"), later


def test_runner_side_f476_chain_verification_precedes_production_pull(steps):
    """The f476 GHCR chain (index -> amd64 manifest -> config) is verified by
    the runner in a step that strictly precedes the SSH/deploy step."""
    verify_idx = _step_index(steps, "Verify incident image digest chain on GHCR (tag -> index -> amd64 manifest -> config -> labels)")
    assert verify_idx < _step_index(steps, DEPLOY_STEP)
    source = _heredoc(_step(steps, REGISTRY_STEP)["run"], "REGISTRY_PY")
    assert "f476 = describe_image(registry, F476_INDEX_DIGEST)" in source


@pytest.mark.parametrize("verb", ["reset", "checkout", "switch", "pull", "merge", "clean", "stash", "fetch", "commit", "push", "rebase", "restore", "am", "apply"])
def test_no_production_git_mutation(remote_script, verb):
    assert not re.search(rf"\bgit {verb}\b", _code_only(remote_script))


def test_only_read_only_git_commands(remote_script):
    verbs = set(re.findall(r"(?:^|\$\(|;|&&|\|\|)\s*git ([a-z-]+)", _code_only(remote_script), re.MULTILINE))
    assert verbs <= {"rev-parse", "status"}, verbs


def test_no_scheduler_mutation(remote_script):
    code = _code_only(remote_script)
    invocations = re.findall(r"(?:timeout 5 |sudo -n )crontab ([^)\n]*)", code)
    assert invocations and all(i.startswith(("-l", "-u root -l")) for i in invocations), invocations
    assert not re.search(r"(?<![/\w.])crontab\s+(-e|-r|-i|-)(\s|$)", code, re.MULTILINE)
    assert not re.search(r"systemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload|edit|set-property|kill)", code)
    for path in ("/etc/crontab", "/etc/cron.d", ".collection.env"):
        assert not re.search(rf">\s*\S*{re.escape(path)}", code)


def test_no_collection_execution(remote_script):
    code = _code_only(remote_script)
    assert "python -m scripts." not in code
    assert not re.search(r"(^|[;&|]\s*)(bash |sh )?\S*run_collection_cycle_safe\.sh", code, re.MULTILINE)
    assert "docker compose exec" not in code and "docker compose run" not in code
    execs = [line.strip() for line in code.splitlines() if "docker exec" in line]
    allowed = (
        "docker exec jobpulse-api-prod printenv ",
        "docker exec jobpulse-api-prod python -c 'import hashlib; print(hashlib.sha256(open(\"/app/scripts/collector_postgres.py\", \"rb\").read()).hexdigest())'",
        "docker exec jobpulse-api-prod python -c 'from app.config import get_search_transport_mode; print(get_search_transport_mode())'",
        "docker exec jobpulse-api-prod python -c 'import hashlib; print(hashlib.sha256(open(\"/app/scripts/providers/linkedin_browser_provider.py\", \"rb\").read()).hexdigest())'",
    )
    for line in execs:
        assert any(a in line for a in allowed), line


def test_no_linkedin_tor_controlport_or_proxy_actions(workflow_text, remote_script):
    lowered = workflow_text.lower()
    assert "linkedin.com" not in lowered
    code = _code_only(remote_script)
    for forbidden in ("9050", "9051", "NEWNYM", "SIGNAL", "TOR_SOCKS", "TOR_CONTROL", "SEARCH_TRANSPORT=proxy",
                      "TOR_ENABLED=true", "tor_client", "get_proxy_config"):
        assert forbidden not in code, forbidden
    assert not re.search(r"^\s*(export\s+)?(TOR_ENABLED|SEARCH_TRANSPORT)=", code, re.MULTILINE)


def test_no_normalization_or_phase4m_or_dispatch(workflow_text, remote_script):
    code = _code_only(workflow_text)
    for forbidden in ("NORMALIZE_F476_API_REFERENCE", "PHASE_4M_PRENORMALIZATION_READONLY_VERIFIED",
                      "API_REFERENCE_NORMALIZED", "gh workflow", "gh api", "/dispatches", "workflow_dispatch:\n      inputs:\n        image"):
        assert forbidden not in code
    # The canonical f476 reference is only ever ACCEPTED as a pre-state; it is
    # never a deploy/normalization target.
    remote_code = _code_only(remote_script)
    assert 'JOBPULSE_API_IMAGE="$CANONICAL_F476_REFERENCE"' not in remote_code
    assert remote_code.count("$CANONICAL_F476_REFERENCE") == 1


def test_no_full_environment_or_secret_dump(remote_script):
    code = _code_only(remote_script)
    assert not re.search(r"docker exec[^\n]*\b(env|printenv)\s*($|[|)\"'])", code, re.MULTILINE)
    for env_file in (".api_keys.env", ".admin.env", "/.env", ".tor_control_password"):
        assert not re.search(rf"\b(cat|source|\.)\s+\S*{re.escape(env_file)}", code)
    assert 'echo "$RUNTIME_ENV' not in code and 'echo "$TOP_OUTPUT"' not in code


# =====================================================================
# 6. Ordering: every preflight gate precedes the single mutation marker
# =====================================================================
def test_mutation_marker_set_once_immediately_before_candidate_up(remote_script):
    code = _code_only(remote_script)
    assert code.count("API_MUTATION_STARTED=true") == 1
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    i = lines.index("API_MUTATION_STARTED=true")
    assert lines[i + 1].startswith('JOBPULSE_API_IMAGE="$INCIDENT_IMAGE_DIGEST_REFERENCE" docker compose')


@pytest.mark.parametrize(
    "needle",
    [
        '[ "$CURRENT_SHA" = "$EXPECTED_PRODUCTION_GIT_SHA" ]',
        '[ -z "$DIRTY_TRACKED_BEFORE" ]',
        '[ "$ACTUAL_COMPOSE_SHA256" = "$COMPOSE_SHA256" ]',
        '[ "$API_IMAGE_ID_BEFORE" = "$EXPECTED_F476_IMAGE_ID" ]',
        '[ "$API_HEALTH_BEFORE" = "healthy" ]',
        '[ "$F476_PULLED_ID" = "$EXPECTED_F476_IMAGE_ID" ]',
        '[ "$TOR_ENABLED_BEFORE" = "false" ]',
        '[ "$COLLECTOR_SHA_BEFORE" = "$F476_COLLECTOR_SHA256" ]',
        '[ "$PROVIDER_SHA_BEFORE" = "$F476_PROVIDER_SHA256" ]',
        '[ "$ROLLBACK_F476_LOCAL_ID" = "$EXPECTED_F476_IMAGE_ID" ]',
        "ROLLBACK_PREPARED=true",
        'DB_STATE_BEFORE="$(container_state jobpulse-postgres-prod)"',
        'FRONTEND_STATE_BEFORE="$(container_state jobpulse-frontend-prod)"',
        'TOR_STATE_BEFORE="$(container_state jobpulse-tor-prod)"',
        "own_crontab_provenance=confirmed_canonical_launcher",
        'SCHEDULER_FINGERPRINT_BEFORE="$(scheduler_fingerprint)"',
        'flock -n "$OUTER_LOCK_FD"',
        'flock -n "$INTERNAL_LOCK_FD"',
        '[ "$ACTIVE_COLLECTOR" = "no" ]',
        '[ "$PULLED_IMAGE_ID" = "$EXPECTED_INCIDENT_IMAGE_ID" ]',
        '[ "$PULLED_IMAGE_LABELS" = "$TARGET_SOURCE_SHA|$EXPECTED_PRODUCTION_GIT_SHA|$INCIDENT_PURPOSE_LABEL|$INCIDENT_BUILD_RUN_ID" ]',
        "final_pre_mutation_revalidation=ok",
        "final_deploy_inputs_revalidation=ok",
    ],
)
def test_preflight_gate_precedes_mutation(remote_script, needle):
    body = _main_body(remote_script)
    assert needle in body, needle
    assert body.index(needle) < body.index("API_MUTATION_STARTED=true")


def test_deploy_inputs_proof_runs_twice_after_locks(remote_script):
    body = _main_body(remote_script)
    calls = [m.start() for m in re.finditer(r"^prove_deploy_inputs$", body, re.MULTILINE)]
    assert len(calls) == 2
    assert all(body.index('flock -n "$INTERNAL_LOCK_FD"') < c < body.index("API_MUTATION_STARTED=true") for c in calls)


def test_locks_outer_then_internal_then_collector_check(remote_script):
    body = _main_body(remote_script)
    assert body.index('flock -n "$OUTER_LOCK_FD"') < body.index('flock -n "$INTERNAL_LOCK_FD"') < body.index("active collector check")


def test_locks_released_only_in_exit_trap(remote_script):
    assert "release_locks" in _extract_bash_function(remote_script, "final_exit_trap")
    release_func = _extract_bash_function(remote_script, "release_locks")
    for line in remote_script.splitlines():
        if "exec {" in line and ">&-" in line:
            assert line.strip() in release_func


@pytest.mark.parametrize(
    "needle",
    [
        'wait_for_api_convergence "$INCIDENT_IMAGE_DIGEST_REFERENCE" "$EXPECTED_INCIDENT_IMAGE_ID"',
        'verify_api_stability "$EXPECTED_INCIDENT_IMAGE_ID"',
        '[ "$RUNNING_LABELS" = "$TARGET_SOURCE_SHA|$EXPECTED_PRODUCTION_GIT_SHA|$INCIDENT_PURPOSE_LABEL|$INCIDENT_BUILD_RUN_ID" ]',
        '[ "$COLLECTOR_SHA_AFTER" = "$INCIDENT_COLLECTOR_SHA256" ]',
        '[ "$PROVIDER_SHA_AFTER" = "$F476_PROVIDER_SHA256" ]',
        '[ "$TRANSPORT_MODE_AFTER" = "direct" ]',
        '[ "$TOR_ENABLED_AFTER" = "false" ]',
        '[ "$ACTUAL_RUNTIME_ENV_SHA256" = "$EXPECTED_CANDIDATE_ENV_SHA256" ]',
        "verify_other_services_unchanged || exit 1",
        '[ "$LIVE_FRONTEND_SHA256_AFTER" = "$FRONTEND_SHA256" ]',
        "verify_git_unchanged || exit 1",
        '[ "$SCHEDULER_FINGERPRINT_AFTER" = "$SCHEDULER_FINGERPRINT_BEFORE" ]',
    ],
)
def test_post_deploy_check_precedes_success(remote_script, needle):
    body = _main_body(remote_script)
    assert body.index("API_MUTATION_STARTED=true") < body.index(needle) < body.index("DEPLOY_CONFIRMED=true")


def test_success_flags_set_only_at_the_end(remote_script):
    code = _code_only(remote_script)
    assert code.count("DEPLOY_CONFIRMED=true") == 1
    assert code.count("WORKFLOW_SUCCESS=true") == 1
    tail = [line.strip() for line in code.splitlines() if line.strip()][-2:]
    assert tail == ["DEPLOY_CONFIRMED=true", "WORKFLOW_SUCCESS=true"]


def test_convergence_is_bounded_two_ways(remote_script):
    func = _extract_bash_function(remote_script, "wait_for_api_convergence")
    assert "deadline=$((SECONDS + 120))" in func
    assert "for attempt in $(seq 1 40)" in func
    assert "timeout 3 docker inspect" in func
    assert '"$health" = "healthy"' in func and '"$image_id" = "$expected_image_id"' in func
    health = _extract_bash_function(remote_script, "health_json_ok")
    assert "--max-time 3" in health and '"database") == "connected"' in health


# =====================================================================
# 7. Rollback structure
# =====================================================================
def test_rollback_only_from_exit_trap_after_mutation(remote_script):
    trap = _extract_bash_function(remote_script, "final_exit_trap")
    assert '[ "$API_MUTATION_STARTED" = "true" ] && [ "$DEPLOY_CONFIRMED" != "true" ]' in trap
    assert _code_only(remote_script).count("rollback_api") == 2  # definition + one call
    assert "WORKFLOW_SUCCESS=true" not in trap


def test_rollback_refuses_unless_prepared_before_mutation(remote_script):
    func = _extract_bash_function(remote_script, "rollback_api")
    assert func.index('[ "$ROLLBACK_PREPARED" != "true" ]') < func.index("docker compose")
    code = _code_only(remote_script)
    assert code.count("ROLLBACK_PREPARED=true") == 1
    body = _main_body(remote_script)
    assert body.index("final_deploy_inputs_revalidation=ok") < body.index("ROLLBACK_PREPARED=true") < body.index("API_MUTATION_STARTED=true")


def test_rollback_restores_exact_prestate_reference_and_image(remote_script):
    func = _extract_bash_function(remote_script, "rollback_api")
    assert 'JOBPULSE_API_IMAGE="$F476_IMAGE_DIGEST_REFERENCE" docker compose' in func
    assert 'wait_for_api_convergence "$F476_IMAGE_DIGEST_REFERENCE" "$EXPECTED_F476_IMAGE_ID"' in func
    assert '"$rb_provider_sha" != "$F476_PROVIDER_SHA256"' in func
    assert '"$(scheduler_fingerprint)" != "$SCHEDULER_FINGERPRINT_BEFORE"' in func
    assert func.index('rb_local_id="$(docker image inspect "$F476_IMAGE_DIGEST_REFERENCE"') < func.index('JOBPULSE_API_IMAGE="$F476_IMAGE_DIGEST_REFERENCE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" up')
    assert '"$rb_collector_sha" != "$F476_COLLECTOR_SHA256"' in func
    assert '"$ACTUAL_RUNTIME_ENV_SHA256" != "$FINAL_EXPECTED_CURRENT_ENV_SHA256"' in func
    assert "verify_other_services_unchanged" in func and "verify_git_unchanged" in func
    assert not re.search(r"(^|\$\()\s*git ", _code_only(func), re.MULTILINE)


def test_rollback_input_drift_guards_precede_recreation(remote_script):
    func = _extract_bash_function(remote_script, "rollback_api")
    tag_idx = func.index('JOBPULSE_API_IMAGE="$F476_IMAGE_DIGEST_REFERENCE" docker compose -p "$PRODUCTION_PROJECT" -f "$COMPOSE_FILE" up')
    for needle in ('!= "$COMPOSE_SHA256"', '!= "$FINAL_EFFECTIVE_ROLLBACK_CONFIG_SHA256"',
                   '"$EXPECTED_RUNTIME_ENV_SHA256" != "$FINAL_EXPECTED_CURRENT_ENV_SHA256"'):
        assert func.index(needle) < tag_idx


def test_rollback_outcome_markers(remote_script):
    trap = _extract_bash_function(remote_script, "final_exit_trap")
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_CONFIRMED_PRESTATE_RESTORED" in trap
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in trap
    assert "classify_api_state_after_failed_rollback" in trap
    assert "INCIDENT_DEPLOY_ABORTED_BEFORE_MUTATION" in trap


# =====================================================================
# 8. Scheduler provenance parity with the reviewed Phase 4M runner
# =====================================================================
def _scheduler_section(script: str) -> str:
    start = script.index("scheduler provenance: this account's own crontab")
    end = script.index("SCHEDULER_FINGERPRINT_BEFORE=\"$(scheduler_fingerprint)\"")
    return script[start:end]


def test_scheduler_provenance_parity_with_normalization_runner(remote_script):
    norm_steps = yaml.safe_load(NORMALIZATION_WORKFLOW_PATH.read_text())["jobs"]["normalize"]["steps"]
    norm_remote = _split_remote_and_runner(_step(norm_steps, "Run production API reference normalization")["run"])[1]
    norm_end = norm_remote.rindex("\n", 0, norm_remote.index("collection scheduler safety: acquire locks"))
    norm_section = norm_remote[norm_remote.index("scheduler provenance: this account's own crontab") : norm_end]
    norm = lambda t: "\n".join(line.rstrip() for line in t.splitlines() if line.strip())
    assert norm(_scheduler_section(remote_script)) == norm(norm_section)


# =====================================================================
# 9. CI coverage, Tor allowlist, runbook
# =====================================================================
def test_ci_runs_this_test_file():
    assert "tests/test_production_incident_259c507_deploy_workflow.py" in CI_WORKFLOW_PATH.read_text()


def test_runbook_documents_incident_deploy_boundaries():
    text = RUNBOOK_PATH.read_text()
    section = text[text.index("## Incident 259c507: API-only deployment runner") :]
    for needle in (
        "production-incident-259c507-deploy.yml",
        CONFIRMATION_TOKEN,
        "Building the artifact did not authorize deployment",
        "Deployment does not authorize a collection run",
        "Phase 4M remains paused",
        INCIDENT_IMAGE_INDEX_DIGEST,
    ):
        assert needle in section, needle


# =====================================================================
# 10. Behavioral harness: the ACTUAL remote script against fakes
# =====================================================================
F476_COLLECTOR = "# collector f476\n"
INCIDENT_COLLECTOR = "# collector 259c507\n"
F476_IMAGE_ENV = ["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.12.10"]
INCIDENT_IMAGE_ENV = ["PATH=/usr/local/bin:/usr/bin", "LANG=C.UTF-8", "PYTHON_VERSION=3.12.11"]
COMPOSE_ENV = {"POSTGRES_HOST": "db", "APP_ENV": "production", "TOR_ENABLED": "false", "JOBPULSE_PUBLIC_API_KEYS": "secret-value"}
INCIDENT_LABELS_LINE = f"{TARGET_SOURCE_SHA}|{EXPECTED_PRODUCTION_GIT_SHA}|{INCIDENT_PURPOSE_LABEL}|{INCIDENT_BUILD_RUN_ID}"
F476_PROVIDER = "# provider f476 (no Phase 4B)\n"
HEALTH_OK = '{"status":"ok","database":"connected"}'
HEALTH_DB_DOWN = '{"status":"error","database":"disconnected","error":"x"}'


def _env_merge(image_env, compose_env, extra=None):
    merged = dict(e.split("=", 1) for e in image_env)
    merged.update(compose_env)
    merged.update(extra or {})
    return [f"{k}={v}" for k, v in merged.items()]


def _write_exe(bin_dir: Path, name: str, body: str):
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


class Scenario:
    """Default = the exact known recovered production state (alias
    Config.Image over the f476 image ID) with a healthy candidate."""

    def __init__(self, tmp_path: Path, **overrides):
        self.tmp = tmp_path
        self.root = tmp_path / "jobpulse_root"
        self.outer_lock = tmp_path / "outer.lock"
        self.etc_crontab = tmp_path / "etc_crontab"
        self.etc_cron_d = tmp_path / "etc_cron_d"
        self.phase_file = tmp_path / "phase"
        self.round_file = tmp_path / "round"
        self.log = tmp_path / "docker.log"
        defaults = dict(
            git_sha=EXPECTED_PRODUCTION_GIT_SHA,
            git_sha_after_mutation=None,
            git_status_lines=(),
            compose_content="compose f476\n",
            frontend_content="<html>f476</html>\n",
            api_labels="api|jobpulse",
            api_id_before="a" * 64,
            api_image_before=EXPECTED_F476_IMAGE_ID,
            api_config_image_before=KNOWN_F476_ROLLBACK_ALIAS,
            api_restart_before="0",
            api_running_before="true",
            api_health_before="healthy",
            health_body_before=HEALTH_OK,
            search_transport_before="",
            tor_enabled_before="false",
            collector_before=F476_COLLECTOR,
            provider_before=F476_PROVIDER,
            # Production reality (2026-10-02): no local f476 image object at start.
            f476_local_at_start=False,
            f476_pull_exit=0,
            f476_pulled_id=EXPECTED_F476_IMAGE_ID,
            f476_pulled_platform="linux/amd64",
            f476_vanishes_before_rollback=False,
            runtime_env_extra_before=None,
            db_state="d" * 64 + "|sha256:db|0|true|healthy|2026-09-01T00:00:00Z|postgres:16-alpine",
            frontend_state="f" * 64 + "|sha256:fe|0|true|none|2026-09-01T00:00:00Z|nginx:alpine",
            tor_state="t" * 64 + "|sha256:tor|0|true|healthy|2026-09-01T00:00:00Z|ghcr.io/mrezamaghouli/jobpulse-tor:x",
            frontend_state_after=None,
            tor_present=True,
            tor_ports="",
            mount_line=None,
            docker_top="PID USER TIME COMMAND\n1 root 0:00 uvicorn app.main:app",
            own_crontab_ok=True,
            own_crontab_extra_after_mutation="",
            pulled_image_id=INCIDENT_IMAGE_INDEX_DIGEST,
            pulled_labels=INCIDENT_LABELS_LINE,
            compose_search_transport_current="",
            compose_search_transport_candidate="",
            candidate_extra_config_line="",
            compose_env=dict(COMPOSE_ENV),
            compose_env_after_mutation=None,
            candidate_up_exit=0,
            candidate_sequence=(dict(running="true", health="healthy", image=INCIDENT_IMAGE_INDEX_DIGEST, direct=True, nginx=True),),
            candidate_config_image=INCIDENT_IMAGE_DIGEST_REFERENCE,
            candidate_restart_sequence=("0",),
            candidate_collector=INCIDENT_COLLECTOR,
            candidate_provider=F476_PROVIDER,
            candidate_labels=INCIDENT_LABELS_LINE,
            candidate_transport_mode="direct",
            candidate_tor_enabled="false",
            candidate_env_extra=None,
            rollback_up_exit=0,
            rollback_provider=F476_PROVIDER,
            rollback_config_image=F476_IMAGE_DIGEST_REFERENCE,
            rollback_sequence=(dict(running="true", health="healthy", image=EXPECTED_F476_IMAGE_ID, direct=True, nginx=True),),
            rollback_collector=F476_COLLECTOR,
        )
        unknown = set(overrides) - set(defaults)
        assert not unknown, unknown
        defaults.update(overrides)
        self.__dict__.update(defaults)
        if self.mount_line is None:
            self.mount_line = f"bind|{self.root}/frontend|false"
        self.candidate_marker = tmp_path / "candidate_up"
        self.rollback_marker = tmp_path / "rollback_up"
        self.f476_pulled_marker = tmp_path / "f476_pulled"

    q = staticmethod(lambda v: shlex.quote(str(v)))

    def _phase_state_files(self):
        for phase, seq in (("candidate", self.candidate_sequence), ("rollback", self.rollback_sequence)):
            d = self.tmp / f"{phase}_state"
            d.mkdir(exist_ok=True)
            for i, s in enumerate(seq):
                for key in ("running", "health", "image"):
                    (d / f"{i}.{key}").write_text(s[key])
                (d / f"{i}.direct").write_text("true" if s["direct"] else "false")
                (d / f"{i}.nginx").write_text("true" if s["nginx"] else "false")
        d = self.tmp / "candidate_restart"
        d.mkdir(exist_ok=True)
        for i, value in enumerate(self.candidate_restart_sequence):
            (d / str(i)).write_text(value)

    def build(self) -> Path:
        bin_dir = self.tmp / "fake_bin"
        bin_dir.mkdir()
        for name in ("sha256sum", "flock", "timeout", "python3"):
            _write_exe(bin_dir, name, f'exec {self.q(_REAL[name])} "$@"\n')
        _write_exe(bin_dir, "sleep", "exit 0\n")
        self.phase_file.write_text("before")
        self.round_file.write_text("0")
        self._phase_state_files()

        self.root.mkdir()
        (self.root / "frontend").mkdir()
        (self.root / "state").mkdir()
        (self.root / "docker-compose.prod.yml").write_text(self.compose_content)
        (self.root / "frontend" / "index.html").write_text(self.frontend_content)

        status = "\n".join(f"printf '%s\\n' {self.q(line)}" for line in self.git_status_lines) or "true"
        after_sha = self.git_sha_after_mutation or self.git_sha
        _write_exe(bin_dir, "git", f'''
case "$1" in
  rev-parse)
    if [ "$(cat {self.q(self.phase_file)})" = before ]; then echo {self.q(self.git_sha)}; else echo {self.q(after_sha)}; fi ;;
  status) {status} ;;
  *) echo "FAKE_GIT_FORBIDDEN $*" >> {self.q(self.log)}; exit 97 ;;
esac
''')

        cron_line = f"*/30 * * * * cd {self.root} && flock -n {self.outer_lock} ./scripts/run_collection_cycle_safe.sh"
        _write_exe(bin_dir, "crontab", f'''
if [ "$1" = "-u" ]; then echo "no crontab for root" >&2; exit 1; fi
if [ "$1" = "-l" ]; then
  {"true" if self.own_crontab_ok else 'echo "no crontab for tester" >&2; exit 1'}
  printf '%s\\n' {self.q(cron_line)}
  if [ "$(cat {self.q(self.phase_file)})" != before ] && [ -n {self.q(self.own_crontab_extra_after_mutation)} ]; then
    printf '%s\\n' {self.q(self.own_crontab_extra_after_mutation)}
  fi
  exit 0
fi
echo "FAKE_CRONTAB_FORBIDDEN $*" >> {self.q(self.log)}; exit 97
''')
        _write_exe(bin_dir, "sudo", 'while [[ "$1" == -* ]]; do shift; done\nexec "$@"\n')
        _write_exe(bin_dir, "systemctl", '''
case "$1" in
  list-unit-files|list-units) exit 0 ;;
  *) echo "FAKE_SYSTEMCTL_FORBIDDEN $*" >&2; exit 97 ;;
esac
''')
        self._build_docker(bin_dir)
        self._build_curl(bin_dir)
        return bin_dir

    def _compose_env(self):
        phase_dependent = self.compose_env_after_mutation
        return self.compose_env, (phase_dependent if phase_dependent is not None else self.compose_env)

    def _build_docker(self, bin_dir):
        env_before, env_after = self._compose_env()

        def compose_json(env, st):
            env = dict(env)
            if st:
                env["SEARCH_TRANSPORT"] = st
            return json.dumps({"services": {"api": {"environment": env}}})

        def compose_yaml(image_var, env, st, extra):
            lines = ["services:", "  api:", f"    image: {image_var}", "    environment:"]
            for k, v in env.items():
                lines.append(f'      {k}: "{v}"')
            if st:
                lines.append(f'      SEARCH_TRANSPORT: "{st}"')
            if extra:
                lines.append(f"      {extra}")
            lines += ["  db:", "    image: postgres:16-alpine", "  frontend:", "    image: nginx:alpine"]
            return "\\n".join(lines)

        runtime_before = _env_merge(F476_IMAGE_ENV, self.compose_env, self.runtime_env_extra_before)
        if self.search_transport_before:
            runtime_before.append(f"SEARCH_TRANSPORT={self.search_transport_before}")
        runtime_before = [e for e in runtime_before if not e.startswith("TOR_ENABLED=")] + [f"TOR_ENABLED={self.tor_enabled_before}"]
        runtime_candidate = _env_merge(INCIDENT_IMAGE_ENV, env_after, self.candidate_env_extra)
        runtime_candidate = [e for e in runtime_candidate if not e.startswith("TOR_ENABLED=")] + [f"TOR_ENABLED={self.candidate_tor_enabled}"]
        runtime_rollback = _env_merge(F476_IMAGE_ENV, env_after)
        frontend_after = self.frontend_state_after or self.frontend_state
        state_fmt = '{{.Id}}|{{.Image}}|{{.RestartCount}}|{{.State.Running}}|{{with (index .State "Health")}}{{.Status}}{{else}}none{{end}}|{{.State.StartedAt}}|{{.Config.Image}}'
        labels_fmt = '{{index .Config.Labels "org.opencontainers.image.revision"}}|{{index .Config.Labels "io.jobpulse.incident.parent"}}|{{index .Config.Labels "io.jobpulse.incident.purpose"}}|{{index .Config.Labels "io.jobpulse.incident.build-run-id"}}'
        q = self.q

        body = f'''
LOG={q(self.log)}
PHASE=$(cat {q(self.phase_file)})
ROUND_FILE={q(self.round_file)}
printf '%s JOBPULSE_API_IMAGE=%s PHASE=%s\\n' "$*" "${{JOBPULSE_API_IMAGE:-}}" "$PHASE" >> "$LOG"
STATE_FMT={q(state_fmt)}
LABELS_FMT={q(labels_fmt)}
seq_value() {{
  local dir="{self.tmp}/$1_state" idx max
  max=$(ls "$dir" | sed -n 's/^\\([0-9]*\\)\\.running$/\\1/p' | sort -n | tail -1)
  idx=$(cat "$ROUND_FILE"); [ "$idx" -gt "$max" ] && idx=$max
  cat "$dir/$idx.$2"
}}
api_state() {{
  case "$PHASE" in
    before) echo {q(f"{self.api_id_before}|{self.api_image_before}|{self.api_restart_before}|{self.api_running_before}|{self.api_health_before}|2026-09-01T00:00:00Z|{self.api_config_image_before}")} ;;
    candidate)
      local r_dir={q(self.tmp / "candidate_restart")} r_max r_idx
      r_max=$(ls "$r_dir" | sort -n | tail -1); r_idx=$(cat "$ROUND_FILE"); [ "$r_idx" -gt "$r_max" ] && r_idx=$r_max
      echo "{"c" * 64}|$(seq_value candidate image)|$(cat "$r_dir/$r_idx")|$(seq_value candidate running)|$(seq_value candidate health)|2026-10-02T00:00:00Z|"{q(self.candidate_config_image)} ;;
    rollback) echo "{"r" * 64}|$(seq_value rollback image)|0|$(seq_value rollback running)|$(seq_value rollback health)|2026-10-02T00:01:00Z|"{q(self.rollback_config_image)} ;;
  esac
  echo $(( $(cat "$ROUND_FILE") + 1 )) > "$ROUND_FILE"
}}
runtime_env_json() {{
  case "$PHASE" in
    before) printf '%s' {q(json.dumps(runtime_before))} ;;
    candidate) printf '%s' {q(json.dumps(runtime_candidate))} ;;
    rollback) printf '%s' {q(json.dumps(runtime_rollback))} ;;
  esac
}}
cmd="$1"; shift
case "$cmd" in
  inspect)
    fmt=""; name=""
    while [ $# -gt 0 ]; do
      case "$1" in -f|--format) fmt="$2"; shift 2 ;; *) name="$1"; shift ;; esac
    done
    case "$name" in
      jobpulse-api-prod)
        case "$fmt" in
          "") exit 0 ;;
          "$STATE_FMT") api_state ;;
          '{{{{index .Config.Labels "com.docker.compose.service"}}}}|{{{{index .Config.Labels "com.docker.compose.project"}}}}') echo {q(self.api_labels)} ;;
          '{{{{json .Config.Env}}}}') runtime_env_json ;;
          '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}') runtime_env_json | python3 -c 'import json,sys; [print(e) for e in json.load(sys.stdin)]' ;;
          "$LABELS_FMT") if [ "$PHASE" = candidate ]; then echo {q(self.candidate_labels)}; else echo "||"; fi ;;
          *) echo "UNKNOWN_API_FORMAT $fmt" >&2; exit 1 ;;
        esac ;;
      jobpulse-postgres-prod) [ "$fmt" = "$STATE_FMT" ] && echo {q(self.db_state)} ;;
      jobpulse-frontend-prod)
        case "$fmt" in
          "$STATE_FMT") if [ "$PHASE" = before ]; then echo {q(self.frontend_state)}; else echo {q(frontend_after)}; fi ;;
          *Mounts*) echo {q(self.mount_line)} ;;
        esac ;;
      jobpulse-tor-prod) {"" if self.tor_present else "exit 1;"} [ "$fmt" = "$STATE_FMT" ] && echo {q(self.tor_state)} ;;
      *) exit 1 ;;
    esac ;;
  image)
    [ "$1" = inspect ] || exit 1; shift
    ref="$1"; shift; fmt=""
    [ "$1" = "--format" ] && fmt="$2"
    case "$ref|$fmt" in
      {q(INCIDENT_IMAGE_DIGEST_REFERENCE)}"|{{{{.Id}}}}") echo {q(self.pulled_image_id)} ;;
      {q(INCIDENT_IMAGE_DIGEST_REFERENCE)}"|$LABELS_FMT") echo {q(self.pulled_labels)} ;;
      {q(F476_IMAGE_DIGEST_REFERENCE)}"|{{{{.Id}}}}")
        if [ "$PHASE" != before ] && [ {int(self.f476_vanishes_before_rollback)} -eq 1 ]; then exit 1; fi
        if [ -e {q(self.f476_pulled_marker)} ]; then echo {q(self.f476_pulled_id)}; elif [ {int(self.f476_local_at_start)} -eq 1 ]; then echo {q(EXPECTED_F476_IMAGE_ID)}; else exit 1; fi ;;
      {q(F476_IMAGE_DIGEST_REFERENCE)}"|{{{{.Os}}}}/{{{{.Architecture}}}}") [ -e {q(self.f476_pulled_marker)} ] && echo {q(self.f476_pulled_platform)} || exit 1 ;;
      *) exit 1 ;;
    esac ;;
  exec)
    name="$1"; shift
    if [ "$1" = printenv ]; then
      runtime_env_json | python3 -c 'import json,sys; d=dict(e.split("=",1) for e in json.load(sys.stdin)); v=d.get(sys.argv[1]); print(v) if v is not None else sys.exit(1)' "$2"
      exit $?
    fi
    if [ "$1" = python ] && [[ "$3" == *collector_postgres.py* ]]; then
      case "$PHASE" in
        before) c={q(self.collector_before)} ;;
        candidate) c={q(self.candidate_collector)} ;;
        rollback) c={q(self.rollback_collector)} ;;
      esac
      printf '%s' "$c" | {q(_REAL["sha256sum"])} | cut -d' ' -f1; exit 0
    fi
    if [ "$1" = python ] && [[ "$3" == *linkedin_browser_provider.py* ]]; then
      case "$PHASE" in
        candidate) c={q(self.candidate_provider)} ;;
        rollback) c={q(self.rollback_provider)} ;;
        *) c={q(self.provider_before)} ;;
      esac
      printf '%s' "$c" | {q(_REAL["sha256sum"])} | cut -d' ' -f1; exit 0
    fi
    if [ "$1" = python ] && [[ "$3" == *get_search_transport_mode* ]]; then echo {q(self.candidate_transport_mode)}; exit 0; fi
    echo "FAKE_EXEC_FORBIDDEN $*" >> "$LOG"; exit 97 ;;
  top) printf '%s\\n' {q(self.docker_top)} ;;
  port) printf '%s' {q(self.tor_ports)} ;;
  login) cat > /dev/null ;;
  pull)
    if [ "$1" = {q(INCIDENT_IMAGE_DIGEST_REFERENCE)} ]; then exit 0; fi
    if [ "$1" = {q(F476_IMAGE_DIGEST_REFERENCE)} ]; then
      [ {int(self.f476_pull_exit)} -eq 0 ] || exit {int(self.f476_pull_exit)}
      touch {q(self.f476_pulled_marker)}; exit 0
    fi
    echo "FAKE_UNEXPECTED_PULL $1" >> "$LOG"; exit 1 ;;
  compose)
    sub=""; services=no; json=no; up=no; prev=""
    for a in "$@"; do
      case "$a" in config) sub=config ;; up) up=yes ;; --services) services=yes ;; json) [ "$prev" = --format ] && json=yes ;; esac
      prev="$a"
    done
    if [ "$sub" = config ] && [ "$services" = yes ]; then printf 'db\\napi\\nfrontend\\n'; exit 0; fi
    if [ "$PHASE" = before ]; then ENVJSON_CUR={q(compose_json(env_before, self.compose_search_transport_current))}; ENVJSON_CAND={q(compose_json(env_before, self.compose_search_transport_candidate))}
    else ENVJSON_CUR={q(compose_json(env_after, self.compose_search_transport_current))}; ENVJSON_CAND={q(compose_json(env_after, self.compose_search_transport_candidate))}; fi
    if [ "$sub" = config ] && [ "$json" = yes ]; then
      if [ "$JOBPULSE_API_IMAGE" = {q(INCIDENT_IMAGE_DIGEST_REFERENCE)} ]; then printf '%s' "$ENVJSON_CAND"; else printf '%s' "$ENVJSON_CUR"; fi
      exit 0
    fi
    if [ "$sub" = config ]; then
      if [ "$PHASE" = before ]; then
        Y_CUR={q(compose_yaml("IMG", env_before, self.compose_search_transport_current, ""))}
        Y_CAND={q(compose_yaml("IMG", env_before, self.compose_search_transport_candidate, self.candidate_extra_config_line))}
      else
        Y_CUR={q(compose_yaml("IMG", env_after, self.compose_search_transport_current, ""))}
        Y_CAND={q(compose_yaml("IMG", env_after, self.compose_search_transport_candidate, self.candidate_extra_config_line))}
      fi
      if [ "$JOBPULSE_API_IMAGE" = {q(INCIDENT_IMAGE_DIGEST_REFERENCE)} ]; then Y="$Y_CAND"; else Y="$Y_CUR"; fi
      printf '%b\\n' "${{Y/IMG/$JOBPULSE_API_IMAGE}}"
      exit 0
    fi
    if [ "$up" = yes ]; then
      if [ "$JOBPULSE_API_IMAGE" = {q(INCIDENT_IMAGE_DIGEST_REFERENCE)} ]; then
        echo candidate > {q(self.phase_file)}; echo 0 > "$ROUND_FILE"; touch {q(self.candidate_marker)}
        exit {int(self.candidate_up_exit)}
      elif [ "$JOBPULSE_API_IMAGE" = {q(F476_IMAGE_DIGEST_REFERENCE)} ]; then
        [ {int(self.rollback_up_exit)} -eq 0 ] || exit {int(self.rollback_up_exit)}
        echo rollback > {q(self.phase_file)}; echo 0 > "$ROUND_FILE"; touch {q(self.rollback_marker)}
        exit 0
      fi
      echo "FAKE_UNEXPECTED_UP" >> "$LOG"; exit 1
    fi
    exit 1 ;;
  *) echo "FAKE_DOCKER_FORBIDDEN $cmd $*" >> "$LOG"; exit 97 ;;
esac
'''
        _write_exe(bin_dir, "docker", body)

    def _build_curl(self, bin_dir):
        q = self.q
        _write_exe(bin_dir, "curl", f'''
url="${{@: -1}}"
PHASE=$(cat {q(self.phase_file)})
ok() {{
  [ "$PHASE" = before ] && {{ printf '%s' {q(self.health_body_before)}; exit 0; }}
  local dir="{self.tmp}/${{PHASE}}_state" idx max
  max=$(ls "$dir" | sed -n 's/^\\([0-9]*\\)\\.running$/\\1/p' | sort -n | tail -1)
  idx=$(cat {q(self.round_file)}); [ "$idx" -gt "$max" ] && idx=$max
  [ "$(cat "$dir/$idx.$1")" = true ] && {{ printf '%s' {q(HEALTH_OK)}; exit 0; }}
  exit 22
}}
case "$url" in
  http://127.0.0.1:8000/health) ok direct ;;
  http://127.0.0.1/api/health) ok nginx ;;
  http://127.0.0.1/) printf '%s' {q(self.frontend_content)} ;;
  *) exit 7 ;;
esac
''')

    def run(self, remote_script: str, *, hold_outer_lock=False):
        bin_dir = self.build()
        script = (
            remote_script.replace("/opt/jobpulse", str(self.root))
            .replace("/tmp/jobpulse_collection_cycle.lock", str(self.outer_lock))
            .replace("/etc/crontab", str(self.etc_crontab))
            .replace("/etc/cron.d", str(self.etc_cron_d))
        )
        b64 = lambda v: base64.b64encode(json.dumps(v).encode()).decode()
        args = [
            hashlib.sha256(self.compose_content.encode()).hexdigest(),
            hashlib.sha256(self.frontend_content.encode()).hexdigest(),
            hashlib.sha256(F476_COLLECTOR.encode()).hexdigest(),
            hashlib.sha256(INCIDENT_COLLECTOR.encode()).hexdigest(),
            hashlib.sha256(F476_PROVIDER.encode()).hexdigest(),
            b64(F476_IMAGE_ENV),
            b64(INCIDENT_IMAGE_ENV),
            "false",
        ]
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
        holder = None
        if hold_outer_lock:
            holder = sp.Popen(["flock", str(self.outer_lock), "sleep", "10"])
            import time

            time.sleep(0.3)
        try:
            result = sp.run(["bash", "-c", script, "bash", *args], capture_output=True, text=True,
                            timeout=120, env=env, cwd=self.tmp)
        finally:
            if holder is not None:
                holder.terminate()
                holder.wait(timeout=5)
        result.log = self.log.read_text() if self.log.exists() else ""
        return result


def _out(r):
    return r.stdout + r.stderr + "\n--- docker log ---\n" + r.log


def _assert_aborted_before_mutation(r, s, needle):
    assert r.returncode != 0, _out(r)
    assert needle in r.stderr, _out(r)
    assert "INCIDENT_DEPLOY_ABORTED_BEFORE_MUTATION" in r.stdout, _out(r)
    assert not s.candidate_marker.exists() and not s.rollback_marker.exists(), _out(r)
    assert " up " not in r.log and not r.log.startswith("up "), _out(r)


def _assert_no_forbidden_calls(r):
    assert "FORBIDDEN" not in r.log, _out(r)
    assert "FAKE_UNEXPECTED_UP" not in r.log, _out(r)


# --- happy paths --------------------------------------------------------------
def test_b01_happy_path_from_known_rollback_alias_state(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_sequence=(
        dict(running="true", health="starting", image=INCIDENT_IMAGE_INDEX_DIGEST, direct=False, nginx=False),
        dict(running="true", health="healthy", image=INCIDENT_IMAGE_INDEX_DIGEST, direct=True, nginx=True),
    ))
    r = s.run(remote_script)
    assert r.returncode == 0, _out(r)
    assert "INCIDENT_259C507_API_DEPLOYED" in r.stdout
    assert "api_config_image_before=known_f476_rollback_alias" in r.stdout
    assert s.candidate_marker.exists() and not s.rollback_marker.exists()
    # A/C: f476 absent at start, pulled by digest, verified, preflight continued.
    assert "f476_rollback_artifact_locally_present_before_pull=no" in r.stdout
    assert f"f476_rollback_artifact=confirmed id={EXPECTED_F476_IMAGE_ID}" in r.stdout
    assert "api_config_image_before_local_resolution=not_required" in r.stdout
    assert f"rollback_prepared=yes source={F476_IMAGE_DIGEST_REFERENCE}" in r.stdout
    assert "effective_rollback_config_parity=confirmed_image_reference_only" in r.stdout
    assert "rollback_env_parity=confirmed" in r.stdout
    _assert_no_forbidden_calls(r)
    ups = [line for line in r.log.splitlines() if line.startswith("compose") and " up " in line]
    assert len(ups) == 1
    assert ups[0].startswith("compose -p jobpulse -f ") and ups[0].split(" JOBPULSE_API_IMAGE=")[0].endswith(
        "up -d --no-build --no-deps --force-recreate api")
    assert f"JOBPULSE_API_IMAGE={INCIDENT_IMAGE_DIGEST_REFERENCE}" in ups[0]
    pulls = [line for line in r.log.splitlines() if line.startswith("pull ")]
    assert [p.split(" JOBPULSE_API_IMAGE")[0] for p in pulls] == [f"pull {INCIDENT_IMAGE_DIGEST_REFERENCE}", f"pull {F476_IMAGE_DIGEST_REFERENCE}"]
    assert not re.search(r"^(tag|rmi|image rm)", r.log, re.MULTILINE)
    for marker in ("runtime_env_parity=confirmed", "effective_config_parity=confirmed_image_reference_only",
                   "in_container_collector=f476", "in_container_collector=259c507", "post_deploy_env_parity=confirmed",
                   "db_frontend_tor_unchanged=confirmed", "git_invariant=confirmed_f476", "scheduler_unchanged=confirmed",
                   "api_stability=confirmed", "collection_cycle_run=no", "phase4m_action=none",
                   "rollback_prepared=yes", "in_container_provider=f476_no_phase4b"):
        assert marker in r.stdout, marker
    assert r.stdout.count("deploy_inputs_proof=confirmed") == 2
    assert "secret-value" not in r.stdout + r.stderr  # env values never printed


def test_b02_happy_path_from_canonical_f476_reference_state(remote_script, tmp_path):
    s = Scenario(tmp_path, api_config_image_before=CANONICAL_F476_REFERENCE)
    r = s.run(remote_script)
    assert r.returncode == 0, _out(r)
    assert "api_config_image_before=canonical_f476_reference" in r.stdout


# --- pre-mutation fail-closed --------------------------------------------------
@pytest.mark.parametrize(
    "overrides,needle",
    [
        (dict(git_sha="0" * 40), "production git HEAD"),
        (dict(git_status_lines=(" M scripts/collector_postgres.py",)), "tracked worktree is not clean"),
        (dict(api_labels="api|otherproject"), "compose labels"),
        (dict(api_image_before="sha256:" + "8" * 64), "API underlying image ID"),
        (dict(api_image_before="sha256:" + "8" * 64, api_config_image_before=CANONICAL_F476_REFERENCE), "API underlying image ID"),
        (dict(api_config_image_before="ghcr.io/mrezamaghouli/jobpulse-api:main"), "neither the known f476 rollback alias"),
        (dict(api_config_image_before="jobpulse-api-rollback:other"), "neither the known f476 rollback alias"),
        # E: f476 pull failure; D: wrong f476 ID; wrong platform.
        (dict(f476_pull_exit=1), "could not pull the f476 rollback artifact"),
        (dict(f476_pulled_id="sha256:" + "9" * 64), "f476 rollback artifact resolved to ID"),
        (dict(f476_pulled_id=INCIDENT_IMAGE_INDEX_DIGEST), "f476 rollback artifact resolved to ID"),
        (dict(f476_pulled_platform="linux/arm64"), "f476 rollback artifact platform"),
        (dict(api_health_before="starting"), "API container is not healthy"),
        (dict(api_restart_before="2"), "restart_count is not 0"),
        (dict(health_body_before=HEALTH_DB_DOWN), "API direct health check failed"),
        (dict(search_transport_before="proxy"), "SEARCH_TRANSPORT is neither unset nor 'direct'"),
        (dict(tor_enabled_before="true"), "TOR_ENABLED is not exactly 'false'"),
        (dict(collector_before="# something else\n"), "is not the f476 version"),
        (dict(provider_before="# phase 4b provider\n"), "linkedin_browser_provider.py SHA-256"),
        (dict(db_state="d" * 64 + "|sha256:db|0|true|unhealthy|2026|postgres"), "DB is not healthy"),
        (dict(tor_present=False), "jobpulse-tor-prod does not exist"),
        (dict(tor_ports="9050/tcp -> 0.0.0.0:9050"), "Tor publishes host ports"),
        (dict(mount_line="bind|/elsewhere|false"), "frontend html mount"),
        (dict(own_crontab_ok=False), "cannot read this account's own crontab"),
        (dict(docker_top="PID USER TIME COMMAND\n7 root 0:01 python -m scripts.process_search_demand_queue"), "active collector process detected"),
        (dict(pulled_image_id="sha256:" + "5" * 64), "incident image resolved to ID"),
        (dict(pulled_image_id=EXPECTED_F476_IMAGE_ID), "incident image resolved to ID"),
        (dict(pulled_labels=f"{TARGET_SOURCE_SHA}|{EXPECTED_PRODUCTION_GIT_SHA}|{INCIDENT_PURPOSE_LABEL}|1"), "incident image labels"),
        (dict(pulled_labels=f"{TARGET_SOURCE_SHA}|{EXPECTED_PRODUCTION_GIT_SHA}|other-purpose|{INCIDENT_BUILD_RUN_ID}"), "incident image labels"),
        (dict(compose_search_transport_current="proxy"), "effective current Compose SEARCH_TRANSPORT"),
        (dict(compose_search_transport_candidate="proxy"), "effective candidate Compose SEARCH_TRANSPORT"),
        (dict(candidate_extra_config_line='EXTRA_ONLY_IN_CANDIDATE: "1"'), "differ beyond the api image reference"),
        (dict(runtime_env_extra_before={"STALE_KEY": "x"}), "running API environment does not match"),
    ],
)
def test_b10_preflight_fails_closed_without_mutation(remote_script, tmp_path, overrides, needle):
    s = Scenario(tmp_path, **overrides)
    r = s.run(remote_script)
    _assert_aborted_before_mutation(r, s, needle)


def test_b11_outer_lock_busy_aborts(remote_script, tmp_path):
    s = Scenario(tmp_path)
    r = s.run(remote_script, hold_outer_lock=True)
    _assert_aborted_before_mutation(r, s, "outer collection lock busy")


# --- post-mutation failure -> verified rollback ----------------------------------
@pytest.mark.parametrize(
    "overrides,needle",
    [
        (dict(candidate_up_exit=1), "INCIDENT_DEPLOY_ERROR_TRACE"),
        (dict(candidate_sequence=(dict(running="true", health="starting", image=INCIDENT_IMAGE_INDEX_DIGEST, direct=True, nginx=True),)), "did not converge"),
        (dict(candidate_sequence=(dict(running="true", health="healthy", image="sha256:" + "6" * 64, direct=True, nginx=True),)), "did not converge"),
        (dict(candidate_sequence=(dict(running="true", health="healthy", image=INCIDENT_IMAGE_INDEX_DIGEST, direct=True, nginx=False),)), "did not converge"),
        (dict(candidate_config_image=INCIDENT_IMAGE_REFERENCE), "does not equal expected"),
        (dict(candidate_restart_sequence=("0", "0", "0", "0", "1")), "not stable across the observation window"),
        (dict(candidate_labels="x|y|z"), "running API labels"),
        (dict(candidate_collector=F476_COLLECTOR), "is not the 259c507 version"),
        (dict(candidate_provider="# phase 4b provider\n"), "Phase 4B code must not be deployed"),
        (dict(candidate_transport_mode="proxy"), "get_search_transport_mode()"),
        (dict(candidate_tor_enabled="true"), "TOR_ENABLED is not exactly 'false'"),
        (dict(candidate_env_extra={"SURPRISE": "1"}), "post-deploy API environment is not exactly"),
        (dict(git_sha_after_mutation="1" * 40), None),
    ],
)
def test_b20_post_deploy_failure_triggers_verified_rollback(remote_script, tmp_path, overrides, needle):
    s = Scenario(tmp_path, **overrides)
    r = s.run(remote_script)
    if overrides.get("git_sha_after_mutation"):
        # Git drift is not repairable by an API-only rollback: rollback must
        # detect it and refuse to claim the pre-state was restored.
        assert r.returncode != 0
        assert s.rollback_marker.exists()
        assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)
        return
    assert r.returncode != 0, _out(r)
    if needle:
        assert needle in r.stderr, _out(r)
    assert s.candidate_marker.exists() and s.rollback_marker.exists(), _out(r)
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_CONFIRMED_PRESTATE_RESTORED" in r.stdout, _out(r)
    assert "INCIDENT_259C507_API_DEPLOYED" not in r.stdout
    assert "rollback_confirmed=true" in r.stderr
    _assert_no_forbidden_calls(r)
    rb = [line for line in r.log.splitlines() if line.startswith("compose") and " up " in line][-1]
    # H/I: rollback recreates from the f476 digest (never the alias) and its
    # Config.Image is accepted as that digest reference.
    assert f"JOBPULSE_API_IMAGE={F476_IMAGE_DIGEST_REFERENCE}" in rb
    assert rb.split(" JOBPULSE_API_IMAGE=")[0].endswith("up -d --no-build --no-deps --force-recreate api")
    assert KNOWN_F476_ROLLBACK_ALIAS not in rb
    assert not re.search(r"^(tag|rmi|image rm)", r.log, re.MULTILINE)
    assert r.log.count(" up ") == 2  # Q: exactly one candidate + exactly one rollback


def test_b21_rollback_uses_f476_digest_even_when_prestate_was_canonical_reference(remote_script, tmp_path):
    s = Scenario(tmp_path, api_config_image_before=CANONICAL_F476_REFERENCE, candidate_collector=F476_COLLECTOR)
    r = s.run(remote_script)
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_CONFIRMED_PRESTATE_RESTORED" in r.stdout, _out(r)
    assert f"JOBPULSE_API_IMAGE={F476_IMAGE_DIGEST_REFERENCE}" in [l for l in r.log.splitlines() if " up " in l][-1]
    assert not re.search(r"^tag ", r.log, re.MULTILINE)


def test_b21b_f476_already_local_is_still_verified_and_accepted(remote_script, tmp_path):
    s = Scenario(tmp_path, f476_local_at_start=True)
    r = s.run(remote_script)
    assert r.returncode == 0, _out(r)
    assert "f476_rollback_artifact_locally_present_before_pull=yes" in r.stdout


def test_b21c_rollback_config_image_must_be_the_f476_digest(remote_script, tmp_path):
    """I (negative): after rollback the container must report the f476
    digest reference; anything else (e.g. the old alias) is not confirmed."""
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, rollback_config_image=KNOWN_F476_ROLLBACK_ALIAS)
    r = s.run(remote_script)
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)
    assert "does not equal expected" in r.stderr


def test_b21d_rollback_detects_phase4b_provider(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, rollback_provider="# phase 4b provider\n")
    r = s.run(remote_script)
    assert "rolled-back API is not running the f476 provider" in r.stderr
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)


def test_b21e_rollback_detects_scheduler_drift(remote_script, tmp_path):
    s = Scenario(tmp_path, own_crontab_extra_after_mutation="0 * * * * echo drift")
    r = s.run(remote_script)
    assert "scheduler inputs changed during deployment" in r.stderr
    assert s.rollback_marker.exists()
    assert "scheduler inputs differ from the pre-deployment fingerprint after rollback" in r.stderr
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)


def test_b22_rollback_itself_is_verified_not_assumed(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, rollback_sequence=(
        dict(running="true", health="unhealthy", image=EXPECTED_F476_IMAGE_ID, direct=False, nginx=False),))
    r = s.run(remote_script)
    assert r.returncode != 0
    assert s.rollback_marker.exists()
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)
    assert "api_state_classification=F476_API_RUNNING" in r.stderr
    assert "rollback_confirmed=false" in r.stderr
    assert r.log.count(" up ") == 2  # candidate + exactly one rollback, never a second attempt


def test_b23_rollback_wrong_collector_is_not_confirmed(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, rollback_collector=INCIDENT_COLLECTOR)
    r = s.run(remote_script)
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)
    assert "rolled-back API is not running the f476 collector" in r.stderr


def test_b24_rollback_refuses_on_input_drift(remote_script, tmp_path):
    drifted = dict(COMPOSE_ENV, SEARCH_TRANSPORT="proxy")
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, compose_env_after_mutation=drifted)
    r = s.run(remote_script)
    assert r.returncode != 0
    assert "ROLLBACK_ABORTED_INPUT_DRIFT" in r.stderr, _out(r)
    assert not s.rollback_marker.exists()
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout
    assert "api_state_classification=INCIDENT_API_PRESENT" in r.stderr


def test_b25_rollback_detects_disturbed_frontend(remote_script, tmp_path):
    s = Scenario(tmp_path, frontend_state_after="e" * 64 + "|sha256:fe|0|true|none|2026-10-02T00:00:00Z|nginx:alpine")
    r = s.run(remote_script)
    assert "frontend container identity/state changed" in r.stderr
    assert s.rollback_marker.exists()
    # The API was restored, but the pre-state is NOT fully restored -> not confirmed.
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout, _out(r)


def test_b26_rollback_refuses_if_f476_artifact_vanished(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, f476_vanishes_before_rollback=True)
    r = s.run(remote_script)
    assert "f476 rollback artifact" in r.stderr and "no longer locally present" in r.stderr, _out(r)
    assert not s.rollback_marker.exists()
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout


def test_b26b_rollback_recreation_failure_is_not_confirmed(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR, rollback_up_exit=1)
    r = s.run(remote_script)
    assert "rollback recreation command failed" in r.stderr, _out(r)
    assert "INCIDENT_DEPLOY_FAILED_ROLLBACK_NOT_CONFIRMED_MANUAL_REVIEW_REQUIRED" in r.stdout


def test_b27_no_rollback_when_preflight_fails(remote_script, tmp_path):
    s = Scenario(tmp_path, pulled_image_id="sha256:" + "5" * 64)
    r = s.run(remote_script)
    assert "rollback_attempted=false" in r.stderr
    assert " up " not in r.log
    _assert_no_forbidden_calls(r)


def test_b28_remote_script_never_writes_scheduler_or_git_in_any_scenario(remote_script, tmp_path):
    s = Scenario(tmp_path, candidate_collector=F476_COLLECTOR)
    r = s.run(remote_script)
    _assert_no_forbidden_calls(r)
    assert "FAKE_GIT_FORBIDDEN" not in r.log and "FAKE_CRONTAB_FORBIDDEN" not in r.log
