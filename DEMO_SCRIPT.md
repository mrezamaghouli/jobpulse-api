# JobPulse Demo Script

This document explains how to run and demonstrate the JobPulse project locally.

JobPulse is a FastAPI + PostgreSQL job search dashboard that collects authorized LinkedIn job listings, stores them in PostgreSQL, updates existing jobs, tracks collector runs, and displays the results in a frontend dashboard.

---

## 1. Requirements

Before running the demo, make sure the following are installed:

* Python 3.12+
* Docker Desktop
* Google Chrome
* Git
* PowerShell
* A valid LinkedIn login session saved locally

The LinkedIn session is stored locally in:

```text
.auth/linkedin_storage_state.json
```

This file must never be committed to GitHub.

---

## 2. Start Docker Services

Start the database, API, frontend, and Adminer:

```powershell
docker compose up -d --build --force-recreate
```

Check that services are running:

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

## 3. Check API Health

Open:

```text
http://127.0.0.1:8000/health
```

Expected result:

```json
{
  "status": "ok",
  "database": "connected"
}
```

---

## 4. Run LinkedIn Multi-Query Collector

The LinkedIn collector reads queries from:

```text
config/job_queries.json
```

Example queries:

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
  }
]
```

To run the collector once:

```powershell
$env:POSTGRES_HOST="localhost"
$env:POSTGRES_PORT="5432"
$env:POSTGRES_DB="jobpulse"
$env:POSTGRES_USER="jobpulse_user"
$env:POSTGRES_PASSWORD="jobpulse_password"
$env:LINKEDIN_BROWSER="chrome"
$env:LINKEDIN_STALE_DAYS="7"

python -m scripts.linkedin_multi_collect
```

The collector will:

* Open LinkedIn using the authorized saved browser session
* Search jobs based on configured keywords and locations
* Extract job title, company, location, and job URL
* Insert or update jobs in PostgreSQL
* Update `last_seen_at`
* Keep active jobs marked as `is_active = true`
* Deactivate stale jobs that have not been seen recently
* Save collector execution records in `collector_runs`

---

## 5. Run Scheduled LinkedIn Update

To run the scheduler once:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -RunOnce
```

To run it every 3 hours:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -IntervalMinutes 180
```

For quick testing every 5 minutes:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -IntervalMinutes 5
```

To stop the scheduler:

```text
Ctrl + C
```

Scheduler logs are stored in:

```text
logs/linkedin_scheduler.log
```

The `logs/` folder should not be committed.

---

## 6. Run Smoke Tests

Run:

```powershell
python scripts\smoke_test.py
```

Expected result:

```text
Passed: 7/7
All smoke tests passed.
```

The smoke test checks:

* `/health`
* `/meta`
* `/jobs/stats`
* `/collector-runs/latest`
* `/collector-runs/recent`
* `/jobs/search`
* `/jobs/{id}`

---

## 7. Open the Frontend Dashboard

Open:

```text
http://127.0.0.1:5500
```

The dashboard should show:

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

Try searching:

```text
UX
Designer
Product
Frontend
Germany
Berlin
```

Each job card should include:

* Job title
* Company
* Location
* Tags
* Open Job link
* View Details link
* Company LinkedIn link when available

---

## 8. Open Adminer

Open:

```text
http://127.0.0.1:8081
```

Login details:

```text
System: PostgreSQL
Server: db
Username: jobpulse_user
Password: jobpulse_password
Database: jobpulse
```

Useful SQL checks:

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

## 9. Important Security Notes

Do not commit:

```text
.auth/
logs/
sample_output/
.env
```

The LinkedIn session file is local-only:

```text
.auth/linkedin_storage_state.json
```

The crawler is designed for authorized research use only. It does not bypass CAPTCHA, does not rotate proxies, and does not use hidden login automation. Login and verification are completed manually by the authorized user.

---

## 10. Demo Flow

Recommended live demo order:

1. Show the GitHub repository structure.
2. Open `README.md`.
3. Open `config/job_queries.json`.
4. Run Docker services.
5. Open `/health`.
6. Run the LinkedIn collector once.
7. Open Adminer and show inserted jobs.
8. Open `/jobs/stats`.
9. Open `/collector-runs/latest`.
10. Open the frontend dashboard.
11. Search for `UX` or `Designer`.
12. Open a LinkedIn job link.
13. Run `python scripts\smoke_test.py`.
14. Show scheduler command.

---

## 11. Final Validation Commands

Run these before delivery:

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

If all services work and smoke tests pass, the project is ready for demo.
