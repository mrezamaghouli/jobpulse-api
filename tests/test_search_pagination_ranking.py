"""
Regression tests for "search pagination applied before relevance/quality
ranking".

Root cause (confirmed against the unmodified source before this fix): for
filters-only browsing (no free-text query) with sort_by=relevance,
get_all_jobs_from_db had no SQL-recognized "relevance" sort column, so it
silently fell back to ORDER BY j.last_seen_at DESC and applied LIMIT/OFFSET
in SQL *before* any relevance signal was computed. search_jobs_from_db then
called sort_results_by_backend_relevance() on only that already-sliced page.
A job that was globally most relevant (by quality + freshness) but ranked,
say, 13th by last_seen_at could never appear on page 1 -- it was excluded by
SQL before Python ever saw it.

The fix moves the equivalent ranking expression (RELEVANCE_SCORE_SQL, an
exact port of backend_relevance_value() for the no-query case, where
match_score is always 0) into the SQL ORDER BY, so the complete filtered
candidate set is ranked before LIMIT/OFFSET -- not a fetch-more-then-filter
strategy, so it does not fetch more rows than before and needs no candidate
pool cap.

The free-text hybrid path (get_all_jobs_from_db's use_hybrid_search branch)
already ranked its bounded candidate pool and applied its relevance
threshold before slicing -- these tests also lock in that existing, correct
behavior as a regression guard.

No real PostgreSQL server is used anywhere in this file; the DB cursor is a
small in-memory fake that records executed SQL/params and returns
pre-programmed rows.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import app.repositories.jobs_postgres_repository as repo

REPO_ROOT = Path(__file__).resolve().parent.parent


NOW = datetime.now(timezone.utc)


class FakeCursor:
    """Records every execute() call and replays pre-programmed responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (sql, params)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        index = len(self.calls) - 1
        return self._responses[index][0]

    def fetchall(self):
        index = len(self.calls) - 1
        return self._responses[index][1] or []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor

    def close(self):
        pass


def mock_db(responses):
    cursor = FakeCursor(responses)
    connection = FakeConnection(cursor)
    return patch.object(repo, "get_postgres_connection", return_value=connection), cursor


def make_job(
    id,
    last_seen_days_ago,
    posted_hours_ago,
    has_logo=False,
    has_apply_url=False,
    has_description=False,
    has_linkedin=False,
):
    """A row shaped like what RealDictCursor would return for the jobs table."""
    last_seen = NOW - timedelta(days=last_seen_days_ago)
    posted = NOW - timedelta(hours=posted_hours_ago)
    return {
        "id": id,
        "linkedin_job_id": None,
        "title": f"Job {id}",
        "company": "Acme",
        "company_id": None,
        "company_linkedin_url": "https://linkedin.example/company/acme" if has_linkedin else None,
        "company_logo_url": "https://logo.example/acme.png" if has_logo else None,
        "location": "Remote",
        "remote": True,
        "work_mode": "remote",
        "job_type": "full_time",
        "seniority": "mid",
        "salary_min": None,
        "salary_max": None,
        "currency": None,
        "source": "LinkedIn",
        "job_url": f"https://example.com/job/{id}",
        "job_description": "A great role." if has_description else None,
        "job_about": None,
        "date_posted_text": None,
        "date_posted_at": posted,
        "apply_type": "external",
        "apply_url": "https://apply.example/x" if has_apply_url else None,
        "apply_label": None,
        "poster_name": None,
        "poster_title": None,
        "poster_profile_url": None,
        "date_posted": None,
        "first_seen_at": last_seen,
        "last_seen_at": last_seen,
        "is_active": True,
        "inactive_at": None,
        "inactive_reason": None,
        "archived_at": None,
        "deleted_at": None,
    }


def reference_relevance_score(job):
    """Independent re-implementation of RELEVANCE_SCORE_SQL's arithmetic,
    used to compute what a real Postgres engine would return for a given
    ORDER BY -- kept separate from backend_relevance_value() so the test
    does not simply assert the code agrees with itself."""
    quality = 0.0
    if job.get("company_logo_url"):
        quality += 0.30
    if job.get("apply_url"):
        quality += 0.25
    if job.get("job_description"):
        quality += 0.25
    if job.get("company_linkedin_url"):
        quality += 0.20
    quality_component = quality * 0.15

    posted = job.get("date_posted_at") or job.get("first_seen_at") or job.get("last_seen_at")
    age_hours = (NOW - posted).total_seconds() / 3600.0

    if age_hours <= 24:
        freshness_component = 0.10
    elif age_hours <= 72:
        freshness_component = 0.06
    elif age_hours <= 168:
        freshness_component = 0.03
    else:
        freshness_component = 0.0

    return quality_component + freshness_component


def sql_order_candidates(candidates, sort_order="desc"):
    reverse = sort_order != "asc"
    return sorted(
        candidates,
        key=lambda j: (
            reference_relevance_score(j),
            j["last_seen_at"].isoformat(),
            j["id"],
        ),
        reverse=reverse,
    )


@pytest.fixture
def candidates():
    """12 jobs that are maximally "fresh" by last_seen_at (fetched by the
    collector moments ago) but posted long ago and with zero quality
    signals, plus one job (id=42) that is genuinely the best by the
    intended relevance formula (posted 1 hour ago, all quality signals
    present) despite ranking 13th purely by last_seen_at recency."""
    rows = [
        make_job(id=1000 + i, last_seen_days_ago=0, posted_hours_ago=24 * 40)
        for i in range(12)
    ]
    best_job = make_job(
        id=42,
        last_seen_days_ago=1,
        posted_hours_ago=1,
        has_logo=True,
        has_apply_url=True,
        has_description=True,
        has_linkedin=True,
    )
    rows.append(best_job)
    return rows


# --- The core bug: globally best result must appear on page 1 ---


