import argparse
import sys
from datetime import datetime, timezone

import psycopg2

from app.config import get_postgres_config


MIGRATIONS = [
    (
        "001_schema_migrations_table",
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            migration_key TEXT UNIQUE NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
    ),
    (
        "002_jobs_production_columns",
        """
        DO $$
        BEGIN
            IF to_regclass('public.jobs') IS NOT NULL THEN
                ALTER TABLE jobs ADD COLUMN IF NOT EXISTS company_logo_url TEXT;
                ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'linkedin';
                ALTER TABLE jobs ADD COLUMN IF NOT EXISTS raw_data JSONB DEFAULT '{}'::jsonb;
                ALTER TABLE jobs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
            END IF;
        END $$;
        """,
    ),
    (
        "003_search_intelligence_tables",
        """
        CREATE TABLE IF NOT EXISTS job_search_events (
            id BIGSERIAL PRIMARY KEY,
            raw_query TEXT,
            normalized_query TEXT,
            job_family TEXT,
            filters_json JSONB DEFAULT '{}'::jsonb,
            result_count INTEGER DEFAULT 0,
            high_quality_result_count INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS job_search_demand_queue (
            id BIGSERIAL PRIMARY KEY,
            raw_query TEXT NOT NULL,
            normalized_query TEXT NOT NULL UNIQUE,
            job_family TEXT DEFAULT 'General',
            filters_json JSONB DEFAULT '{}'::jsonb,
            search_count INTEGER DEFAULT 0,
            zero_result_count INTEGER DEFAULT 0,
            low_result_count INTEGER DEFAULT 0,
            last_result_count INTEGER DEFAULT 0,
            priority_score DOUBLE PRECISION DEFAULT 0,
            status TEXT DEFAULT 'pending',
            locked_at TIMESTAMPTZ,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_collected_at TIMESTAMPTZ,
            fail_count INTEGER DEFAULT 0,
            last_error TEXT
        );

        CREATE TABLE IF NOT EXISTS linkedin_collection_targets (
            id BIGSERIAL PRIMARY KEY,
            job_family TEXT DEFAULT 'General',
            target_query TEXT NOT NULL,
            normalized_query TEXT,
            priority_score DOUBLE PRECISION DEFAULT 0,
            status TEXT DEFAULT 'active',
            last_collected_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ
        );
        """,
    ),
    (
        "004_search_intelligence_safe_columns",
        """
        ALTER TABLE job_search_events
            ADD COLUMN IF NOT EXISTS raw_query TEXT,
            ADD COLUMN IF NOT EXISTS normalized_query TEXT,
            ADD COLUMN IF NOT EXISTS job_family TEXT,
            ADD COLUMN IF NOT EXISTS filters_json JSONB DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS result_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS high_quality_result_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        ALTER TABLE job_search_demand_queue
            ADD COLUMN IF NOT EXISTS raw_query TEXT,
            ADD COLUMN IF NOT EXISTS normalized_query TEXT,
            ADD COLUMN IF NOT EXISTS job_family TEXT DEFAULT 'General',
            ADD COLUMN IF NOT EXISTS filters_json JSONB DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS search_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS zero_result_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS low_result_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_result_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS priority_score DOUBLE PRECISION DEFAULT 0,
            ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending',
            ADD COLUMN IF NOT EXISTS locked_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ADD COLUMN IF NOT EXISTS last_collected_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS last_error TEXT;

        ALTER TABLE linkedin_collection_targets
            ADD COLUMN IF NOT EXISTS job_family TEXT DEFAULT 'General',
            ADD COLUMN IF NOT EXISTS target_query TEXT,
            ADD COLUMN IF NOT EXISTS normalized_query TEXT,
            ADD COLUMN IF NOT EXISTS priority_score DOUBLE PRECISION DEFAULT 0,
            ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active',
            ADD COLUMN IF NOT EXISTS last_collected_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
        """,
    ),
    (
        "005_basic_indexes",
        """
        DO $$
        BEGIN
            IF to_regclass('public.jobs') IS NOT NULL THEN
                CREATE INDEX IF NOT EXISTS idx_jobs_linkedin_job_id ON jobs(linkedin_job_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at ON jobs(last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_first_seen_at ON jobs(first_seen_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
            END IF;
        END $$;

        CREATE INDEX IF NOT EXISTS idx_job_search_events_normalized_query
            ON job_search_events(normalized_query);

        CREATE INDEX IF NOT EXISTS idx_job_search_events_created_at
            ON job_search_events(created_at);

        CREATE INDEX IF NOT EXISTS idx_job_search_demand_queue_status_priority
            ON job_search_demand_queue(status, priority_score DESC);

        CREATE INDEX IF NOT EXISTS idx_job_search_demand_queue_last_seen
            ON job_search_demand_queue(last_seen_at DESC);

        CREATE INDEX IF NOT EXISTS idx_linkedin_collection_targets_status_priority
            ON linkedin_collection_targets(status, priority_score DESC);
        """,
    ),
    (
        "006_collection_cycles_reporting",
        """
        CREATE TABLE IF NOT EXISTS collection_cycles (
            id BIGSERIAL PRIMARY KEY,
            cycle_id TEXT UNIQUE NOT NULL,
            trigger_name TEXT DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'running',
            seed_limit INTEGER DEFAULT 0,
            process_limit INTEGER DEFAULT 0,
            workers INTEGER DEFAULT 1,
            skip_company_enrichment BOOLEAN DEFAULT TRUE,
            jobs_count_before INTEGER,
            jobs_count_after INTEGER,
            jobs_delta INTEGER,
            pending_before INTEGER,
            pending_after INTEGER,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            duration_seconds DOUBLE PRECISION,
            stdout_tail TEXT,
            stderr_tail TEXT,
            error TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_collection_cycles_started_at
            ON collection_cycles(started_at DESC);

        CREATE INDEX IF NOT EXISTS idx_collection_cycles_status
            ON collection_cycles(status);
        """,
    ),
    (
        "007_jobs_performance_indexes",
        """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;

        DO $$
        BEGIN
            IF to_regclass('public.jobs') IS NOT NULL THEN
                CREATE INDEX IF NOT EXISTS idx_jobs_active_last_seen
                    ON jobs(is_active, last_seen_at DESC);

                CREATE INDEX IF NOT EXISTS idx_jobs_active_date_posted_at
                    ON jobs(is_active, date_posted_at DESC);

                CREATE INDEX IF NOT EXISTS idx_jobs_work_mode
                    ON jobs(work_mode);

                CREATE INDEX IF NOT EXISTS idx_jobs_remote
                    ON jobs(remote);

                CREATE INDEX IF NOT EXISTS idx_jobs_source
                    ON jobs(source);

                CREATE INDEX IF NOT EXISTS idx_jobs_title_trgm
                    ON jobs USING GIN (title gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS idx_jobs_company_trgm
                    ON jobs USING GIN (company gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS idx_jobs_location_trgm
                    ON jobs USING GIN (location gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS idx_jobs_description_trgm
                    ON jobs USING GIN (job_description gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS idx_jobs_about_trgm
                    ON jobs USING GIN (job_about gin_trgm_ops);

                CREATE INDEX IF NOT EXISTS idx_jobs_active_remote_recent
                    ON jobs(last_seen_at DESC)
                    WHERE is_active = TRUE AND (remote = TRUE OR LOWER(COALESCE(work_mode, '')) = 'remote');
            END IF;
        END $$;
        """,
    ),
]


