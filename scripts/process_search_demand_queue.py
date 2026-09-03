import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config
from scripts.linkedin_auth_preflight import preflight_linkedin_auth
from scripts.search_transport.metrics import record_metric
from scripts.search_transport.retry_policy import decide_retry
from scripts import linkedin_plan_collect as lpc
from scripts.process_summary import (
    RESULT_PATH_ENV_VAR as PROCESS_SUMMARY_RESULT_PATH_ENV_VAR,
    build_summary,
    write_summary_atomic,
)


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
                # Per-task retry budget (Phase 3.4K, Section 8): fail_count
                # BEFORE this failure is what decide_retry() needs, so it is
                # read first, per row, rather than folded into one blind
                # bulk UPDATE -- a row that has already exhausted its
                # budget moves to the TERMINAL 'failed' status instead of
                # being requeued to 'pending' again, closing the
                # previously-unbounded retry loop. decide_retry() is the
                # single source of truth for this threshold; this function
                # never reimplements the comparison itself.
                cur.execute(
                    "SELECT id, fail_count FROM job_search_demand_queue WHERE id = ANY(%s);",
                    (ids,),
                )
                fail_counts_by_id = {row[0]: row[1] or 0 for row in cur.fetchall()}

                requeue_ids = []
                exhausted_ids = []

                for task_id in ids:
                    decision = decide_retry(fail_counts_by_id.get(task_id, 0))

                    if decision.exhausted:
                        exhausted_ids.append(task_id)
                    else:
                        requeue_ids.append(task_id)

                    record_metric(
                        "search_demand_queue_retry",
                        queue_status=decision.next_status,
                        fail_count=decision.attempts_used,
                        max_attempts=decision.max_attempts,
                    )

                error_text = str(error or "unknown error")[:1000]

                if requeue_ids:
                    cur.execute(
                        """
                        UPDATE job_search_demand_queue
                        SET status = 'pending',
                            locked_at = NULL,
                            fail_count = fail_count + 1,
                            last_error = %s
                        WHERE id = ANY(%s);
                        """,
                        (error_text, requeue_ids),
                    )

                if exhausted_ids:
                    cur.execute(
                        """
                        UPDATE job_search_demand_queue
                        SET status = 'failed',
                            locked_at = NULL,
                            fail_count = fail_count + 1,
                            last_error = %s
                        WHERE id = ANY(%s);
                        """,
                        (error_text, exhausted_ids),
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


def write_process_summary_if_configured(batch_report, had_pending_targets: bool):
    """No-op if JOBPULSE_PROCESS_SUMMARY_RESULT_PATH isn't set (e.g. this
    script run standalone by an operator, outside the wrapper). When it
    IS set, a write failure is NOT swallowed -- the caller must let this
    propagate, since an unrecorded summary cannot be trusted by
    scripts/run_collection_cycle_safe.sh reading the same path back out
    on the host side."""
    result_path = os.getenv(PROCESS_SUMMARY_RESULT_PATH_ENV_VAR)
    if not result_path:
        return

    summary = build_summary(batch_report, had_pending_targets=had_pending_targets)
    write_summary_atomic(result_path, summary)


def main():
    preflight_linkedin_auth()
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--skip-company-enrichment", action="store_true")
    parser.add_argument("--skip-post-processing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    rows = fetch_pending_targets(args.limit)

    if not rows:
        print("No pending search demand targets.")
        # Nothing to do is not a failure, but it must still be
        # DISTINGUISHABLE from "we tried and everything failed" -- write
        # a summary with had_pending_targets=False so the wrapper (which
        # otherwise only sees "the docker exec step exited 0") can tell
        # the two apart instead of treating a missing summary file as an
        # error, or a truly-empty run as if useful work happened.
        write_process_summary_if_configured(None, had_pending_targets=False)
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
        # No summary written here: --dry-run is an operator debugging
        # aid, never invoked by run_collection_cycle_safe.sh, so there is
        # no wrapper waiting to read a summary for this invocation.
        return

    mark_targets(ids, "running")

    plan_result_path = LOGS_DIR / f".plan_collect_result_{uuid.uuid4().hex}.json"

    env = {
        "LINKEDIN_QUERIES_FILE": str(DEMAND_QUERIES_FILE),
        "LINKEDIN_LIMIT": os.getenv("SEARCH_DEMAND_LINKEDIN_LIMIT", "20"),
        "LINKEDIN_MAX_PAGES": os.getenv("SEARCH_DEMAND_LINKEDIN_MAX_PAGES", "2"),
        "LINKEDIN_QUERY_COOLDOWN_HOURS": os.getenv("SEARCH_DEMAND_COOLDOWN_HOURS", "0"),
        lpc.PLAN_COLLECT_RESULT_PATH_ENV_VAR: str(plan_result_path),
    }

    batch_report = None

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

        # Success is never inferred from returncode alone: exit 0 paired
        # with a missing or malformed batch report is still a failure --
        # the report is the only machine-readable source of truth for
        # what linkedin_plan_collect actually did.
        try:
            batch_report = lpc.read_batch_result(plan_result_path)
        except lpc.BatchResultReadError as exc:
            raise RuntimeError(
                f"linkedin_plan_collect exited 0 but its batch report could not be used "
                f"(category={exc.category})"
            ) from exc

        if not args.skip_post_processing:
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
        else:
            print("Skipping post-processing: company backfill, logo sync, embeddings.")

        # Written BEFORE mark_targets(ids, "done") deliberately: if this
        # write fails, we must fall through to the `except` block below
        # and mark these rows "failed" (requeued) rather than leaving a
        # contradictory "done" status that a failed summary write can
        # never undo cleanly.
        write_process_summary_if_configured(batch_report, had_pending_targets=True)

        # Preserved, documented behavior (NOT changed by this pass): the
        # entire fetched set is marked "done" once linkedin_plan_collect
        # reports at least one successful query, even if batch_report says
        # partial_failure=True. There is no exact query-to-row mapping
        # available here to attribute failures to specific ids -- guessing
        # one would risk silently losing/mis-marking real work. This is an
        # explicit, documented phase 2B blocker (see
        # docs/PRODUCTION_RUNBOOK.md), not an oversight. What IS new this
        # pass: batch_report["partial_failure"] is still truthfully
        # recorded in the process summary above, so the wrapper's
        # heartbeat shows "partial_failure" even though these demand-queue
        # rows are marked done.
        mark_targets(ids, "done")
        print("Search demand queue processed successfully.")

    except Exception as exc:
        mark_targets(ids, "failed", error=exc)
        raise

    finally:
        try:
            plan_result_path.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
