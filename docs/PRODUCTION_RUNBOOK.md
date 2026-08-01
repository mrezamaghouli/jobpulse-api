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
```

Do not commit production secrets.

`.api_keys.env` must contain:

```text
JOBPULSE_PUBLIC_API_KEYS=<comma-separated-strong-random-keys>
```

This protects /jobs* endpoints. If it is missing or empty, the API container
still starts and /health still passes, but every /jobs* request fails closed
with 503 api_key_not_configured. Run
./scripts/check_production_readiness.py before every deploy to catch this
ahead of time.

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

---

## Production Alert Reliability

`scripts/run_production_alert_checks.sh` is the single, independently
schedulable entry point that runs BOTH alert checks every time, regardless
of collector activity:

```bash
cd /opt/jobpulse
./scripts/run_production_alert_checks.sh
```

It runs `scripts/production_health_alert.sh` (Docker/API/DB/disk) and then
`scripts/send_telegram_alerts.py` (collector heartbeat + `/api/admin/status`
alerts) unconditionally -- the second check always runs even if the first
fails -- and exits non-zero if either one failed. This closes the gap where
`send_telegram_alerts.py` was previously only invoked from inside
`run_collection_cycle_safe.sh`'s own failure/abort paths, so a collector
that stopped running entirely (cron removed, or a cycle that "succeeded"
with zero jobs) was never independently checked.

**No cron/systemd schedule for this script is installed by this
repository or by any commit history in it.** The following is an EXAMPLE
an operator must install explicitly on the production host:

```cron
# Example only -- NOT installed automatically. Add via `crontab -e` on
# the production host if you want this to run on a schedule.
*/5 * * * * /opt/jobpulse/scripts/run_production_alert_checks.sh >> /opt/jobpulse/logs/production_alert_checks.cron.log 2>&1
```

Under normal operation a run finishes in well under 5 minutes (see
"Bounded execution" below). The wrapper's absolute worst case -- both
components independently hitting their full configured deadline -- is
larger than 5 minutes and can in principle overlap the next tick. This
is expected and handled safely, not a bug: an overlapping tick simply
exits cleanly via the wrapper's own top-level lock
(`run_production_alert_checks_already_running`, exit 0) instead of
running concurrently or racing state -- see "Lock-busy exit semantics"
below for the exact figures and why exit 0 is correct there. If you want
overlap to never happen at all rather than being safely absorbed by the
lock, use a scheduler interval comfortably above the documented absolute
maximum (500s with default settings) instead of 5 minutes.

To check what is actually installed on the production host (never assume
from this repo or from CI):

```bash
crontab -l
systemctl list-timers 2>/dev/null
```

### Delivery, cooldown, and recovery semantics

- **Telegram success criteria**: the HTTP request must succeed AND the
  decoded JSON response body must contain `"ok": true`. A network error,
  timeout, non-2xx response, malformed/non-JSON body, or `"ok": false` are
  all treated as failed delivery -- never as success.
- **Cooldown starts only after a confirmed successful delivery.** The
  cooldown/dedup timestamp is written *after* Telegram confirms the send,
  never before or regardless of outcome. A failed delivery leaves the
  incident immediately eligible for retry on the very next scheduled poll.
- **Recovery notifications** are sent only for an incident whose opening
  failure alert was itself confirmed delivered, and the delivered-failure
  marker is cleared only after the recovery message itself is confirmed
  delivered -- a failed recovery send is retried on the next poll, not
  silently dropped. This check is on the recovery-pending marker alone
  (`failure_delivered` in the shell script's state / a non-empty
  `delivered_codes` in the Python script's state); it does not additionally
  require the detector's last-observed status to still say "failed," since
  a prior *failed* recovery attempt legitimately leaves the observed
  status at "healthy" while the incident is still open pending a
  successfully delivered recovery.
- **Missing Telegram configuration fails closed.** If
  `scripts/send_telegram_alerts.py` runs with `TELEGRAM_BOT_TOKEN` and/or
  `TELEGRAM_CHAT_ID` unset, it exits **non-zero** -- an independently
  scheduled alert monitor that has no way to deliver a notification is a
  failed check, not a silent no-op, so it must not be reported as success.
  No state is marked delivered in this case. To intentionally run this
  script somewhere Telegram delivery is not wanted, set
  `JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED=1` explicitly; without it, the
  default is always fail-closed.
- Both `scripts/production_health_alert.sh` and `scripts/send_telegram_alerts.py`
  hold their own non-blocking `flock`-based lock for the duration of a
  single invocation (released automatically on process exit, so a crash
  can never leave it permanently held); `run_production_alert_checks.sh`
  holds a separate, higher-level lock so overlapping wrapper invocations
  (e.g. cron overlapping a slow manual run) are rejected cleanly instead
  of racing state.

### Bounded execution: every external call has a hard timeout

Every command each script runs that could otherwise block indefinitely is
now wrapped in an explicit, configurable timeout -- HTTP calls via curl's
own `--max-time` / Python's `urllib` `timeout=`, and every Docker/DB
command via GNU coreutils `timeout`. This was not always true: an earlier
version of this remediation bounded the two HTTP calls but left every
`docker compose ...` / `docker inspect` / `docker compose exec ... psql`
call completely unbounded, and documented that gap as an accepted,
out-of-scope caveat. That gap has since been reproduced (a hung fake
`docker compose ps -q` was shown to hold the flock forever, and a
subsequent invocation reported `health_alert_already_running` -- exit 0
-- indefinitely, while the real check never completed even once) and
fixed. **No external call in these three scripts is unbounded any more.**

#### `scripts/production_health_alert.sh` timeouts

| Variable | Default | Bounds | Applies to |
|---|---|---|---|
| `HEALTH_ALERT_DOCKER_COMMAND_TIMEOUT_SECONDS` | 15s | 1-120s | `docker compose ps -q`, `docker inspect`, `docker compose ps` (alert/recovery context) |
| `HEALTH_ALERT_DB_QUERY_TIMEOUT_SECONDS` | 15s | 1-120s | `docker compose exec ... psql` (jobs-count query) |
| `HEALTH_ALERT_KILL_AFTER_SECONDS` | 5s | 1-30s | grace period before SIGKILL, for every Docker/DB command above |
| `HEALTH_ALERT_REQUEST_TIMEOUT_SECONDS` | 20s | 1-120s | Telegram send (curl `--max-time`) |
| `HEALTH_ALERT_API_TIMEOUT_SECONDS` | 10s | 1-120s | `/api/health` check (curl `--max-time`) |
| `HEALTH_ALERT_COOLDOWN_SECONDS` | 3600s | 1-86400s | failure-alert cooldown (unrelated to bounding execution) |

Every Docker/DB command is run via GNU `timeout -k <kill_after> <duration> <cmd>` -- deliberately **without** `--foreground`: without it, `timeout` puts the command in its own new process group and signals the whole group on timeout, so any subprocess the command itself forks (as `docker` sometimes does) is killed too. This was verified empirically: `--foreground` left a forked grandchild process running after `timeout` reaped only the direct child, which would have violated "no child process may remain running." A timeout is logged with `category=timeout` (distinct from `category=non_zero_exit` for an ordinary command failure) and always marks the health check failed -- it is never silently absorbed.

**Documented maximum lock duration for `production_health_alert.sh`, with default settings:** up to 3 services x 2 Docker calls x (15+5)s = 120s, + 1 DB query x (15+5)s = 20s, + 1 API health check x 10s, + up to 1 `docker compose ps` context call x (15+5)s = 20s, + 1 Telegram send x 20s = **190 seconds (≈3.2 minutes) worst case**, safely under the 5-minute example cron interval below. If you customize these variables, keep `(3 x 2 x (DOCKER_COMMAND_TIMEOUT + KILL_AFTER)) + (DB_QUERY_TIMEOUT + KILL_AFTER) + API_TIMEOUT + (DOCKER_COMMAND_TIMEOUT + KILL_AFTER) + REQUEST_TIMEOUT` comfortably below your chosen scheduler interval.

If GNU `timeout` is not installed, every Docker-related check fails immediately and clearly (`category=timeout_utility_missing`, health check marked failed) rather than running that command unbounded.

#### `scripts/run_production_alert_checks.sh` timeouts

| Variable | Default | Bounds | Applies to |
|---|---|---|---|
| `JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS` | 240s | 1-3600s | each child's own independent deadline |
| `JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS` | 10s | 1-60s | grace period before SIGKILL, per child |

Each child (`production_health_alert.sh` and `send_telegram_alerts.py`) gets its **own, independent** deadline -- the wrapper does not share one deadline across both. This matters: it was reproduced that a hung health-check child previously blocked the wrapper forever, and the telegram/admin-status check **never ran even once** while it was stuck. With per-child deadlines, the second check always runs on schedule regardless of what the first one does, including a hang. Like the inner script, this uses `timeout` without `--foreground` so any grandchild process (docker, curl, python3 subprocesses the child itself spawns) is killed together with it, and a timeout is logged as `category=timeout`, distinct from an ordinary non-zero exit.

**Documented maximum lock duration for `run_production_alert_checks.sh`:** under normal operation (assuming each sub-script's own internal bounds hold, which they now do), at most ~190s (health) + ~40s (telegram, at most two HTTP calls x default 20s) = **~230 seconds** -- comfortably inside the 5-minute example cron interval above. The wrapper additionally enforces a hard backstop regardless of what happens inside either sub-script: 2 x (`JOBPULSE_ALERT_COMPONENT_TIMEOUT_SECONDS` + `JOBPULSE_ALERT_COMPONENT_KILL_AFTER_SECONDS`) = 2 x 250s = **500 seconds absolute maximum** with default settings -- this figure is **larger than** the 5-minute (300s) example interval, not smaller; it is a pathological worst case (both components independently timing out), not the typical run duration. A tick that fires while a prior pathological run is still finishing exits cleanly via the wrapper's own lock (see "Lock-busy exit semantics" below) rather than overlapping it or racing state -- it is not silently dropped, it is safely deferred.

If GNU `timeout` is not installed, both components fail immediately and clearly (`category=timeout_utility_missing`) rather than running unbounded.

#### `scripts/send_telegram_alerts.py` timeout

`JOBPULSE_TELEGRAM_TIMEOUT_SECONDS` (default 20s, capped at 120s) bounds both the admin-status fetch and the Telegram send -- at most two calls, so **at most ~40 seconds** with default settings. This value is parsed safely (see "Safe configuration parsing" below) and re-evaluated after `.telegram_alert.env` is loaded, not frozen at import time.

### Lock-busy exit semantics (intentional, now fully bounded)

If any of the three locks (`production_health_alert.sh`'s own lock,
`send_telegram_alerts.py`'s own lock, or `run_production_alert_checks.sh`'s
top-level wrapper lock) is already held by another running invocation, that
script logs `*_already_running` and exits **0**, not non-zero. This is a
deliberate design choice: it means *"another invocation of this exact
check is already in flight,"* not *"monitoring status is unknown."*
Reporting a lock-busy exit as a check *failure* would be a false positive
-- the check is actively being performed by the other invocation, just not
by this one.

This is now safe on all three locks because **every** code path that can
hold each lock is bounded, as documented above -- there is no remaining
exception. Concretely:

- `production_health_alert.sh`'s own lock: held for at most ~190s (documented above).
- `send_telegram_alerts.py`'s own lock: held for at most ~40s (documented above).
- `run_production_alert_checks.sh`'s wrapper lock: held for at most ~230s under normal operation, with a hard 500s backstop regardless (documented above).

This was proven, not just asserted: `tests/test_health_alert_reliability.py`
includes regression tests that make each of `docker compose ps -q`,
`docker inspect`, the DB query, the `docker compose ps` context call, and
each wrapper child hang indefinitely via fake executables, and assert (a)
the timeout fires and the process (including any forked grandchild) is
killed, (b) the check is marked failed with a `category=timeout` log line,
(c) the lock is released promptly afterward, and (d) a subsequent
invocation performs the real check rather than reporting
`*_already_running` again. Under normal operation, lock contention still
just means an operator manually running a script while cron also fires --
but now it is also true, and tested, in the timeout case: contention
resolves within the documented maximum, never indefinitely.

### Safe configuration parsing

Both bash duration variables (the tables above) and the Python
`JOBPULSE_TELEGRAM_TIMEOUT_SECONDS` / `JOBPULSE_TELEGRAM_ALLOW_UNCONFIGURED`
variables are validated before use:

- **Bash** (`validate_duration_seconds` in both scripts): a value must be
  a plain unsigned integer of at most 9 digits (rejects arithmetic
  overflow) within the variable's documented `[min,max]` range. Empty,
  malformed, negative (no `-` is ever accepted by the pattern), zero, or
  excessively large values silently fall back to the documented default
  -- never crash bash arithmetic, never disable monitoring, and the raw
  malformed value is never logged.
- **Python** (`parse_timeout_seconds` in `send_telegram_alerts.py`): a
  value must parse as a finite (`math.isfinite` -- rejects `nan`/`inf`/
  `Infinity` strings, which `float()` itself accepts without raising),
  positive number; whitespace is trimmed; malformed/empty/zero/negative/
  non-finite values fall back to the default (20s); values above the
  maximum (120s) are capped. This parsing happens inside `run_check()`,
  **after** `load_env_file()` runs -- not as a module-level constant
  evaluated at import time, which would both crash on a malformed value
  and never see a value set only in `.telegram_alert.env`. The raw value
  is never included in logged output.

### State and lock file locations

```text
/opt/jobpulse/state/health_alert_state.json          # infrastructure check state
/opt/jobpulse/state/health_alert.lock                # infrastructure check lock
/opt/jobpulse/state/run_production_alert_checks.lock  # wrapper's top-level lock
/opt/jobpulse/logs/telegram_alert_state.json          # collector/admin-status alert state
/opt/jobpulse/logs/telegram_alert_state.lock          # collector/admin-status alert lock
```

All of the above are overridable via environment variables
(`JOBPULSE_ALERT_ROOT`, `JOBPULSE_ALERT_STATE_DIR`,
`JOBPULSE_ALERT_STATE_FILE`, `JOBPULSE_ALERT_LOCK_FILE`,
`JOBPULSE_TELEGRAM_STATE_PATH`, `JOBPULSE_TELEGRAM_LOCK_PATH`, etc.) for
testing; production defaults require no configuration.

### Inspecting alert logs

```bash
tail -n 100 /opt/jobpulse/logs/production_alert_checks.log
tail -n 100 /opt/jobpulse/logs/health_alert.log
cat /opt/jobpulse/state/health_alert_state.json | python3 -m json.tool
cat /opt/jobpulse/logs/telegram_alert_state.json | python3 -m json.tool
```

To specifically find timeout failures (as opposed to ordinary non-zero
exits) in the logs:

```bash
grep 'category=timeout' /opt/jobpulse/logs/health_alert.log
grep 'category=timeout' /opt/jobpulse/logs/production_alert_checks.log
grep 'category=timeout_utility_missing' /opt/jobpulse/logs/health_alert.log /opt/jobpulse/logs/production_alert_checks.log
```

`category=timeout` means a Docker/DB command or a wrapper child hit its
configured deadline and was killed; `category=timeout_utility_missing`
means GNU `timeout` isn't installed on the host, so the corresponding
check refused to run unbounded and failed immediately instead --
installing `coreutils` (or the `timeout` package providing a GNU-compatible
`timeout(1)`) resolves that.

The Telegram bot token, admin token, and chat ID are redacted (replaced
with `***REDACTED***`) before anything is ever written to these logs or
state files. Specifically, `scripts/production_health_alert.sh`:

- never logs the constructed Telegram API URL (which embeds the bot
  token) at all;
- never logs a Telegram response body verbatim -- on a non-2xx or
  `"ok": false` response it logs only Telegram's own `"description"`
  field, truncated to 120 characters, and still passed through the same
  redaction as everything else;
- redacts both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (not just the
  token) from curl's own diagnostic text before logging it, as defense in
  depth against an upstream response that happens to echo either value
  back; a non-JSON response body is logged only as a fixed marker
  (`unparseable_response_body`), never as raw content;
- never logs the outgoing alert message text (which contains
  operational detail, not secrets, but is not useful diagnostic output
  either).

### Local validation without contacting Telegram

There is no dry-run flag in this phase. To validate the alert scripts'
logic without contacting Telegram, Docker, or production, run the test
suite -- it exercises the real scripts via fake `curl`/`docker` binaries
on `PATH` and a monkeypatched Python transport:

```bash
PYTHONPATH=. python -m pytest tests/test_health_alert_reliability.py -v
```

`scripts/production_health_alert.sh --test` still sends one real Telegram
message (unchanged from before this phase) for an operator who explicitly
wants a live, end-to-end confirmation that `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` are configured correctly.

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


---

## Public API Smoke Test

Run this after production deploys or public API changes.

### Local Nginx Test

```bash
cd /opt/jobpulse

./scripts/smoke_test_public_api.sh http://localhost
```

### Public IP Test

```bash
cd /opt/jobpulse

./scripts/smoke_test_public_api.sh http://35.192.251.190
```

The smoke test checks:

```text
/api/health
/api/version
/api/docs-info
/api-docs.html
/jobs/search without API key must return 401
/jobs/search with API key must return 200
JSON response validity
rate-limit headers when available
```

