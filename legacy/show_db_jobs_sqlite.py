import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_FILE = BASE_DIR / "storage" / "jobpulse.db"


def show_jobs():
    connection = sqlite3.connect(DB_FILE)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT id, title, company, location, source, job_url
        FROM jobs
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    connection.close()

    print(f"Total jobs found: {len(rows)}")
    print("-" * 80)

    for row in rows:
        print(f"ID: {row[0]}")
        print(f"Title: {row[1]}")
        print(f"Company: {row[2]}")
        print(f"Location: {row[3]}")
        print(f"Source: {row[4]}")
        print(f"URL: {row[5]}")
        print("-" * 80)


if __name__ == "__main__":
    show_jobs()