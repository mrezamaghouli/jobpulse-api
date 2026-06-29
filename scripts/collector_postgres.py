import re
from datetime import date

import psycopg2

from app.config import get_postgres_config
from scripts.providers.provider_factory import get_job_provider


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


def insert_job(cursor, job):
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
        return

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
        return

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
            is_active = TRUE;
        """,
        job,
    )


def collect_jobs_to_postgres():
    provider = get_job_provider()

    raw_jobs = provider.fetch_jobs()

    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    ensure_jobs_runtime_columns(cursor)

    inserted_or_updated_count = 0
    skipped_duplicate_count = 0
    skipped_non_linkedin_count = 0
    skipped_invalid_count = 0

    for raw_job in raw_jobs:
        normalized_job = normalize_job(raw_job)

        if not is_valid_job(normalized_job):
            skipped_invalid_count += 1
            continue

        if not is_linkedin_job(normalized_job):
            skipped_non_linkedin_count += 1
            continue

        insert_job(cursor, normalized_job)
        inserted_or_updated_count += 1

    connection.commit()

    cursor.close()
    connection.close()

    print("LinkedIn PostgreSQL collector finished successfully.")
    print(f"Provider: {provider.__class__.__name__}")
    print(f"Inserted or updated LinkedIn jobs: {inserted_or_updated_count}")
    print(f"Skipped duplicate jobs: {skipped_duplicate_count}")
    print(f"Skipped non-LinkedIn jobs: {skipped_non_linkedin_count}")
    print(f"Skipped invalid jobs: {skipped_invalid_count}")


if __name__ == "__main__":
    collect_jobs_to_postgres()
