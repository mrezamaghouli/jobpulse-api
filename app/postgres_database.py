import psycopg2
from psycopg2.extras import RealDictCursor

from app.config import get_postgres_config


def get_postgres_connection():
    postgres_config = get_postgres_config()

    connection = psycopg2.connect(
        **postgres_config,
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