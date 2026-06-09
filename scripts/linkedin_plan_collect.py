import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
QUERIES_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"
SEARCH_PLAN_FILE = BASE_DIR / "config" / "linkedin_search_plan.json"
LOG_DIR = BASE_DIR / "logs"


def load_json_file(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_default_workers() -> int:
    if SEARCH_PLAN_FILE.exists():
        search_plan = load_json_file(SEARCH_PLAN_FILE)

        try:
            workers = int(search_plan.get("parallel_workers", 2))
        except ValueError:
            workers = 2
    else:
        workers = 2

    if workers < 1:
        workers = 1

    if workers > 4:
        workers = 4

    return workers


def normalize_query(query: dict) -> dict:
    return {
        "category": query.get("category") or "unknown",
        "keywords": query.get("keywords") or "",
        "location": query.get("location") or "",
        "work_mode": query.get("work_mode") or "any",
        "lookback_days": int(query.get("lookback_days") or 60),
        "limit": int(query.get("limit") or 30),
        "max_pages": int(query.get("max_pages") or 1),
    }


def build_query_env(query: dict) -> dict:
    env = os.environ.copy()

    env["JOB_PROVIDER"] = "linkedin_browser"
    env["LINKEDIN_BROWSER"] = env.get("LINKEDIN_BROWSER", "chrome")

    env["LINKEDIN_KEYWORDS"] = query["keywords"]
    env["LINKEDIN_LOCATION"] = query["location"]
    env["LINKEDIN_LIMIT"] = str(query["limit"])

    env["LINKEDIN_WORK_MODE"] = query["work_mode"]
    env["LINKEDIN_LOOKBACK_DAYS"] = str(query["lookback_days"])
    env["LINKEDIN_MAX_PAGES"] = str(query["max_pages"])
    env["LINKEDIN_QUERY_RETRY_COUNT"] = env.get(
        "LINKEDIN_QUERY_RETRY_COUNT",
        "1"
    )

    env["LINKEDIN_QUERY_RETRY_DELAY_SECONDS"] = env.get(
        "LINKEDIN_QUERY_RETRY_DELAY_SECONDS",
        "10"
    )
    env["LINKEDIN_SKIP_EXISTING_ENRICHED"] = env.get(
        "LINKEDIN_SKIP_EXISTING_ENRICHED",
        "true"
    )

    env["POSTGRES_HOST"] = env.get("POSTGRES_HOST", "localhost")
    env["POSTGRES_PORT"] = env.get("POSTGRES_PORT", "5432")
    env["POSTGRES_DB"] = env.get("POSTGRES_DB", "jobpulse")
    env["POSTGRES_USER"] = env.get("POSTGRES_USER", "jobpulse_user")
    env["POSTGRES_PASSWORD"] = env.get("POSTGRES_PASSWORD", "jobpulse_password")

    return env


def get_retry_count() -> int:
    try:
        retry_count = int(os.getenv("LINKEDIN_QUERY_RETRY_COUNT", "1"))
    except ValueError:
        retry_count = 1

    if retry_count < 0:
        retry_count = 0

    if retry_count > 3:
        retry_count = 3

    return retry_count


def get_retry_delay_seconds() -> int:
    try:
        delay_seconds = int(os.getenv("LINKEDIN_QUERY_RETRY_DELAY_SECONDS", "10"))
    except ValueError:
        delay_seconds = 10

    if delay_seconds < 0:
        delay_seconds = 0

    if delay_seconds > 120:
        delay_seconds = 120

    return delay_seconds


def run_single_query(query: dict, index: int, total: int) -> dict:
    retry_count = get_retry_count()
    retry_delay_seconds = get_retry_delay_seconds()

    attempts = []
    final_result = None

    for attempt_number in range(1, retry_count + 2):
        started_at = datetime.now()

        label = (
            f"[{index}/{total}] "
            f"{query['category']} | "
            f"{query['keywords']} | "
            f"{query['location'] or 'Worldwide'} | "
            f"{query['work_mode']} | "
            f"attempt {attempt_number}/{retry_count + 1}"
        )

        print("\n" + "=" * 90)
        print(f"Starting query {label}")
        print("=" * 90)

        env = build_query_env(query)

        process = subprocess.run(
            [sys.executable, "-m", "scripts.collector_postgres"],
            cwd=str(BASE_DIR),
            env=env,
            text=True,
            capture_output=True,
        )

        finished_at = datetime.now()
        duration_seconds = round((finished_at - started_at).total_seconds(), 2)

        success = process.returncode == 0

        attempt_result = {
            "attempt_number": attempt_number,
            "success": success,
            "returncode": process.returncode,
            "duration_seconds": duration_seconds,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "stdout": process.stdout,
            "stderr": process.stderr,
        }

        attempts.append(attempt_result)

        final_result = {
            "index": index,
            "total": total,
            "success": success,
            "returncode": process.returncode,
            "category": query["category"],
            "keywords": query["keywords"],
            "location": query["location"],
            "work_mode": query["work_mode"],
            "lookback_days": query["lookback_days"],
            "limit": query["limit"],
            "duration_seconds": duration_seconds,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "attempts": attempts,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }

        if success:
            print(f"Finished successfully: {label} in {duration_seconds}s")
            return final_result

        print(f"Failed: {label} in {duration_seconds}s")
        print(process.stderr[-1500:])

        if attempt_number <= retry_count:
            print(f"Retrying after {retry_delay_seconds}s...")
            time.sleep(retry_delay_seconds)

    return final_result


def write_run_log(results: list[dict]):
    LOG_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"linkedin_plan_collect_{timestamp}.json"

    with log_file.open("w", encoding="utf-8") as file:
        json.dump(results, file, ensure_ascii=False, indent=2)

    print(f"\nRun log saved: {log_file}")


def run_plan_collect(
    workers: int,
    max_queries: int | None,
    offset: int,
    categories: list[str] | None,
):
    queries = load_json_file(QUERIES_FILE)
    queries = [normalize_query(query) for query in queries]

    if categories:
        allowed_categories = set(categories)
        queries = [
            query
            for query in queries
            if query["category"] in allowed_categories
        ]

    if offset > 0:
        queries = queries[offset:]

    if max_queries:
        queries = queries[:max_queries]

    total = len(queries)

    if total == 0:
        print("No queries to run.")
        return

    print("LinkedIn parallel plan collector started.")
    print(f"Queries file: {QUERIES_FILE}")
    print(f"Total selected queries: {total}")
    print(f"Workers: {workers}")

    results = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_query = {
            executor.submit(run_single_query, query, index, total): query
            for index, query in enumerate(queries, start=1)
        }

        for future in as_completed(future_to_query):
            result = future.result()
            results.append(result)

    results = sorted(results, key=lambda item: item["index"])

    successful = sum(1 for result in results if result["success"])
    failed = len(results) - successful

    write_run_log(results)

    print("\n" + "=" * 90)
    print("LinkedIn parallel plan collector finished.")
    print(f"Successful queries: {successful}")
    print(f"Failed queries: {failed}")
    print("=" * 90)

    if successful == 0:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Run LinkedIn expanded search queries in parallel."
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=get_default_workers(),
        help="Number of parallel query workers."
    )

    parser.add_argument(
        "--max-queries",
        type=int,
        default=None,
        help="Maximum number of queries to run."
    )

    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many queries from the beginning."
    )

    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Run only one or more categories. Can be repeated."
    )

    args = parser.parse_args()

    workers = args.workers

    if workers < 1:
        workers = 1

    if workers > 4:
        workers = 4

    run_plan_collect(
        workers=workers,
        max_queries=args.max_queries,
        offset=args.offset,
        categories=args.category,
    )


if __name__ == "__main__":
    main()