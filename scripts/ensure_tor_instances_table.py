import psycopg2

from app.config import get_postgres_config
from scripts.tor.circuit_manager import ensure_tor_instances_table


def main():
    connection = psycopg2.connect(**get_postgres_config())
    ensure_tor_instances_table(connection)
    connection.close()

    print("tor_instances table is ready.")


if __name__ == "__main__":
    main()
