import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import hashlib
import psycopg2

from app.config import (
    get_linkedin_plan_collect_query_kill_after_seconds,
    get_linkedin_plan_collect_query_timeout_seconds,
    get_postgres_config,
)
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from scripts.collector_result import (
    ERROR_CATEGORY_INVALID_RESULT,
    ERROR_CATEGORY_MISSING_RESULT,
    ERROR_CATEGORY_RESULT_READ_ERROR,
    OUTCOME_SUCCESS_FILTERED_ALL,
    OUTCOME_SUCCESS_NO_NEW_ROWS,
    OUTCOME_SUCCESS_NO_RESULTS,
    OUTCOME_SUCCESS_WITH_NEW_ROWS,
    RESULT_PATH_ENV_VAR,
    ResultReadError,
    SUCCESS_OUTCOMES,
    read_result,
)

ZERO_YIELD_OUTCOMES = frozenset({
    OUTCOME_SUCCESS_NO_RESULTS,
    OUTCOME_SUCCESS_FILTERED_ALL,
    OUTCOME_SUCCESS_NO_NEW_ROWS,
})

COLLECTOR_COUNTER_FIELDS = (
    "jobs_discovered",
    "jobs_valid",
    "jobs_filtered_invalid",
    "jobs_filtered_non_linkedin",
    "jobs_filtered_header_artifact",
    "jobs_filtered_missing_identifier",
    "rows_inserted",
    "rows_updated_existing",
    "persistence_errors",
)


def classify_query_result(returncode: int, result_path: Path) -> dict:
    """Determines what actually happened for one collector subprocess
    invocation, per the required interpretation:

      - non-zero subprocess exit -> query failure
      - exit zero + missing result file -> failure, category missing_result
      - exit zero + malformed result -> failure, category invalid_result
      - exit zero + success_no_results / success_filtered_all /
        success_no_new_rows -> technically successful, zero-yield
      - exit zero + success_with_new_rows -> useful-ingestion success

    Success is never inferred from returncode alone: a returncode of 0
    paired with a missing/malformed/failed-outcome result is still a
    failure, and (defensively) a non-zero returncode is always a failure
    even if a result file happens to be present and look successful.
    """
    collector_result = None
    read_error_category = None

    try:
        collector_result = read_result(result_path)
    except ResultReadError as exc:
        read_error_category = exc.category
    except Exception:
        # read_result() itself catches every expected failure mode into
        # ResultReadError; reaching this branch means something truly
        # unanticipated happened while reading/validating -- that is
        # neither "the file is missing" nor necessarily "the file is
        # malformed JSON," so it gets its own distinct category rather
        # than being mislabeled as missing_result.
        read_error_category = ERROR_CATEGORY_RESULT_READ_ERROR

    if returncode != 0:
        category = "non_zero_exit"
        if collector_result is not None and collector_result.error_category:
            category = collector_result.error_category
        return {
            "query_status": "failed",
            "useful": False,
            "zero_yield": False,
            "failure_category": category,
            "collector_result": collector_result.to_dict() if collector_result else None,
        }

    if collector_result is None:
        return {
            "query_status": "failed",
            "useful": False,
            "zero_yield": False,
            "failure_category": read_error_category or ERROR_CATEGORY_MISSING_RESULT,
            "collector_result": None,
        }

    if collector_result.outcome not in SUCCESS_OUTCOMES:
        # Exit 0 but the collector's own structured result reports a
        # failed_* outcome -- trust the structured result over the
        # (contradictory) process exit code.
        return {
            "query_status": "failed",
            "useful": False,
            "zero_yield": False,
            "failure_category": collector_result.error_category or "unknown",
            "collector_result": collector_result.to_dict(),
        }

    return {
        "query_status": "success",
        "useful": collector_result.outcome == OUTCOME_SUCCESS_WITH_NEW_ROWS,
        "zero_yield": collector_result.outcome in ZERO_YIELD_OUTCOMES,
        "failure_category": None,
        "collector_result": collector_result.to_dict(),
    }


BASE_DIR = Path(__file__).resolve().parent.parent
QUERIES_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"
SEARCH_PLAN_FILE = BASE_DIR / "config" / "linkedin_search_plan.json"
LOG_DIR = BASE_DIR / "logs"
DEFAULT_QUERIES_FILE = BASE_DIR / "config" / "linkedin_expanded_queries.json"


