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

## Collector Monitoring: Truthful Outcome Accounting (Phase 2A)

Phase 2A closes the "zero-job collection visibility" gap identified in
the phase 2 investigation: a collector cycle that exited 0 could not
previously be distinguished from one that discovered nothing, discovered
jobs but inserted zero (all duplicates or all filtered), or genuinely
inserted new jobs -- all four cases produced an identical
`heartbeat.last_status == "success"`.

This phase is scoped to **truthful accounting and instrumentation**. It
does **not** wire `collection_cycles` into PostgreSQL, does **not**
change `app/admin_status.py` / `/api/admin/status` / `/api/admin/jobs-health`,
and does **not** open any new admin alerts. See "Phase 2B limitations"
below for what is deliberately deferred.

An adversarial correctness pass was run against the first draft of this
phase and found (and fixed) several claims that were not actually proven
by the code. This section documents the CURRENT, corrected behavior --
several statements below explicitly note what an EARLIER revision
claimed and why that claim did not hold up.

### Collector result schema (`scripts/collector_result.py`)

Every `python -m scripts.collector_postgres` invocation writes exactly
one JSON document describing what actually happened, if
`JOBPULSE_COLLECTOR_RESULT_PATH` is set in its environment (the caller,
`scripts/linkedin_plan_collect.py`, always sets this to a fresh temp
path per query). Written via tempfile + fsync + `os.replace` -- no
partial file is ever exposed at the target path.

```json
{
  "schema_version": 1,
  "provider": "LinkedInBrowserProvider",
  "started_at": "...", "finished_at": "...", "duration_seconds": 12.3,
  "jobs_discovered": 20,
  "jobs_valid": 18,
  "jobs_filtered_invalid": 1,
  "jobs_filtered_non_linkedin": 1,
  "jobs_filtered_header_artifact": 1,
  "jobs_filtered_missing_identifier": 1,
  "rows_inserted": 12,
  "rows_updated_existing": 4,
  "persistence_errors": 0,
  "outcome": "success_with_new_rows",
  "error_category": null,
  "sanitized_error": null
}
```

