# JobPulse API

JobPulse is a LinkedIn-style job search dashboard built with **FastAPI**, **PostgreSQL**, **Docker Compose**, and vanilla **HTML/CSS/JavaScript**.

The project includes a REST API, PostgreSQL database, provider-based job collector, search filters, sorting, pagination, statistics endpoint, health check endpoint, smoke test script, optional API key authentication, optional rate limiting, and a frontend dashboard for browsing job listings.

> Note: This project currently uses LinkedIn-style sample data. It does **not** scrape LinkedIn directly. A production version should use an authorized data source, official integration, or a compliant third-party job data provider.

---

## Features

* FastAPI REST API
* PostgreSQL database
* Docker Compose setup
* Nginx-based frontend container
* Provider-based job collector architecture
* LinkedIn-style job data model
* PostgreSQL collector service
* Job search by title, location, remote status, seniority, job type, and salary
* Sorting by date, salary, title, and company
* Pagination
* Job statistics endpoint
* Job details endpoint
* Health check endpoint
* Smoke test script
* Optional API key authentication
* Optional in-memory rate limiting
* Frontend dashboard
* Adminer database UI
* Duplicate prevention using LinkedIn job ID and job URL
* Frontend runtime config through `frontend/config.js`
* Configurable CORS origins through environment variables
* GitHub Actions CI workflow

---

## Tech Stack

* Python
* FastAPI
* PostgreSQL
* psycopg2
* Docker
* Docker Compose
* Nginx
* HTML
* CSS
* JavaScript
* Adminer
* GitHub Actions

---

## Architecture

```mermaid
flowchart TD
    A[Frontend Dashboard<br/>HTML CSS JavaScript] --> B[FastAPI REST API]
    B --> C[PostgreSQL Database]

    D[LinkedIn-style new_jobs.json] --> E[JsonJobProvider]
    E --> F[Collector Service]
    F --> C

    G[Future Authorized LinkedIn Provider] -.-> F

    H[Adminer Database UI] --> C

    B --> I[/health endpoint]
    B --> J[/jobs/search endpoint]
    B --> K[/jobs/stats endpoint]
    B --> L[/jobs/{id} endpoint]
```

---

## Project Structure

```text
linkedin-api/
├── app/
│   ├── config.py
│   ├── main.py
│   ├── models.py
│   ├── postgres_database.py
│   └── repositories/
│       ├── __init__.py
│       └── jobs_postgres_repository.py
│
├── db/
│   └── init.sql
│
├── frontend/
│   ├── Dockerfile
│   ├── config.js
│   └── index.html
│
├── legacy/
│   ├── collector_sqlite.py
│   ├── create_indexes_sqlite.py
│   ├── database_sqlite.py
│   ├── import_jobs_to_db_sqlite.py
│   ├── import_sqlite_to_postgres.py
│   ├── init_db_sqlite.py
│   ├── jobs_repository_sqlite.py
│   ├── main_old.py
│   └── show_db_jobs_sqlite.py
│
├── sample_data/
│   └── new_jobs.json
│
├── scripts/
│   ├── __init__.py
│   ├── collector_postgres.py
│   ├── smoke_test.py
│   └── providers/
│       ├── __init__.py
│       ├── base_provider.py
│       ├── json_provider.py
│       ├── linkedin_provider_placeholder.py
│       └── provider_factory.py
│
├── .github/
│   └── workflows/
│       └── ci.yml
│
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
├── .gitignore
├── .env.example
├── GCP_DEPLOYMENT_PLAN.md
├── PROJECT_NOTES.md
└── README.md
```

---

## Environment Variables

Create a `.env` file in the project root.

You can copy `.env.example` to `.env`:

```powershell
copy .env.example .env
```

Example `.env` values:

