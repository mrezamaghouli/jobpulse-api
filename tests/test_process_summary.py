"""
Tests for scripts/process_summary.py: the cycle-level structured summary
that closes the "always technical_success" gap in
scripts/run_collection_cycle_safe.sh's heartbeat finish call (phase 2A
adversarial pass, item 8).

No real Docker, PostgreSQL, or LinkedIn is ever used -- this module has
no I/O dependency on any of them; only local temp files.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.process_summary as ps


def agg(**overrides) -> dict:
    d = {field: 0 for field in ps._AGGREGATE_COUNTER_FIELDS}
    d.update(overrides)
    return d


def batch(**overrides) -> dict:
    d = dict(
        total_queries=1, successful_queries=1, failed_queries=0,
        useful_queries=0, zero_yield_queries=1, skipped_queries=0,
        partial_failure=False, aggregate_collector_metrics=agg(),
    )
    d.update(overrides)
    return d


# =====================================================================
# classify_cycle_outcome()
# =====================================================================

def test_no_pending_targets_is_technical_success_no_results():
    assert ps.classify_cycle_outcome(None, had_pending_targets=False) == ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS


def test_no_pending_targets_ignores_batch_report_entirely():
    """Even a batch_report claiming failure must not matter when there
    was nothing to search for in the first place."""
    b = batch(total_queries=0, successful_queries=0, failed_queries=0)
    assert ps.classify_cycle_outcome(b, had_pending_targets=False) == ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS


def test_missing_batch_report_with_pending_targets_is_failed():
    assert ps.classify_cycle_outcome(None, had_pending_targets=True) == ps.OUTCOME_FAILED


def test_zero_total_queries_with_pending_targets_is_failed():
    b = batch(total_queries=0, successful_queries=0, failed_queries=0)
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_FAILED


def test_zero_successful_queries_is_failed():
    b = batch(total_queries=3, successful_queries=0, failed_queries=3)
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_FAILED


def test_partial_failure_is_visible_not_folded_into_success():
    b = batch(total_queries=3, successful_queries=2, failed_queries=1, partial_failure=True,
              aggregate_collector_metrics=agg(rows_inserted=5))
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_PARTIAL_FAILURE


def test_all_succeeded_with_proven_insert_is_useful_success():
    b = batch(aggregate_collector_metrics=agg(rows_inserted=3, jobs_discovered=5))
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_USEFUL_SUCCESS


def test_all_succeeded_with_updates_but_no_inserts_is_no_new_rows():
    b = batch(aggregate_collector_metrics=agg(rows_updated_existing=4, jobs_discovered=4))
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS


def test_all_succeeded_with_nothing_discovered_is_no_results():
    b = batch(aggregate_collector_metrics=agg(jobs_discovered=0))
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS


def test_all_succeeded_with_discovered_but_all_filtered_is_filtered_all():
    b = batch(aggregate_collector_metrics=agg(jobs_discovered=5, jobs_filtered_header_artifact=5))
    assert ps.classify_cycle_outcome(b, had_pending_targets=True) == ps.OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL


def test_classify_is_a_deterministic_pure_function():
    b = batch(aggregate_collector_metrics=agg(rows_inserted=1))
    results = {ps.classify_cycle_outcome(b, True) for _ in range(5)}
    assert results == {ps.OUTCOME_USEFUL_SUCCESS}


# =====================================================================
# build_summary() / write_summary_atomic() / read_summary()
# =====================================================================

def test_build_summary_with_none_batch_report_and_no_pending_targets():
    summary = ps.build_summary(None, had_pending_targets=False)
    assert summary.outcome == ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS
    assert summary.had_pending_targets is False
    assert summary.total_queries == 0


def test_write_summary_atomic_produces_valid_json_no_partial_file(tmp_path):
    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(aggregate_collector_metrics=agg(rows_inserted=1)), True)
    ps.write_summary_atomic(path, summary)

    assert path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".process_summary.")]
    assert leftovers == []


def test_write_then_read_round_trips(tmp_path):
    path = tmp_path / "summary.json"
    summary = ps.build_summary(
        batch(aggregate_collector_metrics=agg(rows_inserted=2, jobs_valid=2, jobs_discovered=2)), True,
    )
    ps.write_summary_atomic(path, summary)
    loaded = ps.read_summary(path)
    assert loaded == summary
    assert loaded.outcome == ps.OUTCOME_USEFUL_SUCCESS


def test_read_summary_missing_file_is_missing_result(tmp_path):
    with pytest.raises(ps.SummaryReadError) as excinfo:
        ps.read_summary(tmp_path / "nope.json")
    assert excinfo.value.category == ps.ERROR_CATEGORY_MISSING_RESULT


def test_read_summary_directory_is_result_read_error(tmp_path):
    path = tmp_path / "a_dir"
    path.mkdir()
    with pytest.raises(ps.SummaryReadError) as excinfo:
        ps.read_summary(path)
    assert excinfo.value.category == ps.ERROR_CATEGORY_RESULT_READ_ERROR


def test_read_summary_malformed_json_is_invalid_result(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    with pytest.raises(ps.SummaryReadError) as excinfo:
        ps.read_summary(path)
    assert excinfo.value.category == ps.ERROR_CATEGORY_INVALID_RESULT


@pytest.mark.parametrize("field,value", [
    ("schema_version", 999),
    ("had_pending_targets", "true"),  # string, not bool
    ("partial_failure", 1),  # int, not bool
    ("outcome", "not_a_real_outcome"),
    ("total_queries", "5"),  # numeric string
    ("total_queries", True),  # bool, not int
    ("total_queries", -1),
])
def test_read_summary_rejects_malformed_fields(tmp_path, field, value):
    summary = ps.build_summary(batch(), True)
    doc = summary.to_dict()
    doc[field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ps.SummaryReadError) as excinfo:
        ps.read_summary(path)
    assert excinfo.value.category == ps.ERROR_CATEGORY_INVALID_RESULT


def test_read_summary_rejects_useful_success_without_proven_insert(tmp_path):
    summary = ps.build_summary(batch(aggregate_collector_metrics=agg(rows_inserted=1)), True)
    doc = summary.to_dict()
    doc["aggregate_collector_metrics"]["rows_inserted"] = 0  # contradicts outcome
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ps.SummaryReadError):
        ps.read_summary(path)


def test_read_summary_rejects_query_count_mismatch(tmp_path):
    summary = ps.build_summary(batch(), True)
    doc = summary.to_dict()
    doc["successful_queries"] = 99  # no longer sums to total_queries
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ps.SummaryReadError):
        ps.read_summary(path)


def test_read_summary_rejects_excessively_large_counters(tmp_path):
    summary = ps.build_summary(batch(), True)
    doc = summary.to_dict()
    doc["total_queries"] = ps.MAX_COUNTER_VALUE + 1
    doc["successful_queries"] = ps.MAX_COUNTER_VALUE + 1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ps.SummaryReadError):
        ps.read_summary(path)


# =====================================================================
# Full semantic validation (adversarial pass, item 2): read_summary()
# must recompute the canonical outcome via the SAME classify_cycle_outcome()
# the writer used, and reject any document whose persisted outcome
# doesn't match -- not merely check field types/ranges in isolation.
# =====================================================================

def _valid_doc(**overrides) -> dict:
    """A baseline internally-consistent, valid document (useful_success)
    that tests mutate to introduce exactly one contradiction at a time."""
    summary = ps.build_summary(
        batch(aggregate_collector_metrics=agg(rows_inserted=2, jobs_valid=2, jobs_discovered=2)), True,
    )
    doc = summary.to_dict()
    doc.update(overrides)
    return doc


def _assert_rejected(tmp_path, doc, name="bad.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    with pytest.raises(ps.SummaryReadError) as excinfo:
        ps.read_summary(path)
    return excinfo.value


def test_reject_successful_and_failed_with_partial_failure_false_but_useful_success(tmp_path):
    """successful=1, failed=1, partial_failure=false, outcome=useful_success
    -- contradictory on multiple levels (partial_failure should be true
    given successful>0 and failed>0)."""
    doc = _valid_doc(
        total_queries=2, successful_queries=1, failed_queries=1,
        partial_failure=False, outcome=ps.OUTCOME_USEFUL_SUCCESS,
    )
    _assert_rejected(tmp_path, doc)


@pytest.mark.parametrize("outcome", [
    ps.OUTCOME_USEFUL_SUCCESS,
    ps.OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS,
    ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS,
    ps.OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL,
])
def test_reject_failed_queries_with_any_technical_success_outcome(tmp_path, outcome):
    doc = _valid_doc(
        total_queries=2, successful_queries=1, failed_queries=1,
        partial_failure=True, outcome=outcome,
    )
    _assert_rejected(tmp_path, doc)


def test_reject_useful_success_with_zero_inserted_rows(tmp_path):
    doc = _valid_doc(outcome=ps.OUTCOME_USEFUL_SUCCESS)
    doc["aggregate_collector_metrics"]["rows_inserted"] = 0
    _assert_rejected(tmp_path, doc)


def test_reject_no_pending_targets_with_nonzero_query_counts(tmp_path):
    doc = _valid_doc(
        had_pending_targets=False, outcome=ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS,
        total_queries=1, successful_queries=1, failed_queries=0,
    )
    _assert_rejected(tmp_path, doc)


def test_reject_partial_failure_outcome_with_zero_failed_queries(tmp_path):
    doc = _valid_doc(
        total_queries=1, successful_queries=1, failed_queries=0,
        partial_failure=False, outcome=ps.OUTCOME_PARTIAL_FAILURE,
    )
    _assert_rejected(tmp_path, doc)


def test_reject_failed_outcome_with_nonzero_successful_queries(tmp_path):
    doc = _valid_doc(
        total_queries=2, successful_queries=1, failed_queries=1,
        partial_failure=True, outcome=ps.OUTCOME_FAILED,
    )
    _assert_rejected(tmp_path, doc)


@pytest.mark.parametrize("bad_timestamp", [
    "2026-01-01T00:00:00",             # naive (no tzinfo)
    "2026-01-01",                       # bare date
    "not-a-timestamp",
    "2026-01-01T00:00:00+05:00",        # tz-aware but NOT UTC
    "x" * 100,                          # excessively long
])
def test_reject_naive_or_malformed_or_non_utc_generated_at(tmp_path, bad_timestamp):
    doc = _valid_doc(generated_at=bad_timestamp)
    _assert_rejected(tmp_path, doc)


def test_accept_utc_generated_at_with_z_suffix(tmp_path):
    """'...Z' (Zulu/UTC suffix) is a valid ISO-8601 UTC representation."""
    summary = ps.build_summary(
        batch(aggregate_collector_metrics=agg(rows_inserted=1, jobs_valid=1, jobs_discovered=1)), True,
    )
    doc = summary.to_dict()
    doc["generated_at"] = "2026-01-01T00:00:00Z".replace("Z", "+00:00")  # datetime.fromisoformat accepts this form
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(doc))
    loaded = ps.read_summary(path)
    assert loaded.generated_at.endswith("+00:00")


def test_reject_useful_queries_plus_zero_yield_exceeding_successful(tmp_path):
    doc = _valid_doc(successful_queries=1, useful_queries=1, zero_yield_queries=1, total_queries=1, failed_queries=0)
    _assert_rejected(tmp_path, doc)


def test_reject_zero_yield_queries_exceeding_successful(tmp_path):
    doc = _valid_doc(successful_queries=1, zero_yield_queries=2, total_queries=1, failed_queries=0)
    _assert_rejected(tmp_path, doc)


def test_reject_aggregate_outer_partition_exceeding_discovered(tmp_path):
    doc = _valid_doc()
    doc["aggregate_collector_metrics"]["jobs_filtered_invalid"] = 100
    _assert_rejected(tmp_path, doc)


def test_reject_aggregate_row_level_exceeding_jobs_valid(tmp_path):
    doc = _valid_doc()
    doc["aggregate_collector_metrics"]["rows_inserted"] = 100
    _assert_rejected(tmp_path, doc)


def test_technical_success_no_new_rows_requires_zero_inserts(tmp_path):
    doc = _valid_doc(
        outcome=ps.OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS,
        aggregate_collector_metrics=agg(rows_inserted=1, rows_updated_existing=1, jobs_valid=2, jobs_discovered=2),
    )
    _assert_rejected(tmp_path, doc)


def test_technical_success_filtered_all_requires_zero_discovered_to_be_false(tmp_path):
    """filtered_all requires jobs_discovered > 0 -- zero discovered
    contradicts 'discovered jobs existed but were all filtered'."""
    doc = _valid_doc(
        outcome=ps.OUTCOME_TECHNICAL_SUCCESS_FILTERED_ALL,
        aggregate_collector_metrics=agg(jobs_discovered=0),
    )
    _assert_rejected(tmp_path, doc)


def test_canonical_recomputation_catches_contradiction_individual_checks_alone_miss(tmp_path):
    """A document can satisfy every individually-named invariant above
    and still be internally contradictory. Here: had_pending_targets=True,
    total_queries=0, outcome=technical_success_no_results. The dedicated
    'no_results with pending targets' check only examines failed_queries
    and jobs_discovered (both 0, so it passes) -- it does not separately
    check total_queries. But classify_cycle_outcome() checks
    `total == 0 or successful == 0` FIRST and would return "failed" for
    this exact evidence, before ever reaching the no-results branch. Only
    the final recompute-and-compare step (using the SAME function the
    writer used) catches this specific contradiction."""
    doc = _valid_doc(
        outcome=ps.OUTCOME_TECHNICAL_SUCCESS_NO_RESULTS,
        total_queries=0, successful_queries=0, failed_queries=0, partial_failure=False,
        useful_queries=0, zero_yield_queries=0, skipped_queries=0,
        aggregate_collector_metrics=agg(jobs_discovered=0, jobs_valid=0),
    )
    exc = _assert_rejected(tmp_path, doc)
    assert "recomputed" in str(exc)
    assert ps.OUTCOME_FAILED in str(exc)


def test_read_summary_never_logs_or_persists_raw_malformed_value(tmp_path, capsys):
    doc = _valid_doc(outcome="SECRET_MALFORMED_VALUE_MARKER_xyz")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ps.SummaryReadError) as excinfo:
        ps.read_summary(path)
    assert "SECRET_MALFORMED_VALUE_MARKER_xyz" not in str(excinfo.value)
    captured = capsys.readouterr()
    assert "SECRET_MALFORMED_VALUE_MARKER_xyz" not in captured.out
    assert "SECRET_MALFORMED_VALUE_MARKER_xyz" not in captured.err


def test_build_summary_generated_at_is_utc_and_timezone_aware():
    """Regression guard: an earlier revision used datetime.now() (naive,
    local time) for generated_at, which read_summary() now rejects."""
    summary = ps.build_summary(batch(), True)
    dt = datetime.fromisoformat(summary.generated_at)
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)