def test_relevance_sort_without_query_ranks_full_set_before_pagination(candidates):
    total = len(candidates)
    page_1_rows = sql_order_candidates(candidates, "desc")[:10]

    patcher, cursor = mock_db([
        ({"total": total}, None),
        (None, page_1_rows),
    ])

    with patcher:
        response = repo.search_jobs_from_db(
            sort_by="relevance", sort_order="desc", page=1, limit=10
        )

    result_ids = [row["id"] for row in response["results"]]
    assert 42 in result_ids, "globally most relevant job must appear on page 1"
    assert result_ids[0] == 42, "job 42 has the highest relevance score and must rank first"
    assert response["count"] == 13
    assert response["total_pages"] == 2
    assert response["page"] == 1
    assert response["limit"] == 10


def test_relevance_sort_sql_ranks_before_limit_offset_not_after(candidates):
    """Pins the actual mechanism: the SELECT statement's ORDER BY must use
    the relevance expression, not just last_seen_at, when sort_by=relevance
    and there is no free-text query."""
    total = len(candidates)
    page_1_rows = sql_order_candidates(candidates, "desc")[:10]

    patcher, cursor = mock_db([
        ({"total": total}, None),
        (None, page_1_rows),
    ])

    with patcher:
        repo.get_all_jobs_from_db(sort_by="relevance", sort_order="desc", page=1, limit=10)

    select_sql, select_params = cursor.calls[1]
    assert repo.RELEVANCE_SCORE_SQL in select_sql
    assert "ORDER BY" in select_sql
    # LIMIT/OFFSET must still be present and applied AFTER the ORDER BY
    # clause in the same statement -- i.e. pagination is a post-ranking
    # slice, not a pre-ranking one.
    order_by_index = select_sql.index("ORDER BY")
    limit_index = select_sql.index("LIMIT")
    assert order_by_index < limit_index
    assert select_params[-2:] == [10, 0]


def test_page_2_contains_next_ranked_results_without_duplicates(candidates):
    full_order = sql_order_candidates(candidates, "desc")
    total = len(candidates)

    page_1_rows = full_order[:10]
    page_2_rows = full_order[10:20]

    patcher1, _ = mock_db([({"total": total}, None), (None, page_1_rows)])
    with patcher1:
        page_1 = repo.search_jobs_from_db(sort_by="relevance", sort_order="desc", page=1, limit=10)

    patcher2, _ = mock_db([({"total": total}, None), (None, page_2_rows)])
    with patcher2:
        page_2 = repo.search_jobs_from_db(sort_by="relevance", sort_order="desc", page=2, limit=10)

    page_1_ids = [row["id"] for row in page_1["results"]]
    page_2_ids = [row["id"] for row in page_2["results"]]

    assert 42 in page_1_ids
    assert 42 not in page_2_ids
    assert set(page_1_ids).isdisjoint(set(page_2_ids))
    assert len(page_2_ids) == 3  # 13 total, 10 on page 1, 3 remain


def test_page_beyond_final_page_returns_empty_results_with_consistent_metadata(candidates):
    total = len(candidates)

    patcher, _ = mock_db([({"total": total}, None), (None, [])])
    with patcher:
        response = repo.search_jobs_from_db(sort_by="relevance", sort_order="desc", page=99, limit=10)

    assert response["results"] == []
    assert response["count"] == 13
    assert response["total_pages"] == 2
    assert response["page"] == 99
    assert response["limit"] == 10


def test_ascending_relevance_sort_reverses_order(candidates):
    total = len(candidates)
    page_1_rows = sql_order_candidates(candidates, "asc")[:10]

    patcher, cursor = mock_db([
        ({"total": total}, None),
        (None, page_1_rows),
    ])

    with patcher:
        response = repo.search_jobs_from_db(sort_by="relevance", sort_order="asc", page=1, limit=10)

    result_ids = [row["id"] for row in response["results"]]
    assert 42 not in result_ids, "job 42 is the best match and must sort LAST in ascending order"

    select_sql, _ = cursor.calls[1]
    assert "ASC" in select_sql
    assert "DESC" not in select_sql


def test_deterministic_tie_break_columns_present_in_order_by():
    """Rows with an identical relevance score must break ties deterministically
    (by last_seen_at, then id) rather than in arbitrary storage order."""
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])

    with patcher:
        repo.get_all_jobs_from_db(sort_by="relevance", sort_order="desc", page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    order_by_clause = select_sql[select_sql.index("ORDER BY"):]
    assert "j.last_seen_at" in order_by_clause
    assert "j.id" in order_by_clause
    # Tie-break columns must appear after the relevance expression.
    assert order_by_clause.index("last_seen_at") > order_by_clause.index(")")


def test_filters_still_apply_before_relevance_ranking():
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])

    with patcher:
        repo.get_all_jobs_from_db(
            sort_by="relevance",
            sort_order="desc",
            page=1,
            limit=10,
            location="Berlin",
            company="Acme",
            work_mode="remote",
        )

    count_sql, count_params = cursor.calls[0]
    select_sql, select_params = cursor.calls[1]

    assert "j.location ILIKE" in count_sql
    assert "j.company ILIKE" in count_sql
    assert "j.work_mode = " in count_sql

    assert "j.location ILIKE" in select_sql
    assert "j.company ILIKE" in select_sql
    assert "j.work_mode = " in select_sql

    # Filter values must be bound parameters, never interpolated into the
    # SQL text (no SQL injection risk).
    assert "Berlin" not in select_sql
    assert "Acme" not in select_sql
    assert "%Berlin%" in select_params
    assert "%Acme%" in count_params
    assert "remote" in count_params


