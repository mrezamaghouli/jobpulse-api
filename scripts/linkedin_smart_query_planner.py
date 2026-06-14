import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from app.config import get_postgres_config


BASE_DIR = Path(__file__).resolve().parent.parent
EXPANDED_QUERIES_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"
SMART_QUERIES_FILE = BASE_DIR / "config" / "linkedin_smart_queries.json"
SMART_REPORT_FILE = BASE_DIR / "logs" / "linkedin_smart_query_report.json"


def make_query_signature(query: dict) -> str:
    raw_signature = "|".join(
        [
            str(query.get("category", "")).strip().lower(),
            str(query.get("keywords", "")).strip().lower(),
            str(query.get("location", "")).strip().lower(),
            str(query.get("work_mode", "")).strip().lower(),
            str(query.get("lookback_days", "")).strip().lower(),
        ]
    )

    return hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()


def get_cooldown_hours() -> int:
    raw_value = os.getenv("LINKEDIN_QUERY_COOLDOWN_HOURS", "12")

    try:
        value = int(raw_value)
    except ValueError:
        value = 12

    if value < 0:
        value = 0

    if value > 168:
        value = 168

    return value


def load_expanded_queries() -> list[dict]:
    if not EXPANDED_QUERIES_FILE.exists():
        raise FileNotFoundError(
            f"Expanded queries file not found: {EXPANDED_QUERIES_FILE}. "
            "Run: python -m scripts.linkedin_query_expander"
        )

    with EXPANDED_QUERIES_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, list):
        return data

    if isinstance(data, dict) and isinstance(data.get("queries"), list):
        return data["queries"]

    raise ValueError("Expanded queries file format is not supported.")


