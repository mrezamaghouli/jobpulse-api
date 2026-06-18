import json
import os
import re
from typing import Any

import psycopg2
from psycopg2.extras import Json

from app.config import get_postgres_config


def normalize_search_query(value: str | None) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9+#.\- /]", "", value)
    return value.strip()


def infer_job_family(query: str | None) -> str:
    q = normalize_search_query(query)

    rules = [
        ("Design / UX", ["ux", "ui", "product design", "designer", "figma"]),
        ("Frontend", ["frontend", "front-end", "react", "vue", "angular", "next.js", "nuxt"]),
        ("Backend", ["backend", "back-end", "python", "django", "fastapi", "node", "java", "spring", "go developer"]),
        ("Data / AI", ["data", "machine learning", "ml", "ai", "llm", "nlp", "analytics", "scientist"]),
        ("DevOps / Cloud", ["devops", "cloud", "aws", "azure", "gcp", "kubernetes", "docker", "sre"]),
        ("QA / Testing", ["qa", "test", "automation", "quality assurance"]),
        ("Product / Project", ["product manager", "project manager", "scrum", "agile"]),
        ("Marketing / Growth", ["marketing", "seo", "growth", "content"]),
        ("Sales / Business", ["sales", "business development", "account executive"]),
        ("Finance", ["finance", "accounting", "risk", "banking"]),
        ("HR / Recruiting", ["hr", "recruiter", "talent acquisition"]),
    ]

    for family, keywords in rules:
        if any(keyword in q for keyword in keywords):
            return family

    return "General"


def ensure_search_intelligence_schema(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_search_events (
            id SERIAL PRIMARY KEY,
            raw_query TEXT,
            normalized_query TEXT,
            filters_json JSONB DEFAULT '{}'::jsonb,
            result_count INTEGER DEFAULT 0,
            search_mode TEXT,
            searched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_job_search_events_normalized_query
        ON job_search_events(normalized_query);

        CREATE INDEX IF NOT EXISTS idx_job_search_events_searched_at
        ON job_search_events(searched_at);

        CREATE TABLE IF NOT EXISTS job_search_demand_queue (
            id SERIAL PRIMARY KEY,
            raw_query TEXT NOT NULL,
            normalized_query TEXT NOT NULL UNIQUE,
            job_family TEXT,
            filters_json JSONB DEFAULT '{}'::jsonb,
            search_count INTEGER DEFAULT 1,
            zero_result_count INTEGER DEFAULT 0,
            low_result_count INTEGER DEFAULT 0,
            last_result_count INTEGER DEFAULT 0,
            priority_score NUMERIC DEFAULT 0,
            status TEXT DEFAULT 'pending',
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            locked_at TIMESTAMP NULL,
            last_collected_at TIMESTAMP NULL,
            fail_count INTEGER DEFAULT 0,
            last_error TEXT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_job_search_demand_status_priority
        ON job_search_demand_queue(status, priority_score DESC, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS linkedin_collection_targets (
            id SERIAL PRIMARY KEY,
            target_query TEXT NOT NULL,
            normalized_query TEXT NOT NULL UNIQUE,
            job_family TEXT,
            location TEXT,
            work_mode TEXT,
            demand_score NUMERIC DEFAULT 0,
            inventory_score NUMERIC DEFAULT 0,
            zero_result_score NUMERIC DEFAULT 0,
            priority_score NUMERIC DEFAULT 0,
            demand_percentile NUMERIC DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_linkedin_collection_targets_priority
        ON linkedin_collection_targets(status, priority_score DESC);
        """
    )


def record_job_search_event(
    *,
    query: str | None,
    filters: dict[str, Any] | None,
    result_count: int,
    search_mode: str | None,
):
    normalized_query = normalize_search_query(query)

    if not normalized_query:
        return

    min_results = int(os.getenv("SEARCH_DEMAND_MIN_RESULTS", "3"))
    should_queue = int(result_count or 0) <= min_results

    filters = filters or {}
    job_family = infer_job_family(normalized_query)

    zero_result_increment = 1 if int(result_count or 0) == 0 else 0
    low_result_increment = 1 if should_queue and int(result_count or 0) > 0 else 0

    connection = psycopg2.connect(**get_postgres_config())

    try:
        with connection.cursor() as cursor:
            ensure_search_intelligence_schema(cursor)

            cursor.execute(
                """
                INSERT INTO job_search_events (
                    raw_query,
                    normalized_query,
                    filters_json,
                    result_count,
                    search_mode
                )
                VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    str(query or "").strip(),
                    normalized_query,
                    Json(filters),
                    int(result_count or 0),
                    search_mode,
                ),
            )

            if should_queue:
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
                        status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        1, %s, %s, %s,
                        %s,
                        'pending'
                    )
                    ON CONFLICT (normalized_query) DO UPDATE SET
                        raw_query = EXCLUDED.raw_query,
                        job_family = EXCLUDED.job_family,
                        filters_json = EXCLUDED.filters_json,
                        search_count = job_search_demand_queue.search_count + 1,
                        zero_result_count = job_search_demand_queue.zero_result_count + EXCLUDED.zero_result_count,
                        low_result_count = job_search_demand_queue.low_result_count + EXCLUDED.low_result_count,
                        last_result_count = EXCLUDED.last_result_count,
                        last_seen_at = CURRENT_TIMESTAMP,
                        status = CASE
                            WHEN job_search_demand_queue.status IN ('done', 'failed')
                            THEN 'pending'
                            ELSE job_search_demand_queue.status
                        END,
                        priority_score =
                            ((job_search_demand_queue.search_count + 1) * 1.0)
                            + ((job_search_demand_queue.zero_result_count + EXCLUDED.zero_result_count) * 3.0)
                            + ((job_search_demand_queue.low_result_count + EXCLUDED.low_result_count) * 1.5)
                            + GREATEST(0, 5 - EXCLUDED.last_result_count) * 0.6;
                    """,
                    (
                        str(query or "").strip(),
                        normalized_query,
                        job_family,
                        Json(filters),
                        zero_result_increment,
                        low_result_increment,
                        int(result_count or 0),
                        (
                            1
                            + zero_result_increment * 3
                            + low_result_increment * 1.5
                            + max(0, 5 - int(result_count or 0)) * 0.6
                        ),
                    ),
                )

        connection.commit()

    except Exception as exc:
        connection.rollback()
        print(f"Search intelligence logging failed: {exc}")

    finally:
        connection.close()