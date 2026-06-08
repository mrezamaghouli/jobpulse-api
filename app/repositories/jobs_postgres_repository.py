from datetime import date, datetime
from decimal import Decimal
from math import ceil
from typing import Any

from psycopg2.extras import RealDictCursor

from app.postgres_database import get_postgres_connection


JOB_SELECT_COLUMNS = """
    id,
    linkedin_job_id,
    title,
    company,
    company_linkedin_url,
    location,
    remote,
    job_type,
    seniority,
    salary_min,
    salary_max,
    currency,
    source,
    job_url,
    apply_type,
    apply_url,
    apply_label,
    poster_name,
    poster_title,
    poster_profile_url,
    date_posted,
    first_seen_at,
    last_seen_at,
    is_active
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


def get_jobs_from_db(
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
    page=1,
    limit=10,
    sort_by="last_seen_at",
    sort_order="desc",
    **ignored_kwargs,
):
    page = normalize_positive_int(
        page,
        default_value=1,
        min_value=1,
        max_value=100000
    )

    limit = normalize_positive_int(
        limit,
        default_value=10,
        min_value=1,
        max_value=100
    )

    offset = (page - 1) * limit

    where_clause, params = build_jobs_filters(
        title=title,
        company=company,
        location=location,
        remote=remote,
        seniority=seniority,
        job_type=job_type,
        min_salary=min_salary,
        max_salary=max_salary,
        source=source,
        apply_type=apply_type,
        is_active=is_active,
        active_only=active_only,
    )

    order_clause = get_safe_sort_clause(
        sort_by=sort_by,
        sort_order=sort_order
    )

    connection = get_postgres_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total_count
                FROM jobs
                {where_clause};
                """,
                params
            )

            count_row = cursor.fetchone()
            total_count = count_row["total_count"] if count_row else 0

            cursor.execute(
                f"""
                SELECT
                    {JOB_SELECT_COLUMNS}
                FROM jobs
                {where_clause}
                ORDER BY {order_clause}
                LIMIT %s
                OFFSET %s;
                """,
                params + [limit, offset]
            )

            rows = cursor.fetchall()

        total_pages = ceil(total_count / limit) if total_count else 0

        return {
            "results": [serialize_row(row) for row in rows],
            "count": total_count,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }

    finally:
        connection.close()


def search_jobs_from_db(**kwargs):
    return get_jobs_from_db(**kwargs)


def get_all_jobs_from_db(**kwargs):
    return get_jobs_from_db(**kwargs)


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