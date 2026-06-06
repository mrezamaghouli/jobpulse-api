import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2

from app.config import get_postgres_config
from scripts.ensure_collector_runs_table import ensure_collector_runs_table


BASE_DIR = Path(__file__).resolve().parent.parent
QUERY_FILE = BASE_DIR / "config" / "job_queries.json"


def load_queries():
    if not QUERY_FILE.exists():
        raise FileNotFoundError(f"Query file not found: {QUERY_FILE}")

    with QUERY_FILE.open("r", encoding="utf-8") as file:
        queries = json.load(file)

    if not isinstance(queries, list):
        raise ValueError("job_queries.json must contain a list of query objects.")

    return queries


def save_collector_run(
    provider,
    keywords,
    location,
    job_limit,
    status,
    started_at,
    finished_at,
    error_message=None
):
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        INSERT INTO collector_runs (
            provider,
            keywords,
            location,
            job_limit,
            status,
            started_at,
            finished_at,
            duration_seconds,
            error_message
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """,
        (
            provider,
            keywords,
            location,
            job_limit,
            status,
            started_at,
            finished_at,
            duration_seconds,
            error_message
        )
    )

    connection.commit()

    cursor.close()
    connection.close()



def deactivate_stale_linkedin_jobs():
    stale_days = int(os.getenv("LINKEDIN_STALE_DAYS", "7"))
    cutoff_time = datetime.now() - timedelta(days=stale_days)

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE jobs
        SET is_active = FALSE
        WHERE source = 'LinkedIn'
          AND is_active = TRUE
          AND last_seen_at < %s;
        """,
        (cutoff_time,)
    )

    deactivated_count = cursor.rowcount

    connection.commit()

    cursor.close()
    connection.close()

    print("\n" + "=" * 70)
    print("Stale LinkedIn job cleanup finished.")
    print(f"Stale after days: {stale_days}")
    print(f"Deactivated jobs: {deactivated_count}")
    print("=" * 70)

    return deactivated_count
def run_collector_for_query(query):
    keywords = query.get("keywords", "").strip()
    location = query.get("location", "").strip()
    limit = str(query.get("limit", 10))

    if not keywords or not location:
        print(f"Skipping invalid query: {query}")
        return False

    print("\n" + "=" * 70)
    print("Collecting LinkedIn jobs")
    print(f"Keywords: {keywords}")
    print(f"Location: {location}")
    print(f"Limit: {limit}")
    print("=" * 70)

    env = os.environ.copy()

    env["JOB_PROVIDER"] = "linkedin_browser"
    env["LINKEDIN_BROWSER"] = env.get("LINKEDIN_BROWSER", "chrome")
    env["LINKEDIN_KEYWORDS"] = keywords
    env["LINKEDIN_LOCATION"] = location
    env["LINKEDIN_LIMIT"] = limit

    started_at = datetime.now()

    process = subprocess.run(
        [sys.executable, "-m", "scripts.collector_postgres"],
        cwd=str(BASE_DIR),
        env=env,
        text=True,
        capture_output=True
    )

    finished_at = datetime.now()

    print(process.stdout)

    if process.stderr:
        print(process.stderr)

    if process.returncode == 0:
        save_collector_run(
            provider="linkedin_browser",
            keywords=keywords,
            location=location,
            job_limit=int(limit),
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            error_message=None
        )

        return True

    save_collector_run(
        provider="linkedin_browser",
        keywords=keywords,
        location=location,
        job_limit=int(limit),
        status="failed",
        started_at=started_at,
        finished_at=finished_at,
        error_message=process.stderr or "Collector failed"
    )

    return False


def main():
    ensure_collector_runs_table()

    queries = load_queries()

    print(f"Loaded queries: {len(queries)}")

    success_count = 0
    failed_count = 0

    for index, query in enumerate(queries, start=1):
        print(f"\nRunning query {index}/{len(queries)}")

        success = run_collector_for_query(query)

        if success:
            success_count += 1
        else:
            failed_count += 1

        if index < len(queries):
            print("Waiting before next query...")
            time.sleep(5)

    deactivated_count = deactivate_stale_linkedin_jobs()

    print("\n" + "=" * 70)
    print("Multi-query LinkedIn collection finished.")
    print(f"Successful queries: {success_count}")
    print(f"Failed queries: {failed_count}")
    print(f"Deactivated stale jobs: {deactivated_count}")
    print("=" * 70)

    if failed_count > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()