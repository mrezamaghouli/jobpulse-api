import os
import re
import sys
import time
from datetime import date, datetime, timezone

import psycopg2

from app.config import get_postgres_config
from scripts.providers.provider_factory import get_job_provider
from scripts.collector_result import (
    CollectorResult,
    ERROR_CATEGORY_FETCH,
    ERROR_CATEGORY_INTERNAL,
    ERROR_CATEGORY_PERSIST,
    OUTCOME_FAILED_FETCH,
    OUTCOME_FAILED_INTERNAL,
    OUTCOME_FAILED_PERSIST,
    RESULT_PATH_ENV_VAR,
    ROW_OUTCOME_FILTERED_HEADER_ARTIFACT,
    ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER,
    ROW_OUTCOME_INSERTED,
    ROW_OUTCOME_UPDATED_EXISTING,
    SCHEMA_VERSION,
    SUCCESS_OUTCOMES,
    determine_outcome,
    sanitize_error_text,
    write_result_atomic,
)


def extract_linkedin_job_id_from_url(value):
    import re

    if not value:
        return None

    text = str(value)

    patterns = [
        r"/jobs/view/(?:[^/?#]+-)?(\d+)",
        r"currentJobId=(\d+)",
        r"jobId=(\d+)",
        r"jobs/view/(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    return None


def ensure_linkedin_job_id(job):
    if not isinstance(job, dict):
        return job

    current = job.get("linkedin_job_id") or job.get("job_id")
    if current:
        job["linkedin_job_id"] = str(current)
        return job

    for key in ("job_url", "url", "apply_url", "linkedin_url"):
        recovered = extract_linkedin_job_id_from_url(job.get(key))
        if recovered:
            job["linkedin_job_id"] = recovered
            return job

    return job



def ensure_jobs_runtime_columns(cursor):
    cursor.execute(
        """
        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS apply_type VARCHAR(50);

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS apply_url TEXT;

        ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS apply_label VARCHAR(255);

        CREATE INDEX IF NOT EXISTS idx_jobs_last_seen_at
        ON jobs(last_seen_at);

        CREATE INDEX IF NOT EXISTS idx_jobs_is_active
        ON jobs(is_active);

        CREATE INDEX IF NOT EXISTS idx_jobs_apply_type
        ON jobs(apply_type);
        """
    )


def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def extract_linkedin_job_id(job_url):
    if not job_url:
        return ""

    patterns = [
        r"/jobs/view/(\d+)",
        r"currentJobId=(\d+)",
        r"jobId=(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, job_url)

        if match:
            return match.group(1)

    return ""


def canonicalize_linkedin_job_url(job_url, linkedin_job_id):
    if linkedin_job_id:
        return f"https://www.linkedin.com/jobs/view/{linkedin_job_id}/"

    extracted_id = extract_linkedin_job_id(job_url)

    if extracted_id:
        return f"https://www.linkedin.com/jobs/view/{extracted_id}/"

    return job_url


def normalize_bool(value):
    if value is None:
        return False

    if isinstance(value, bool):
        return value

    value_as_text = str(value).strip().lower()

    if value_as_text in ["true", "1", "yes", "y", "remote"]:
        return True

    return False


def normalize_apply_fields(job):
    apply_type = job.get("apply_type") or "unknown"
    apply_url = job.get("apply_url") or None
    apply_label = job.get("apply_label") or None

    if apply_type == "easy_apply" and not apply_url:
        apply_url = job.get("job_url")

    job["apply_type"] = apply_type
    job["apply_url"] = apply_url
    job["apply_label"] = apply_label

    return job


def normalize_job(raw_job):
    raw_job = dict(raw_job)

    raw_job_url = raw_job.get("job_url") or ""
    raw_linkedin_job_id = raw_job.get("linkedin_job_id") or ""

    linkedin_job_id = raw_linkedin_job_id or extract_linkedin_job_id(raw_job_url)

    job_url = canonicalize_linkedin_job_url(
        job_url=raw_job_url,
        linkedin_job_id=linkedin_job_id,
    )

    title = clean_text(raw_job.get("title"))
    company = clean_text(raw_job.get("company")) or "Unknown Company"
    location = clean_text(raw_job.get("location")) or "Unknown Location"

    apply_type = raw_job.get("apply_type") or "unknown"
    apply_url = raw_job.get("apply_url") or None
    apply_label = raw_job.get("apply_label") or None

    if apply_type == "easy_apply" and not apply_url:
        apply_url = job_url

    normalized_job = {
        "linkedin_job_id": linkedin_job_id,
        "title": title,
        "company": company,
        "company_linkedin_url": raw_job.get("company_linkedin_url"),
        "company_logo_url": raw_job.get("company_logo_url"),
        "job_description": raw_job.get("job_description"),
        "job_about": raw_job.get("job_about"),
        "work_mode": raw_job.get("work_mode"),
        "date_posted_text": raw_job.get("date_posted_text"),
        "date_posted_at": raw_job.get("date_posted_at"),

        "location": location,
        "remote": normalize_bool(raw_job.get("remote")) or ("remote" in location.lower()),

        "job_type": raw_job.get("job_type"),
        "seniority": raw_job.get("seniority"),

        "salary_min": raw_job.get("salary_min"),
        "salary_max": raw_job.get("salary_max"),
        "currency": raw_job.get("currency"),

        "source": raw_job.get("source") or "LinkedIn",
        "job_url": job_url,

        "apply_type": apply_type,
        "apply_url": apply_url,
        "apply_label": apply_label,

        "poster_name": raw_job.get("poster_name"),
        "poster_title": raw_job.get("poster_title"),
        "poster_profile_url": raw_job.get("poster_profile_url"),

        "date_posted": raw_job.get("date_posted") or str(date.today()),
    }

    return normalize_apply_fields(normalized_job)


def is_valid_job(job):
    if not job.get("title"):
        return False

    if not job.get("job_url"):
        return False

    return True


def is_linkedin_job(job):
    source = str(job.get("source") or "").lower()
    job_url = str(job.get("job_url") or "").lower()

    return source == "linkedin" or "linkedin.com/jobs/view" in job_url



def is_invalid_linkedin_search_header_job(job):
    import re

    title = str(job.get("title") or "").strip()
    location = str(job.get("location") or "").strip()
    description = str(job.get("job_description") or job.get("job_about") or "").strip()

    if re.match(r"^\d+\s+.+\s+Jobs\s+in\s+.+$", title, re.IGNORECASE):
        return True

    if title.lower().endswith(" jobs in australia"):
        return True

    if title.lower().endswith(" jobs in united states"):
        return True

    if title.lower().endswith(" jobs in canada"):
        return True

    if title.lower().endswith(" jobs in germany"):
        return True

    if title.lower().endswith(" jobs in united kingdom"):
        return True

    if location.lower() == "unknown location" and not description:
        return True

    return False



def truncate_job_varchar_fields(job):
    if not isinstance(job, dict):
        return job

    limits = {
        "source": 100,
        "work_mode": 100,
        "seniority": 100,
        "job_type": 100,
        "apply_type": 100,
        "currency": 20,
    }

    for key, max_len in limits.items():
        value = job.get(key)
        if value is None:
            continue

        value = str(value).strip()
        if len(value) > max_len:
            print("Truncating long field:", key, "len=", len(value), "max=", max_len)
            value = value[:max_len]

        job[key] = value

    return job



def sanitize_linkedin_apply_fields(job):
    if not isinstance(job, dict):
        return job

    apply_type = str(job.get("apply_type") or "").strip().lower()
    apply_label = str(job.get("apply_label") or "").strip()
    apply_url = str(job.get("apply_url") or "").strip()

    label_clean = apply_label.lower().replace("\n", " ").strip()
    url_clean = apply_url.lower()

    label_is_exact_easy_apply = label_clean == "easy apply"

    has_external_apply_url = (
        apply_url.startswith("http")
        and "linkedin.com" not in url_clean
        and "lnkd.in" not in url_clean
    )

    bad_fragments = [
        "promoted",
        "applicant",
        "people clicked",
        "actively reviewing",
        "responses managed",
        "company review time",
        "be an early applicant",
        "over 100",
        "·",
    ]

    label_is_bad = False
    if apply_label:
        if len(apply_label) > 40:
            label_is_bad = True
        if any(fragment in label_clean for fragment in bad_fragments):
            label_is_bad = True

    # External apply is valid ONLY when we have a real non-LinkedIn apply_url.
    if has_external_apply_url:
        job["apply_type"] = "external"
        job["apply_label"] = "Apply"
        job["apply_url"] = apply_url
        return job

    # Easy Apply is valid ONLY when LinkedIn explicitly says exactly Easy Apply.
    if label_is_exact_easy_apply:
        job["apply_type"] = "easy_apply"
        job["apply_label"] = "Easy Apply"
        job["apply_url"] = None
        return job

    # Everything else is unknown. Do not invent apply state from polluted text.
    job["apply_type"] = "unknown"
    job["apply_label"] = None

    if not has_external_apply_url:
        job["apply_url"] = None

    return job


class UpsertEvidenceError(RuntimeError):
    """Raised when the RETURNING (xmax = 0) AS inserted clause on
    insert_job()'s own UPSERT did not produce conclusive boolean
    evidence: no row at all, a null value, or any non-boolean value.

    An inconclusive read is NEVER treated as "not inserted" / "updated
    existing" -- that would be reporting an unproven guess as proven
    evidence, the same category of defect this whole schema exists to
    eliminate. The caller (collect_jobs_to_postgres()) catches this like
    any other persistence-layer exception: the transaction is rolled
    back, the row is counted as a persistence_error, and the whole run
    is reported as failed_persist with a non-zero exit code -- it is
    never silently absorbed into a technical-success outcome.
    """


def _interpret_upsert_returning(row) -> str:
    """Strictly validates the single RETURNING row from insert_job()'s
    UPSERT statement. Only an ACTUAL Python bool (which is exactly what
    psycopg2 produces for a real PostgreSQL `boolean` RETURNING
    expression) is accepted -- not a truthy/falsy value of any other
    type (0/1, "true"/"false" strings, etc.), and not a missing row or a
    null value."""
    if row is None:
        raise UpsertEvidenceError("UPSERT RETURNING produced no row")

    value = row[0]

    if not isinstance(value, bool):
        raise UpsertEvidenceError(
            f"UPSERT RETURNING produced inconclusive evidence (expected bool, got {type(value).__name__})"
        )

    return ROW_OUTCOME_INSERTED if value else ROW_OUTCOME_UPDATED_EXISTING


def insert_job(cursor, job):
    """Attempts to upsert one normalized job row.

    Returns an explicit ROW_OUTCOME_* string on every path -- including
    every early return -- never None. The caller must count based on this
    return value, never on the mere fact that insert_job() was called.
    """
    job = sanitize_linkedin_apply_fields(job)
    job = truncate_job_varchar_fields(job)

    # LinkedIn jobs must have a stable linkedin_job_id.
    if is_invalid_linkedin_search_header_job(job):
        print(
            "Skipping invalid LinkedIn search header job:",
            job.get("title"),
            "|",
            job.get("company"),
        )
        # Distinct from ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER below: this
        # row is a LinkedIn search-results header artifact (e.g. "500+
        # Software Engineer Jobs in Germany"), not a real job listing that
        # merely lacks an identifier. Conflating the two previously hid how
        # much of a query's "filtered" output was junk-row noise versus a
        # real job LinkedIn simply didn't expose an ID for.
        return ROW_OUTCOME_FILTERED_HEADER_ARTIFACT

    # If provider returns an empty ID, try to recover it from job_url/apply_url.
    # If it still cannot be recovered, skip it to avoid unique constraint errors on "".
    import re as _re

    linkedin_job_id = str(job.get("linkedin_job_id") or "").strip()

    if not linkedin_job_id:
        for _url_key in ("job_url", "apply_url", "url", "linkedin_url"):
            _url = str(job.get(_url_key) or "")

            for _pattern in (
                r"/jobs/view/(?:[^/?#]+-)?(\d+)(?:[/?#]|$)",
                r"/jobs/view/(\d+)(?:[/?#]|$)",
                r"currentJobId=(\d+)",
                r"jobId=(\d+)",
            ):
                _match = _re.search(_pattern, _url)
                if _match:
                    linkedin_job_id = _match.group(1)
                    job["linkedin_job_id"] = linkedin_job_id
                    break

            if linkedin_job_id:
                break

    if not linkedin_job_id:
        print(
            "Skipping job without linkedin_job_id:",
            job.get("title"),
            "|",
            job.get("company"),
        )
        return ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER

    job["linkedin_job_id"] = linkedin_job_id

    job = normalize_apply_fields(job)

    cursor.execute(
        """
        INSERT INTO jobs (
            linkedin_job_id,
            title,
            company,
            company_linkedin_url,
            company_logo_url,
            job_description,
            job_about,
            work_mode,
            date_posted_text,
            date_posted_at,
            location,
            remote,
            job_type,
            seniority,
            salary_min,
            salary_max,
            currency,
            source,
            job_url,
            apply_type,
            apply_url,
            apply_label,
            poster_name,
            poster_title,
            poster_profile_url,
            date_posted,
            first_seen_at,
            last_seen_at,
            is_active
        )
        VALUES (
            %(linkedin_job_id)s,
            %(title)s,
            %(company)s,
            %(company_linkedin_url)s,
            %(company_logo_url)s,
            %(job_description)s,
            %(job_about)s,
            %(work_mode)s,
            %(date_posted_text)s,
            %(date_posted_at)s,
            %(location)s,
            %(remote)s,
            %(job_type)s,
            %(seniority)s,
            %(salary_min)s,
            %(salary_max)s,
            %(currency)s,
            %(source)s,
            %(job_url)s,
            %(apply_type)s,
            %(apply_url)s,
            %(apply_label)s,
            %(poster_name)s,
            %(poster_title)s,
            %(poster_profile_url)s,
            %(date_posted)s,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP,
            TRUE
        )
        ON CONFLICT (job_url) DO UPDATE SET
            linkedin_job_id = COALESCE(EXCLUDED.linkedin_job_id, jobs.linkedin_job_id),

            title = COALESCE(EXCLUDED.title, jobs.title),
            company = COALESCE(EXCLUDED.company, jobs.company),
            company_linkedin_url = COALESCE(EXCLUDED.company_linkedin_url, jobs.company_linkedin_url),

            company_logo_url = COALESCE(EXCLUDED.company_logo_url, jobs.company_logo_url),
            job_description = COALESCE(EXCLUDED.job_description, jobs.job_description),
            job_about = COALESCE(EXCLUDED.job_about, jobs.job_about),
            work_mode = COALESCE(EXCLUDED.work_mode, jobs.work_mode),
            date_posted_text = COALESCE(EXCLUDED.date_posted_text, jobs.date_posted_text),
            date_posted_at = COALESCE(EXCLUDED.date_posted_at, jobs.date_posted_at),

            location = COALESCE(EXCLUDED.location, jobs.location),
            remote = COALESCE(EXCLUDED.remote, jobs.remote),
            job_type = COALESCE(EXCLUDED.job_type, jobs.job_type),
            seniority = COALESCE(EXCLUDED.seniority, jobs.seniority),

            salary_min = COALESCE(EXCLUDED.salary_min, jobs.salary_min),
            salary_max = COALESCE(EXCLUDED.salary_max, jobs.salary_max),
            currency = COALESCE(EXCLUDED.currency, jobs.currency),

            source = COALESCE(EXCLUDED.source, jobs.source),

            apply_type = CASE
                WHEN EXCLUDED.apply_type IS NOT NULL
                AND EXCLUDED.apply_type != 'unknown'
                THEN EXCLUDED.apply_type
                ELSE jobs.apply_type
            END,

            apply_url = COALESCE(EXCLUDED.apply_url, jobs.apply_url),
            apply_label = COALESCE(EXCLUDED.apply_label, jobs.apply_label),

            poster_name = COALESCE(EXCLUDED.poster_name, jobs.poster_name),
            poster_title = COALESCE(EXCLUDED.poster_title, jobs.poster_title),
            poster_profile_url = COALESCE(EXCLUDED.poster_profile_url, jobs.poster_profile_url),

            date_posted = COALESCE(EXCLUDED.date_posted, jobs.date_posted),

            last_seen_at = CURRENT_TIMESTAMP,
            is_active = TRUE
        RETURNING (xmax = 0) AS inserted;
        """,
        job,
    )

    # `xmax = 0` is a PostgreSQL-SPECIFIC system-column technique for
    # distinguishing an INSERT ... ON CONFLICT DO UPDATE statement's two
    # branches from its own RETURNING clause -- it is NOT a SQL-standard
    # or officially-guaranteed feature; it is an accepted, widely-deployed
    # convention that depends on PostgreSQL's own MVCC implementation
    # details (a row's `xmax` system column is unset (0) for a tuple's
    # very first version, and is set to the current transaction's ID the
    # moment any UPDATE touches it). This is per-statement,
    # transaction-safe evidence from the exact SQL this collector
    # executed -- not an approximation, and not a pre-SELECT-then-INSERT
    # race (there is no separate SELECT: the single INSERT statement both
    # performs the write and proves which branch fired). Its exact
    # behavior against this schema, its constraints, and any triggers on
    # `jobs` is validated by a real PostgreSQL 16 integration test in
    # tests/test_upsert_returning_integration.py -- see
    # docs/PRODUCTION_RUNBOOK.md for whether that test has actually been
    # run in this environment (it requires either local PostgreSQL 16
    # binaries or a disposable test DSN; a mocked-cursor unit test alone
    # proves only that the SQL string is well-formed, never that
    # PostgreSQL 16 executes it as expected).
    row = cursor.fetchone()
    return _interpret_upsert_returning(row)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_result(started_at, started_monotonic, provider_name, counters, outcome,
                   error_category=None, sanitized_error=None) -> CollectorResult:
    return CollectorResult(
        schema_version=SCHEMA_VERSION,
        provider=provider_name,
        started_at=started_at,
        finished_at=_now_iso(),
        duration_seconds=round(time.monotonic() - started_monotonic, 2),
        jobs_discovered=counters["jobs_discovered"],
        jobs_valid=counters["jobs_valid"],
        jobs_filtered_invalid=counters["jobs_filtered_invalid"],
        jobs_filtered_non_linkedin=counters["jobs_filtered_non_linkedin"],
        jobs_filtered_header_artifact=counters["jobs_filtered_header_artifact"],
        jobs_filtered_missing_identifier=counters["jobs_filtered_missing_identifier"],
        rows_inserted=counters["rows_inserted"],
        rows_updated_existing=counters["rows_updated_existing"],
        persistence_errors=counters["persistence_errors"],
        outcome=outcome,
        error_category=error_category,
        sanitized_error=sanitized_error,
    )


def _finalize(result: CollectorResult, result_path) -> int:
    """Writes the result document (if a path is configured) and prints one
    short prefixed summary line for operators. The summary line is for
    humans only -- no caller may parse it; the result file is the only
    machine-readable contract. Returns the process exit code: 0 for any
    success_* outcome, 1 for any failed_* outcome, and -- independent of
    the outcome above -- a failed result-file write always forces a
    non-zero return, since an unrecorded outcome cannot be trusted by any
    caller reading the result path."""
    print(f"COLLECTOR_RESULT outcome={result.outcome} "
          f"discovered={result.jobs_discovered} valid={result.jobs_valid} "
          f"inserted={result.rows_inserted} updated_existing={result.rows_updated_existing} "
          f"persistence_errors={result.persistence_errors}")

    exit_code = 0 if result.outcome in SUCCESS_OUTCOMES else 1

    if result_path:
        try:
            write_result_atomic(result_path, result)
        except Exception as exc:
            print(f"COLLECTOR_RESULT_WRITE_FAILED path={result_path} error={sanitize_error_text(exc)}", file=sys.stderr)
            return 1

    return exit_code


def collect_jobs_to_postgres() -> int:
    """Runs one collection pass and returns a process exit code (0/1).

    Every input row produces an explicit, provable outcome -- nothing is
    ever counted as inserted/updated unless the corresponding SQL
    statement's own RETURNING evidence proves it (see insert_job()).
    There is deliberately no whole-table COUNT(*) before/after delta
    anywhere in this function: a global count cannot distinguish this
    collector's own writes from a concurrent process's inserts/deletes
    happening in the same window, so it is never used as insert proof.
    """
    started_at = _now_iso()
    started_monotonic = time.monotonic()
    result_path = os.getenv(RESULT_PATH_ENV_VAR)

    counters = {
        "jobs_discovered": 0,
        "jobs_valid": 0,
        "jobs_filtered_invalid": 0,
        "jobs_filtered_non_linkedin": 0,
        "jobs_filtered_header_artifact": 0,
        "jobs_filtered_missing_identifier": 0,
        "rows_inserted": 0,
        "rows_updated_existing": 0,
        "persistence_errors": 0,
    }

    try:
        provider = get_job_provider()
        provider_name = provider.__class__.__name__
    except Exception as exc:
        result = _build_result(
            started_at, started_monotonic, "unknown", counters,
            OUTCOME_FAILED_INTERNAL, ERROR_CATEGORY_INTERNAL, sanitize_error_text(exc),
        )
        return _finalize(result, result_path)

    try:
        raw_jobs = provider.fetch_jobs()
    except Exception as exc:
        result = _build_result(
            started_at, started_monotonic, provider_name, counters,
            OUTCOME_FAILED_FETCH, ERROR_CATEGORY_FETCH, sanitize_error_text(exc),
        )
        return _finalize(result, result_path)

    counters["jobs_discovered"] = len(raw_jobs)

    connection = None
    try:
        connection = psycopg2.connect(**get_postgres_config())
        cursor = connection.cursor()

        ensure_jobs_runtime_columns(cursor)

        for raw_job in raw_jobs:
            normalized_job = normalize_job(raw_job)

            if not is_valid_job(normalized_job):
                counters["jobs_filtered_invalid"] += 1
                continue

            if not is_linkedin_job(normalized_job):
                counters["jobs_filtered_non_linkedin"] += 1
                continue

            counters["jobs_valid"] += 1

            row_outcome = insert_job(cursor, normalized_job)

            if row_outcome == ROW_OUTCOME_INSERTED:
                counters["rows_inserted"] += 1
            elif row_outcome == ROW_OUTCOME_UPDATED_EXISTING:
                counters["rows_updated_existing"] += 1
            elif row_outcome == ROW_OUTCOME_FILTERED_HEADER_ARTIFACT:
                counters["jobs_filtered_header_artifact"] += 1
            elif row_outcome == ROW_OUTCOME_FILTERED_MISSING_IDENTIFIER:
                counters["jobs_filtered_missing_identifier"] += 1
            else:
                # Defensive: insert_job() must always return a known
                # ROW_OUTCOME_* value. An unrecognized outcome is treated
                # as neither a write nor a filter -- it is simply not
                # counted as anything provable, rather than guessed.
                pass

        connection.commit()
        cursor.close()
        connection.close()

    except Exception as exc:
        counters["persistence_errors"] += 1

        if connection is not None:
            try:
                connection.rollback()
                connection.close()
            except Exception:
                pass

        result = _build_result(
            started_at, started_monotonic, provider_name, counters,
            OUTCOME_FAILED_PERSIST, ERROR_CATEGORY_PERSIST, sanitize_error_text(exc),
        )
        return _finalize(result, result_path)

    outcome = determine_outcome(
        jobs_discovered=counters["jobs_discovered"],
        rows_inserted=counters["rows_inserted"],
        rows_updated_existing=counters["rows_updated_existing"],
        persistence_errors=counters["persistence_errors"],
    )

    result = _build_result(started_at, started_monotonic, provider_name, counters, outcome)
    return _finalize(result, result_path)


if __name__ == "__main__":
    sys.exit(collect_jobs_to_postgres())
