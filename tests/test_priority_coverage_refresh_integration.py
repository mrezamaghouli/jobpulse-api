"""
Real PostgreSQL 16 integration evidence for the LinkedIn ingestion
continuous-collection hotfix: scripts/seed_priority_coverage_queue.py's
new recurring 'done'-coverage eligibility, and
scripts/reconcile_priority_coverage.py's 'queued' -> 'done' reconciliation
(including the exact stuck-row shape observed in production: 3561
job_collection_coverage rows at status='queued' whose linked
job_search_demand_queue row is already status='done').

Same discipline and gating as tests/test_upsert_returning_integration.py:
no mocks for the SQL under test, a disposable schema matching the real
migration DDL in scripts/migrate_database.py (migration
008_priority_job_country_catalog and the job_search_demand_queue table
from migration 003/004), gated on JOBPULSE_TEST_POSTGRES_DSN, SKIPPED
(not silently passed) when that variable is unset. No test in this file
ever connects to production, sends a LinkedIn request, or runs a real
collection cycle.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_DSN_ENV_VAR = "JOBPULSE_TEST_POSTGRES_DSN"

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_DSN_ENV_VAR),
    reason=(
        f"No local PostgreSQL 16 binaries were found in this environment, and "
        f"{TEST_DSN_ENV_VAR} is not set to a disposable test database DSN. "
        f"This is a real, unexecuted blocker -- NOT evidence the recurring-"
        f"refresh/reconciliation logic works against real PostgreSQL 16. Set "
        f"{TEST_DSN_ENV_VAR} to a disposable database to run this file for "
        f"real, or rely on a CI job that provides a PostgreSQL 16 service "
        f"container."
    ),
)


@pytest.fixture
def pg(monkeypatch):
    """Real psycopg2 connection against a disposable schema matching the
    production DDL for job_catalog_titles / job_catalog_countries /
    job_collection_coverage / job_search_demand_queue / a minimal `jobs`
    (only COUNT(*) is ever used against it by the scripts under test)."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    dsn = os.environ[TEST_DSN_ENV_VAR]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    with conn.cursor() as cur:
        for stmt in (
            "DROP TABLE IF EXISTS job_search_demand_queue CASCADE;",
            "DROP TABLE IF EXISTS job_collection_coverage CASCADE;",
            "DROP TABLE IF EXISTS job_catalog_titles CASCADE;",
            "DROP TABLE IF EXISTS job_catalog_countries CASCADE;",
            "DROP TABLE IF EXISTS jobs CASCADE;",
        ):
            cur.execute(stmt)

        cur.execute("CREATE TABLE jobs (id SERIAL PRIMARY KEY);")

        cur.execute(
            """
            CREATE TABLE job_catalog_titles (
                id BIGSERIAL PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT UNIQUE NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE job_catalog_countries (
                id BIGSERIAL PRIMARY KEY,
                country_name TEXT NOT NULL,
                normalized_country TEXT UNIQUE NOT NULL,
                priority INTEGER NOT NULL DEFAULT 99,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE job_collection_coverage (
                id BIGSERIAL PRIMARY KEY,
                job_title_id BIGINT NOT NULL REFERENCES job_catalog_titles(id) ON DELETE CASCADE,
                country_id BIGINT NOT NULL REFERENCES job_catalog_countries(id) ON DELETE CASCADE,
                search_query TEXT NOT NULL,
                linkedin_location TEXT NOT NULL,
                country_priority INTEGER NOT NULL DEFAULT 99,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_queued_at TIMESTAMPTZ,
                last_collected_at TIMESTAMPTZ,
                jobs_count_before INTEGER,
                jobs_count_after INTEGER,
                jobs_delta INTEGER,
                last_error TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ,
                UNIQUE(job_title_id, country_id)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE job_search_demand_queue (
                id BIGSERIAL PRIMARY KEY,
                raw_query TEXT NOT NULL,
                normalized_query TEXT NOT NULL UNIQUE,
                job_family TEXT DEFAULT 'General',
                filters_json JSONB DEFAULT '{}'::jsonb,
                search_count INTEGER DEFAULT 0,
                zero_result_count INTEGER DEFAULT 0,
                low_result_count INTEGER DEFAULT 0,
                last_result_count INTEGER DEFAULT 0,
                priority_score DOUBLE PRECISION DEFAULT 0,
                status TEXT DEFAULT 'pending',
                locked_at TIMESTAMPTZ,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_collected_at TIMESTAMPTZ,
                fail_count INTEGER DEFAULT 0,
                last_error TEXT
            );
            """
        )
    conn.commit()

    # Both scripts under test call their own module-level connect(), which
    # reads app.config.get_postgres_config() -- monkeypatch each script's
    # connect() to open a FRESH connection to the same disposable DSN
    # instead (never the real config-driven target). A fresh connection
    # per call -- not the shared fixture `conn` below -- because
    # reconcile_priority_coverage.main() unconditionally closes whatever
    # connection it opened in its own `finally: conn.close()`; handing it
    # the fixture's shared connection would close that out from under
    # the rest of this fixture. This also mirrors production more
    # closely: each `python -m scripts.X` invocation really does open its
    # own connection.
    import scripts.seed_priority_coverage_queue as seed_mod
    import scripts.reconcile_priority_coverage as reconcile_mod

    monkeypatch.setattr(seed_mod, "connect", lambda: psycopg2.connect(dsn))
    monkeypatch.setattr(reconcile_mod, "connect", lambda: psycopg2.connect(dsn))

    yield conn, seed_mod, reconcile_mod, RealDictCursor

    conn.rollback()
    conn.close()


