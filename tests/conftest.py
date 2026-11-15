"""Tests run against a real PostgreSQL database.

Set TEST_DATABASE_URL (or DATABASE_URL) to an empty database you do not
mind being wiped, for example:

    postgresql://postgres:postgres@localhost:5432/jd_ledger_test

Without it the tests are skipped locally. In CI (CI=true) a missing
database is an error, so a green run always means the tests really ran.
"""

import os
import uuid
from collections.abc import Iterator

import pytest

from jd_ledger import ledger
from jd_ledger.db import connect
from jd_ledger.ledger import Conn
from jd_ledger.migrate import migrate


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("TEST_DATABASE_URL is not set in CI")
        pytest.skip("no TEST_DATABASE_URL, skipping database tests")
    with connect(url) as conn:
        migrate(conn)
    return url


def reset(conn: Conn) -> None:
    # TRUNCATE does not fire the row-level append-only triggers, which is
    # why it works here while DELETE would not.
    conn.execute("TRUNCATE ledger_entries, operations, accounts RESTART IDENTITY CASCADE")
    conn.execute(
        "INSERT INTO accounts (name, allow_negative) VALUES (%s, TRUE)",
        (ledger.ISSUANCE_ACCOUNT,),
    )


@pytest.fixture
def conn(db_url: str) -> Iterator[Conn]:
    with connect(db_url) as c:
        reset(c)
        yield c


def key() -> str:
    return str(uuid.uuid4())


def make_player(conn: Conn, name: str, points: int = 0) -> int:
    account_id = ledger.create_account(conn, name)
    if points:
        ledger.credit(conn, account_id, points, idempotency_key=key())
    return account_id


def total_points(conn: Conn) -> int:
    row = conn.execute("SELECT COALESCE(sum(balance), 0) FROM accounts").fetchone()
    assert row is not None
    return int(row[0])
