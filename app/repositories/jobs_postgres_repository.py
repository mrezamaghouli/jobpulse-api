import math

from app.postgres_database import get_postgres_connection


def row_to_job(row):
    return {
        "id": row["id"],
        "linkedin_job_id": row["linkedin_job_id"],

        "title": row["title"],
        "company": row["company"],
        "company_linkedin_url": row["company_linkedin_url"],

        "location": row["location"],
        "remote": bool(row["remote"]),

        "job_type": row["job_type"],
        "seniority": row["seniority"],

        "salary_min": row["salary_min"],
        "salary_max": row["salary_max"],
        "currency": row["currency"],

        "source": row["source"],
        "job_url": row["job_url"],

        "poster_name": row["poster_name"],
        "poster_title": row["poster_title"],
        "poster_profile_url": row["poster_profile_url"],

        "date_posted": row["date_posted"]
    }


def get_all_jobs_from_db():
    connection = get_postgres_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM jobs
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    cursor.close()
    connection.close()

    return [row_to_job(row) for row in rows]


def search_jobs_from_db(
    title=None,
    location=None,
    remote=None,
    job_type=None,
    seniority=None,
    min_salary=None,
    max_salary=None,
    source=None,
    sort_by="date_posted",
    sort_order="desc",
    page=1,
    limit=10
):
    connection = get_postgres_connection()
    cursor = connection.cursor()

    where_clauses = []
    params = []

    if title:
        where_clauses.append("title ILIKE %s")
        params.append(f"%{title}%")

    if location:
        where_clauses.append("location ILIKE %s")
        params.append(f"%{location}%")

    if remote is not None:
        where_clauses.append("remote = %s")
        params.append(remote)

    if job_type:
        where_clauses.append("job_type ILIKE %s")
        params.append(f"%{job_type}%")

    if seniority:
        where_clauses.append("seniority ILIKE %s")
        params.append(f"%{seniority}%")

    if source:
        where_clauses.append("source ILIKE %s")
        params.append(f"%{source}%")

    if min_salary is not None:
        where_clauses.append("salary_max IS NOT NULL AND salary_max >= %s")
        params.append(min_salary)

    if max_salary is not None:
        where_clauses.append("salary_min IS NOT NULL AND salary_min <= %s")
        params.append(max_salary)

    where_sql = ""

    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    allowed_sort_fields = {
        "id": "id",
        "title": "title",
        "company": "company",
        "location": "location",
        "salary_min": "salary_min",
        "salary_max": "salary_max",
        "date_posted": "date_posted"
    }

    sort_column = allowed_sort_fields.get(sort_by, "date_posted")

    if sort_order.lower() not in ["asc", "desc"]:
        sort_order = "desc"

    sort_direction = sort_order.upper()

    count_query = f"""
        SELECT COUNT(*) AS total_count
        FROM jobs
        {where_sql}
    """

    cursor.execute(count_query, params)
    total_count = cursor.fetchone()["total_count"]

    total_pages = math.ceil(total_count / limit) if total_count > 0 else 0
    offset = (page - 1) * limit

    data_query = f"""
        SELECT *
        FROM jobs
        {where_sql}
        ORDER BY {sort_column} {sort_direction}
        LIMIT %s
        OFFSET %s
    """

    cursor.execute(data_query, params + [limit, offset])
    rows = cursor.fetchall()

    cursor.close()
    connection.close()

    return {
        "count": total_count,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "results": [row_to_job(row) for row in rows]
    }


def get_jobs_stats_from_db():
    connection = get_postgres_connection()
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) AS total_jobs FROM jobs")
    total_jobs = cursor.fetchone()["total_jobs"]

    cursor.execute("SELECT COUNT(*) AS remote_jobs FROM jobs WHERE remote = TRUE")
    remote_jobs = cursor.fetchone()["remote_jobs"]

    cursor.execute("SELECT COUNT(*) AS onsite_jobs FROM jobs WHERE remote = FALSE")
    onsite_jobs = cursor.fetchone()["onsite_jobs"]

    cursor.execute("""
        SELECT source, COUNT(*) AS count
        FROM jobs
        WHERE source IS NOT NULL
        GROUP BY source
        ORDER BY count DESC
    """)
    sources = {
        row["source"]: row["count"]
        for row in cursor.fetchall()
    }

    cursor.execute("""
        SELECT seniority, COUNT(*) AS count
        FROM jobs
        WHERE seniority IS NOT NULL
        GROUP BY seniority
        ORDER BY count DESC
    """)
    seniorities = {
        row["seniority"]: row["count"]
        for row in cursor.fetchall()
    }

    cursor.execute("""
        SELECT job_type, COUNT(*) AS count
        FROM jobs
        WHERE job_type IS NOT NULL
        GROUP BY job_type
        ORDER BY count DESC
    """)
    job_types = {
        row["job_type"]: row["count"]
        for row in cursor.fetchall()
    }

    cursor.close()
    connection.close()

    return {
        "total_jobs": total_jobs,
        "remote_jobs": remote_jobs,
        "onsite_jobs": onsite_jobs,
        "sources": sources,
        "seniorities": seniorities,
        "job_types": job_types
    }


def get_job_by_id_from_db(job_id: int):
    connection = get_postgres_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM jobs
        WHERE id = %s
    """, (job_id,))

    row = cursor.fetchone()

    cursor.close()
    connection.close()

    if row is None:
        return None

    return row_to_job(row)