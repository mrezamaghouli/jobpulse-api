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


def get_current_priority(cursor, retry_after_hours: int):
    cursor.execute(
        """
        SELECT MIN(country_priority) AS priority
        FROM job_collection_coverage
        WHERE status IN ('pending', 'retry_later')
          AND (
                last_queued_at IS NULL
                OR last_queued_at < NOW() - (%s || ' hours')::interval
          )
        """,
        (retry_after_hours,),
    )
    row = cursor.fetchone()
    return row["priority"] if row else None


def select_tasks(cursor, priority: int, limit: int, retry_after_hours: int):
    cursor.execute(
        """
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
        WHERE cov.country_priority = %s
          AND cov.status IN ('pending', 'retry_later')
          AND t.is_active = TRUE
          AND c.is_active = TRUE
          AND (
                cov.last_queued_at IS NULL
                OR cov.last_queued_at < NOW() - (%s || ' hours')::interval
          )
        ORDER BY
            cov.country_priority ASC,
            c.priority ASC,
            c.country_name ASC,
            t.category ASC,
            cov.id ASC
        LIMIT %s
        FOR UPDATE SKIP LOCKED
        """,
        (priority, retry_after_hours, limit),
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
