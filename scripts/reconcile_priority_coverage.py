import argparse

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


DONE_STATUSES = ("done", "success", "completed")
FAILED_STATUSES = ("failed", "error")


def connect():
    return psycopg2.connect(**get_postgres_config())


def current_jobs_count(cursor) -> int:
    cursor.execute("SELECT COUNT(*) AS count FROM jobs;")
    return int(cursor.fetchone()["count"] or 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--stale-hours", type=int, default=12)
    args = parser.parse_args()

    conn = connect()
    conn.autocommit = False

    updated = 0

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            count_after = current_jobs_count(cursor)

            cursor.execute(
                """
                SELECT
                    cov.id AS coverage_id,
                    cov.status AS coverage_status,
                    cov.attempts,
                    cov.jobs_count_before,
                    q.status AS queue_status,
                    q.last_error AS queue_error
                FROM job_collection_coverage cov
                LEFT JOIN job_search_demand_queue q
                    ON (q.filters_json->>'coverage_id')::bigint = cov.id
                WHERE cov.status IN ('queued', 'running')
                """
            )

            rows = cursor.fetchall()

            for row in rows:
                coverage_id = row["coverage_id"]
                queue_status = row["queue_status"]
                attempts = int(row["attempts"] or 0)
                before = row["jobs_count_before"]

                if queue_status in DONE_STATUSES:
                    cursor.execute(
                        """
                        UPDATE job_collection_coverage
                        SET
                            status = 'done',
                            jobs_count_after = %s,
                            jobs_delta = COALESCE(%s, 0) - COALESCE(jobs_count_before, 0),
                            last_collected_at = NOW(),
                            updated_at = NOW(),
                            last_error = NULL
                        WHERE id = %s
                        """,
                        (count_after, count_after, coverage_id),
                    )
                    updated += 1

                elif queue_status in FAILED_STATUSES:
                    next_status = "failed" if attempts >= args.max_attempts else "retry_later"
                    cursor.execute(
                        """
                        UPDATE job_collection_coverage
                        SET
                            status = %s,
                            jobs_count_after = %s,
                            jobs_delta = COALESCE(%s, 0) - COALESCE(jobs_count_before, 0),
                            updated_at = NOW(),
                            last_error = %s
                        WHERE id = %s
                        """,
                        (
                            next_status,
                            count_after,
                            count_after,
                            row["queue_error"],
                            coverage_id,
                        ),
                    )
                    updated += 1

            cursor.execute(
                """
                UPDATE job_collection_coverage
                SET
                    status = 'retry_later',
                    updated_at = NOW(),
                    last_error = 'Queued task became stale before completion.'
                WHERE status = 'queued'
                  AND last_queued_at < NOW() - (%s || ' hours')::interval
                """,
                (args.stale_hours,),
            )
            updated += cursor.rowcount

        conn.commit()
        print(f"Priority coverage reconciliation finished. Updated rows: {updated}")

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
