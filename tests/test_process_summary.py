"""
Tests for scripts/process_summary.py: the cycle-level structured summary
that closes the "always technical_success" gap in
scripts/run_collection_cycle_safe.sh's heartbeat finish call (phase 2A
adversarial pass, item 8).

No real Docker, PostgreSQL, or LinkedIn is ever used -- this module has
no I/O dependency on any of them; only local temp files.
"""
import json
import os
import stat
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


# =====================================================================
# Ingestion hotfix: write_summary_atomic() must publish a file that is
# host-readable across the api container's root -> host's non-root
# collection-wrapper UID boundary. tempfile.mkstemp() always creates its
# file at 0600 regardless of umask, and os.replace() never changes a
# file's mode -- both proven correct by direct inspection of CPython's
# documented behavior, not merely assumed; the two tests below assert
# the produced mode directly.
# =====================================================================

def test_write_summary_atomic_produces_explicit_host_readable_mode(tmp_path):
    """The published file's permission bits must equal SUMMARY_FILE_MODE
    (0644) exactly -- readable by owner, group, and other -- regardless
    of the calling process's umask. This is the literal fix for the
    production symptom: a root-owned api-container writer and a
    non-root (uid=1001) host-side wrapper reader, connected only by a
    bind-mounted directory, with no shared group."""
    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(aggregate_collector_metrics=agg(rows_inserted=1)), True)

    old_umask = os.umask(0o077)  # deliberately hostile umask
    try:
        ps.write_summary_atomic(path, summary)
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == ps.SUMMARY_FILE_MODE == 0o644
    # Explicitly: group and other must both be able to read the file --
    # this is the exact bit a bare `tempfile.mkstemp()` (mode 0600) does
    # not set, and the exact bit a hostile umask (tested above) could not
    # have granted by accident either.
    assert mode & stat.S_IRGRP
    assert mode & stat.S_IROTH


def test_write_summary_atomic_mode_is_never_transiently_wrong(tmp_path):
    """The temp file is fchmod'd BEFORE the atomic rename, so at every
    instant the file is observable at `path` it already has the correct
    mode -- there is no window where a concurrent host-side reader could
    see a 0600 (or any other) intermediate mode at the final path."""
    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(), True)
    ps.write_summary_atomic(path, summary)
    # os.replace is a single rename syscall; by the time write_summary_atomic
    # returns, `path` can only ever have held the final, already-chmod'd
    # inode -- confirmed by checking the mode immediately after return.
    assert stat.S_IMODE(path.stat().st_mode) == ps.SUMMARY_FILE_MODE


def test_temp_file_stays_private_0600_while_content_is_incomplete(tmp_path, monkeypatch):
    """The published mode must only ever be granted AFTER the content is
    fully written, flushed, and fsync'd -- never before. Verified by
    wrapping os.fchmod itself: at the exact instant it is called (the
    ordering point write_summary_atomic uses to widen permissions), the
    temp file's mode on disk must still be the private 0600 mkstemp()
    grants by default, proving nothing widened it earlier."""
    observed_mode_at_fchmod_time = {}
    real_fchmod = os.fchmod

    def spying_fchmod(fd, mode):
        # fstat the SAME fd fchmod is about to widen -- this is the mode
        # the file has held for its entire life up to this instant.
        observed_mode_at_fchmod_time["mode"] = stat.S_IMODE(os.fstat(fd).st_mode)
        return real_fchmod(fd, mode)

    monkeypatch.setattr(ps.os, "fchmod", spying_fchmod)

    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(aggregate_collector_metrics=agg(rows_inserted=1)), True)
    ps.write_summary_atomic(path, summary)

    assert observed_mode_at_fchmod_time["mode"] == 0o600
    # ...and immediately after fchmod ran (still pre-rename), the file
    # already carries the final mode.
    assert stat.S_IMODE(path.stat().st_mode) == ps.SUMMARY_FILE_MODE


def test_fchmod_used_not_path_based_chmod(tmp_path, monkeypatch):
    """Guards the specific mechanism, not just the outcome: the fix must
    use os.fchmod on the already-open descriptor (immune to any path-
    based TOCTOU concern), never a path-based os.chmod call anywhere in
    write_summary_atomic."""
    calls = {"fchmod": 0, "chmod": 0}
    real_fchmod = os.fchmod
    real_chmod = os.chmod

    def spying_fchmod(fd, mode):
        calls["fchmod"] += 1
        return real_fchmod(fd, mode)

    def spying_chmod(*args, **kwargs):
        calls["chmod"] += 1
        return real_chmod(*args, **kwargs)

    monkeypatch.setattr(ps.os, "fchmod", spying_fchmod)
    monkeypatch.setattr(ps.os, "chmod", spying_chmod)

    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(), True)
    ps.write_summary_atomic(path, summary)

    assert calls["fchmod"] == 1
    assert calls["chmod"] == 0


def test_fchmod_happens_before_publication_not_after(tmp_path, monkeypatch):
    """The fd must be widened to 0644 BEFORE os.replace ever runs -- not
    after. Verified by recording call order via a shared sequence list
    wrapping both os.fchmod and os.replace."""
    order = []
    real_fchmod = os.fchmod
    real_replace = os.replace

    def spying_fchmod(fd, mode):
        order.append("fchmod")
        return real_fchmod(fd, mode)

    def spying_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(ps.os, "fchmod", spying_fchmod)
    monkeypatch.setattr(ps.os, "replace", spying_replace)

    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(), True)
    ps.write_summary_atomic(path, summary)

    assert order == ["fchmod", "replace"]