**Filter reasons are separated.** `jobs_filtered_header_artifact` (a
LinkedIn search-results header artifact -- e.g. "500+ Software Engineer
Jobs in Germany" -- not a real job listing) and
`jobs_filtered_missing_identifier` (a real job row LinkedIn simply didn't
expose a stable ID for) used to be folded into one counter, hiding how
much of a query's filtered output was junk-row noise versus a real,
un-identifiable job. They are now distinct, both in `insert_job()`'s
return value (`ROW_OUTCOME_FILTERED_HEADER_ARTIFACT` vs
`ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER`) and in this schema.

**`rows_inserted` / `rows_updated_existing` are per-statement, proven
evidence -- not a global count.** An earlier revision of this phase
computed `success_with_new_rows` from a whole-table
`SELECT COUNT(*) FROM jobs` taken before and after the batch. That is
**not proof this collector inserted anything**: any concurrent process
inserting or deleting rows during the same window changes that same
delta without this collector having written a single row (and the
reverse: a real insert can be masked by a concurrent delete). That
before/after count is **gone entirely** -- `collect_jobs_to_postgres()`
no longer issues a whole-table `COUNT(*)` at all.

In its place, `insert_job()`'s own `INSERT ... ON CONFLICT DO UPDATE`
statement carries a `RETURNING (xmax = 0) AS inserted` clause. **This is
a PostgreSQL-SPECIFIC system-column technique, NOT a SQL-standard or
officially-guaranteed feature** -- it is an accepted, widely-deployed
convention, not a portable one: a row's `xmax` system column is unset
(`0`) for a tuple's very first version, and gets set to the current
transaction's ID the instant any UPDATE touches it, so a
freshly-INSERTed row reads back `xmax = 0` while a row that went through
the `ON CONFLICT DO UPDATE` branch (even one inserted moments earlier in
a *different* transaction) reads back a non-zero `xmax`. This is
per-statement, transaction-safe evidence from the EXACT SQL this
collector executed -- there is no separate pre-SELECT-then-INSERT (which
would introduce a TOCTOU race); the single INSERT statement both
performs the write and proves which branch fired, in the same round
trip. It would need re-deriving entirely if the collector ever targeted
a different database engine.

**Inconclusive RETURNING evidence is a FAILURE, never a guessed
outcome** (corrected in a second adversarial pass -- an earlier revision
treated `cursor.fetchone()` returning `None` as `ROW_OUTCOME_UPDATED_EXISTING`,
which is an unproven GUESS reported as if it were proven evidence, the
exact category of defect this whole schema exists to eliminate). The
current, corrected rule, in `_interpret_upsert_returning()`: only an
ACTUAL Python `bool` is accepted (`True` -> inserted, `False` -> updated
existing); a missing row, a `null`/`None` value, or ANY non-boolean value
(a numeric `0`/`1`, a string `"true"`/`"false"`, etc. -- `bool` is a
subtype of `int` in Python, so this check is `isinstance(value, bool)`,
never a truthiness check) raises `UpsertEvidenceError`. That exception is
caught by `collect_jobs_to_postgres()`'s existing persistence-error
handler exactly like any other DB exception: the transaction is rolled
back, `persistence_errors` is incremented, the whole run is reported as
`failed_persist` with a non-zero exit code -- it can never silently
become `success_no_new_rows` or any other success outcome.

**Real PostgreSQL 16 evidence for this SQL, versus a mocked cursor:**
`tests/test_upsert_returning_integration.py` proves (against a real,
disposable PostgreSQL 16 database, when one is available -- see "Phase
2B limitations" / CI below) that a fresh row's UPSERT returns
`inserted=true`, a conflicting UPSERT on the same `job_url` returns
`inserted=false`, this holds across separate transactions/connections
(not just within one), and a rolled-back transaction leaves no
persistent state. **A DISCOVERED, PRE-EXISTING FINDING, unrelated to any
code change in this pass:** grepping the whole repository found no
`UNIQUE` constraint (or unique index) on `jobs.job_url` anywhere in the
tracked PostgreSQL schema-management scripts
(`scripts/repair_jobpulse_schema.py`, `scripts/migrate_database.py`) --
only a plain, non-unique `idx_jobs_job_url` index. `insert_job()`'s
`ON CONFLICT (job_url) DO UPDATE` REQUIRES a unique/exclusion constraint
on `job_url` to be valid SQL at all; PostgreSQL raises `there is no
unique or exclusion constraint matching the ON CONFLICT specification`
otherwise. The integration test's own disposable table explicitly
creates this constraint (as instructed, "the minimum disposable jobs
table and matching unique constraint needed by insert_job()"), so a PASS
there proves the SQL is correct GIVEN the constraint exists -- it does
**NOT** prove production's real, live schema has it. This must be
verified directly against the real production database by an operator
(`\d jobs` or `SELECT conname FROM pg_constraint WHERE conrelid =
'jobs'::regclass;`) before deploying anything that depends on this
UPSERT -- connecting to production to check this is explicitly out of
scope for every phase of this work.

**No triggers or rules were found on `jobs`** in any tracked
schema-management script (`grep -r "CREATE TRIGGER\|CREATE RULE"` across
the whole repository returns nothing) -- there is currently nothing that
could suppress or alter the `RETURNING` clause's behavior. If a trigger
is ever added to `jobs` in the future, its interaction with this
`RETURNING (xmax = 0)` clause specifically should be re-verified.

**CI job structure and readiness, corrected in a further adversarial
pass:**

- `postgres-upsert-integration` is a direct sibling of `verify` under
  `jobs:` in `.github/workflows/ci.yml` (verified by parsing the
  workflow YAML and asserting the job key set, not merely by eye) -- it
  was never actually nested inside `verify` in the version that shipped
  from the prior pass, but this is now asserted structurally rather than
  claimed from visual inspection alone, and re-verified every time this
  section is touched.
- The job's own `services.postgres` health check (`pg_isready` run
  *inside* the `postgres:16-alpine` service container via `docker exec`,
  which GitHub Actions performs automatically before any step runs) does
  not need anything installed on the runner -- that image ships
  `pg_isready` itself. The separate readiness gate that previously ran
  directly on the RUNNER (`pg_isready -h localhost ...`) is a different
  concern: `pg_isready` is part of the `postgresql-client` package, which
  the job's "Install system packages" step did NOT install (only
  `build-essential` and `libpq-dev`, the latter being C headers for
  compiling against libpq, not the client binaries). That readiness loop
  is now REPLACED with a bounded `psycopg2` connection retry (30 attempts
  x 1s, using the exact same driver and DSN the integration test itself
  uses), which additionally proves `JOBPULSE_TEST_POSTGRES_DSN` is set
  and `psycopg2` importable before ever attempting a connection.
  `postgresql-client` is still installed too, for operator debugging
  (`psql` availability) even though the readiness gate no longer depends
  on it.
- **The old loop could silently "succeed" without PostgreSQL ever being
  ready**: `for i in $(seq 1 30); do pg_isready ... && break; sleep 1;
  done` has no `set -e` and no post-loop check -- if `pg_isready` were
  simply missing (as it was), every iteration's `&&` short-circuits
  silently, the loop exhausts all 30 iterations, and the `for` statement
  itself still exits 0, so the STEP was reported successful regardless of
  whether the database was ever reachable. The replacement always
  `sys.exit(1)`s after exhausting its bounded attempts.
- A final step now runs the integration test with `--junit-xml` and
  parses the resulting report to assert `skipped == 0` and `tests > 0`,
  failing the job if the file was unexpectedly skipped -- this is what
  actually proves the test ran for real in CI, since pytest's own exit
  code is 0 whether tests executed or were entirely skipped.

**Conservative counters, not invented ones.** The original
`skipped_duplicate_count` was declared, printed, and never incremented
anywhere -- this schema does not resurrect an equivalent placeholder
field, and there is still no separate "duplicates" counter: a
re-collected, unchanged row and a row whose fields genuinely changed are
both `rows_updated_existing` (the UPSERT's `DO UPDATE` branch does not
distinguish a no-op update from a real one, and this phase does not
change that SQL further to add that distinction).

**Outcome rules** (`determine_outcome()`, pure and unit-tested in
`tests/test_collector_outcomes.py`):

| Condition | Outcome |
|---|---|
| any `persistence_errors > 0` | `failed_persist` |
| `jobs_discovered == 0` | `success_no_results` |
| discovered but `rows_inserted == 0 and rows_updated_existing == 0` (every row filtered, at the outer valid/source check or inside `insert_job()`) | `success_filtered_all` |
| `rows_inserted > 0` (at least one PROVEN insert) | `success_with_new_rows` |
| `rows_inserted == 0` and `rows_updated_existing > 0` | `success_no_new_rows` |
| provider raised before any DB work | `failed_fetch` |
| unexpected internal error (e.g. provider construction) | `failed_internal` |

`insert_job()` returns one of four explicit `ROW_OUTCOME_*` values on
**every** path -- including both early returns -- never `None`. The
caller (`collect_jobs_to_postgres()`) increments its counters based only
on that return value, never unconditionally after merely calling
`insert_job()` (the original phase 2A defect).

`sanitize_error_text()` bounds every persisted error to 500 characters
and redacts `password=...` / `user:pass@host` DSN fragments before
anything is written to the result file -- no credential, connection
string, or raw provider payload is ever persisted.

### Strict result validation (`read_result()`)

An earlier revision of `read_result()` only checked that required fields
were *present* -- not their types, ranges, or whether `outcome` and the
counters were even mutually consistent. A malformed or self-contradictory
document (e.g. `"outcome": "success_with_new_rows"` with
`"rows_inserted": 0`, or `"jobs_discovered": "5"` as a string) could pass
through unnoticed. `read_result()` now validates, and rejects with a
descriptive (never value-echoing) `ResultReadError`:

- top-level JSON is an object;
- `schema_version` is exactly the supported integer;
- `provider` is a non-empty string, bounded to 200 characters;
- `started_at` / `finished_at` are valid, **timezone-aware** ISO-8601
  strings (a naive timestamp, a bare date, or a non-ISO string are all
  rejected);
- `duration_seconds` is a real number, finite (`NaN`/`Infinity` -- both
  of which Python's own `json.loads` will parse from the literal tokens
  `NaN`/`Infinity` without raising -- are rejected), non-negative, and
  bounded to 7 days;
- every counter is a real `int` (a numeric string like `"5"`, or a
  `bool` -- a subtype of `int` in Python -- are both rejected), is
  non-negative, and is bounded to 1,000,000;
- `outcome` is one of the seven supported values;
- `error_category` / `sanitized_error` are `null` or bounded strings
  (max 2000 chars);
- **cross-field invariants**, not just per-field types: a `success_*`
  outcome cannot carry `persistence_errors > 0`; `success_no_results`
  requires `jobs_discovered == 0`; `success_filtered_all` requires
  `jobs_discovered > 0` and zero proven writes; `success_with_new_rows`
  requires `rows_inserted > 0`; a `failed_*` outcome requires a non-null
  `error_category`; and the filter/write counters must exactly partition
  `jobs_discovered`/`jobs_valid` whenever `persistence_errors == 0` (an
  inexact partition is only legitimate when a persistence error is known
  to have aborted the loop partway through).

**Three distinct read-failure categories**, not two: `missing_result`
(the file does not exist), `result_read_error` (the file exists but
couldn't be read -- permission denied, is a directory, I/O error: an
infrastructure problem, not evidence of a malformed collector output),
and `invalid_result` (read successfully, but the content is malformed or
self-contradictory). An earlier revision collapsed the middle category
into `missing_result`, which would have misdirected an operator
investigating a permissions problem toward "the collector never ran"
instead of "a file exists that this process can't read."

### Subprocess result propagation (`scripts/linkedin_plan_collect.py`)

For every collector subprocess invocation, `linkedin_plan_collect.py`:

1. creates a unique temp path and sets `JOBPULSE_COLLECTOR_RESULT_PATH`;
2. runs the collector;
3. reads and validates the result via `classify_query_result()`;
4. deletes the temp file (always, even if the subprocess crashed before
   writing anything);
5. attaches the structured classification to the existing timestamped
   `logs/linkedin_plan_collect_<timestamp>.json` report, AND writes a
   small, versioned batch-result document to
   `JOBPULSE_PLAN_COLLECT_RESULT_PATH` (if set) for the next stage
   (`process_search_demand_queue.py`) to consume -- see "Cycle-level
   classification" below.

Required interpretation (never inferred from returncode alone):

| Condition | Classification |
|---|---|
| non-zero subprocess exit | query failure |
| exit 0 + missing result file | failure, category `missing_result` |
| exit 0 + unreadable result file | failure, category `result_read_error` |
| exit 0 + malformed/self-contradictory result | failure, category `invalid_result` |
| exit 0 + result reports a `failed_*` outcome | failure (trusts the structured result over the contradictory exit code) |
| exit 0 + `success_no_results` / `success_filtered_all` / `success_no_new_rows` | technically successful, explicitly zero-yield |
| exit 0 + `success_with_new_rows` | useful-ingestion success |

Human-readable stdout/stderr is still captured for operator debugging
but is **never** parsed for success/failure -- only the result file (or
its documented absence/malformedness) decides.

### Batch-level truthfulness

`build_batch_report()` computes, per run of `linkedin_plan_collect.py`:
`total_queries`, `successful_queries`, `failed_queries`,
`useful_queries` (at least one proven insert), `zero_yield_queries`,
`skipped_queries` (cooldown cache hits), `partial_failure`
(`failed_queries > 0 and successful_queries > 0`), and
`aggregate_collector_metrics` (sum of every collector counter across all
queries that produced a result). A batch with at least one failed and
one successful query is always visibly `partial_failure: true` in the
JSON report -- never silently described as an unqualified success. This
report is written to BOTH the timestamped
`logs/linkedin_plan_collect_<timestamp>.json` file and (if the env var is
set) `JOBPULSE_PLAN_COLLECT_RESULT_PATH`, atomically, even for a
zero-matching-queries run (`total_queries == 0`) -- a downstream reader
must never see "file missing" for a legitimately empty plan.

**Known, documented, un-fixed gap (not guessed at, per explicit
instruction not to invent an unsafe mapping):**
`scripts/process_search_demand_queue.py` still marks its **entire**
fetched set of `job_search_demand_queue` rows `done` whenever
`linkedin_plan_collect` reports at least one successful query
(`successful_queries > 0`), even when `partial_failure` is true. There is
no explicit 1:1 query-to-demand-queue-row mapping available to
`process_search_demand_queue.py` today, so this phase deliberately does
**not** attempt a guessed mapping -- it only makes `partial_failure`
truthfully VISIBLE (in the process summary and the final heartbeat's
`final_outcome`), it does not change which rows get marked `done`.
**This remains an explicit phase 2B blocker.**

### Cycle-level classification (`scripts/process_summary.py`)

An earlier revision of this phase always wrote
`final_outcome: "technical_success"` to the heartbeat's terminal `finish`
call and never updated `last_useful_ingestion_at` from real evidence --
i.e., cycle-level useful-ingestion monitoring was claimed in the design
but not actually implemented. **This is now completed, not deferred**,
because the cross-container transport it requires is provable directly
from this repository's own configuration: `docker-compose.prod.yml`
bind-mounts `./logs:/app/logs` on the `api` service, so a file the
container writes under `/app/logs/...` is the SAME file the host-side
wrapper sees under `$JOBPULSE_COLLECTION_ROOT/logs/...` (this is also the
existing, pre-phase-2A convention `app/admin_status.py`'s
`get_collection_heartbeat()`/`get_collection_performance()` already rely
on for `/app/logs/collection_heartbeat.json` and
`/app/logs/collection_history.jsonl`).

Flow:

1. `scripts/process_search_demand_queue.py`, running INSIDE the `api`
   container, reads the batch report `linkedin_plan_collect.py` wrote
   (via `JOBPULSE_PLAN_COLLECT_RESULT_PATH`, a path local to that
   container invocation -- not shared with the host), builds a
   `ProcessSummary` via `scripts.process_summary.build_summary()`, and
   writes it -- atomically, versioned -- to
   `JOBPULSE_PROCESS_SUMMARY_RESULT_PATH`, which the wrapper points at
   `/app/logs/.process_summary_<run_id>.json` inside the container.
2. `scripts/run_collection_cycle_safe.sh`, running on the HOST, passes
   that same env var (`docker compose exec -T -e
   JOBPULSE_PROCESS_SUMMARY_RESULT_PATH=... api ...`) and, after the
   `docker compose exec` call itself exits 0, reads the identical file
   back from `$ROOT/logs/.process_summary_<run_id>.json` on the host
   side, strictly validates it (`scripts.process_summary.read_summary()`,
   same three-category failure split as `collector_result.py`), and uses
   it -- never the bare exit code -- to decide the final classification.
3. **A missing or malformed summary after an otherwise-successful `docker
   compose exec` is itself treated as a process-step failure** (exit 5,
   `stage=process_summary`, `error_category` one of `missing_result` /
   `result_read_error` / `invalid_result`) -- success is never inferred
   from the bare exit code at this boundary either.

`classify_cycle_outcome()` (pure, deterministic, unit-tested in
`tests/test_process_summary.py`):

| Condition | `final_outcome` |
|---|---|
| no pending demand-queue targets at all (nothing to search for) | `technical_success_no_results` |
| pending targets existed but zero queries succeeded | `failed` |
| at least one query failed and at least one succeeded | `partial_failure` |
| all succeeded, at least one proven insert anywhere in the batch | `useful_success` |
| all succeeded, no proven insert but at least one proven update | `technical_success_no_new_rows` |
| all succeeded, nothing discovered anywhere | `technical_success_no_results` |
| all succeeded, discovered but all filtered | `technical_success_filtered_all` |

**Corrected in a second adversarial pass -- an earlier revision of this
section was itself wrong.** That revision called `heartbeat finish
--status success` and `append_history "success"` UNCONDITIONALLY for
every structurally-valid process summary, including
`final_outcome=partial_failure` -- meaning a batch where some queries
failed and some succeeded was reported as a fully successful terminal
cycle: `heartbeat.status` and `last_status` said `"success"`,
`last_success_at` advanced, `history.status` said `"success"`, the
wrapper exited 0, `get_collection_performance()` counted it as a
success, and `build_alerts()` (which only ever reads `last_status`) had
no way to see the incident at all. This is fixed. The current, corrected
contract:

| Outcome | `heartbeat.status` / `last_status` | `last_success_at` | `history.status` | wrapper exit | `--useful-ingestion` |
|---|---|---|---|---|---|
| `useful_success` | `success` | advances | `success` | `0` | always passed |
| `technical_success_no_new_rows` / `technical_success_no_results` / `technical_success_filtered_all` | `success` | advances | `success` | `0` | never passed |
| `partial_failure` | **`failed`** | **does not advance** | **`failed`** | **`8`** | passed **only if** `rows_inserted > 0` anywhere in the aggregate |

Three points worth being explicit about:

1. **`--useful-ingestion` (and therefore `last_useful_ingestion_at`) is
   computed from `aggregate_collector_metrics.rows_inserted > 0`
   directly -- NOT from `outcome == "useful_success"`.** This is
   deliberate and was itself a bug caught by this pass's own new tests: a
   `partial_failure` batch can still have genuinely proven inserts from
   its *successful* queries, and `last_useful_ingestion_at` must still be
   allowed to advance for that real evidence -- without the cycle's
   `status`/`final_outcome`/history ever becoming "success" because of
   it. An earlier fix attempt gated this flag on the outcome string
   instead, which meant a partially-failed-but-partly-useful batch never
   advanced `last_useful_ingestion_at` at all.
2. **History uses `status="failed"`, not `status="partial_failure"`**,
   specifically so the EXISTING, unmodified
   `get_collection_performance()` reader (which only recognizes
   `"success"` for its `success_count`, treating everything else as
   non-success) correctly excludes a partial-failure cycle from
   `success_count` with zero changes to `app/admin_status.py`. The more
   precise `"partial_failure"` label lives in the heartbeat's
   `final_outcome` field for any richer reader.
3. **Exit code 8 is distinct from exit 5** (missing/malformed process
   summary). Exit 5 means "the summary couldn't be read/validated at
   all." Exit 8 means "the summary was read and is structurally and
   semantically VALID, and its own truthful classification is not a
   success." Different failure modes, different remediation, never
   conflated.

`build_alerts()` (unmodified, commit `83d7b00`) observes a
`partial_failure` cycle exactly the way it already observes any other
`last_status="failed"` heartbeat -- via its existing `collection_failed`
alert code -- with zero phase-2B changes required. This is verified
directly in `tests/test_collection_cycle_wrapper.py` by calling the real
`build_alerts()` function against a heartbeat produced by an actual
partial-failure wrapper run.

### Heartbeat (`scripts/collection_heartbeat.py`)

Same file location as before (`logs/collection_heartbeat.json`), schema:

```json
{
  "schema_version": 1,
  "run_id": "6c2f...",
  "owner_pid": 12345, "writer_pid": 12489,
  "status": "success", "stage": "cycle_finished",
  "message": "Collection cycle completed successfully (outcome=useful_success).",
  "started_at": "...", "updated_at": "...", "last_progress_at": "...", "finished_at": "...",
  "progress_seq": 8,
  "last_success_at": "...", "last_useful_ingestion_at": "...",
  "current_metrics": {"pending_before": 3, "running_before": 0, "pending_after": 3, "rows_inserted": 2, "...": "..."},
  "final_outcome": "useful_success",
  "error_category": null,

  "last_status": "success", "last_message": "Collection cycle completed successfully (outcome=useful_success)."
}
```

`last_status` / `last_message` are a **deliberate compatibility shim**:
`app/admin_status.py`'s `build_alerts()` (commit `83d7b00`, not modified
this phase) reads exactly those two field names to drive the existing
`collection_heartbeat_missing` / `collection_cycle_stuck` /
`collection_cron_stale` / `collection_failed` / `collection_aborted_auth`
alerts, and `updated_at` is unchanged from before. **Phase 2B should
retire this duplication** once `app/admin_status.py` is updated to read
the new field names natively.

