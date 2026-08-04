"""
Regression tests for scripts/production_smoke_test.sh.

Root cause: production has JOBPULSE_PUBLIC_API_KEYS configured and
/jobs* correctly requires it (app/api_security.py fails closed on a
missing/invalid key), but the smoke test sent protected /jobs requests
without any X-API-Key header. /api/health passed, /api/jobs came back
401 invalid_api_key, and the smoke test reported a false production
failure -- the app was behaving correctly the whole time.

These tests exercise the real script against fake `curl` and `docker`
binaries (no real network, Docker daemon, PostgreSQL, or production
host is touched) to prove: a usable public API key is selected from
JOBPULSE_PUBLIC_API_KEYS (trimmed, empty entries skipped); the script
fails clearly before any protected request if no usable key exists;
every protected /jobs request carries X-API-Key while health stays
unauthenticated; the unauthenticated-jobs-401-then-authenticated-200
sequence holds; admin checks are unaffected; secrets are never printed;
and the temporary output directory is always cleaned up.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "production_smoke_test.sh"

FAKE_CURL = r"""#!/usr/bin/env bash
# Fake curl for tests: understands just enough of the flags
# production_smoke_test.sh actually uses (-sS, -i, -o FILE, -w FMT,
# -H "X-API-Key: ..."/"X-Admin-Key: ...", and a trailing URL). No real
# network request is ever made.
set -u

OUT_FILE=""
INCLUDE_HEADERS=0
API_KEY=""
ADMIN_KEY=""
URL=""

args=("$@")
n=${#args[@]}
i=0
while [[ $i -lt $n ]]; do
  arg="${args[$i]}"
  case "$arg" in
    -sS|-s|-S) ;;
    -i) INCLUDE_HEADERS=1 ;;
    -o) i=$((i + 1)); OUT_FILE="${args[$i]}" ;;
    -w) i=$((i + 1)) ;;
    -H)
      i=$((i + 1))
      header="${args[$i]}"
      case "$header" in
        X-API-Key:*) API_KEY="${header#X-API-Key: }" ;;
        X-Admin-Key:*) ADMIN_KEY="${header#X-Admin-Key: }" ;;
      esac
      ;;
    http://*|https://*) URL="$arg" ;;
    *) ;;
  esac
  i=$((i + 1))
done

if [[ -n "${FAKE_CURL_CALL_LOG:-}" ]]; then
  printf 'URL=%s API_KEY=%s ADMIN_KEY=%s HEADERS=%s\n' \
    "$URL" "${API_KEY:-<none>}" "${ADMIN_KEY:-<none>}" "$INCLUDE_HEADERS" >> "$FAKE_CURL_CALL_LOG"
fi

status=500
body='{}'

case "$URL" in
  */health)
    status=200
    body='{"status":"ok","database":"connected"}'
    ;;
  */admin/summary*)
    if [[ -n "$ADMIN_KEY" ]]; then
      status=200
      body='{"jobs":42}'
    else
      status=401
      body='{"error":"invalid_admin_key"}'
    fi
    ;;
  */jobs*)
    if [[ -z "$API_KEY" ]]; then
      status=401
      body='{"error":"invalid_api_key"}'
    elif [[ "$URL" == *"limit=999"* ]]; then
      status=400
      body='{"error":"limit must be between 1 and 100"}'
    elif [[ "$URL" =~ a{100,} ]]; then
      status=400
      body='{"error":"query too long"}'
    else
      status=200
      body='{"results":[{"title":"Fake Job","quality_score":0.87}]}'
    fi
    ;;
  *)
    status=404
    body='{"error":"not_found"}'
    ;;
esac

if [[ "$INCLUDE_HEADERS" -eq 1 ]]; then
  {
    printf 'HTTP/1.1 %s OK\r\n' "$status"
    printf 'Content-Type: application/json\r\n'
    printf 'X-JobPulse-Cache: MISS\r\n'
    printf '\r\n'
    printf '%s' "$body"
  } > "$OUT_FILE"
else
  printf '%s' "$body" > "$OUT_FILE"
fi

