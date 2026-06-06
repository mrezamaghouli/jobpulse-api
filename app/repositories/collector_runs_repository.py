from app.postgres_database import get_postgres_connection


def get_latest_collector_run_from_db():
    connection = get_postgres_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            id,
            provider,
            keywords,
            location,
            job_limit,
            status,
            started_at,
            finished_at,
            duration_seconds,
            error_message,
            created_at
        FROM collector_runs
        ORDER BY started_at DESC
        LIMIT 1;
        """
    )

    collector_run = cursor.fetchone()

    cursor.close()
    connection.close()

    return collector_run


def get_recent_collector_runs_from_db(limit: int = 10):
    connection = get_postgres_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            id,
            provider,
            keywords,
            location,
            job_limit,
            status,
            started_at,
            finished_at,
            duration_seconds,
            error_message,
            created_at
        FROM collector_runs
        ORDER BY started_at DESC
        LIMIT %s;
        """,
        (limit,)
    )

    collector_runs = cursor.fetchall()

    cursor.close()
    connection.close()

    return collector_runs