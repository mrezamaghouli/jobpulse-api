"""
Real PostgreSQL 16 integration evidence for insert_job()'s
`RETURNING (xmax = 0) AS inserted` distinction (scripts/collector_postgres.py).

WHY THIS FILE EXISTS: every other test in this repository proves only
that `insert_job()` executes the SQL string it constructs and correctly
interprets a MOCKED cursor's `fetchone()` return value. That proves the
Python-side interpretation logic (see tests/test_collector_outcomes.py),
but it does NOT prove that PostgreSQL 16 itself, against the real `jobs`
schema, its constraints, and any triggers, actually returns `xmax = 0`
for a fresh insert and a non-zero xmax for the ON CONFLICT DO UPDATE
branch. `xmax = 0` is a PostgreSQL-SPECIFIC system-column technique, not
a SQL-standard or officially-guaranteed feature (see
scripts/collector_postgres.py's own comment on this) -- its behavior
against THIS schema is exactly the kind of claim that must be backed by
a real database, not a mock, before it can be called deployment-validated.

ENVIRONMENT STATUS AT TIME OF WRITING (see docs/PRODUCTION_RUNBOOK.md for
the current status): no local PostgreSQL binaries (`postgres`, `initdb`,
`pg_ctl`, `psql`) were found in this development/CI sandbox
(`command -v postgres|initdb|pg_ctl|psql` all failed). This file
therefore CANNOT run locally in this environment right now. It is gated
on an explicit, disposable test DSN
(`JOBPULSE_TEST_POSTGRES_DSN`) supplied by the environment -- e.g. a
GitHub Actions PostgreSQL 16 SERVICE container (see .github/workflows/ci.yml,
job `postgres-upsert-integration`) -- and is SKIPPED, not silently
passed, when that variable is unset. No test in this file ever connects
to production, and none uses Docker directly (a CI-provided `services:`
container is not this file using Docker -- it is GitHub Actions
providing a plain TCP-reachable PostgreSQL server, indistinguishable
from any other disposable test database from this file's point of view).

A DISCOVERED, PRE-EXISTING FINDING (not introduced by this pass, but
directly relevant to it): the repository's OWN tracked schema-management
scripts (scripts/repair_jobpulse_schema.py, scripts/migrate_database.py)
create only a non-unique `idx_jobs_job_url` index on `jobs.job_url`, not
a UNIQUE constraint -- `grep` of the whole repository found no
`ADD CONSTRAINT ... UNIQUE` / `UNIQUE INDEX` on `job_url` for the
PostgreSQL schema anywhere (only the LEGACY SQLite schema in
legacy/init_db_sqlite.py has `job_url ... UNIQUE`). `insert_job()`'s
`ON CONFLICT (job_url) DO UPDATE` REQUIRES a unique or exclusion
constraint on `job_url` to be valid SQL at all -- PostgreSQL raises
`there is no unique or exclusion constraint matching the ON CONFLICT
specification` otherwise. This test's own disposable schema explicitly
creates that constraint (matching the task's instruction to create
"the minimum disposable jobs table and matching unique constraint needed
by insert_job()"), so a PASS here does NOT prove production's real
schema has this constraint -- only that the SQL is correct GIVEN the
constraint exists. This must be verified directly against the real
production schema by an operator (e.g. `\\d jobs` or
`SELECT conname FROM pg_constraint WHERE conrelid = 'jobs'::regclass;`)
before this UPSERT-based insert/update distinction can be trusted in
production -- this repository cannot verify it, since connecting to
production is explicitly out of scope for this pass.
"""
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TEST_DSN_ENV_VAR = "JOBPULSE_TEST_POSTGRES_DSN"

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_DSN_ENV_VAR),
    reason=(
        f"No local PostgreSQL 16 binaries were found in this environment "
        f"(command -v postgres/initdb/pg_ctl/psql all failed), and "
        f"{TEST_DSN_ENV_VAR} is not set to a disposable test database DSN. "
        f"This is a real, unexecuted blocker -- NOT evidence the UPSERT "
        f"distinction works against real PostgreSQL 16. Set {TEST_DSN_ENV_VAR} "
        f"to a disposable database (e.g. 'postgresql://postgres:postgres@localhost:5432/jobpulse_upsert_test') "
        f"to run this file for real, or rely on the CI job that provides a "
        f"PostgreSQL 16 service container."
    ),
)


