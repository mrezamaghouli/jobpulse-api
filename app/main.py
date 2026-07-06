from app.admin_api import router as admin_router
import logging
import os
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.postgres_database import get_postgres_connection
from app.repositories.collector_runs_repository import (
    get_latest_collector_run_from_db,
    get_recent_collector_runs_from_db,
)
from app.repositories.jobs_postgres_repository import (
    get_all_jobs_from_db,
    get_jobs_from_db,
    get_job_by_id_from_db,
    get_jobs_stats_from_db,
    search_jobs_from_db,
)


APP_NAME = os.getenv("APP_NAME", "JobPulse API")
APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
APP_ENV = os.getenv("APP_ENV", "development")

API_KEY = os.getenv("API_KEY", "")
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "false").lower() == "true"
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("jobpulse")


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    description="JobPulse API for searching and monitoring LinkedIn job listings.",
)

def get_cors_origins() -> list[str]:
    raw_origins = os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://127.0.0.1,http://localhost,http://127.0.0.1:80,http://localhost:80,http://127.0.0.1:5500,http://localhost:5500",
    )

    origins = [
        origin.strip()
        for origin in raw_origins.split(",")
        if origin.strip()
    ]

    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


rate_limit_store = {}


PUBLIC_PATHS = {
    "/",
    "/health",
    "/meta",
    "/jobs",
    "/jobs/search",
    "/jobs/stats",
    "/collector-runs/latest",
    "/collector-runs/recent",
    "/docs",
    "/openapi.json",
    "/redoc",
}


def serialize_value(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return float(value)

    return value


def serialize_data(data: Any):
    if isinstance(data, list):
        return [serialize_data(item) for item in data]

    if isinstance(data, dict):
        return {
            key: serialize_value(value)
            for key, value in data.items()
        }

    return serialize_value(data)


class Job(BaseModel):
    id: int
    linkedin_job_id: Optional[str] = None

    title: str
    company: str
    company_id: Optional[int] = None
    company_linkedin_url: Optional[str] = None
    company_logo_url: Optional[str] = None

    location: str
    remote: bool
    work_mode: Optional[str] = None

    job_type: Optional[str] = None
    seniority: Optional[str] = None

    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None

    source: str
    job_url: str

    job_description: Optional[str] = None
    job_about: Optional[str] = None

    date_posted_text: Optional[str] = None
    date_posted_at: Optional[Any] = None

    apply_type: Optional[str] = None
    apply_url: Optional[str] = None
    apply_label: Optional[str] = None

    poster_name: Optional[str] = None
    poster_title: Optional[str] = None
    poster_profile_url: Optional[str] = None

    date_posted: Optional[Any] = None
    first_seen_at: Optional[Any] = None
    last_seen_at: Optional[Any] = None
    is_active: Optional[bool] = None

    inactive_at: Optional[datetime] = None
    inactive_reason: Optional[str] = None
    archived_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    search_score: Optional[float] = None
    quality_score: float | None = None
    quality_reasons: list[str] | None = None


class JobSearchResponse(BaseModel):
    results: list[Job]
    count: int
    page: int
    limit: int
    total_pages: int


class JobStats(BaseModel):
    total_jobs: int = 0
    linkedin_jobs: int = 0
    active_linkedin_jobs: int = 0
    inactive_linkedin_jobs: int = 0
    remote_jobs: int = 0
    onsite_jobs: Optional[int] = 0
    easy_apply_jobs: Optional[int] = 0
    external_apply_jobs: Optional[int] = 0
    jobs_with_apply_url: Optional[int] = 0
    total_companies: int = 0
    total_locations: int = 0
    last_linkedin_job_seen_at: Optional[str] = None
    newest_linkedin_job_first_seen_at: Optional[str] = None


class CollectorRun(BaseModel):
    id: Optional[int] = None
    provider: Optional[str] = None
    keywords: Optional[str] = None
    location: Optional[str] = None
    job_limit: Optional[int] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None


class CollectorRunsResponse(BaseModel):
    results: list[CollectorRun]


@app.middleware("http")
async def log_requests(request: Request, call_next):
    started_at = time.time()

    response = await call_next(request)

    duration_ms = round((time.time() - started_at) * 1000, 2)

    logger.info(
        "%s %s %s %sms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )

    return response


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if not API_KEY:
        return await call_next(request)

    path = request.url.path

    if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc"):
        return await call_next(request)

    request_api_key = request.headers.get("X-API-Key")

    if request_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

    return await call_next(request)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if not RATE_LIMIT_ENABLED:
        return await call_next(request)

    path = request.url.path

    if path in PUBLIC_PATHS:
        return await call_next(request)

    client_host = request.client.host if request.client else "unknown"
    now = time.time()

    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    request_times = rate_limit_store.get(client_host, [])
    request_times = [
        request_time
        for request_time in request_times
        if request_time >= window_start
    ]

    if len(request_times) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    request_times.append(now)
    rate_limit_store[client_host] = request_times

    return await call_next(request)


@app.get("/")
def root():
    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "message": "JobPulse API is running.",
    }


@app.get("/health")
def health_check():
    try:
        connection = get_postgres_connection()
        cursor = connection.cursor()

        cursor.execute("SELECT 1;")
        cursor.fetchone()

        cursor.close()
        connection.close()

        return {
            "status": "ok",
            "database": "connected",
        }

    except Exception as error:
        logger.exception("Health check failed")

        return {
            "status": "error",
            "database": "disconnected",
            "error": str(error),
        }


@app.get("/meta")
def get_meta():
    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "environment": APP_ENV,
        "features": [
            "FastAPI backend",
            "PostgreSQL database",
            "LinkedIn authorized browser collector",
            "Multi-query collection",
            "Scheduled updates",
            "Apply link extraction",
            "Collector run logging",
            "Frontend dashboard",
        ],
    }


