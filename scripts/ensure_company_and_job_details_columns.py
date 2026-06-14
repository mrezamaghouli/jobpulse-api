import psycopg2

from app.config import get_postgres_config


SQL = """
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

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS company_logo_url TEXT;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS job_description TEXT;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS job_about TEXT;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS work_mode VARCHAR(50);

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS date_posted_text TEXT;

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS date_posted_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_companies_linkedin_company_url
ON companies(linkedin_company_url);

CREATE INDEX IF NOT EXISTS idx_jobs_company_id
ON jobs(company_id);

CREATE INDEX IF NOT EXISTS idx_jobs_date_posted_at
ON jobs(date_posted_at);

CREATE INDEX IF NOT EXISTS idx_jobs_work_mode
ON jobs(work_mode);
"""


def ensure_company_and_job_details_columns():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(SQL)

    connection.commit()

    cursor.close()
    connection.close()

    print("Company cache and job detail columns are ready.")


if __name__ == "__main__":
    ensure_company_and_job_details_columns()