def _insert_title(conn, cursor_factory, category="Engineering", title="Backend Engineer", is_active=True):
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        cur.execute(
            "INSERT INTO job_catalog_titles (category, title, normalized_title, is_active) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (category, title, title.lower().replace(" ", "_"), is_active),
        )
        title_id = cur.fetchone()["id"]
    conn.commit()
    return title_id


def _insert_country(conn, cursor_factory, name="Germany", priority=1, is_active=True):
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        cur.execute(
            "INSERT INTO job_catalog_countries (country_name, normalized_country, priority, is_active) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (name, name.lower(), priority, is_active),
        )
        country_id = cur.fetchone()["id"]
    conn.commit()
    return country_id


def _insert_coverage(conn, cursor_factory, title_id, country_id, *, status="pending",
                      country_priority=1, last_queued_at=None, last_collected_at=None,
                      search_query="Backend Engineer", linkedin_location="Germany"):
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        cur.execute(
            """
            INSERT INTO job_collection_coverage
                (job_title_id, country_id, search_query, linkedin_location,
                 country_priority, status, last_queued_at, last_collected_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (title_id, country_id, search_query, linkedin_location,
             country_priority, status, last_queued_at, last_collected_at),
        )
        coverage_id = cur.fetchone()["id"]
    conn.commit()
    return coverage_id


def _insert_demand_row(conn, cursor_factory, coverage_id, *, status="done", normalized_query=None):
    """Mirrors the exact identity enqueue_task() itself produces --
    coverage:{coverage_id}:{normalized title}:{normalized country} -- so
    reconcile_priority_coverage's `filters_json->>'coverage_id'` join
    finds this row the same way it would find a real one."""
    import json
    normalized_query = normalized_query or f"coverage:{coverage_id}:backend engineer:germany"
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        cur.execute(
            """
            INSERT INTO job_search_demand_queue
                (raw_query, normalized_query, status, filters_json)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            ("Backend Engineer", normalized_query, status, json.dumps({"coverage_id": coverage_id})),
        )
        demand_id = cur.fetchone()["id"]
    conn.commit()
    return demand_id


def _coverage_status(conn, cursor_factory, coverage_id):
    with conn.cursor(cursor_factory=cursor_factory) as cur:
        cur.execute("SELECT status FROM job_collection_coverage WHERE id = %s", (coverage_id,))
        return cur.fetchone()["status"]


