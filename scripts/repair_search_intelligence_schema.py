import psycopg2

from app.config import get_postgres_config
from app.search_intelligence import ensure_search_intelligence_schema


def main():
    connection = psycopg2.connect(**get_postgres_config())

    try:
        with connection.cursor() as cursor:
            ensure_search_intelligence_schema(cursor)

        connection.commit()
        print("Search intelligence schema is ready.")

    finally:
        connection.close()


if __name__ == "__main__":
    main()