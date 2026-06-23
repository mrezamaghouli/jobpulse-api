import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
DEMAND_QUERIES_FILE = LOGS_DIR / "search_demand_queries.json"


def fetch_pending_targets(limit: int):
    conn = psycopg2.connect(**get_postgres_config())

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    raw_query,
                    normalized_query,
                    job_family,
                    filters_json,
                    priority_score
                FROM job_search_demand_queue
                WHERE status = 'pending'
                ORDER BY priority_score DESC, last_seen_at DESC
                LIMIT %s;
                """,
                (limit,),
            )

            return cur.fetchall()

    finally:
        conn.close()


def mark_targets(ids, status, error=None):
    if not ids:
        return

    conn = psycopg2.connect(**get_postgres_config())

    try:
        with conn.cursor() as cur:
            if status == "running":
                cur.execute(
                    """
                    UPDATE job_search_demand_queue
                    SET status = 'running',
                        locked_at = CURRENT_TIMESTAMP
                    WHERE id = ANY(%s);
                    """,
                    (ids,),
                )

            elif status == "done":
                cur.execute(
                    """
                    UPDATE job_search_demand_queue
                    SET status = 'done',
                        last_collected_at = CURRENT_TIMESTAMP,
                        locked_at = NULL,
                        last_error = NULL
                    WHERE id = ANY(%s);
                    """,
                    (ids,),
                )

            elif status == "failed":
                cur.execute(
                    """
                    UPDATE job_search_demand_queue
                    SET status = 'pending',
                        locked_at = NULL,
                        fail_count = fail_count + 1,
                        last_error = %s
                    WHERE id = ANY(%s);
                    """,
                    (str(error or "unknown error")[:1000], ids),
                )

        conn.commit()

    finally:
        conn.close()


def build_linkedin_queries(rows):
    queries = []

    for row in rows:
        filters = row.get("filters_json") or {}

        location = (
            filters.get("linkedin_location")
            or filters.get("location")
            or filters.get("country")
            or ""
        )
        work_mode = filters.get("work_mode") or "any"

        queries.append(
            {
                "category": row.get("job_family") or "Search Demand",
                "keywords": row.get("raw_query") or row.get("normalized_query"),
                "location": location,
                "work_mode": work_mode,
                "lookback_days": 7,
                "limit": int(os.getenv("SEARCH_DEMAND_LINKEDIN_LIMIT", "20")),
            }
        )

    return queries


def run_module(module_name, extra_env=None):
    env = os.environ.copy()

    if extra_env:
        env.update(extra_env)

    print("")
    print("=" * 90)
    print(f"Running: python -m {module_name}")
    print("=" * 90)

    return subprocess.run(
        [sys.executable, "-m", module_name],
        cwd=str(BASE_DIR),
        env=env,
        text=True,
    ).returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--skip-company-enrichment", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = fetch_pending_targets(args.limit)

    if not rows:
        print("No pending search demand targets.")
        return

    ids = [row["id"] for row in rows]
    queries = build_linkedin_queries(rows)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    with DEMAND_QUERIES_FILE.open("w", encoding="utf-8") as file:
        json.dump(queries, file, ensure_ascii=False, indent=2)

    print(f"Prepared {len(queries)} demand queries:")
    print(DEMAND_QUERIES_FILE)

    for query in queries:
        print(
            f"- {query['category']} | {query['keywords']} | "
            f"{query['location'] or 'Worldwide'} | {query['work_mode']}"
        )

    if args.dry_run:
        print("Dry run only. Nothing collected.")
        return

    mark_targets(ids, "running")

    env = {
        "LINKEDIN_QUERIES_FILE": str(DEMAND_QUERIES_FILE),
        "LINKEDIN_LIMIT": os.getenv("SEARCH_DEMAND_LINKEDIN_LIMIT", "20"),
        "LINKEDIN_MAX_PAGES": os.getenv("SEARCH_DEMAND_LINKEDIN_MAX_PAGES", "2"),
        "LINKEDIN_QUERY_COOLDOWN_HOURS": os.getenv("SEARCH_DEMAND_COOLDOWN_HOURS", "0"),
    }

    try:
        code = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.linkedin_plan_collect",
                "--workers",
                str(args.workers),
                "--max-queries",
                str(len(queries)),
            ],
            cwd=str(BASE_DIR),
            env={**os.environ.copy(), **env},
            text=True,
        ).returncode

        if code != 0:
            raise RuntimeError(f"linkedin_plan_collect failed with code {code}")

        run_module("scripts.backfill_companies_from_jobs")

        if not args.skip_company_enrichment:
            run_module(
                "scripts.enrich_companies_from_linkedin",
                extra_env={
                    "COMPANY_ENRICH_LIMIT": os.getenv("SEARCH_DEMAND_COMPANY_ENRICH_LIMIT", "50"),
                    "COMPANY_ENRICH_STALE_DAYS": "30",
                },
            )

        run_module("scripts.sync_job_company_logos")
        run_module("scripts.build_job_search_embeddings")

        mark_targets(ids, "done")
        print("Search demand queue processed successfully.")

    except Exception as exc:
        mark_targets(ids, "failed", error=exc)
        raise


if __name__ == "__main__":
    main()