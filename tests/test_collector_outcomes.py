"""
Tests for phase 2A collector outcome accounting, including the phase 2A
adversarial-pass corrections:

  - scripts/collector_result.py (schema, atomic write/read, strict
    validation, outcome rules)
  - scripts/collector_postgres.py (insert_job() explicit 4-way outcomes
    via per-statement RETURNING evidence, collect_jobs_to_postgres()
    counters and result-file production)

No real Docker, PostgreSQL, Telegram, LinkedIn, or production endpoint is
ever used: `psycopg2.connect` and the job provider are the only patched
symbols, everything else is the real, unmodified production code path.

Root causes being regression-guarded here:
  1. (phase 2A) `collect_jobs_to_postgres()` used to increment a counter
     unconditionally after calling `insert_job()`, even on insert_job()'s
     own internal early-return paths where no SQL was ever executed.
  2. (phase 2A) `skipped_duplicate_count` was declared, printed, and
     never incremented anywhere.
  3. (adversarial pass) An early revision classified `success_with_new_rows`
     from a whole-table `SELECT COUNT(*)` before/after delta -- not proof
     THIS collector inserted anything, since a concurrent process's own
     inserts/deletes change the same delta. Replaced with per-statement
     `RETURNING (xmax = 0) AS inserted` evidence.
  4. (adversarial pass) A LinkedIn search-results header artifact (junk
     row) and a real job missing a recoverable identifier were both
     folded into the same "missing_identifier" outcome, hiding how much
     of a query's filtered output was junk-row noise.
  5. (adversarial pass) read_result() only checked field presence, not
     types, ranges, or outcome/counter consistency -- a malformed or
     self-contradictory document could pass through unnoticed.
"""
import dataclasses
import json
import math
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.collector_postgres as cp
import scripts.collector_result as cr


# =====================================================================
# Pure outcome-rule matrix (deterministic, no I/O)
# =====================================================================

@pytest.mark.parametrize(
    "jobs_discovered,rows_inserted,rows_updated_existing,persistence_errors,expected",
    [
        (0, 0, 0, 0, cr.OUTCOME_SUCCESS_NO_RESULTS),
        (5, 0, 0, 0, cr.OUTCOME_SUCCESS_FILTERED_ALL),
        (5, 2, 1, 0, cr.OUTCOME_SUCCESS_WITH_NEW_ROWS),
        (5, 0, 2, 0, cr.OUTCOME_SUCCESS_NO_NEW_ROWS),
        (5, 0, 0, 1, cr.OUTCOME_FAILED_PERSIST),
        (0, 0, 0, 1, cr.OUTCOME_FAILED_PERSIST),  # persistence error always wins, even with nothing discovered
        (5, 3, 2, 1, cr.OUTCOME_FAILED_PERSIST),  # persistence error wins even alongside apparent successful writes
    ],
)
def test_determine_outcome_matrix(jobs_discovered, rows_inserted, rows_updated_existing, persistence_errors, expected):
    assert cr.determine_outcome(
        jobs_discovered=jobs_discovered,
        rows_inserted=rows_inserted,
        rows_updated_existing=rows_updated_existing,
        persistence_errors=persistence_errors,
    ) == expected


def test_determine_outcome_is_a_deterministic_pure_function():
    for _ in range(3):
        assert cr.determine_outcome(jobs_discovered=10, rows_inserted=3, rows_updated_existing=0, persistence_errors=0) == cr.OUTCOME_SUCCESS_WITH_NEW_ROWS


def test_global_count_delta_is_never_the_evidence_source():
    """Structural proof: determine_outcome()'s signature has no
    "new_rows_delta"/whole-table-count parameter at all -- only
    per-statement rows_inserted/rows_updated_existing. A concurrent
    process inserting or deleting rows elsewhere cannot influence this
    function's output because it never receives that information."""
    import inspect
    params = set(inspect.signature(cr.determine_outcome).parameters)
    assert "new_rows_delta" not in params
    assert "jobs_after" not in params
    assert "jobs_before" not in params
    assert params == {"jobs_discovered", "rows_inserted", "rows_updated_existing", "persistence_errors"}


