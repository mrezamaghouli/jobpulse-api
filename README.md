# JobPulse

JobPulse is a production-ready LinkedIn job collection, search, and dashboard system.

It collects job data, stores it in PostgreSQL, exposes searchable API endpoints, and provides an admin dashboard for monitoring production health, collection status, backups, deployment status, and operational actions.

---

## Production URLs

```text
Frontend: http://35.192.251.190
Admin:    http://35.192.251.190/admin.html
Health:   http://35.192.251.190/api/health
```

---

## Production Server

```text
VM:       jobpulse-prod
IP:       35.192.251.190
Path:     /opt/jobpulse
Compose:  /opt/jobpulse/docker-compose.prod.yml
```

Main production containers:

```text
jobpulse-postgres-prod
jobpulse-api-prod
jobpulse-frontend-prod
```

---

## Production Deployment

Production API deployment uses GitHub Container Registry.

The production VM must not build the API image locally.

### Normal API Deploy

```bash
cd /opt/jobpulse

./scripts/deploy_prod_from_ghcr.sh

docker compose -f docker-compose.prod.yml ps

curl -fsS http://localhost/api/health && echo OK
```

### Frontend-only Restart

Use this when only frontend static files changed.

```bash
cd /opt/jobpulse

docker compose -f docker-compose.prod.yml restart frontend
```

Do not run this on the production VM:

```bash
docker compose up -d --build
```

---

## Deploy Status

Deploy status is stored here:

```text
/opt/jobpulse/logs/deploy_status.json
```

Check it with:

```bash
cd /opt/jobpulse

cat logs/deploy_status.json | python3 -m json.tool

tail -n 100 logs/deploy_prod_from_ghcr.log
```

Expected healthy status:

```json
{
  "status": "success",
  "message": "Deploy completed successfully"
}
```

The deploy script includes a no-build guard and automatic rollback if the API health check fails after deploy.

---

## Production Runbook

Full production operations guide:

```text
docs/PRODUCTION_RUNBOOK.md
```

The runbook includes:

```text
Deploy
Rollback notes
Backup
Restore verification
Collection cycle
Admin status
Logs
Disk cleanup
Safety rules
```

---

## Public API Usage

Public API endpoints are protected by API key.

Send the API key using this header:

```http
X-API-Key: YOUR_API_KEY
```

---

## API Base URL

```text
http://35.192.251.190/api
```

---

## Health Check

```bash
curl -fsS http://35.192.251.190/api/health
```

---

## Search Jobs

```bash
curl -sS \
  -H "X-API-Key: YOUR_API_KEY" \
  "http://35.192.251.190/api/jobs/search?query=data%20analyst&location=Germany&limit=10"
```

---

## Common Search Parameters

```text
query
location
company
work_mode
apply_type
posted_within_days
has_apply_url
has_logo
sort_by
sort_order
page
limit
```

---

## Sort Modes

```text
relevance
last_seen_at
first_seen_at
date_posted_at
```

---

## Example: Relevance Search

```bash
curl -sS \
  -H "X-API-Key: YOUR_API_KEY" \
  "http://35.192.251.190/api/jobs/search?query=data%20analyst&location=Germany&has_logo=true&sort_by=relevance&sort_order=desc&limit=10"
```

---

## API Quickstart

See:

```text
docs/API_QUICKSTART.md
```


---

## API Metadata Endpoints

These public endpoints do not require an API key.

### Version

```bash
curl -fsS http://35.192.251.190/api/version | python3 -m json.tool
```

Example response fields:

```text
name
status
environment
version
image
server_time_utc
docs
```

### Docs Info

```bash
curl -fsS http://35.192.251.190/api/docs-info | python3 -m json.tool
```

Example response fields:

```text
name
base_url
authentication
endpoints
sort_fields
docs_files
```

These endpoints expose runtime metadata, API authentication information, available public endpoints, and documentation references.

---

## Backup

PostgreSQL backups are stored here:

```text
/opt/jobpulse/backups/postgres/
```

Backup status is stored here:

```text
/opt/jobpulse/logs/postgres_backup_status.json
```

Check backup status:

```bash
cd /opt/jobpulse

cat logs/postgres_backup_status.json | python3 -m json.tool

ls -lh backups/postgres | tail -n 20
```

Run backup manually:

```bash
cd /opt/jobpulse

./scripts/backup_postgres_prod.sh

./scripts/check_postgres_backups.py | python3 -m json.tool
```

Run restore verification manually:

```bash
cd /opt/jobpulse

./scripts/restore_verify_postgres_prod.sh

./scripts/check_postgres_backups.py | python3 -m json.tool
```

---

## Collection Cycle

Run a safe collection cycle manually:

```bash
cd /opt/jobpulse

./scripts/run_collection_cycle_safe.sh

tail -n 100 logs/collection_cycle.log

cat logs/collection_heartbeat.json | python3 -m json.tool
```

Check queue status:

```bash
cd /opt/jobpulse

docker compose -f docker-compose.prod.yml exec -T db psql -U jobpulse_user -d jobpulse -c "
SELECT status, COUNT(*)
FROM job_search_demand_queue
GROUP BY status
ORDER BY status;
"
```

---

## Admin Status API

```bash
cd /opt/jobpulse

TOKEN="$(cat /opt/jobpulse/.admin_token)"

curl -sS \
  -H "X-Admin-Token: $TOKEN" \
  http://127.0.0.1:8000/api/admin/status \
  | python3 -m json.tool | head -n 120
```

---

## Production Safety Rules

Do not commit production secrets.

Never commit these files or folders:

```text
.env
.api_keys.env
.admin.env
.admin_token
.admin_htpasswd
.telegram_alert.env
.auth/
logs/
backups/
.collection.env
```

Production rules:

```text
API deploy must use GHCR image pull.
Do not build API image on the VM.
Check /api/health after deploy.
Check Admin Dashboard after important changes.
Do not remove PostgreSQL volumes.
Keep backups and restore verification active.
```

---

## GitHub Actions

Main workflows:

```text
.github/workflows/docker-build.yml
.github/workflows/deploy.yml
```

Expected flow:

```text
Push to main
→ Build JobPulse API Image
→ Deploy Production
→ VM pulls ghcr.io/mrezamaghouli/jobpulse-api:main
→ Health check
```

---

## Local Development Note

Local development can use Docker Compose, but production must use the production compose file and GHCR deploy script.

For production operations, follow:

```text
docs/PRODUCTION_RUNBOOK.md
```
