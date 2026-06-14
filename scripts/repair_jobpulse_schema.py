import psycopg2

from app.config import get_postgres_config


def repair_jobpulse_schema():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    print("Repairing JobPulse database schema...")
    
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            remote BOOLEAN DEFAULT FALSE,
            job_type TEXT,
            seniority TEXT,
            salary_min NUMERIC,
            salary_max NUMERIC,
            currency TEXT,
            source TEXT DEFAULT 'LinkedIn',
            job_url TEXT,
            date_posted TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id SERIAL PRIMARY KEY,
            linkedin_company_url TEXT UNIQUE,
            name TEXT,
            logo_url TEXT,
            website_url TEXT,
            industry TEXT,
            company_size TEXT,
            headquarters TEXT,
            about TEXT,
            last_enriched_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    job_columns = [
        "ADD COLUMN IF NOT EXISTS linkedin_job_id TEXT",
        "ADD COLUMN IF NOT EXISTS title TEXT",
        "ADD COLUMN IF NOT EXISTS company TEXT",
        "ADD COLUMN IF NOT EXISTS company_linkedin_url TEXT",
        "ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id)",
        "ADD COLUMN IF NOT EXISTS company_logo_url TEXT",

        "ADD COLUMN IF NOT EXISTS location TEXT",
        "ADD COLUMN IF NOT EXISTS remote BOOLEAN DEFAULT FALSE",
        "ADD COLUMN IF NOT EXISTS work_mode VARCHAR(50)",

        "ADD COLUMN IF NOT EXISTS job_type TEXT",
        "ADD COLUMN IF NOT EXISTS seniority TEXT",

        "ADD COLUMN IF NOT EXISTS salary_min NUMERIC",
        "ADD COLUMN IF NOT EXISTS salary_max NUMERIC",
        "ADD COLUMN IF NOT EXISTS currency TEXT",

        "ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'LinkedIn'",
        "ADD COLUMN IF NOT EXISTS job_url TEXT",

        "ADD COLUMN IF NOT EXISTS job_description TEXT",
        "ADD COLUMN IF NOT EXISTS job_about TEXT",

        "ADD COLUMN IF NOT EXISTS date_posted TEXT",
        "ADD COLUMN IF NOT EXISTS date_posted_text TEXT",
        "ADD COLUMN IF NOT EXISTS date_posted_at TIMESTAMP",

        "ADD COLUMN IF NOT EXISTS apply_type VARCHAR(50) DEFAULT 'unknown'",
        "ADD COLUMN IF NOT EXISTS apply_url TEXT",
        "ADD COLUMN IF NOT EXISTS apply_label VARCHAR(100)",

        "ADD COLUMN IF NOT EXISTS poster_name TEXT",
        "ADD COLUMN IF NOT EXISTS poster_title TEXT",
        "ADD COLUMN IF NOT EXISTS poster_profile_url TEXT",

        "ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",

        "ADD COLUMN IF NOT EXISTS inactive_at TIMESTAMP",
        "ADD COLUMN IF NOT EXISTS inactive_reason TEXT",
        "ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP",
        "ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    ]

    for column_sql in job_columns:
        cursor.execute(f"ALTER TABLE jobs {column_sql};")

    cursor.execute(
        """
        UPDATE jobs
        SET apply_type = 'unknown'
        WHERE apply_type IS NULL;
        """
    )

    cursor.execute(
        """
        UPDATE jobs
        SET source = 'LinkedIn'
        WHERE source IS NULL OR source = '';
        """
    )

    cursor.execute(
        """
        UPDATE jobs
        SET remote = FALSE
        WHERE remote IS NULL;
        """
    )

    cursor.execute(
        """
        UPDATE jobs
        SET first_seen_at = CURRENT_TIMESTAMP
        WHERE first_seen_at IS NULL;
        """
    )

    cursor.execute(
        """
        UPDATE jobs
        SET last_seen_at = CURRENT_TIMESTAMP
        WHERE last_seen_at IS NULL;
        """
    )

    cursor.execute(
        """
        UPDATE jobs
        SET is_active = TRUE
        WHERE is_active IS NULL;
        """
    )

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

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_job_url ON jobs(job_url);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_linkedin_job_id ON jobs(linkedin_job_id);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_apply_type ON jobs(apply_type);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_is_active ON jobs(is_active);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at ON jobs(last_seen_at);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_first_seen_at ON jobs(first_seen_at);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_work_mode ON jobs(work_mode);",

        "CREATE INDEX IF NOT EXISTS idx_companies_linkedin_url ON companies(linkedin_company_url);",
        "CREATE INDEX IF NOT EXISTS idx_companies_name ON companies(name);",

        "CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_signature ON linkedin_query_runs(query_signature);",
        "CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_status ON linkedin_query_runs(status);",
        "CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_started_at ON linkedin_query_runs(started_at);",
        "CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_category ON linkedin_query_runs(category);",
        "CREATE INDEX IF NOT EXISTS idx_linkedin_query_runs_location ON linkedin_query_runs(location);",

        "CREATE INDEX IF NOT EXISTS idx_jobs_inactive_at ON jobs(inactive_at);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_archived_at ON jobs(archived_at);",
        "CREATE INDEX IF NOT EXISTS idx_jobs_deleted_at ON jobs(deleted_at);",
    ]

    for index_sql in indexes:
        cursor.execute(index_sql)

    connection.commit()

    cursor.close()
    connection.close()

    print("Schema repair finished successfully.")


if __name__ == "__main__":
    repair_jobpulse_schema()