"""Apply versioned PostgreSQL migrations and the restricted application role.

SQLite does not use this module. A missing database URL is a successful no-op
so the container entrypoint can call it on every start.
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "infra" / "db" / "migrations"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def apply_migrations(database_url, directory=MIGRATIONS):
    """Apply each unapplied SQL file in name order."""

    import psycopg

    files = sorted(Path(directory).glob("*.sql"))
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations(
              version TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.commit()
        applied = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        for path in files:
            if path.name in applied:
                continue
            statements = _statements(path.read_text())
            with connection.transaction():
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES(%s)",
                    (path.name,),
                )
    return [path.name for path in files]


def ensure_app_role(master_url):
    """Create or update the application role from ``FNOL_APP_SECRET_ARN``."""

    secret_id = os.environ.get("FNOL_APP_SECRET_ARN", "").strip()
    if not secret_id:
        return None
    import json

    import psycopg
    from psycopg import sql

    from tools.aws_config import _secret_string, database_parts

    payload = json.loads(_secret_string(secret_id))
    username = str(payload["username"])
    password = str(payload["password"])
    if not _IDENT.fullmatch(username):
        raise RuntimeError("The application database user name is not a safe identifier.")
    _host, _port, database = database_parts(master_url)
    if not _IDENT.fullmatch(database):
        raise RuntimeError("The database name is not a safe identifier.")
    user = sql.Identifier(username)
    with psycopg.connect(master_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (username,),
        ).fetchone()
        statement = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} LOGIN PASSWORD {}"
        connection.execute(sql.SQL(statement).format(user, sql.Literal(password)))
    with psycopg.connect(master_url) as connection:
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), user)
        )
        connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(user))
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(user)
        )
        connection.execute(
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(user)
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(user)
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(user)
        )
        connection.commit()
    return username


def _statements(script):
    return [part.strip() for part in script.split(";") if part.strip()]


def main():
    from tools.aws_config import master_database_url

    url = master_database_url()
    if not url:
        print("No PostgreSQL URL is configured. Local SQLite initializes itself.")
        return 0
    apply_migrations(url)
    ensure_app_role(url)
    print("PostgreSQL migrations applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