def test_non_relevance_sort_is_unaffected():
    """sort_by values other than "relevance" must keep using a plain column
    ORDER BY (no behavior change for the existing, already-correct case)."""
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])

    with patcher:
        repo.get_all_jobs_from_db(sort_by="last_seen_at", sort_order="desc", page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    assert repo.RELEVANCE_SCORE_SQL not in select_sql
    assert "ORDER BY" in select_sql
    order_by_clause = select_sql[select_sql.index("ORDER BY"):select_sql.index("LIMIT")]
    assert "j.last_seen_at" in order_by_clause
    assert "j.id DESC" in order_by_clause


# --- Hybrid (free-text query) path: already-correct behavior, locked in ---


def test_hybrid_relevance_threshold_applied_before_pagination(monkeypatch):
    """get_all_jobs_from_db's hybrid branch must drop below-threshold
    candidates and paginate the remaining, already-sorted set -- not
    truncate first and filter/sort the truncated page.

    calculate_title_match_score/calculate_keyword_score are monkeypatched to
    fixed, controlled values (rather than relying on the real NLP-ish text
    scoring producing a specific number) so this test verifies the
    threshold-then-paginate ORDERING of operations, independent of the exact
    scoring formula. should_use_semantic_query is forced off so no real
    embedding model is ever loaded -- this test never touches PostgreSQL or
    any external service.
    """
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(
        repo, "calculate_title_match_score",
        lambda row, query: 0.9 if row["id"] == 1 else 0.1,
    )
    monkeypatch.setattr(
        repo, "calculate_keyword_score",
        lambda row, query: 0.9 if row["id"] == 1 else 0.1,
    )
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.5")

    strong_match = make_job(id=1, last_seen_days_ago=0, posted_hours_ago=1)
    weak_match = make_job(id=2, last_seen_days_ago=0, posted_hours_ago=1)

    patcher, cursor = mock_db([
        (None, [strong_match, weak_match]),
    ])

    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=10)

    result_ids = [row["id"] for row in result["results"]]
    assert result_ids == [1], "the below-threshold candidate must be dropped, not just outranked"
    # total must reflect the count AFTER threshold filtering, not the raw
    # candidate-pool size.
    assert result["total"] == 1


def test_hybrid_quality_score_contributes_to_ranking_before_pagination(monkeypatch):
    """Two candidates with identical title/keyword/freshness signals but
    different quality inputs: the higher-quality one must outrank the
    other, and that ordering must already be decided before pagination --
    proven here by requesting only limit=1 and getting the higher-quality
    job. calculate_quality_score is monkeypatched to a controlled value per
    row id so the causal link between quality and final order is
    unambiguous; should_use_semantic_query is forced off so no real
    embedding model is ever loaded."""
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.3)
    monkeypatch.setattr(
        repo, "calculate_quality_score",
        lambda row: 1.0 if row["id"] == 2 else 0.0,
    )
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    # This test isolates the effect of calculate_quality_score() on ranking
    # ORDER (via final_score); the separate text-relevance quality GATE
    # (search_quality.quality_score, applied before slicing -- see item 5)
    # is a different, additional mechanism and is neutralized here so it
    # cannot drop both fixture rows on its own account.
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.0")

    job_low_quality = make_job(id=1, last_seen_days_ago=0, posted_hours_ago=1)
    job_high_quality = make_job(id=2, last_seen_days_ago=0, posted_hours_ago=1)

    # Candidate pool query returns the LOWER quality job first (as if it
    # were "more recent") -- if pagination happened before ranking, a
    # limit=1 request could return the wrong one.
    patcher, cursor = mock_db([
        (None, [job_low_quality, job_high_quality]),
    ])

    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=1)

    assert len(result["results"]) == 1
    assert result["results"][0]["id"] == 2


# --- get_jobs_from_db must no longer run a post-pagination reranker ---


def test_get_jobs_from_db_returns_repository_result_unchanged(monkeypatch):
    """Regression lock for the rerank_jobs-after-pagination bug: previously,
    get_jobs_from_db called rerank_jobs(get_all_jobs_from_db(...), query),
    which -- for a non-empty query -- could drop rows and reorder an
    already-paginated page. get_all_jobs_from_db is now the single owner of
    filtering/ranking/pagination, so get_jobs_from_db must return its result
    completely untouched, for both query=None and a real query."""
    already_final = {
        "results": [
            {"id": 1, "title": "Job 1", "last_seen_at": "2026-01-01T00:00:00+00:00"},
            {"id": 2, "title": "Job 2", "last_seen_at": "2026-01-01T00:00:00+00:00"},
        ],
        "page": 1,
        "limit": 10,
        "total": 47,
        "total_pages": 5,
        "search_mode": "hybrid",
    }

    with patch.object(repo, "get_all_jobs_from_db", return_value=dict(already_final)):
        result_with_query = repo.get_jobs_from_db(query="python engineer", page=1, limit=10)

    with patch.object(repo, "get_all_jobs_from_db", return_value=dict(already_final)):
        result_without_query = repo.get_jobs_from_db(query=None, page=1, limit=10)

    for label, result in [("query", result_with_query), ("no query", result_without_query)]:
        assert result["results"] == already_final["results"], label
        assert [row["id"] for row in result["results"]] == [1, 2], label
        assert result["total"] == 47, label
        assert result["total_pages"] == 5, label
        assert "count" not in result, (
            f"{label}: get_jobs_from_db must not add/mutate a 'count' key the "
            "way rerank_jobs used to -- it must be a pure passthrough"
        )


def test_rerank_jobs_function_no_longer_exists_in_search_quality():
    """rerank_jobs was the post-pagination reranker; it is now fully unused
    (its only caller, get_jobs_from_db, has been fixed) and has been
    removed. quality_score/lexical_score remain, since get_all_jobs_from_db
    now calls quality_score directly, before slicing."""
    import app.search_quality as search_quality_module

    assert not hasattr(search_quality_module, "rerank_jobs")
    assert hasattr(search_quality_module, "quality_score")
    assert hasattr(search_quality_module, "lexical_score")


# --- Filters-only: quality eligibility applied via WHERE, before COUNT/SELECT ---


