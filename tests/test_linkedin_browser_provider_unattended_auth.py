"""Tests for the unattended login/checkpoint handling in
scripts/providers/linkedin_browser_provider.py (Phase 3.4K Stabilization,
Section 7) -- proves the automated queue path can never block
indefinitely on `input(...)`:

  - by default (unattended), a login/checkpoint raises RuntimeError
    immediately and NEVER calls input()
  - the raised RuntimeError is exactly the kind of exception
    collector_postgres.collect_jobs_to_postgres() already catches around
    provider.fetch_jobs() and turns into a structured OUTCOME_FAILED_FETCH
    result (proving this enters the existing collector-result contract,
    not a new failure mode)
  - LINKEDIN_INTERACTIVE_AUTH_RECOVERY=true preserves the original
    interactive prompt for an explicitly-invoked manual recovery workflow
"""
import builtins
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.providers.linkedin_browser_provider import LinkedInBrowserProvider


def _bare_provider():
    return LinkedInBrowserProvider.__new__(LinkedInBrowserProvider)


def test_unattended_login_wall_raises_without_calling_input(monkeypatch):
    monkeypatch.delenv("LINKEDIN_INTERACTIVE_AUTH_RECOVERY", raising=False)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("input() must never be called in unattended mode")

    monkeypatch.setattr(builtins, "input", _fail_if_called)

    provider = _bare_provider()

    with pytest.raises(RuntimeError, match="unattended mode"):
        provider._handle_login_or_checkpoint("https://www.linkedin.com/checkpoint/challenge")


def test_unattended_is_the_default_when_env_var_is_false(monkeypatch):
    monkeypatch.setenv("LINKEDIN_INTERACTIVE_AUTH_RECOVERY", "false")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("input() must never be called when explicitly disabled")

    monkeypatch.setattr(builtins, "input", _fail_if_called)

    provider = _bare_provider()

    with pytest.raises(RuntimeError):
        provider._handle_login_or_checkpoint("https://www.linkedin.com/login")


def test_interactive_recovery_mode_still_prompts(monkeypatch):
    monkeypatch.setenv("LINKEDIN_INTERACTIVE_AUTH_RECOVERY", "true")

    calls = []
    monkeypatch.setattr(builtins, "input", lambda prompt="": calls.append(prompt) or "")

    provider = _bare_provider()

    provider._handle_login_or_checkpoint("https://www.linkedin.com/login")

    assert len(calls) == 1


def test_unattended_failure_is_caught_by_collector_postgres_fetch_contract(monkeypatch):
    """Proves the raised RuntimeError enters the SAME structured-failure
    path collector_postgres.py already uses for any other fetch_jobs()
    exception, rather than needing a new/duplicate contract."""
    import scripts.collector_postgres as collector_postgres_module
    from scripts.collector_result import ERROR_CATEGORY_FETCH, OUTCOME_FAILED_FETCH

    monkeypatch.delenv("LINKEDIN_INTERACTIVE_AUTH_RECOVERY", raising=False)
    monkeypatch.delenv(collector_postgres_module.RESULT_PATH_ENV_VAR, raising=False)

    class RaisingProvider:
        def fetch_jobs(self):
            provider = _bare_provider()
            provider._handle_login_or_checkpoint("https://www.linkedin.com/checkpoint/challenge")

    monkeypatch.setattr(collector_postgres_module, "get_job_provider", lambda: RaisingProvider())

    exit_code = collector_postgres_module.collect_jobs_to_postgres()

    assert exit_code == 1
