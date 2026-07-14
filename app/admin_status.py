import json
from datetime import datetime, timezone
import os
import shutil
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





def parse_iso_datetime(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None



def get_collection_performance() -> dict:
    path = Path(os.getenv("JOBPULSE_COLLECTION_HISTORY", "/app/logs/collection_history.jsonl"))

    if not path.exists():
        fallback = Path("/opt/jobpulse/logs/collection_history.jsonl")
        if fallback.exists():
            path = fallback

    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "recent": [],
            "avg_drain_per_success": None,
            "avg_duration_minutes": None,
            "success_count": 0,
            "failure_count": 0,
        }

    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-30:]:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    except Exception as exc:
        return {
            "exists": True,
            "path": str(path),
            "error": str(exc),
            "recent": [],
            "avg_drain_per_success": None,
            "avg_duration_minutes": None,
            "success_count": 0,
            "failure_count": 0,
        }

    successes = [r for r in rows if r.get("status") == "success"]
    failures = [r for r in rows if r.get("status") in ("failed", "aborted_auth")]

    drains = []
    durations = []

    for row in successes[-10:]:
        before = row.get("pending_before")
        after = row.get("pending_after")
        if isinstance(before, int) and isinstance(after, int):
            drain = before - after
            if drain > 0:
                drains.append(drain)

        duration = row.get("duration_seconds")
        if isinstance(duration, (int, float)) and duration > 0:
            durations.append(duration / 60)

    avg_drain = round(sum(drains) / len(drains), 2) if drains else None
    avg_duration = round(sum(durations) / len(durations), 2) if durations else None

    return {
        "exists": True,
        "path": str(path),
        "recent": rows[-10:],
        "avg_drain_per_success": avg_drain,
        "avg_duration_minutes": avg_duration,
        "success_count": len(successes),
        "failure_count": len(failures),
    }


def get_collection_heartbeat() -> dict:
    path = Path(os.getenv("JOBPULSE_COLLECTION_HEARTBEAT", "/app/logs/collection_heartbeat.json"))
    if not path.exists():
        fallback = Path("/opt/jobpulse/logs/collection_heartbeat.json")
        if fallback.exists():
            path = fallback

    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "last_status": "missing",
            "age_minutes": None,
        }

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return {
            "exists": True,
            "path": str(path),
            "last_status": "unreadable",
            "last_message": str(exc),
            "age_minutes": None,
        }

    updated_at = parse_iso_datetime(data.get("updated_at"))
    now = datetime.now(timezone.utc)

    age_minutes = None
    if updated_at:
        age_minutes = round((now - updated_at).total_seconds() / 60, 2)

    data["exists"] = True
    data["path"] = str(path)
    data["age_minutes"] = age_minutes

    return data


def get_linkedin_auth_state() -> dict:
    state_path = Path(os.getenv("LINKEDIN_STORAGE_STATE", "/app/.auth/linkedin_storage_state.json"))

    if not state_path.exists():
        fallback = Path("/opt/jobpulse/.auth/linkedin_storage_state.json")
        if fallback.exists():
            state_path = fallback

    if not state_path.exists():
        return {
            "exists": False,
            "path": str(state_path),
            "size_bytes": 0,
            "updated_at": None,
            "age_hours": None,
            "status": "missing",
        }

    stat = state_path.stat()
    updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    age_hours = round((now - updated_at).total_seconds() / 3600, 2)

    if age_hours >= 336:
        status = "critical"
    elif age_hours >= 168:
        status = "warning"
    else:
        status = "ok"

    return {
        "exists": True,
        "path": str(state_path),
        "size_bytes": stat.st_size,
        "updated_at": updated_at.isoformat(),
        "age_hours": age_hours,
        "status": status,
    }


def get_disk_usage(path: str = "/opt/jobpulse") -> dict:
    total, used, free = shutil.disk_usage(path)

    total_gb = round(total / 1024 / 1024 / 1024, 2)
    used_gb = round(used / 1024 / 1024 / 1024, 2)
    free_gb = round(free / 1024 / 1024 / 1024, 2)
    used_percent = round((used / total) * 100, 2) if total else 0

    return {
        "path": path,
        "total_gb": total_gb,
        "used_gb": used_gb,
        "free_gb": free_gb,
        "used_percent": used_percent,
    }


