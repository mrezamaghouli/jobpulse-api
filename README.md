# JobPulse API

JobPulse is a job search and monitoring dashboard built with FastAPI, PostgreSQL, Docker, and a simple frontend.

The project collects authorized LinkedIn job listings, stores them in PostgreSQL, updates existing jobs, tracks collector runs, and displays searchable job results in a local dashboard.

---

## Features

* FastAPI backend
* PostgreSQL database
* Docker Compose setup
* Adminer database UI
* Frontend dashboard
* Authorized LinkedIn browser-based job collector
* Multi-query LinkedIn collection
* Scheduled LinkedIn updater
* Job deduplication by URL
* First seen / last seen tracking
* Active / inactive job tracking
* Collector run logging
* Smoke tests
* Local demo script

---

## Project Structure

```text
jobpulse-api/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── postgres_database.py
│   └── repositories/
│       ├── jobs_postgres_repository.py
│       └── collector_runs_repository.py
│
├── config/
│   └── job_queries.json
│
├── db/
│   └── init.sql
│
├── frontend/
│   ├── Dockerfile
│   ├── index.html
│   └── config.js
│
├── scripts/
│   ├── collector_postgres.py
│   ├── linkedin_multi_collect.py
│   ├── linkedin_scheduler.py
│   ├── run_linkedin_scheduler.ps1
│   ├── smoke_test.py
│   ├── ensure_collector_runs_table.py
│   ├── ensure_jobs_tracking_columns.py
│   ├── linkedin_login.py
│   ├── linkedin_save_session_from_browser.py
│   ├── linkedin_preview_jobs.py
│   └── providers/
│       ├── provider_factory.py
│       ├── json_provider.py
│       ├── linkedin_provider_placeholder.py
│       └── linkedin_browser_provider.py
│
├── DEMO_SCRIPT.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
└── .env.example
```

---

## Requirements

Install these before running the project:

* Python 3.12+
* Docker Desktop
* Google Chrome
* Git
* PowerShell
* A valid authorized LinkedIn account/session for research collection

---

## Environment Variables

Copy the example env file:

```powershell
copy .env.example .env
```

Important variables:

```env
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=jobpulse
POSTGRES_USER=jobpulse_user
POSTGRES_PASSWORD=jobpulse_password

JOB_PROVIDER=json

API_KEY=
RATE_LIMIT_ENABLED=false

APP_NAME=JobPulse API
APP_VERSION=1.0.0
APP_ENV=development

LINKEDIN_BROWSER=chrome
LINKEDIN_KEYWORDS=UX Designer
LINKEDIN_LOCATION=Germany
LINKEDIN_LIMIT=10
LINKEDIN_STALE_DAYS=7
LINKEDIN_SCHEDULE_INTERVAL_MINUTES=180
```

For local collector execution with PostgreSQL running in Docker, use:

```powershell
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
$env:POSTGRES_DB="jobpulse"
$env:POSTGRES_USER="jobpulse_user"
$env:POSTGRES_PASSWORD="jobpulse_password"
```

Inside Docker, `POSTGRES_HOST` should be:

```env
POSTGRES_HOST=db
```

---

## Start the Project with Docker

Run:

```powershell
docker compose up -d --build --force-recreate
```

Check services:

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

## API URLs

API base:

```text
http://127.0.0.1:8000
```

Frontend:

```text
http://127.0.0.1:5500
```

Adminer:

```text
http://127.0.0.1:8081
```

---

## Main API Endpoints

```text
GET /
GET /health
GET /meta
GET /jobs/search
GET /jobs/stats
GET /jobs/{job_id}
GET /collector-runs/latest
GET /collector-runs/recent
```

---

## Example API Requests

Health check:

```text
http://127.0.0.1:8000/health
```

Metadata:

```text
http://127.0.0.1:8000/meta
```

Search LinkedIn jobs:

```text
http://127.0.0.1:8000/jobs/search?source=linkedin&page=1&limit=10
```

Search by title:

```text
http://127.0.0.1:8000/jobs/search?source=linkedin&title=UX&page=1&limit=10
```

Stats:

```text
http://127.0.0.1:8000/jobs/stats
```

Latest collector run:

```text
http://127.0.0.1:8000/collector-runs/latest
```

Recent collector runs:

```text
http://127.0.0.1:8000/collector-runs/recent?limit=10
```

---

## Adminer Login

Open:

```text
http://127.0.0.1:8081
```

Login:

```text
System: PostgreSQL
Server: db
Username: jobpulse_user
Password: jobpulse_password
Database: jobpulse
```

Useful SQL:

```sql
SELECT COUNT(*)
FROM jobs
WHERE source = 'LinkedIn';
```

```sql
SELECT
  COUNT(*) FILTER (WHERE is_active = TRUE) AS active_jobs,
  COUNT(*) FILTER (WHERE is_active = FALSE) AS inactive_jobs,
  MAX(last_seen_at) AS latest_seen
FROM jobs
WHERE source = 'LinkedIn';
```

```sql
SELECT *
FROM collector_runs
ORDER BY started_at DESC
LIMIT 10;
```

---

## LinkedIn Authorized Collection

This project uses an authorized browser-based LinkedIn collection workflow for research use.

