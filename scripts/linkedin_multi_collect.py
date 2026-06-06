import json
import os
import subprocess
import sys
import time
from pathlib import Path


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


def run_collector_for_query(query):
    keywords = query.get("keywords", "").strip()
    location = query.get("location", "").strip()
    limit = str(query.get("limit", 10))

    if not keywords or not location:
        print(f"Skipping invalid query: {query}")
        return False

    print("\n" + "=" * 70)
    print(f"Collecting LinkedIn jobs")
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

    process = subprocess.run(
        [sys.executable, "-m", "scripts.collector_postgres"],
        cwd=str(BASE_DIR),
        env=env,
        text=True
    )

    return process.returncode == 0


def main():
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

    print("\n" + "=" * 70)
    print("Multi-query LinkedIn collection finished.")
    print(f"Successful queries: {success_count}")
    print(f"Failed queries: {failed_count}")
    print("=" * 70)

    if failed_count > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()