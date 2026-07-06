import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


import psycopg2

from app.config import get_postgres_config


INDEXES = [
    {
        "name": "idx_jobs_deleted_last_seen",
        "table": "jobs",
        "columns": ["deleted_at", "last_seen_at"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_deleted_last_seen ON jobs (deleted_at, last_seen_at DESC);",
    },
    {
        "name": "idx_jobs_first_seen",
        "table": "jobs",
        "columns": ["first_seen_at"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs (first_seen_at DESC);",
    },
    {
        "name": "idx_jobs_last_seen",
        "table": "jobs",
        "columns": ["last_seen_at"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs (last_seen_at DESC);",
    },
    {
        "name": "idx_jobs_apply_type_last_seen",
        "table": "jobs",
        "columns": ["apply_type", "last_seen_at"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_apply_type_last_seen ON jobs (apply_type, last_seen_at DESC);",
    },
    {
        "name": "idx_jobs_linkedin_job_id",
        "table": "jobs",
        "columns": ["linkedin_job_id"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_linkedin_job_id ON jobs (linkedin_job_id);",
    },
    {
        "name": "idx_jobs_title_trgm",
        "table": "jobs",
        "columns": ["title"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_title_trgm ON jobs USING gin (title gin_trgm_ops);",
        "extension": "pg_trgm",
    },
    {
        "name": "idx_jobs_company_trgm",
        "table": "jobs",
        "columns": ["company"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_company_trgm ON jobs USING gin (company gin_trgm_ops);",
        "extension": "pg_trgm",
    },
    {
        "name": "idx_jobs_location_trgm",
        "table": "jobs",
        "columns": ["location"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_jobs_location_trgm ON jobs USING gin (location gin_trgm_ops);",
        "extension": "pg_trgm",
    },
    {
        "name": "idx_demand_status_priority",
        "table": "job_search_demand_queue",
        "columns": ["status", "priority_score"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_demand_status_priority ON job_search_demand_queue (status, priority_score DESC);",
    },
    {
        "name": "idx_demand_last_collected",
        "table": "job_search_demand_queue",
        "columns": ["last_collected_at"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_demand_last_collected ON job_search_demand_queue (last_collected_at DESC NULLS LAST);",
    },
    {
        "name": "idx_coverage_status_priority",
        "table": "job_collection_coverage",
        "columns": ["status", "country_priority"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_coverage_status_priority ON job_collection_coverage (status, country_priority);",
    },
    {
        "name": "idx_coverage_country_status",
        "table": "job_collection_coverage",
        "columns": ["country_id", "status"],
        "sql": "CREATE INDEX IF NOT EXISTS idx_coverage_country_status ON job_collection_coverage (country_id, status);",
    },
]


def table_exists(cur, table):
    cur.execute("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        );
    """, (table,))
    return bool(cur.fetchone()[0])


def columns_exist(cur, table, columns):
    cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s;
    """, (table,))
    existing = {row[0] for row in cur.fetchall()}
    return all(col in existing for col in columns)


def main():
    conn = psycopg2.connect(**get_postgres_config())
    conn.autocommit = True

    try:
        with conn.cursor() as cur:
            for item in INDEXES:
                name = item["name"]
                table = item["table"]
                columns = item["columns"]

                if not table_exists(cur, table):
                    print(f"SKIP {name}: table {table} not found")
                    continue

                if not columns_exist(cur, table, columns):
                    print(f"SKIP {name}: missing columns on {table}: {columns}")
                    continue

                if item.get("extension"):
                    cur.execute(f"CREATE EXTENSION IF NOT EXISTS {item['extension']};")

                print(f"CREATE/VERIFY {name}")
                cur.execute(item["sql"])

            cur.execute("ANALYZE;")
            print("ANALYZE complete ✅")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
