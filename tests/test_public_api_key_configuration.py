"""
Regression tests for the fresh-deployment public API key configuration gap.

Root cause: nothing in .env.example, .env.prod.example,
scripts/check_production_readiness.py, README.md, or docs/PRODUCTION_RUNBOOK.md
told an operator that JOBPULSE_PUBLIC_API_KEYS must be set. A fresh, fully
documented deployment would start successfully and pass /health, but every
protected /jobs* request would fail closed with 503 api_key_not_configured
(app/api_security.py's fail-closed behavior was already correct and secure --
this was a configuration/tooling/documentation gap, not a code defect).

These tests cover: the runtime middleware behavior (already correct, verified
here as a baseline), the new production-readiness check, the new
non-blocking production startup warning, and that the example env files and
readiness check now consistently document/enforce the requirement.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.api_security as api_security_module
import app.main as main_module

REPO_ROOT = Path(__file__).resolve().parent.parent

FAKE_SEARCH_RESULT = {
    "results": [],
    "count": 0,
    "page": 1,
    "limit": 10,
    "total_pages": 0,
}


@pytest.fixture(autouse=True)
def reset_middleware_state():
    main_module.rate_limit_store.clear()
    api_security_module._rate_buckets.clear()
    main_module.app.middleware_stack = None
    yield
    main_module.rate_limit_store.clear()
    api_security_module._rate_buckets.clear()
    main_module.app.middleware_stack = None


@pytest.fixture
def client():
    return TestClient(main_module.app)


def _search(client, headers=None):
    with patch.object(main_module, "search_jobs_from_db", return_value=FAKE_SEARCH_RESULT):
        return client.get("/jobs/search", params={"query": "engineer"}, headers=headers or {})


# --- Runtime middleware behavior (baseline: already correct, fail-closed) ---


def test_missing_configuration_fails_closed_with_503(client, monkeypatch):
    monkeypatch.delenv("JOBPULSE_PUBLIC_API_KEYS", raising=False)

    response = _search(client)

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "api_key_not_configured"
    assert "JOBPULSE_PUBLIC_API_KEYS" in body["message"]


def test_one_configured_key_works(client, monkeypatch):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "only-key")

    response = _search(client, {"X-API-Key": "only-key"})

    assert response.status_code == 200


def test_multiple_configured_keys_all_work(client, monkeypatch):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "key-one,key-two,key-three")

    for key in ("key-one", "key-two", "key-three"):
        response = _search(client, {"X-API-Key": key})
        assert response.status_code == 200, key


def test_missing_key_header_is_rejected(client, monkeypatch):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "configured-key")

    response = _search(client)

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_api_key"


def test_invalid_key_is_rejected(client, monkeypatch):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "configured-key")

    response = _search(client, {"X-API-Key": "wrong-key"})

    assert response.status_code == 401


def test_whitespace_and_empty_entries_are_ignored_safely(client, monkeypatch):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "  key-a , , key-b ,   ")

    ok_a = _search(client, {"X-API-Key": "key-a"})
    ok_b = _search(client, {"X-API-Key": "key-b"})
    empty_entry_is_not_a_valid_key = _search(client, {"X-API-Key": ""})
    literal_whitespace_is_not_a_valid_key = _search(client, {"X-API-Key": "   "})

    assert ok_a.status_code == 200
    assert ok_b.status_code == 200
    assert empty_entry_is_not_a_valid_key.status_code == 401
    assert literal_whitespace_is_not_a_valid_key.status_code == 401


def test_only_commas_is_treated_as_unconfigured(client, monkeypatch):
    """A malformed value that parses to zero usable keys must fail closed,
    the same as a completely missing value -- never an open bypass."""
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", " , , ,")

    response = _search(client, {"X-API-Key": "anything"})

    assert response.status_code == 503
    assert response.json()["error"] == "api_key_not_configured"


def test_no_secret_value_is_exposed_in_error_responses(client, monkeypatch):
    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "super-secret-real-key")

    wrong_key_response = _search(client, {"X-API-Key": "attempted-wrong-key"})
    assert wrong_key_response.status_code == 401
    assert "super-secret-real-key" not in wrong_key_response.text
    assert "attempted-wrong-key" not in wrong_key_response.text

    monkeypatch.delenv("JOBPULSE_PUBLIC_API_KEYS", raising=False)
    unconfigured_response = _search(client)
    assert "super-secret-real-key" not in unconfigured_response.text


# --- Non-blocking production startup warning (subprocess: isolates the
# module-level import-time check from the shared app.main singleton used by
# every other test in this suite) ---


def _run_import_app_main(env_overrides, unset=()):
    env = dict(os.environ)
    env.update(env_overrides)
    for key in unset:
        env.pop(key, None)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", "import app.main; print('IMPORT_OK')"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_production_startup_without_keys_logs_warning_but_still_starts():
    result = _run_import_app_main(
        {"APP_ENV": "production", "JOB_PROVIDER": "json"},
        unset=["JOBPULSE_PUBLIC_API_KEYS"],
    )

    assert result.returncode == 0
    assert "IMPORT_OK" in result.stdout
    assert "JOBPULSE_PUBLIC_API_KEYS is not set" in result.stderr


def test_production_startup_with_key_configured_is_silent():
    result = _run_import_app_main(
        {
            "APP_ENV": "production",
            "JOB_PROVIDER": "json",
            "JOBPULSE_PUBLIC_API_KEYS": "prod-key-value",
        }
    )

    assert result.returncode == 0
    assert "JOBPULSE_PUBLIC_API_KEYS is not set" not in result.stderr
    assert "prod-key-value" not in result.stderr
    assert "prod-key-value" not in result.stdout


def test_development_startup_without_keys_does_not_warn():
    result = _run_import_app_main(
        {"APP_ENV": "development", "JOB_PROVIDER": "json"},
        unset=["JOBPULSE_PUBLIC_API_KEYS"],
    )

    assert result.returncode == 0
    assert "JOBPULSE_PUBLIC_API_KEYS is not set" not in result.stderr


# --- Production readiness check ---


def test_readiness_check_fails_when_key_missing(monkeypatch, capsys):
    from scripts.check_production_readiness import check_public_api_key

    monkeypatch.delenv("JOBPULSE_PUBLIC_API_KEYS", raising=False)

    assert check_public_api_key() is False
    assert "JOBPULSE_PUBLIC_API_KEYS" in capsys.readouterr().out


def test_readiness_check_fails_when_key_is_only_commas(monkeypatch, capsys):
    from scripts.check_production_readiness import check_public_api_key

    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", " , , ,")

    assert check_public_api_key() is False


def test_readiness_check_passes_with_a_single_key(monkeypatch, capsys):
    from scripts.check_production_readiness import check_public_api_key

    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "only-key")

    assert check_public_api_key() is True
    output = capsys.readouterr().out
    assert "1 key(s)" in output
    assert "only-key" not in output


def test_readiness_check_passes_when_key_configured(monkeypatch, capsys):
    from scripts.check_production_readiness import check_public_api_key

    monkeypatch.setenv("JOBPULSE_PUBLIC_API_KEYS", "key-a,key-b")

    assert check_public_api_key() is True
    output = capsys.readouterr().out
    assert "2 key(s)" in output
    # the readiness check must never print the raw secret values
    assert "key-a" not in output
    assert "key-b" not in output


# --- Example env files document the required setting ---


def test_env_example_has_empty_active_assignment():
    text = (REPO_ROOT / ".env.example").read_text()
    assert "JOBPULSE_PUBLIC_API_KEYS" in text

    active_assignments = re.findall(r"^JOBPULSE_PUBLIC_API_KEYS=(.*)$", text, re.MULTILINE)
    assert active_assignments == [""], (
        ".env.example must ship an empty JOBPULSE_PUBLIC_API_KEYS= value, "
        "not a predictable working key"
    )


def test_env_prod_example_documents_but_does_not_activate_public_api_keys():
    text = (REPO_ROOT / ".env.prod.example").read_text()
    assert "JOBPULSE_PUBLIC_API_KEYS" in text

    # No active (uncommented) assignment anywhere in the file: this value
    # must live only in /opt/jobpulse/.api_keys.env, never in .env, because
    # docker-compose.prod.yml loads .api_keys.env, then .admin.env, then
    # .env last -- a value here would silently override the real secret.
    active_assignments = [
        line for line in text.splitlines()
        if re.match(r"^JOBPULSE_PUBLIC_API_KEYS=", line)
    ]
    assert active_assignments == [], (
        f".env.prod.example must not activate JOBPULSE_PUBLIC_API_KEYS directly, "
        f"found: {active_assignments}"
    )

    assert "/opt/jobpulse/.api_keys.env" in text


def test_env_prod_example_api_key_line_is_commented_out():
    text = (REPO_ROOT / ".env.prod.example").read_text()
    commented_examples = [
        line for line in text.splitlines()
        if re.match(r"^\s*#\s*JOBPULSE_PUBLIC_API_KEYS=", line)
    ]
    assert commented_examples, "expected a commented-out JOBPULSE_PUBLIC_API_KEYS= example"


# --- API_KEY legacy variable comment accuracy ---


def test_env_example_api_key_comment_does_not_claim_it_is_unused():
    text = (REPO_ROOT / ".env.example").read_text()
    api_key_section_start = text.index("API_KEY=")
    comment_block = text[:api_key_section_start]
    # Take just the comment lines directly above the API_KEY= assignment.
    preceding_lines = [
        line for line in comment_block.splitlines()[-4:] if line.strip()
    ]
    preceding_comment = "\n".join(preceding_lines)

    assert "unused" not in preceding_comment.lower()
    assert "legacy" in preceding_comment.lower()
    assert "JOBPULSE_PUBLIC_API_KEYS" in preceding_comment


# --- Public helper rename: no private cross-module import ---


def test_public_helper_name_is_exported_and_private_alias_is_gone():
    assert hasattr(api_security_module, "get_configured_public_api_keys")
    assert not hasattr(api_security_module, "_configured_api_keys")


def test_main_and_readiness_check_use_the_renamed_helper():
    import scripts.check_production_readiness as readiness_module

    assert main_module.get_configured_public_api_keys is api_security_module.get_configured_public_api_keys
    assert (
        readiness_module.get_configured_public_api_keys
        is api_security_module.get_configured_public_api_keys
    )
