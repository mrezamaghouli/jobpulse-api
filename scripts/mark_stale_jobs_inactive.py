import os
import psycopg2

from app.config import get_postgres_config


def get_stale_days() -> int:
    raw_value = os.getenv("STALE_JOB_DAYS", "30")

    try:
        value = int(raw_value)
    except ValueError:
        value = 30

    if value < 1:
        value = 1

    if value > 365:
        value = 365

    return value


def mark_stale_jobs_inactive():
    stale_days = get_stale_days()

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE jobs
        SET is_active = FALSE
        WHERE source = 'LinkedIn'
          AND is_active = TRUE
          AND last_seen_at IS NOT NULL
          AND last_seen_at < NOW() - (%s || ' days')::INTERVAL;
        """,
        (stale_days,),
    )

    inactive_count = cursor.rowcount

    connection.commit()

    cursor.close()
    connection.close()

    print("Stale job cleanup finished.")
    print(f"Stale days: {stale_days}")
    print(f"Jobs marked inactive: {inactive_count}")


if __name__ == "__main__":
    mark_stale_jobs_inactive()