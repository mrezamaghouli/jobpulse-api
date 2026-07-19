# JobPulse Production Status

## Current Status

JobPulse production is live and operational.

## Public URLs

- Frontend: http://35.192.251.190
- Admin: http://35.192.251.190/admin.html
- API Health: http://35.192.251.190/api/health
- API Docs: http://35.192.251.190/api-docs.html
- API Version: http://35.192.251.190/api/version
- API Docs Info: http://35.192.251.190/api/docs-info

## Production Stack

- Server path: /opt/jobpulse
- Compose file: docker-compose.prod.yml
- API image: ghcr.io/mrezamaghouli/jobpulse-api:main
- Database: PostgreSQL
- Frontend: Nginx static frontend
- CI/CD: GitHub Actions + GHCR pull deploy

## Completed Production Features

- Public API key protection
- Rate limiting
- Search ranking and filters
- Admin dashboard
- Backup monitoring
- Restore verification
- Deploy status monitoring
- GHCR no-build deploy
- Automatic rollback on failed API health check
- Public API docs page
- Public API smoke test
- Production runbook
- Final production checklist

## Validation Commands

- ./scripts/smoke_test_public_api.sh http://localhost
- ./scripts/smoke_test_public_api.sh http://35.192.251.190
- curl -fsS http://35.192.251.190/api/version | python3 -m json.tool
- curl -fsS http://35.192.251.190/api/docs-info | python3 -m json.tool

## Production Rule

Do not build the API image on the production VM.

Use:

- ./scripts/deploy_prod_from_ghcr.sh
