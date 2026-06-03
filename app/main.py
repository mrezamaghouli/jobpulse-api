import time
import logging

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import (
    get_cors_allowed_origins,
    get_api_key,
    get_rate_limit_enabled,
    get_rate_limit_max_requests,
    get_rate_limit_window_seconds
)
from app.models import Job, JobSearchResponse
from app.postgres_database import check_postgres_connection
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
rate_limit_store = {}

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


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    configured_api_key = get_api_key()

    public_paths = [
        "/",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc"
    ]

    if not configured_api_key:
        return await call_next(request)

    if request.url.path in public_paths:
        return await call_next(request)

    request_api_key = request.headers.get("X-API-Key")

    if request_api_key != configured_api_key:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Invalid or missing API key"
            }
        )

    return await call_next(request)

@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if not get_rate_limit_enabled():
        return await call_next(request)

    public_paths = [
        "/",
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc"
    ]

    if request.url.path in public_paths:
        return await call_next(request)

    client_host = request.client.host if request.client else "unknown"
    current_time = time.time()

    window_seconds = get_rate_limit_window_seconds()
    max_requests = get_rate_limit_max_requests()

    client_record = rate_limit_store.get(client_host)

    if not client_record:
        rate_limit_store[client_host] = {
            "window_start": current_time,
            "request_count": 1
        }

        return await call_next(request)

    window_start = client_record["window_start"]
    request_count = client_record["request_count"]

    if current_time - window_start > window_seconds:
        rate_limit_store[client_host] = {
            "window_start": current_time,
            "request_count": 1
        }

        return await call_next(request)

    if request_count >= max_requests:
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Rate limit exceeded"
            }
        )

    client_record["request_count"] += 1

    return await call_next(request)


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