@app.get("/jobs", response_model=list[Job])
def get_jobs(
    query: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    remote: Optional[bool] = None,
    work_mode: Optional[str] = None,
    seniority: Optional[str] = None,
    job_type: Optional[str] = None,
    min_salary: Optional[float] = None,
    max_salary: Optional[float] = None,
    source: Optional[str] = None,
    apply_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    active_only: Optional[bool] = True,
    page: int = 1,
    limit: int = 10,
    sort_by: str = "last_seen_at",
    sort_order: str = "desc",
):
    data = get_jobs_from_db(
        query=query,
        title=title,
        company=company,
        location=location,
        remote=remote,
        work_mode=work_mode,
        seniority=seniority,
        job_type=job_type,
        min_salary=min_salary,
        max_salary=max_salary,
        source=source,
        apply_type=apply_type,
        is_active=is_active,
        active_only=active_only,
        page=page,
        limit=limit,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    return serialize_data(data.get("results", []))


@app.get("/jobs/search", response_model=JobSearchResponse)
def search_jobs(
    query: Optional[str] = None,
    search: Optional[str] = None,
    search_query: Optional[str] = None,
    q: Optional[str] = None,
    keywords: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    remote: Optional[bool] = None,
    work_mode: Optional[str] = None,
    seniority: Optional[str] = None,
    job_type: Optional[str] = None,
    min_salary: Optional[float] = None,
    max_salary: Optional[float] = None,
    source: Optional[str] = None,
    apply_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    active_only: Optional[bool] = True,
    page: int = 1,
    limit: int = 10,
    sort_by: str = "last_seen_at",
    sort_order: str = "desc",
):
    resolved_query = query or search or search_query or q or keywords

    data = search_jobs_from_db(
        query=resolved_query,
        title=title,
        company=company,
        location=location,
        remote=remote,
        work_mode=work_mode,
        seniority=seniority,
        job_type=job_type,
        min_salary=min_salary,
        max_salary=max_salary,
        source=source,
        apply_type=apply_type,
        is_active=is_active,
        active_only=active_only,
        page=page,
        limit=limit,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    return serialize_data(data)


@app.get("/jobs/stats", response_model=JobStats)
def get_jobs_stats():
    stats = get_jobs_stats_from_db()

    return serialize_data(stats)

@app.get("/jobs/quality")
def get_jobs_quality():
    connection = None
    cursor = None

    try:
        connection = get_postgres_connection()
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_linkedin_jobs,
                COUNT(*) FILTER (WHERE is_active = TRUE) AS active_linkedin_jobs,
                COUNT(*) FILTER (WHERE apply_url IS NOT NULL AND apply_url != '') AS jobs_with_apply_url,
                COUNT(*) FILTER (WHERE apply_type = 'easy_apply') AS easy_apply_jobs,
                COUNT(*) FILTER (WHERE apply_type = 'external') AS external_apply_jobs,
                COUNT(*) FILTER (WHERE job_description IS NOT NULL AND job_description != '') AS jobs_with_description,
                COUNT(*) FILTER (WHERE company_logo_url IS NOT NULL AND company_logo_url != '') AS jobs_with_company_logo,
                COUNT(*) FILTER (WHERE work_mode IS NOT NULL AND work_mode != '') AS jobs_with_work_mode,
                COUNT(*) FILTER (WHERE date_posted_text IS NOT NULL AND date_posted_text != '') AS jobs_with_date_posted_text,
                COUNT(DISTINCT company) AS unique_companies,
                MAX(first_seen_at) AS newest_job_first_seen_at,
                MAX(last_seen_at) AS last_job_seen_at
            FROM jobs
            WHERE source = 'LinkedIn';
            """
        )

        job_row = cursor.fetchone()
        job_summary = dict(job_row) if job_row else {}

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_companies,
                COUNT(*) FILTER (WHERE logo_url IS NOT NULL AND logo_url != '') AS companies_with_logo,
                COUNT(*) FILTER (WHERE about IS NOT NULL AND about != '') AS companies_with_about,
                COUNT(*) FILTER (WHERE website_url IS NOT NULL AND website_url != '') AS companies_with_website,
                COUNT(*) FILTER (WHERE industry IS NOT NULL AND industry != '') AS companies_with_industry,
                COUNT(*) FILTER (WHERE company_size IS NOT NULL AND company_size != '') AS companies_with_size,
                COUNT(*) FILTER (WHERE headquarters IS NOT NULL AND headquarters != '') AS companies_with_headquarters,
                MAX(last_enriched_at) AS last_company_enriched_at
            FROM companies;
            """
        )

        company_row = cursor.fetchone()
        company_summary = dict(company_row) if company_row else {}  
        def to_int(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        def percent(part, total):
            part = to_int(part)
            total = to_int(total)

            if total <= 0:
                return 0

            return round((part / total) * 100, 2)

        total_jobs = to_int(job_summary.get("total_linkedin_jobs"))
        total_companies = to_int(company_summary.get("total_companies"))

        apply_coverage = percent(
            job_summary.get("jobs_with_apply_url"),
            total_jobs,
        )

        description_coverage = percent(
            job_summary.get("jobs_with_description"),
            total_jobs,
        )

        logo_coverage = percent(
            job_summary.get("jobs_with_company_logo"),
            total_jobs,
        )

        work_mode_coverage = percent(
            job_summary.get("jobs_with_work_mode"),
            total_jobs,
        )

        company_logo_coverage = percent(
            company_summary.get("companies_with_logo"),
            total_companies,
        )

        company_about_coverage = percent(
            company_summary.get("companies_with_about"),
            total_companies,
        )

        company_enrichment_coverage = round(
            (
                company_logo_coverage +
                company_about_coverage
            ) / 2,
            2,
        )

        overall_quality_score = round(
            (
                apply_coverage +
                description_coverage +
                logo_coverage +
                work_mode_coverage +
                company_enrichment_coverage
            ) / 5,
            2,
        )

        quality_scores = {
            "apply_coverage": apply_coverage,
            "description_coverage": description_coverage,
            "logo_coverage": logo_coverage,
            "work_mode_coverage": work_mode_coverage,
            "company_logo_coverage": company_logo_coverage,
            "company_about_coverage": company_about_coverage,
            "company_enrichment_coverage": company_enrichment_coverage,
            "overall_quality_score": overall_quality_score,
        }
        return {
            "jobs": job_summary,
            "companies": company_summary,
            "quality_scores": quality_scores,
        }

    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error))

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()


