CREATE TABLE IF NOT EXISTS jobs (
    id SERIAL PRIMARY KEY,

    linkedin_job_id TEXT UNIQUE,

    title TEXT NOT NULL,
    company TEXT NOT NULL,
    company_linkedin_url TEXT,

    location TEXT NOT NULL,
    remote BOOLEAN NOT NULL DEFAULT FALSE,

    job_type TEXT,
    seniority TEXT,

    salary_min INTEGER,
    salary_max INTEGER,
    currency TEXT,

    source TEXT NOT NULL,
    job_url TEXT NOT NULL UNIQUE,

    poster_name TEXT,
    poster_title TEXT,
    poster_profile_url TEXT,

    date_posted TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_title ON jobs(title);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_location ON jobs(location);
CREATE INDEX IF NOT EXISTS idx_jobs_remote ON jobs(remote);
CREATE INDEX IF NOT EXISTS idx_jobs_seniority ON jobs(seniority);
CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON jobs(job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_salary_min ON jobs(salary_min);
CREATE INDEX IF NOT EXISTS idx_jobs_salary_max ON jobs(salary_max);
CREATE INDEX IF NOT EXISTS idx_jobs_date_posted ON jobs(date_posted);
CREATE TABLE IF NOT EXISTS collector_runs (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    keywords TEXT,
    location TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    raw_jobs_count INTEGER DEFAULT 0,
    added_count INTEGER DEFAULT 0,
    skipped_duplicate_count INTEGER DEFAULT 0,
    skipped_non_linkedin_count INTEGER DEFAULT 0,
    skipped_invalid_count INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_collector_runs_started_at
ON collector_runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_collector_runs_provider
ON collector_runs(provider);