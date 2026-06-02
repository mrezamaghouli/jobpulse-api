# GCP Deployment Plan for JobPulse

This document describes the planned Google Cloud Platform deployment architecture for JobPulse.

JobPulse currently runs locally with Docker Compose:

```text
frontend
api
postgres
adminer
collector
```

For production on GCP, the architecture should be split into managed cloud services.

---

## Target GCP Architecture

```text
Frontend
   ↓
Firebase Hosting or Cloud Run
   ↓
FastAPI API on Cloud Run
   ↓
Cloud SQL PostgreSQL

Collector
   ↓
Cloud Run Job or Cloud Run Service
   ↓
Cloud SQL PostgreSQL

Secrets
   ↓
Secret Manager

Container Images
   ↓
Artifact Registry
```

---

## GCP Services

### 1. Cloud Run

Cloud Run will host the FastAPI API container.

The API container is already prepared for Cloud Run because it listens on:

```text
0.0.0.0
```

and uses the `PORT` environment variable:

```dockerfile
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

Cloud Run injects the `PORT` environment variable into the container at runtime.

---

### 2. Cloud SQL for PostgreSQL

The local PostgreSQL service in Docker Compose should be replaced by Cloud SQL PostgreSQL.

Local development:

```text
FastAPI → Docker PostgreSQL
```

Production:

```text
FastAPI on Cloud Run → Cloud SQL PostgreSQL
```

Required environment variables:

```env
POSTGRES_DB=jobpulse
POSTGRES_USER=jobpulse_user
POSTGRES_PASSWORD=<from Secret Manager>
POSTGRES_HOST=<Cloud SQL connection host or socket config>
POSTGRES_PORT=5432
```

---

### 3. Artifact Registry

Docker images should be pushed to Artifact Registry before deployment to Cloud Run.

Images:

```text
jobpulse-api
jobpulse-frontend
```

The API image is the main deployment target.

The frontend can be deployed either as:

```text
Firebase Hosting
```

or as:

```text
Cloud Run static frontend container
```

---

### 4. Secret Manager

Sensitive configuration should not be stored in GitHub.

Secrets should be stored in Secret Manager:

```text
POSTGRES_PASSWORD
POSTGRES_USER
POSTGRES_DB
```

The local `.env` file is only for development.

---

### 5. Cloud Run Jobs or Cloud Scheduler

The collector should not run continuously.

Recommended production pattern:

```text
Cloud Scheduler
   ↓
Cloud Run Job
   ↓
Collector
   ↓
Cloud SQL PostgreSQL
```

This allows the collector to run on a schedule, for example:

```text
Every day
Every 6 hours
Every hour
```

The current collector command is:

```bash
python -m scripts.collector_postgres
```

---

## Deployment Steps

### Phase 1 — Prepare Google Cloud

1. Create or select a GCP project.
2. Enable billing.
3. Install and authenticate `gcloud`.
4. Enable required APIs:

   * Cloud Run
   * Artifact Registry
   * Cloud SQL Admin
   * Secret Manager
   * Cloud Build
   * Cloud Scheduler

---

### Phase 2 — Create Cloud SQL PostgreSQL

1. Create a PostgreSQL instance.
2. Create the `jobpulse` database.
3. Create a database user.
4. Store database credentials in Secret Manager.
5. Run `db/init.sql` against the Cloud SQL database to create tables and indexes.

---

### Phase 3 — Build and Push API Image

1. Create an Artifact Registry repository.
2. Build the API Docker image.
3. Tag the image for Artifact Registry.
4. Push the image to Artifact Registry.

Example image name:

```text
REGION-docker.pkg.dev/PROJECT_ID/jobpulse/jobpulse-api:latest
```

---

### Phase 4 — Deploy API to Cloud Run

Deploy the API container to Cloud Run.

Required environment variables:

```env
POSTGRES_DB=jobpulse
POSTGRES_USER=jobpulse_user
POSTGRES_PASSWORD=<secret>
POSTGRES_HOST=<cloud-sql-host-or-socket>
POSTGRES_PORT=5432
JOB_PROVIDER=json
```

The service should expose:

```text
/health
/jobs/search
/jobs/stats
/jobs/{job_id}
```

After deployment, test:

```text
https://API_URL/health
```

---

### Phase 5 — Deploy Frontend

Two possible options:

#### Option A — Firebase Hosting

Recommended for a static frontend.

Frontend should point to the deployed Cloud Run API URL.

#### Option B — Cloud Run Frontend Container

Deploy the Nginx frontend container to Cloud Run.

This keeps frontend and backend both containerized.

---

### Phase 6 — Deploy Collector

The collector should be deployed as a Cloud Run Job.

Command:

```bash
python -m scripts.collector_postgres
```

Later, connect it to Cloud Scheduler.

---

## Important Production Changes Needed

Before production deployment, the project should support:

### 1. API URL Configuration for Frontend

Currently the frontend dynamically builds the API URL:

```js
const API_BASE_URL = `${window.location.protocol}//${window.location.hostname}:8000`;
```

This works for local Docker Compose.

For production, the frontend should support a fixed deployed API URL or runtime config.

---

### 2. Cloud SQL Connection

The current local config uses:

```env
POSTGRES_HOST=db
```

For GCP, database connection logic may need to support Cloud SQL connection format.

---

### 3. Collector Data Source

The current provider is:

```text
JsonJobProvider
```

A production version should replace this with an authorized job data provider.

The project does not scrape LinkedIn directly.

---

### 4. Authentication and Rate Limiting

Before public deployment, the API should include:

```text
API key
rate limiting
CORS restrictions
request logging
```

---

## Current Local Commands

Start local environment:

```powershell
docker compose up -d --build
```

Run collector locally:

```powershell
docker compose run --rm collector
```

Run smoke test:

```powershell
python scripts/smoke_test.py
```

Stop local environment:

```powershell
docker compose down
```

---

## Future GCP Commands Placeholder

The following commands will be finalized during the actual deployment phase:

```bash
gcloud auth login
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com
gcloud services enable artifactregistry.googleapis.com
gcloud services enable sqladmin.googleapis.com
gcloud services enable secretmanager.googleapis.com
gcloud services enable cloudbuild.googleapis.com
```

---

## Summary

The local project is now close to cloud-ready.

Current status:

```text
Dockerized API
Dockerized frontend
PostgreSQL database
Collector service
Provider layer
Health checks
Smoke tests
GitHub Actions CI
```

Next production steps:

```text
Cloud SQL setup
Artifact Registry setup
Cloud Run API deployment
Frontend deployment
Cloud Scheduler collector automation
Secret Manager integration
```