# =====================================================================
# C/E. recurring completed-coverage eligibility: a 'done' coverage row
# whose last_collected_at is older than the refresh interval IS eligible.
# =====================================================================
def test_stale_done_coverage_is_eligible_for_reseeding(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    stale_at = datetime.now(timezone.utc) - timedelta(hours=48)
    coverage_id = _insert_coverage(
        conn, cf, title_id, country_id, status="done", last_collected_at=stale_at,
    )

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        assert priority == 1
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    assert [t["coverage_id"] for t in tasks] == [coverage_id]


def test_done_coverage_with_null_last_collected_at_is_eligible(pg):
    """A 'done' row that somehow never got last_collected_at populated
    must not be permanently excluded -- NULL is treated as eligible, the
    same convention already used for last_queued_at IS NULL."""
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    coverage_id = _insert_coverage(conn, cf, title_id, country_id, status="done", last_collected_at=None)

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    assert [t["coverage_id"] for t in tasks] == [coverage_id]


# =====================================================================
# D. fresh completed coverage is NOT requeued.
# =====================================================================
def test_fresh_done_coverage_is_not_reseeded(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    fresh_at = datetime.now(timezone.utc) - timedelta(hours=1)
    _insert_coverage(conn, cf, title_id, country_id, status="done", last_collected_at=fresh_at)

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
    conn.commit()

    assert priority is None  # nothing eligible at all


# =====================================================================
# F. queued/running coverage is never duplicated -- excluded regardless
# of how old their timestamps are.
# =====================================================================
@pytest.mark.parametrize("status", ["queued", "running"])
def test_actively_in_flight_coverage_is_never_selected(pg, status):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    ancient = datetime.now(timezone.utc) - timedelta(days=30)
    _insert_coverage(
        conn, cf, title_id, country_id, status=status,
        last_queued_at=ancient, last_collected_at=ancient,
    )

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
    conn.commit()

    assert priority is None


# =====================================================================
# G. retry_later behavior remains intact (unchanged from before this fix).
# =====================================================================
def test_retry_later_still_gated_by_last_queued_at(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    fresh_id = _insert_coverage(
        conn, cf, title_id, country_id, status="retry_later",
        last_queued_at=datetime.now(timezone.utc) - timedelta(hours=1),
        search_query="Fresh", linkedin_location="Germany",
    )
    title_id2 = _insert_title(conn, cf, title="Frontend Engineer")
    stale_id = _insert_coverage(
        conn, cf, title_id2, country_id, status="retry_later",
        last_queued_at=datetime.now(timezone.utc) - timedelta(hours=48),
        search_query="Stale", linkedin_location="Germany",
    )

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    ids = {t["coverage_id"] for t in tasks}
    assert stale_id in ids
    assert fresh_id not in ids


# =====================================================================
# H. country priority ordering remains intact.
# =====================================================================
def test_country_priority_ordering_preserved(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_high = _insert_country(conn, cf, name="Germany", priority=1)
    country_low = _insert_country(conn, cf, name="Brazil", priority=5)
    _insert_coverage(conn, cf, title_id, country_low, status="pending", country_priority=2,
                      search_query="X", linkedin_location="Brazil")
    high_id = _insert_coverage(conn, cf, title_id, country_high, status="pending", country_priority=1,
                                search_query="Y", linkedin_location="Germany")

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        assert priority == 1  # the lower (higher-priority) tier is picked first
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    assert [t["coverage_id"] for t in tasks] == [high_id]


# =====================================================================
# I. bounded seed limit remains.
# =====================================================================
def test_seed_limit_is_bounded(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    for i in range(5):
        title = _insert_title(conn, cf, title=f"Title {i}")
        _insert_coverage(conn, cf, title, country_id, status="pending",
                          search_query=f"Q{i}", linkedin_location="Germany")

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        tasks = seed_mod.select_tasks(cur, priority, limit=2, retry_after_hours=24)
    conn.commit()

    assert len(tasks) == 2


# =====================================================================
# J/M. ON CONFLICT identity remains deterministic; re-running seeding
# against the SAME still-fresh 'done' row does not grow the demand queue
# (no unbounded backlog): once seeded, the coverage row is marked
# 'queued', which is immediately excluded from further selection.
# =====================================================================
def test_seeding_a_row_marks_it_queued_and_is_then_excluded(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    stale_at = datetime.now(timezone.utc) - timedelta(hours=48)
    coverage_id = _insert_coverage(conn, cf, title_id, country_id, status="done", last_collected_at=stale_at)

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        before_count = seed_mod.jobs_count(cur)
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
        for task in tasks:
            seed_mod.enqueue_task(cur, task, before_count)
    conn.commit()

    assert _coverage_status(conn, cf, coverage_id) == "queued"

    with conn.cursor(cursor_factory=cf) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM job_search_demand_queue")
        assert cur.fetchone()["n"] == 1

        # Re-running seeding immediately must select nothing further --
        # the row is now 'queued', not 'done' or 'pending'/'retry_later'.
        priority2 = seed_mod.get_current_priority(cur, retry_after_hours=24)
        assert priority2 is None
    conn.commit()


def test_reenqueue_of_a_previously_seeded_coverage_id_does_not_duplicate(pg):
    """If the same coverage_id becomes eligible again later (e.g. after
    reconcile flips it back to 'done' and it goes stale once more),
    enqueue_task's normalized_query is a deterministic function of
    coverage_id/title/country, so ON CONFLICT (normalized_query) updates
    the SAME demand-queue row rather than inserting a second one."""
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    coverage_id = _insert_coverage(conn, cf, title_id, country_id, status="done",
                                    last_collected_at=datetime.now(timezone.utc) - timedelta(hours=48))

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        before_count = seed_mod.jobs_count(cur)
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
        for task in tasks:
            seed_mod.enqueue_task(cur, task, before_count)
    conn.commit()

    # Simulate the row completing and going stale again, exactly as
    # reconcile_priority_coverage.py would leave it.
    with conn.cursor(cursor_factory=cf) as cur:
        cur.execute(
            "UPDATE job_collection_coverage SET status='done', "
            "last_collected_at = %s WHERE id = %s",
            (datetime.now(timezone.utc) - timedelta(hours=48), coverage_id),
        )
        cur.execute(
            "UPDATE job_search_demand_queue SET status='done' "
            "WHERE filters_json->>'coverage_id' = %s",
            (str(coverage_id),),
        )
    conn.commit()

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        before_count = seed_mod.jobs_count(cur)
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
        for task in tasks:
            seed_mod.enqueue_task(cur, task, before_count)
    conn.commit()

    with conn.cursor(cursor_factory=cf) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM job_search_demand_queue")
        assert cur.fetchone()["n"] == 1  # still exactly one row, not two


# =====================================================================
# K. reconciliation still converts completed queue work correctly.
# =====================================================================
def test_reconcile_converts_queued_coverage_to_done_when_demand_row_is_done(pg):
    conn, _seed_mod, reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    coverage_id = _insert_coverage(conn, cf, title_id, country_id, status="queued",
                                    last_queued_at=datetime.now(timezone.utc))
    _insert_demand_row(conn, cf, coverage_id, status="done")

    import sys as _sys
    old_argv = _sys.argv
    try:
        _sys.argv = ["reconcile_priority_coverage.py"]
        reconcile_mod.main()
    finally:
        _sys.argv = old_argv

    assert _coverage_status(conn, cf, coverage_id) == "done"


# =====================================================================
# L. first post-fix recovery path for the exact production-observed
# stuck shape: many 'queued' coverage rows whose linked demand-queue row
# is already 'done'. Proves reconciliation alone (no manual UPDATE)
# repairs all of them in a single pass.
# =====================================================================
def test_recovery_of_stuck_queued_rows_with_done_demand_counterparts(pg):
    conn, _seed_mod, reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)

    coverage_ids = []
    for i in range(25):  # scaled-down stand-in for the real 3561
        t = _insert_title(conn, cf, title=f"Stuck Title {i}")
        cov_id = _insert_coverage(
            conn, cf, t, country_id, status="queued",
            last_queued_at=datetime.now(timezone.utc) - timedelta(days=10),
            search_query=f"Stuck {i}", linkedin_location="Germany",
        )
        _insert_demand_row(conn, cf, cov_id, status="done",
                            normalized_query=f"coverage:{cov_id}:stuck {i}:germany")
        coverage_ids.append(cov_id)

    import sys as _sys
    old_argv = _sys.argv
    try:
        _sys.argv = ["reconcile_priority_coverage.py"]
        reconcile_mod.main()
    finally:
        _sys.argv = old_argv

    with conn.cursor(cursor_factory=cf) as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM job_collection_coverage WHERE status = 'done' AND id = ANY(%s)",
            (coverage_ids,),
        )
        assert cur.fetchone()["n"] == len(coverage_ids)
        cur.execute(
            "SELECT COUNT(*) AS n FROM job_collection_coverage WHERE status = 'queued' AND id = ANY(%s)",
            (coverage_ids,),
        )
        assert cur.fetchone()["n"] == 0


# =====================================================================
# M. no unbounded queue growth: a full seed -> (simulated completion) ->
# reconcile -> stale -> seed cycle over several iterations never leaves
# more than one demand-queue row per coverage_id.
# =====================================================================
def test_no_unbounded_backlog_across_multiple_refresh_cycles(pg):
    conn, seed_mod, reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    country_id = _insert_country(conn, cf)
    coverage_id = _insert_coverage(conn, cf, title_id, country_id, status="done",
                                    last_collected_at=datetime.now(timezone.utc) - timedelta(hours=48))

    import sys as _sys
    old_argv = _sys.argv
    try:
        for _cycle in range(4):
            with conn.cursor(cursor_factory=cf) as cur:
                priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
                if priority is not None:
                    before_count = seed_mod.jobs_count(cur)
                    tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
                    for task in tasks:
                        seed_mod.enqueue_task(cur, task, before_count)
            conn.commit()

            # Simulate the demand-queue row completing, exactly as a real
            # process_search_demand_queue.py run would leave it.
            with conn.cursor(cursor_factory=cf) as cur:
                cur.execute(
                    "UPDATE job_search_demand_queue SET status='done' "
                    "WHERE filters_json->>'coverage_id' = %s",
                    (str(coverage_id),),
                )
            conn.commit()

            _sys.argv = ["reconcile_priority_coverage.py"]
            reconcile_mod.main()

            # Age the row out so the NEXT cycle's seed step considers it
            # eligible again (this test proves boundedness across many
            # cycles, not real wall-clock waiting).
            with conn.cursor(cursor_factory=cf) as cur:
                cur.execute(
                    "UPDATE job_collection_coverage SET last_collected_at = %s WHERE id = %s",
                    (datetime.now(timezone.utc) - timedelta(hours=48), coverage_id),
                )
            conn.commit()
    finally:
        _sys.argv = old_argv

    with conn.cursor(cursor_factory=cf) as cur:
        cur.execute("SELECT COUNT(*) AS n FROM job_search_demand_queue")
        assert cur.fetchone()["n"] == 1  # never more than one row for this coverage_id


# =====================================================================
# Priority starvation fix: get_current_priority() must use the EXACT
# same eligibility universe (including t.is_active/c.is_active) as
# select_tasks(), so a stale-but-inactive-title/country row can never
# win MIN(country_priority) only for select_tasks() to then correctly
# exclude it and return zero rows -- starving every real eligible row at
# a later priority tier.
# =====================================================================

# A. an inactive stale priority-1 done row does NOT block an active
#    eligible priority-2 row.
def test_inactive_stale_done_priority_1_does_not_starve_active_priority_2(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    stale = datetime.now(timezone.utc) - timedelta(hours=48)

    inactive_title = _insert_title(conn, cf, title="Ghost Role", is_active=False)
    country_p1 = _insert_country(conn, cf, name="Germany", priority=1)
    _insert_coverage(
        conn, cf, inactive_title, country_p1, status="done", country_priority=1,
        last_collected_at=stale, search_query="Ghost", linkedin_location="Germany",
    )

    active_title = _insert_title(conn, cf, title="Real Role")
    country_p2 = _insert_country(conn, cf, name="Brazil", priority=5)
    real_id = _insert_coverage(
        conn, cf, active_title, country_p2, status="done", country_priority=2,
        last_collected_at=stale, search_query="Real", linkedin_location="Brazil",
    )

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        # Must skip straight to priority=2 -- NOT return 1 and then find
        # nothing (the pre-fix starvation bug).
        assert priority == 2
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    assert [t["coverage_id"] for t in tasks] == [real_id]


# B. an inactive pending/retry_later priority-1 row also does NOT block
#    active later priorities.
def test_inactive_pending_priority_1_does_not_starve_active_priority_2(pg):
    conn, seed_mod, _reconcile_mod, cf = pg

    inactive_title = _insert_title(conn, cf, title="Ghost Role 2", is_active=False)
    country_p1 = _insert_country(conn, cf, name="Germany", priority=1)
    _insert_coverage(
        conn, cf, inactive_title, country_p1, status="pending", country_priority=1,
        search_query="Ghost2", linkedin_location="Germany",
    )

    active_title = _insert_title(conn, cf, title="Real Role 2")
    country_p2 = _insert_country(conn, cf, name="Brazil", priority=5)
    real_id = _insert_coverage(
        conn, cf, active_title, country_p2, status="retry_later", country_priority=2,
        last_queued_at=datetime.now(timezone.utc) - timedelta(hours=48),
        search_query="Real2", linkedin_location="Brazil",
    )

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        assert priority == 2
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    assert [t["coverage_id"] for t in tasks] == [real_id]


# C. inactive titles are never selected.
def test_inactive_title_never_selected(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    inactive_title = _insert_title(conn, cf, title="Inactive Title", is_active=False)
    country_id = _insert_country(conn, cf)
    _insert_coverage(conn, cf, inactive_title, country_id, status="pending",
                      search_query="X", linkedin_location="Germany")

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
    conn.commit()

    assert priority is None


# D. inactive countries are never selected.
def test_inactive_country_never_selected(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title_id = _insert_title(conn, cf)
    inactive_country = _insert_country(conn, cf, name="Nowhere", is_active=False)
    _insert_coverage(conn, cf, title_id, inactive_country, status="pending",
                      search_query="Y", linkedin_location="Nowhere")

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
    conn.commit()

    assert priority is None


# E. when active eligible rows exist at priorities 1 and 2, priority 1
#    is still selected first (the starvation fix must not have disturbed
#    ordinary, healthy priority ordering).
def test_active_priority_1_still_selected_before_active_priority_2(pg):
    conn, seed_mod, _reconcile_mod, cf = pg
    title1 = _insert_title(conn, cf, title="Role P1")
    country_p1 = _insert_country(conn, cf, name="Germany", priority=1)
    p1_id = _insert_coverage(conn, cf, title1, country_p1, status="pending", country_priority=1,
                              search_query="P1", linkedin_location="Germany")

    title2 = _insert_title(conn, cf, title="Role P2")
    country_p2 = _insert_country(conn, cf, name="Brazil", priority=5)
    _insert_coverage(conn, cf, title2, country_p2, status="pending", country_priority=2,
                      search_query="P2", linkedin_location="Brazil")

    with conn.cursor(cursor_factory=cf) as cur:
        priority = seed_mod.get_current_priority(cur, retry_after_hours=24)
        assert priority == 1
        tasks = seed_mod.select_tasks(cur, priority, limit=10, retry_after_hours=24)
    conn.commit()

    assert [t["coverage_id"] for t in tasks] == [p1_id]
