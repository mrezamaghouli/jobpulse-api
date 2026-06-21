import argparse
import json
import random
from datetime import datetime

import psycopg2
from psycopg2.extras import Json

from app.config import get_postgres_config


POPULAR_QUERIES = [
    # Backend / Software
    ("Backend", "backend engineer remote", 95),
    ("Backend", "python backend engineer remote", 92),
    ("Backend", "node.js backend engineer", 82),
    ("Backend", "java backend engineer", 78),
    ("Backend", "golang backend engineer remote", 76),
    ("Backend", "fastapi backend engineer", 70),
    ("Backend", "django backend engineer", 66),
    ("Backend", "elixir phoenix backend engineer", 48),

    # Frontend / Fullstack
    ("Frontend", "frontend engineer remote", 90),
    ("Frontend", "react frontend engineer", 88),
    ("Frontend", "next.js frontend engineer", 74),
    ("Frontend", "typescript frontend engineer", 76),
    ("Fullstack", "fullstack engineer remote", 92),
    ("Fullstack", "react node fullstack engineer", 80),

    # Data / AI
    ("Data", "data engineer remote", 90),
    ("Data", "senior data engineer", 82),
    ("Data", "analytics engineer remote", 72),
    ("AI / ML", "machine learning engineer remote", 88),
    ("AI / ML", "ai engineer remote", 86),
    ("AI / ML", "llm engineer remote", 80),
    ("AI / ML", "computer vision engineer", 60),

    # DevOps / Cloud / Security
    ("DevOps", "devops engineer remote", 85),
    ("DevOps", "site reliability engineer remote", 78),
    ("Cloud", "cloud engineer remote", 74),
    ("Cloud", "aws cloud engineer", 70),
    ("Security", "cybersecurity engineer remote", 70),
    ("Security", "application security engineer", 64),

    # Product / Design / QA
    ("Product", "product manager remote", 78),
    ("Product", "technical product manager", 72),
    ("Design / UX", "ux designer remote", 82),
    ("Design / UX", "product designer remote", 84),
    ("Design / UX", "ui ux designer", 70),
    ("QA", "qa automation engineer remote", 72),
    ("QA", "software test automation engineer", 64),

    # Mobile
    ("Mobile", "ios developer remote", 62),
    ("Mobile", "android developer remote", 62),
    ("Mobile", "flutter developer remote", 58),
]


def normalize_query(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def load_dynamic_targets(cursor, max_targets=50):
    """Use existing smart collection targets if the table exists."""
    try:
        cursor.execute(
            """
            SELECT
                COALESCE(job_family, 'General') AS job_family,
                target_query,
                COALESCE(priority_score, 0) AS priority_score
            FROM linkedin_collection_targets
            WHERE COALESCE(status, 'active') <> 'disabled'
              AND COALESCE(target_query, '') <> ''
            ORDER BY COALESCE(priority_score, 0) DESC
            LIMIT %s
            """,
            (max_targets,),
        )
        rows = cursor.fetchall()
    except Exception:
        cursor.connection.rollback()
        return []

    result = []
    for job_family, target_query, priority_score in rows:
        score = max(float(priority_score or 0), 20.0)
        result.append((job_family or "General", target_query, score))
    return result


def weighted_unique_sample(candidates, limit):
    selected = []
    selected_norms = set()

    pool = list(candidates)
    random.shuffle(pool)

    while pool and len(selected) < limit:
        weights = [max(float(item[2]), 1.0) for item in pool]
        picked = random.choices(pool, weights=weights, k=1)[0]
        pool.remove(picked)

        norm = normalize_query(picked[1])
        if norm in selected_norms:
            continue

        selected.append(picked)
        selected_norms.add(norm)

    return selected


def upsert_queue_item(cursor, job_family, query, priority_score, cooldown_hours, dry_run=False):
    normalized_query = normalize_query(query)
    if not normalized_query:
        return "skipped_empty"

    cursor.execute(
        """
        SELECT id, status, last_collected_at
        FROM job_search_demand_queue
        WHERE normalized_query = %s
        LIMIT 1
        """,
        (normalized_query,),
    )
    row = cursor.fetchone()

    if row:
        item_id, status, last_collected_at = row

        cursor.execute(
            """
            UPDATE job_search_demand_queue
            SET status = 'pending',
                locked_at = NULL,
                last_error = NULL,
                priority_score = GREATEST(COALESCE(priority_score, 0), %s),
                search_count = GREATEST(COALESCE(search_count, 0), 1),
                zero_result_count = GREATEST(COALESCE(zero_result_count, 0), 1),
                last_result_count = 0,
                last_seen_at = NOW()
            WHERE id = %s
              AND (
                    last_collected_at IS NULL
                    OR last_collected_at < NOW() - (%s * INTERVAL '1 hour')
                  )
            RETURNING id
            """,
            (priority_score, item_id, cooldown_hours),
        )
        updated = cursor.fetchone()

        if updated:
            return "requeued"

        return "skipped_cooldown"

    if dry_run:
        return "would_insert"

    cursor.execute(
        """
        INSERT INTO job_search_demand_queue (
            raw_query,
            normalized_query,
            job_family,
            filters_json,
            search_count,
            zero_result_count,
            low_result_count,
            last_result_count,
            priority_score,
            status,
            first_seen_at,
            last_seen_at
        )
        VALUES (
            %s, %s, %s, %s,
            1, 1, 0, 0,
            %s, 'pending',
            NOW(), NOW()
        )
        """,
        (
            query,
            normalized_query,
            job_family,
            Json({}),
            priority_score,
        ),
    )
    return "inserted"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--cooldown-hours", type=int, default=48)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = psycopg2.connect(**get_postgres_config())
    cursor = conn.cursor()

    dynamic_targets = load_dynamic_targets(cursor)
    candidates = dynamic_targets + POPULAR_QUERIES

    selected = weighted_unique_sample(candidates, args.limit)

    print(f"Autonomous queue seeding started")
    print(f"Selected queries: {len(selected)}")
    print(f"Cooldown hours: {args.cooldown_hours}")
    print(f"Dry run: {args.dry_run}")

    stats = {}

    for job_family, query, score in selected:
        status = upsert_queue_item(
            cursor,
            job_family=job_family,
            query=query,
            priority_score=float(score),
            cooldown_hours=args.cooldown_hours,
            dry_run=args.dry_run,
        )
        stats[status] = stats.get(status, 0) + 1
        print(f"- {status}: [{job_family}] {query} | priority={score}")

    if args.dry_run:
        conn.rollback()
    else:
        conn.commit()

    cursor.close()
    conn.close()

    print("Stats:", stats)
    print("Autonomous queue seeding finished")


if __name__ == "__main__":
    main()