def test_filters_only_quality_threshold_applied_to_count_and_select_identically():
    """When JOB_SEARCH_FILTER_LOW_QUALITY is enabled (the default), the
    canonical intrinsic quality expression must gate both the COUNT and the
    SELECT query identically, using a bound parameter -- never two
    different conditions, and never a literal number spliced into the SQL
    text."""
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])

    with patcher:
        repo.get_all_jobs_from_db(page=1, limit=10)

    count_sql, count_params = cursor.calls[0]
    select_sql, select_params = cursor.calls[1]

    assert repo.QUALITY_SCORE_SQL in count_sql
    assert repo.QUALITY_SCORE_SQL in select_sql
    assert ">= %s" in count_sql
    assert ">= %s" in select_sql

    # The default threshold (JOB_SEARCH_MIN_QUALITY_SCORE, 0.18) must be a
    # bound parameter -- not interpolated as a literal into the SQL text.
    assert "0.18" not in count_sql
    assert "0.18" not in select_sql
    assert 0.18 in count_params
    assert count_params == select_params[: len(count_params)]


def test_filters_only_quality_threshold_can_be_disabled(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_FILTER_LOW_QUALITY", "false")

    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
    with patcher:
        repo.get_all_jobs_from_db(page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    assert repo.QUALITY_SCORE_SQL not in select_sql


def test_filters_only_quality_threshold_uses_configured_value(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.5")

    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
    with patcher:
        repo.get_all_jobs_from_db(page=1, limit=10)

    _, select_params = cursor.calls[1]
    assert 0.5 in select_params
    assert 0.18 not in select_params


def test_filters_only_count_reflects_quality_eligible_set_not_raw_rowcount():
    """The mocked COUNT response IS the "final eligible set" contract this
    test enforces: get_all_jobs_from_db must report exactly that number as
    total/total_pages, proving count/total_pages are read from the same
    query that carries the quality (and other) WHERE conditions -- not
    computed separately from an unfiltered row count."""
    patcher, cursor = mock_db([
        ({"total": 23}, None),
        (None, [{"id": i} for i in range(1, 11)]),
    ])

    with patcher:
        result = repo.get_all_jobs_from_db(page=1, limit=10)

    assert result["total"] == 23
    assert result["total_pages"] == 3
    assert len(result["results"]) == 10


# --- Hybrid: quality gate runs once, before slicing; no second pass after ---


def make_scored_row(id, last_seen_days_ago=0, posted_hours_ago=1):
    return make_job(id=id, last_seen_days_ago=last_seen_days_ago, posted_hours_ago=posted_hours_ago)


def test_hybrid_eligible_rows_beyond_naive_candidate_order_still_fill_the_page(monkeypatch):
    """10 candidates are intrinsically low quality (ineligible) and appear
    FIRST in candidate-pool order (as if they were "more recent"); 12
    eligible candidates come after them in raw candidate order. If
    filtering happened after slicing (the old bug), a limit=10 page could
    be starved down to 0 eligible rows. With filtering-before-slicing, page
    1 must be fully populated with 10 eligible rows."""
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.3)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.5")

    def fake_quality_score(query, job):
        # Ineligible ids are 1..10 (score below threshold); eligible ids are
        # 100..111 (score above threshold).
        return (0.9, ["eligible"]) if job["id"] >= 100 else (0.1, ["ineligible"])

    monkeypatch.setattr(repo, "text_relevance_quality_score", fake_quality_score)

    ineligible_first = [make_scored_row(id=i) for i in range(1, 11)]
    eligible_after = [make_scored_row(id=100 + i) for i in range(12)]
    candidate_rows = ineligible_first + eligible_after

    patcher, cursor = mock_db([(None, candidate_rows)])
    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=10)

    result_ids = [row["id"] for row in result["results"]]
    assert len(result_ids) == 10, "page must be fully populated from the eligible pool"
    assert all(id >= 100 for id in result_ids), "no ineligible row may appear on the page"
    assert result["total"] == 12
    assert result["total_pages"] == 2


def test_hybrid_hard_to_reach_page_2_has_the_remaining_eligible_rows(monkeypatch):
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.3)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.5")
    monkeypatch.setattr(
        repo, "text_relevance_quality_score",
        lambda query, job: (0.9, []) if job["id"] >= 100 else (0.1, []),
    )

    candidate_rows = [make_scored_row(id=i) for i in range(1, 11)] + [
        make_scored_row(id=100 + i) for i in range(12)
    ]

    patcher, _ = mock_db([(None, list(candidate_rows))])
    with patcher:
        page_2 = repo.get_all_jobs_from_db(query="anything", page=2, limit=10)

    page_2_ids = [row["id"] for row in page_2["results"]]
    assert len(page_2_ids) == 2
    assert all(id >= 100 for id in page_2_ids)


def test_hybrid_search_jobs_from_db_does_not_run_a_second_quality_filter(monkeypatch):
    """End-to-end through the full pipeline (search_jobs_from_db, not just
    get_all_jobs_from_db): the response must match get_all_jobs_from_db's
    own output exactly -- proving nothing upstream (get_jobs_from_db /
    search_jobs_from_db) filters or reorders it a second time."""
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.5)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.5)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.0")

    candidate_rows = [make_scored_row(id=i) for i in (3, 1, 2)]

    patcher, _ = mock_db([(None, list(candidate_rows))])
    with patcher:
        direct = repo.get_all_jobs_from_db(query="anything", page=1, limit=10, sort_by="relevance")

    patcher2, _ = mock_db([(None, list(candidate_rows))])
    with patcher2:
        via_pipeline = repo.search_jobs_from_db(
            query="anything", page=1, limit=10, sort_by="relevance", sort_order="desc"
        )

    direct_ids = [row["id"] for row in direct["results"]]
    pipeline_ids = [row["id"] for row in via_pipeline["results"]]
    assert pipeline_ids == direct_ids
    assert via_pipeline["count"] == direct["total"]
    assert via_pipeline["total_pages"] == direct["total_pages"]


