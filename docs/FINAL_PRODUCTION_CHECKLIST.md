# JobPulse Final Production Checklist

## Production Access

- [x] Production VM is available.
- [x] Project path is `/opt/jobpulse`.
- [x] Docker Compose production file exists.
- [x] Public frontend is reachable.
- [x] Admin dashboard is protected.
- [x] API health endpoint is reachable.

## Production URLs

```text
Frontend: http://35.192.251.190
Admin:    http://35.192.251.190/admin.html
Health:   http://35.192.251.190/api/health
API Docs: http://35.192.251.190/api-docs.html
Version:  http://35.192.251.190/api/version
DocsInfo: http://35.192.251.190/api/docs-info
CI/CD
 API image is built by GitHub Actions.
 API image is pushed to GHCR.
 Production deploy pulls from GHCR.
 Production VM does not build the API image locally.
 Deploy script has no-build guard.
 Deploy script writes deploy status.
 Deploy script supports automatic rollback on failed health check.
 Frontend and docs changes trigger production deploy.
API
 /api/health works.
 /api/version works.
 /api/docs-info works.
 /api/jobs/search requires API key.
 Search with API key works.
 Rate-limit headers are present.
 Public API smoke test exists.
 API quickstart docs exist.
 Public API docs page exists.
Admin Dashboard
 Admin dashboard is available.
 Basic Auth protects admin page.
 Admin token protects admin API.
 Collection status is visible.
 Backup status is visible.
 Backup inventory is visible.
 Deploy status is visible.
 Admin action buttons exist.
 API docs quick link exists.
Backups
 PostgreSQL backup script exists.
 Backup monitor exists.
 Restore verification script exists.
 Backup status JSON exists.
 Backup inventory is shown in Admin.
 Manual backup action exists.
 Manual restore verification action exists.
Collection
 Safe collection wrapper exists.
 LinkedIn auth preflight exists.
 Collection heartbeat exists.
 Collection history exists.
 Queue status can be checked.
 Heavy post-processing is separated from collection.
Documentation
 README has production information.
 Production runbook exists.
 API quickstart exists.
 Public API docs page exists.
 Smoke test documentation exists.
 Final production checklist exists.
Security Rules
 Production secrets are not committed.
 .env is not committed.
 .api_keys.env is not committed.
 .admin.env is not committed.
 .admin_token is not committed.
 .admin_htpasswd is not committed.
 .telegram_alert.env is not committed.
 .auth/ is not committed.
 logs/ is not committed.
 backups/ is not committed.
 .collection.env is not committed.
Final Validation Commands
cd /opt/jobpulse

curl -fsS http://localhost/api/health && echo OK

curl -fsS http://35.192.251.190/api/version | python3 -m json.tool

curl -fsS http://35.192.251.190/api/docs-info | python3 -m json.tool

curl -fsS http://35.192.251.190/api-docs.html | grep -n "JobPulse API Docs" | head

./scripts/smoke_test_public_api.sh http://localhost

./scripts/smoke_test_public_api.sh http://35.192.251.190
Production Rule

Do not run API builds on the production VM.

Use:

cd /opt/jobpulse

./scripts/deploy_prod_from_ghcr.sh

Do not use:

docker compose up -d --build

