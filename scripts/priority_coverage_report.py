import argparse

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


def connect():
    return psycopg2.connect(**get_postgres_config())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    conn = connect()

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    country_priority,
                    status,
                    COUNT(*) AS count
                FROM job_collection_coverage
                GROUP BY country_priority, status
                ORDER BY country_priority, status
                """
            )
            rows = cursor.fetchall()

            print("\nPriority coverage summary")
            print("-" * 90)

            for row in rows:
                print(
                    f"priority={row['country_priority']} "
                    f"status={row['status']} "
                    f"count={row['count']}"
                )

            cursor.execute(
                """
                SELECT MIN(country_priority) AS current_priority
                FROM job_collection_coverage
                WHERE status IN ('pending', 'retry_later', 'queued', 'running')
                """
            )
            current_priority = cursor.fetchone()["current_priority"]

            print("-" * 90)
            print(f"current_active_priority={current_priority}")

            if current_priority is not None:
                cursor.execute(
                    """
                    SELECT
                        c.country_name,
                        cov.status,
                        COUNT(*) AS count
                    FROM job_collection_coverage cov
                    JOIN job_catalog_countries c ON c.id = cov.country_id
                    WHERE cov.country_priority = %s
                    GROUP BY c.country_name, cov.status
                    ORDER BY c.country_name, cov.status
                    """,
                    (current_priority,),
                )

                print("\nCurrent priority country progress")
                print("-" * 90)

                for row in cursor.fetchall():
                    print(
                        f"{row['country_name']}: "
                        f"{row['status']}={row['count']}"
                    )

            cursor.execute(
                """
                SELECT
                    cov.id,
                    t.title,
                    c.country_name,
                    cov.country_priority,
                    cov.status,
                    cov.attempts,
                    cov.last_queued_at,
                    cov.last_collected_at,
                    cov.jobs_delta,
                    cov.last_error
                FROM job_collection_coverage cov
                JOIN job_catalog_titles t ON t.id = cov.job_title_id
                JOIN job_catalog_countries c ON c.id = cov.country_id
                WHERE cov.status IN ('pending', 'retry_later', 'queued', 'running', 'failed')
                ORDER BY cov.country_priority, c.country_name, cov.status, cov.id
                LIMIT %s
                """,
                (args.limit,),
            )

            print("\nNext / active tasks")
            print("-" * 90)

            for row in cursor.fetchall():
                print(
                    f"#{row['id']} "
                    f"p={row['country_priority']} "
                    f"{row['country_name']} | {row['title']} | "
                    f"status={row['status']} attempts={row['attempts']} "
                    f"delta={row['jobs_delta']} error={row['last_error'] or '-'}"
                )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
