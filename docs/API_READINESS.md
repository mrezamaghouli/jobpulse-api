# JobPulse API Production Readiness

## Public API

Base URL:

http://35.192.251.190/api

## Main Endpoints

GET /health
GET /jobs
GET /jobs/search
GET /jobs/stats
GET /jobs/quality
GET /jobs/{job_id}

## Example Search

GET /api/jobs?query=python%20backend%20remote&limit=5&page=1

## Search Quality

The API returns ranked job results using:

- base search score
- title relevance
- query term matching
- synonym matching
- remote match boost
- recency boost
- negative role penalty
- quality_score
- quality_reasons

## API Protection

Enabled production guards:

- max query length
- max limit/page
- max URL length
- method restrictions
- rate limiting
- safe error responses
- request IDs
- security headers

## Caching

Public job search endpoints use short-lived in-memory cache.

Default:

API_CACHE_TTL_SECONDS=90
API_CACHE_MAX_ITEMS=500

## Admin API

Protected by:

X-Admin-Key: private key

Admin endpoints:

GET /api/admin/summary
GET /api/admin/collection-cycles
GET /api/admin/demand-queue
GET /api/admin/search-events
GET /api/admin/jobs-health

## Collection System

The production collector runs through:

scripts.run_collection_cycle

It records every cycle in:

collection_cycles

Each cycle stores:

- trigger name
- status
- seed/process limit
- jobs before/after
- jobs delta
- pending queue before/after
- stdout/stderr tail
- error if failed

## Monitoring

Production monitoring includes:

- API health check
- Docker service check
- database job count check
- disk usage check
- Telegram alert on failure/recovery
- daily cleanup
- log rotation

## Smoke Test

Run:

cd /opt/jobpulse
./scripts/production_smoke_test.sh

Expected final line:

production_smoke_test_finished status=OK