def get_queries_file() -> Path:
    custom_file = os.getenv("LINKEDIN_QUERIES_FILE")

    if custom_file:
        path = Path(custom_file)

        if not path.is_absolute():
            path = BASE_DIR / path

        return path

    return DEFAULT_QUERIES_FILE



def load_json_file(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_default_workers() -> int:
    if SEARCH_PLAN_FILE.exists():
        search_plan = load_json_file(SEARCH_PLAN_FILE)

        try:
            workers = int(search_plan.get("parallel_workers", 2))
        except ValueError:
            workers = 2
    else:
        workers = 2

    if workers < 1:
        workers = 1

    if workers > 4:
        workers = 4

    return workers


def normalize_query(query: dict) -> dict:
    return {
        "category": query.get("category") or "unknown",
        "keywords": query.get("keywords") or "",
        "location": query.get("location") or "",
        "work_mode": query.get("work_mode") or "any",
        "lookback_days": int(query.get("lookback_days") or 60),
        "limit": int(query.get("limit") or 30),
        "max_pages": int(query.get("max_pages") or 1),
    }


def build_query_env(query: dict) -> dict:
    env = os.environ.copy()

    env["JOB_PROVIDER"] = "linkedin_browser"
    env["LINKEDIN_BROWSER"] = env.get("LINKEDIN_BROWSER", "chrome")

    env["LINKEDIN_KEYWORDS"] = query["keywords"]
    env["LINKEDIN_LOCATION"] = query["location"]
    env["LINKEDIN_LIMIT"] = str(query["limit"])

    env["LINKEDIN_WORK_MODE"] = query["work_mode"]
    env["LINKEDIN_LOOKBACK_DAYS"] = str(query["lookback_days"])
    env["LINKEDIN_MAX_PAGES"] = str(query["max_pages"])
    env["LINKEDIN_QUERY_RETRY_COUNT"] = env.get(
        "LINKEDIN_QUERY_RETRY_COUNT",
        "1"
    )
    env["LINKEDIN_QUERY_COOLDOWN_HOURS"] = env.get(
        "LINKEDIN_QUERY_COOLDOWN_HOURS",
        "12"
    )

    env["LINKEDIN_QUERY_RETRY_DELAY_SECONDS"] = env.get(
        "LINKEDIN_QUERY_RETRY_DELAY_SECONDS",
        "10"
    )
    env["LINKEDIN_SKIP_EXISTING_ENRICHED"] = env.get(
        "LINKEDIN_SKIP_EXISTING_ENRICHED",
        "true"
    )

    env["POSTGRES_HOST"] = env.get("POSTGRES_HOST", "localhost")
    env["POSTGRES_PORT"] = env.get("POSTGRES_PORT", "5432")
    env["POSTGRES_DB"] = env.get("POSTGRES_DB", "jobpulse")
    env["POSTGRES_USER"] = env.get("POSTGRES_USER", "jobpulse_user")
    env["POSTGRES_PASSWORD"] = env.get("POSTGRES_PASSWORD", "jobpulse_password")

    return env


def get_retry_count() -> int:
    try:
        retry_count = int(os.getenv("LINKEDIN_QUERY_RETRY_COUNT", "1"))
    except ValueError:
        retry_count = 1

    if retry_count < 0:
        retry_count = 0

    if retry_count > 3:
        retry_count = 3

    return retry_count


def get_retry_delay_seconds() -> int:
    try:
        delay_seconds = int(os.getenv("LINKEDIN_QUERY_RETRY_DELAY_SECONDS", "10"))
    except ValueError:
        delay_seconds = 10

    if delay_seconds < 0:
        delay_seconds = 0

    if delay_seconds > 120:
        delay_seconds = 120

    return delay_seconds

def make_query_signature(query: dict) -> str:
    raw_signature = "|".join(
        [
            str(query.get("category", "")).strip().lower(),
            str(query.get("keywords", "")).strip().lower(),
            str(query.get("location", "")).strip().lower(),
            str(query.get("work_mode", "")).strip().lower(),
            str(query.get("lookback_days", "")).strip().lower(),
        ]
    )

    return hashlib.sha256(raw_signature.encode("utf-8")).hexdigest()


def get_query_cooldown_hours() -> int:
    try:
        cooldown_hours = int(os.getenv("LINKEDIN_QUERY_COOLDOWN_HOURS", "12"))
    except ValueError:
        cooldown_hours = 12

    if cooldown_hours < 0:
        cooldown_hours = 0

    if cooldown_hours > 168:
        cooldown_hours = 168

    return cooldown_hours


def count_linkedin_jobs() -> int:
    try:
        connection = psycopg2.connect(**get_postgres_config())
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE source = 'LinkedIn';
            """
        )

        row = cursor.fetchone()

        cursor.close()
        connection.close()

        return int(row[0] or 0)

    except Exception as error:
        print(f"Could not count LinkedIn jobs: {error}")
        return 0


def was_query_recently_successful(query_signature: str, cooldown_hours: int) -> bool:
    if cooldown_hours <= 0:
        return False

    try:
        connection = psycopg2.connect(**get_postgres_config())
        cursor = connection.cursor()

        cursor.execute(
            """
            SELECT id
            FROM linkedin_query_runs
            WHERE query_signature = %s
              AND status = 'success'
              AND finished_at IS NOT NULL
              AND finished_at >= NOW() - (%s || ' hours')::INTERVAL
            ORDER BY finished_at DESC
            LIMIT 1;
            """,
            (
                query_signature,
                cooldown_hours,
            ),
        )

        row = cursor.fetchone()

        cursor.close()
        connection.close()

        return row is not None

    except Exception as error:
        print(f"Could not check query cooldown: {error}")
        return False


def insert_query_run_record(record: dict):
    try:
        connection = psycopg2.connect(**get_postgres_config())
        cursor = connection.cursor()

        cursor.execute(
            """
            INSERT INTO linkedin_query_runs (
                query_signature,
                category,
                keywords,
                location,
                work_mode,
                lookback_days,
                status,
                started_at,
                finished_at,
                duration_seconds,
                jobs_before,
                jobs_after,
                jobs_delta,
                failed_queries,
                profile_level,
                profile_name,
                error,
                log_file
            )
            VALUES (
                %(query_signature)s,
                %(category)s,
                %(keywords)s,
                %(location)s,
                %(work_mode)s,
                %(lookback_days)s,
                %(status)s,
                %(started_at)s,
                %(finished_at)s,
                %(duration_seconds)s,
                %(jobs_before)s,
                %(jobs_after)s,
                %(jobs_delta)s,
                %(failed_queries)s,
                %(profile_level)s,
                %(profile_name)s,
                %(error)s,
                %(log_file)s
            );
            """,
            record,
        )

        connection.commit()

        cursor.close()
        connection.close()

    except Exception as error:
        print(f"Could not insert query run record: {error}")


def run_single_query(query: dict, index: int, total: int) -> dict:
    retry_count = get_retry_count()
    retry_delay_seconds = get_retry_delay_seconds()
    
    query_signature = make_query_signature(query)
    cooldown_hours = get_query_cooldown_hours()

    jobs_before = count_linkedin_jobs()

    if was_query_recently_successful(
        query_signature=query_signature,
        cooldown_hours=cooldown_hours,
    ):
        started_at = datetime.now()
        finished_at = datetime.now()

        print("\n" + "=" * 90)
        print(
            f"Skipping recently successful query [{index}/{total}] "
            f"{query['category']} | {query['keywords']} | "
            f"{query['location'] or 'Worldwide'} | {query['work_mode']}"
        )
        print(f"Cooldown hours: {cooldown_hours}")
        print("=" * 90)

        skipped_result = {
            "index": index,
            "total": total,
            "success": True,
            "skipped": True,
            "returncode": 0,
            "category": query["category"],
            "keywords": query["keywords"],
            "location": query["location"],
            "work_mode": query["work_mode"],
            "lookback_days": query["lookback_days"],
            "limit": query["limit"],
            "duration_seconds": 0,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "attempts": [],
            "stdout": "",
            "stderr": "",
        }

        insert_query_run_record(
            {
                "query_signature": query_signature,
                "category": query.get("category"),
                "keywords": query.get("keywords"),
                "location": query.get("location"),
                "work_mode": query.get("work_mode"),
                "lookback_days": query.get("lookback_days"),
                "status": "skipped_recent_success",
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": 0,
                "jobs_before": jobs_before,
                "jobs_after": jobs_before,
                "jobs_delta": 0,
                "failed_queries": 0,
                "profile_level": None,
                "profile_name": None,
                "error": None,
                "log_file": None,
            }
        )

        return skipped_result

    attempts = []
    final_result = None

    for attempt_number in range(1, retry_count + 2):
        started_at = datetime.now()

        label = (
            f"[{index}/{total}] "
            f"{query['category']} | "
            f"{query['keywords']} | "
            f"{query['location'] or 'Worldwide'} | "
            f"{query['work_mode']} | "
            f"attempt {attempt_number}/{retry_count + 1}"
        )

        print("\n" + "=" * 90)
        print(f"Starting query {label}")
        print("=" * 90)

        env = build_query_env(query)

        result_fd, result_path_str = tempfile.mkstemp(prefix="collector_result_", suffix=".json")
        os.close(result_fd)
        os.unlink(result_path_str)  # collector must create it fresh via atomic write; a stale empty file must never be mistaken for its output
        result_path = Path(result_path_str)
        env[RESULT_PATH_ENV_VAR] = str(result_path)

        try:
            # Bounded by scripts.run_with_deadline (Phase 3.4K
            # Stabilization, Section 7): a wedged collector_postgres
            # subprocess is SIGTERM'd/SIGKILL'd on its own, per-query
            # deadline instead of being able to silently consume the
            # entire outer step budget (see
            # scripts/run_collection_cycle_safe.sh's STEP_TIMEOUT_SECONDS)
            # and abort every other query in this batch along with it. A
            # deadline expiry surfaces as a non-zero returncode, which
            # classify_query_result() below already treats as an ordinary
            # query failure -- no new failure contract needed.
            process = subprocess.run(
                [
                    sys.executable, "-m", "scripts.run_with_deadline",
                    "--seconds", str(get_linkedin_plan_collect_query_timeout_seconds()),
                    "--kill-after", str(get_linkedin_plan_collect_query_kill_after_seconds()),
                    "--",
                    sys.executable, "-m", "scripts.collector_postgres",
                ],
                cwd=str(BASE_DIR),
                env=env,
                text=True,
                capture_output=True,
            )

            # Human-readable stdout/stderr are kept in the report for
            # operator debugging only -- classification never parses them.
            # The structured result file is the only machine-readable
            # source of truth for what actually happened.
            classification = classify_query_result(process.returncode, result_path)
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass

        finished_at = datetime.now()
        duration_seconds = round((finished_at - started_at).total_seconds(), 2)

        success = classification["query_status"] == "success"

        attempt_result = {
            "attempt_number": attempt_number,
            "success": success,
            "returncode": process.returncode,
            "duration_seconds": duration_seconds,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "stdout": process.stdout,
            "stderr": process.stderr,
            "classification": classification,
        }

        attempts.append(attempt_result)

        final_result = {
            "index": index,
            "total": total,
            "success": success,
            "returncode": process.returncode,
            "category": query["category"],
            "keywords": query["keywords"],
            "location": query["location"],
            "work_mode": query["work_mode"],
            "lookback_days": query["lookback_days"],
            "limit": query["limit"],
            "duration_seconds": duration_seconds,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "attempts": attempts,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "classification": classification,
        }

        if success:
            print(f"Finished successfully: {label} in {duration_seconds}s")
            return final_result

        print(f"Failed: {label} in {duration_seconds}s")
        print(process.stderr[-1500:])

        if attempt_number <= retry_count:
            print(f"Retrying after {retry_delay_seconds}s...")
            time.sleep(retry_delay_seconds)
            
            jobs_after = count_linkedin_jobs()

            insert_query_run_record(
                {
                    "query_signature": query_signature,
                    "category": query.get("category"),
                    "keywords": query.get("keywords"),
                    "location": query.get("location"),
                    "work_mode": query.get("work_mode"),
                    "lookback_days": query.get("lookback_days"),
                    "status": "success",
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "duration_seconds": duration_seconds,
                    "jobs_before": jobs_before,
                    "jobs_after": jobs_after,
                    "jobs_delta": jobs_after - jobs_before,
                    "failed_queries": 0,
                    "profile_level": None,
                    "profile_name": None,
                    "error": None,
                    "log_file": None,
                }
            )
    jobs_after = count_linkedin_jobs()

    insert_query_run_record(
        {
            "query_signature": query_signature,
            "category": query.get("category"),
            "keywords": query.get("keywords"),
            "location": query.get("location"),
            "work_mode": query.get("work_mode"),
            "lookback_days": query.get("lookback_days"),
            "status": "failed",
            "started_at": final_result.get("started_at") if final_result else None,
            "finished_at": final_result.get("finished_at") if final_result else None,
            "duration_seconds": final_result.get("duration_seconds") if final_result else None,
            "jobs_before": jobs_before,
            "jobs_after": jobs_after,
            "jobs_delta": jobs_after - jobs_before,
            "failed_queries": 1,
            "profile_level": None,
            "profile_name": None,
            "error": (final_result.get("stderr") or "")[-2000:] if final_result else "unknown error",
            "log_file": None,
        }
    )

    return final_result


def build_batch_report(results: list[dict]) -> dict:
    """Truthful batch-level classification. A batch with at least one
    failed query AND at least one successful query is always visibly
    marked partial_failure=True -- it is never silently described as an
    unqualified success just because SOME queries went through."""
    total_queries = len(results)
    failed_queries = sum(1 for r in results if not r.get("success"))
    successful_queries = total_queries - failed_queries
    useful_queries = sum(1 for r in results if r.get("classification", {}).get("useful"))
    zero_yield_queries = sum(1 for r in results if r.get("classification", {}).get("zero_yield"))
    skipped_queries = sum(1 for r in results if r.get("skipped"))

    aggregate_collector_metrics = {field: 0 for field in COLLECTOR_COUNTER_FIELDS}
    for result in results:
        collector_result = (result.get("classification") or {}).get("collector_result")
        if not collector_result:
            continue
        for field in COLLECTOR_COUNTER_FIELDS:
            aggregate_collector_metrics[field] += collector_result.get(field) or 0

    return {
        "total_queries": total_queries,
        "successful_queries": successful_queries,
        "failed_queries": failed_queries,
        "useful_queries": useful_queries,
        "zero_yield_queries": zero_yield_queries,
        "skipped_queries": skipped_queries,
        "partial_failure": failed_queries > 0 and successful_queries > 0,
        "aggregate_collector_metrics": aggregate_collector_metrics,
    }


PLAN_COLLECT_RESULT_PATH_ENV_VAR = "JOBPULSE_PLAN_COLLECT_RESULT_PATH"
PLAN_COLLECT_RESULT_SCHEMA_VERSION = 1


def write_batch_result_atomic(batch_report: dict) -> None:
    """Writes a small, versioned, atomic summary of this run's batch
    report to JOBPULSE_PLAN_COLLECT_RESULT_PATH, if set -- this is the
    cross-process contract scripts/process_search_demand_queue.py reads
    to learn the truthful batch classification (including
    `partial_failure`) instead of inferring anything from this script's
    own exit code or stdout. A no-op if the env var isn't set (e.g. when
    this script is run standalone/manually).

    Same atomicity contract as scripts.collector_result.write_result_atomic:
    tempfile in the same directory, fsync, os.replace -- no partial file
    is ever exposed at the target path. Raises on failure; the caller
    (run_plan_collect) must not swallow that, since an unrecorded batch
    report cannot be trusted by process_search_demand_queue.py.
    """
    result_path = os.getenv(PLAN_COLLECT_RESULT_PATH_ENV_VAR)
    if not result_path:
        return

    path = Path(result_path)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)

    document = {
        "schema_version": PLAN_COLLECT_RESULT_SCHEMA_VERSION,
        "generated_at": datetime.now().isoformat(),
        "batch": batch_report,
    }

    fd, tmp_path = tempfile.mkstemp(prefix=".plan_collect_result.", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(document, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class BatchResultReadError(Exception):
    """Raised by read_batch_result() for a missing, unreadable, or
    malformed batch-result document. `category` mirrors
    scripts.collector_result.ResultReadError's three-way split
    (missing_result / result_read_error / invalid_result) for the same
    reason: an unreadable file is an infrastructure problem, not evidence
    this script produced something malformed."""

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


def read_batch_result(path: os.PathLike) -> dict:
    """Reads back the document written by write_batch_result_atomic() and
    returns its `batch` field (the same dict build_batch_report() built).
    Strictly validated -- never returns a value the caller could mistake
    for a real batch report if the file is missing or malformed."""
    path = Path(path)

    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BatchResultReadError(f"batch result file missing: {path}", ERROR_CATEGORY_MISSING_RESULT)
    except OSError as exc:
        raise BatchResultReadError(
            f"batch result file could not be read ({type(exc).__name__})", ERROR_CATEGORY_RESULT_READ_ERROR
        ) from exc

    try:
        raw = json.loads(raw_text)
    except Exception as exc:
        raise BatchResultReadError(f"batch result file is not valid JSON: {type(exc).__name__}", ERROR_CATEGORY_INVALID_RESULT) from exc

    if not isinstance(raw, dict) or raw.get("schema_version") != PLAN_COLLECT_RESULT_SCHEMA_VERSION:
        raise BatchResultReadError("batch result file has an unsupported shape/schema_version", ERROR_CATEGORY_INVALID_RESULT)

    batch = raw.get("batch")
    if not isinstance(batch, dict):
        raise BatchResultReadError("batch result file is missing its 'batch' object", ERROR_CATEGORY_INVALID_RESULT)

    required_keys = {
        "total_queries", "successful_queries", "failed_queries",
        "useful_queries", "zero_yield_queries", "skipped_queries",
        "partial_failure", "aggregate_collector_metrics",
    }
    missing_keys = required_keys - set(batch.keys())
    if missing_keys:
        raise BatchResultReadError(f"batch result file is missing required keys: {sorted(missing_keys)}", ERROR_CATEGORY_INVALID_RESULT)

    return batch


def write_run_log(results: list[dict], batch_report: dict) -> Path:
    LOG_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"linkedin_plan_collect_{timestamp}.json"

    report = {
        "generated_at": datetime.now().isoformat(),
        "batch": batch_report,
        "results": results,
    }

    with log_file.open("w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)

    print(f"\nRun log saved: {log_file}")
    return log_file


def run_plan_collect(
    workers: int,
    max_queries: int | None,
    offset: int,
    categories: list[str] | None,
):
 
    queries_file = get_queries_file()
    queries_data = load_json_file(queries_file)

    if isinstance(queries_data, list):
        queries = queries_data
    elif isinstance(queries_data, dict) and isinstance(queries_data.get("queries"), list):
        queries = queries_data["queries"]
    else:
        raise ValueError(f"Unsupported queries file format: {queries_file}")

    print(f"Loaded {len(queries)} queries from: {queries_file}")
    queries = [normalize_query(query) for query in queries]

    if categories:
        allowed_categories = set(categories)
        queries = [
            query
            for query in queries
            if query["category"] in allowed_categories
        ]

    if offset > 0:
        queries = queries[offset:]

    if max_queries:
        queries = queries[:max_queries]

    total = len(queries)

    if total == 0:
        print("No queries to run.")
        empty_batch_report = build_batch_report([])
        write_batch_result_atomic(empty_batch_report)
        return empty_batch_report

    print("LinkedIn parallel plan collector started.")
    print(f"Queries file: {QUERIES_FILE}")
    print(f"Total selected queries: {total}")
    print(f"Workers: {workers}")

    results = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_query = {
            executor.submit(run_single_query, query, index, total): query
            for index, query in enumerate(queries, start=1)
        }

        for future in as_completed(future_to_query):
            result = future.result()
            results.append(result)

    results = sorted(results, key=lambda item: item["index"])

    batch_report = build_batch_report(results)

    write_run_log(results, batch_report)
    write_batch_result_atomic(batch_report)

    print("\n" + "=" * 90)
    print("LinkedIn parallel plan collector finished.")
    print(f"Successful queries: {batch_report['successful_queries']}")
    print(f"Failed queries: {batch_report['failed_queries']}")
    print(f"Useful queries (new rows proven): {batch_report['useful_queries']}")
    print(f"Zero-yield queries: {batch_report['zero_yield_queries']}")
    if batch_report["partial_failure"]:
        print("BATCH_CLASSIFICATION: partial_failure "
              f"({batch_report['failed_queries']} failed, "
              f"{batch_report['successful_queries']} succeeded)")
    print("=" * 90)

    if batch_report["successful_queries"] == 0:
        raise SystemExit(1)

    return batch_report


def main():
    parser = argparse.ArgumentParser(
        description="Run LinkedIn expanded search queries in parallel."
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=get_default_workers(),
        help="Number of parallel query workers."
    )

    parser.add_argument(
        "--max-queries",
        type=int,
        default=None,
        help="Maximum number of queries to run."
    )

    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip this many queries from the beginning."
    )

    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="Run only one or more categories. Can be repeated."
    )

    args = parser.parse_args()

    workers = args.workers

    if workers < 1:
        workers = 1

    if workers > 4:
        workers = 4

    run_plan_collect(
        workers=workers,
        max_queries=args.max_queries,
        offset=args.offset,
        categories=args.category,
    )


if __name__ == "__main__":
    main()