**`owner_pid` vs `writer_pid` -- corrected from an earlier revision that
recorded only a single `pid` field.** That single field was
`os.getpid()` of the SHORT-LIVED `python3 -m scripts.collection_heartbeat`
helper process invoked once per state transition -- meaning it changed on
every single call and never represented "the process running this
cycle" at all. Now there are two, deliberately different, fields:

- `owner_pid`: the STABLE PID of `run_collection_cycle_safe.sh` itself
  (`$$`), supplied explicitly via `--owner-pid` on every single
  start/progress/finish/fail call for the run -- identical across all of
  them.
- `writer_pid`: `os.getpid()` of the short-lived CLI helper process
  handling THIS ONE call -- expected to be different almost every time,
  purely a diagnostic breadcrumb (e.g. to spot an unexpectedly
  long-running helper invocation in a process list at the moment of a
  hang).

**Neither PID is a liveness signal, in either direction.** The
collector subprocesses this cycle triggers run inside the `api` Docker
container while the wrapper and this module run on the host -- a PID
recorded here lives in a completely different PID namespace than any
in-container process, so it is not even meaningful to try to check
`owner_pid` against `ps` output inside the container. It is also not
used internally by this module for any correctness decision -- run
identity (below) is keyed on `run_id` alone, never PID. Canonical
liveness/progress signals in this phase remain `run_id`, `updated_at`,
`last_progress_at`, `progress_seq`, and `stage`.

