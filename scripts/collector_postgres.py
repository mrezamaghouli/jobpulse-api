import os
import json
from pathlib import Path
from datetime import date

import psycopg2


BASE_DIR = Path(__file__).resolve().parent.parent

NEW_JOBS_FILE = BASE_DIR / "sample_data" / "new_jobs.json"

POSTGRES_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": os.getenv("POSTGRES_PORT", "5432"),
    "database": os.getenv("POSTGRES_DB", "jobpulse"),
    "user": os.getenv("POSTGRES_USER", "jobpulse_user"),
    "password": os.getenv("POSTGRES_PASSWORD", "jobpulse_password"),
}

ALLOWED_SOURCE = "LinkedIn"


def load_json(file_path):
    if not file_path.exists():
        return []

    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


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
            date_posted
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
            %(date_posted)s
        )
        ON CONFLICT (job_url) DO NOTHING;
        """,
        job
    )


def collect_jobs_to_postgres():
    raw_jobs = load_json(NEW_JOBS_FILE)

    if not raw_jobs:
        print("No jobs found in new_jobs.json")
        return

    connection = psycopg2.connect(**POSTGRES_CONFIG)
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

        if cursor.rowcount == 1:
            added_count += 1
        else:
            skipped_duplicate_count += 1

    connection.commit()

    cursor.close()
    connection.close()

    print("LinkedIn PostgreSQL collector finished successfully.")
    print(f"Added LinkedIn jobs: {added_count}")
    print(f"Skipped duplicate jobs: {skipped_duplicate_count}")
    print(f"Skipped non-LinkedIn jobs: {skipped_non_linkedin_count}")
    print(f"Skipped invalid jobs: {skipped_invalid_count}")


if __name__ == "__main__":
    collect_jobs_to_postgres()