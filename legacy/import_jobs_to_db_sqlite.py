import json
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent

JOBS_JSON_FILE = BASE_DIR / "sample_data" / "jobs.json"
DB_FILE = BASE_DIR / "storage" / "jobpulse.db"


def load_jobs_from_json():
    if not JOBS_JSON_FILE.exists():
        print(f"jobs.json not found: {JOBS_JSON_FILE}")
        return []

    with open(JOBS_JSON_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def parse_remote(value):
    if isinstance(value, bool):
        return 1 if value else 0

    if isinstance(value, str):
        return 1 if value.lower() in ["true", "yes", "1", "remote"] else 0

    return 0


def insert_jobs():
    jobs = load_jobs_from_json()

    if not jobs:
        print("No jobs found in jobs.json")
        return

    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    inserted_count = 0
    skipped_count = 0

    for job in jobs:
        try:
            cursor.execute("""
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job.get("linkedin_job_id"),
                job.get("title"),
                job.get("company"),
                job.get("company_linkedin_url"),
                job.get("location"),
                parse_remote(job.get("remote")),
                job.get("job_type"),
                job.get("seniority"),
                job.get("salary_min"),
                job.get("salary_max"),
                job.get("currency"),
                job.get("source", "LinkedIn"),
                job.get("job_url"),
                job.get("poster_name"),
                job.get("poster_title"),
                job.get("poster_profile_url"),
                job.get("date_posted")
            ))

            inserted_count += 1

        except sqlite3.IntegrityError:
            skipped_count += 1

    connection.commit()
    connection.close()

    print("Import finished successfully.")
    print(f"Inserted jobs: {inserted_count}")
    print(f"Skipped duplicate jobs: {skipped_count}")
    print(f"Total jobs processed: {len(jobs)}")


if __name__ == "__main__":
    insert_jobs()