@app.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: int):
    job = get_job_by_id_from_db(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return serialize_data(job)


@app.get("/collector-runs/latest")
def get_latest_collector_run():
    collector_run = get_latest_collector_run_from_db()

    if collector_run is None:
        return {
            "status": "empty",
            "message": "No collector runs found.",
        }

    return serialize_data(collector_run)


@app.get("/collector-runs/recent", response_model=CollectorRunsResponse)
def get_recent_collector_runs(limit: int = 10):
    if limit < 1:
        limit = 1

    if limit > 50:
        limit = 50

    collector_runs = get_recent_collector_runs_from_db(limit=limit)

    return {
        "results": serialize_data(collector_runs),
    }


# Production API guard: request validation, rate limiting, and safe error responses.
from app.api_guard import ApiGuardMiddleware
app.add_middleware(ApiGuardMiddleware)


# Internal short-lived cache for repeated public job search queries.
from app.api_cache import SimpleApiCacheMiddleware
app.add_middleware(SimpleApiCacheMiddleware)

# Private admin endpoints protected by X-Admin-Key.
app.include_router(admin_router)


# Admin status routes
from app.admin_status import register_admin_status_routes
register_admin_status_routes(app)


# Admin logs routes
from app.admin_status import register_admin_logs_routes
register_admin_logs_routes(app)


# Admin action routes
from app.admin_status import register_admin_action_routes
register_admin_action_routes(app)

