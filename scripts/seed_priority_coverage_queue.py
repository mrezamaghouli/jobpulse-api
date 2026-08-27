import argparse
import json
import re
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from app.config import get_postgres_config


def normalize(value: str | None) -> str:
    value = "" if value is None else str(value)
    value = value.strip().lower()
    value = value.replace("&", "and")
    value = re.sub(r"[^a-z0-9+#.]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def connect():
    return psycopg2.connect(**get_postgres_config())


def jobs_count(cursor) -> int:
    cursor.execute("SELECT COUNT(*) AS count FROM jobs;")
    return int(cursor.fetchone()["count"] or 0)


# Single shared eligibility predicate, used by BOTH get_current_priority()
# and select_tasks(). This closes a starvation hazard: an earlier revision
# had get_current_priority() consider only job_collection_coverage's own
# columns (no t.is_active/c.is_active join), while select_tasks() applied
# those two filters in addition. A stale-but-otherwise-eligible coverage
# row whose title or country had since been deactivated could then win
# MIN(country_priority) in get_current_priority(), only for select_tasks()
# to correctly exclude it and return zero rows for that priority -- main()
# would then report "No eligible tasks found for priority=..." and do
# nothing for that cycle, even though a real eligible row existed at a
# later (worse) priority. Recurring 'done' refresh makes this materially
# more dangerous than it was before: a pending/retry_later row eventually
# gets marked 'failed' after max_attempts (see
# reconcile_priority_coverage.py) and stops being reconsidered, but a
# 'done' row with an inactive title/country would otherwise go stale,
# become "eligible" by get_current_priority()'s old predicate, and starve
# every lower-priority tier forever, every single cycle.
#
# Both functions now build their WHERE clause from this exact same SQL
# fragment (parameterized only by %(hours)s, bound normally by psycopg2 --
# this is a fixed, hardcoded fragment, never string-built from external
# input) rather than hand-duplicating two independently-maintained copies
# that could drift apart again in a future edit.
_ELIGIBILITY_PREDICATE = """
    t.is_active = TRUE
    AND c.is_active = TRUE
    AND (
          (
            cov.status IN ('pending', 'retry_later')
            AND (
                  cov.last_queued_at IS NULL
                  OR cov.last_queued_at < NOW() - (%(hours)s || ' hours')::interval
            )
          )
          OR
          (
            cov.status = 'done'
            AND (
                  cov.last_collected_at IS NULL
                  OR cov.last_collected_at < NOW() - (%(hours)s || ' hours')::interval
            )
          )
        )
"""


def get_current_priority(cursor, retry_after_hours: int):
    cursor.execute(
        f"""
        SELECT MIN(cov.country_priority) AS priority
        FROM job_collection_coverage cov
        JOIN job_catalog_titles t ON t.id = cov.job_title_id
        JOIN job_catalog_countries c ON c.id = cov.country_id
        WHERE {_ELIGIBILITY_PREDICATE}
        """,
        {"hours": retry_after_hours},
    )
    row = cursor.fetchone()
    return row["priority"] if row else None


def select_tasks(cursor, priority: int, limit: int, retry_after_hours: int):
    # Never selects 'queued' or 'running' coverage -- those are actively
    # in flight and must not be duplicated or reset. Eligibility beyond
    # that (encoded in _ELIGIBILITY_PREDICATE above, shared with
    # get_current_priority()) is a union of two disjoint cases:
    #   - pending/retry_later: unchanged from before this fix, gated by
    #     last_queued_at (a row that was just queued should not be
    #     immediately re-queued again).
    #   - done: a *recurring* coverage search. Eligible again only once
    #     it's stale by the same retry_after_hours interval, measured
    #     from last_collected_at (set by reconcile_priority_coverage.py
    #     only on a PROVEN completed collection -- the correct freshness
    #     signal for "how long since this title/country combination was
    #     actually last searched", as opposed to last_queued_at, which
    #     would also be set by an attempt that never actually completed).
    #     This is what makes collection continuous rather than a single
    #     one-time pass over the whole coverage catalog: without this
    #     branch, a coverage row that reaches 'done' can never be
    #     selected again by this function, no matter how much time
    #     passes.
    cursor.execute(
        f"""
        SELECT
            cov.id AS coverage_id,
            cov.search_query,
            cov.linkedin_location,
            cov.country_priority,
            cov.attempts,
            t.category,
            t.title,
            c.country_name
        FROM job_collection_coverage cov
        JOIN job_catalog_titles t ON t.id = cov.job_title_id
        JOIN job_catalog_countries c ON c.id = cov.country_id
        WHERE cov.country_priority = %(priority)s
          AND {_ELIGIBILITY_PREDICATE}
        ORDER BY
            cov.country_priority ASC,
            c.priority ASC,
            c.country_name ASC,
            t.category ASC,
            cov.id ASC
        LIMIT %(limit)s
        FOR UPDATE SKIP LOCKED
        """,
        {"priority": priority, "hours": retry_after_hours, "limit": limit},
    )
    return cursor.fetchall()


def enqueue_task(cursor, task, before_count: int):
    title = task["search_query"]
    country = task["linkedin_location"]
    category = task["category"]
    coverage_id = task["coverage_id"]
    priority = int(task["country_priority"])

    normalized_query = f"coverage:{coverage_id}:{normalize(title)}:{normalize(country)}"

    filters = {
        "coverage_id": coverage_id,
        "catalog_title": title,
        "catalog_category": category,
        "linkedin_keywords": title,
        "linkedin_location": country,
        "location": country,
        "country": country,
        "country_priority": priority,
        "source": "priority_coverage",
    }

    # Keep raw_query human-readable. The processor can use filters_json for exact LinkedIn location.
    raw_query = title

    priority_score = 100000 - (priority * 1000) - int(coverage_id)

    cursor.execute(
        """
        INSERT INTO job_search_demand_queue (
            raw_query,
            normalized_query,
            job_family,
            filters_json,
            search_count,
            zero_result_count,
            low_result_count,
            last_result_count,
            priority_score,
            status,
            first_seen_at,
            last_seen_at
        )
        VALUES (%s, %s, %s, %s, 1, 0, 0, 0, %s, 'pending', NOW(), NOW())
        ON CONFLICT (normalized_query)
        DO UPDATE SET
            raw_query = EXCLUDED.raw_query,
            job_family = EXCLUDED.job_family,
            filters_json = EXCLUDED.filters_json,
            priority_score = EXCLUDED.priority_score,
            status = CASE
                WHEN job_search_demand_queue.status = 'processing'
                THEN job_search_demand_queue.status
                ELSE 'pending'
            END,
            last_seen_at = NOW()
        """,
        (
            raw_query,
            normalized_query,
            category,
            Json(filters),
            priority_score,
        ),
    )

    cursor.execute(
        """
        UPDATE job_collection_coverage
        SET
            status = 'queued',
            attempts = attempts + 1,
            last_queued_at = NOW(),
            jobs_count_before = %s,
            updated_at = NOW(),
            last_error = NULL
        WHERE id = %s
        """,
        (before_count, coverage_id),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--retry-after-hours", type=int, default=24)
    args = parser.parse_args()

    conn = connect()
    conn.autocommit = False

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            priority = get_current_priority(cursor, args.retry_after_hours)

            if priority is None:
                print("No pending priority coverage tasks found.")
                conn.commit()
                return

            before_count = jobs_count(cursor)
            tasks = select_tasks(cursor, priority, args.limit, args.retry_after_hours)

            if not tasks:
                print(f"No eligible tasks found for priority={priority}.")
                conn.commit()
                return

            for task in tasks:
                enqueue_task(cursor, task, before_count)
                print(
                    f"queued coverage_id={task['coverage_id']} "
                    f"priority={task['country_priority']} "
                    f"title={task['search_query']} "
                    f"country={task['linkedin_location']}"
                )

        conn.commit()
        print(f"Seeded {len(tasks)} priority coverage tasks into demand queue.")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
