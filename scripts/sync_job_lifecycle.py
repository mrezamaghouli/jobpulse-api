import os
import psycopg2

from app.config import get_postgres_config


def get_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default))

    try:
        value = int(raw_value)
    except ValueError:
        value = default

    if value < minimum:
        value = minimum

    if value > maximum:
        value = maximum

    return value


def get_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)

    if raw_value is None:
        return default

    return raw_value.strip().lower() in ["1", "true", "yes", "y", "on"]


def sync_job_lifecycle():
    inactive_after_days = get_int_env(
        "JOB_INACTIVE_AFTER_DAYS",
        default=30,
        minimum=1,
        maximum=365,
    )

    archive_after_days = get_int_env(
        "JOB_ARCHIVE_AFTER_DAYS",
        default=60,
        minimum=2,
        maximum=730,
    )

    hard_delete_after_days = get_int_env(
        "JOB_HARD_DELETE_AFTER_DAYS",
        default=120,
        minimum=7,
        maximum=1095,
    )

    hard_delete_enabled = get_bool_env(
        "JOB_HARD_DELETE_ENABLED",
        default=False,
    )

    if archive_after_days <= inactive_after_days:
        archive_after_days = inactive_after_days + 30

    if hard_delete_after_days <= archive_after_days:
        hard_delete_after_days = archive_after_days + 60

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    print("Running job lifecycle sync...")
    print(f"Inactive after days: {inactive_after_days}")
    print(f"Archive after days: {archive_after_days}")
    print(f"Hard delete enabled: {hard_delete_enabled}")
    print(f"Hard delete after days: {hard_delete_after_days}")

    cursor.execute(
        """
        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS inactive_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS inactive_reason TEXT,
        ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP,
        ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP;
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_inactive_at
        ON jobs(inactive_at);
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_archived_at
        ON jobs(archived_at);
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_deleted_at
        ON jobs(deleted_at);
        """
    )

    cursor.execute(
        """
        UPDATE jobs
        SET
            is_active = TRUE,
            inactive_at = NULL,
            inactive_reason = NULL,
            archived_at = NULL,
            deleted_at = NULL
        WHERE source = 'LinkedIn'
          AND deleted_at IS NULL
          AND last_seen_at >= NOW() - (%s || ' days')::INTERVAL;
        """,
        (inactive_after_days,),
    )

    reactivated_count = cursor.rowcount

    cursor.execute(
        """
        UPDATE jobs
        SET
            is_active = FALSE,
            inactive_at = COALESCE(inactive_at, NOW()),
            inactive_reason = 'not_seen_recently'
        WHERE source = 'LinkedIn'
          AND deleted_at IS NULL
          AND archived_at IS NULL
          AND is_active = TRUE
          AND last_seen_at < NOW() - (%s || ' days')::INTERVAL;
        """,
        (inactive_after_days,),
    )

    inactive_count = cursor.rowcount

    cursor.execute(
        """
        UPDATE jobs
        SET
            is_active = FALSE,
            archived_at = COALESCE(archived_at, NOW()),
            inactive_reason = COALESCE(inactive_reason, 'not_seen_recently')
        WHERE source = 'LinkedIn'
          AND deleted_at IS NULL
          AND archived_at IS NULL
          AND last_seen_at < NOW() - (%s || ' days')::INTERVAL;
        """,
        (archive_after_days,),
    )

    archived_count = cursor.rowcount

    hard_deleted_count = 0

    if hard_delete_enabled:
        cursor.execute(
            """
            DELETE FROM jobs
            WHERE source = 'LinkedIn'
              AND is_active = FALSE
              AND archived_at IS NOT NULL
              AND last_seen_at < NOW() - (%s || ' days')::INTERVAL;
            """,
            (hard_delete_after_days,),
        )

        hard_deleted_count = cursor.rowcount

    cursor.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE source = 'LinkedIn') AS total_linkedin_jobs,
            COUNT(*) FILTER (
                WHERE source = 'LinkedIn'
                  AND is_active = TRUE
                  AND deleted_at IS NULL
                  AND archived_at IS NULL
            ) AS active_jobs,
            COUNT(*) FILTER (
                WHERE source = 'LinkedIn'
                  AND is_active = FALSE
                  AND archived_at IS NULL
                  AND deleted_at IS NULL
            ) AS inactive_jobs,
            COUNT(*) FILTER (
                WHERE source = 'LinkedIn'
                  AND archived_at IS NOT NULL
                  AND deleted_at IS NULL
            ) AS archived_jobs
        FROM jobs;
        """
    )

    summary = cursor.fetchone()

    connection.commit()

    cursor.close()
    connection.close()

    print("Job lifecycle sync finished.")
    print(f"Reactivated/reconfirmed jobs: {reactivated_count}")
    print(f"Marked inactive: {inactive_count}")
    print(f"Archived: {archived_count}")
    print(f"Hard deleted: {hard_deleted_count}")

    if summary:
        print("")
        print("Current LinkedIn job lifecycle summary:")
        print(f"Total LinkedIn jobs: {summary[0]}")
        print(f"Active jobs: {summary[1]}")
        print(f"Inactive jobs: {summary[2]}")
        print(f"Archived jobs: {summary[3]}")


if __name__ == "__main__":
    sync_job_lifecycle()