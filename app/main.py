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


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "*",
    ],
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
    company: Optional[str] = None
    company_linkedin_url: Optional[str] = None
    location: Optional[str] = None

    remote: Optional[bool] = None
    job_type: Optional[str] = None
    seniority: Optional[str] = None

    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None

    source: Optional[str] = None
    job_url: Optional[str] = None

    apply_type: Optional[str] = None
    apply_url: Optional[str] = None
    apply_label: Optional[str] = None

    poster_name: Optional[str] = None
    poster_title: Optional[str] = None
    poster_profile_url: Optional[str] = None

    date_posted: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    is_active: Optional[bool] = None


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
    title: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    remote: Optional[bool] = None,
    seniority: Optional[str] = None,
    job_type: Optional[str] = None,
    min_salary: Optional[float] = None,
    max_salary: Optional[float] = None,
    source: Optional[str] = None,
    apply_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    active_only: Optional[bool] = None,
    page: int = 1,
    limit: int = 10,
    sort_by: str = "last_seen_at",
    sort_order: str = "desc",
):
    data = get_all_jobs_from_db(
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
        page=page,
        limit=limit,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    return serialize_data(data.get("results", []))


@app.get("/jobs/search", response_model=JobSearchResponse)
def search_jobs(
    title: Optional[str] = None,
    company: Optional[str] = None,
    location: Optional[str] = None,
    remote: Optional[bool] = None,
    seniority: Optional[str] = None,
    job_type: Optional[str] = None,
    min_salary: Optional[float] = None,
    max_salary: Optional[float] = None,
    source: Optional[str] = None,
    apply_type: Optional[str] = None,
    is_active: Optional[bool] = None,
    active_only: Optional[bool] = None,
    page: int = 1,
    limit: int = 10,
    sort_by: str = "last_seen_at",
    sort_order: str = "desc",
):
    data = search_jobs_from_db(
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