def ensure_schema_migrations_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            migration_key TEXT UNIQUE NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def is_applied(cursor, migration_key: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_key = %s LIMIT 1",
        (migration_key,),
    )
    return cursor.fetchone() is not None


def mark_applied(cursor, migration_key: str):
    cursor.execute(
        """
        INSERT INTO schema_migrations (migration_key, applied_at)
        VALUES (%s, %s)
        ON CONFLICT (migration_key) DO NOTHING
        """,
        (migration_key, datetime.now(timezone.utc)),
    )


def run_migrations(dry_run: bool = False):
    conn = psycopg2.connect(**get_postgres_config())
    conn.autocommit = False

    try:
        with conn.cursor() as cursor:
            ensure_schema_migrations_table(cursor)

            applied_count = 0
            skipped_count = 0

            for migration_key, sql in MIGRATIONS:
                if is_applied(cursor, migration_key):
                    print(f"SKIP already applied: {migration_key}")
                    skipped_count += 1
                    continue

                print(f"APPLY migration: {migration_key}")

                if not dry_run:
                    cursor.execute(sql)
                    mark_applied(cursor, migration_key)

                applied_count += 1

            if dry_run:
                conn.rollback()
                print("Dry run only. No migrations were committed.")
            else:
                conn.commit()

            print("Migration finished successfully.")
            print(f"Applied: {applied_count}")
            print(f"Skipped: {skipped_count}")

    except Exception:
        conn.rollback()
        print("Migration failed. Rolled back.", file=sys.stderr)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_migrations(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
