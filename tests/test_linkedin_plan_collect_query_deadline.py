"""Tests for the per-query subprocess deadline in
scripts/linkedin_plan_collect.py::run_single_query() (Phase 3.4K
Stabilization, Section 7) -- proves a wedged scripts.collector_postgres
subprocess is bounded by scripts.run_with_deadline instead of being able
to hang the whole batch indefinitely:

  - the collector_postgres invocation is wrapped with
    `python -m scripts.run_with_deadline --seconds <N> --kill-after <M> --`
  - N/M come from app.config.get_linkedin_plan_collect_query_timeout_seconds()
    / get_linkedin_plan_collect_query_kill_after_seconds() -- reusing the
    existing deadline runner rather than inventing duplicate logic
  - a deadline expiry (non-zero returncode) is classified as an ordinary
    query failure through the existing classify_query_result() path, no
    new failure contract

No real subprocess, browser, or database is used: psycopg2.connect is
stubbed to fail fast (every helper that touches Postgres already degrades
gracefully on a connection error) and subprocess.run is stubbed to record
its argv and return a fake CompletedProcess.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.linkedin_plan_collect as lpc


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fail_db_connect(*args, **kwargs):
    raise RuntimeError("no database in this unit test")


def _sample_query():
    return {
        "category": "Search Demand",
        "keywords": "Backend Engineer",
        "location": "Berlin",
        "work_mode": "remote",
        "lookback_days": 7,
        "limit": 10,
        "max_pages": 1,
    }


def test_collector_postgres_invocation_is_wrapped_with_run_with_deadline(monkeypatch):
    monkeypatch.setattr(lpc.psycopg2, "connect", _fail_db_connect)
    monkeypatch.setenv("LINKEDIN_PLAN_COLLECT_QUERY_TIMEOUT_SECONDS", "123")
    monkeypatch.setenv("LINKEDIN_PLAN_COLLECT_QUERY_KILL_AFTER_SECONDS", "7")
    monkeypatch.setenv("LINKEDIN_QUERY_RETRY_COUNT", "0")

    captured_cmds = []

    def _fake_subprocess_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return _FakeCompletedProcess(returncode=1)

    monkeypatch.setattr(lpc.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(lpc, "classify_query_result", lambda returncode, result_path: {
        "query_status": "failed",
        "useful": False,
        "zero_yield": False,
        "failure_category": "non_zero_exit",
        "collector_result": None,
    })

    lpc.run_single_query(_sample_query(), index=1, total=1)

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]

    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "scripts.run_with_deadline"]
    assert "--seconds" in cmd
    assert cmd[cmd.index("--seconds") + 1] == "123"
    assert "--kill-after" in cmd
    assert cmd[cmd.index("--kill-after") + 1] == "7"
    assert cmd[-1] == "scripts.collector_postgres"
    assert cmd[-2] == "-m"
    assert cmd[-3] == sys.executable
    assert "--" in cmd


def test_deadline_uses_config_defaults_when_unset(monkeypatch):
    monkeypatch.setattr(lpc.psycopg2, "connect", _fail_db_connect)
    monkeypatch.delenv("LINKEDIN_PLAN_COLLECT_QUERY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LINKEDIN_PLAN_COLLECT_QUERY_KILL_AFTER_SECONDS", raising=False)
    monkeypatch.setenv("LINKEDIN_QUERY_RETRY_COUNT", "0")

    captured_cmds = []

    def _fake_subprocess_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return _FakeCompletedProcess(returncode=1)

    monkeypatch.setattr(lpc.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(lpc, "classify_query_result", lambda returncode, result_path: {
        "query_status": "failed",
        "useful": False,
        "zero_yield": False,
        "failure_category": "non_zero_exit",
        "collector_result": None,
    })

    lpc.run_single_query(_sample_query(), index=1, total=1)

    cmd = captured_cmds[0]
    assert cmd[cmd.index("--seconds") + 1] == "900"
    assert cmd[cmd.index("--kill-after") + 1] == "15"


def test_deadline_expiry_returncode_is_classified_as_ordinary_failure(monkeypatch):
    """A run_with_deadline TIMEOUT_EXIT_CODE (124) must flow through the
    same classify_query_result() failure path as any other non-zero
    collector_postgres exit -- no bespoke handling needed."""
    monkeypatch.setattr(lpc.psycopg2, "connect", _fail_db_connect)
    monkeypatch.setenv("LINKEDIN_QUERY_RETRY_COUNT", "0")

    def _fake_subprocess_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=124)  # run_with_deadline TIMEOUT_EXIT_CODE

    monkeypatch.setattr(lpc.subprocess, "run", _fake_subprocess_run)

    result = lpc.run_single_query(_sample_query(), index=1, total=1)

    assert result["success"] is False
    assert result["returncode"] == 124
    assert result["classification"]["query_status"] == "failed"