def test_hybrid_total_is_calculated_after_relevance_and_quality_thresholds(monkeypatch):
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(
        repo, "calculate_title_match_score",
        lambda row, query: 0.9 if row["id"] in (1, 2, 3) else 0.1,
    )
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.5)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.5")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.0")

    candidate_rows = [make_scored_row(id=i) for i in range(1, 8)]  # 3 pass, 4 fail relevance
    patcher, _ = mock_db([(None, candidate_rows)])

    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=10)

    assert result["total"] == 3
    assert result["total_pages"] == 1
    assert len(result["results"]) == 3


# --- Non-relevance sorting must remain intact ---


@pytest.mark.parametrize("sort_by,sql_column", [
    ("last_seen_at", "j.last_seen_at"),
    ("date_posted_at", "j.date_posted_at"),
    ("date_posted", "j.date_posted_at"),
])
@pytest.mark.parametrize("sort_order,direction", [("asc", "ASC"), ("desc", "DESC")])
def test_non_relevance_sort_uses_requested_column_and_direction(sort_by, sql_column, sort_order, direction):
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])

    with patcher:
        repo.get_all_jobs_from_db(sort_by=sort_by, sort_order=sort_order, page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    assert repo.RELEVANCE_SCORE_SQL not in select_sql
    order_by_clause = select_sql[select_sql.index("ORDER BY"):select_sql.index("LIMIT")]
    assert f"{sql_column} {direction} NULLS LAST" in order_by_clause


# --- Canonical formula boundary values ---


@pytest.mark.parametrize("flags,expected", [
    ({}, 0.0),
    ({"company_logo_url": "https://x/y.png"}, 0.30),
    ({"apply_url": "https://x/y"}, 0.25),
    ({"job_description": "text"}, 0.25),
    ({"company_linkedin_url": "https://x/y"}, 0.20),
    (
        {
            "company_logo_url": "https://x/y.png",
            "apply_url": "https://x/y",
            "job_description": "text",
            "company_linkedin_url": "https://x/y",
        },
        1.0,
    ),
])
def test_canonical_quality_formula_boundary_values(flags, expected):
    row = {
        "company_logo_url": None,
        "apply_url": None,
        "job_description": None,
        "company_linkedin_url": None,
    }
    row.update(flags)
    assert repo.calculate_quality_score(row) == pytest.approx(expected)


def test_quality_score_sql_and_python_formula_share_the_same_weights():
    """The SQL fragment's weights must exactly match
    calculate_quality_score()'s weights -- not an approximation."""
    assert "0.30" in repo.QUALITY_SCORE_SQL
    assert "0.25" in repo.QUALITY_SCORE_SQL
    assert "0.20" in repo.QUALITY_SCORE_SQL
    assert repo.QUALITY_SCORE_SQL.count("0.25") == 2  # apply_url AND job_description


# --- Hybrid: sort_by/sort_order must be honored (previously always
# ignored -- ranked.sort() was hardcoded to score descending) ---


def make_row_with_dates(id, last_seen_days_ago, date_posted_days_ago):
    """Like make_job, but with a controlled, distinct date_posted_at value
    (overriding make_job's fixed posted_hours_ago=1). date_posted_at is the
    canonical posted-time column: both the "date_posted_at" sort_by value
    and the legacy "date_posted" alias resolve to it via ALLOWED_SORT_FIELDS
    -- see test_non_relevance_sort_uses_requested_column_and_direction and
    test_hybrid_sort_accepts_both_date_posted_aliases_resolving_to_date_posted_at."""
    row = make_job(id=id, last_seen_days_ago=last_seen_days_ago, posted_hours_ago=1)
    row["date_posted_at"] = NOW - timedelta(days=date_posted_days_ago)
    return row


def _hybrid_scored_rows_with_distinct_signals():
    """4 rows where last_seen_at, date_posted_at, and score are all set up
    so that "most recent by last_seen_at", "most recently posted", and
    "highest score" all agree on the SAME relative order (id=4 best/newest,
    id=1 worst/oldest on every axis) -- so any test that observes the wrong
    order for a given sort_by definitively proves that field is not being
    honored (as opposed to some other axis coincidentally producing the
    same order)."""
    return {
        1: make_row_with_dates(id=1, last_seen_days_ago=3, date_posted_days_ago=3),
        2: make_row_with_dates(id=2, last_seen_days_ago=2, date_posted_days_ago=2),
        3: make_row_with_dates(id=3, last_seen_days_ago=1, date_posted_days_ago=1),
        4: make_row_with_dates(id=4, last_seen_days_ago=0, date_posted_days_ago=0),
    }


def _run_hybrid_sort(monkeypatch, sort_by, sort_order):
    rows_by_id = _hybrid_scored_rows_with_distinct_signals()
    score_by_id = {1: 0.10, 2: 0.30, 3: 0.60, 4: 0.90}

    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(
        repo, "calculate_keyword_score",
        lambda row, query: score_by_id[row["id"]] * 2,
    )
    monkeypatch.setattr(repo, "calculate_quality_score", lambda row: 0.0)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.0")

    candidate_rows = [rows_by_id[i] for i in (2, 4, 1, 3)]  # deliberately out of order
    patcher, _ = mock_db([(None, candidate_rows)])

    with patcher:
        result = repo.get_all_jobs_from_db(
            query="anything", page=1, limit=10, sort_by=sort_by, sort_order=sort_order
        )

    return [row["id"] for row in result["results"]]


def test_hybrid_relevance_descending_sorts_by_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "relevance", "desc") == [4, 3, 2, 1]


def test_hybrid_relevance_ascending_sorts_by_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "relevance", "asc") == [1, 2, 3, 4]


def test_hybrid_last_seen_at_descending_ignores_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "last_seen_at", "desc") == [4, 3, 2, 1]


def test_hybrid_last_seen_at_ascending_ignores_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "last_seen_at", "asc") == [1, 2, 3, 4]


def test_hybrid_posted_date_descending_ignores_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "date_posted", "desc") == [4, 3, 2, 1]


def test_hybrid_posted_date_ascending_ignores_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "date_posted", "asc") == [1, 2, 3, 4]


