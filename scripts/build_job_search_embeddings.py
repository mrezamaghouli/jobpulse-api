import hashlib
import json
import os

import psycopg2
from psycopg2.extras import RealDictCursor
from sentence_transformers import SentenceTransformer

from app.config import get_postgres_config


MODEL_NAME = os.getenv("JOB_SEARCH_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
BATCH_SIZE = int(os.getenv("JOB_SEARCH_EMBEDDING_BATCH_SIZE", "64"))
LIMIT = int(os.getenv("JOB_SEARCH_EMBEDDING_LIMIT", "0"))


def clean(value):
    return "" if value is None else str(value).strip()


def build_search_text(job):
    parts = [
        clean(job.get("title")),
        clean(job.get("company")),
        clean(job.get("location")),
        clean(job.get("work_mode")),
        clean(job.get("job_type")),
        clean(job.get("seniority")),
        clean(job.get("apply_type")),
        clean(job.get("job_description"))[:3000],
    ]
    return "\n".join([part for part in parts if part])


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def ensure_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS job_search_embeddings (
            job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            model_name TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            embedding_json JSONB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_job_search_embeddings_model
        ON job_search_embeddings(model_name);

        CREATE INDEX IF NOT EXISTS idx_job_search_embeddings_updated_at
        ON job_search_embeddings(updated_at);
        """
    )


def fetch_jobs_to_embed(cursor):
    limit_sql = ""
    params = [MODEL_NAME]

    if LIMIT > 0:
        limit_sql = "LIMIT %s"
        params.append(LIMIT)

    cursor.execute(
        f"""
        SELECT
            j.id,
            j.title,
            j.company,
            j.location,
            j.work_mode,
            j.job_type,
            j.seniority,
            j.apply_type,
            j.job_description
        FROM jobs j
        LEFT JOIN job_search_embeddings e
            ON e.job_id = j.id
           AND e.model_name = %s
        WHERE j.deleted_at IS NULL
          AND (
                e.job_id IS NULL
                OR e.text_hash IS NULL
          )
        ORDER BY j.last_seen_at DESC NULLS LAST, j.id DESC
        {limit_sql};
        """,
        params,
    )
    return cursor.fetchall()


def upsert_embeddings(cursor, rows):
    for row in rows:
        cursor.execute(
            """
            INSERT INTO job_search_embeddings (
                job_id,
                model_name,
                text_hash,
                embedding_json,
                updated_at
            )
            VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (job_id) DO UPDATE SET
                model_name = EXCLUDED.model_name,
                text_hash = EXCLUDED.text_hash,
                embedding_json = EXCLUDED.embedding_json,
                updated_at = CURRENT_TIMESTAMP;
            """,
            (
                row["job_id"],
                row["model_name"],
                row["text_hash"],
                json.dumps(row["embedding"]),
            ),
        )


def main():
    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    connection = psycopg2.connect(**get_postgres_config())

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            ensure_table(cursor)
            connection.commit()

            jobs = fetch_jobs_to_embed(cursor)
            print(f"Jobs to embed: {len(jobs)}")

            if not jobs:
                return

            embedded_count = 0

            for start in range(0, len(jobs), BATCH_SIZE):
                batch = jobs[start:start + BATCH_SIZE]
                texts = [build_search_text(job) for job in batch]
                hashes = [text_hash(text) for text in texts]

                vectors = model.encode(
                    texts,
                    batch_size=BATCH_SIZE,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                )

                rows = []

                for job, hash_value, vector in zip(batch, hashes, vectors):
                    rows.append(
                        {
                            "job_id": job["id"],
                            "model_name": MODEL_NAME,
                            "text_hash": hash_value,
                            "embedding": vector.astype(float).tolist(),
                        }
                    )

                upsert_embeddings(cursor, rows)
                connection.commit()

                embedded_count += len(rows)
                print(f"Embedded {embedded_count}/{len(jobs)} jobs")

        print("Job search embeddings built successfully.")

    finally:
        connection.close()


if __name__ == "__main__":
    main()