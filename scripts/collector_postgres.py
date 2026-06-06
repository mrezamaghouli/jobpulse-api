from datetime import date

import psycopg2

from app.config import get_postgres_config
from scripts.providers.provider_factory import get_job_provider


ALLOWED_SOURCE = "LinkedIn"


def parse_remote(value):
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        return value.lower() in ["true", "yes", "1", "remote"]

    return False


def is_linkedin_job(raw_job):
    source = raw_job.get("source", "").strip().lower()
    job_url = raw_job.get("job_url", "").strip().lower()

    return source == ALLOWED_SOURCE.lower() and "linkedin.com/jobs" in job_url


def normalize_job(raw_job):
    return {
        "linkedin_job_id": raw_job.get("linkedin_job_id"),

        "title": raw_job.get("title", "").strip(),
        "company": raw_job.get("company", "").strip(),
        "company_linkedin_url": raw_job.get("company_linkedin_url"),

        "location": raw_job.get("location", "").strip(),
        "remote": parse_remote(raw_job.get("remote", False)),

        "job_type": raw_job.get("job_type"),
        "seniority": raw_job.get("seniority"),

        "salary_min": raw_job.get("salary_min"),
        "salary_max": raw_job.get("salary_max"),
        "currency": raw_job.get("currency"),

        "source": ALLOWED_SOURCE,
        "job_url": raw_job.get("job_url", "").strip(),

        "poster_name": raw_job.get("poster_name"),
        "poster_title": raw_job.get("poster_title"),
        "poster_profile_url": raw_job.get("poster_profile_url"),

        "date_posted": raw_job.get("date_posted", str(date.today()))
    }


def is_valid_job(job):
    required_fields = ["title", "company", "location", "job_url"]

    for field in required_fields:
        if not job.get(field):
            return False

    return True


def insert_job(cursor, job):
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
                %(poster_name)s,
                %(poster_title)s,
                %(poster_profile_url)s,
                %(date_posted)s,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                TRUE
            )
            ON CONFLICT (job_url) DO UPDATE SET
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
                poster_name = EXCLUDED.poster_name,
                poster_title = EXCLUDED.poster_title,
                poster_profile_url = EXCLUDED.poster_profile_url,
                date_posted = EXCLUDED.date_posted,
                last_seen_at = CURRENT_TIMESTAMP,
                is_active = TRUE;
            """,
            job
        )


def collect_jobs_to_postgres():
    provider = get_job_provider()
    raw_jobs = provider.fetch_jobs()

    if not raw_jobs:
        print("No jobs found from provider.")
        return

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    added_count = 0
    skipped_duplicate_count = 0
    skipped_non_linkedin_count = 0
    skipped_invalid_count = 0

    for raw_job in raw_jobs:
        if not is_linkedin_job(raw_job):
            skipped_non_linkedin_count += 1
            continue

        normalized_job = normalize_job(raw_job)

        if not is_valid_job(normalized_job):
            skipped_invalid_count += 1
            continue

        insert_job(cursor, normalized_job)
        added_count += 1

    connection.commit()

    cursor.close()
    connection.close()

    print("LinkedIn PostgreSQL collector finished successfully.")
    print(f"Provider: {provider.__class__.__name__}")
    print(f"Inserted or updated LinkedIn jobs: {added_count}")
    print(f"Skipped duplicate jobs: {skipped_duplicate_count}")
    print(f"Skipped non-LinkedIn jobs: {skipped_non_linkedin_count}")
    print(f"Skipped invalid jobs: {skipped_invalid_count}")


if __name__ == "__main__":
    collect_jobs_to_postgres()