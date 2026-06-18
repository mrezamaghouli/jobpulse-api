import math
import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config
from app.search_intelligence import infer_job_family, normalize_search_query


def get_inventory_count(cursor, query, location=None, work_mode=None):
    clauses = [
        "deleted_at IS NULL",
        "source = 'LinkedIn'",
        """
        (
            title ILIKE %s
            OR job_description ILIKE %s
            OR job_about ILIKE %s
        )
        """
    ]

    params = [f"%{query}%", f"%{query}%", f"%{query}%"]

    if location:
        clauses.append("location ILIKE %s")
        params.append(f"%{location}%")

    if work_mode:
        clauses.append("work_mode = %s")
        params.append(work_mode)

    cursor.execute(
        f"""
        SELECT COUNT(*) AS inventory_count
        FROM jobs
        WHERE {" AND ".join(clauses)}
        """,
        params,
    )

    row = cursor.fetchone()

    if not row:
        return 0

    if isinstance(row, dict):
        return int(row.get("inventory_count") or row.get("count") or 0)

    return int(row[0] or 0)


def main():
    conn = psycopg2.connect(**get_postgres_config())

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    normalized_query,
                    MAX(raw_query) AS raw_query,
                    COUNT(*) AS demand_count,
                    SUM(CASE WHEN result_count = 0 THEN 1 ELSE 0 END) AS zero_count,
                    AVG(result_count) AS avg_result_count,
                    MAX(filters_json->>'location') AS location,
                    MAX(filters_json->>'work_mode') AS work_mode
                FROM job_search_events
                WHERE normalized_query IS NOT NULL
                  AND normalized_query != ''
                GROUP BY normalized_query
                ORDER BY demand_count DESC;
                """
            )

            rows = cur.fetchall()

            if not rows:
                print("No search events found yet.")
                return

            scored = []

            for row in rows:
                query = normalize_search_query(row["normalized_query"])
                raw_query = row["raw_query"] or query
                demand_count = int(row["demand_count"] or 0)
                zero_count = int(row["zero_count"] or 0)
                avg_result_count = float(row["avg_result_count"] or 0)
                location = row.get("location") or None
                work_mode = row.get("work_mode") or None

                inventory_count = get_inventory_count(
                    cur,
                    query=query,
                    location=location,
                    work_mode=work_mode,
                )

                demand_score = min(1.0, math.log1p(demand_count) / math.log1p(50))
                zero_result_score = min(1.0, zero_count / max(demand_count, 1))
                inventory_score = 1.0 if inventory_count == 0 else max(0.0, 1.0 - min(inventory_count, 50) / 50)

                priority_score = (
                    demand_score * 0.50
                    + zero_result_score * 0.30
                    + inventory_score * 0.20
                )

                scored.append(
                    {
                        "target_query": raw_query,
                        "normalized_query": query,
                        "job_family": infer_job_family(query),
                        "location": location,
                        "work_mode": work_mode,
                        "demand_score": demand_score,
                        "inventory_score": inventory_score,
                        "zero_result_score": zero_result_score,
                        "priority_score": priority_score,
                    }
                )

            scored.sort(key=lambda item: item["priority_score"], reverse=True)

            total = len(scored)

            for index, item in enumerate(scored, start=1):
                percentile = round(1.0 - ((index - 1) / max(total, 1)), 4)

                cur.execute(
                    """
                    INSERT INTO linkedin_collection_targets (
                        target_query,
                        normalized_query,
                        job_family,
                        location,
                        work_mode,
                        demand_score,
                        inventory_score,
                        zero_result_score,
                        priority_score,
                        demand_percentile,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (normalized_query) DO UPDATE SET
                        target_query = EXCLUDED.target_query,
                        job_family = EXCLUDED.job_family,
                        location = EXCLUDED.location,
                        work_mode = EXCLUDED.work_mode,
                        demand_score = EXCLUDED.demand_score,
                        inventory_score = EXCLUDED.inventory_score,
                        zero_result_score = EXCLUDED.zero_result_score,
                        priority_score = EXCLUDED.priority_score,
                        demand_percentile = EXCLUDED.demand_percentile,
                        updated_at = CURRENT_TIMESTAMP,
                        status = 'active';
                    """,
                    (
                        item["target_query"],
                        item["normalized_query"],
                        item["job_family"],
                        item["location"],
                        item["work_mode"],
                        item["demand_score"],
                        item["inventory_score"],
                        item["zero_result_score"],
                        item["priority_score"],
                        percentile,
                    ),
                )

            conn.commit()

            print("LinkedIn collection targets rebuilt.")
            print("")
            print("Top targets:")

            for item in scored[:20]:
                print(
                    f"{item['priority_score']:.3f} | "
                    f"{item['job_family']} | "
                    f"{item['target_query']} | "
                    f"{item['location'] or 'Worldwide'} | "
                    f"{item['work_mode'] or 'any'}"
                )

    finally:
        conn.close()


if __name__ == "__main__":
    main()