"""
Regression tests for scripts/production_smoke_test.sh.

Root cause (public API keys): production has JOBPULSE_PUBLIC_API_KEYS
configured and /jobs* correctly requires it (app/api_security.py fails
closed on a missing/invalid key), but the smoke test sent protected
/jobs requests without any X-API-Key header. /api/health passed,
/api/jobs came back 401 invalid_api_key, and the smoke test reported a
false production failure -- the app was behaving correctly the whole
time.

Root cause (admin auth false negative): admin access is protected by
two independent layers -- nginx Basic Auth in front of the public
`/api/admin/` proxy location, and FastAPI's own X-Admin-Key check on
the loopback-only backend. The smoke test used to send X-Admin-Key
through the public proxy without Basic Auth; nginx correctly rejected
that request with 401 before it ever reached FastAPI, and the test
conflated the two layers into a single false failure. The script now
probes each layer on its own URL: an unauthenticated request through
${BASE_URL} must be rejected by nginx (401 + WWW-Authenticate: Basic),
and X-Admin-Key is tested directly against the loopback-only
${ADMIN_API_BASE_URL} backend, never through the public proxy.

These tests exercise the real script against fake `curl` and `docker`
binaries (no real network, Docker daemon, PostgreSQL, or production
host is touched) to prove: a usable public API key is selected from
JOBPULSE_PUBLIC_API_KEYS (trimmed, empty entries skipped); the script
fails clearly before any protected request if no usable key exists;
every protected /jobs request carries X-API-Key while health stays
unauthenticated; the unauthenticated-jobs-401-then-authenticated-200
sequence holds; the nginx Basic Auth and FastAPI X-Admin-Key admin
layers are checked independently and never conflated; secrets are
never printed; and the temporary output directory is always cleaned
up.
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

# FAKE_CURL_JOBS_MODE controls the /jobs (list[Job]) body shape:
#   empty (default)     -> [] -- the realistic "no matching active jobs" case
#   nonempty             -> a one-element array of objects
#   malformed             -> not valid JSON at all
#   top_level_object      -> a JSON object instead of an array
#   non_object_elements   -> an array whose elements are not objects
#
# FAKE_CURL_SEARCH_MODE controls the /jobs/search envelope body shape:
#   empty (default)  -> a structurally valid envelope with zero results
#   nonempty          -> a structurally valid envelope with one result
#   missing_key       -> envelope missing "total_pages"
#   results_not_list  -> "results" is a string instead of a list
#   malformed         -> not valid JSON at all
#
# FAKE_CURL_ADMIN_MODE controls the direct-backend authenticated
# admin/summary body shape (only used when ADMIN_KEY is present):
#   empty (default)  -> a structurally valid envelope with all 5 keys
#   missing_key       -> envelope missing "collection"
#   malformed         -> not valid JSON at all
#
# Admin auth has two independent layers, matched here the same way
# they are reached in production: the public nginx proxy location
# (BASE_URL, no 127.0.0.1 in the host) always 401s with a
# WWW-Authenticate: Basic header and never looks at X-Admin-Key --
# nginx rejects before FastAPI is ever reached. The loopback-only
# direct backend (ADMIN_API_BASE_URL, host contains 127.0.0.1) only
# enforces FastAPI's X-Admin-Key check and never involves Basic Auth.
WWW_AUTH_HEADER=""
case "$URL" in
  */health)
    status=200
    body='{"status":"ok","database":"connected"}'
    ;;
  */admin/summary*)
    if [[ "$URL" == *"127.0.0.1"* ]]; then
      if [[ -n "$ADMIN_KEY" ]]; then
        status=200
        case "${FAKE_CURL_ADMIN_MODE:-empty}" in
          empty) body='{"status":"ok","jobs":42,"demand_queue":[],"collection":{"pending":0},"top_searches_7d":[]}' ;;
          missing_key) body='{"status":"ok","jobs":42,"demand_queue":[],"top_searches_7d":[]}' ;;
          malformed) body='not valid json' ;;
          *) body='{"status":"ok","jobs":42,"demand_queue":[],"collection":{"pending":0},"top_searches_7d":[]}' ;;
        esac
      else
        status=401
        body='{"error":"invalid_admin_key"}'
      fi
    else
      status=401
      body='<html><head><title>401 Authorization Required</title></head><body>401 Authorization Required</body></html>'
      if [[ -z "${FAKE_CURL_OMIT_WWW_AUTH:-}" ]]; then
        WWW_AUTH_HEADER='WWW-Authenticate: Basic realm="JobPulse Admin"\r\n'
      fi
    fi
    ;;
  */jobs/search*)
    if [[ -z "$API_KEY" ]]; then
      status=401
      body='{"error":"invalid_api_key"}'
    else
      status=200
      case "${FAKE_CURL_SEARCH_MODE:-empty}" in
        empty) body='{"results":[],"count":0,"page":1,"limit":5,"total_pages":0}' ;;
        nonempty) body='{"results":[{"title":"Fake Job","quality_score":0.87}],"count":1,"page":1,"limit":5,"total_pages":1}' ;;
        missing_key) body='{"results":[],"count":0,"page":1,"limit":5}' ;;
        results_not_list) body='{"results":"nope","count":0,"page":1,"limit":5,"total_pages":0}' ;;
        malformed) body='not valid json' ;;
        *) body='{"results":[],"count":0,"page":1,"limit":5,"total_pages":0}' ;;
      esac
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
      case "${FAKE_CURL_JOBS_MODE:-empty}" in
        empty) body='[]' ;;
        nonempty) body='[{"title":"Fake Job","quality_score":0.87}]' ;;
        malformed) body='not valid json' ;;
        top_level_object) body='{"oops":"not an array"}' ;;
        non_object_elements) body='[1,2,3]' ;;
        *) body='[]' ;;
      esac
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
    if [[ -n "$WWW_AUTH_HEADER" ]]; then
      printf '%b' "$WWW_AUTH_HEADER"
    fi
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
    env["ADMIN_API_BASE_URL"] = "http://127.0.0.1:8000/api"
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
        ("jobs_search_endpoint", "/jobs/search?query=python%20backend%20remote&limit=5"),
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


# --- Admin auth: two independent layers, checked independently --------
#
# Root cause: nginx protects the public `/api/admin/` proxy location
# with HTTP Basic Auth; FastAPI independently protects `/api/admin/*`
# with X-Admin-Key. The old smoke test sent X-Admin-Key through the
# public proxy without Basic Auth, nginx correctly rejected it with
# 401 before FastAPI ever saw the request, and the test reported a
# false production failure. The script now probes each layer on its
# own URL: BASE_URL (public, proxied by nginx) for the Basic Auth
# layer, and ADMIN_API_BASE_URL (loopback-only, 127.0.0.1:8000) for
# FastAPI's X-Admin-Key layer -- never mixed.


def test_admin_proxy_basic_auth_required_returns_401_and_is_accepted(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_proxy_basic_auth_required http=401" in result.stdout

    calls = _call_log_lines(sandbox)
    proxy_calls = [c for c in calls if "URL=http://localhost/api/admin/summary" in c]
    assert proxy_calls, "expected a request to the public nginx admin proxy"


def test_admin_proxy_request_carries_no_admin_key_or_api_key(sandbox):
    """The nginx Basic Auth layer must be probed with nothing else
    attached -- no X-Admin-Key (that's FastAPI's job) and no
    X-API-Key (that's the public /jobs layer's job)."""
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = _call_log_lines(sandbox)
    proxy_calls = [c for c in calls if "URL=http://localhost/api/admin/summary" in c]
    assert proxy_calls
    assert all("ADMIN_KEY=<none>" in c for c in proxy_calls)
    assert all("API_KEY=<none>" in c for c in proxy_calls)


def test_admin_api_without_key_blocked_returns_401_via_direct_backend(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_api_without_key_blocked http=401" in result.stdout
    assert "PASS admin_api_without_key_blocked_is_valid_json" in result.stdout

    calls = _call_log_lines(sandbox)
    direct_calls = [
        c for c in calls
        if "URL=http://127.0.0.1:8000/api/admin/summary" in c and "ADMIN_KEY=<none>" in c
    ]
    assert direct_calls, "expected an unauthenticated request straight to the loopback backend"


def test_admin_api_with_key_returns_200_via_direct_backend(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_api_with_key http=200" in result.stdout

    calls = _call_log_lines(sandbox)
    direct_calls = [
        c for c in calls
        if "URL=http://127.0.0.1:8000/api/admin/summary" in c and "ADMIN_KEY=admin-secret-token" in c
    ]
    assert direct_calls
    # the direct admin backend request must never carry the public API key
    assert all("API_KEY=<none>" in c for c in direct_calls)


def test_admin_api_with_key_envelope_is_structurally_validated(sandbox):
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_api_with_key_envelope" in result.stdout


def test_admin_api_with_key_envelope_rejects_missing_required_key(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_ADMIN_MODE": "missing_key"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL admin_api_with_key_envelope" in combined
    assert "missing_keys=collection" in combined


def test_admin_api_with_key_envelope_rejects_malformed_json(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_ADMIN_MODE": "malformed"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL admin_api_with_key_envelope" in combined
    assert "invalid_json" in combined


def test_admin_env_file_is_loaded_for_admin_api_key(sandbox):
    """ADMIN_API_KEY only exists in .admin.env in this sandbox (not
    .env), so a successful admin_api_with_key check proves .admin.env
    was actually sourced."""
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_api_with_key" in result.stdout


def test_admin_proxy_basic_auth_required_fails_without_www_authenticate_header(sandbox):
    """A bare 401 through the public proxy is not proof the Basic Auth
    layer produced it -- the check must fail clearly if the
    WWW-Authenticate: Basic header is absent, rather than silently
    accepting any 401 as the nginx layer."""
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_OMIT_WWW_AUTH": "1"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL admin_proxy_basic_auth_required" in combined
    assert "missing_www_authenticate_basic_header" in combined


def test_temp_output_directory_is_removed_after_admin_envelope_failure(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_ADMIN_MODE": "malformed"})
    assert result.returncode != 0

    leftovers = list(sandbox["scratch_tmpdir"].glob("jobpulse_smoke_test.*"))
    assert leftovers == [], f"temp directory left behind after an admin envelope failure: {leftovers}"


def test_admin_api_base_url_override_is_used_for_direct_backend_checks(sandbox):
    result = run_smoke_test(sandbox, extra_env={"ADMIN_API_BASE_URL": "http://127.0.0.1:9000/api"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS admin_api_without_key_blocked http=401" in result.stdout
    assert "PASS admin_api_with_key http=200" in result.stdout

    calls = _call_log_lines(sandbox)
    overridden_calls = [c for c in calls if "URL=http://127.0.0.1:9000/api/admin/summary" in c]
    assert overridden_calls, "expected direct backend requests to use the overridden ADMIN_API_BASE_URL"
    default_port_calls = [c for c in calls if "URL=http://127.0.0.1:8000/api/admin/summary" in c]
    assert not default_port_calls, "the default ADMIN_API_BASE_URL must not be used once overridden"


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


# --- Data-independent /jobs and /jobs/search validation -------------------
#
# Root cause: GET /jobs has response_model=list[Job] and legitimately
# returns [] when the selected query has no matching active jobs -- that
# is real, healthy production behavior, not a failure. The smoke test
# previously grepped the /jobs body for "title" and "quality_score",
# which only exist when a row happens to match, so a query with zero
# matches produced a false FAIL against a perfectly healthy production.
# GET /jobs/search has a stable envelope (results, count, page, limit,
# total_pages) regardless of match count, so it is the endpoint used to
# probe structural/contract stability; /jobs itself is only checked for
# being a JSON array whose elements (if any) are objects, never for row
# content.
#
# FAKE_CURL_JOBS_MODE/FAKE_CURL_SEARCH_MODE (see FAKE_CURL above) drive
# the response bodies for these tests; the default for both is "empty",
# so the whole suite's default sandbox run already proves the zero-match
# case works end to end without any test depending on a production row
# actually existing.


def test_full_smoke_run_succeeds_with_zero_matching_jobs(sandbox):
    """The sandbox's default FAKE_CURL_JOBS_MODE/FAKE_CURL_SEARCH_MODE
    are both "empty" -- this is the exact production scenario (a healthy
    deploy where the smoke test's query happens to match nothing) that
    previously produced a false failure."""
    result = run_smoke_test(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS jobs_search_is_valid_json_array" in result.stdout
    assert "PASS jobs_search_endpoint_envelope" in result.stdout
    assert "production_smoke_test_finished status=OK" in result.stdout


def test_jobs_array_accepts_nonempty_list_of_objects(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_JOBS_MODE": "nonempty"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS jobs_search_is_valid_json_array" in result.stdout


def test_jobs_array_rejects_malformed_json(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_JOBS_MODE": "malformed"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL jobs_search_is_valid_json_array" in combined
    assert "invalid_json" in combined


def test_jobs_array_rejects_top_level_object(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_JOBS_MODE": "top_level_object"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL jobs_search_is_valid_json_array" in combined
    assert "expected_top_level_array" in combined


def test_jobs_array_rejects_non_object_elements(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_JOBS_MODE": "non_object_elements"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL jobs_search_is_valid_json_array" in combined
    assert "not_an_object" in combined


def test_jobs_search_envelope_accepts_empty_results(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_SEARCH_MODE": "empty"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS jobs_search_endpoint_envelope" in result.stdout


def test_jobs_search_envelope_accepts_nonempty_results(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_SEARCH_MODE": "nonempty"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS jobs_search_endpoint_envelope" in result.stdout


def test_jobs_search_envelope_rejects_missing_required_key(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_SEARCH_MODE": "missing_key"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL jobs_search_endpoint_envelope" in combined
    assert "missing_keys=total_pages" in combined


def test_jobs_search_envelope_rejects_results_not_a_list(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_SEARCH_MODE": "results_not_list"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL jobs_search_endpoint_envelope" in combined
    assert "results_not_a_list" in combined


def test_jobs_search_envelope_rejects_malformed_json(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_SEARCH_MODE": "malformed"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "FAIL jobs_search_endpoint_envelope" in combined
    assert "invalid_json" in combined


def test_secrets_never_appear_in_output_regardless_of_jobs_data_shape(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_JOBS_MODE": "nonempty"})
    assert result.returncode == 0, result.stdout + result.stderr

    combined = result.stdout + result.stderr
    assert "first-public-key" not in combined
    assert "admin-secret-token" not in combined

    calls = _call_log_lines(sandbox)
    assert any("API_KEY=first-public-key" in c for c in calls), "sanity: the key really was used"


def test_temp_output_directory_is_removed_after_a_json_validation_failure(sandbox):
    result = run_smoke_test(sandbox, extra_env={"FAKE_CURL_JOBS_MODE": "malformed"})
    assert result.returncode != 0

    leftovers = list(sandbox["scratch_tmpdir"].glob("jobpulse_smoke_test.*"))
    assert leftovers == [], f"temp directory left behind after a JSON validation failure: {leftovers}"
