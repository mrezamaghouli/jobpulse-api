import json
import os
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2
from fastapi import Body, Header, HTTPException
from psycopg2.extras import RealDictCursor, Json

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

                ensure_admin_action_runs_table()
                cur.execute("""
                    SELECT
                      id,
                      action,
                      status,
                      started_at,
                      finished_at,
                      duration_seconds,
                      returncode
                    FROM admin_action_runs
                    ORDER BY started_at DESC
                    LIMIT 20;
                """)
                recent_admin_actions = [dict(row) for row in cur.fetchall()]


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
                "recent_admin_actions": recent_admin_actions,
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




def app_workdir() -> Path:
    if Path("/app/scripts").exists():
        return Path("/app")
    return Path.cwd()


def run_admin_command(args: list[str], timeout: int = 600) -> dict:
    started = time.time()

    try:
        result = subprocess.run(
            args,
            cwd=str(app_workdir()),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

        stdout = result.stdout[-12000:] if result.stdout else ""
        stderr = result.stderr[-12000:] if result.stderr else ""

        return {
            "args": args,
            "returncode": result.returncode,
            "duration_seconds": round(time.time() - started, 2),
            "stdout": stdout,
            "stderr": stderr,
        }

    except subprocess.TimeoutExpired as exc:
        return {
            "args": args,
            "returncode": 124,
            "duration_seconds": round(time.time() - started, 2),
            "stdout": exc.stdout or "",
            "stderr": f"Command timed out after {timeout} seconds.",
        }




def ensure_admin_action_runs_table():
    conn = psycopg2.connect(**get_postgres_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_action_runs (
                    id BIGSERIAL PRIMARY KEY,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    finished_at TIMESTAMPTZ,
                    duration_seconds NUMERIC,
                    returncode INTEGER,
                    results_json JSONB,
                    stdout_tail TEXT,
                    stderr_tail TEXT
                );
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS admin_action_runs_one_running_idx
                ON admin_action_runs ((status))
                WHERE status = 'running';
            """)
        conn.commit()
    finally:
        conn.close()


def begin_admin_action_run(action: str) -> int:
    ensure_admin_action_runs_table()

    conn = psycopg2.connect(**get_postgres_config())
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                DELETE FROM admin_action_runs
                WHERE status = 'running'
                  AND started_at < NOW() - INTERVAL '30 minutes';
            """)

            try:
                cur.execute("""
                    INSERT INTO admin_action_runs (action, status)
                    VALUES (%s, 'running')
                    RETURNING id;
                """, (action,))
            except Exception as exc:
                conn.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Another admin action is already running. Wait for it to finish.",
                ) from exc

            row = cur.fetchone()
        conn.commit()
        return int(row["id"])
    finally:
        conn.close()


def finish_admin_action_run(run_id: int, status: str, results: list[dict], started: float):
    duration = round(time.time() - started, 2)
    returncode = 0

    stdout_tail = ""
    stderr_tail = ""

    for item in results:
        rc = int(item.get("returncode") or 0)
        if rc != 0 and returncode == 0:
            returncode = rc

        if item.get("stdout"):
            stdout_tail += "\n" + str(item.get("stdout"))[-4000:]

        if item.get("stderr"):
            stderr_tail += "\n" + str(item.get("stderr"))[-4000:]

    conn = psycopg2.connect(**get_postgres_config())
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE admin_action_runs
                SET
                    status = %s,
                    finished_at = NOW(),
                    duration_seconds = %s,
                    returncode = %s,
                    results_json = %s,
                    stdout_tail = %s,
                    stderr_tail = %s
                WHERE id = %s;
            """, (
                status,
                duration,
                returncode,
                Json(results),
                stdout_tail[-8000:],
                stderr_tail[-8000:],
                run_id,
            ))
        conn.commit()
    finally:
        conn.close()


def register_admin_action_routes(app):
    @app.post("/api/admin/action")
    @app.post("/admin/action")
    def admin_action(
        payload: dict | None = Body(default=None),
        x_admin_token: str | None = Header(default=None),
    ):
        require_admin_token(x_admin_token)

        payload = payload or {}
        action = str(payload.get("action", "")).strip()

        action_commands = {
            "seed_priority_queue": [
                ["python", "-m", "scripts.seed_priority_coverage_queue", "--limit", "10", "--retry-after-hours", "24"],
            ],
            "process_queue_once": [
                ["python", "-m", "scripts.process_search_demand_queue", "--limit", "5", "--workers", "1", "--skip-company-enrichment"],
            ],
            "reconcile_coverage": [
                ["python", "-m", "scripts.reconcile_priority_coverage"],
            ],
            "telegram_alert_check": [
                ["python", "scripts/send_telegram_alerts.py"],
            ],
            "run_collection_cycle": [
                ["python", "-m", "scripts.seed_priority_coverage_queue", "--limit", "10", "--retry-after-hours", "24"],
                ["python", "-m", "scripts.process_search_demand_queue", "--limit", "5", "--workers", "1", "--skip-company-enrichment"],
                ["python", "-m", "scripts.reconcile_priority_coverage"],
            ],
        }

        if action not in action_commands:
            raise HTTPException(status_code=400, detail="Invalid admin action.")

        started = time.time()
        run_id = begin_admin_action_run(action)

        results = []
        success = True

        for cmd in action_commands[action]:
            item = run_admin_command(cmd)
            results.append(item)

            if item["returncode"] != 0:
                success = False
                break

        final_status = "ok" if success else "failed"
        finish_admin_action_run(run_id, final_status, results, started)

        return {
            "status": final_status,
            "action": action,
            "run_id": run_id,
            "results": results,
        }


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