@pytest.fixture
def pg_conn():
    import psycopg2

    dsn = os.environ[TEST_DSN_ENV_VAR]
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    with conn.cursor() as cur:
        # Minimum disposable schema needed by insert_job(): every column
        # its INSERT statement references, plus the UNIQUE constraint its
        # ON CONFLICT target requires. Intentionally NOT the full
        # production `jobs` table (which this repo's own schema scripts
        # don't fully define in one place either) -- just enough for the
        # exact statement under test to be valid, real SQL.
        cur.execute("DROP TABLE IF EXISTS jobs_upsert_test;")
        cur.execute(
            """
            CREATE TABLE jobs_upsert_test (
                id SERIAL PRIMARY KEY,
                linkedin_job_id TEXT,
                title TEXT,
                company TEXT,
                company_linkedin_url TEXT,
                company_logo_url TEXT,
                job_description TEXT,
                job_about TEXT,
                work_mode TEXT,
                date_posted_text TEXT,
                date_posted_at TEXT,
                location TEXT,
                remote BOOLEAN,
                job_type TEXT,
                seniority TEXT,
                salary_min NUMERIC,
                salary_max NUMERIC,
                currency TEXT,
                source TEXT,
                job_url TEXT UNIQUE NOT NULL,
                apply_type TEXT,
                apply_url TEXT,
                apply_label TEXT,
                poster_name TEXT,
                poster_title TEXT,
                poster_profile_url TEXT,
                date_posted TEXT,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE
            );
            """
        )
    conn.commit()

    yield conn

    # Cleanup: drop the disposable table and close -- leaves no
    # persistent state in the test database.
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS jobs_upsert_test;")
    conn.commit()
    conn.close()


UPSERT_SQL = """
    INSERT INTO jobs_upsert_test (
        linkedin_job_id, title, company, job_url, source,
        first_seen_at, last_seen_at, is_active
    )
    VALUES (
        %(linkedin_job_id)s, %(title)s, %(company)s, %(job_url)s, %(source)s,
        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, TRUE
    )
    ON CONFLICT (job_url) DO UPDATE SET
        title = COALESCE(EXCLUDED.title, jobs_upsert_test.title),
        last_seen_at = CURRENT_TIMESTAMP,
        is_active = TRUE
    RETURNING (xmax = 0) AS inserted;
"""


def _upsert(conn, job_url, title="Engineer"):
    with conn.cursor() as cur:
        cur.execute(UPSERT_SQL, {
            "linkedin_job_id": "123", "title": title, "company": "Acme",
            "job_url": job_url, "source": "LinkedIn",
        })
        row = cur.fetchone()
    conn.commit()
    return row


def test_first_upsert_on_a_fresh_row_returns_inserted_true(pg_conn):
    row = _upsert(pg_conn, "https://www.linkedin.com/jobs/view/111111/")
    assert row is not None
    assert row[0] is True
    assert isinstance(row[0], bool)


def test_conflicting_upsert_on_the_same_job_url_returns_inserted_false(pg_conn):
    first = _upsert(pg_conn, "https://www.linkedin.com/jobs/view/222222/", title="Original Title")
    assert first[0] is True

    second = _upsert(pg_conn, "https://www.linkedin.com/jobs/view/222222/", title="Updated Title")
    assert second[0] is False
    assert isinstance(second[0], bool)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT title FROM jobs_upsert_test WHERE job_url = %s;", ("https://www.linkedin.com/jobs/view/222222/",))
        (title,) = cur.fetchone()
    assert title == "Updated Title"


def test_third_upsert_on_the_same_row_still_returns_inserted_false():
    """A row inserted in an EARLIER, separate transaction (already
    committed) must still read back xmax != 0 on a later UPDATE branch --
    this is what distinguishes 'this row's very first version' from 'any
    version', across transaction boundaries, not just within one."""
    import psycopg2
    dsn = os.environ[TEST_DSN_ENV_VAR]

    conn1 = psycopg2.connect(dsn)
    conn1.autocommit = False
    with conn1.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS jobs_upsert_test_2;")
        cur.execute("""
            CREATE TABLE jobs_upsert_test_2 (
                id SERIAL PRIMARY KEY, title TEXT, job_url TEXT UNIQUE NOT NULL
            );
        """)
    conn1.commit()

    try:
        sql = """
            INSERT INTO jobs_upsert_test_2 (title, job_url) VALUES (%(title)s, %(job_url)s)
            ON CONFLICT (job_url) DO UPDATE SET title = EXCLUDED.title
            RETURNING (xmax = 0) AS inserted;
        """
        with conn1.cursor() as cur:
            cur.execute(sql, {"title": "v1", "job_url": "https://example.com/x"})
            (inserted1,) = cur.fetchone()
        conn1.commit()
        assert inserted1 is True

        # Separate, fresh connection -- separate transaction entirely.
        conn2 = psycopg2.connect(dsn)
        conn2.autocommit = False
        try:
            with conn2.cursor() as cur:
                cur.execute(sql, {"title": "v2", "job_url": "https://example.com/x"})
                (inserted2,) = cur.fetchone()
            conn2.commit()
            assert inserted2 is False
        finally:
            conn2.close()
    finally:
        with conn1.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS jobs_upsert_test_2;")
        conn1.commit()
        conn1.close()