printf '%s' "$status"
"""

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker CLI for tests: only understands the `compose -f FILE ...`
# invocations production_smoke_test.sh actually issues. No real
# container, daemon, or database is touched.
set -u

if [[ "${1:-}" != "compose" ]]; then
  echo "fake-docker: unsupported invocation: $*" >&2
  exit 99
fi
shift
[[ "${1:-}" == "-f" ]] && { shift; shift; }

case "${1:-}" in
  ps)
    echo "fake-docker ps: jobpulse-api-prod running"
    exit 0
    ;;
  exec)
    shift
    [[ "${1:-}" == "-T" ]] && shift
    shift # service name
    cmd="${1:-}"
    case "$cmd" in
      psql)
        echo "42"
        exit 0
        ;;
      python)
        echo "fake-docker exec: $*"
        exit 0
        ;;
      *)
        echo "fake-docker: unsupported exec command: $*" >&2
        exit 99
        ;;
    esac
    ;;
  *)
    echo "fake-docker: unsupported compose subcommand: $*" >&2
    exit 99
    ;;
esac
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sandbox(tmp_path):
    """A disposable sandbox: fake `curl`/`docker` on PATH, an isolated
    PROJECT_DIR with its own .api_keys.env/.admin.env/.env, and a
    scratch TMPDIR so leftover temp files are easy to detect."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", FAKE_CURL)
    _write_executable(bin_dir / "docker", FAKE_DOCKER)

    project_dir = tmp_path / "opt_jobpulse"
    project_dir.mkdir()
    (project_dir / "logs").mkdir()

    (project_dir / ".api_keys.env").write_text(
        'JOBPULSE_PUBLIC_API_KEYS="  first-public-key  , , second-public-key  "\n'
    )
    (project_dir / ".admin.env").write_text('ADMIN_API_KEY="admin-secret-token"\n')
    (project_dir / ".env").write_text("")

    scratch_tmpdir = tmp_path / "script_tmp"
    scratch_tmpdir.mkdir()

    call_log = tmp_path / "curl_calls.log"

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["PROJECT_DIR"] = str(project_dir)
    env["COMPOSE_FILE"] = "docker-compose.prod.yml"
    env["BASE_URL"] = "http://localhost/api"
    env["TMPDIR"] = str(scratch_tmpdir)
    env["FAKE_CURL_CALL_LOG"] = str(call_log)

    return {
        "env": env,
        "project_dir": project_dir,
        "scratch_tmpdir": scratch_tmpdir,
        "call_log": call_log,
    }


def run_smoke_test(sandbox, extra_env=None):
    env = dict(sandbox["env"])
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _call_log_lines(sandbox):
    if not sandbox["call_log"].exists():
        return []
    return [line for line in sandbox["call_log"].read_text().splitlines() if line]