```env
POSTGRES_DB=jobpulse
POSTGRES_USER=jobpulse_user
POSTGRES_PASSWORD=jobpulse_password
POSTGRES_HOST=db
POSTGRES_PORT=5432
JOB_PROVIDER=json
PORT=8000
CORS_ALLOWED_ORIGINS=http://127.0.0.1:5500,http://localhost:5500
API_KEY=
RATE_LIMIT_ENABLED=false
RATE_LIMIT_MAX_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
```

The `.env` file is ignored by Git and should not be committed.

---

## Run the Project

Start all services:

```powershell
docker compose up -d --build
```

Check running containers:

```powershell
docker compose ps
```

Expected services:

```text
jobpulse-api
jobpulse-frontend
jobpulse-postgres
jobpulse-adminer
```

---

## Service URLs

Backend API:

```text
http://127.0.0.1:8000
```

API Docs:

```text
http://127.0.0.1:8000/docs
```

Health Check:

```text
http://127.0.0.1:8000/health
```

Frontend Dashboard:

```text
http://127.0.0.1:5500
```

Frontend Runtime Config:

```text
http://127.0.0.1:5500/config.js
```

Adminer:

```text
http://127.0.0.1:8081
```

---

## Adminer Login

Use the following credentials to log in to Adminer:

```text
System: PostgreSQL
Server: db
Username: jobpulse_user
Password: jobpulse_password
Database: jobpulse
```

---

## API Endpoints

```text
GET /
GET /health
GET /jobs
GET /jobs/search
GET /jobs/stats
GET /jobs/{job_id}
```

---

## Example API Requests

Get API status:

```text
http://127.0.0.1:8000/
```

Health check:

```text
http://127.0.0.1:8000/health
```

Get all jobs:

```text
http://127.0.0.1:8000/jobs
```

Search jobs:

```text
http://127.0.0.1:8000/jobs/search?source=linkedin&title=ux&page=1&limit=10
```

Search remote jobs:

```text
http://127.0.0.1:8000/jobs/search?source=linkedin&remote=true
```

Search by salary:

```text
http://127.0.0.1:8000/jobs/search?source=linkedin&min_salary=60000&sort_by=salary_max&sort_order=desc
```

Get job statistics:

```text
http://127.0.0.1:8000/jobs/stats
```

Get job details:

```text
http://127.0.0.1:8000/jobs/1
```

---

## Provider Layer

The collector uses a provider-based architecture.

Current provider:

```text
JsonJobProvider
```

Current data source:

```text
sample_data/new_jobs.json
```

Provider files:

```text
scripts/providers/base_provider.py
scripts/providers/json_provider.py
scripts/providers/linkedin_provider_placeholder.py
scripts/providers/provider_factory.py
```

The provider is selected through the environment variable:

```env
JOB_PROVIDER=json
```

The `LinkedInProviderPlaceholder` exists only as a placeholder for a future authorized LinkedIn data source.

The project does not scrape LinkedIn directly.

---

## Run the Collector

Add new LinkedIn-style jobs to:

```text
sample_data/new_jobs.json
```

Example:

```json
[
  {
    "linkedin_job_id": "li-3001",
    "title": "Data Product Analyst",
    "company": "InsightFlow GmbH",
    "company_linkedin_url": "https://www.linkedin.com/company/insightflow-example",
    "location": "Hamburg, Germany",
    "remote": true,
    "job_type": "Full-time",
    "seniority": "Mid",
    "salary_min": 58000,
    "salary_max": 82000,
    "currency": "EUR",
    "source": "LinkedIn",
    "job_url": "https://www.linkedin.com/jobs/view/li-3001",
    "poster_name": "Mia Schneider",
    "poster_title": "Talent Acquisition Manager",
    "poster_profile_url": "https://www.linkedin.com/in/mia-example",
    "date_posted": "2026-06-02"
  }
]
```

Then run the collector:

```powershell
docker compose run --rm collector
```

The collector:

* uses the provider layer to fetch job records
* reads `sample_data/new_jobs.json` through `JsonJobProvider`
* validates LinkedIn-style job records
* skips non-LinkedIn jobs
* prevents duplicates using `job_url`
* inserts new records into PostgreSQL