def test_rollback_after_upsert_leaves_no_persistent_state(pg_conn):
    """Proves the fixture's own cleanup contract: an aborted transaction
    must not leave a row behind for a later test to observe."""
    with pg_conn.cursor() as cur:
        cur.execute(UPSERT_SQL, {
            "linkedin_job_id": "999", "title": "Should Not Persist", "company": "Acme",
            "job_url": "https://www.linkedin.com/jobs/view/999999/", "source": "LinkedIn",
        })
        row = cur.fetchone()
        assert row[0] is True
    pg_conn.rollback()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM jobs_upsert_test WHERE job_url = %s;", ("https://www.linkedin.com/jobs/view/999999/",))
        (count,) = cur.fetchone()
    assert count == 0


def test_real_insert_job_function_against_real_postgresql(pg_conn, monkeypatch):
    """End-to-end: calls the REAL scripts.collector_postgres.insert_job()
    (not a reimplementation of its SQL) against the real PostgreSQL 16
    connection, by pointing its INSERT at the disposable test table via a
    thin cursor proxy that rewrites `INTO jobs ` -> `INTO jobs_upsert_test `
    and `ON CONFLICT (job_url)` stays identical (same column name)."""
    import scripts.collector_postgres as cp
    import scripts.collector_result as cr

    class TableRewritingCursor:
        def __init__(self, real_cursor):
            self._cursor = real_cursor

        def execute(self, sql, params=None):
            rewritten = sql.replace("INSERT INTO jobs (", "INSERT INTO jobs_upsert_test (")
            rewritten = rewritten.replace("ON CONFLICT (job_url) DO UPDATE SET", "ON CONFLICT (job_url) DO UPDATE SET")
            rewritten = rewritten.replace("jobs.linkedin_job_id", "jobs_upsert_test.linkedin_job_id")
            rewritten = rewritten.replace("jobs.title", "jobs_upsert_test.title")
            rewritten = rewritten.replace("jobs.company", "jobs_upsert_test.company")
            rewritten = rewritten.replace("jobs.company_linkedin_url", "jobs_upsert_test.company_linkedin_url")
            rewritten = rewritten.replace("jobs.company_logo_url", "jobs_upsert_test.company_logo_url")
            rewritten = rewritten.replace("jobs.job_description", "jobs_upsert_test.job_description")
            rewritten = rewritten.replace("jobs.job_about", "jobs_upsert_test.job_about")
            rewritten = rewritten.replace("jobs.work_mode", "jobs_upsert_test.work_mode")
            rewritten = rewritten.replace("jobs.date_posted_text", "jobs_upsert_test.date_posted_text")
            rewritten = rewritten.replace("jobs.date_posted_at", "jobs_upsert_test.date_posted_at")
            rewritten = rewritten.replace("jobs.location", "jobs_upsert_test.location")
            rewritten = rewritten.replace("jobs.remote", "jobs_upsert_test.remote")
            rewritten = rewritten.replace("jobs.job_type", "jobs_upsert_test.job_type")
            rewritten = rewritten.replace("jobs.seniority", "jobs_upsert_test.seniority")
            rewritten = rewritten.replace("jobs.salary_min", "jobs_upsert_test.salary_min")
            rewritten = rewritten.replace("jobs.salary_max", "jobs_upsert_test.salary_max")
            rewritten = rewritten.replace("jobs.currency", "jobs_upsert_test.currency")
            rewritten = rewritten.replace("jobs.source", "jobs_upsert_test.source")
            rewritten = rewritten.replace("jobs.apply_type", "jobs_upsert_test.apply_type")
            rewritten = rewritten.replace("jobs.apply_url", "jobs_upsert_test.apply_url")
            rewritten = rewritten.replace("jobs.apply_label", "jobs_upsert_test.apply_label")
            rewritten = rewritten.replace("jobs.poster_name", "jobs_upsert_test.poster_name")
            rewritten = rewritten.replace("jobs.poster_title", "jobs_upsert_test.poster_title")
            rewritten = rewritten.replace("jobs.poster_profile_url", "jobs_upsert_test.poster_profile_url")
            rewritten = rewritten.replace("jobs.date_posted", "jobs_upsert_test.date_posted")
            return self._cursor.execute(rewritten, params)

        def fetchone(self):
            return self._cursor.fetchone()

    job = cp.normalize_job({
        "title": "Real PG Integration Engineer",
        "company": "Acme",
        "location": "Berlin, Germany",
        "job_description": "Integration-tested row.",
        "job_url": "https://www.linkedin.com/jobs/view/555555/",
        "linkedin_job_id": "555555",
        "source": "LinkedIn",
    })

    proxy_cursor = TableRewritingCursor(pg_conn.cursor())
    outcome = cp.insert_job(proxy_cursor, job)
    pg_conn.commit()
    assert outcome == cr.ROW_OUTCOME_INSERTED

    proxy_cursor2 = TableRewritingCursor(pg_conn.cursor())
    outcome2 = cp.insert_job(proxy_cursor2, dict(job))
    pg_conn.commit()
    assert outcome2 == cr.ROW_OUTCOME_UPDATED_EXISTING