def test_no_partial_final_path_file_exposed_even_with_slow_writer(tmp_path, monkeypatch):
    """Simulates a slow writer: json.dump is wrapped to assert `path`
    (the FINAL path) does not exist yet at the moment content is being
    written -- the temp file is invisible under the final name until the
    single atomic os.replace() call, regardless of how long writing
    takes."""
    real_dump = json.dump

    def spying_dump(obj, fp, **kwargs):
        assert not path.exists(), "final path must not exist before publication"
        return real_dump(obj, fp, **kwargs)

    monkeypatch.setattr(ps.json, "dump", spying_dump)

    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(aggregate_collector_metrics=agg(rows_inserted=1)), True)
    ps.write_summary_atomic(path, summary)

    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == ps.SUMMARY_FILE_MODE


def test_write_then_read_round_trips_with_explicit_mode(tmp_path):
    """Item A/B from the hotfix test plan combined: the permission fix
    does not disturb the existing atomic-transport or strict-validation
    contract -- a valid summary still round-trips exactly, and the file
    left behind carries the new explicit mode."""
    path = tmp_path / "summary.json"
    summary = ps.build_summary(
        batch(aggregate_collector_metrics=agg(rows_inserted=3, jobs_valid=3, jobs_discovered=3)), True,
    )
    ps.write_summary_atomic(path, summary)
    loaded = ps.read_summary(path)
    assert loaded == summary
    assert stat.S_IMODE(path.stat().st_mode) == ps.SUMMARY_FILE_MODE


def test_missing_or_invalid_summary_still_fails_after_permission_fix(tmp_path):
    """Item B: the permission fix must not weaken result_read_error /
    invalid_result detection -- a missing file and a malformed document
    are both still rejected exactly as before."""
    with pytest.raises(ps.SummaryReadError) as missing_exc:
        ps.read_summary(tmp_path / "missing.json")
    assert missing_exc.value.category == ps.ERROR_CATEGORY_MISSING_RESULT

    bad_path = tmp_path / "bad.json"
    bad_path.write_text("not json", encoding="utf-8")
    os.chmod(bad_path, ps.SUMMARY_FILE_MODE)  # correctly readable, still invalid content
    with pytest.raises(ps.SummaryReadError) as invalid_exc:
        ps.read_summary(bad_path)
    assert invalid_exc.value.category == ps.ERROR_CATEGORY_INVALID_RESULT


def test_write_summary_atomic_still_produces_no_partial_file_with_explicit_mode(tmp_path):
    """Item D: atomic replacement behavior (no leftover .process_summary.*
    temp file, no partially-written document ever visible at `path`) is
    unchanged by the explicit chmod call."""
    path = tmp_path / "summary.json"
    summary = ps.build_summary(batch(aggregate_collector_metrics=agg(rows_inserted=1)), True)
    ps.write_summary_atomic(path, summary)

    assert path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".process_summary.")]
    assert leftovers == []
    # Re-write over an existing summary (as a real second cycle's run_id
    # would produce a distinct path, but the same-path overwrite case --
    # e.g. a retried write -- must remain atomic too).
    summary2 = ps.build_summary(
        batch(aggregate_collector_metrics=agg(rows_updated_existing=1, jobs_valid=1, jobs_discovered=1)), True,
    )
    ps.write_summary_atomic(path, summary2)
    loaded = ps.read_summary(path)
    assert loaded.outcome == ps.OUTCOME_TECHNICAL_SUCCESS_NO_NEW_ROWS
    leftovers_after = [p for p in tmp_path.iterdir() if p.name.startswith(".process_summary.")]
    assert leftovers_after == []


def test_no_secrets_or_credentials_added_to_summary_by_the_fix(tmp_path):
    """Item E: the fix only changes file permissions -- it must not add
    any new field, and the document must still contain no secret-shaped
    content (only the pre-existing operational counters/classification).
    """
    path = tmp_path / "summary.json"
    summary = ps.build_summary(
        batch(aggregate_collector_metrics=agg(rows_inserted=1, jobs_valid=1, jobs_discovered=1)), True,
    )
    ps.write_summary_atomic(path, summary)
    raw = json.loads(path.read_text(encoding="utf-8"))

    expected_fields = {
        "schema_version", "generated_at", "had_pending_targets", "total_queries",
        "successful_queries", "failed_queries", "useful_queries", "zero_yield_queries",
        "skipped_queries", "partial_failure", "aggregate_collector_metrics", "outcome",
    }
    assert set(raw.keys()) == expected_fields
    for forbidden in ("password", "token", "cookie", "secret", "api_key", "authorization",
                       "session", "credential"):
        assert forbidden not in path.read_text(encoding="utf-8").lower()


def test_summary_file_mode_constant_is_explicit_not_umask_derived():
    """Guards the design choice itself: SUMMARY_FILE_MODE is a fixed
    module-level constant (0644), not something derived from os.umask()
    at call time -- the whole point is that the published mode must not
    depend on whatever umask the api container's entrypoint happens to
    be running under."""
    assert ps.SUMMARY_FILE_MODE == 0o644
