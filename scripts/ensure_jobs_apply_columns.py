import psycopg2

from app.config import get_postgres_config


ALTER_JOBS_APPLY_COLUMNS_SQL = """
ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS apply_type VARCHAR(50);

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS apply_url TEXT;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS apply_label VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_jobs_apply_type
ON jobs(apply_type);
"""


def ensure_jobs_apply_columns():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(ALTER_JOBS_APPLY_COLUMNS_SQL)

    connection.commit()

    cursor.close()
    connection.close()

    print("jobs apply columns are ready.")


if __name__ == "__main__":
    ensure_jobs_apply_columns()