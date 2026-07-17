# JobPulse Production Runbook

## Production Server

- VM: `jobpulse-prod`
- App path: `/opt/jobpulse`
- Public IP: `35.192.251.190`
- Frontend: `http://35.192.251.190`
- Admin: `http://35.192.251.190/admin.html`
- API health: `http://35.192.251.190/api/health`

## Important Files

```text
/opt/jobpulse/docker-compose.prod.yml
/opt/jobpulse/.env
/opt/jobpulse/.api_keys.env
/opt/jobpulse/.admin.env
/opt/jobpulse/.admin_token
/opt/jobpulse/.telegram_alert.env
/opt/jobpulse/logs/
/opt/jobpulse/backups/postgres/
Do not commit production secrets.

Normal API Deploy

Production API deploy must use GHCR image pull. Do not build on the VM.

cd /opt/jobpulse

./scripts/deploy_prod_from_ghcr.sh

docker compose -f docker-compose.prod.yml ps

curl -fsS http://localhost/api/health && echo OK
Frontend Restart Only

Use this when only frontend/*.html or nginx static files changed.

cd /opt/jobpulse

docker compose -f docker-compose.prod.yml restart frontend

curl -fsS http://localhost/admin.html >/tmp/admin.html

Never use --build for frontend-only changes.

Check Current Status
cd /opt/jobpulse

docker compose -f docker-compose.prod.yml ps

curl -fsS http://localhost/api/health && echo OK

TOKEN="$(cat /opt/jobpulse/.admin_token)"
curl -sS -H "X-Admin-Token: $TOKEN" http://127.0.0.1:8000/api/admin/status \
  | python3 -m json.tool | head -n 120
Deploy Status
cd /opt/jobpulse

cat logs/deploy_status.json | python3 -m json.tool

tail -n 100 logs/deploy_prod_from_ghcr.log

Expected healthy deploy status:

{
  "status": "success",
  "message": "Deploy completed successfully"
}
Manual Rollback Notes

The deploy script automatically rolls back to the previous API image if health check fails.

To inspect previous/current image IDs:

cd /opt/jobpulse

cat logs/deploy_status.json | python3 -m json.tool

docker inspect --format '{{.Image}}' jobpulse-api-prod
Backup Now
cd /opt/jobpulse

./scripts/backup_postgres_prod.sh

./scripts/check_postgres_backups.py | python3 -m json.tool
Restore Verify Now

This restores the latest backup into a temporary verification database and then drops it.

cd /opt/jobpulse

./scripts/restore_verify_postgres_prod.sh

./scripts/check_postgres_backups.py | python3 -m json.tool
Backup Status
cd /opt/jobpulse

cat logs/postgres_backup_status.json | python3 -m json.tool

ls -lh backups/postgres | tail -n 20

Expected healthy backup status:

{
  "ok": true,
  "latest_backup_sha256_ok": true
}
Admin Manual Backup Actions

Admin action requests are created by the API and executed by the host runner.

cd /opt/jobpulse

./scripts/run_requested_backup_actions.sh

ls -lah logs/admin_requests

cat logs/admin_requests/postgres_backup_last_result.json 2>/dev/null | python3 -m json.tool || true
cat logs/admin_requests/postgres_restore_verify_last_result.json 2>/dev/null | python3 -m json.tool || true
Collection Cycle
cd /opt/jobpulse

./scripts/run_collection_cycle_safe.sh

tail -n 100 logs/collection_cycle.log

cat logs/collection_heartbeat.json | python3 -m json.tool
Collection Queue Status
cd /opt/jobpulse

docker compose -f docker-compose.prod.yml exec -T db psql -U jobpulse_user -d jobpulse -c "
SELECT status, COUNT(*)
FROM job_search_demand_queue
GROUP BY status
ORDER BY status;
"
Logs
cd /opt/jobpulse

tail -n 100 logs/deploy_prod_from_ghcr.log
tail -n 100 logs/postgres_backup.log
tail -n 100 logs/postgres_restore_verify.log
tail -n 100 logs/postgres_backup_monitor.cron.log
tail -n 100 logs/collection_cycle.log
Disk Usage
df -h /opt/jobpulse

docker system df

Safe cleanup:

docker builder prune -af
docker image prune -af
docker container prune -f

Do not remove PostgreSQL volumes.

GitHub Actions

Main workflows:

.github/workflows/docker-build.yml
.github/workflows/deploy.yml

Expected flow:

Push to main
→ Build JobPulse API Image
→ Deploy Production
→ VM pulls ghcr.io/mrezamaghouli/jobpulse-api:main
→ Health check
Production Rules
Do not run docker compose up -d --build on the VM.
Do not commit .env, .api_keys.env, .admin.env, .admin_token, .telegram_alert.env, .auth, logs, or backups.
API deploy must use ./scripts/deploy_prod_from_ghcr.sh.
Frontend-only changes use docker compose -f docker-compose.prod.yml restart frontend.
Check /api/health after every deploy.
Check Admin Dashboard after important changes.