---

## Smoke Test

After starting the services, run:

```powershell
python scripts/smoke_test.py
```

The smoke test checks:

* `/health`
* `/jobs/stats`
* `/jobs/search`
* `/jobs/{job_id}`

Expected output:

```text
Running JobPulse smoke tests...
API base URL: http://127.0.0.1:8000
API key: disabled
--------------------------------------------------
✅ Health check passed
✅ Stats endpoint passed
✅ Search endpoint passed
✅ Job details endpoint passed
--------------------------------------------------
Passed: 4/4
🎉 All smoke tests passed.
```

If API key authentication is enabled, pass the API key as an environment variable before running the smoke test.

PowerShell example:

```powershell
$env:API_KEY="your_secret_key"
python scripts/smoke_test.py
```

To remove it from the current PowerShell session:

```powershell
Remove-Item Env:\API_KEY
```

If the job details test fails, make sure the database contains job records.

---

## Database Initialization

The PostgreSQL database is initialized using:

```text
db/init.sql
```

This file creates the `jobs` table and indexes automatically when PostgreSQL starts with a fresh Docker volume.

Main table:

```text
jobs
```

Important fields:

```text
id
linkedin_job_id
title
company
company_linkedin_url
location
remote
job_type
seniority
salary_min
salary_max
currency
source
job_url
poster_name
poster_title
poster_profile_url
date_posted
```

---

## Health Check

The project includes a health check endpoint:

```text
GET /health
```

Example response:

```json
{
  "status": "ok",
  "api": "running",
  "database": "connected",
  "database_type": "PostgreSQL"
}
```

Docker Compose also uses health checks for:

* PostgreSQL
* FastAPI API

This helps ensure services are actually ready, not just running.

---

## Frontend Dashboard

The frontend dashboard supports:

* job title search
* location search
* remote-only filter
* seniority filter
* job type filter
* minimum salary filter
* sorting
* pagination
* job cards
* job detail view
* system health status
* links to job URL
* links to poster profile
* links to company LinkedIn page

Frontend URL:

```text
http://127.0.0.1:5500
```

---

## Frontend API Configuration

The frontend reads the API URL from:

```text
frontend/config.js
```

Example local config:

```js
window.JOBPULSE_CONFIG = {
  API_BASE_URL: "http://127.0.0.1:8000"
};
```

Then `index.html` uses:

```js
const API_BASE_URL = window.JOBPULSE_CONFIG.API_BASE_URL;
```

For local Docker Compose usage:

* Frontend runs on port `5500`
* API runs on port `8000`

Example:

```text
http://127.0.0.1:5500 → http://127.0.0.1:8000
```

For production deployment, update `frontend/config.js` to point to the deployed API URL.

---

## CORS Configuration

The API reads allowed frontend origins from the environment variable:

```env
CORS_ALLOWED_ORIGINS=http://127.0.0.1:5500,http://localhost:5500
```

Multiple origins can be separated by commas.

For local Docker Compose usage, the default allowed origins are:

```text
http://127.0.0.1:5500
http://localhost:5500
```

For production deployment, this value should be updated to the deployed frontend domain.

Example:

```env
CORS_ALLOWED_ORIGINS=https://your-frontend-domain.com
```

---

## Optional API Key Authentication

The API supports optional API key authentication.

By default, local development runs without an API key:

```env
API_KEY=
```

When `API_KEY` is empty, all API endpoints are accessible normally.

To enable API key protection, set:

```env
API_KEY=your_secret_key
```

When enabled, protected endpoints require the following request header:

```text
X-API-Key: your_secret_key
```

Public endpoints remain accessible without an API key:

```text
GET /
GET /health
GET /docs
GET /openapi.json
GET /redoc
```

Protected endpoints include:

```text
GET /jobs
GET /jobs/search
GET /jobs/stats
GET /jobs/{job_id}
```

Example request with API key:

