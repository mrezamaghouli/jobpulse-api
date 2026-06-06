import psycopg2

from app.config import get_postgres_config


ALTER_JOBS_TABLE_SQL = """
ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at
ON jobs(last_seen_at);

CREATE INDEX IF NOT EXISTS idx_jobs_is_active
ON jobs(is_active);
"""


def ensure_jobs_tracking_columns():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(ALTER_JOBS_TABLE_SQL)

    connection.commit()

    cursor.close()
    connection.close()

    print("jobs tracking columns are ready.")


if __name__ == "__main__":
    ensure_jobs_tracking_columns()