def load_query_run_history() -> dict:
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            query_signature,
            status,
            started_at,
            finished_at,
            duration_seconds,
            jobs_delta,
            failed_queries
        FROM linkedin_query_runs
        ORDER BY
            finished_at DESC NULLS LAST,
            started_at DESC NULLS LAST,
            id DESC;
        """
    )

    rows = cursor.fetchall()

    cursor.close()
    connection.close()

    history = {}

    for row in rows:
        signature = row[0]

        if signature not in history:
            history[signature] = {
                "runs": [],
                "success_count": 0,
                "failed_count": 0,
                "skipped_count": 0,
                "total_jobs_delta": 0,
                "successful_delta_count": 0,
                "total_duration": 0,
                "duration_count": 0,
                "latest_status": None,
                "latest_finished_at": None,
            }

        status = row[1]
        started_at = row[2]
        finished_at = row[3]
        duration_seconds = float(row[4] or 0)
        jobs_delta = int(row[5] or 0)
        failed_queries = int(row[6] or 0)

        record = {
            "status": status,
            "started_at": started_at.isoformat() if started_at else None,
            "finished_at": finished_at.isoformat() if finished_at else None,
            "duration_seconds": duration_seconds,
            "jobs_delta": jobs_delta,
            "failed_queries": failed_queries,
        }

        history[signature]["runs"].append(record)

        if history[signature]["latest_status"] is None:
            history[signature]["latest_status"] = status
            history[signature]["latest_finished_at"] = finished_at

        if status == "success":
            history[signature]["success_count"] += 1
            history[signature]["total_jobs_delta"] += jobs_delta
            history[signature]["successful_delta_count"] += 1

        elif status == "failed":
            history[signature]["failed_count"] += 1

        elif status == "skipped_recent_success":
            history[signature]["skipped_count"] += 1

        if duration_seconds > 0:
            history[signature]["total_duration"] += duration_seconds
            history[signature]["duration_count"] += 1

    return history


def calculate_score(query: dict, stats: dict | None, cooldown_hours: int) -> dict:
    now = datetime.now(timezone.utc)
    score = 0.0
    reasons = []

    lookback_days = int(query.get("lookback_days") or 60)

    if lookback_days <= 1:
        score += 80
        reasons.append("fresh_1d")

    elif lookback_days <= 7:
        score += 60
        reasons.append("fresh_7d")

    elif lookback_days <= 14:
        score += 35
        reasons.append("fresh_14d")

    elif lookback_days <= 30:
        score += 15
        reasons.append("fresh_30d")

    else:
        score += 5
        reasons.append("broad_old_range")

    if not stats:
        score += 500
        reasons.append("never_run")
        return {
            "score": round(score, 2),
            "should_skip": False,
            "reasons": reasons,
        }

    success_count = stats["success_count"]
    failed_count = stats["failed_count"]
    total_runs = max(1, success_count + failed_count)

    success_rate = success_count / total_runs
    failed_rate = failed_count / total_runs

    avg_jobs_delta = 0

    if stats["successful_delta_count"] > 0:
        avg_jobs_delta = stats["total_jobs_delta"] / stats["successful_delta_count"]

    avg_duration = 0

    if stats["duration_count"] > 0:
        avg_duration = stats["total_duration"] / stats["duration_count"]

    latest_status = stats.get("latest_status")
    latest_finished_at = stats.get("latest_finished_at")

    recent_success = False
    age_hours = None

    if latest_finished_at:
        if latest_finished_at.tzinfo is None:
            latest_finished_at = latest_finished_at.replace(tzinfo=timezone.utc)

        age_seconds = (now - latest_finished_at).total_seconds()
        age_hours = age_seconds / 3600

        if latest_status == "success" and age_hours < cooldown_hours:
            recent_success = True

    if recent_success:
        score -= 10000
        reasons.append("recent_success_cooldown")
        return {
            "score": round(score, 2),
            "should_skip": True,
            "reasons": reasons,
        }

    score += min(avg_jobs_delta * 10, 300)

    if avg_jobs_delta > 0:
        reasons.append(f"good_delta_{round(avg_jobs_delta, 2)}")

    score += success_rate * 80
    score -= failed_rate * 120

    if failed_count:
        reasons.append(f"failures_{failed_count}")

    if avg_duration > 0:
        duration_penalty = min(avg_duration / 60 * 2, 100)
        score -= duration_penalty
        reasons.append(f"duration_{round(avg_duration, 1)}s")

    if age_hours is not None:
        age_bonus = min(age_hours * 1.5, 120)
        score += age_bonus
        reasons.append(f"age_{round(age_hours, 1)}h")

    if latest_status == "failed":
        score -= 80
        reasons.append("latest_failed")

    if query.get("work_mode") in ["remote", "hybrid"]:
        score += 10
        reasons.append("valuable_work_mode")

    return {
        "score": round(score, 2),
        "should_skip": False,
        "reasons": reasons,
    }


def build_smart_query_plan():
    cooldown_hours = get_cooldown_hours()

    queries = load_expanded_queries()
    history = load_query_run_history()

    scored_queries = []

    for query in queries:
        signature = make_query_signature(query)
        stats = history.get(signature)

        scoring = calculate_score(
            query=query,
            stats=stats,
            cooldown_hours=cooldown_hours,
        )

        enriched_query = {
            **query,
            "query_signature": signature,
            "smart_score": scoring["score"],
            "smart_should_skip": scoring["should_skip"],
            "smart_reasons": scoring["reasons"],
        }

        scored_queries.append(enriched_query)

    runnable_queries = [
        query
        for query in scored_queries
        if not query["smart_should_skip"]
    ]

    skipped_queries = [
        query
        for query in scored_queries
        if query["smart_should_skip"]
    ]

    runnable_queries.sort(
        key=lambda item: item["smart_score"],
        reverse=True,
    )

    final_queries = runnable_queries + skipped_queries

    SMART_QUERIES_FILE.parent.mkdir(exist_ok=True)

    with SMART_QUERIES_FILE.open("w", encoding="utf-8") as file:
        json.dump(final_queries, file, ensure_ascii=False, indent=2)

    SMART_REPORT_FILE.parent.mkdir(exist_ok=True)

    report = {
        "generated_at": datetime.now().isoformat(),
        "cooldown_hours": cooldown_hours,
        "total_queries": len(scored_queries),
        "runnable_queries": len(runnable_queries),
        "skipped_recent_success": len(skipped_queries),
        "top_20": final_queries[:20],
        "output_file": str(SMART_QUERIES_FILE),
    }

    with SMART_REPORT_FILE.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    print("Smart LinkedIn query plan generated.")
    print(f"Total queries: {len(scored_queries)}")
    print(f"Runnable queries: {len(runnable_queries)}")
    print(f"Skipped recent success: {len(skipped_queries)}")
    print(f"Output: {SMART_QUERIES_FILE}")
    print(f"Report: {SMART_REPORT_FILE}")

    print("\nTop 10 smart queries:")

    for index, query in enumerate(final_queries[:10], start=1):
        print(
            f"{index}. score={query['smart_score']} | "
            f"{query.get('category')} | "
            f"{query.get('keywords')} | "
            f"{query.get('location') or 'Worldwide'} | "
            f"{query.get('work_mode')} | "
            f"reasons={','.join(query.get('smart_reasons', []))}"
        )


if __name__ == "__main__":
    build_smart_query_plan()