**Run identity and stale-write protection:**

- One UUID `run_id` is generated by `run_collection_cycle_safe.sh` once
  per invocation, strictly AFTER the top-level cycle lock (below) is
  acquired -- a lock-busy invocation never generates a `run_id` and never
  touches heartbeat state at all.
- `start` unconditionally claims the heartbeat for its `run_id`, safe
  only because `start` is only ever called after the caller already
  holds the top-level cycle lock -- lock possession, not the heartbeat
  file's own prior contents, is the authoritative liveness signal. This
  is what lets a fresh run recover from a heartbeat left at
  `status: "running"` by a killed process.
- `progress` / `finish` / `fail` are refused (raise `StaleRunError`, CLI
  exits non-zero) if the heartbeat's current `run_id` belongs to a
  *different* run. An older/slower run can never overwrite a newer run's
  progress or terminal state. Under normal operation this should never
  trigger, since the cycle lock already prevents two runs from being
  active at once -- this is defense in depth.
- `progress_seq` increases by exactly 1 on every accepted write for the
  owning `run_id`, reset to 0 by `start`.
- `last_success_at` and `last_useful_ingestion_at` are carry-forward
  fields: preserved across writes (including across a brand new run's
  `start()`) unless the current write explicitly sets them. `current_metrics`,
  by contrast, is reset to empty by every fresh `start()` -- it describes
  the *current* run's progress, not history.

