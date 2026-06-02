# JobPulse Project Notes

## Project Goal

The goal of JobPulse is to build a backend-focused job search platform that simulates a real-world job aggregation system.

The project is designed around this flow:

```text
Job Data Provider
   ↓
Collector
   ↓
PostgreSQL Database
   ↓
FastAPI REST API
   ↓
Frontend Dashboard
```

The main focus of the project is not only to display job listings, but to design a clean, scalable, and maintainable architecture.

---

## Why FastAPI?

FastAPI was chosen because it is lightweight, modern, and suitable for building REST APIs quickly.

It also provides automatic API documentation through Swagger UI, which makes testing endpoints easier during development.

Important benefits:

* clean API structure
* automatic documentation
* strong typing with Pydantic
* good performance
* easy integration with Docker

---

## Why PostgreSQL?

The first version of the project used SQLite for quick local development. Later, the database was migrated to PostgreSQL.

PostgreSQL was chosen because it is more suitable for production-like projects.

Compared to SQLite, PostgreSQL is better for:

* larger datasets
* concurrent access
* production deployment
* indexing and search performance
* cloud deployment with services like Cloud SQL

The current main database is PostgreSQL.

SQLite-related files were moved into the `legacy` folder.

---

## Why Docker Compose?

Docker Compose is used to run the whole system with one command.

The project includes multiple services:

```text
frontend
api
postgres
adminer
collector
```

Docker Compose makes it easier to run these services together without manually starting each one.

Main benefits:

* reproducible local environment
* easier onboarding
* production-like development setup
* simpler path toward cloud deployment

---

## Why a Provider Layer?

The collector does not directly depend on a single hardcoded data source.

Instead, the project uses a provider-based architecture.

Current provider:

```text
JsonJobProvider
```

This provider reads LinkedIn-style sample job data from:

```text
sample_data/new_jobs.json
```

The purpose of this design is to make the collector extensible.

In the future, a new provider can be added for an authorized job data source without rewriting the whole collector.

Example future provider:

```text
AuthorizedLinkedInProvider
```

This project does not scrape LinkedIn directly.

---

## Why Not Scrape LinkedIn Directly?

Direct scraping of LinkedIn using bots, cookies, browser automation, or bypass techniques is not suitable for this project.

A production version should use one of the following:

* official or partner API access
* authorized LinkedIn integration
* compliant third-party job data provider
* legally permitted job data source

This makes the project safer, more professional, and more suitable for a portfolio or resume.

---

## API Design

The API includes the following endpoints:

```text
GET /
GET /health
GET /jobs
GET /jobs/search
GET /jobs/stats
GET /jobs/{job_id}
```

The most important endpoint is:

```text
GET /jobs/search
```

It supports:

* title search
* location search
* remote filter
* seniority filter
* job type filter
* salary filter
* sorting
* pagination

This makes the API behave more like a real job search backend.

---

## Frontend Design

The frontend is intentionally built with vanilla HTML, CSS, and JavaScript.

The goal was not to focus on a complex frontend framework, but to create a simple dashboard that demonstrates the backend functionality.

The frontend supports:

* search
* filters
* sorting
* pagination
* job cards
* job details
* recruiter/profile links
* company links

---

## Health Checks

The project includes a `/health` endpoint.

This endpoint checks:

* whether the API is running
* whether PostgreSQL is connected

Docker Compose also uses health checks to make sure services are actually ready, not just started.

This is useful for local development, CI, and future deployment.

---

## Smoke Tests

A smoke test script is included:

```text
scripts/smoke_test.py
```

It checks the most important API endpoints:

* `/health`
* `/jobs/stats`
* `/jobs/search`
* `/jobs/{job_id}`

The purpose is to quickly verify that the system works after changes.

---

## GitHub Actions CI

The project includes a GitHub Actions workflow.

The CI pipeline:

1. checks out the repository
2. creates the `.env` file from `.env.example`
3. builds Docker services
4. starts the system
5. waits for the API health check
6. runs the collector
7. runs smoke tests
8. shuts down services

This helps make the repository more reliable and portfolio-ready.

---

## Current Limitations

The project currently uses LinkedIn-style sample data.

It does not connect to a real LinkedIn data provider yet.

The current data source is:

```text
sample_data/new_jobs.json
```

A production version would need:

* authorized job data source
* scheduled collector runs
* production database
* authentication
* rate limiting
* deployment to cloud infrastructure

---

## Future Improvements

Possible next steps:

* deploy API to Google Cloud Run
* use Cloud SQL PostgreSQL
* deploy frontend to Firebase Hosting or Cloud Run
* add Cloud Scheduler for collector automation
* add Redis caching
* add API key authentication
* add full-text search
* add tests with pytest
* add job expiration logic
* add active/inactive job status
* improve frontend UI
* add charts for job statistics

---

## Summary

JobPulse is designed as a portfolio-ready backend project that demonstrates:

* REST API development
* database design
* PostgreSQL usage
* Docker Compose
* data collector architecture
* provider-based design
* frontend-backend integration
* health checks
* smoke testing
* GitHub Actions CI

The project is intentionally structured to be extendable and cloud-ready.
