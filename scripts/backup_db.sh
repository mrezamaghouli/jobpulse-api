#!/usr/bin/env bash
set -euo pipefail

cd /opt/jobpulse

mkdir -p /opt/jobpulse/backups

docker compose -f docker-compose.prod.yml exec -T db pg_dump \
  -U jobpulse_user \
  -d jobpulse \
  > /opt/jobpulse/backups/jobpulse_$(date +%Y%m%d_%H%M%S).sql

find /opt/jobpulse/backups -name "jobpulse_*.sql" -type f -mtime +7 -delete
