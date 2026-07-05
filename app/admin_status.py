import os
from decimal import Decimal
from pathlib import Path
from typing import Any

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


def clean_json(value: Any):
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, list):
        return [clean_json(item) for item in value]

    if isinstance(value, dict):
        return {key: clean_json(item) for key, item in value.items()}

    return value


def tail_file(path: Path, max_lines: int = 200) -> list[str]:
    if not path.exists() or not path.is_file():
        return []

    max_lines = max(1, min(int(max_lines or 200), 1000))

    with path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return [line.rstrip("\n") for line in lines[-max_lines:]]



def count_status(items: list[dict], status: str) -> int:
    for item in items or []:
        if item.get("status") == status:
            return int(item.get("count") or 0)
    return 0


def build_alerts(
    job_stats: dict,
    bad_apply: dict,
    demand_queue: list[dict],
    coverage: list[dict],
    backups: list[Path],
) -> list[dict]:
    alerts = []

    bad_apply_count = int(bad_apply.get("bad_external_apply_count") or 0)
    jobs_seen_1h = int(job_stats.get("jobs_seen_last_hour") or 0)
    jobs_added_24h = int(job_stats.get("jobs_added_last_24h") or 0)

    demand_failed = count_status(demand_queue, "failed")
    demand_running = count_status(demand_queue, "running")
    coverage_failed = count_status(coverage, "failed")
    coverage_queued = count_status(coverage, "queued")

    if bad_apply_count > 0:
        alerts.append({
            "level": "critical",
            "code": "bad_external_apply",
            "message": f"{bad_apply_count} bad external apply records found in the last 24h.",
        })

    if jobs_seen_1h == 0:
        alerts.append({
            "level": "warning",
            "code": "no_jobs_seen_1h",
            "message": "No jobs were seen in the last hour.",
        })

    if jobs_added_24h == 0:
        alerts.append({
            "level": "warning",
            "code": "no_jobs_added_24h",
            "message": "No new jobs were added in the last 24 hours.",
        })

    if demand_failed > 0:
        alerts.append({
            "level": "warning",
            "code": "demand_queue_failed",
            "message": f"{demand_failed} demand queue tasks are failed.",
        })

    if coverage_failed > 0:
        alerts.append({
            "level": "warning",
            "code": "coverage_failed",
            "message": f"{coverage_failed} coverage tasks are failed.",
        })

    if coverage_queued > 50:
        alerts.append({
            "level": "warning",
            "code": "coverage_queue_backlog",
            "message": f"{coverage_queued} coverage tasks are still queued.",
        })

    if demand_running > 20:
        alerts.append({
            "level": "warning",
            "code": "demand_running_backlog",
            "message": f"{demand_running} demand queue tasks are still running.",
        })

    if not backups:
        alerts.append({
            "level": "critical",
            "code": "no_backups",
            "message": "No database backup files are visible.",
        })

    return alerts