**Bounded lock-retry for transient contention.** Every heartbeat write
(via the CLI) now retries a LockBusyError a bounded number of times with
a bounded delay before giving up -- `JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_ATTEMPTS`
(default 5, range 1-100) and `JOBPULSE_COLLECTION_HEARTBEAT_LOCK_RETRY_DELAY_SECONDS`
(default 0.2s, range 0-10s). Malformed/empty/out-of-range values fall
back to the default silently (never crash, never echo the raw value).
The library function `update_heartbeat()` itself defaults to
`lock_retry_attempts=1` (fail-fast, no retry) for direct callers --
retry is opt-in, and only the CLI (the wrapper's actual code path)
supplies the higher default.

**Persistent heartbeat-write failure semantics, corrected from an
earlier revision that only logged a warning and continued:**

- `start` failure is fatal (exit 3) -- nothing else has happened yet.
- A `progress` write failure is now ALSO fatal: it stops the cycle
  **before the next collector stage runs** (exit 3), rather than
  continuing with an unrecorded gap in the run's progress history.
- `finish` (terminal success) failure is fatal (exit 3) -- **no cycle may
  exit 0 when its terminal heartbeat was not durably persisted.**
- Every `fail` write inside an already-failing branch (auth/seed/process/
  reconcile/queue-count) is best-effort: its own failure is logged but
  never replaces the branch's own already-decided exit code -- the
  original collector failure is never masked by a secondary heartbeat
  failure.

**Atomicity and locking:** one inter-process, non-blocking `flock` per
CLI invocation attempt (tied to a file descriptor, always released on
process exit including a crash), and one atomic tempfile+fsync+`os.replace`
write per call -- identical pattern to `scripts/send_telegram_alerts.py`'s
`save_state()`. Missing, empty, or malformed state always recovers to a
safe empty object rather than raising.

State/lock paths (env-overridable for tests, matching the phase 1
convention):

```text
/opt/jobpulse/logs/collection_heartbeat.json   (JOBPULSE_COLLECTION_HEARTBEAT_STATE_PATH)
/opt/jobpulse/logs/collection_heartbeat.lock   (JOBPULSE_COLLECTION_HEARTBEAT_LOCK_PATH)
```

### Durable collection-history persistence

`collection_history.jsonl` (append-only, one JSON object per line) keeps
the SAME field shape it always has --
`status`/`message`/`started_at`/`finished_at`/`duration_seconds`/
`pending_before`/`running_before`/`pending_after` -- because
`app/admin_status.py`'s `get_collection_performance()` (unmodified this
phase) already parses exactly those fields for `avg_drain_per_success`
and `avg_duration_minutes`. `tests/test_collection_cycle_wrapper.py`
includes a regression test that runs the REAL, unmodified parser against
a history file this wrapper produces.

Each append is protected by its own **bounded, non-blocking** retry loop
around `flock` (25 attempts, 0.2s apart -- roughly 5 seconds worst case),
fsync'd before the lock releases. An earlier revision used a plain
BLOCKING `flock` here, which meant a stuck concurrent holder of the
history lock could hang the entire wrapper indefinitely -- exactly the
kind of unbounded wait the rest of this phase eliminates everywhere else.
A history-write failure now returns a non-zero exit from the append
itself, and every call site in the wrapper checks it: a failure inside an
already-failing branch is logged without changing that branch's exit
code (same non-masking rule as heartbeat writes above), while a failure
on either of the two exit-0 paths (normal success, or the
`skipped_running` no-op) forces exit 3 instead -- a "success" that was
never durably recorded in history is not reported as a success.

### Top-level cycle lock and wrapper exit codes