def test_hybrid_posted_date_at_canonical_descending_ignores_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "date_posted_at", "desc") == [4, 3, 2, 1]


def test_hybrid_posted_date_at_canonical_ascending_ignores_score(monkeypatch):
    assert _run_hybrid_sort(monkeypatch, "date_posted_at", "asc") == [1, 2, 3, 4]


def test_hybrid_sort_accepts_both_date_posted_aliases_resolving_to_date_posted_at(monkeypatch):
    """Both the canonical "date_posted_at" and the legacy "date_posted"
    compatibility alias must resolve to the identical underlying
    date_posted_at column via the single shared ALLOWED_SORT_FIELDS
    allowlist -- proven both structurally (the dict mapping) and
    behaviorally (both external values produce the exact same order)."""
    assert "date_posted_at" in repo.ALLOWED_SORT_FIELDS
    assert repo.ALLOWED_SORT_FIELDS["date_posted_at"] == "date_posted_at"
    assert "date_posted" in repo.ALLOWED_SORT_FIELDS
    assert repo.ALLOWED_SORT_FIELDS["date_posted"] == "date_posted_at"

    for sort_order in ("asc", "desc"):
        canonical_order = _run_hybrid_sort(monkeypatch, "date_posted_at", sort_order)
        alias_order = _run_hybrid_sort(monkeypatch, "date_posted", sort_order)
        assert canonical_order == alias_order, (
            f"date_posted_at and date_posted must match for sort_order={sort_order}"
        )


def test_hybrid_deterministic_tie_break_across_pages(monkeypatch):
    """Multiple rows with an IDENTICAL last_seen_at must still produce a
    stable, non-overlapping split across pages, tie-broken by id."""
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.5)
    monkeypatch.setattr(repo, "calculate_quality_score", lambda row: 0.0)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.0")

    # All 14 rows share the exact same last_seen_at -- only id can break ties.
    tied_rows = [make_job(id=i, last_seen_days_ago=0, posted_hours_ago=1) for i in range(1, 15)]

    patcher1, _ = mock_db([(None, list(tied_rows))])
    with patcher1:
        page_1 = repo.get_all_jobs_from_db(
            query="anything", page=1, limit=10, sort_by="last_seen_at", sort_order="desc"
        )

    patcher2, _ = mock_db([(None, list(tied_rows))])
    with patcher2:
        page_2 = repo.get_all_jobs_from_db(
            query="anything", page=2, limit=10, sort_by="last_seen_at", sort_order="desc"
        )

    page_1_ids = [row["id"] for row in page_1["results"]]
    page_2_ids = [row["id"] for row in page_2["results"]]

    assert page_1_ids == list(range(14, 4, -1))  # ids 14..5, descending tie-break
    assert page_2_ids == [4, 3, 2, 1]
    assert set(page_1_ids).isdisjoint(page_2_ids), "no duplicates across pages"
    assert set(page_1_ids) | set(page_2_ids) == {row["id"] for row in tied_rows}, "no gaps"


def test_hybrid_sort_runs_after_relevance_and_quality_but_before_slicing(monkeypatch):
    """A row that would be ranked first by raw score, but fails the quality
    gate, must never appear -- proving quality filtering happens before the
    final sort/slice, not after."""
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.5)
    monkeypatch.setattr(repo, "calculate_quality_score", lambda row: 0.0)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.5")

    # id=1 has the most recent last_seen_at (would sort first for
    # sort_by=last_seen_at) but fails the quality gate; id=2 is older but
    # eligible.
    ineligible_newest = make_job(id=1, last_seen_days_ago=0, posted_hours_ago=1)
    eligible_older = make_job(id=2, last_seen_days_ago=5, posted_hours_ago=1)

    monkeypatch.setattr(
        repo, "text_relevance_quality_score",
        lambda query, job: (0.1, []) if job["id"] == 1 else (0.9, []),
    )

    patcher, _ = mock_db([(None, [ineligible_newest, eligible_older])])
    with patcher:
        result = repo.get_all_jobs_from_db(
            query="anything", page=1, limit=10, sort_by="last_seen_at", sort_order="desc"
        )

    result_ids = [row["id"] for row in result["results"]]
    assert result_ids == [2], "the ineligible row must never reach the final sort/slice"


# --- Safe environment parsing for the quality threshold ---


def test_safe_quality_threshold_default_when_unset(monkeypatch):
    monkeypatch.delenv("JOB_SEARCH_MIN_QUALITY_SCORE", raising=False)
    assert repo.get_safe_quality_threshold() == 0.18


def test_safe_quality_threshold_falls_back_on_malformed_value(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "not-a-number")
    assert repo.get_safe_quality_threshold() == 0.18


def test_safe_quality_threshold_trims_whitespace(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "  0.42  ")
    assert repo.get_safe_quality_threshold() == pytest.approx(0.42)


def test_safe_quality_threshold_clamps_negative_to_zero(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "-5")
    assert repo.get_safe_quality_threshold() == 0.0


def test_safe_quality_threshold_clamps_above_one_to_one(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "3.7")
    assert repo.get_safe_quality_threshold() == 1.0


def test_malformed_quality_threshold_does_not_crash_filters_only_search(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "garbage")

    patcher, _ = mock_db([({"total": 0}, None), (None, [])])
    with patcher:
        result = repo.get_all_jobs_from_db(page=1, limit=10)  # must not raise

    assert result["total"] == 0


def test_malformed_quality_threshold_does_not_crash_hybrid_search(monkeypatch):
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.9)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.9)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "not-a-number")

    patcher, _ = mock_db([(None, [make_job(id=1, last_seen_days_ago=0, posted_hours_ago=1)])])
    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=10)  # must not raise

    assert isinstance(result["total"], int)


