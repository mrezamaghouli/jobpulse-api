import psycopg2

from app.config import get_postgres_config


CREATE_COLLECTOR_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS collector_runs (
    id SERIAL PRIMARY KEY,

    provider VARCHAR(100) NOT NULL,
    keywords VARCHAR(255),
    location VARCHAR(255),
    job_limit INTEGER,

    status VARCHAR(50) NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP NOT NULL,
    duration_seconds NUMERIC(10, 2),

    error_message TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


ALTER_COLLECTOR_RUNS_TABLE_SQL = """
ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS provider VARCHAR(100);

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS keywords VARCHAR(255);

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS location VARCHAR(255);

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS job_limit INTEGER;

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS status VARCHAR(50);

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP;

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS duration_seconds NUMERIC(10, 2);

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS error_message TEXT;

ALTER TABLE collector_runs
ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_collector_runs_provider
ON collector_runs(provider);

CREATE INDEX IF NOT EXISTS idx_collector_runs_status
ON collector_runs(status);

CREATE INDEX IF NOT EXISTS idx_collector_runs_started_at
ON collector_runs(started_at);
"""


def ensure_collector_runs_table():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(CREATE_COLLECTOR_RUNS_TABLE_SQL)
    cursor.execute(ALTER_COLLECTOR_RUNS_TABLE_SQL)

    connection.commit()

    cursor.close()
    connection.close()

    print("collector_runs table is ready.")


if __name__ == "__main__":
    ensure_collector_runs_table()