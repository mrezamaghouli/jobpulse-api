import psycopg2

from app.config import get_postgres_config


def sync_job_company_logos():
    connection = psycopg2.connect(**get_postgres_config())
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE jobs j
        SET company_logo_url = c.logo_url
        FROM companies c
        WHERE j.company_id = c.id
          AND c.logo_url IS NOT NULL
          AND c.logo_url != ''
          AND (
            j.company_logo_url IS NULL
            OR j.company_logo_url = ''
          );
        """
    )

    jobs_updated_from_companies = cursor.rowcount

    cursor.execute(
        """
        UPDATE companies c
        SET logo_url = j.company_logo_url,
            updated_at = CURRENT_TIMESTAMP
        FROM jobs j
        WHERE j.company_id = c.id
          AND j.company_logo_url IS NOT NULL
          AND j.company_logo_url != ''
          AND (
            c.logo_url IS NULL
            OR c.logo_url = ''
          );
        """
    )

    companies_updated_from_jobs = cursor.rowcount

    connection.commit()

    cursor.close()
    connection.close()

    print("Logo sync finished.")
    print(f"Jobs updated from companies: {jobs_updated_from_companies}")
    print(f"Companies updated from jobs: {companies_updated_from_jobs}")


if __name__ == "__main__":
    sync_job_company_logos()