def tail_file(path: Path, max_lines: int = 200) -> list[str]:
    if not path.exists() or not path.is_file():
        return []

    max_lines = max(1, min(int(max_lines or 200), 1000))

    with path.open("r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return [line.rstrip("\n") for line in lines[-max_lines:]]




def get_postgres_backup_status() -> dict:
    path = Path(os.getenv("JOBPULSE_BACKUP_STATUS_FILE", "/app/logs/postgres_backup_status.json"))

    if not path.exists():
        fallback = Path("/opt/jobpulse/logs/postgres_backup_status.json")
        if fallback.exists():
            path = fallback

    if not path.exists():
        return {
            "exists": False,
            "ok": False,
            "path": str(path),
            "errors": ["PostgreSQL backup status file is missing."],
            "warnings": [],
        }

    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return {
            "exists": True,
            "ok": False,
            "path": str(path),
            "errors": [f"PostgreSQL backup status file is unreadable: {exc}"],
            "warnings": [],
        }

    data["exists"] = True
    data["path"] = str(path)
    return data


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
    disk_usage: dict | None = None,
    linkedin_auth: dict | None = None,
    collection_heartbeat: dict | None = None,
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

    heartbeat = collection_heartbeat or {}
    heartbeat_status = heartbeat.get("last_status")
    heartbeat_age = heartbeat.get("age_minutes")

    if not heartbeat.get("exists"):
        alerts.append({
            "level": "warning",
            "code": "collection_heartbeat_missing",
            "message": "Collection heartbeat file is missing. The safe collection cycle may not have run yet.",
        })
    elif heartbeat_status == "running" and heartbeat_age is not None and float(heartbeat_age) >= 60:
        alerts.append({
            "level": "critical",
            "code": "collection_cycle_stuck",
            "message": f"Collection cycle appears stuck. Last heartbeat is {float(heartbeat_age):.1f} minutes old.",
        })
    elif heartbeat_age is not None and float(heartbeat_age) >= 90:
        alerts.append({
            "level": "critical",
            "code": "collection_cron_stale",
            "message": f"Collection cron appears stale. Last heartbeat is {float(heartbeat_age):.1f} minutes old.",
        })
    elif heartbeat_status in ("failed", "aborted_auth"):
        alerts.append({
            "level": "critical" if heartbeat_status == "aborted_auth" else "warning",
            "code": f"collection_{heartbeat_status}",
            "message": heartbeat.get("last_message") or "Collection cycle did not complete successfully.",
        })

    auth = linkedin_auth or {}
    auth_age = auth.get("age_hours")

    if not auth.get("exists"):
        alerts.append({
            "level": "critical",
            "code": "linkedin_auth_missing",
            "message": "LinkedIn auth storage state file is missing.",
        })
    elif auth_age is not None and float(auth_age) >= 336:
        alerts.append({
            "level": "critical",
            "code": "linkedin_auth_very_old",
            "message": f"LinkedIn auth state is {float(auth_age):.1f} hours old. Refresh it soon.",
        })
    elif auth_age is not None and float(auth_age) >= 168:
        alerts.append({
            "level": "warning",
            "code": "linkedin_auth_old",
            "message": f"LinkedIn auth state is {float(auth_age):.1f} hours old. Consider refreshing it.",
        })

    disk_used = float((disk_usage or {}).get("used_percent") or 0)
    disk_free_gb = float((disk_usage or {}).get("free_gb") or 0)

    if disk_used >= 90:
        alerts.append({
            "level": "critical",
            "code": "disk_usage_critical",
            "message": f"Disk usage is {disk_used:.2f}% with {disk_free_gb:.2f} GB free.",
        })
    elif disk_used >= 80:
        alerts.append({
            "level": "warning",
            "code": "disk_usage_high",
            "message": f"Disk usage is {disk_used:.2f}% with {disk_free_gb:.2f} GB free.",
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


            linkedin_auth = get_linkedin_auth_state()
            collection_heartbeat = get_collection_heartbeat()
            collection_performance = get_collection_performance()
            disk_usage = get_disk_usage("/opt/jobpulse")
            postgres_backup = get_postgres_backup_status()

            alerts = build_alerts(
                job_stats=job_stats,
                bad_apply=bad_apply,
                demand_queue=demand_queue,
                coverage=coverage,
                backups=backups,
                disk_usage=disk_usage,
                linkedin_auth=linkedin_auth,
                collection_heartbeat=collection_heartbeat,
            )

            return clean_json({
                "status": "ok",
                "database": "connected",
                "disk": disk_usage,
                "postgres_backup": postgres_backup,
                "linkedin_auth": linkedin_auth,
                "collection_heartbeat": collection_heartbeat,
                "collection_performance": collection_performance,
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
            "linkedin_auth_preflight": [
                ["python", "-m", "scripts.linkedin_auth_preflight"],
            ],
            "run_collection_cycle": [
                ["bash", "-lc", 'cd /opt/jobpulse && mkdir -p /opt/jobpulse/logs && if flock -n /tmp/jobpulse_collection_cycle.lock nohup ./scripts/run_collection_cycle_safe.sh >> /opt/jobpulse/logs/admin_collection_cycle_now.log 2>&1 & then echo collection_cycle_started; else echo collection_cycle_already_running; fi'],
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