The crawler does not:

* bypass CAPTCHA
* rotate proxies
* fake hidden login behavior
* store LinkedIn password in code
* commit browser sessions to GitHub

The user manually logs into LinkedIn, and the local session is saved in:

```text
.auth/linkedin_storage_state.json
```

This folder must never be committed.

---

## Save LinkedIn Session

If the LinkedIn session has not been saved yet, run the session setup process.

First make sure Playwright is installed:

```powershell
pip install playwright
```

If normal PyPI is blocked, use the mirror:

```powershell
pip install -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --default-timeout=120 playwright
```

Then use the browser session helper:

```powershell
python scripts/linkedin_save_session_from_browser.py
```

The session file should be created here:

```text
.auth/linkedin_storage_state.json
```

---

## Configure LinkedIn Queries

Queries are stored in:

```text
config/job_queries.json
```

Example:

```json
[
  {
    "keywords": "UX Designer",
    "location": "Germany",
    "limit": 10
  },
  {
    "keywords": "UI Designer",
    "location": "Germany",
    "limit": 10
  },
  {
    "keywords": "Product Designer",
    "location": "Germany",
    "limit": 10
  },
  {
    "keywords": "Frontend Developer",
    "location": "Germany",
    "limit": 10
  }
]
```

---

## Run LinkedIn Collector Once

Start PostgreSQL:

```powershell
docker compose up -d db adminer
```

Set local env variables:

```powershell
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
$env:POSTGRES_DB="jobpulse"
$env:POSTGRES_USER="jobpulse_user"
$env:POSTGRES_PASSWORD="jobpulse_password"
$env:LINKEDIN_BROWSER="chrome"
$env:LINKEDIN_STALE_DAYS="7"
```

Run:

```powershell
python -m scripts.linkedin_multi_collect
```

The collector will:

* read queries from `config/job_queries.json`
* open LinkedIn with the saved authorized session
* collect job listings
* normalize job data
* insert or update jobs in PostgreSQL
* update `last_seen_at`
* keep active jobs active
* deactivate stale jobs
* log collector runs in `collector_runs`

---

## Scheduled LinkedIn Updates

Run once:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -RunOnce
```

Run every 3 hours:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -IntervalMinutes 180
```

Run every 5 minutes for testing:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -IntervalMinutes 5
```

Stop scheduler:

```text
Ctrl + C
```

Scheduler logs:

```text
logs/linkedin_scheduler.log
```

---

## Smoke Tests

Run:

```powershell
python scripts\smoke_test.py
```

Expected successful result:

```text
Passed: 7/7
All smoke tests passed.
```

Smoke test checks:

* `/health`
* `/meta`
* `/jobs/stats`
* `/collector-runs/latest`
* `/collector-runs/recent`
* `/jobs/search`
* `/jobs/{id}`

---

## Frontend Dashboard

Open:

```text
http://127.0.0.1:5500
```

The dashboard shows:

* Total jobs
* LinkedIn jobs
* Active jobs
* Remote jobs
* Companies
* Locations
* Last job update
* System health
* Last LinkedIn collector run
* Searchable job cards

Try searches like:

```text
UX
Designer
Product
Frontend
Germany
Berlin
```

---

## Security Notes

Do not commit these:

```text
.env
.auth/
logs/
sample_output/
```

Make sure `.gitignore` includes:

```gitignore
.env
.auth/
logs/
sample_output/
__pycache__/
*.pyc
```

---

## Local Development Mode

If Docker API/frontend build is not needed, you can run only PostgreSQL in Docker and API locally.

Start database:

```powershell
docker compose up -d db adminer
```

Set local env:

```powershell
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
$env:POSTGRES_DB="jobpulse"
$env:POSTGRES_USER="jobpulse_user"
$env:POSTGRES_PASSWORD="jobpulse_password"
```

Run API locally:

```powershell
python -m uvicorn app.main:app --reload
```

Then open:

```text
http://127.0.0.1:8000/health
```

---

## Docker Build Note

If Docker build cannot reach PyPI directly, the Dockerfile uses a PyPI mirror for installing Python dependencies.

The key line is:

```dockerfile
RUN pip install --no-cache-dir \
    -i https://mirrors.aliyun.com/pypi/simple/ \
    --trusted-host mirrors.aliyun.com \
    --default-timeout=120 \
    --retries 10 \
    -r requirements.txt
```

---

## Demo Flow

Recommended demo order:

1. Start Docker services.
2. Open `/health`.
3. Open `/meta`.
4. Run the LinkedIn collector once.
5. Open Adminer and show `jobs`.
6. Open `/jobs/stats`.
7. Open `/collector-runs/latest`.
8. Open frontend dashboard.
9. Search for `UX` or `Designer`.
10. Open a LinkedIn job link.
11. Run smoke test.
12. Show scheduler command.

---

## Final Validation Before Delivery

Run:

```powershell
docker compose down
docker compose up -d --build --force-recreate
python -m scripts.linkedin_multi_collect
python scripts\smoke_test.py
```

Then open:

```text
http://127.0.0.1:5500
```

If Docker services are running, LinkedIn jobs are shown in the frontend, and smoke tests pass, the project is ready for demo.
