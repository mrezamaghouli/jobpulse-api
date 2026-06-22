import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from psycopg2.extras import RealDictCursor

from app.postgres_database import get_postgres_connection


router = APIRouter(prefix="/admin", tags=["admin"])


def serialize_value(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def serialize_row(row):
    return {key: serialize_value(value) for key, value in dict(row).items()}


def require_admin_key(x_admin_key: str | None):
    expected_key = os.getenv("ADMIN_API_KEY")

    if not expected_key:
        raise HTTPException(
            status_code=503,
            detail="Admin API is not configured.",
        )

    if not x_admin_key or x_admin_key != expected_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid admin key.",
        )


def fetch_one(cursor, sql: str, params=None):
    cursor.execute(sql, params or ())
    row = cursor.fetchone()
    if not row:
        return None
    return serialize_row(row)


def fetch_all(cursor, sql: str, params=None):
    cursor.execute(sql, params or ())
    return [serialize_row(row) for row in cursor.fetchall()]


@router.get("/summary")
def admin_summary(x_admin_key: str | None = Header(default=None)):
    require_admin_key(x_admin_key)

    with get_postgres_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            jobs = fetch_one(
                cursor,
                """
                SELECT
                    COUNT(*) AS total_jobs,
                    COUNT(*) FILTER (WHERE is_active = TRUE) AS active_jobs,
                    COUNT(*) FILTER (WHERE remote = TRUE OR LOWER(COALESCE(work_mode, '')) = 'remote') AS remote_jobs,
                    COUNT(*) FILTER (WHERE company_logo_url IS NOT NULL AND company_logo_url <> '') AS jobs_with_logo,
                    MAX(last_seen_at) AS latest_seen_at,
                    MAX(first_seen_at) AS latest_inserted_at
                FROM jobs
                """,
            )

            queue = fetch_all(
                cursor,
                """
                SELECT status, COUNT(*) AS count
                FROM job_search_demand_queue
                GROUP BY status
                ORDER BY count DESC
                """,
            )

            collection = fetch_one(
                cursor,
                """
                SELECT
                    COUNT(*) AS total_cycles,
                    COUNT(*) FILTER (WHERE status = 'success') AS successful_cycles,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed_cycles,
                    MAX(started_at) AS latest_cycle_at
                FROM collection_cycles
                """,
            )

            recent_searches = fetch_all(
                cursor,
                """
                SELECT
                    normalized_query,
                    job_family,
                    COUNT(*) AS search_count,
                    AVG(result_count)::numeric(10,2) AS avg_result_count,
                    MAX(created_at) AS last_seen_at
                FROM job_search_events
                WHERE created_at >= NOW() - INTERVAL '7 days'
                GROUP BY normalized_query, job_family
                ORDER BY search_count DESC, last_seen_at DESC
                LIMIT 10
                """,
            )

            return {
                "status": "ok",
                "jobs": jobs,
                "demand_queue": queue,
                "collection": collection,
                "top_searches_7d": recent_searches,
            }


@router.get("/collection-cycles")
def admin_collection_cycles(
    x_admin_key: str | None = Header(default=None),
    limit: int = Query(default=10, ge=1, le=100),
):
    require_admin_key(x_admin_key)

    with get_postgres_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            rows = fetch_all(
                cursor,
                """
                SELECT
                    id,
                    cycle_id,
                    trigger_name,
                    status,
                    seed_limit,
                    process_limit,
                    jobs_count_before,
                    jobs_count_after,
                    jobs_delta,
                    pending_before,
                    pending_after,
                    duration_seconds,
                    started_at,
                    finished_at,
                    error
                FROM collection_cycles
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )

            return {
                "count": len(rows),
                "results": rows,
            }


@router.get("/demand-queue")
def admin_demand_queue(
    x_admin_key: str | None = Header(default=None),
    status: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
):
    require_admin_key(x_admin_key)

    allowed_statuses = {"pending", "processing", "done", "failed", "active"}

    where = ""
    params = []

    if status:
        if status not in allowed_statuses:
            raise HTTPException(status_code=400, detail="Invalid status.")
        where = "WHERE status = %s"
        params.append(status)

    params.append(limit)

    with get_postgres_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            rows = fetch_all(
                cursor,
                f"""
                SELECT
                    id,
                    raw_query,
                    normalized_query,
                    job_family,
                    status,
                    priority_score,
                    search_count,
                    zero_result_count,
                    low_result_count,
                    last_result_count,
                    fail_count,
                    last_error,
                    first_seen_at,
                    last_seen_at,
                    last_collected_at
                FROM job_search_demand_queue
                {where}
                ORDER BY
                    CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                    priority_score DESC,
                    last_seen_at DESC
                LIMIT %s
                """,
                params,
            )

            return {
                "count": len(rows),
                "results": rows,
            }


@router.get("/search-events")
def admin_search_events(
    x_admin_key: str | None = Header(default=None),
    limit: int = Query(default=25, ge=1, le=100),
):
    require_admin_key(x_admin_key)

    with get_postgres_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            rows = fetch_all(
                cursor,
                """
                SELECT
                    id,
                    raw_query,
                    normalized_query,
                    job_family,
                    result_count,
                    high_quality_result_count,
                    created_at
                FROM job_search_events
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )

            return {
                "count": len(rows),
                "results": rows,
            }


@router.get("/jobs-health")
def admin_jobs_health(x_admin_key: str | None = Header(default=None)):
    require_admin_key(x_admin_key)

    with get_postgres_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            by_source = fetch_all(
                cursor,
                """
                SELECT source, COUNT(*) AS count
                FROM jobs
                GROUP BY source
                ORDER BY count DESC
                """
            )

            by_work_mode = fetch_all(
                cursor,
                """
                SELECT COALESCE(work_mode, 'unknown') AS work_mode, COUNT(*) AS count
                FROM jobs
                GROUP BY COALESCE(work_mode, 'unknown')
                ORDER BY count DESC
                """
            )

            stale = fetch_one(
                cursor,
                """
                SELECT
                    COUNT(*) FILTER (WHERE last_seen_at < NOW() - INTERVAL '7 days') AS stale_7d,
                    COUNT(*) FILTER (WHERE last_seen_at < NOW() - INTERVAL '14 days') AS stale_14d,
                    COUNT(*) FILTER (WHERE company_logo_url IS NULL OR company_logo_url = '') AS missing_logo
                FROM jobs
                """
            )

            return {
                "status": "ok",
                "by_source": by_source,
                "by_work_mode": by_work_mode,
                "quality": stale,
            }
