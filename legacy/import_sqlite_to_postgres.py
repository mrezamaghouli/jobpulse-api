import sqlite3
from pathlib import Path

import psycopg2


BASE_DIR = Path(__file__).resolve().parent.parent

SQLITE_DB_FILE = BASE_DIR / "storage" / "jobpulse.db"

POSTGRES_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "jobpulse",
    "user": "jobpulse_user",
    "password": "jobpulse_password",
}


def get_sqlite_jobs():
    sqlite_connection = sqlite3.connect(SQLITE_DB_FILE)
    sqlite_connection.row_factory = sqlite3.Row
    cursor = sqlite_connection.cursor()

    cursor.execute("""
        SELECT *
        FROM jobs
        ORDER BY id ASC
    """)

    rows = cursor.fetchall()
    sqlite_connection.close()

    return rows


def insert_job_to_postgres(cursor, job):
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
    """, {
        "linkedin_job_id": job["linkedin_job_id"],
        "title": job["title"],
        "company": job["company"],
        "company_linkedin_url": job["company_linkedin_url"],
        "location": job["location"],
        "remote": bool(job["remote"]),
        "job_type": job["job_type"],
        "seniority": job["seniority"],
        "salary_min": job["salary_min"],
        "salary_max": job["salary_max"],
        "currency": job["currency"],
        "source": job["source"],
        "job_url": job["job_url"],
        "poster_name": job["poster_name"],
        "poster_title": job["poster_title"],
        "poster_profile_url": job["poster_profile_url"],
        "date_posted": job["date_posted"],
    })


def import_jobs():
    sqlite_jobs = get_sqlite_jobs()

    if not sqlite_jobs:
        print("No jobs found in SQLite database.")
        return

    postgres_connection = psycopg2.connect(**POSTGRES_CONFIG)
    postgres_cursor = postgres_connection.cursor()

    inserted_count = 0

    for job in sqlite_jobs:
        before_count = postgres_cursor.rowcount

        insert_job_to_postgres(postgres_cursor, job)

        if postgres_cursor.rowcount == 1:
            inserted_count += 1

    postgres_connection.commit()

    postgres_cursor.close()
    postgres_connection.close()

    print("SQLite to PostgreSQL import finished.")
    print(f"SQLite jobs found: {len(sqlite_jobs)}")
    print(f"Inserted into PostgreSQL: {inserted_count}")
    print(f"Skipped duplicates: {len(sqlite_jobs) - inserted_count}")


if __name__ == "__main__":
    import_jobs()