# =====================================================================
# CollectorResult atomic write/read
# =====================================================================

def make_result(**overrides) -> cr.CollectorResult:
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
    return cr.CollectorResult(**fields)


def test_write_result_atomic_produces_valid_json_no_partial_file(tmp_path):
    path = tmp_path / "result.json"
    cr.write_result_atomic(path, make_result())

    assert path.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".collector_result.")]
    assert leftovers == []

    data = json.loads(path.read_text())
    assert data["outcome"] == cr.OUTCOME_SUCCESS_WITH_NEW_ROWS
    assert data["schema_version"] == cr.SCHEMA_VERSION


def test_write_result_atomic_uses_tempfile_and_replace_in_same_dir(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "result.json"
    calls = {}
    real_replace = cr.os.replace

    def spy_replace(src, dst):
        calls["src_dir"] = cr.os.path.dirname(src)
        calls["dst_dir"] = cr.os.path.dirname(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(cr.os, "replace", spy_replace)
    cr.write_result_atomic(path, make_result())

    assert calls["src_dir"] == calls["dst_dir"] == str(path.parent)


def test_read_result_round_trips(tmp_path):
    path = tmp_path / "result.json"
    original = make_result(jobs_discovered=5, rows_inserted=2, rows_updated_existing=3, jobs_valid=5)
    cr.write_result_atomic(path, original)

    loaded = cr.read_result(path)
    assert loaded == original


def test_read_result_missing_file_raises_missing_result_category(tmp_path):
    with pytest.raises(cr.ResultReadError) as excinfo:
        cr.read_result(tmp_path / "does_not_exist.json")
    assert excinfo.value.category == cr.ERROR_CATEGORY_MISSING_RESULT


def test_read_result_permission_error_is_not_classified_missing_result(tmp_path):
    """A file that EXISTS but can't be read for an infrastructure reason
    (permission denied) must never be mislabeled as 'missing' -- the two
    are different failure modes with different remediation."""
    path = tmp_path / "unreadable.json"
    path.write_text(json.dumps(make_result().to_dict()))
    path.chmod(0o000)
    try:
        import os
        if os.geteuid() == 0:
            pytest.skip("running as root -- permission bits are not enforced")
        with pytest.raises(cr.ResultReadError) as excinfo:
            cr.read_result(path)
        assert excinfo.value.category == cr.ERROR_CATEGORY_RESULT_READ_ERROR
        assert excinfo.value.category != cr.ERROR_CATEGORY_MISSING_RESULT
    finally:
        path.chmod(0o644)


def test_read_result_directory_instead_of_file_is_result_read_error(tmp_path):
    path = tmp_path / "a_directory"
    path.mkdir()
    with pytest.raises(cr.ResultReadError) as excinfo:
        cr.read_result(path)
    assert excinfo.value.category == cr.ERROR_CATEGORY_RESULT_READ_ERROR


@pytest.mark.parametrize("content", ["not json at all", "[]", "{}", '{"schema_version": 999}'])
def test_read_result_malformed_or_wrong_schema_raises_invalid_result_category(tmp_path, content):
    path = tmp_path / "result.json"
    path.write_text(content)
    with pytest.raises(cr.ResultReadError) as excinfo:
        cr.read_result(path)
    assert excinfo.value.category == cr.ERROR_CATEGORY_INVALID_RESULT


def test_result_write_failure_is_not_swallowed(tmp_path):
    bad_path = tmp_path / "not_a_directory" / "result.json"
    (tmp_path / "not_a_directory").write_text("i am a file, not a directory")

    with pytest.raises(Exception):
        cr.write_result_atomic(bad_path, make_result())


# =====================================================================
# Strict validation: type/range rejection
# =====================================================================

def _write_raw(path, **overrides):
    doc = make_result().to_dict()
    doc.update(overrides)
    path.write_text(json.dumps(doc))
    return path


@pytest.mark.parametrize("field", [
    "jobs_discovered", "jobs_valid", "jobs_filtered_invalid", "jobs_filtered_non_linkedin",
    "jobs_filtered_header_artifact", "jobs_filtered_missing_identifier",
    "rows_inserted", "rows_updated_existing", "persistence_errors",
])
def test_numeric_string_counter_is_rejected(tmp_path, field):
    path = _write_raw(tmp_path / "r.json", **{field: "5"})
    with pytest.raises(cr.ResultReadError) as excinfo:
        cr.read_result(path)
    assert excinfo.value.category == cr.ERROR_CATEGORY_INVALID_RESULT


@pytest.mark.parametrize("field", ["jobs_discovered", "rows_inserted", "persistence_errors"])
def test_boolean_is_rejected_as_counter(tmp_path, field):
    """bool is a subclass of int in Python -- True/False must never be
    silently accepted where a counter is expected."""
    path = _write_raw(tmp_path / "r.json", **{field: True})
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


@pytest.mark.parametrize("field", ["jobs_discovered", "rows_inserted", "rows_updated_existing"])
def test_negative_counter_is_rejected(tmp_path, field):
    path = _write_raw(tmp_path / "r.json", **{field: -1})
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_excessively_large_counter_is_rejected(tmp_path):
    path = _write_raw(tmp_path / "r.json", jobs_discovered=cr.MAX_COUNTER_VALUE + 1, jobs_valid=cr.MAX_COUNTER_VALUE + 1)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_nan_duration_is_rejected(tmp_path):
    # json.dumps(float('nan')) emits the literal `NaN` token, which
    # Python's own json.loads happily parses back to float('nan') -- so
    # this is a realistic malformed-but-parseable document, not an
    # artificial test-only shape.
    doc = make_result().to_dict()
    doc["duration_seconds"] = float("nan")
    path = tmp_path / "r.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_infinity_duration_is_rejected(tmp_path):
    doc = make_result().to_dict()
    doc["duration_seconds"] = float("inf")
    path = tmp_path / "r.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_excessively_large_duration_is_rejected(tmp_path):
    path = _write_raw(tmp_path / "r.json", duration_seconds=cr.MAX_DURATION_SECONDS + 1)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


@pytest.mark.parametrize("value", ["not-a-timestamp", "2026-01-01", "2026-01-01T00:00:00", 12345, None])
def test_malformed_or_naive_timestamp_is_rejected(tmp_path, value):
    """A bare date, a non-ISO string, a number, null, or a timezone-naive
    timestamp must all be rejected -- only a real timezone-AWARE ISO-8601
    string is accepted."""
    path = _write_raw(tmp_path / "r.json", started_at=value)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_empty_provider_is_rejected(tmp_path):
    path = _write_raw(tmp_path / "r.json", provider="")
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_excessively_long_provider_is_rejected(tmp_path):
    path = _write_raw(tmp_path / "r.json", provider="x" * (cr.MAX_PROVIDER_LEN + 1))
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_excessively_long_sanitized_error_is_rejected(tmp_path):
    path = _write_raw(
        tmp_path / "r.json",
        outcome=cr.OUTCOME_FAILED_INTERNAL, error_category=cr.ERROR_CATEGORY_INTERNAL,
        sanitized_error="x" * (cr.MAX_ERROR_TEXT_LEN + 1),
    )
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


# =====================================================================
# Strict validation: contradictory outcome/counter invariants
# =====================================================================

def test_success_no_results_requires_zero_discovered(tmp_path):
    path = _write_raw(tmp_path / "r.json", outcome=cr.OUTCOME_SUCCESS_NO_RESULTS, jobs_discovered=5, jobs_valid=0,
                       jobs_filtered_invalid=5, rows_inserted=0, rows_updated_existing=0)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_success_filtered_all_requires_zero_writes(tmp_path):
    path = _write_raw(
        tmp_path / "r.json", outcome=cr.OUTCOME_SUCCESS_FILTERED_ALL,
        jobs_discovered=1, jobs_valid=1, jobs_filtered_header_artifact=0, jobs_filtered_missing_identifier=0,
        rows_inserted=1, rows_updated_existing=0,
    )
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_success_with_new_rows_requires_proven_insert(tmp_path):
    """This is the direct regression guard for the original whole-table
    COUNT delta defect: a document claiming success_with_new_rows with
    rows_inserted == 0 must be rejected, even if it looks otherwise
    self-consistent."""
    path = _write_raw(
        tmp_path / "r.json", outcome=cr.OUTCOME_SUCCESS_WITH_NEW_ROWS,
        jobs_discovered=1, jobs_valid=1, rows_inserted=0, rows_updated_existing=1,
    )
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_success_no_new_rows_requires_zero_inserts_and_some_updates(tmp_path):
    path = _write_raw(
        tmp_path / "r.json", outcome=cr.OUTCOME_SUCCESS_NO_NEW_ROWS,
        jobs_discovered=1, jobs_valid=1, rows_inserted=1, rows_updated_existing=0,
    )
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_success_outcome_cannot_have_persistence_errors(tmp_path):
    path = _write_raw(tmp_path / "r.json", outcome=cr.OUTCOME_SUCCESS_NO_RESULTS, jobs_discovered=0, persistence_errors=1)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_failed_outcome_requires_error_category(tmp_path):
    path = _write_raw(tmp_path / "r.json", outcome=cr.OUTCOME_FAILED_INTERNAL, error_category=None, persistence_errors=0)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_failed_persist_requires_persistence_errors(tmp_path):
    path = _write_raw(tmp_path / "r.json", outcome=cr.OUTCOME_FAILED_PERSIST, error_category=cr.ERROR_CATEGORY_PERSIST, persistence_errors=0)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_counters_exceeding_jobs_discovered_are_rejected(tmp_path):
    path = _write_raw(tmp_path / "r.json", jobs_discovered=1, jobs_valid=1, jobs_filtered_invalid=1, jobs_filtered_non_linkedin=1)
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_partition_must_be_exact_when_no_persistence_errors(tmp_path):
    """jobs_filtered_invalid + jobs_filtered_non_linkedin + jobs_valid
    must equal jobs_discovered exactly when persistence_errors == 0 --
    an under-count (some rows unaccounted for) is just as invalid as an
    over-count."""
    path = _write_raw(
        tmp_path / "r.json", outcome=cr.OUTCOME_SUCCESS_FILTERED_ALL,
        jobs_discovered=5, jobs_valid=1, jobs_filtered_invalid=1, jobs_filtered_non_linkedin=1,
        jobs_filtered_header_artifact=1, rows_inserted=0, rows_updated_existing=0, persistence_errors=0,
    )
    with pytest.raises(cr.ResultReadError):
        cr.read_result(path)


def test_valid_document_with_persistence_error_partial_partition_is_accepted(tmp_path):
    """When persistence_errors > 0, the loop may have aborted partway
    through -- an inexact (but not over-counted) partition is legitimate
    here, unlike the zero-error case above."""
    path = _write_raw(
        tmp_path / "r.json", outcome=cr.OUTCOME_FAILED_PERSIST, error_category=cr.ERROR_CATEGORY_PERSIST,
        jobs_discovered=5, jobs_valid=1, jobs_filtered_invalid=1, jobs_filtered_non_linkedin=0,
        jobs_filtered_header_artifact=0, jobs_filtered_missing_identifier=0,
        rows_inserted=0, rows_updated_existing=0, persistence_errors=1,
    )
    result = cr.read_result(path)
    assert result.outcome == cr.OUTCOME_FAILED_PERSIST


# =====================================================================
# insert_job(): every path returns an explicit ROW_OUTCOME_*, proven by
# per-statement RETURNING (xmax = 0) evidence, never a pre-SELECT
# =====================================================================

class FakeCursor:
    def __init__(self, inserted_sequence=None):
        self.executed = []
        self._inserted_sequence = list(inserted_sequence) if inserted_sequence is not None else [True]
        self._call_index = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        idx = min(self._call_index, len(self._inserted_sequence) - 1)
        value = self._inserted_sequence[idx]
        self._call_index += 1
        return (value,) if value is not None else None

    def close(self):
        pass

    @property
    def insert_calls(self):
        return [c for c in self.executed if isinstance(c[0], str) and "INSERT INTO jobs" in c[0]]


def valid_job(**overrides):
    job = {
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "job_description": "We are hiring.",
        "job_url": "https://www.linkedin.com/jobs/view/123456/",
        "linkedin_job_id": "123456",
        "source": "LinkedIn",
    }
    job.update(overrides)
    return job


def test_insert_job_sql_uses_returning_xmax_not_a_pre_select():
    """Structural proof this is per-statement evidence, not a
    pre-SELECT-then-INSERT race: there is exactly one SQL statement
    executed by insert_job() for a real row, and it is the INSERT itself
    carrying the RETURNING clause -- no separate SELECT is ever issued
    beforehand to check for an existing row."""
    cursor = FakeCursor([True])
    cp.insert_job(cursor, cp.normalize_job(valid_job()))
    assert len(cursor.executed) == 1
    sql = cursor.executed[0][0]
    assert "INSERT INTO jobs" in sql
    assert "RETURNING (xmax = 0) AS inserted" in sql
    assert "SELECT" not in sql.split("RETURNING")[0].replace("SELECT COUNT", "")  # no separate probe SELECT


def test_insert_job_proven_insert_returns_inserted_outcome():
    cursor = FakeCursor([True])
    outcome = cp.insert_job(cursor, cp.normalize_job(valid_job()))
    assert outcome == cr.ROW_OUTCOME_INSERTED


def test_insert_job_proven_conflict_update_returns_updated_existing_outcome():
    """xmax != 0 (the RETURNING clause evaluates to false) means the row
    went through the ON CONFLICT DO UPDATE branch -- this must NEVER be
    counted as an insert."""
    cursor = FakeCursor([False])
    outcome = cp.insert_job(cursor, cp.normalize_job(valid_job()))
    assert outcome == cr.ROW_OUTCOME_UPDATED_EXISTING


# =====================================================================
# Inconclusive RETURNING evidence is a FAILURE, never a guessed outcome
# (adversarial-pass item 3 correction). An earlier revision treated a
# missing/null/non-boolean RETURNING value as "not inserted" -- silently
# reporting an unproven guess as if it were proven evidence, exactly the
# category of defect this whole schema exists to eliminate. It must
# instead raise UpsertEvidenceError, which the caller
# (collect_jobs_to_postgres()) catches like any other persistence-layer
# exception: rollback, persistence_errors += 1, outcome=failed_persist,
# non-zero exit -- never success_no_new_rows or any other success.
# =====================================================================

def test_interpret_upsert_returning_none_row_raises():
    with pytest.raises(cp.UpsertEvidenceError):
        cp._interpret_upsert_returning(None)


@pytest.mark.parametrize("row", [(None,), ("false",), ("true",), (0,), (1,), (0.0,), (1.0,), ([],), ({},)])
def test_interpret_upsert_returning_non_boolean_values_raise(row):
    """Only an ACTUAL Python bool is accepted -- not a truthy/falsy value
    of any other type. 0/1 are the classic trap: bool is a subclass of
    int, but int is NOT a subclass of bool, so `isinstance(0, bool)` is
    False and must be rejected, never silently treated as False/inserted=false."""
    with pytest.raises(cp.UpsertEvidenceError):
        cp._interpret_upsert_returning(row)


@pytest.mark.parametrize("row,expected", [
    ((True,), cr.ROW_OUTCOME_INSERTED),
    ((False,), cr.ROW_OUTCOME_UPDATED_EXISTING),
])
def test_interpret_upsert_returning_actual_booleans_are_accepted(row, expected):
    assert cp._interpret_upsert_returning(row) == expected


def test_insert_job_none_row_raises_upsert_evidence_error():
    cursor = FakeCursor()
    cursor.fetchone = lambda: None
    with pytest.raises(cp.UpsertEvidenceError):
        cp.insert_job(cursor, cp.normalize_job(valid_job()))


def test_insert_job_none_value_in_row_raises_upsert_evidence_error():
    cursor = FakeCursor()
    cursor.fetchone = lambda: (None,)
    with pytest.raises(cp.UpsertEvidenceError):
        cp.insert_job(cursor, cp.normalize_job(valid_job()))


def test_insert_job_string_false_raises_upsert_evidence_error():
    cursor = FakeCursor()
    cursor.fetchone = lambda: ("false",)
    with pytest.raises(cp.UpsertEvidenceError):
        cp.insert_job(cursor, cp.normalize_job(valid_job()))


def test_insert_job_integer_zero_raises_upsert_evidence_error():
    cursor = FakeCursor()
    cursor.fetchone = lambda: (0,)
    with pytest.raises(cp.UpsertEvidenceError):
        cp.insert_job(cursor, cp.normalize_job(valid_job()))


def test_insert_job_integer_one_raises_upsert_evidence_error():
    cursor = FakeCursor()
    cursor.fetchone = lambda: (1,)
    with pytest.raises(cp.UpsertEvidenceError):
        cp.insert_job(cursor, cp.normalize_job(valid_job()))


def test_collect_jobs_inconclusive_returning_becomes_failed_persist_never_success(result_path):
    """End-to-end through collect_jobs_to_postgres(): an inconclusive
    RETURNING value must produce outcome=failed_persist, a rollback, a
    non-zero exit code -- and specifically must NEVER produce
    success_no_new_rows (the outcome an earlier, incorrect revision would
    have silently produced by treating the inconclusive read as 'not
    inserted')."""
    class InconclusiveCursor(FakeCursor):
        def fetchone(self):
            return (None,)

    class InconclusiveConn(FakeConn):
        def cursor(self):
            self._cursor = InconclusiveCursor()
            return self._cursor

    fake_conn = InconclusiveConn()
    with mock.patch.object(cp, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cp, "get_job_provider", return_value=FakeProvider([valid_job()])):
        fake_psycopg2.connect.return_value = fake_conn
        exit_code = cp.collect_jobs_to_postgres()

    result = cr.read_result(result_path)
    assert exit_code == 1
    assert result.outcome == cr.OUTCOME_FAILED_PERSIST
    assert result.outcome != cr.OUTCOME_SUCCESS_NO_NEW_ROWS
    assert result.error_category == cr.ERROR_CATEGORY_PERSIST
    assert result.persistence_errors == 1
    assert result.rows_inserted == 0
    assert result.rows_updated_existing == 0
    assert fake_conn.rolled_back is True
    assert "UpsertEvidenceError" in (result.sanitized_error or "")


def test_insert_job_header_artifact_has_its_own_outcome_without_sql():
    cursor = FakeCursor()
    job = cp.normalize_job({"title": "Engineer", "job_url": "https://linkedin.com/jobs/view/1"})
    outcome = cp.insert_job(cursor, job)
    assert outcome == cr.ROW_OUTCOME_FILTERED_HEADER_ARTIFACT
    assert cursor.insert_calls == []


def test_insert_job_missing_identifier_has_a_distinct_outcome_from_header_artifact():
    """Regression guard: these two skip reasons used to be conflated into
    one 'missing_identifier' outcome. They must now be distinguishable."""
    cursor = FakeCursor()
    job = cp.normalize_job(valid_job(job_url="https://example.com/no-id-here", linkedin_job_id=""))
    job["linkedin_job_id"] = ""
    job["job_url"] = "https://example.com/no-id-here"
    outcome = cp.insert_job(cursor, job)
    assert outcome == cr.ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER
    assert outcome != cr.ROW_OUTCOME_FILTERED_HEADER_ARTIFACT
    assert cursor.insert_calls == []


def test_insert_job_never_returns_none():
    cursor = FakeCursor([True])
    cases = [
        {"title": "Engineer", "job_url": "https://linkedin.com/jobs/view/1"},  # header artifact
        valid_job(),  # normal insert
    ]
    for raw in cases:
        job = cp.normalize_job(raw)
        outcome = cp.insert_job(cursor, job)
        assert outcome is not None
        assert outcome in cr.ROW_OUTCOMES


# =====================================================================
# collect_jobs_to_postgres(): counters reflect only proven code paths,
# never a whole-table COUNT(*) delta
# =====================================================================

class FakeConn:
    def __init__(self, inserted_sequence=None):
        self._cursor = FakeCursor(inserted_sequence)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeProvider:
    def __init__(self, jobs=None, raise_on_fetch=None):
        self._jobs = jobs or []
        self._raise = raise_on_fetch

    def fetch_jobs(self):
        if self._raise:
            raise self._raise
        return self._jobs


@pytest.fixture
def result_path(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    monkeypatch.setenv(cr.RESULT_PATH_ENV_VAR, str(path))
    yield path
    monkeypatch.delenv(cr.RESULT_PATH_ENV_VAR, raising=False)


def run_collect(jobs, inserted_sequence=None, raise_on_fetch=None):
    fake_conn = FakeConn(inserted_sequence)
    with mock.patch.object(cp, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cp, "get_job_provider", return_value=FakeProvider(jobs, raise_on_fetch)):
        fake_psycopg2.connect.return_value = fake_conn
        exit_code = cp.collect_jobs_to_postgres()
    return exit_code, fake_conn


def test_collect_jobs_never_issues_a_whole_table_count_query():
    """Structural regression guard: no `SELECT COUNT(*) FROM jobs` is
    ever executed by the collector any more -- the only COUNT-shaped
    statement family in the module is per-row RETURNING evidence."""
    exit_code, conn = run_collect([valid_job()], inserted_sequence=[True])
    for sql, _ in conn._cursor.executed:
        assert "COUNT(*) FROM jobs" not in sql
        assert "SELECT COUNT(*) FROM jobs" not in sql


def test_zero_discovered_jobs_yields_success_no_results(result_path):
    exit_code, conn = run_collect([])
    result = cr.read_result(result_path)
    assert exit_code == 0
    assert result.outcome == cr.OUTCOME_SUCCESS_NO_RESULTS
    assert result.jobs_discovered == 0
    assert result.rows_inserted == 0
    assert conn._cursor.insert_calls == []


def test_all_rows_filtered_yields_success_filtered_all_with_separated_reasons(result_path):
    jobs = [
        {"title": "", "job_url": "https://linkedin.com/jobs/view/1"},          # fails is_valid_job
        {"title": "Engineer", "job_url": "https://linkedin.com/jobs/view/2"},  # header-artifact filtered
    ]
    exit_code, conn = run_collect(jobs)
    result = cr.read_result(result_path)
    assert exit_code == 0
    assert result.outcome == cr.OUTCOME_SUCCESS_FILTERED_ALL
    assert result.jobs_discovered == 2
    assert result.jobs_filtered_invalid == 1
    assert result.jobs_filtered_header_artifact == 1
    assert result.jobs_filtered_missing_identifier == 0
    assert result.rows_inserted == 0
    assert result.rows_updated_existing == 0
    assert conn._cursor.insert_calls == []


def test_proven_insert_increments_rows_inserted_and_yields_success_with_new_rows(result_path):
    jobs = [valid_job()]
    exit_code, conn = run_collect(jobs, inserted_sequence=[True])
    result = cr.read_result(result_path)
    assert exit_code == 0
    assert result.outcome == cr.OUTCOME_SUCCESS_WITH_NEW_ROWS
    assert result.rows_inserted == 1
    assert result.rows_updated_existing == 0
    assert len(conn._cursor.insert_calls) == 1


def test_proven_conflict_update_does_not_increment_rows_inserted(result_path):
    jobs = [valid_job()]
    exit_code, conn = run_collect(jobs, inserted_sequence=[False])
    result = cr.read_result(result_path)
    assert exit_code == 0
    assert result.outcome == cr.OUTCOME_SUCCESS_NO_NEW_ROWS
    assert result.rows_inserted == 0
    assert result.rows_updated_existing == 1


def test_mixed_batch_counts_each_row_by_its_own_proven_outcome(result_path):
    jobs = [
        valid_job(job_url="https://www.linkedin.com/jobs/view/1/", linkedin_job_id="1"),  # inserted
        valid_job(job_url="https://www.linkedin.com/jobs/view/2/", linkedin_job_id="2"),  # updated
        valid_job(job_url="https://www.linkedin.com/jobs/view/3/", linkedin_job_id="3"),  # inserted
    ]
    exit_code, conn = run_collect(jobs, inserted_sequence=[True, False, True])
    result = cr.read_result(result_path)
    assert result.rows_inserted == 2
    assert result.rows_updated_existing == 1
    assert result.outcome == cr.OUTCOME_SUCCESS_WITH_NEW_ROWS  # at least one proven insert -> useful


def test_concurrent_unrelated_global_activity_cannot_produce_success_with_new_rows(result_path):
    """Even if some OTHER process is concurrently inserting rows into
    `jobs` during this run (which this collector cannot see or control),
    a batch where every one of THIS collector's own writes was a proven
    conflict/update must still classify as success_no_new_rows -- there
    is no global count anywhere in this code path for a concurrent writer
    to influence."""
    jobs = [valid_job()]
    exit_code, conn = run_collect(jobs, inserted_sequence=[False])
    result = cr.read_result(result_path)
    assert result.outcome != cr.OUTCOME_SUCCESS_WITH_NEW_ROWS
    assert result.outcome == cr.OUTCOME_SUCCESS_NO_NEW_ROWS
    # No COUNT(*) query exists in this module to be influenced in the
    # first place -- confirmed again here for this specific scenario.
    for sql, _ in conn._cursor.executed:
        assert "COUNT(*) FROM jobs" not in sql


def test_fetch_failure_yields_failed_fetch_and_nonzero_exit(result_path):
    exit_code, conn = run_collect([], raise_on_fetch=RuntimeError("provider exploded"))
    result = cr.read_result(result_path)
    assert exit_code == 1
    assert result.outcome == cr.OUTCOME_FAILED_FETCH
    assert result.error_category == cr.ERROR_CATEGORY_FETCH
    assert "provider exploded" in result.sanitized_error


def test_persist_failure_yields_failed_persist_and_nonzero_exit(result_path):
    class ExplodingCursor(FakeCursor):
        def execute(self, sql, params=None):
            if "INSERT INTO jobs" in sql:
                raise RuntimeError("duplicate key value violates unique constraint")
            super().execute(sql, params)

    class ExplodingConn(FakeConn):
        def cursor(self):
            self._cursor = ExplodingCursor()
            return self._cursor

    fake_conn = ExplodingConn()
    with mock.patch.object(cp, "psycopg2") as fake_psycopg2, \
         mock.patch.object(cp, "get_job_provider", return_value=FakeProvider([valid_job()])):
        fake_psycopg2.connect.return_value = fake_conn
        exit_code = cp.collect_jobs_to_postgres()

    result = cr.read_result(result_path)
    assert exit_code == 1
    assert result.outcome == cr.OUTCOME_FAILED_PERSIST
    assert result.error_category == cr.ERROR_CATEGORY_PERSIST
    assert result.persistence_errors == 1
    assert fake_conn.rolled_back is True


def test_no_raw_exception_or_secret_persisted_on_failure(result_path):
    secret_exc = RuntimeError("connect failed: postgresql://jobpulse_user:topsecret123@db/jobpulse")
    exit_code, _ = run_collect([], raise_on_fetch=secret_exc)
    raw_text = result_path.read_text()
    assert "topsecret123" not in raw_text


def test_result_write_failure_returns_nonzero(result_path, monkeypatch):
    def boom(path, result):
        raise OSError("disk full")

    monkeypatch.setattr(cp, "write_result_atomic", boom)
    exit_code, conn = run_collect([valid_job()], inserted_sequence=[True])
    assert exit_code == 1


def test_no_dead_duplicate_or_upsert_delta_field_reported_as_a_real_metric():
    field_names = {f.name for f in dataclasses.fields(cr.CollectorResult)}
    assert "skipped_duplicate_count" not in field_names
    assert "duplicates" not in field_names
    assert "upserts_executed" not in field_names  # superseded by rows_inserted/rows_updated_existing
    assert "new_rows_delta" not in field_names  # the removed, race-prone whole-table delta
    required = {
        "jobs_discovered", "jobs_valid", "jobs_filtered_invalid",
        "jobs_filtered_non_linkedin", "jobs_filtered_header_artifact",
        "jobs_filtered_missing_identifier", "rows_inserted", "rows_updated_existing",
        "persistence_errors",
    }
    assert required.issubset(field_names)


def test_module_entrypoint_uses_sys_exit_of_return_code():
    source = (REPO_ROOT / "scripts" / "collector_postgres.py").read_text()
    assert "sys.exit(collect_jobs_to_postgres())" in source
