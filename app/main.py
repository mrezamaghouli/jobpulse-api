import os
import time
import logging
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from app.postgres_database import check_postgres_connection
from app.config import get_cors_allowed_origins
from app.models import Job, JobSearchResponse
from app.repositories.jobs_postgres_repository import (
    get_all_jobs_from_db,
    search_jobs_from_db,
    get_jobs_stats_from_db,
    get_job_by_id_from_db
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("jobpulse-api")

app = FastAPI(title="JobPulse API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()

    response = await call_next(request)

    duration_ms = round((time.time() - start_time) * 1000, 2)

    logger.info(
        "%s %s %s %sms",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms
    )

    return response

@app.get("/")
def home():
    return {
        "message": "JobPulse API is running",
        "docs": "/docs",
        "database": "PostgreSQL"
    }


@app.get("/health")
def health_check():
    database_status = check_postgres_connection()

    if database_status["connected"]:
        return {
            "status": "ok",
            "api": "running",
            "database": "connected",
            "database_type": "PostgreSQL"
        }

    return {
        "status": "error",
        "api": "running",
        "database": "disconnected",
        "database_type": "PostgreSQL",
        "details": database_status.get("error")
    }

@app.get("/jobs", response_model=list[Job])
def get_all_jobs():
    return get_all_jobs_from_db()


@app.get("/jobs/stats")
def get_jobs_stats():
    return get_jobs_stats_from_db()


@app.get("/jobs/search", response_model=JobSearchResponse)
def search_jobs(
    title: str | None = Query(default=None),
    location: str | None = Query(default=None),
    remote: bool | None = Query(default=None),

    job_type: str | None = Query(default=None),
    seniority: str | None = Query(default=None),
    min_salary: int | None = Query(default=None, ge=0),
    max_salary: int | None = Query(default=None, ge=0),
    source: str | None = Query(default=None),

    sort_by: str = Query(default="date_posted"),
    sort_order: str = Query(default="desc"),

    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=100)
):
    return search_jobs_from_db(
        title=title,
        location=location,
        remote=remote,
        job_type=job_type,
        seniority=seniority,
        min_salary=min_salary,
        max_salary=max_salary,
        source=source,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        limit=limit
    )


@app.get("/jobs/{job_id}", response_model=Job)
def get_job_by_id(job_id: int):
    job = get_job_by_id_from_db(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return job