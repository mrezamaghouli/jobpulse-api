import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "storage" / "jobpulse.db"


def create_indexes():
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_title
        ON jobs(title);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_company
        ON jobs(company);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_location
        ON jobs(location);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_remote
        ON jobs(remote);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_seniority
        ON jobs(seniority);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_job_type
        ON jobs(job_type);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_source
        ON jobs(source);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_salary_min
        ON jobs(salary_min);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_salary_max
        ON jobs(salary_max);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_jobs_date_posted
        ON jobs(date_posted);
    """)

    connection.commit()
    connection.close()

    print("Database indexes created successfully.")


if __name__ == "__main__":
    create_indexes()