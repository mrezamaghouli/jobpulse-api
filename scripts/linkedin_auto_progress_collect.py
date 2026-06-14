import argparse
import os
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import psycopg2

from app.config import get_postgres_config


BASE_DIR = Path(__file__).resolve().parent.parent
PROGRESS_FILE = BASE_DIR / "logs" / "linkedin_auto_progress.json"
QUERIES_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"


def load_queries_count() -> int:
    if not QUERIES_FILE.exists():
        raise FileNotFoundError(
            f"Expanded queries file not found: {QUERIES_FILE}. "
            "Run: python -m scripts.linkedin_query_expander"
        )

    with QUERIES_FILE.open("r", encoding="utf-8") as file:
        queries = json.load(file)

    return len(queries)


def load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {
            "next_offset": 0,
            "runs": []
        }

    with PROGRESS_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_progress(progress: dict):
    PROGRESS_FILE.parent.mkdir(exist_ok=True)

    with PROGRESS_FILE.open("w", encoding="utf-8") as file:
        json.dump(progress, file, ensure_ascii=False, indent=2)


def run_batch(offset: int, batch_size: int, workers: int, category: str | None):
    command = [
        sys.executable,
        "-m",
        "scripts.linkedin_plan_collect",
        "--max-queries",
        str(batch_size),
        "--offset",
        str(offset),
        "--workers",
        str(workers),
    ]

    if category:
        command.extend(["--category", category])

    print("\n" + "=" * 90)
    print("Running LinkedIn auto-progress batch")
    print(f"Offset: {offset}")
    print(f"Batch size: {batch_size}")
    print(f"Workers: {workers}")
    print(f"Category: {category or 'all'}")
    print("Command:")
    print(" ".join(command))
    print("=" * 90)

    started_at = datetime.now()

    process = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        text=True,
    )

    finished_at = datetime.now()
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    return {
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "offset": offset,
        "batch_size": batch_size,
        "workers": workers,
        "category": category,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
    }


def run_module_task(module_name: str, title: str, extra_env: dict | None = None) -> dict:
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)

    started_at = datetime.now()

    env = os.environ.copy()

    if extra_env:
        env.update(extra_env)

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            module_name,
        ],
        cwd=str(BASE_DIR),
        env=env,
        text=True,
        capture_output=True,
    )

    finished_at = datetime.now()
    duration_seconds = round((finished_at - started_at).total_seconds(), 2)

    if process.stdout:
        print(process.stdout)

    if process.stderr:
        print(process.stderr)

    return {
        "module": module_name,
        "success": process.returncode == 0,
        "returncode": process.returncode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
        "stdout": process.stdout,
        "stderr": process.stderr,
    }

def run_company_backfill() -> dict:
    return run_module_task(
        module_name="scripts.backfill_companies_from_jobs",
        title="Running company backfill after batch",
    )


def run_company_enrichment(company_enrich_limit: int, company_enrich_stale_days: int) -> dict:
    return run_module_task(
        module_name="scripts.enrich_companies_from_linkedin",
        title="Running company enrichment after batch",
        extra_env={
            "COMPANY_ENRICH_LIMIT": str(company_enrich_limit),
            "COMPANY_ENRICH_STALE_DAYS": str(company_enrich_stale_days),
        },
    )

def run_logo_sync() -> dict:
    return run_module_task(
        module_name="scripts.sync_job_company_logos",
        title="Running logo sync after batch",
    )

def run_schema_repair() -> dict:
    return run_module_task(
        module_name="scripts.repair_jobpulse_schema",
        title="Running schema repair before batch",
    )

def run_job_lifecycle_sync() -> dict:
    return run_module_task(
        module_name="scripts.sync_job_lifecycle",
        title="Running job lifecycle sync",
    )

def run_smart_query_planner() -> dict:
    return run_module_task(
        module_name="scripts.linkedin_smart_query_planner",
        title="Generating smart LinkedIn query plan",
    )

def run_stale_jobs_cleanup(stale_job_days: int) -> dict:
    return run_module_task(
        module_name="scripts.mark_stale_jobs_inactive",
        title="Running stale jobs cleanup after batch",
        extra_env={
            "STALE_JOB_DAYS": str(stale_job_days),
        },
    )

