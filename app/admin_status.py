import os
from pathlib import Path

import psycopg2
from fastapi import Header, HTTPException
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


ADMIN_TOKEN = os.getenv("JOBPULSE_ADMIN_TOKEN", "").strip()


def require_admin_token(x_admin_token: str | None):
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Admin status is not configured. Set JOBPULSE_ADMIN_TOKEN.",
        )

    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token.")


def register_admin_status_routes(app):
    @app.get("/api/admin/status")
    @app.get("/admin/status")
    def admin_status(x_admin_token: str | None = Header(default=None)):
        require_admin_token(x_admin_token)

        conn = psycopg2.connect(**get_postgres_config())

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) AS total_jobs,
                      MAX(first_seen_at) AS newest_first_seen,
                      MAX(last_seen_at) AS newest_last_seen,
                      COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '1 hour') AS jobs_added_last_hour,
                      COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '1 hour') AS jobs_seen_last_hour,
                      COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '24 hours') AS jobs_added_last_24h,
                      COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '24 hours') AS jobs_seen_last_24h
                    FROM jobs;
                    """
                )
                job_stats = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT COUNT(*) AS bad_external_apply_count
                    FROM jobs
                    WHERE last_seen_at >= NOW() - INTERVAL '24 hours'
                      AND apply_type = 'external'
                      AND (
                        apply_url IS NULL
                        OR TRIM(apply_url) = ''
                        OR apply_url ILIKE '%linkedin.com%'
                        OR apply_url ILIKE '%lnkd.in%'
                      );
                    """
                )
                bad_apply = dict(cur.fetchone() or {})

                cur.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM job_search_demand_queue
                    GROUP BY status
                    ORDER BY status;
                    """
                )
                demand_queue = [dict(row) for row in cur.fetchall()]

                cur.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM job_collection_coverage
                    GROUP BY status
                    ORDER BY status;
                    """
                )
                coverage = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT
                      id,
                      linkedin_job_id,
                      title,
                      company,
                      location,
                      apply_type,
                      apply_label,
                      apply_url,
                      job_url,
                      first_seen_at,
                      last_seen_at
                    FROM jobs
                    WHERE deleted_at IS NULL
                    ORDER BY first_seen_at DESC NULLS LAST, id DESC
                    LIMIT 15;
                """)
                recent_jobs = [dict(row) for row in cur.fetchall()]

            backups_dir = Path("/opt/jobpulse/backups")
            backups = sorted(backups_dir.glob("jobpulse_*.sql")) if backups_dir.exists() else []
            latest_backup = backups[-1] if backups else None

            return {
                "status": "ok",
                "database": "connected",
                "jobs": job_stats,
                "bad_apply": bad_apply,
                "demand_queue": demand_queue,
                "coverage": coverage,
                "recent_jobs": recent_jobs,
                "backups": {
                    "count": len(backups),
                    "latest": latest_backup.name if latest_backup else None,
                    "latest_size_mb": round(latest_backup.stat().st_size / 1024 / 1024, 2) if latest_backup else None,
                },
            }

        finally:
            conn.close()