```powershell
curl.exe -H "X-API-Key: your_secret_key" "http://127.0.0.1:8000/jobs/search?source=linkedin"
```

---

## Optional Rate Limiting

The API supports optional in-memory rate limiting.

By default, rate limiting is disabled:

```env
RATE_LIMIT_ENABLED=false
```

To enable it:

```env
RATE_LIMIT_ENABLED=true
RATE_LIMIT_MAX_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
```

This means each client can make up to 60 requests per 60 seconds.

If the limit is exceeded, the API returns:

```json
{
  "detail": "Rate limit exceeded"
}
```

with HTTP status:

```text
429 Too Many Requests
```

Public endpoints are excluded from rate limiting:

```text
GET /
GET /health
GET /docs
GET /openapi.json
GET /redoc
```

Protected/search endpoints can be rate-limited:

```text
GET /jobs
GET /jobs/search
GET /jobs/stats
GET /jobs/{job_id}
```

This rate limiter is currently in-memory and suitable for local development or simple single-instance deployments. For production with multiple instances, Redis or another shared store should be used.

---

## Docker Commands

Start all services:

```powershell
docker compose up -d --build
```

Stop all services:

```powershell
docker compose down
```

View running services:

```powershell
docker compose ps
```

View logs:

```powershell
docker compose logs -f
```

View API logs:

```powershell
docker compose logs -f api
```

View PostgreSQL logs:

```powershell
docker compose logs -f db
```

Run collector:

```powershell
docker compose run --rm collector
```

Run smoke test:

```powershell
python scripts/smoke_test.py
```

Rebuild everything:

```powershell
docker compose down
docker compose up -d --build
```

---

## GitHub Actions CI

The project includes a GitHub Actions workflow:

```text
.github/workflows/ci.yml
```

The CI pipeline:

1. checks out the repository
2. creates `.env` from `.env.example`
3. builds Docker services
4. starts the system
5. waits for the API health check
6. runs the collector
7. runs the smoke test
8. shuts down the services

This helps verify that the project can build and run successfully after each push to `main`.

---

## Git Workflow

Check project status:

```powershell
git status
```

Stage changes:

```powershell
git add .
```

Commit changes:

```powershell
git commit -m "Update project"
```

Push to GitHub:

```powershell
git push
```

---

## Current Limitations

This project currently uses LinkedIn-style sample data.

It does not scrape LinkedIn directly.

A production version should use one of the following:

* authorized LinkedIn integration
* official/partner API access
* compliant third-party job data provider
* legally permitted job data source

---

## Future Improvements

* Deploy API to Google Cloud Run
* Use Cloud SQL PostgreSQL on GCP
* Deploy frontend to Firebase Hosting or Cloud Run
* Add Cloud Scheduler for automated collector runs
* Add authentication or API keys
* Add Redis caching for faster repeated searches
* Replace in-memory rate limiting with Redis-backed rate limiting
* Add full-text search
* Add company filtering
* Add country/city normalization
* Add job expiration and `is_active` status
* Add automated tests with pytest
* Add production logging and monitoring
* Replace JSON provider with an authorized job data provider

---

## Related Documentation

Additional project documentation:

```text
PROJECT_NOTES.md
GCP_DEPLOYMENT_PLAN.md
```

`PROJECT_NOTES.md` explains architecture decisions.

`GCP_DEPLOYMENT_PLAN.md` describes a possible future deployment plan for Google Cloud Platform.

---

## Resume Description

```text
Built a Dockerized LinkedIn-style job search platform using FastAPI, PostgreSQL, Docker Compose, and vanilla JavaScript. The system includes a REST API, PostgreSQL-backed job database, provider-based job collector pipeline, search filters, sorting, pagination, job statistics, health checks, smoke tests, optional API key authentication, optional rate limiting, GitHub Actions CI, and a frontend dashboard for browsing job listings and recruiter profile links.
```

---

## License

This project is for educational and portfolio purposes.
