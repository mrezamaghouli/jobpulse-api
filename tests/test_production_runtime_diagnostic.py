"""Static audit of .github/workflows/production-runtime-diagnostic.yml
(Phase 3.1A -- read-only production frontend/container diagnosis).

This workflow only ever gathers evidence over SSH: `git status`/`rev-parse`,
`docker compose ... config|ps`, `docker ps`, a small number of metadata-only
`docker inspect` calls, and two plain `curl` health probes. It must never
start/stop/restart/create/remove any container, never touch Tor, never read
a secret, and never mutate git or the filesystem on the production VM.

No SSH, no real Docker, no network -- these are all static source/YAML
checks against the version-controlled workflow file, following the same
discipline as tests/test_tor_secret_provision.py's static workflow checks.
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "production-runtime-diagnostic.yml"
DOCKER_BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "docker-build.yml"
TOR_IMAGE_BUILD_PATH = REPO_ROOT / ".github" / "workflows" / "tor-image-build.yml"
DEPLOY_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

EXPECTED_PRODUCTION_SHA = "5dffbd669eec52f5283503bb6409a430509175a0"

PHASE_3_1A_CHANGED_FILES = [
    ".github/workflows/production-runtime-diagnostic.yml",
    ".github/workflows/ci.yml",
    "tests/test_production_runtime_diagnostic.py",
]


def _load_workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _on_block(data: dict):
    # PyYAML (1.1 resolver) parses the bare `on:` key as boolean True.
    if "on" in data:
        return data["on"]
    return data[True]


def _code_only(source: str) -> str:
    """Strip full-line `#` comments so substring checks don't false-positive
    on prose (e.g. this file's own header comment mentioning forbidden
    commands to explain why they're absent)."""
    return "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )


def test_workflow_file_exists_and_parses_as_yaml():
    assert WORKFLOW_PATH.is_file()
    data = _load_workflow(WORKFLOW_PATH)
    assert data["name"] == "Production Runtime Diagnostic"


# =====================================================================
# Trigger
# =====================================================================
def test_workflow_is_workflow_dispatch_only():
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert set(on_block.keys() if isinstance(on_block, dict) else []) == {"workflow_dispatch"} \
        or on_block == {"workflow_dispatch": None}


def test_workflow_source_has_no_other_trigger_keys():
    source = WORKFLOW_PATH.read_text()
    for forbidden in ("push:", "pull_request:", "workflow_run:", "schedule:"):
        assert forbidden not in source, forbidden


def test_workflow_permissions_are_read_only():
    data = _load_workflow(WORKFLOW_PATH)
    assert data["permissions"] == {"contents": "read"}


# =====================================================================
# Secrets
# =====================================================================
def test_workflow_uses_only_vm_ssh_key_secret():
    source = WORKFLOW_PATH.read_text()
    referenced_secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", source))
    assert referenced_secrets == {"VM_SSH_KEY"}


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


def test_workflow_ssh_key_chmod_is_the_only_chmod_and_is_key_material_only():
    """The workflow legitimately runs `chmod 700 ~/.ssh` / `chmod 600
    .../jobpulse_diagnostic_key` to protect the SSH key material on the
    *runner*, not on production. Assert that's the only use of chmod, and
    that it never touches anything under /opt/jobpulse."""
    source = WORKFLOW_PATH.read_text()
    chmod_lines = [l for l in source.splitlines() if "chmod" in l and not l.strip().startswith("#")]
    assert len(chmod_lines) == 2, chmod_lines
    for line in chmod_lines:
        assert "~/.ssh" in line or "jobpulse_diagnostic_key" in line
        assert "/opt/jobpulse" not in line


# =====================================================================
# No secret reads / Tor / environment inspection
# =====================================================================
FORBIDDEN_SECRET_AND_TOR_TOKENS = (
    ".tor_control_password",
    "NEWNYM",
    "AUTHENTICATE",
    "SETEVENTS",
    "SOCKS",
    "ControlPort",
    "9050",
    "9051",
)


def test_workflow_never_reads_secrets_or_touches_tor():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in FORBIDDEN_SECRET_AND_TOR_TOKENS:
        assert forbidden not in code, forbidden


def test_workflow_never_inspects_docker_environment_or_config():
    """docker inspect must only ever use the restricted metadata format
    string -- never .Config.Env, never a bare/no-format inspect that would
    dump full container JSON (including env vars and mounts). This
    includes the existence probe itself -- it must not be a bare
    `docker inspect "$name"` with no --format, even though its output is
    discarded to /dev/null (a bare inspect still touches full object
    data internally and is banned outright per Phase 3.1A hardening)."""
    code = _code_only(WORKFLOW_PATH.read_text())
    assert ".Config.Env" not in code
    assert "docker inspect" in code

    for line in code.splitlines():
        if "docker inspect" not in line:
            continue
        # every single docker inspect invocation in this file -- existence
        # probe included -- must carry --format. No bare inspect anywhere.
        assert "--format" in line, line


def test_workflow_no_bare_docker_inspect_exists():
    """Belt-and-suspenders regex check: no `docker inspect <arg>` occurrence
    is immediately followed by something other than ` --format` (allowing
    for the trailing `>/dev/null 2>&1` on the existence probe)."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for match in re.finditer(r"docker inspect\s+\S+", code):
        tail_start = match.end()
        tail = code[tail_start:tail_start + 40]
        assert tail.lstrip().startswith("--format"), (match.group(0), tail)


def test_workflow_no_forbidden_fields_anywhere_in_inspect_templates():
    """Mounts/Binds/Secrets must never appear in ANY docker inspect
    --format template in this file, not just the metadata-report one."""
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden_field in ("Mounts", "Binds", "Secrets"):
        assert forbidden_field not in code, forbidden_field


def test_workflow_inspect_format_restricted_to_allowed_metadata_fields():
    source = WORKFLOW_PATH.read_text()
    match = re.search(r"docker inspect \"\$name\" --format \\\s*\n\s*'([^']+)'", source)
    assert match, "expected a single-quoted --format template for inspect_metadata"
    template = match.group(1)

    allowed_gostruct_fields = ("{{.Name}}", "{{.Id}}", "{{.Image}}", "{{.State.Status}}",
                                "{{.State.StartedAt}}", "{{.RestartCount}}",
                                "{{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}")
    for field in allowed_gostruct_fields:
        assert field in template, field

    # No env/mount/secret-shaped field made it into the template.
    for forbidden_field in ("Env", "Mounts", "Binds", "Secrets"):
        assert forbidden_field not in template, forbidden_field


def test_workflow_inspects_exactly_the_three_expected_containers():
    source = WORKFLOW_PATH.read_text()
    for name in ("jobpulse-api-prod", "jobpulse-postgres-prod", "jobpulse-frontend-prod"):
        assert f"inspect_metadata {name}" in source


def test_workflow_existence_probe_uses_restricted_format_not_bare_inspect():
    """Section 2 hardening: the existence check itself must carry
    --format '{{.Id}}' (or equally restricted), never a bare
    `docker inspect "$name" >/dev/null 2>&1`."""
    source = WORKFLOW_PATH.read_text()
    match = re.search(
        r'if docker inspect "\$name" --format \'([^\']+)\' >/dev/null 2>&1; then',
        source,
    )
    assert match, "expected existence probe to use --format"
    template = match.group(1)
    assert template == "{{.Id}}"


# =====================================================================
# No file creation on production
# =====================================================================
def test_remote_block_never_redirects_output_into_a_new_file():
    """The remote (production-side) heredoc must never write a file --
    only stdout/stderr, which the runner captures locally. `>` / `>>`
    redirection targeting a path is disallowed anywhere inside the
    REMOTE...REMOTE block."""
    source = WORKFLOW_PATH.read_text()
    remote_match = re.search(r"<<'REMOTE'[^\n]*\n(.*?)\n\s*REMOTE\n", source, re.DOTALL)
    assert remote_match, "expected a <<'REMOTE' ... REMOTE heredoc block"
    remote_code = _code_only(remote_match.group(1))

    for line in remote_code.splitlines():
        # `2>&1` (fd duplication) and redirects to /dev/null (discarding,
        # not writing a file) are fine; `>` followed by any other path is
        # not.
        stripped = re.sub(r"\d?>&\d", "", line)
        stripped = re.sub(r">\s*/dev/null", "", stripped)
        assert not re.search(r"[^|]>\s*\S", stripped), line


def test_remote_block_is_quoted_heredoc_not_expanded_locally():
    """<<'REMOTE' (quoted delimiter) is required so runner-side variable
    expansion never leaks into the command sent to production."""
    source = WORKFLOW_PATH.read_text()
    assert "<<'REMOTE'" in source


# =====================================================================
# Required read-only diagnostic commands are present
# =====================================================================
EXPECTED_REMOTE_COMMANDS = (
    "cd /opt/jobpulse",
    "hostname",
    "git rev-parse HEAD",
    "git status --short",
    "docker compose -f \"$COMPOSE_FILE\" config --services",
    "docker compose -f \"$COMPOSE_FILE\" ps -a",
    "docker ps -a",
    "docker compose -f \"$COMPOSE_FILE\" ps -a -q frontend",
    "label=com.docker.compose.service=frontend",
    "name=jobpulse",
    "publish=80",
    "curl -fsS http://127.0.0.1:8000/health",
    "curl -sS -o /dev/null",
    "http://127.0.0.1/",
)


def test_all_required_diagnostic_commands_present():
    source = WORKFLOW_PATH.read_text()
    for expected in EXPECTED_REMOTE_COMMANDS:
        assert expected in source, expected


def test_frontend_probe_does_not_print_page_body():
    """curl against http://127.0.0.1/ must discard the body (-o /dev/null)
    and only report the status code via -w, never print the HTML."""
    source = WORKFLOW_PATH.read_text()
    match = re.search(r"curl -sS -o /dev/null -w '([^']+)'.*\n\s*http://127\.0\.0\.1/", source)
    assert match, "expected the frontend curl probe with -o /dev/null and -w status format"
    assert "%{http_code}" in match.group(1)


# =====================================================================
# Expected vs. actual production SHA reporting (evidence only)
# =====================================================================
def test_expected_production_sha_literal_is_exact():
    source = WORKFLOW_PATH.read_text()
    assert f'EXPECTED_PRODUCTION_SHA="{EXPECTED_PRODUCTION_SHA}"' in source
    assert len(EXPECTED_PRODUCTION_SHA) == 40
    assert all(c in "0123456789abcdef" for c in EXPECTED_PRODUCTION_SHA)


def test_expected_production_sha_is_not_a_workflow_dispatch_input():
    """Mirrors the Phase 3.1 secret-provision workflow's discipline: the
    expected SHA is a hardcoded, non-secret constant in the remote script,
    never an operator-editable workflow_dispatch input (which could target
    the wrong commit by typo)."""
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert on_block == {"workflow_dispatch": None}


def test_actual_production_sha_obtained_only_via_git_rev_parse_head():
    source = WORKFLOW_PATH.read_text()
    assert 'ACTUAL_PRODUCTION_SHA="$(git rev-parse HEAD)"' in source
    # Guard against a second, different mechanism (e.g. reading a file or
    # env var) sneaking in to source the "actual" SHA.
    assignments = re.findall(r'ACTUAL_PRODUCTION_SHA="\$\(([^)]+)\)"', source)
    assert assignments == ["git rev-parse HEAD"]


def test_sha_comparison_reports_expected_actual_and_match_yes_no():
    source = WORKFLOW_PATH.read_text()
    assert 'echo "production_sha_expected=$EXPECTED_PRODUCTION_SHA"' in source
    assert 'echo "production_sha_actual=$ACTUAL_PRODUCTION_SHA"' in source
    assert 'echo "production_sha_match=yes"' in source
    assert 'echo "production_sha_match=no"' in source
    assert '[ "$ACTUAL_PRODUCTION_SHA" = "$EXPECTED_PRODUCTION_SHA" ]' in source


def test_sha_mismatch_does_not_abort_the_remote_diagnostic():
    """The remote block must not `exit`/`fail` on a SHA mismatch -- a
    mismatch is evidence to report, not a reason to stop gathering the
    rest of the evidence. The only early-exit in the whole remote block is
    the `cd /opt/jobpulse` guard, which is unrelated to the SHA check."""
    source = WORKFLOW_PATH.read_text()
    remote_match = re.search(r"<<'REMOTE'[^\n]*\n(.*?)\n\s*REMOTE\n", source, re.DOTALL)
    assert remote_match
    remote_code = _code_only(remote_match.group(1))

    sha_block_match = re.search(
        r"expected vs actual production SHA.*?(?=\n\s*echo \"== )",
        remote_code, re.DOTALL,
    )
    assert sha_block_match, "expected a delimited SHA-comparison section"
    sha_block = sha_block_match.group(0)
    assert "exit" not in sha_block
    assert "fail" not in sha_block


def test_no_git_fetch_reset_checkout_or_pull_anywhere():
    code = _code_only(WORKFLOW_PATH.read_text())
    for forbidden in ("git fetch", "git reset", "git checkout", "git pull"):
        assert forbidden not in code, forbidden


# =====================================================================
# No build/deploy trigger path is matched by this new file
# =====================================================================
def test_new_workflow_name_is_distinct_from_other_workflows():
    data = _load_workflow(WORKFLOW_PATH)
    other_workflow_dir = WORKFLOW_PATH.parent
    for other in other_workflow_dir.glob("*.yml"):
        if other == WORKFLOW_PATH:
            continue
        other_data = _load_workflow(other)
        assert other_data.get("name") != data["name"], other


def test_no_other_workflow_references_the_new_workflow_by_name():
    """Guards against this new workflow accidentally being wired up as a
    workflow_run dependency of anything else (e.g. deploy.yml)."""
    data = _load_workflow(WORKFLOW_PATH)
    workflow_dir = WORKFLOW_PATH.parent
    for other in workflow_dir.glob("*.yml"):
        if other == WORKFLOW_PATH:
            continue
        other_data = _load_workflow(other)
        on_block = _on_block(other_data)
        if isinstance(on_block, dict) and "workflow_run" in on_block:
            workflows = on_block["workflow_run"].get("workflows", [])
            assert data["name"] not in workflows, other


def test_workflow_has_no_push_or_path_trigger_at_all():
    """Since the only trigger is workflow_dispatch with no `paths:` filter
    of any kind, no build/deploy pipeline path (docker-build.yml,
    tor-image-build.yml, deploy.yml push/workflow_run chains) can ever
    reach this file as a side effect of a normal push or PR."""
    data = _load_workflow(WORKFLOW_PATH)
    on_block = _on_block(data)
    assert on_block == {"workflow_dispatch": None}


# =====================================================================
# Real build/deploy trigger-path audit (Section 4 hardening): parses the
# actual repository workflow files rather than merely asserting in prose.
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


def test_docker_build_paths_do_not_match_any_phase_3_1a_changed_file():
    patterns = _push_paths(DOCKER_BUILD_PATH)
    for changed_file in PHASE_3_1A_CHANGED_FILES:
        for pattern in patterns:
            assert not _path_matches_filter(changed_file, pattern), (changed_file, pattern)


def test_tor_image_build_paths_do_not_match_any_phase_3_1a_changed_file():
    patterns = _push_paths(TOR_IMAGE_BUILD_PATH)
    for changed_file in PHASE_3_1A_CHANGED_FILES:
        for pattern in patterns:
            assert not _path_matches_filter(changed_file, pattern), (changed_file, pattern)


def test_docker_build_and_tor_image_build_have_push_paths_defined():
    """Sanity check that the audit above is actually exercising real
    filters and not vacuously passing against an empty list."""
    assert len(_push_paths(DOCKER_BUILD_PATH)) > 0
    assert len(_push_paths(TOR_IMAGE_BUILD_PATH)) > 0


def test_deploy_production_reacts_only_to_build_jobpulse_api_image():
    """Deploy Production's only *automatic* trigger must remain the
    completion of the workflow literally named "Build JobPulse API Image".
    `workflow_dispatch` (manual) may also be present -- that's an operator
    override, not an automatic reaction -- but no push/schedule/other
    workflow_run entry may exist."""
    data = _load_workflow(DEPLOY_PATH)
    on_block = _on_block(data)
    assert set(on_block.keys()) <= {"workflow_run", "workflow_dispatch"}
    assert "workflow_run" in on_block
    assert on_block["workflow_run"]["workflows"] == ["Build JobPulse API Image"]

    docker_build_data = _load_workflow(DOCKER_BUILD_PATH)
    assert docker_build_data["name"] == "Build JobPulse API Image"

    tor_image_build_data = _load_workflow(TOR_IMAGE_BUILD_PATH)
    assert tor_image_build_data["name"] != "Build JobPulse API Image"

    diagnostic_data = _load_workflow(WORKFLOW_PATH)
    assert diagnostic_data["name"] != "Build JobPulse API Image"


def test_ci_yml_runs_the_phase_3_1a_test_file():
    """Section 1: proves tests/test_production_runtime_diagnostic.py is
    now part of ordinary deterministic CI, not left unexercised."""
    source = CI_PATH.read_text()
    assert "tests/test_production_runtime_diagnostic.py" in source


def test_ci_yml_real_tor_gate_unchanged():
    """This hardening pass must not touch the existing manual gating of
    the real-Tor network job."""
    source = CI_PATH.read_text()
    assert "github.event_name == 'workflow_dispatch' && inputs.run_real_tor == true" in source


# =====================================================================
# bash -n syntax sanity for the embedded remote script
# =====================================================================
def test_remote_heredoc_passes_bash_syntax_check(tmp_path):
    import subprocess

    source = WORKFLOW_PATH.read_text()
    remote_match = re.search(r"<<'REMOTE'[^\n]*\n(.*?)\n\s*REMOTE\n", source, re.DOTALL)
    assert remote_match

    script_path = tmp_path / "remote.sh"
    script_path.write_text(remote_match.group(1))
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_local_step_passes_bash_syntax_check(tmp_path):
    """Sanity-check the outer (runner-side) script too, with the embedded
    REMOTE heredoc's contents blanked out (it targets a different bash
    invocation over ssh and would otherwise be parsed twice)."""
    import subprocess

    source = WORKFLOW_PATH.read_text()
    diag_step = re.search(
        r"- name: Run read-only production runtime diagnostic.*?\n(\s+run: \|\n)(.*)",
        source, re.DOTALL,
    )
    assert diag_step
    block = diag_step.group(2)
    lines = block.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip(" "))
    script_lines = [l[indent:] if l.startswith(" " * indent) else l for l in lines]
    script = "\n".join(script_lines)

    script_path = tmp_path / "runner.sh"
    script_path.write_text(script)
    result = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
