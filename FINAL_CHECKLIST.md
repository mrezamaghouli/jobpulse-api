# JobPulse Final Checklist

Use this checklist before submitting or demoing the JobPulse project.

---

## 1. Git Status

Check that there are no unexpected uncommitted files:

```powershell
git status
```

Expected:

```text
working tree clean
```

Allowed untracked/ignored local-only folders:

```text
.auth/
logs/
sample_output/
```

These must not be committed.

---

## 2. Required Local Files

Make sure the LinkedIn session exists:

```powershell
dir .auth
```

Expected file:

```text
linkedin_storage_state.json
```

Make sure frontend config exists:

```powershell
dir frontend
```

Expected file:

```text
config.js
```

`frontend/config.js` should contain:

```js
window.JOBPULSE_CONFIG = {
  API_BASE_URL: "http://127.0.0.1:8000"
};
```

---

## 3. Docker Services

Restart everything from zero:

```powershell
docker compose down
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

All should be running.

---

## 4. API Health

Open:

```text
http://127.0.0.1:8000/health
```

Expected:

```json
{
  "status": "ok",
  "database": "connected"
}
```

Open:

```text
http://127.0.0.1:8000/meta
```

Expected:

```text
JobPulse API metadata
```

---

## 5. LinkedIn Collector

Run the collector once:

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

Expected:

```text
Multi-query LinkedIn collection finished.
Successful queries: ...
Failed queries: ...
Deactivated stale jobs: ...
```

At least one query should succeed.

---

## 6. Scheduler

Run scheduler once:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -RunOnce
```

Expected:

```text
Scheduled LinkedIn collection finished successfully
```

Check log:

```powershell
notepad logs\linkedin_scheduler.log
```

---

## 7. Smoke Tests

Run:

```powershell
python scripts\smoke_test.py
```

Expected:

```text
Passed: 7/7
All smoke tests passed.
```

If collector endpoints are empty, run:

```powershell
python -m scripts.linkedin_multi_collect
python scripts\smoke_test.py
```

---

## 8. Frontend Dashboard

Open:

```text
http://127.0.0.1:5500
```

Check that the dashboard shows:

* Total Jobs
* LinkedIn Jobs
* Active Jobs
* Remote Jobs
* Companies
* Locations
* Last Job Update
* System Status
* System Health
* Last LinkedIn Collector Run

Search for:

```text
UX
Designer
Product
Frontend
Germany
Berlin
```

Expected:

* Job cards are displayed.
* Pagination works.
* Search filters work.
* Reset button works.
* Open Job opens LinkedIn job page.
* View Details opens internal detail panel.
* Company LinkedIn opens when available.
* Open Poster Profile is hidden when unavailable.

---

## 9. Adminer Checks

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

Run:

```sql
SELECT COUNT(*)
FROM jobs
WHERE source = 'LinkedIn';
```

Run:

```sql
SELECT
  COUNT(*) FILTER (WHERE is_active = TRUE) AS active_jobs,
  COUNT(*) FILTER (WHERE is_active = FALSE) AS inactive_jobs,
  MAX(last_seen_at) AS latest_seen
FROM jobs
WHERE source = 'LinkedIn';
```

Run:

```sql
SELECT *
FROM collector_runs
ORDER BY started_at DESC
LIMIT 10;
```

Expected:

* LinkedIn jobs exist.
* Active jobs count is greater than zero.
* `last_seen_at` has recent timestamps.
* `collector_runs` has recent records.

---

## 10. API Endpoint Checks

Open these URLs:

```text
http://127.0.0.1:8000/jobs/stats
http://127.0.0.1:8000/jobs/search?source=linkedin&page=1&limit=10
http://127.0.0.1:8000/collector-runs/latest
http://127.0.0.1:8000/collector-runs/recent?limit=10
```

Expected:

* `/jobs/stats` returns job counters.
* `/jobs/search` returns results.
* `/collector-runs/latest` returns latest collector run.
* `/collector-runs/recent` returns recent collector runs.

---

## 11. Security Check

Make sure these are ignored:

```text
.env
.auth/
logs/
sample_output/
__pycache__/
*.pyc
```

Check:

```powershell
git status
```

Do not commit:

```text
.auth/linkedin_storage_state.json
logs/linkedin_scheduler.log
sample_output/linkedin_browser_provider_last_run.json
```

---

## 12. Demo Order

Recommended live demo order:

1. Show repository structure.
2. Show `README.md`.
3. Show `DEMO_SCRIPT.md`.
4. Show `config/job_queries.json`.
5. Start Docker services.
6. Open `/health`.
7. Run LinkedIn collector once.
8. Open Adminer and show jobs.
9. Open `/jobs/stats`.
10. Open `/collector-runs/latest`.
11. Open frontend dashboard.
12. Search for `UX` or `Designer`.
13. Open a LinkedIn job.
14. Run smoke test.
15. Show scheduler command.

---

## 13. Final Delivery Notes

The project is ready for delivery when:

* Docker services start successfully.
* LinkedIn collector can collect at least one query.
* Jobs are inserted or updated in PostgreSQL.
* Collector runs are logged.
* Frontend dashboard displays jobs and stats.
* Smoke tests pass.
* README and DEMO_SCRIPT are up to date.
* Sensitive files are not committed.
