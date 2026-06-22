import argparse
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


def fmt(value):
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    conn = psycopg2.connect(**get_postgres_config())

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    trigger_name,
                    status,
                    seed_limit,
                    process_limit,
                    jobs_count_before,
                    jobs_count_after,
                    jobs_delta,
                    pending_before,
                    pending_after,
                    duration_seconds,
                    started_at,
                    finished_at,
                    error
                FROM collection_cycles
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (args.limit,),
            )

            rows = cursor.fetchall()

            print("\nLatest collection cycles")
            print("-" * 120)

            if not rows:
                print("No collection cycles found.")
                return

            for row in rows:
                print(
                    f"#{row['id']} "
                    f"status={fmt(row['status'])} "
                    f"trigger={fmt(row['trigger_name'])} "
                    f"seed={fmt(row['seed_limit'])} "
                    f"process={fmt(row['process_limit'])} "
                    f"jobs_delta={fmt(row['jobs_delta'])} "
                    f"jobs={fmt(row['jobs_count_before'])}->{fmt(row['jobs_count_after'])} "
                    f"pending={fmt(row['pending_before'])}->{fmt(row['pending_after'])} "
                    f"duration={fmt(row['duration_seconds'])}s "
                    f"started={fmt(row['started_at'])}"
                )

                if row.get("error"):
                    print(f"  error={row['error']}")

            print("-" * 120)

            cursor.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM job_search_demand_queue
                GROUP BY status
                ORDER BY count DESC
                """
            )

            queue_rows = cursor.fetchall()

            print("\nDemand queue status")
            print("-" * 120)
            for row in queue_rows:
                print(f"{row['status']}: {row['count']}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