def test_shell_script_passes_bash_syntax_check():
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_full_run_succeeds_with_a_configured_public_key(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "production_smoke_test_finished status=OK" in result.stdout


def test_public_api_key_is_selected_from_configured_list(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    jobs_search_calls = [c for c in calls if "/jobs?query=python%20backend%20remote&limit=5" in c]
    authenticated_calls = [c for c in jobs_search_calls if "API_KEY=<none>" not in c]
    assert authenticated_calls, "expected an authenticated jobs search call"
    # whitespace/empty entries in JOBPULSE_PUBLIC_API_KEYS must be ignored
    # and the first usable key selected -- never the second one.
    assert all("API_KEY=first-public-key" in c for c in authenticated_calls)
    assert all("second-public-key" not in c for c in calls)


def test_health_request_carries_no_api_key_header(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    health_calls = [c for c in calls if "URL=http://localhost/api/health" in c]
    assert health_calls, "expected a health check call"
    assert all("API_KEY=<none>" in c for c in health_calls)


def test_unauthenticated_jobs_probe_runs_before_authenticated_one_and_expects_401(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS jobs_without_key_blocked http=401" in result.stdout
    assert "PASS jobs_search http=200" in result.stdout
    assert result.stdout.index("jobs_without_key_blocked") < result.stdout.index("PASS jobs_search")

    calls = _call_log_lines(sandbox)
    unauth_calls = [
        c for c in calls
        if "/jobs?query=python%20backend%20remote&limit=5" in c and "API_KEY=<none>" in c
    ]
    assert unauth_calls, "the first jobs probe must be sent with no API key"


@pytest.mark.parametrize(
    "check_name,url_fragment",
    [
        ("jobs_search", "/jobs?query=python%20backend%20remote&limit=5"),
        ("api_guard_limit_validation", "limit=999"),
        ("api_guard_query_length_validation", "aaaaaaaaaa"),
    ],
)
def test_protected_jobs_requests_include_api_key(sandbox, check_name, url_fragment):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    matching = [c for c in calls if url_fragment in c and "/jobs" in c]
    assert matching, f"expected at least one /jobs call containing {url_fragment!r}"
    authenticated = [c for c in matching if "API_KEY=<none>" not in c]
    assert authenticated, f"no /jobs call for {check_name} carried the selected API key"
    assert all("API_KEY=first-public-key" in c for c in authenticated)


def test_validation_checks_still_expect_400(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS api_guard_limit_validation http=400" in result.stdout
    assert "PASS api_guard_query_length_validation http=400" in result.stdout


def test_cache_requests_are_authenticated_and_expect_200(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    cache_calls = [c for c in calls if "HEADERS=1" in c and "/jobs" in c]
    assert len(cache_calls) == 2, "expected exactly two header-inspecting cache calls"
    assert all("API_KEY=first-public-key" in c for c in cache_calls)


def test_admin_without_key_still_expects_401(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_without_key_blocked http=401" in result.stdout

    calls = _call_log_lines(sandbox)
    admin_calls = [c for c in calls if "/admin/summary" in c and "ADMIN_KEY=<none>" in c]
    assert admin_calls


def test_admin_with_key_still_uses_x_admin_key_header(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_with_key" in result.stdout

    calls = _call_log_lines(sandbox)
    admin_calls = [c for c in calls if "/admin/summary" in c and "ADMIN_KEY=admin-secret-token" in c]
    assert admin_calls
    # the admin path must never receive the public API key header
    assert all("API_KEY=<none>" in c for c in admin_calls)


def test_admin_env_file_is_loaded_for_admin_api_key(sandbox):
    """ADMIN_API_KEY only exists in .admin.env in this sandbox (not
    .env), so a successful admin_with_key check proves .admin.env was
    actually sourced."""
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_with_key" in result.stdout


def test_missing_public_api_key_fails_before_any_protected_request(sandbox):
    (sandbox["project_dir"] / ".api_keys.env").write_text('JOBPULSE_PUBLIC_API_KEYS=" , ,  "\n')

    result = run_smoke_test(sandbox)
    assert result.returncode != 0
    assert "no usable public API key" in result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    assert not calls, "no curl call (protected or not) should happen once key selection fails"


def test_missing_public_api_key_env_var_entirely_fails_clearly(sandbox):
    (sandbox["project_dir"] / ".api_keys.env").write_text("")

    result = run_smoke_test(sandbox)
    assert result.returncode != 0
    assert "no usable public API key" in result.stdout + result.stderr


def test_public_and_admin_keys_never_appear_in_output(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    combined = result.stdout + result.stderr
    assert "first-public-key" not in combined
    assert "second-public-key" not in combined
    assert "admin-secret-token" not in combined

    log_file = sandbox["project_dir"] / "logs" / "production_smoke_test.log"
    log_text = log_file.read_text()
    assert "first-public-key" not in log_text
    assert "second-public-key" not in log_text
    assert "admin-secret-token" not in log_text


def test_temp_output_directory_is_removed_on_success(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    leftovers = list(sandbox["scratch_tmpdir"].glob("jobpulse_smoke_test.*"))
    assert leftovers == [], f"temp directory left behind: {leftovers}"


def test_temp_output_directory_is_removed_on_failure(sandbox):
    (sandbox["project_dir"] / ".api_keys.env").write_text('JOBPULSE_PUBLIC_API_KEYS=""\n')

    result = run_smoke_test(sandbox)
    assert result.returncode != 0

    leftovers = list(sandbox["scratch_tmpdir"].glob("jobpulse_smoke_test.*"))
    assert leftovers == [], f"temp directory left behind after failure: {leftovers}"


def test_preexisting_hostile_file_at_old_fixed_tmp_paths_has_no_effect(sandbox):
    hostile_body = sandbox["scratch_tmpdir"] / "jobpulse_smoke_body.out"
    hostile_body.write_text("PRE-EXISTING HOSTILE CONTENT")
    hostile_headers = sandbox["scratch_tmpdir"] / "jobpulse_smoke_headers.out"
    hostile_headers.write_text("PRE-EXISTING HOSTILE CONTENT")

    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert hostile_body.read_text() == "PRE-EXISTING HOSTILE CONTENT"
    assert hostile_headers.read_text() == "PRE-EXISTING HOSTILE CONTENT"


def test_env_files_loaded_in_compose_precedence_order(sandbox):
    """Later env_file entries override earlier ones in
    docker-compose.prod.yml (.api_keys.env, then .admin.env, then
    .env) -- the script must source them in the same order so a value
    re-declared in a later file wins, matching runtime behavior."""
    (sandbox["project_dir"] / ".api_keys.env").write_text(
        'JOBPULSE_PUBLIC_API_KEYS="from-api-keys-env"\n'
    )
    (sandbox["project_dir"] / ".env").write_text(
        'JOBPULSE_PUBLIC_API_KEYS="from-dot-env"\n'
    )

    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    jobs_search_calls = [c for c in calls if "/jobs?query=python%20backend%20remote&limit=5" in c]
    authenticated_calls = [c for c in jobs_search_calls if "API_KEY=<none>" not in c]
    assert authenticated_calls
    assert all("API_KEY=from-dot-env" in c for c in authenticated_calls)
