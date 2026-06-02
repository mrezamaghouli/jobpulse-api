# JobPulse API

JobPulse is a LinkedIn-style job search dashboard built with **FastAPI**, **PostgreSQL**, **Docker Compose**, and vanilla **HTML/CSS/JavaScript**.

The project includes a REST API, PostgreSQL database, LinkedIn-style job collector, search filters, sorting, pagination, statistics endpoint, health check endpoint, and a frontend dashboard for browsing job listings.

> Note: This project currently uses LinkedIn-style sample data. It does **not** scrape LinkedIn directly. A production version should use an authorized data source, official integration, or a compliant third-party job data provider.

---

## Features

* FastAPI REST API
* PostgreSQL database
* Docker Compose setup
* Nginx-based frontend container
* LinkedIn-style job data model
* PostgreSQL collector service
* Job search by title, location, remote status, seniority, job type, and salary
* Sorting by date, salary, title, and company
* Pagination
* Job statistics endpoint
* Job details endpoint
* Health check endpoint
* Frontend dashboard
* Adminer database UI
* Duplicate prevention using LinkedIn job ID and job URL

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

---

## Architecture

```mermaid
flowchart TD
    A[Frontend Dashboard<br/>HTML CSS JavaScript] --> B[FastAPI REST API]
    B --> C[PostgreSQL Database]

    D[LinkedIn-style new_jobs.json] --> E[Collector Service]
    E --> C

    F[Adminer Database UI] --> C

    B --> G[/health endpoint]
    B --> H[/jobs/search endpoint]
    B --> I[/jobs/stats endpoint]
    B --> J[/jobs/{id} endpoint]
```

---

## Project Structure

```text
linkedin-api/
├── app/
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
│   └── show_db_jobs_sqlite.py
│
├── sample_data/
│   └── new_jobs.json
│
├── scripts/
│   └── collector_postgres.py
│
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .dockerignore
├── .gitignore
├── .env
└── README.md
```

---

## Environment Variables

Create a `.env` file in the project root:

```env
POSTGRES_DB=jobpulse
POSTGRES_USER=jobpulse_user
POSTGRES_PASSWORD=jobpulse_password
POSTGRES_HOST=db
POSTGRES_PORT=5432
```

The `.env` file is ignored by Git and should not be committed.

You can copy `.env.example` to `.env` and update the values:

```powershell
copy .env.example .env
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

* reads `sample_data/new_jobs.json`
* validates LinkedIn-style job records
* skips non-LinkedIn jobs
* prevents duplicates using `job_url`
* inserts new records into PostgreSQL

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
* links to job URL
* links to poster profile
* links to company LinkedIn page

Frontend URL:

```text
http://127.0.0.1:5500
```

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

Run collector:

```powershell
docker compose run --rm collector
```

Rebuild everything:

```powershell
docker compose down
docker compose up -d --build
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
* Add full-text search
* Add company filtering
* Add country/city normalization
* Add job expiration and `is_active` status
* Add automated tests
* Add CI/CD with GitHub Actions
* Add production logging and monitoring

---

## Resume Description

```text
Built a Dockerized LinkedIn-style job search platform using FastAPI, PostgreSQL, Docker Compose, and vanilla JavaScript. The system includes a REST API, PostgreSQL-backed job database, job collector pipeline, search filters, sorting, pagination, job statistics, health checks, and a frontend dashboard for browsing job listings and recruiter profile links.
```

---

## License

This project is for educational and portfolio purposes.