def test_filters_only_and_hybrid_use_the_same_safe_threshold_parser(monkeypatch):
    """Both call sites must go through get_safe_quality_threshold() -- there
    must not be a second, independent parser for the same setting."""
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.77")
    assert repo.get_safe_quality_threshold() == pytest.approx(0.77)

    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
    with patcher:
        repo.get_all_jobs_from_db(page=1, limit=10)

    _, select_params = cursor.calls[1]
    assert pytest.approx(0.77) in select_params


def test_quality_filter_disabled_skips_the_gate_but_keeps_relevance_results(monkeypatch):
    """JOB_SEARCH_FILTER_LOW_QUALITY=false must skip the quality gate
    entirely while the relevance-thresholded, sorted results still come
    through untouched."""
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.9)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.9)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_FILTER_LOW_QUALITY", "false")

    def fail_if_called(query, job):
        raise AssertionError("text_relevance_quality_score must not be called when disabled")

    # A very strict threshold would normally reject everything -- but the
    # gate must be skipped entirely, and never even invoked, when disabled.
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "1.0")

    row = make_job(id=1, last_seen_days_ago=0, posted_hours_ago=1)
    patcher, _ = mock_db([(None, [row])])

    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=10)

    result_ids = [r["id"] for r in result["results"]]
    assert result_ids == [1]
    # quality_score/quality_reasons are still attached for API-response
    # consistency, even though they did not gate eligibility.
    assert "quality_score" in result["results"][0]


# --- Filters-only quality gate: an intentional new default behavior ---


def test_filters_only_job_below_quality_threshold_is_excluded_before_pagination():
    """JOB_SEARCH_FILTER_LOW_QUALITY defaults to true, so query-less
    browsing now also drops jobs below the intrinsic quality threshold --
    proven here via the actual WHERE-bound COUNT/SELECT contract (the fake
    cursor's canned rows represent what Postgres would already have
    excluded, since the eligibility condition is a real WHERE clause)."""
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])

    with patcher:
        repo.get_all_jobs_from_db(page=1, limit=10)

    select_sql, select_params = cursor.calls[1]
    assert repo.QUALITY_SCORE_SQL in select_sql, (
        "the intrinsic quality eligibility condition must be present in the "
        "SELECT so a below-threshold job is excluded before LIMIT/OFFSET"
    )
    assert 0.18 in select_params


def test_filters_only_job_below_quality_threshold_is_retained_when_disabled(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_FILTER_LOW_QUALITY", "false")

    patcher, cursor = mock_db([({"total": 5}, None), (None, [{"id": 1}])])
    with patcher:
        result = repo.get_all_jobs_from_db(page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    assert repo.QUALITY_SCORE_SQL not in select_sql
    assert result["total"] == 5


# --- Shared sort allowlist: get_safe_sort_clause, filters-only SQL, and
# hybrid sort_ranked_candidates must never diverge, and must never let a
# sort_by value reach raw SQL text unallowlisted ---


def test_get_safe_sort_clause_null_last_for_date_posted_both_directions():
    assert repo.get_safe_sort_clause("date_posted_at", "asc") == "date_posted_at ASC NULLS LAST"
    assert repo.get_safe_sort_clause("date_posted_at", "desc") == "date_posted_at DESC NULLS LAST"
    assert repo.get_safe_sort_clause("date_posted", "asc") == "date_posted_at ASC NULLS LAST"
    assert repo.get_safe_sort_clause("date_posted", "desc") == "date_posted_at DESC NULLS LAST"


def test_get_safe_sort_clause_invalid_sort_by_falls_back_deterministically():
    assert repo.get_safe_sort_clause("'; DROP TABLE jobs; --", "desc") == "last_seen_at DESC NULLS LAST"
    assert repo.get_safe_sort_clause(None, None) == "last_seen_at DESC NULLS LAST"
    assert repo.get_safe_sort_clause("", "bogus") == "last_seen_at DESC NULLS LAST"


def test_filters_only_invalid_sort_by_falls_back_to_last_seen_at_without_interpolation():
    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
    malicious_sort_by = "id; DROP TABLE jobs; --"

    with patcher:
        repo.get_all_jobs_from_db(sort_by=malicious_sort_by, sort_order="desc", page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    assert malicious_sort_by not in select_sql
    order_by_clause = select_sql[select_sql.index("ORDER BY"):select_sql.index("LIMIT")]
    assert "j.last_seen_at" in order_by_clause, "unrecognized sort_by must fall back to last_seen_at"


@pytest.mark.parametrize("malicious_sort_by", [
    "id; DROP TABLE jobs; --",
    "(SELECT 1)",
    "last_seen_at, pg_sleep(5)",
    "unknown_column",
])
def test_filters_only_order_by_column_is_always_allowlisted(malicious_sort_by):
    """No sort_by value -- valid or attacker-controlled -- may cause the
    generated ORDER BY to reference a column outside the shared
    ALLOWED_SORT_FIELDS allowlist (plus the fixed id tie-break)."""
    allowlisted_columns = {f"j.{column}" for column in set(repo.ALLOWED_SORT_FIELDS.values())}
    allowlisted_columns.add("j.id")

    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
    with patcher:
        repo.get_all_jobs_from_db(sort_by=malicious_sort_by, sort_order="desc", page=1, limit=10)

    select_sql, _ = cursor.calls[1]
    order_by_clause = select_sql[select_sql.index("ORDER BY"):select_sql.index("LIMIT")]
    used_column = order_by_clause.split()[2].rstrip(",")
    assert used_column in allowlisted_columns, (
        f"sort_by={malicious_sort_by!r} produced unallowlisted ORDER BY column {used_column!r}"
    )


def test_hybrid_invalid_sort_by_falls_back_to_last_seen_at(monkeypatch):
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.0)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.5)
    monkeypatch.setattr(repo, "calculate_quality_score", lambda row: 0.0)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "0.0")

    rows_by_id = _hybrid_scored_rows_with_distinct_signals()
    candidate_rows = [rows_by_id[i] for i in (2, 4, 1, 3)]  # deliberately out of order
    patcher, _ = mock_db([(None, candidate_rows)])

    with patcher:
        result = repo.get_all_jobs_from_db(
            query="anything", page=1, limit=10,
            sort_by="not_a_real_field; DROP TABLE jobs;", sort_order="desc",
        )

    result_ids = [row["id"] for row in result["results"]]
    assert result_ids == [4, 3, 2, 1], "unrecognized sort_by must fall back to last_seen_at desc"