`run_collection_cycle_safe.sh` acquires a non-blocking `flock` on
`state/run_collection_cycle.lock` (`JOBPULSE_COLLECTION_CYCLE_LOCK_PATH`)
**before** writing any heartbeat or invoking any collector operation. On
contention it logs `SAFE_CYCLE_ALREADY_RUNNING`, prints
`run_collection_cycle_already_running`, creates no `run_id`, touches no
heartbeat state, and exits **0** -- the other invocation is actively
doing the work; this is not a failure, matching the exact same
"lock-busy exit semantics" convention documented above for
`production_health_alert.sh` / `run_production_alert_checks.sh`.

**A failed cycle no longer exits 0 based solely on process status.**
Documented, deliberately small exit-code taxonomy:

| Exit code | Meaning |
|---|---|
| `0` | technical success (including a zero-yield or no-op/skipped-running cycle) |
| `2` | auth/preflight failure (including a timeout) |
| `3` | heartbeat or history persistence failure (nothing else went wrong) |
| `4` | seed step failure (including a timeout) |
| `5` | queue-processing step failure (including a timeout, OR a missing/malformed process summary after an otherwise-successful docker exec) |
| `6` | reconciliation step failure (including a timeout) |
| `7` | queue-count (pending/running) DB-query failure (including a timeout or malformed output) |
| `8` | the process summary was structurally and semantically VALID but its own classification is not a success outcome (e.g. `partial_failure`) -- distinct from `5`, which means the summary was missing, unreadable, or failed validation |

Alert delivery (`send_telegram_alerts.py`, invoked from the wrapper's own
failure/abort paths as before) is best-effort: its own exit code is
logged (`ALERT_SENDER_FAILED rc=... category=...`) but **never** allowed
to replace or hide the exit code the wrapper already determined for the
actual collector failure that triggered the alert attempt -- confirmed
even when the alert sender itself hangs (bounded, see below).

### Bounded external calls -- every Docker/PostgreSQL/alert-sender call

An earlier revision of this phase had NO timeout at all on any of the
`docker compose exec` calls (auth preflight, seed, process, reconcile),
the `queue_count`/`reset_recent_running` PostgreSQL queries, or the
telegram alert sender -- any one of them hanging would hang the entire
cycle, and (with the top-level cycle lock held for the duration) block
every subsequent scheduled invocation too. This is now fixed: every
external call in this script runs under GNU coreutils `timeout`, using
the exact same `run_with_timeout` pattern (and the exact same
`validate_duration_seconds` input-sanitization helper) already
established in `scripts/production_health_alert.sh`.

| Variable | Default | Bounds | Applies to |
|---|---|---|---|
| `JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS` | 1800s (30 min) | 1-21600s | auth preflight, seed, process, reconcile (`docker compose exec`) |
| `JOBPULSE_COLLECTION_DB_QUERY_TIMEOUT_SECONDS` | 30s | 1-300s | `queue_count`, `reset_recent_running` |
| `JOBPULSE_COLLECTION_ALERT_TIMEOUT_SECONDS` | 60s | 1-600s | `send_telegram_alerts.py` invocation |
| `JOBPULSE_COLLECTION_KILL_AFTER_SECONDS` | 10s | 1-60s | grace period before SIGKILL, shared by all of the above |

Every value is parsed by `validate_duration_seconds`: a value must be a
plain unsigned integer of at most 9 digits (avoids bash arithmetic
overflow) within the documented `[min,max]` range; anything malformed,
empty, zero, negative, internally spaced, or excessively large silently
falls back to the default -- the raw value is never logged. `timeout` is
invoked **without** `--foreground`, exactly like
`production_health_alert.sh`: without it, `timeout` puts the wrapped
command in its own new process group and signals the WHOLE group on
timeout, so any subprocess the command itself forks (as `docker`
sometimes does) is killed too -- no child or grandchild process of the
HOST-SIDE fake `docker` process may remain running after a timeout,
verified in `tests/test_collection_cycle_wrapper.py` via `pgrep` after
each hang test. **This is host-side proof only -- see "Dual host/
in-container deadline" immediately below for why it does not, by itself,
prove anything about a real container process.** A timeout is logged
with `category=timeout` (distinct from `category=non_zero_exit` for an
ordinary command failure) and this category is persisted into the
failing heartbeat's `error_category`. If GNU `timeout` isn't installed,
every bounded call fails immediately and clearly
(`category=timeout_utility_missing`) rather than running unbounded. The
top-level cycle lock is released in every case (tied to the file
descriptor, closed on any exit path), so the NEXT scheduled invocation
always runs the real cycle rather than reporting `already_running`
indefinitely -- verified directly in the test suite by re-acquiring the
lock immediately after a timed-out run and confirming a subsequent
invocation succeeds.

### Dual host/in-container deadline (`scripts/run_with_deadline.py`)

