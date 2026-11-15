import os

import psycopg

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/jd_ledger"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def connect(url: str | None = None) -> psycopg.Connection[tuple[object, ...]]:
    # autocommit=True so nothing runs inside a hidden transaction. Every
    # write in this project opens its own with conn.transaction().
    return psycopg.connect(url or database_url(), autocommit=True)
