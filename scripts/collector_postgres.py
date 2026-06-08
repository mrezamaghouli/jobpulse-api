import re
from datetime import date

import psycopg2

from app.config import get_postgres_config
from scripts.providers.provider_factory import get_job_provider


def ensure_jobs_runtime_columns(cursor):
    cursor.execute(
        """
        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS apply_type VARCHAR(50);

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS apply_url TEXT;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS apply_label VARCHAR(255);

        CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at
        ON jobs(last_seen_at);

        CREATE INDEX IF NOT EXISTS idx_jobs_is_active
        ON jobs(is_active);

        CREATE INDEX IF NOT EXISTS idx_jobs_apply_type
        ON jobs(apply_type);
        """
    )


def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def extract_linkedin_job_id(job_url):
    if not job_url:
        return ""

    patterns = [
        r"/jobs/view/(\d+)",
        r"currentJobId=(\d+)",
        r"jobId=(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, job_url)

        if match:
            return match.group(1)

    return ""


def canonicalize_linkedin_job_url(job_url, linkedin_job_id):
    if linkedin_job_id:
        return f"https://www.linkedin.com/jobs/view/{linkedin_job_id}/"

    extracted_id = extract_linkedin_job_id(job_url)

    if extracted_id:
        return f"https://www.linkedin.com/jobs/view/{extracted_id}/"

    return job_url


def normalize_bool(value):
    if value is None:
        return False

    if isinstance(value, bool):
        return value

    value_as_text = str(value).strip().lower()

    if value_as_text in ["true", "1", "yes", "y", "remote"]:
        return True

    return False


def normalize_apply_fields(job):
    apply_type = job.get("apply_type") or "unknown"
    apply_url = job.get("apply_url") or None
    apply_label = job.get("apply_label") or None

    if apply_type == "easy_apply" and not apply_url:
        apply_url = job.get("job_url")

    job["apply_type"] = apply_type
    job["apply_url"] = apply_url
    job["apply_label"] = apply_label

    return job


def normalize_job(raw_job):
    raw_job = dict(raw_job)

    raw_job_url = raw_job.get("job_url") or ""
    raw_linkedin_job_id = raw_job.get("linkedin_job_id") or ""

    linkedin_job_id = raw_linkedin_job_id or extract_linkedin_job_id(raw_job_url)

    job_url = canonicalize_linkedin_job_url(
        job_url=raw_job_url,
        linkedin_job_id=linkedin_job_id,
    )

    title = clean_text(raw_job.get("title"))
    company = clean_text(raw_job.get("company")) or "Unknown Company"
    location = clean_text(raw_job.get("location")) or "Unknown Location"

    apply_type = raw_job.get("apply_type") or "unknown"
    apply_url = raw_job.get("apply_url") or None
    apply_label = raw_job.get("apply_label") or None

    if apply_type == "easy_apply" and not apply_url:
        apply_url = job_url

    normalized_job = {
        "linkedin_job_id": linkedin_job_id,
        "title": title,
        "company": company,
        "company_linkedin_url": raw_job.get("company_linkedin_url"),

        "location": location,
        "remote": normalize_bool(raw_job.get("remote")) or ("remote" in location.lower()),

        "job_type": raw_job.get("job_type"),
        "seniority": raw_job.get("seniority"),

        "salary_min": raw_job.get("salary_min"),
        "salary_max": raw_job.get("salary_max"),
        "currency": raw_job.get("currency"),

        "source": raw_job.get("source") or "LinkedIn",
        "job_url": job_url,

        "apply_type": apply_type,
        "apply_url": apply_url,
        "apply_label": apply_label,

        "poster_name": raw_job.get("poster_name"),
        "poster_title": raw_job.get("poster_title"),
        "poster_profile_url": raw_job.get("poster_profile_url"),

        "date_posted": raw_job.get("date_posted") or str(date.today()),
    }

    return normalize_apply_fields(normalized_job)


def is_valid_job(job):
    if not job.get("title"):
        return False

    if not job.get("job_url"):
        return False

    return True


def is_linkedin_job(job):
    source = str(job.get("source") or "").lower()
    job_url = str(job.get("job_url") or "").lower()

    return source == "linkedin" or "linkedin.com/jobs/view" in job_url


def insert_job(cursor, job):
    job = normalize_apply_fields(job)

    cursor.execute(
        """
        INSERT INTO jobs (
            linkedin_job_id,
            title,
            company,
            company_linkedin_url,
            location,
            remote,
            job_type,
            seniority,
            salary_min,
            salary_max,
            currency,
            source,
            job_url,
            apply_type,
            apply_url,
            apply_label,
            poster_name,
            poster_title,
            poster_profile_url,
            date_posted,
            first_seen_at,
            last_seen_at,
            is_active
        )
        VALUES (
            %(linkedin_job_id)s,
            %(title)s,
            %(company)s,
            %(company_linkedin_url)s,
            %(location)s,
            %(remote)s,
            %(job_type)s,
            %(seniority)s,
            %(salary_min)s,
            %(salary_max)s,
            %(currency)s,
            %(source)s,
            %(job_url)s,
            %(apply_type)s,
            %(apply_url)s,
            %(apply_label)s,
            %(poster_name)s,
            %(poster_title)s,
            %(poster_profile_url)s,
            %(date_posted)s,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP,
            TRUE
        )
        ON CONFLICT (job_url) DO UPDATE SET
            linkedin_job_id = EXCLUDED.linkedin_job_id,
            title = EXCLUDED.title,
            company = EXCLUDED.company,
            company_linkedin_url = EXCLUDED.company_linkedin_url,
            location = EXCLUDED.location,
            remote = EXCLUDED.remote,
            job_type = EXCLUDED.job_type,
            seniority = EXCLUDED.seniority,
            salary_min = EXCLUDED.salary_min,
            salary_max = EXCLUDED.salary_max,
            currency = EXCLUDED.currency,
            source = EXCLUDED.source,
            apply_type = EXCLUDED.apply_type,
            apply_url = EXCLUDED.apply_url,
            apply_label = EXCLUDED.apply_label,
            poster_name = EXCLUDED.poster_name,
            poster_title = EXCLUDED.poster_title,
            poster_profile_url = EXCLUDED.poster_profile_url,
            date_posted = EXCLUDED.date_posted,
            last_seen_at = CURRENT_TIMESTAMP,
            is_active = TRUE;
        """,
        job,
    )


def collect_jobs_to_postgres():
    provider = get_job_provider()

    raw_jobs = provider.fetch_jobs()

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    ensure_jobs_runtime_columns(cursor)

    inserted_or_updated_count = 0
    skipped_duplicate_count = 0
    skipped_non_linkedin_count = 0
    skipped_invalid_count = 0

    for raw_job in raw_jobs:
        normalized_job = normalize_job(raw_job)

        if not is_valid_job(normalized_job):
            skipped_invalid_count += 1
            continue

        if not is_linkedin_job(normalized_job):
            skipped_non_linkedin_count += 1
            continue

        insert_job(cursor, normalized_job)
        inserted_or_updated_count += 1

    connection.commit()

    cursor.close()
    connection.close()

    print("LinkedIn PostgreSQL collector finished successfully.")
    print(f"Provider: {provider.__class__.__name__}")
    print(f"Inserted or updated LinkedIn jobs: {inserted_or_updated_count}")
    print(f"Skipped duplicate jobs: {skipped_duplicate_count}")
    print(f"Skipped non-LinkedIn jobs: {skipped_non_linkedin_count}")
    print(f"Skipped invalid jobs: {skipped_invalid_count}")


if __name__ == "__main__":
    collect_jobs_to_postgres()