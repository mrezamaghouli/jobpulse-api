import argparse
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import psycopg2

from app.config import get_postgres_config


def now_utc():
    return datetime.now(timezone.utc)


def tail_text(value: str, max_chars: int = 12000) -> str:
    value = value or ""
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def connect():
    return psycopg2.connect(**get_postgres_config())


def ensure_schema(conn):
    with conn.cursor() as cursor:
        cursor.execute(
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
            """
        )
    conn.commit()


def scalar(conn, sql, default=0):
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
            if not row:
                return default
            return row[0] if row[0] is not None else default
    except Exception:
        return default


def jobs_count(conn):
    return scalar(conn, "SELECT COUNT(*) FROM jobs;", 0)


def pending_count(conn):
    return scalar(
        conn,
        "SELECT COUNT(*) FROM job_search_demand_queue WHERE status = 'pending';",
        0,
    )


def insert_cycle(conn, cycle_id, args, jobs_before, pending_before):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO collection_cycles (
                cycle_id,
                trigger_name,
                status,
                seed_limit,
                process_limit,
                workers,
                skip_company_enrichment,
                jobs_count_before,
                pending_before,
                started_at
            )
            VALUES (%s, %s, 'running', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                cycle_id,
                args.trigger,
                args.seed_limit,
                args.process_limit,
                args.workers,
                args.skip_company_enrichment,
                jobs_before,
                pending_before,
                now_utc(),
            ),
        )
    conn.commit()


def finish_cycle(conn, cycle_id, status, jobs_after, pending_after, started_monotonic, stdout, stderr, error=None):
    duration = round(time.monotonic() - started_monotonic, 2)

    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE collection_cycles
            SET
                status = %s,
                jobs_count_after = %s,
                jobs_delta = COALESCE(%s, 0) - COALESCE(jobs_count_before, 0),
                pending_after = %s,
                finished_at = %s,
                duration_seconds = %s,
                stdout_tail = %s,
                stderr_tail = %s,
                error = %s
            WHERE cycle_id = %s
            """,
            (
                status,
                jobs_after,
                jobs_after,
                pending_after,
                now_utc(),
                duration,
                tail_text(stdout),
                tail_text(stderr),
                tail_text(error, 4000) if error else None,
                cycle_id,
            ),
        )
    conn.commit()


def run_command(cmd, timeout_seconds):
    print("RUN:", " ".join(cmd), flush=True)

    completed = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )

    if completed.stdout:
        print(completed.stdout, flush=True)

    if completed.stderr:
        print(completed.stderr, file=sys.stderr, flush=True)

    return completed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--seed-limit", type=int, default=3)
    parser.add_argument("--process-limit", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--skip-company-enrichment", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("COLLECTION_CYCLE_TIMEOUT_SECONDS", "7200")))
    args = parser.parse_args()

    cycle_id = str(uuid.uuid4())
    started = time.monotonic()
    all_stdout = []
    all_stderr = []

    conn = connect()

    try:
        ensure_schema(conn)

        before_jobs = jobs_count(conn)
        before_pending = pending_count(conn)

        insert_cycle(conn, cycle_id, args, before_jobs, before_pending)

        print(f"collection_cycle_started cycle_id={cycle_id}", flush=True)
        print(f"jobs_before={before_jobs} pending_before={before_pending}", flush=True)

        if args.seed_limit > 0:
            seed_cmd = [
                sys.executable,
                "-m",
                "scripts.seed_autonomous_search_queue",
                "--limit",
                str(args.seed_limit),
            ]
            result = run_command(seed_cmd, args.timeout_seconds)
            all_stdout.append(result.stdout)
            all_stderr.append(result.stderr)

            if result.returncode != 0:
                raise RuntimeError(f"seed command failed with code {result.returncode}")

        if args.process_limit > 0:
            process_cmd = [
                sys.executable,
                "-m",
                "scripts.process_search_demand_queue",
                "--limit",
                str(args.process_limit),
                "--workers",
                str(args.workers),
            ]

            if args.skip_company_enrichment:
                process_cmd.append("--skip-company-enrichment")

            result = run_command(process_cmd, args.timeout_seconds)
            all_stdout.append(result.stdout)
            all_stderr.append(result.stderr)

            if result.returncode != 0:
                raise RuntimeError(f"process command failed with code {result.returncode}")

        after_jobs = jobs_count(conn)
        after_pending = pending_count(conn)

        finish_cycle(
            conn,
            cycle_id,
            "success",
            after_jobs,
            after_pending,
            started,
            "\n".join(all_stdout),
            "\n".join(all_stderr),
        )

        print(
            f"collection_cycle_finished cycle_id={cycle_id} status=success jobs_after={after_jobs} jobs_delta={after_jobs - before_jobs} pending_after={after_pending}",
            flush=True,
        )

    except Exception as exc:
        after_jobs = jobs_count(conn)
        after_pending = pending_count(conn)

        finish_cycle(
            conn,
            cycle_id,
            "failed",
            after_jobs,
            after_pending,
            started,
            "\n".join(all_stdout),
            "\n".join(all_stderr),
            error=str(exc),
        )

        print(f"collection_cycle_finished cycle_id={cycle_id} status=failed error={exc}", file=sys.stderr, flush=True)
        raise

    finally:
        conn.close()


if __name__ == "__main__":
    main()
