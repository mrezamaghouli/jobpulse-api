import psycopg2

from app.config import get_postgres_config


def ensure_linkedin_query_tracking():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS linkedin_query_runs (
            id SERIAL PRIMARY KEY,

            query_signature TEXT NOT NULL,

            category TEXT,
            keywords TEXT,
            location TEXT,
            work_mode TEXT,
            lookback_days INTEGER,

            status TEXT NOT NULL DEFAULT 'unknown',

            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            duration_seconds NUMERIC,

            jobs_before INTEGER,
            jobs_after INTEGER,
            jobs_delta INTEGER,

            failed_queries INTEGER DEFAULT 0,
            profile_level INTEGER,
            profile_name TEXT,

            error TEXT,
            log_file TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_signature
        ON linkedin_query_runs(query_signature);
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_status
        ON linkedin_query_runs(status);
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_started_at
        ON linkedin_query_runs(started_at);
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_category
        ON linkedin_query_runs(category);
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_location
        ON linkedin_query_runs(location);
        """
    )

    connection.commit()

    cursor.close()
    connection.close()

    print("LinkedIn query tracking schema is ready.")


if __name__ == "__main__":
    ensure_linkedin_query_tracking()