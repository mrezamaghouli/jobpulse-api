from datetime import date, datetime
from decimal import Decimal
from math import ceil
from typing import Any
import json
import math
import os
import re
from datetime import datetime, timezone

from psycopg2.extras import RealDictCursor

from app.postgres_database import get_postgres_connection


JOB_SELECT_COLUMNS = """
    id,
    linkedin_job_id,
    title,
    company,
    company_id,
    company_linkedin_url,
    company_logo_url,
    location,
    remote,
    work_mode,
    job_type,
    seniority,
    salary_min,
    salary_max,
    currency,
    source,
    job_url,
    job_description,
    job_about,
    date_posted_text,
    date_posted_at,
    apply_type,
    apply_url,
    apply_label,
    poster_name,
    poster_title,
    poster_profile_url,
    date_posted,
    first_seen_at,
    last_seen_at,
    is_active,
    inactive_at,
    inactive_reason,
    archived_at,
    deleted_at
"""


ALLOWED_SORT_FIELDS = {
    "id": "id",
    "title": "title",
    "company": "company",
    "location": "location",
    "date_posted": "date_posted",
    "first_seen_at": "first_seen_at",
    "last_seen_at": "last_seen_at",
    "salary_min": "salary_min",
    "salary_max": "salary_max",
    "source": "source",
    "apply_type": "apply_type",
}