**The gap this closes:** every hang test above uses a FAKE local
`docker` executable standing in for the real Docker CLI. That proves the
wrapper correctly bounds and kills the *host-side* process it directly
spawns. It proves NOTHING about whether a real `docker compose exec`
call's actual in-container Python process stops when the host-side
`docker` CLI is killed by `timeout` -- `docker compose exec`'s behavior
on an abruptly-disconnected client is not something this repository
controls, and was never verified against a real container in this pass
(no real Docker was used, per this pass's own constraints).

The fix is a SECOND, independent deadline enforced entirely inside the
container: every one of the four collector invocations (`linkedin_auth_preflight`,
`seed_priority_coverage_queue`, `process_search_demand_queue`,
`reconcile_priority_coverage`) is wrapped as
`python -m scripts.run_with_deadline --seconds "$STEP_TIMEOUT_SECONDS"
--kill-after "$KILL_AFTER_SECONDS" -- python -m scripts.<module> ...`
inside the `docker compose exec` command. `run_with_deadline.py` starts
the target module in its own new process group (`start_new_session=True`,
i.e. `setsid()`), and on deadline sends `SIGTERM` to the WHOLE group,
waits up to `--kill-after` seconds, then `SIGKILL`s the whole group --
identical semantics to the host-side `timeout`, just running natively
inside the container's own process tree instead of depending on the
Docker client's disconnect behavior.

**External termination of the runner itself is also handled** (corrected
in a further adversarial pass -- an earlier revision had no signal
handling at all: killing `run_with_deadline.py` itself, e.g. via a
`docker compose exec` client disconnect, would abandon its
separately-sessioned child and grandchildren, which `start_new_session`
makes immune to any signal sent to the runner's own process group).
`SIGTERM`, `SIGINT`, and `SIGHUP` delivered to the runner are all
forwarded through the SAME central cleanup function used for a normal
deadline expiry -- the signal handler itself does no I/O or process
work (just records which signal arrived; async-signal-safe), and the
main polling loop notices the flag and performs the real
SIGTERM-then-grace-then-SIGKILL cleanup in ordinary control flow. An
unexpected exception after the child starts triggers the identical
cleanup path before the runner exits. The runner's own exit code is
`128 + signum` (the standard shell convention for "terminated by signal
N") -- `143` for SIGTERM, `130` for SIGINT, `129` for SIGHUP.

**This half of the design IS verified with a real, executed proof,
including real external signal delivery** -- `tests/test_run_with_deadline.py`
runs the real module as a real subprocess (no Docker, no mocking of the
deadline logic itself) and proves: a hung command is killed within its
deadline; a command that forks a real grandchild has that grandchild
killed too, verified against the EXACT recorded child/grandchild PIDs
(via a written pidfile + `os.kill(pid, 0)`), never a broad `pgrep -f`
pattern that could collide with an unrelated process; sending a real
`SIGTERM`/`SIGINT`/`SIGHUP` to the RUNNER process itself (not the
target) still results in both the target and its grandchild being
reaped, with the documented `128+signum` exit code; a normal command's
exit code and stdout pass through unchanged with no spurious signal
sent; invalid arguments (including `NaN`/`Infinity`/`-Infinity`, zero,
negative, and excessively large values) are rejected by a strict
`argparse` type validator using `math.isfinite()`, never silently
accepted the way Python's own `float()` would accept `"nan"`/`"inf"`
literals. What is **NOT** proven, and remains explicitly out of scope
(no real Docker was used anywhere in this repository's test suite): that
`docker compose exec` itself correctly delivers the host-side kill
signal's consequences to the container in a way that lets
`run_with_deadline.py` ever get a chance to run its own cleanup, or that
the container environment's PID 1 behaves as expected under this
pattern. **Until a real disposable-container integration test exists,
the in-container half of this design is structurally protected and
unit-tested at the process-group AND external-signal level with a real
subprocess, but NOT Docker-runtime integration-tested.**

`tests/test_collection_cycle_wrapper.py` proves the HOST-side half of
the wiring is correct by capturing the exact `docker compose exec`
argv the wrapper constructs at runtime (via a fake `docker` that records
its own command line to a marker file) and asserting it contains
`scripts.run_with_deadline`, `--seconds`, and `--kill-after` with the
configured value -- for all four collector steps.

### Host-versus-inner deadline ordering

**Corrected in a further adversarial pass** -- an earlier revision used
the EXACT SAME duration for both the host-side `run_with_timeout`
wrapping `docker compose exec` and the in-container
`run_with_deadline`'s own `--seconds`/`--kill-after`. Both deadlines
could then fire at essentially the same instant, giving the
in-container runner no head start to do its own cleanup before the
host-side `timeout` killed/disconnected the `docker` CLI out from under
it. The host-side value is now always STRICTLY LARGER, computed as:

```
HOST_STEP_TIMEOUT_SECONDS = STEP_TIMEOUT_SECONDS + KILL_AFTER_SECONDS + HOST_BACKSTOP_MARGIN_SECONDS
HOST_DB_QUERY_TIMEOUT_SECONDS = DB_QUERY_TIMEOUT_SECONDS + HOST_BACKSTOP_MARGIN_SECONDS
```

| Variable | Default | Bounds | Meaning |
|---|---|---|---|
| `JOBPULSE_COLLECTION_HOST_BACKSTOP_MARGIN_SECONDS` | 30s | 1-300s | fixed positive margin added on top of the inner deadline(s) for docker-exec/interpreter startup and IPC overhead, so the host backstop can never equal or invert the inner deadline |

This holds **by construction regardless of malformed input**: the
addition always operates on the already-validated (safely-defaulted if
malformed) `STEP_TIMEOUT_SECONDS`/`KILL_AFTER_SECONDS`/`DB_QUERY_TIMEOUT_SECONDS`/
`HOST_BACKSTOP_MARGIN_SECONDS` variables, never on raw environment
input directly, and the margin's own validated minimum (1) guarantees
`host > inner` even at every variable's smallest legal value.
`run_collection_cycle_safe.sh` logs the fully-computed configuration
once per cycle (`DEADLINE_CONFIG inner_step_timeout=... inner_kill_after=...
host_step_timeout=... db_query_timeout=... host_db_query_timeout=... margin=...`),
which is what `tests/test_collection_cycle_wrapper.py` parses to assert
the actual numeric relationship a real run used -- including with a
deliberately malformed `JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS`
override, proving the ordering cannot be inverted by bad configuration.

### Database statement_timeout -- a third, server-side deadline

Both `queue_count()` and `reset_recent_running()` prefix their SQL with
`SET statement_timeout = '<N>s';` (using `JOBPULSE_COLLECTION_DB_QUERY_TIMEOUT_SECONDS`)
in the same `psql -c` invocation as the actual query -- a THIRD,
independent deadline enforced by PostgreSQL itself, so a query that runs
long is aborted server-side regardless of whether the host-side `psql`
client is killed cleanly. The HOST-side `run_with_timeout` around that
same `docker compose exec ... psql ...` call uses
`HOST_DB_QUERY_TIMEOUT_SECONDS` (strictly larger, see above), so the
server-side deadline always gets the first opportunity to abort the
query on its own terms. **This ordering is NOT runtime-tested against a
real PostgreSQL server** -- it is asserted structurally (the generated
SQL and the computed host timeout value, both captured from a real run
of the wrapper against a fake `docker`) and will only be genuinely
confirmed once `tests/test_upsert_returning_integration.py` (or an
equivalent statement_timeout-specific integration test) actually passes
against real PostgreSQL 16 -- see "PostgreSQL 16 integration evidence"
above for that test's current local/CI status. `queue_count()`'s output
parsing reads only the LAST non-empty line of `psql`'s output (rather
than the whole captured blob) specifically to stay correct regardless of
whether `-tA` (tuples-only, unaligned) suppresses the `SET` command's
own status line -- this could not be verified against a real `psql`
binary in this environment either, so the parsing is deliberately
defensive rather than assumed. Verified in
`tests/test_collection_cycle_wrapper.py` by capturing every DB-bound
command's full argv and asserting `SET statement_timeout` is present,
with the correct configured value, for both `queue_count()` and
`reset_recent_running()`.

### Queue-count truthfulness -- failure is never treated as zero

An earlier revision's `queue_count()` piped `docker compose exec ... psql
... | tr -d '[:space:]'` and used the PIPELINE's exit status, which in
bash defaults to the LAST command's status (`tr`'s), not `docker`'s or
`psql`'s -- so a failed or hung docker/psql call, or one that printed an
error message instead of a count, would almost always still report
success, and the caller then treated a garbage/empty read as `${VAR:-0}`
-- silently "0 pending" / "0 running." Both defects are fixed:

- `queue_count()` no longer pipes into `tr`. It runs the bounded
  `docker compose exec` directly (via `run_with_timeout`, redirecting to
  a temp file so the real exit code is preserved), and separately
  trims **only leading/trailing** whitespace from the captured output --
  never internal whitespace. (An earlier revision of THIS FIX still used
  `tr -d '[:space:]'` for that trim step, which deletes whitespace
  *anywhere* in the string, so malformed output like `"1 5"` would
  silently become the valid-looking `"15"` instead of being rejected;
  the trim now uses the same leading/trailing-only parameter-expansion
  idiom as `validate_duration_seconds`.)
- The result is accepted ONLY if it matches `^[0-9]+$` exactly -- empty,
  whitespace-only, negative, non-numeric, or internally-spaced output are
  all rejected.
- On ANY failure (non-zero exit, timeout, or malformed output),
  `queue_count()` returns non-zero and sets a stable
  `QUEUE_COUNT_LAST_CATEGORY` (`non_zero_exit` / `timeout` /
  `timeout_utility_missing` / `malformed_output` / `temp_file_failed`) --
  and writes NOTHING to the caller's output variable. Every call site in
  the wrapper checks this and fails the cycle (exit 7) rather than ever
  falling back to `PENDING_COUNT=0` / `RUNNING_COUNT=0`.

### Progress checkpoints

`cycle_started` -> `auth_preflight_passed` -> `backlog_checked` ->
(`seeding` -> `seed_completed`, or `seed_skipped_backlog_high`) ->
`demand_queue_processing_started` -> `demand_queue_processing_completed`
(now carrying the real aggregate collector metrics from the validated
process summary) -> `reconciliation_started` -> `cycle_finished`. These
are checkpoints *between* subprocess invocations only -- they do **not**
detect a hang *inside* one long-running subprocess (e.g.
`process_search_demand_queue` taking hours, bounded only by
`JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS` as a whole). Periodic
in-process progress reporting from inside that subprocess is a later
enhancement, not attempted this phase.

### Distinguishing technical success from useful ingestion

- **Per-query** (inside one `collector_postgres.py` invocation): the
  `outcome` field is authoritative and precise, backed by per-statement
  RETURNING evidence.
- **Per-batch** (one `linkedin_plan_collect.py` run, i.e. one
  `process_search_demand_queue` invocation): `useful_queries` /
  `zero_yield_queries` / `partial_failure` in its JSON/transport report
  are authoritative and precise.
- **Per-cycle** (the whole `run_collection_cycle_safe.sh` invocation):
  now ALSO precise (see "Cycle-level classification" above) -- the
  wrapper's terminal heartbeat carries the real `final_outcome` from
  `scripts.process_summary`, and `last_useful_ingestion_at` is set only
  for `useful_success`.

### Phase 2B limitations (explicitly deferred, not fixed here)

- **`collection_cycles` and `/api/admin/status` / `/api/admin/jobs-health`
  are not wired to any of this phase's metrics.** `collection_cycles`
  remains written only by the still-orphaned `scripts/run_collection_cycle.py`
  (not on the production path) and remains unread by any admin endpoint.
  No new admin alert codes exist yet for zero-yield or stale-cycle
  detection specifically (`partial_failure` IS now visible to the
  existing `collection_failed` alert code via `last_status`, see "Cycle-level
  classification" above -- that part is no longer deferred).
- `process_search_demand_queue.py` still marks its entire fetched
  demand-queue batch `done` on a partially-failed `linkedin_plan_collect`
  run (see "Known, documented, un-fixed gap" above) -- an explicit,
  undecided design question requiring an exact query-to-row mapping this
  repository does not currently have, not a guessed fix.
- No periodic in-process progress reporting from inside a single
  long-running collector subprocess -- only checkpoints *between*
  subprocess invocations, so a hang inside `process_search_demand_queue`
  itself is only caught by the dual (host `timeout` + in-container
  `run_with_deadline.py`) `JOBPULSE_COLLECTION_STEP_TIMEOUT_SECONDS`
  bound, not detected earlier via missing incremental progress.
- `app/admin_status.py`'s `build_alerts()` still reads only the legacy
  `last_status`/`last_message` heartbeat fields -- the richer schema
  (`owner_pid`, `final_outcome`, per-outcome `error_category`, etc.) is
  persisted but not yet consumed by any alert logic beyond the
  already-existing `last_status`-based codes.
- **The `xmax = 0` insert/update distinction is real-PostgreSQL-16-tested
  only when `JOBPULSE_TEST_POSTGRES_DSN` is set** (locally, or via the
  `postgres-upsert-integration` CI job) -- no local PostgreSQL binaries
  were available in this development environment, so
  `tests/test_upsert_returning_integration.py` has not been executed
  locally; only the CI job's future runs constitute real evidence. **A
  discovered, pre-existing gap**: no tracked schema script creates a
  `UNIQUE` constraint on `jobs.job_url` (only a non-unique index) --
  production's real schema must be verified directly by an operator
  before this UPSERT can be trusted there; this repository cannot check
  that without connecting to production, which is out of scope.
- **The in-container half of the dual timeout design
  (`scripts/run_with_deadline.py`) is unit-tested as a real subprocess
  with real process groups, but not Docker-runtime integration-tested**
  -- no real `docker compose exec` was used to confirm the host-to-container
  signal-propagation story end to end.

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

