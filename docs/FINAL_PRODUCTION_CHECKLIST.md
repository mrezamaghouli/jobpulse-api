# JobPulse Final Production Checklist

## Production Access

- [x] Production VM is available.
- [x] Project path is /opt/jobpulse.
- [x] Docker Compose production file exists.
- [x] Public frontend is reachable.
- [x] Admin dashboard is protected.
- [x] API health endpoint is reachable.

## Production URLs

- Frontend: http://35.192.251.190
- Admin: http://35.192.251.190/admin.html
- Health: http://35.192.251.190/api/health
- API Docs: http://35.192.251.190/api-docs.html
- Version: http://35.192.251.190/api/version
- DocsInfo: http://35.192.251.190/api/docs-info

## CI/CD

- [x] API image is built by GitHub Actions.
- [x] API image is pushed to GHCR.
- [x] Production deploy pulls from GHCR.
- [x] Production VM does not build the API image locally.
- [x] Deploy script has no-build guard.
- [x] Deploy script writes deploy status.
- [x] Deploy script supports automatic rollback on failed health check.
- [x] Frontend and docs changes trigger production deploy.

## API

- [x] /api/health works.
- [x] /api/version works.
- [x] /api/docs-info works.
- [x] /api/jobs/search requires API key.
- [x] Search with API key works.
- [x] Rate-limit headers are present.
- [x] Public API smoke test exists.
- [x] API quickstart docs exist.
- [x] Public API docs page exists.

## Admin Dashboard

- [x] Admin dashboard is available.
- [x] Basic Auth protects admin page.
- [x] Admin token protects admin API.
- [x] Collection status is visible.
- [x] Backup status is visible.
- [x] Backup inventory is visible.
- [x] Deploy status is visible.
- [x] Admin action buttons exist.
- [x] API docs quick link exists.

## Backups

- [x] PostgreSQL backup script exists.
- [x] Backup monitor exists.
- [x] Restore verification script exists.
- [x] Backup status JSON exists.
- [x] Backup inventory is shown in Admin.
- [x] Manual backup action exists.
- [x] Manual restore verification action exists.

## Collection

- [x] Safe collection wrapper exists.
- [x] LinkedIn auth preflight exists.
- [x] Collection heartbeat exists.
- [x] Collection history exists.
- [x] Queue status can be checked.
- [x] Heavy post-processing is separated from collection.

## Documentation

- [x] README has production information.
- [x] Production runbook exists.
- [x] API quickstart exists.
- [x] Public API docs page exists.
- [x] Smoke test documentation exists.
- [x] Final production checklist exists.

## Security Rules

- [x] Production secrets are not committed.
- [x] .env is not committed.
- [x] .api_keys.env is not committed.
- [x] .admin.env is not committed.
- [x] .admin_token is not committed.
- [x] .admin_htpasswd is not committed.
- [x] .telegram_alert.env is not committed.
- [x] .auth/ is not committed.
- [x] logs/ is not committed.
- [x] backups/ is not committed.
- [x] .collection.env is not committed.

## Final Validation Commands

Run these commands after important production changes:

- cd /opt/jobpulse
- curl -fsS http://localhost/api/health && echo OK
- curl -fsS http://35.192.251.190/api/version | python3 -m json.tool
- curl -fsS http://35.192.251.190/api/docs-info | python3 -m json.tool
- curl -fsS http://35.192.251.190/api-docs.html | grep -n "JobPulse API Docs" | head
- ./scripts/smoke_test_public_api.sh http://localhost
- ./scripts/smoke_test_public_api.sh http://35.192.251.190

## Production Rule

Use this for production API deploy:

- cd /opt/jobpulse
- ./scripts/deploy_prod_from_ghcr.sh

Do not use this on production:

- docker compose up -d --build