def serialize_value(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    return value


def serialize_row(row):
    if row is None:
        return None

    return {
        key: serialize_value(value)
        for key, value in dict(row).items()
    }


def normalize_bool(value):
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    value_as_text = str(value).strip().lower()

    if value_as_text in ["true", "1", "yes", "y"]:
        return True

    if value_as_text in ["false", "0", "no", "n"]:
        return False

    return None


def normalize_positive_int(value, default_value, min_value=1, max_value=100):
    try:
        normalized_value = int(value)
    except (TypeError, ValueError):
        normalized_value = default_value

    if normalized_value < min_value:
        normalized_value = min_value

    if normalized_value > max_value:
        normalized_value = max_value

    return normalized_value


def build_jobs_filters(
    title=None,
    company=None,
    location=None,
    remote=None,
    seniority=None,
    job_type=None,
    min_salary=None,
    max_salary=None,
    source=None,
    apply_type=None,
    is_active=None,
    active_only=None,
):
    filters = []
    params = []

    if title:
        filters.append("title ILIKE %s")
        params.append(f"%{title}%")

    if company:
        filters.append("company ILIKE %s")
        params.append(f"%{company}%")

    if location:
        filters.append("location ILIKE %s")
        params.append(f"%{location}%")

    remote_value = normalize_bool(remote)
    if remote_value is not None:
        filters.append("remote = %s")
        params.append(remote_value)

    if seniority:
        filters.append("seniority = %s")
        params.append(seniority)

    if job_type:
        filters.append("job_type = %s")
        params.append(job_type)

    if min_salary:
        try:
            min_salary_value = float(min_salary)
            filters.append(
                """
                (
                    salary_min >= %s
                    OR salary_max >= %s
                )
                """
            )
            params.extend([min_salary_value, min_salary_value])
        except (TypeError, ValueError):
            pass

    if max_salary:
        try:
            max_salary_value = float(max_salary)
            filters.append(
                """
                (
                    salary_min <= %s
                    OR salary_max <= %s
                )
                """
            )
            params.extend([max_salary_value, max_salary_value])
        except (TypeError, ValueError):
            pass

    if source:
        filters.append("LOWER(source) = LOWER(%s)")
        params.append(source)

    if apply_type:
        filters.append("apply_type = %s")
        params.append(apply_type)

    active_value = normalize_bool(is_active)

    if active_value is None:
        active_value = normalize_bool(active_only)

    if active_value is not None:
        filters.append("is_active = %s")
        params.append(active_value)

    if filters:
        return "WHERE " + " AND ".join(filters), params

    return "", params


def get_safe_sort_clause(sort_by=None, sort_order=None):
    sort_field = ALLOWED_SORT_FIELDS.get(sort_by or "last_seen_at", "last_seen_at")

    normalized_sort_order = str(sort_order or "desc").lower()

    if normalized_sort_order not in ["asc", "desc"]:
        normalized_sort_order = "desc"

    direction = normalized_sort_order.upper()

    if sort_field in [
        "salary_min",
        "salary_max",
        "date_posted",
        "first_seen_at",
        "last_seen_at",
    ]:
        return f"{sort_field} {direction} NULLS LAST"

    return f"{sort_field} {direction}"


def get_jobs_from_db(**kwargs):
    return get_all_jobs_from_db(**kwargs)


def search_jobs_from_db(**kwargs):
    result = get_all_jobs_from_db(**kwargs)

    if isinstance(result, dict) and "results" in result:
        return result["results"]

    return result



_JOB_SEARCH_MODEL = None


def get_job_search_model():
    global _JOB_SEARCH_MODEL

    if _JOB_SEARCH_MODEL is None:
        from sentence_transformers import SentenceTransformer

        model_name = os.getenv(
            "JOB_SEARCH_EMBEDDING_MODEL",
            "sentence-transformers/all-MiniLM-L6-v2",
        )

        _JOB_SEARCH_MODEL = SentenceTransformer(model_name)

    return _JOB_SEARCH_MODEL


def normalize_search_terms(query):
    if not query:
        return []

    return [
        term.lower()
        for term in re.findall(r"[a-zA-Z0-9+#.\-]+", str(query))
        if len(term.strip()) >= 2
    ]


def text_value(value):
    return "" if value is None else str(value).lower()


def calculate_keyword_score(row, query):
    terms = normalize_search_terms(query)

    if not terms:
        return 0.0

    fields = {
        "title": 5.0,
        "company": 2.0,
        "location": 2.0,
        "work_mode": 1.5,
        "job_type": 1.5,
        "seniority": 1.5,
        "apply_type": 1.0,
        "source": 1.0,
        "job_description": 1.0,
        "job_about": 1.0,
    }

    score = 0.0
    max_score = sum(fields.values()) * len(terms)

    for field, weight in fields.items():
        value = text_value(row.get(field))

        if not value:
            continue

        for term in terms:
            if term in value:
                score += weight

    query_text = str(query).lower().strip()

    if query_text:
        title = text_value(row.get("title"))
        description = text_value(row.get("job_description"))

        if query_text in title:
            score += 8.0

        if query_text in description:
            score += 3.0

        max_score += 11.0

    if max_score <= 0:
        return 0.0

    return min(score / max_score, 1.0)


def parse_embedding(value):
    if value is None:
        return None

    if isinstance(value, list):
        return [float(item) for item in value]

    if isinstance(value, str):
        try:
            parsed = json.loads(value)

            if isinstance(parsed, list):
                return [float(item) for item in parsed]
        except Exception:
            return None

    return None


def calculate_semantic_score(query_vector, embedding_value):
    embedding = parse_embedding(embedding_value)

    if not query_vector or not embedding:
        return 0.0

    if len(query_vector) != len(embedding):
        return 0.0

    score = sum(float(a) * float(b) for a, b in zip(query_vector, embedding))

    return max(0.0, min(float(score), 1.0))


def calculate_freshness_score(row):
    value = row.get("last_seen_at") or row.get("first_seen_at")

    if not value:
        return 0.2

    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        days = (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).days

        if days <= 1:
            return 1.0

        if days <= 7:
            return 0.85

        if days <= 30:
            return 0.65

        if days <= 90:
            return 0.35

        return 0.15

    except Exception:
        return 0.2


def calculate_quality_score(row):
    score = 0.0

    if row.get("company_logo_url"):
        score += 0.30

    if row.get("apply_url"):
        score += 0.25

    if row.get("job_description"):
        score += 0.25

    if row.get("company_linkedin_url"):
        score += 0.20

    return min(score, 1.0)


def build_hybrid_query_vector(query):
    if not query:
        return None

    try:
        model = get_job_search_model()

        vector = model.encode(
            [str(query)],
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]

        return vector.astype(float).tolist()

    except Exception as exc:
        print(f"Semantic search disabled for this request: {exc}")
        return None


def serialize_search_result(row, score=None):
    data = dict(row)
    data.pop("search_embedding", None)

    serialized = serialize_row(data)

    if score is not None:
        serialized["search_score"] = round(float(score), 6)

    return serialized


def get_all_jobs_from_db(
    query=None,
    title=None,
    company=None,
    location=None,
    remote=None,
    work_mode=None,
    seniority=None,
    job_type=None,
    min_salary=None,
    max_salary=None,
    source=None,
    apply_type=None,
    is_active=None,
    active_only=None,
    page: int = 1,
    limit: int = 10,
    sort_by: str = "last_seen_at",
    sort_order: str = "desc",
):
    connection = None
    cursor = None

    try:
        connection = get_postgres_connection()
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        safe_page = max(1, int(page or 1))
        safe_limit = max(1, min(int(limit or 10), 500))
        offset = (safe_page - 1) * safe_limit

        where_clauses = []
        params = []

        if title:
            where_clauses.append("j.title ILIKE %s")
            params.append(f"%{title}%")

        if company:
            where_clauses.append("j.company ILIKE %s")
            params.append(f"%{company}%")

        if location:
            where_clauses.append("j.location ILIKE %s")
            params.append(f"%{location}%")

        if remote is not None:
            where_clauses.append("j.remote = %s")
            params.append(remote)

        if work_mode:
            where_clauses.append("j.work_mode = %s")
            params.append(work_mode)

        if seniority:
            where_clauses.append("j.seniority ILIKE %s")
            params.append(f"%{seniority}%")

        if job_type:
            where_clauses.append("j.job_type ILIKE %s")
            params.append(f"%{job_type}%")

        if min_salary is not None:
            where_clauses.append("j.salary_max >= %s")
            params.append(min_salary)

        if max_salary is not None:
            where_clauses.append("j.salary_min <= %s")
            params.append(max_salary)

        if source:
            where_clauses.append("j.source = %s")
            params.append(source)

        if apply_type:
            where_clauses.append("j.apply_type = %s")
            params.append(apply_type)

        if is_active is not None:
            where_clauses.append("j.is_active = %s")
            params.append(is_active)

        if active_only:
            where_clauses.append(
                """
                j.is_active = TRUE
                AND j.archived_at IS NULL
                AND j.deleted_at IS NULL
                """
            )
        else:
            where_clauses.append("j.deleted_at IS NULL")

        where_sql = ""

        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        allowed_sort_columns = {
            "id": "j.id",
            "title": "j.title",
            "company": "j.company",
            "location": "j.location",
            "date_posted": "j.date_posted",
            "first_seen_at": "j.first_seen_at",
            "last_seen_at": "j.last_seen_at",
            "salary_min": "j.salary_min",
            "salary_max": "j.salary_max",
        }

        safe_sort_by = allowed_sort_columns.get(sort_by, "j.last_seen_at")
        safe_sort_order = "ASC" if str(sort_order).lower() == "asc" else "DESC"

        use_hybrid_search = bool(query and str(query).strip())

        if not use_hybrid_search:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM jobs j
                {where_sql};
                """,
                params,
            )

            total_row = cursor.fetchone()
            total = total_row["total"] if total_row else 0

            cursor.execute(
                f"""
                SELECT
                    {JOB_SELECT_COLUMNS}
                FROM jobs j
                {where_sql}
                ORDER BY
                    {safe_sort_by} {safe_sort_order} NULLS LAST,
                    j.id DESC
                LIMIT %s
                OFFSET %s;
                """,
                params + [safe_limit, offset],
            )

            rows = cursor.fetchall()

            total_pages = 0
            if safe_limit > 0:
                total_pages = (int(total) + safe_limit - 1) // safe_limit

            return {
                "results": [serialize_search_result(row) for row in rows],
                "page": safe_page,
                "limit": safe_limit,
                "total": int(total or 0),
                "total_pages": total_pages,
                "search_mode": "filters",
            }

        model_name = os.getenv(
            "JOB_SEARCH_EMBEDDING_MODEL",
            "sentence-transformers/all-MiniLM-L6-v2",
        )

        candidate_limit = int(os.getenv("JOB_SEARCH_CANDIDATE_LIMIT", "3000"))
        candidate_limit = max(candidate_limit, offset + safe_limit * 5)
        candidate_limit = min(candidate_limit, int(os.getenv("JOB_SEARCH_CANDIDATE_MAX_LIMIT", "10000")))

        cursor.execute(
            f"""
            SELECT
                {JOB_SELECT_COLUMNS},
                e.embedding_json AS search_embedding
            FROM jobs j
            LEFT JOIN job_search_embeddings e
                ON e.job_id = j.id
               AND e.model_name = %s
            {where_sql}
            ORDER BY
                j.last_seen_at DESC NULLS LAST,
                j.id DESC
            LIMIT %s;
            """,
            [model_name] + params + [candidate_limit],
        )

        candidate_rows = cursor.fetchall()
        query_vector = build_hybrid_query_vector(query)
        semantic_threshold = float(os.getenv("JOB_SEARCH_SEMANTIC_THRESHOLD", "0.28"))

        ranked = []

        for row in candidate_rows:
            keyword_score = calculate_keyword_score(row, query)
            semantic_score = calculate_semantic_score(query_vector, row.get("search_embedding"))
            freshness_score = calculate_freshness_score(row)
            quality_score = calculate_quality_score(row)

            if query_vector:
                if keyword_score <= 0 and semantic_score < semantic_threshold:
                    continue
            else:
                if keyword_score <= 0:
                    continue

            final_score = (
                keyword_score * 0.45
                + semantic_score * 0.45
                + freshness_score * 0.05
                + quality_score * 0.05
            )

            ranked.append((final_score, row))

        ranked.sort(key=lambda item: (item[0], item[1].get("id") or 0), reverse=True)

        total = len(ranked)
        page_items = ranked[offset:offset + safe_limit]

        total_pages = 0
        if safe_limit > 0:
            total_pages = (int(total) + safe_limit - 1) // safe_limit

        return {
            "results": [
                serialize_search_result(row, score=score)
                for score, row in page_items
            ],
            "page": safe_page,
            "limit": safe_limit,
            "total": int(total or 0),
            "total_pages": total_pages,
            "search_mode": "hybrid",
        }

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


def get_job_by_id_from_db(job_id):
    connection = get_postgres_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT
                    {JOB_SELECT_COLUMNS}
                FROM jobs
                WHERE id = %s
                LIMIT 1;
                """,
                (job_id,)
            )

            row = cursor.fetchone()

        return serialize_row(row)

    finally:
        connection.close()