def register_admin_status_routes(app):
    @app.get("/api/admin/status")
    @app.get("/admin/status")
    def admin_status(x_admin_token: str | None = Header(default=None)):
        require_admin_token(x_admin_token)

        conn = psycopg2.connect(**get_postgres_config())

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                      COUNT(*) AS total_jobs,
                      MAX(first_seen_at) AS newest_first_seen,
                      MAX(last_seen_at) AS newest_last_seen,
                      COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '1 hour') AS jobs_added_last_hour,
                      COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '1 hour') AS jobs_seen_last_hour,
                      COUNT(*) FILTER (WHERE first_seen_at >= NOW() - INTERVAL '24 hours') AS jobs_added_last_24h,
                      COUNT(*) FILTER (WHERE last_seen_at >= NOW() - INTERVAL '24 hours') AS jobs_seen_last_24h
                    FROM jobs;
                """)
                job_stats = dict(cur.fetchone() or {})

                cur.execute("""
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
                """)
                bad_apply = dict(cur.fetchone() or {})

                cur.execute("""
                    SELECT status, COUNT(*) AS count
                    FROM job_search_demand_queue
                    GROUP BY status
                    ORDER BY status;
                """)
                demand_queue = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT status, COUNT(*) AS count
                    FROM job_collection_coverage
                    GROUP BY status
                    ORDER BY status;
                """)
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

                cur.execute("""
                    SELECT
                      c.country_name AS country,
                      cov.status,
                      COUNT(*) AS count
                    FROM job_collection_coverage cov
                    JOIN job_catalog_countries c ON c.id = cov.country_id
                    WHERE cov.country_priority = 1
                    GROUP BY c.country_name, cov.status
                    ORDER BY c.country_name, cov.status;
                """)
                coverage_by_country = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT
                      id,
                      raw_query,
                      job_family,
                      filters_json,
                      status,
                      last_result_count,
                      priority_score,
                      fail_count,
                      last_error,
                      first_seen_at,
                      last_seen_at,
                      last_collected_at
                    FROM job_search_demand_queue
                    ORDER BY last_collected_at DESC NULLS LAST, id DESC
                    LIMIT 20;
                """)
                recent_collection_tasks = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT
                      cov.country_priority,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE cov.status = 'done') AS done,
                      COUNT(*) FILTER (WHERE cov.status = 'pending') AS pending,
                      COUNT(*) FILTER (WHERE cov.status = 'queued') AS queued,
                      COUNT(*) FILTER (WHERE cov.status = 'failed') AS failed,
                      ROUND(
                        100.0 * COUNT(*) FILTER (WHERE cov.status = 'done') / NULLIF(COUNT(*), 0),
                        2
                      ) AS done_percent
                    FROM job_collection_coverage cov
                    GROUP BY cov.country_priority
                    ORDER BY cov.country_priority;
                """)
                coverage_progress = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT
                      c.country_name AS country,
                      cov.country_priority,
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE cov.status = 'done') AS done,
                      COUNT(*) FILTER (WHERE cov.status = 'pending') AS pending,
                      COUNT(*) FILTER (WHERE cov.status = 'queued') AS queued,
                      COUNT(*) FILTER (WHERE cov.status = 'failed') AS failed,
                      ROUND(
                        100.0 * COUNT(*) FILTER (WHERE cov.status = 'done') / NULLIF(COUNT(*), 0),
                        2
                      ) AS done_percent
                    FROM job_collection_coverage cov
                    JOIN job_catalog_countries c ON c.id = cov.country_id
                    GROUP BY c.country_name, cov.country_priority
                    ORDER BY cov.country_priority, c.country_name;
                """)
                coverage_country_progress = [dict(row) for row in cur.fetchall()]

            backups_dir = Path("/opt/jobpulse/backups")
            backups = sorted(backups_dir.glob("jobpulse_*.sql")) if backups_dir.exists() else []
            latest_backup = backups[-1] if backups else None


            alerts = build_alerts(
                job_stats=job_stats,
                bad_apply=bad_apply,
                demand_queue=demand_queue,
                coverage=coverage,
                backups=backups,
            )

            return clean_json({
                "status": "ok",
                "database": "connected",
                "alerts": alerts,
                "jobs": job_stats,
                "bad_apply": bad_apply,
                "demand_queue": demand_queue,
                "coverage": coverage,
                "recent_jobs": recent_jobs,
                "coverage_by_country": coverage_by_country,
                "recent_collection_tasks": recent_collection_tasks,
                "coverage_progress": coverage_progress,
                "coverage_country_progress": coverage_country_progress,
                "backups": {
                    "count": len(backups),
                    "latest": latest_backup.name if latest_backup else None,
                    "latest_size_mb": round(latest_backup.stat().st_size / 1024 / 1024, 2) if latest_backup else None,
                },
            })

        finally:
            conn.close()


def register_admin_logs_routes(app):
    @app.get("/api/admin/logs")
    @app.get("/admin/logs")
    def admin_logs(
        file: str = "collection",
        lines: int = 200,
        x_admin_token: str | None = Header(default=None),
    ):
        require_admin_token(x_admin_token)

        allowed = {
            "collection": Path("/opt/jobpulse/logs/collection_cycle.log"),
            "alerts": Path("/opt/jobpulse/logs/status_alerts.log"),
            "monitor": Path("/opt/jobpulse/logs/status_snapshots.log"),
            "monitor_cron": Path("/opt/jobpulse/logs/monitor_snapshot_cron.log"),
            "backup": Path("/opt/jobpulse/logs/db_backup.log"),
            "health": Path("/opt/jobpulse/logs/health_alert_cron.log"),
            "cleanup": Path("/opt/jobpulse/logs/production_cleanup_cron.log"),
        }

        if file not in allowed:
            raise HTTPException(status_code=400, detail="Invalid log file.")

        target = allowed[file]

        return {
            "status": "ok",
            "file": file,
            "path": str(target),
            "lines": tail_file(target, lines),
        }
