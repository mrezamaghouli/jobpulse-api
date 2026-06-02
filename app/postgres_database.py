import os

import psycopg2
from psycopg2.extras import RealDictCursor


def get_postgres_connection():
    connection = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        database=os.getenv("POSTGRES_DB", "jobpulse"),
        user=os.getenv("POSTGRES_USER", "jobpulse_user"),
        password=os.getenv("POSTGRES_PASSWORD", "jobpulse_password"),
        cursor_factory=RealDictCursor
    )

    return connection


def check_postgres_connection():
    try:
        connection = get_postgres_connection()
        cursor = connection.cursor()

        cursor.execute("SELECT 1 AS status;")
        result = cursor.fetchone()

        cursor.close()
        connection.close()

        return {
            "connected": True,
            "status": result["status"]
        }

    except Exception as error:
        return {
            "connected": False,
            "error": str(error)
        }