def get_database_summary() -> dict:
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            COUNT(*) AS total_linkedin_jobs,
            COUNT(*) FILTER (WHERE is_active = TRUE) AS active_linkedin_jobs,
            COUNT(*) FILTER (WHERE apply_url IS NOT NULL AND apply_url != '') AS jobs_with_apply_url,
            COUNT(*) FILTER (WHERE apply_type = 'easy_apply') AS easy_apply_jobs,
            COUNT(*) FILTER (WHERE apply_type = 'external') AS external_apply_jobs,
            COUNT(*) FILTER (WHERE job_description IS NOT NULL AND job_description != '') AS jobs_with_description,
            COUNT(*) FILTER (WHERE company_logo_url IS NOT NULL AND company_logo_url != '') AS jobs_with_company_logo,
            COUNT(DISTINCT company) AS unique_companies,
            MAX(last_seen_at) AS last_linkedin_job_seen_at
        FROM jobs
        WHERE source = 'LinkedIn';
        """
    )

    job_row = cursor.fetchone()

    cursor.execute(
        """
        SELECT
            COUNT(*) AS total_companies,
            COUNT(*) FILTER (WHERE logo_url IS NOT NULL AND logo_url != '') AS companies_with_logo,
            COUNT(*) FILTER (WHERE about IS NOT NULL AND about != '') AS companies_with_about,
            COUNT(*) FILTER (WHERE website_url IS NOT NULL AND website_url != '') AS companies_with_website,
            MAX(last_enriched_at) AS last_company_enriched_at
        FROM companies;
        """
    )

    company_row = cursor.fetchone()

    cursor.close()
    connection.close()

    summary = {
        "total_linkedin_jobs": job_row[0],
        "active_linkedin_jobs": job_row[1],
        "jobs_with_apply_url": job_row[2],
        "easy_apply_jobs": job_row[3],
        "external_apply_jobs": job_row[4],
        "jobs_with_description": job_row[5],
        "jobs_with_company_logo": job_row[6],
        "unique_companies": job_row[7],
        "last_linkedin_job_seen_at": job_row[8].isoformat() if job_row[8] else None,

        "total_companies": company_row[0],
        "companies_with_logo": company_row[1],
        "companies_with_about": company_row[2],
        "companies_with_website": company_row[3],
        "last_company_enriched_at": company_row[4].isoformat() if company_row[4] else None,
    }

    return summary


def print_database_summary(summary: dict):
    print("\n" + "=" * 90)
    print("Database summary after batch")
    print("=" * 90)

    print(f"Total LinkedIn jobs: {summary.get('total_linkedin_jobs')}")
    print(f"Active LinkedIn jobs: {summary.get('active_linkedin_jobs')}")
    print(f"Jobs with apply URL: {summary.get('jobs_with_apply_url')}")
    print(f"Easy Apply jobs: {summary.get('easy_apply_jobs')}")
    print(f"External Apply jobs: {summary.get('external_apply_jobs')}")
    print(f"Jobs with description: {summary.get('jobs_with_description')}")
    print(f"Jobs with company logo: {summary.get('jobs_with_company_logo')}")
    print(f"Unique companies: {summary.get('unique_companies')}")
    print(f"Total companies: {summary.get('total_companies')}")
    print(f"Companies with logo: {summary.get('companies_with_logo')}")
    print(f"Companies with about: {summary.get('companies_with_about')}")
    print(f"Companies with website: {summary.get('companies_with_website')}")
    print(f"Last job seen at: {summary.get('last_linkedin_job_seen_at')}")
    print(f"Last company enriched at: {summary.get('last_company_enriched_at')}")


def main():
    parser = argparse.ArgumentParser(
        description="Run LinkedIn plan collector with automatic offset progress."
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Number of queries to run in this batch."
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Number of parallel workers."
    )

    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Optional category filter, e.g. data, software, design."
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Reset progress back to offset 0."
    )

    parser.add_argument(
        "--show",
        action="store_true",
        help="Show current progress and exit."
    )

    parser.add_argument(
        "--company-enrich-limit",
        type=int,
        default=5,
        help="Number of companies to enrich after each successful batch."
    )

    parser.add_argument(
        "--company-enrich-stale-days",
        type=int,
        default=30,
        help="Only enrich companies older than this many days."
    )

    parser.add_argument(
        "--skip-company-enrichment",
        action="store_true",
        help="Skip LinkedIn company enrichment after batch."
    )

    parser.add_argument(
        "--stale-job-days",
        type=int,
        default=30,
        help="Mark LinkedIn jobs inactive if not seen for this many days."
    )

    args = parser.parse_args()

    total_queries = load_queries_count()
    progress = load_progress()

    if args.reset:
        progress = {
            "next_offset": 0,
            "runs": []
        }
        save_progress(progress)
        print("Progress reset to offset 0.")
        return

    if args.show:
        print("LinkedIn auto-progress status")
        print(f"Total expanded queries: {total_queries}")
        print(f"Next offset: {progress.get('next_offset', 0)}")
        print(f"Runs recorded: {len(progress.get('runs', []))}")
        return

    batch_size = max(1, args.batch_size)
    workers = max(1, min(args.workers, 4))

    offset = int(progress.get("next_offset", 0))

    if offset >= total_queries:
        print("All expanded queries have already been processed.")
        print(f"Total queries: {total_queries}")
        print(f"Current offset: {offset}")
        return

    remaining_queries = total_queries - offset
    effective_batch_size = min(batch_size, remaining_queries)

    schema_repair_result = run_schema_repair()

    if not schema_repair_result["success"]:
        result = {
            "success": False,
            "returncode": schema_repair_result["returncode"],
            "offset": offset,
            "batch_size": effective_batch_size,
            "workers": workers,
            "category": args.category,
            "started_at": schema_repair_result["started_at"],
            "finished_at": schema_repair_result["finished_at"],
            "duration_seconds": schema_repair_result["duration_seconds"],
            "schema_repair": schema_repair_result,
            "error": "Schema repair failed. Batch was not started.",
        }

        progress.setdefault("runs", [])
        progress["runs"].append(result)
        progress["next_offset"] = offset
        save_progress(progress)

        print("Schema repair failed. Batch cancelled.")
        print(f"Offset kept at: {offset}")
        return
    smart_planner_result = run_smart_query_planner()

    if not smart_planner_result["success"]:
        result = {
            "success": False,
            "returncode": smart_planner_result["returncode"],
            "offset": offset,
            "batch_size": effective_batch_size,
            "workers": workers,
            "category": args.category,
            "started_at": smart_planner_result["started_at"],
            "finished_at": smart_planner_result["finished_at"],
            "duration_seconds": smart_planner_result["duration_seconds"],
            "schema_repair": schema_repair_result,
            "smart_planner": smart_planner_result,
            "error": "Smart query planner failed. Batch was not started.",
        }

        progress.setdefault("runs", [])
        progress["runs"].append(result)
        progress["next_offset"] = offset
        save_progress(progress)

        print("Smart query planner failed. Batch cancelled.")
        print(f"Offset kept at: {offset}")
        return

    os.environ["LINKEDIN_QUERIES_FILE"] = "config\\linkedin_smart_queries.json"
    result = run_batch(
        offset=offset,
        batch_size=effective_batch_size,
        workers=workers,
        category=args.category,
    )
    result["smart_planner"] = smart_planner_result
    result["schema_repair"] = schema_repair_result

    company_backfill_result = None
    company_enrichment_result = None
    logo_sync_result = None
    stale_jobs_cleanup_result = None

    if result["success"]:
        company_backfill_result = run_company_backfill()

        if not args.skip_company_enrichment:
            company_enrichment_result = run_company_enrichment(
                company_enrich_limit=max(1, args.company_enrich_limit),
                company_enrich_stale_days=max(1, args.company_enrich_stale_days),
            )

        logo_sync_result = run_logo_sync()

        job_lifecycle_result = run_job_lifecycle_sync()
        result["job_lifecycle"] = job_lifecycle_result

        stale_jobs_cleanup_result = run_stale_jobs_cleanup(
            stale_job_days=max(1, args.stale_job_days),
        )

    result["company_backfill"] = company_backfill_result
    result["company_enrichment"] = company_enrichment_result
    result["logo_sync"] = logo_sync_result
    result["stale_jobs_cleanup"] = stale_jobs_cleanup_result

    database_summary = None

    if result["success"]:
        try:
            database_summary = get_database_summary()
            print_database_summary(database_summary)
        except Exception as error:
            print(f"Could not generate database summary: {error}")

    result["database_summary"] = database_summary

    progress.setdefault("runs", [])
    progress["runs"].append(result)

    if result["success"]:
        progress["next_offset"] = offset + effective_batch_size
        print("\nBatch finished successfully.")
        print(f"Next offset saved: {progress['next_offset']}")

        if company_backfill_result and company_backfill_result["success"]:
            print("Company backfill completed successfully.")
        else:
            print("Batch succeeded, but company backfill failed or did not run.")

        if args.skip_company_enrichment:
            print("Company enrichment skipped.")
        elif company_enrichment_result and company_enrichment_result["success"]:
            print("Company enrichment completed successfully.")
        else:
            print("Batch succeeded, but company enrichment failed or did not run.")

        if logo_sync_result and logo_sync_result["success"]:
            print("Logo sync completed successfully.")
        else:
            print("Batch succeeded, but logo sync failed or did not run.")

        if stale_jobs_cleanup_result and stale_jobs_cleanup_result["success"]:
            print("Stale jobs cleanup completed successfully.")
        else:
            print("Batch succeeded, but stale jobs cleanup failed or did not run.")
    else:
        progress["next_offset"] = offset
        print("\nBatch failed.")
        print(f"Offset kept at: {offset}")

    save_progress(progress)

    print(f"Progress file: {PROGRESS_FILE}")


if __name__ == "__main__":
    main()