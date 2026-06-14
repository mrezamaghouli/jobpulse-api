import os
import sys
from pathlib import Path

import psycopg2

from app.config import get_postgres_config


BASE_DIR = Path(__file__).resolve().parent.parent


REQUIRED_FILES = [
    "app/main.py",
    "app/config.py",
    "app/repositories/jobs_postgres_repository.py",
    "scripts/repair_jobpulse_schema.py",
    "scripts/linkedin_smart_query_planner.py",
    "scripts/linkedin_plan_collect.py",
    "scripts/linkedin_auto_progress_collect.py",
    "scripts/run_production_collection.py",
    "scripts/sync_job_lifecycle.py",
    "config/linkedin_expanded_queries.json",
]

REQUIRED_ENV_KEYS = [
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
]

REQUIRED_JOB_COLUMNS = [
    "id",
    "title",
    "company",
    "location",
    "source",
    "job_url",
    "linkedin_job_id",
    "company_id",
    "company_logo_url",
    "job_description",
    "work_mode",
    "apply_type",
    "apply_url",
    "first_seen_at",
    "last_seen_at",
    "is_active",
    "inactive_at",
    "inactive_reason",
    "archived_at",
    "deleted_at",
]

REQUIRED_TABLES = [
    "jobs",
    "companies",
    "linkedin_query_runs",
]


def print_ok(message: str):
    print(f"[OK] {message}")


def print_warn(message: str):
    print(f"[WARN] {message}")


def print_fail(message: str):
    print(f"[FAIL] {message}")


def check_files() -> bool:
    print("")
    print("=" * 100)
    print("Checking required files")
    print("=" * 100)

    success = True

    for relative_path in REQUIRED_FILES:
        path = BASE_DIR / relative_path

        if path.exists():
            print_ok(relative_path)
        else:
            print_fail(f"Missing file: {relative_path}")
            success = False

    return success


def check_env() -> bool:
    print("")
    print("=" * 100)
    print("Checking environment variables")
    print("=" * 100)

    success = True

    for key in REQUIRED_ENV_KEYS:
        value = os.getenv(key)

        if value:
            if "PASSWORD" in key:
                print_ok(f"{key}=***")
            else:
                print_ok(f"{key}={value}")
        else:
            print_fail(f"Missing env: {key}")
            success = False

    optional_envs = [
        "LINKEDIN_BROWSER",
        "LINKEDIN_QUERY_COOLDOWN_HOURS",
        "LINKEDIN_SKIP_EXISTING_ENRICHED",
        "JOB_INACTIVE_AFTER_DAYS",
        "JOB_ARCHIVE_AFTER_DAYS",
        "JOB_HARD_DELETE_ENABLED",
    ]

    for key in optional_envs:
        value = os.getenv(key)

        if value:
            print_ok(f"{key}={value}")
        else:
            print_warn(f"Optional env not set: {key}")

    return success


def check_database() -> bool:
    print("")
    print("=" * 100)
    print("Checking database connection and schema")
    print("=" * 100)

    success = True

    try:
        connection = psycopg2.connect(**get_postgres_config())
        cursor = connection.cursor()
        print_ok("Connected to PostgreSQL")
    except Exception as error:
        print_fail(f"Could not connect to PostgreSQL: {error}")
        return False

    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public';
        """
    )

    existing_tables = {row[0] for row in cursor.fetchall()}

    for table in REQUIRED_TABLES:
        if table in existing_tables:
            print_ok(f"Table exists: {table}")
        else:
            print_fail(f"Missing table: {table}")
            success = False

    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'jobs';
        """
    )

    existing_columns = {row[0] for row in cursor.fetchall()}

    for column in REQUIRED_JOB_COLUMNS:
        if column in existing_columns:
            print_ok(f"jobs.{column}")
        else:
            print_fail(f"Missing jobs column: {column}")
            success = False

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM jobs;
        """
    )

    jobs_count = cursor.fetchone()[0]
    print_ok(f"Jobs count: {jobs_count}")

    cursor.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE source = 'LinkedIn') AS total_linkedin,
            COUNT(*) FILTER (
                WHERE source = 'LinkedIn'
                  AND is_active = TRUE
                  AND archived_at IS NULL
                  AND deleted_at IS NULL
            ) AS active_linkedin
        FROM jobs;
        """
    )

    total_linkedin, active_linkedin = cursor.fetchone()
    print_ok(f"LinkedIn jobs: total={total_linkedin}, active={active_linkedin}")

    cursor.close()
    connection.close()

    return success


def check_python_imports() -> bool:
    print("")
    print("=" * 100)
    print("Checking Python imports")
    print("=" * 100)

    modules = [
        "app.main",
        "scripts.repair_jobpulse_schema",
        "scripts.linkedin_smart_query_planner",
        "scripts.linkedin_plan_collect",
        "scripts.linkedin_auto_progress_collect",
        "scripts.run_production_collection",
        "scripts.sync_job_lifecycle",
    ]

    success = True

    for module in modules:
        try:
            __import__(module)
            print_ok(f"Import OK: {module}")
        except Exception as error:
            print_fail(f"Import failed: {module} -> {error}")
            success = False

    return success


def main():
    print("")
    print("#" * 100)
    print("JobPulse Production Readiness Check")
    print("#" * 100)

    checks = [
        check_files(),
        check_env(),
        check_python_imports(),
        check_database(),
    ]

    print("")
    print("#" * 100)

    if all(checks):
        print("READY FOR DEPLOYMENT ✅")
        print("#" * 100)
        return 0

    print("NOT READY YET ❌")
    print("Fix the failed checks above before deploying.")
    print("#" * 100)
    return 1


if __name__ == "__main__":
    sys.exit(main())