"""
Tests for phase 2A structured-result propagation across the subprocess
boundary in scripts/linkedin_plan_collect.py:

  - classify_query_result(): the required interpretation of
    (returncode, result file) pairs
  - build_batch_report(): truthful batch-level classification, including
    partial_failure visibility
  - run_single_query()'s temp result path lifecycle (created, passed via
    env, cleaned up)

No real Docker, PostgreSQL, Telegram, or LinkedIn is ever used:
subprocess.run and the DB helper functions are monkeypatched; the
collector itself is never actually invoked.
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.linkedin_plan_collect as lpc
import scripts.collector_result as cr


def make_result_dict(**overrides) -> dict:
    fields = dict(
        schema_version=cr.SCHEMA_VERSION,
        provider="TestProvider",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        duration_seconds=5.0,
        jobs_discovered=1,
        jobs_valid=1,
        jobs_filtered_invalid=0,
        jobs_filtered_non_linkedin=0,
        jobs_filtered_header_artifact=0,
        jobs_filtered_missing_identifier=0,
        rows_inserted=1,
        rows_updated_existing=0,
        persistence_errors=0,
        outcome=cr.OUTCOME_SUCCESS_WITH_NEW_ROWS,
        error_category=None,
        sanitized_error=None,
    )
    fields.update(overrides)
    return fields


def write_result(path: Path, **overrides):
    path.write_text(json.dumps(make_result_dict(**overrides)))


# =====================================================================
# classify_query_result()
# =====================================================================

def test_nonzero_exit_is_always_failure_even_with_success_looking_result(tmp_path):
    path = tmp_path / "r.json"
    write_result(path, outcome=cr.OUTCOME_SUCCESS_WITH_NEW_ROWS)
    classification = lpc.classify_query_result(1, path)
    assert classification["query_status"] == "failed"
    assert classification["useful"] is False


def test_nonzero_exit_uses_collector_error_category_when_available(tmp_path):
    path = tmp_path / "r.json"
    write_result(path, outcome=cr.OUTCOME_FAILED_PERSIST, error_category=cr.ERROR_CATEGORY_PERSIST, persistence_errors=1)
    classification = lpc.classify_query_result(1, path)
    assert classification["query_status"] == "failed"
    assert classification["failure_category"] == cr.ERROR_CATEGORY_PERSIST


def test_exit_zero_missing_result_file_is_failure_missing_result(tmp_path):
    path = tmp_path / "does_not_exist.json"
    classification = lpc.classify_query_result(0, path)
    assert classification["query_status"] == "failed"
    assert classification["failure_category"] == cr.ERROR_CATEGORY_MISSING_RESULT
    assert classification["collector_result"] is None


def test_exit_zero_malformed_result_is_failure_invalid_result(tmp_path):
    path = tmp_path / "r.json"
    path.write_text("{not valid json")
    classification = lpc.classify_query_result(0, path)
    assert classification["query_status"] == "failed"
    assert classification["failure_category"] == cr.ERROR_CATEGORY_INVALID_RESULT


@pytest.mark.parametrize("outcome", [
    cr.OUTCOME_SUCCESS_NO_RESULTS,
    cr.OUTCOME_SUCCESS_FILTERED_ALL,
    cr.OUTCOME_SUCCESS_NO_NEW_ROWS,
])
def test_exit_zero_zero_yield_outcomes_are_technical_success_not_useful(tmp_path, outcome):
    path = tmp_path / "r.json"
    if outcome == cr.OUTCOME_SUCCESS_NO_RESULTS:
        overrides = dict(jobs_discovered=0, jobs_valid=0, jobs_filtered_invalid=0,
                          jobs_filtered_non_linkedin=0, jobs_filtered_header_artifact=0,
                          jobs_filtered_missing_identifier=0, rows_inserted=0, rows_updated_existing=0)
    elif outcome == cr.OUTCOME_SUCCESS_FILTERED_ALL:
        overrides = dict(jobs_discovered=1, jobs_valid=1, jobs_filtered_invalid=0,
                          jobs_filtered_non_linkedin=0, jobs_filtered_header_artifact=1,
                          jobs_filtered_missing_identifier=0, rows_inserted=0, rows_updated_existing=0)
    else:  # success_no_new_rows
        overrides = dict(jobs_discovered=1, jobs_valid=1, jobs_filtered_invalid=0,
                          jobs_filtered_non_linkedin=0, jobs_filtered_header_artifact=0,
                          jobs_filtered_missing_identifier=0, rows_inserted=0, rows_updated_existing=1)

    write_result(path, outcome=outcome, **overrides)
    classification = lpc.classify_query_result(0, path)
    assert classification["query_status"] == "success"
    assert classification["useful"] is False
    assert classification["zero_yield"] is True


def test_exit_zero_success_with_new_rows_is_useful(tmp_path):
    path = tmp_path / "r.json"
    write_result(path, outcome=cr.OUTCOME_SUCCESS_WITH_NEW_ROWS)
    classification = lpc.classify_query_result(0, path)
    assert classification["query_status"] == "success"
    assert classification["useful"] is True
    assert classification["zero_yield"] is False


def test_exit_zero_but_collector_reports_failed_outcome_is_still_failure(tmp_path):
    """Success must never be inferred from returncode alone: even a clean
    exit 0 paired with a structurally-reported failure must be treated as
    a failure."""
    path = tmp_path / "r.json"
    write_result(path, outcome=cr.OUTCOME_FAILED_INTERNAL, error_category=cr.ERROR_CATEGORY_INTERNAL)
    classification = lpc.classify_query_result(0, path)
    assert classification["query_status"] == "failed"
    assert classification["failure_category"] == cr.ERROR_CATEGORY_INTERNAL


# =====================================================================
# build_batch_report()
# =====================================================================

def _result_entry(success, useful=False, zero_yield=False, collector_result=None, skipped=False):
    return {
        "success": success,
        "skipped": skipped,
        "classification": {
            "query_status": "success" if success else "failed",
            "useful": useful,
            "zero_yield": zero_yield,
            "collector_result": collector_result,
        },
    }


def test_batch_report_all_successful_is_not_partial_failure():
    results = [_result_entry(True, useful=True), _result_entry(True, zero_yield=True)]
    report = lpc.build_batch_report(results)
    assert report["total_queries"] == 2
    assert report["successful_queries"] == 2
    assert report["failed_queries"] == 0
    assert report["partial_failure"] is False


def test_batch_report_all_failed_is_not_partial_failure():
    results = [_result_entry(False), _result_entry(False)]
    report = lpc.build_batch_report(results)
    assert report["failed_queries"] == 2
    assert report["successful_queries"] == 0
    assert report["partial_failure"] is False


def test_batch_report_mixed_is_visibly_partial_failure():
    """At least one failed AND at least one successful query -> must be
    visibly classified as partial_failure, never silently described as
    an unqualified success."""
    results = [_result_entry(True, useful=True), _result_entry(False)]
    report = lpc.build_batch_report(results)
    assert report["successful_queries"] == 1
    assert report["failed_queries"] == 1
    assert report["partial_failure"] is True


def test_batch_report_distinguishes_useful_from_zero_yield():
    results = [
        _result_entry(True, useful=True),
        _result_entry(True, zero_yield=True),
        _result_entry(True, zero_yield=True),
    ]
    report = lpc.build_batch_report(results)
    assert report["useful_queries"] == 1
    assert report["zero_yield_queries"] == 2
    assert report["successful_queries"] == 3


def test_batch_report_aggregates_collector_counters():
    cr1 = make_result_dict(jobs_discovered=5, rows_inserted=2, rows_updated_existing=0)
    cr2 = make_result_dict(jobs_discovered=3, rows_inserted=0, rows_updated_existing=0, outcome=cr.OUTCOME_SUCCESS_FILTERED_ALL)
    results = [
        _result_entry(True, useful=True, collector_result=cr1),
        _result_entry(True, zero_yield=True, collector_result=cr2),
    ]
    report = lpc.build_batch_report(results)
    agg = report["aggregate_collector_metrics"]
    assert agg["jobs_discovered"] == 8
    assert agg["rows_inserted"] == 2
    assert agg["rows_updated_existing"] == 0


def test_batch_report_skips_none_collector_results_in_aggregate():
    results = [_result_entry(False, collector_result=None), _result_entry(True, useful=True, collector_result=make_result_dict())]
    report = lpc.build_batch_report(results)
    assert report["aggregate_collector_metrics"]["jobs_discovered"] == 1


# =====================================================================
# run_single_query(): temp result path lifecycle
# =====================================================================

@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """None of these tests may touch a real DB: every DB-facing helper
    used by run_single_query() is monkeypatched to a safe in-memory
    stand-in."""
    monkeypatch.setattr(lpc, "count_linkedin_jobs", lambda: 0)
    monkeypatch.setattr(lpc, "was_query_recently_successful", lambda **kw: False)
    monkeypatch.setattr(lpc, "insert_query_run_record", lambda record: None)
    monkeypatch.setattr(lpc, "get_retry_count", lambda: 0)
    monkeypatch.setattr(lpc, "get_retry_delay_seconds", lambda: 0)


def _query():
    return {
        "category": "backend",
        "keywords": "python",
        "location": "Berlin",
        "work_mode": "remote",
        "lookback_days": 7,
        "limit": 10,
        "max_pages": 1,
    }


def test_run_single_query_passes_unique_result_path_env_var(monkeypatch):
    seen_paths = []

    def fake_run(cmd, cwd, env, text, capture_output):
        result_path = env.get(cr.RESULT_PATH_ENV_VAR)
        seen_paths.append(result_path)
        assert result_path, "RESULT_PATH_ENV_VAR must be set for the collector subprocess"
        Path(result_path).write_text(json.dumps(make_result_dict(outcome=cr.OUTCOME_SUCCESS_NO_RESULTS, jobs_discovered=0, jobs_valid=0, rows_inserted=0, rows_updated_existing=0)))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(lpc.subprocess, "run", fake_run)

    result1 = lpc.run_single_query(_query(), 1, 2)
    result2 = lpc.run_single_query(_query(), 2, 2)

    assert len(seen_paths) == 2
    assert seen_paths[0] != seen_paths[1], "each subprocess invocation must get a unique result path"
    assert result1["classification"]["zero_yield"] is True


def test_run_single_query_cleans_up_temp_result_file(monkeypatch):
    captured = {}

    def fake_run(cmd, cwd, env, text, capture_output):
        result_path = env.get(cr.RESULT_PATH_ENV_VAR)
        captured["path"] = Path(result_path)
        captured["path"].write_text(json.dumps(make_result_dict()))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(lpc.subprocess, "run", fake_run)
    lpc.run_single_query(_query(), 1, 1)

    assert not captured["path"].exists(), "temp result file must be deleted after being read"


def test_run_single_query_cleans_up_temp_result_file_even_when_never_created(monkeypatch):
    """If the collector subprocess crashes before ever writing a result
    file, the temp path must still be cleaned up (nothing to delete, but
    no exception should propagate from the cleanup itself)."""
    def fake_run(cmd, cwd, env, text, capture_output):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(lpc.subprocess, "run", fake_run)
    result = lpc.run_single_query(_query(), 1, 1)
    assert result["classification"]["query_status"] == "failed"


def test_run_single_query_does_not_infer_success_from_stdout(monkeypatch):
    """A misleading, human-looking success message on stdout must never
    be treated as the source of truth -- only the structured result file
    (or its absence) matters."""
    def fake_run(cmd, cwd, env, text, capture_output):
        result_path = env.get(cr.RESULT_PATH_ENV_VAR)
        # Deliberately never write a result file, but print something
        # that looks like success on stdout.
        return subprocess.CompletedProcess(
            cmd, 0, stdout="LinkedIn PostgreSQL collector finished successfully.\nInserted or updated LinkedIn jobs: 5", stderr=""
        )

    monkeypatch.setattr(lpc.subprocess, "run", fake_run)
    result = lpc.run_single_query(_query(), 1, 1)

    assert result["success"] is False
    assert result["classification"]["failure_category"] == cr.ERROR_CATEGORY_MISSING_RESULT


def test_run_single_query_retries_on_zero_yield_classified_failure(monkeypatch):
    """Retry logic must key off the truthful classification, not the raw
    returncode: a returncode 0 with a missing result file must still
    count as a failed attempt eligible for retry."""
    monkeypatch.setattr(lpc, "get_retry_count", lambda: 1)
    monkeypatch.setattr(lpc, "get_retry_delay_seconds", lambda: 0)

    call_count = {"n": 0}

    def fake_run(cmd, cwd, env, text, capture_output):
        call_count["n"] += 1
        result_path = Path(env.get(cr.RESULT_PATH_ENV_VAR))
        if call_count["n"] == 1:
            # First attempt: exit 0 but never writes a result file.
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        result_path.write_text(json.dumps(make_result_dict()))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(lpc.subprocess, "run", fake_run)
    result = lpc.run_single_query(_query(), 1, 1)

    assert call_count["n"] == 2
    assert result["success"] is True


# =====================================================================
# write_batch_result_atomic() / read_batch_result() (item 8 transport)
# =====================================================================

def test_write_batch_result_atomic_noop_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(lpc.PLAN_COLLECT_RESULT_PATH_ENV_VAR, raising=False)
    lpc.write_batch_result_atomic({"total_queries": 0})  # must not raise


def test_write_then_read_batch_result_round_trips(tmp_path, monkeypatch):
    path = tmp_path / "batch_result.json"
    monkeypatch.setenv(lpc.PLAN_COLLECT_RESULT_PATH_ENV_VAR, str(path))

    batch_report = lpc.build_batch_report([_result_entry(True, useful=True, collector_result=make_result_dict())])
    lpc.write_batch_result_atomic(batch_report)

    assert path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".plan_collect_result.")]
    assert leftovers == []

    loaded = lpc.read_batch_result(path)
    assert loaded == batch_report


def test_read_batch_result_missing_file_is_missing_result(tmp_path):
    with pytest.raises(lpc.BatchResultReadError) as excinfo:
        lpc.read_batch_result(tmp_path / "nope.json")
    assert excinfo.value.category == cr.ERROR_CATEGORY_MISSING_RESULT


def test_read_batch_result_malformed_json_is_invalid_result(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    with pytest.raises(lpc.BatchResultReadError) as excinfo:
        lpc.read_batch_result(path)
    assert excinfo.value.category == cr.ERROR_CATEGORY_INVALID_RESULT


def test_read_batch_result_missing_batch_key_is_invalid_result(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema_version": lpc.PLAN_COLLECT_RESULT_SCHEMA_VERSION}))
    with pytest.raises(lpc.BatchResultReadError) as excinfo:
        lpc.read_batch_result(path)
    assert excinfo.value.category == cr.ERROR_CATEGORY_INVALID_RESULT


def test_read_batch_result_missing_required_keys_is_invalid_result(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        "schema_version": lpc.PLAN_COLLECT_RESULT_SCHEMA_VERSION,
        "batch": {"total_queries": 1},  # missing everything else
    }))
    with pytest.raises(lpc.BatchResultReadError) as excinfo:
        lpc.read_batch_result(path)
    assert excinfo.value.category == cr.ERROR_CATEGORY_INVALID_RESULT


def test_run_plan_collect_writes_batch_result_even_with_zero_matching_queries(tmp_path, monkeypatch):
    """The total==0 early-return path (no queries matched the filters)
    must still produce a batch result document -- a downstream reader
    must never see 'file missing' for a legitimately empty plan."""
    result_path = tmp_path / "batch_result.json"
    monkeypatch.setenv(lpc.PLAN_COLLECT_RESULT_PATH_ENV_VAR, str(result_path))
    monkeypatch.setattr(lpc, "get_queries_file", lambda: tmp_path / "queries.json")
    (tmp_path / "queries.json").write_text(json.dumps([]))

    report = lpc.run_plan_collect(workers=1, max_queries=None, offset=0, categories=None)

    assert result_path.exists()
    assert report["total_queries"] == 0
    loaded = lpc.read_batch_result(result_path)
    assert loaded["total_queries"] == 0
