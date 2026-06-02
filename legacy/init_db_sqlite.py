import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
DB_FILE = STORAGE_DIR / "jobpulse.db"


def create_database():
    STORAGE_DIR.mkdir(exist_ok=True)

    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            linkedin_job_id TEXT UNIQUE,

            title TEXT NOT NULL,
            company TEXT NOT NULL,
            company_linkedin_url TEXT,

            location TEXT NOT NULL,
            remote INTEGER NOT NULL DEFAULT 0,

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
    """)

    connection.commit()
    connection.close()

    print("Database created successfully.")
    print(f"Database path: {DB_FILE}")


if __name__ == "__main__":
    create_database()