def get_job_from_db(job_id):
    return get_job_by_id_from_db(job_id)


def get_job_details_from_db(job_id):
    return get_job_by_id_from_db(job_id)


def get_jobs_stats_from_db():
    connection = get_postgres_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_jobs,

                    COUNT(*) FILTER (
                        WHERE source = 'LinkedIn'
                    ) AS linkedin_jobs,

                    COUNT(*) FILTER (
                        WHERE source = 'LinkedIn' AND is_active = TRUE
                    ) AS active_linkedin_jobs,

                    COUNT(*) FILTER (
                        WHERE source = 'LinkedIn' AND is_active = FALSE
                    ) AS inactive_linkedin_jobs,

                    COUNT(*) FILTER (
                        WHERE remote = TRUE
                    ) AS remote_jobs,

                    COUNT(*) FILTER (
                        WHERE remote = FALSE
                    ) AS onsite_jobs,

                    COUNT(*) FILTER (
                        WHERE apply_type = 'easy_apply'
                    ) AS easy_apply_jobs,

                    COUNT(*) FILTER (
                        WHERE apply_type = 'external'
                    ) AS external_apply_jobs,

                    COUNT(*) FILTER (
                        WHERE apply_url IS NOT NULL
                    ) AS jobs_with_apply_url,

                    COUNT(DISTINCT company) AS total_companies,

                    COUNT(DISTINCT location) AS total_locations,

                    MAX(last_seen_at) FILTER (
                        WHERE source = 'LinkedIn'
                    ) AS last_linkedin_job_seen_at,

                    MAX(first_seen_at) FILTER (
                        WHERE source = 'LinkedIn'
                    ) AS newest_linkedin_job_first_seen_at
                FROM jobs;
                """
            )

            stats = cursor.fetchone()

        return serialize_row(stats)

    finally:
        connection.close()


def get_job_stats_from_db():
    return get_jobs_stats_from_db()


def get_distinct_companies_from_db(limit=100):
    limit = normalize_positive_int(
        limit,
        default_value=100,
        min_value=1,
        max_value=500
    )

    connection = get_postgres_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT company, COUNT(*) AS job_count
                FROM jobs
                WHERE company IS NOT NULL
                  AND company != ''
                GROUP BY company
                ORDER BY job_count DESC, company ASC
                LIMIT %s;
                """,
                (limit,)
            )

            rows = cursor.fetchall()

        return [serialize_row(row) for row in rows]

    finally:
        connection.close()


def get_distinct_locations_from_db(limit=100):
    limit = normalize_positive_int(
        limit,
        default_value=100,
        min_value=1,
        max_value=500
    )

    connection = get_postgres_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT location, COUNT(*) AS job_count
                FROM jobs
                WHERE location IS NOT NULL
                  AND location != ''
                GROUP BY location
                ORDER BY job_count DESC, location ASC
                LIMIT %s;
                """,
                (limit,)
            )

            rows = cursor.fetchall()

        return [serialize_row(row) for row in rows]

    finally:
        connection.close()
