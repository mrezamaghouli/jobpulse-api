import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    linkedin_company_url TEXT UNIQUE,
    name TEXT,
    logo_url TEXT,
    website_url TEXT,
    industry TEXT,
    company_size TEXT,
    headquarters TEXT,
    about TEXT,
    last_enriched_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id);

ALTER TABLE jobs
ADD COLUMN IF NOT EXISTS company_logo_url TEXT;

CREATE INDEX IF NOT EXISTS idx_companies_linkedin_company_url
ON companies(linkedin_company_url);

CREATE INDEX IF NOT EXISTS idx_jobs_company_id
ON jobs(company_id);
"""


def normalize_text(value):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    return value


def upsert_company(cursor, company_name, company_url, logo_url):
    company_name = normalize_text(company_name)
    company_url = normalize_text(company_url)
    logo_url = normalize_text(logo_url)

    if not company_url and not company_name:
        return None

    if company_url:
        cursor.execute(
            """
            INSERT INTO companies (
                linkedin_company_url,
                name,
                logo_url,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (linkedin_company_url) DO UPDATE SET
                name = COALESCE(EXCLUDED.name, companies.name),
                logo_url = COALESCE(EXCLUDED.logo_url, companies.logo_url),
                updated_at = CURRENT_TIMESTAMP
            RETURNING id;
            """,
            (
                company_url,
                company_name,
                logo_url,
            ),
        )

        row = cursor.fetchone()
        return row["id"] if row else None

    cursor.execute(
        """
        INSERT INTO companies (
            name,
            logo_url,
            updated_at
        )
        VALUES (
            %s,
            %s,
            CURRENT_TIMESTAMP
        )
        RETURNING id;
        """,
        (
            company_name,
            logo_url,
        ),
    )

    row = cursor.fetchone()
    return row["id"] if row else None


def backfill_companies_from_jobs():
    connection = psycopg2.connect(**get_postgres_config())

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(ENSURE_SQL)

            cursor.execute(
                """
                SELECT
                    id,
                    company,
                    company_linkedin_url,
                    company_logo_url
                FROM jobs
                WHERE source = 'LinkedIn'
                  AND company IS NOT NULL
                  AND company != '';
                """
            )

            jobs = cursor.fetchall()

            updated_jobs = 0
            companies_seen = set()

            for job in jobs:
                company_id = upsert_company(
                    cursor=cursor,
                    company_name=job.get("company"),
                    company_url=job.get("company_linkedin_url"),
                    logo_url=job.get("company_logo_url"),
                )

                if not company_id:
                    continue

                cursor.execute(
                    """
                    UPDATE jobs
                    SET company_id = %s
                    WHERE id = %s;
                    """,
                    (
                        company_id,
                        job["id"],
                    ),
                )

                updated_jobs += 1
                companies_seen.add(company_id)

            connection.commit()

        print("Company backfill finished successfully.")
        print(f"LinkedIn jobs scanned: {len(jobs)}")
        print(f"Jobs linked to companies: {updated_jobs}")
        print(f"Unique companies touched: {len(companies_seen)}")

    finally:
        connection.close()


if __name__ == "__main__":
    backfill_companies_from_jobs()