def test_sort_ranked_candidates_invalid_sort_by_falls_back_to_last_seen_at():
    ranked = [
        (0.1, {"id": 1, "last_seen_at": NOW - timedelta(days=2)}),
        (0.9, {"id": 2, "last_seen_at": NOW}),
        (0.5, {"id": 3, "last_seen_at": NOW - timedelta(days=1)}),
    ]
    ordered = repo.sort_ranked_candidates(list(ranked), "totally_bogus_field", "desc")
    assert [row["id"] for _, row in ordered] == [2, 3, 1]


# --- Null posted-time handling must match between SQL (NULLS LAST) and the
# hybrid path's Python sort, for both sort_by aliases and both directions ---


@pytest.mark.parametrize("sort_by", ["date_posted_at", "date_posted"])
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_sort_ranked_candidates_null_posted_time_always_last(sort_by, sort_order):
    ranked = [
        (0.5, {"id": 1, "date_posted_at": NOW - timedelta(days=5)}),
        (0.5, {"id": 2, "date_posted_at": None}),
        (0.5, {"id": 3, "date_posted_at": NOW - timedelta(days=1)}),
    ]
    ordered = repo.sort_ranked_candidates(list(ranked), sort_by, sort_order)
    ordered_ids = [row["id"] for _, row in ordered]
    assert ordered_ids[-1] == 2, (
        f"null date_posted_at must sort last for sort_by={sort_by}, sort_order={sort_order}"
    )
    assert set(ordered_ids) == {1, 2, 3}


def test_filters_only_null_posted_time_sql_uses_nulls_last_both_directions():
    """NULLS LAST is unconditional in the generated SQL (not dependent on
    ASC vs DESC), which is what makes null date_posted_at rows sort last
    for both directions in Postgres -- this pins the SQL text itself, since
    the fake cursor cannot execute real NULLS LAST ordering semantics."""
    for sort_order, direction in [("asc", "ASC"), ("desc", "DESC")]:
        patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
        with patcher:
            repo.get_all_jobs_from_db(sort_by="date_posted_at", sort_order=sort_order, page=1, limit=10)

        select_sql, _ = cursor.calls[1]
        assert f"j.date_posted_at {direction} NULLS LAST" in select_sql


# --- README / API_QUICKSTART must document the actual sort contract ---


def test_readme_and_quickstart_document_canonical_and_alias_sort_fields():
    readme = (REPO_ROOT / "README.md").read_text()
    quickstart = (REPO_ROOT / "docs" / "API_QUICKSTART.md").read_text()

    for doc_name, doc_text in [("README.md", readme), ("docs/API_QUICKSTART.md", quickstart)]:
        assert "date_posted_at" in doc_text, (
            f"{doc_name} must document the canonical date_posted_at sort field"
        )
        assert re.search(r"sort_by=date_posted(?!_at)", doc_text), (
            f"{doc_name} must show an example using the date_posted compatibility alias"
        )
        assert "sort_order=asc" in doc_text, f"{doc_name} must show an ascending sort example"
        assert "sort_order=desc" in doc_text, f"{doc_name} must show a descending sort example"
        assert "candidate pool" in doc_text, (
            f"{doc_name} must document that hybrid sorting/totals operate over the "
            "bounded evaluated candidate pool"
        )


# --- Quality threshold hardening: math.isfinite() must reject NaN/Infinity,
# which float() parses without raising ---


@pytest.mark.parametrize("raw_value", [
    "nan", "NaN", "NAN", "-nan",
    "inf", "Infinity", "-Infinity", "-inf", "infinity",
])
def test_safe_quality_threshold_falls_back_on_non_finite_value(monkeypatch, raw_value):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", raw_value)
    assert repo.get_safe_quality_threshold() == 0.18


def test_safe_quality_threshold_falls_back_on_empty_value(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "")
    assert repo.get_safe_quality_threshold() == 0.18


def test_safe_quality_threshold_falls_back_on_whitespace_only_value(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "   ")
    assert repo.get_safe_quality_threshold() == 0.18


def test_malformed_quality_threshold_nan_does_not_crash_filters_only_search(monkeypatch):
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "nan")

    patcher, cursor = mock_db([({"total": 0}, None), (None, [])])
    with patcher:
        result = repo.get_all_jobs_from_db(page=1, limit=10)  # must not raise

    _, select_params = cursor.calls[1]
    assert 0.18 in select_params, "NaN must fall back to the 0.18 default bound parameter"
    assert result["total"] == 0


def test_malformed_quality_threshold_infinity_does_not_crash_hybrid_search(monkeypatch):
    monkeypatch.setattr(repo, "should_use_semantic_query", lambda q: False)
    monkeypatch.setattr(repo, "record_job_search_event", lambda **kwargs: None)
    monkeypatch.setattr(repo, "calculate_title_match_score", lambda row, query: 0.9)
    monkeypatch.setattr(repo, "calculate_keyword_score", lambda row, query: 0.9)
    monkeypatch.setenv("JOB_SEARCH_KEYWORD_MIN_RELEVANCE_SCORE", "0.0")
    monkeypatch.setenv("JOB_SEARCH_MIN_QUALITY_SCORE", "Infinity")

    patcher, _ = mock_db([(None, [make_job(id=1, last_seen_days_ago=0, posted_hours_ago=1)])])
    with patcher:
        result = repo.get_all_jobs_from_db(query="anything", page=1, limit=10)  # must not raise

    assert isinstance(result["total"], int)


# --- Regression guard: unrelated suites must remain green (executed via
# the full `pytest tests/ -v` run; this file focuses on search ranking) ---
