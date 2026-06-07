# JobPulse Project Overview

## 1. Project Summary

JobPulse is a job search and monitoring platform built with FastAPI, PostgreSQL, Docker, and a frontend dashboard.

The system collects authorized LinkedIn job listings, stores them in PostgreSQL, keeps existing jobs updated, tracks collector executions, and displays searchable job data in a local dashboard.

The goal of the project is to create a reliable job data pipeline that can continuously collect and update job listings for research and analysis purposes.

---

## 2. Problem

Job listings change frequently. New jobs are posted, old jobs disappear, and manually checking LinkedIn search results is time-consuming.

For research or job market analysis, it is useful to have a system that can:

* collect job listings automatically
* store structured job data
* update existing records
* track when each job was last seen
* distinguish active and inactive jobs
* provide a searchable dashboard

JobPulse solves this by creating a local job collection and monitoring pipeline.

---

## 3. Main Features

JobPulse includes:

* FastAPI backend
* PostgreSQL database
* Docker Compose environment
* Adminer database UI
* Frontend dashboard
* Authorized LinkedIn browser-based collector
* Multi-query LinkedIn collection
* Scheduled updates
* Job deduplication
* First seen / last seen tracking
* Active / inactive job status
* Collector run logging
* Smoke tests
* Demo and final checklist documentation

---

## 4. LinkedIn Data Collection

The LinkedIn collector works through an authorized browser session.

The user manually logs into LinkedIn, and the local session is stored in:

```text
.auth/linkedin_storage_state.json
```

The system then uses this authorized session to open LinkedIn job search pages, collect job data, normalize the results, and store them in PostgreSQL.

The collector does not bypass CAPTCHA, does not rotate proxies, and does not store LinkedIn credentials in the code.

---

## 5. Data Pipeline

The data flow is:

```text
LinkedIn Search
    ↓
Authorized Browser Provider
    ↓
Job Normalizer
    ↓
PostgreSQL
    ↓
FastAPI
    ↓
Frontend Dashboard
```

The collector reads search queries from:

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
    "keywords": "Product Designer",
    "location": "Germany",
    "limit": 10
  }
]
```

---

## 6. Database Tracking

Each job is stored with structured fields such as:

* title
* company
* location
* source
* job URL
* LinkedIn job ID
* remote status
* first seen timestamp
* last seen timestamp
* active status

The system updates existing jobs using the job URL as a unique identifier.

If a job appears again in a later collection run, its `last_seen_at` field is updated.

If a job has not been seen for a configured number of days, it can be marked as inactive.

---

## 7. Collector Run Logging

Every collector execution is stored in the `collector_runs` table.

This allows the system to track:

* provider name
* keywords
* location
* job limit
* status
* start time
* finish time
* duration
* error message if the run failed

The frontend displays the latest collector run so the user can see when the job data was last updated.

---

## 8. Scheduled Updates

JobPulse includes a local scheduler that can run the LinkedIn collector automatically.

Examples:

Run once:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -RunOnce
```

Run every 3 hours:

```powershell
.\scripts\run_linkedin_scheduler.ps1 -IntervalMinutes 180
```

The scheduler logs output to:

```text
logs/linkedin_scheduler.log
```

---

## 9. Frontend Dashboard

The frontend dashboard displays:

* total jobs
* LinkedIn jobs
* active jobs
* remote jobs
* companies
* locations
* last job update
* system health
* latest collector run
* searchable job cards

Users can search jobs by title, location, seniority, job type, salary, remote status, and sorting options.

Each job card includes links to open the original LinkedIn job and view internal job details.

---

## 10. API Endpoints

Important API endpoints include:

```text
GET /health
GET /meta
GET /jobs/search
GET /jobs/stats
GET /jobs/{job_id}
GET /collector-runs/latest
GET /collector-runs/recent
```

The smoke test validates these endpoints.

---

## 11. Testing

The project includes smoke tests to verify the main API functionality.

Run:

```powershell
python scripts\smoke_test.py
```

Expected result:

```text
Passed: 7/7
All smoke tests passed.
```

---

## 12. Demo Flow

Recommended demo flow:

1. Start Docker services.
2. Open `/health`.
3. Run the LinkedIn collector.
4. Show inserted jobs in Adminer.
5. Open `/jobs/stats`.
6. Open `/collector-runs/latest`.
7. Open the frontend dashboard.
8. Search for jobs.
9. Open a LinkedIn job link.
10. Run smoke tests.
11. Show the scheduler command.

---

## 13. Project Value

JobPulse demonstrates:

* backend API development
* PostgreSQL data modeling
* Docker-based local infrastructure
* browser-based authorized data collection
* scheduled data updates
* frontend dashboard development
* automated validation with smoke tests
* practical data pipeline design

The project is suitable as a job data collection and monitoring prototype for research, analytics, and dashboard-based exploration.
