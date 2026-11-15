"""Apply the SQL files in migrations/ in name order, once each.

Run with: python -m jd_ledger.migrate
"""

from pathlib import Path

import psycopg

from jd_ledger.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migrate(conn: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY,"
        " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with conn.transaction():
            # stops two app containers migrating at the same time
            conn.execute("SELECT pg_advisory_xact_lock(4210)")
            done = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,)
            ).fetchone()
            if done:
                continue
            conn.execute(path.read_text(encoding="utf-8").encode())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def main() -> None:
    with connect() as conn:
        applied = migrate(conn)
    print("applied:", ", ".join(applied) if applied else "nothing new")


if __name__ == "__main__":
    main()
