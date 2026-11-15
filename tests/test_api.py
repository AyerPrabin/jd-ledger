from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jd_ledger.api import create_app
from jd_ledger.ledger import Conn
from tests.conftest import make_player


@pytest.fixture
def client(conn: Conn, db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(db_url)) as c:
        yield c


def test_transfer_then_read_both_accounts(client: TestClient, conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo")

    r = client.post(
        "/transfers",
        json={"from_account_id": alpha, "to_account_id": bravo, "amount": 40},
        headers={"Idempotency-Key": "api-1"},
    )

    assert r.status_code == 201
    assert r.json()["kind"] == "transfer"
    assert client.get(f"/accounts/{alpha}").json()["balance"] == 60
    assert client.get(f"/accounts/{bravo}").json() == {
        "id": bravo,
        "name": "player_bravo",
        "balance": 40,
    }


def test_retried_transfer_returns_200_and_same_body(client: TestClient, conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo")
    body = {"from_account_id": alpha, "to_account_id": bravo, "amount": 40}

    first = client.post("/transfers", json=body, headers={"Idempotency-Key": "api-2"})
    again = client.post("/transfers", json=body, headers={"Idempotency-Key": "api-2"})

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    assert client.get(f"/accounts/{alpha}").json()["balance"] == 60


def test_transfer_errors(client: TestClient, conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 10)
    bravo = make_player(conn, "player_bravo")

    def send(body: dict[str, int], k: str = "k") -> int:
        return client.post("/transfers", json=body, headers={"Idempotency-Key": k}).status_code

    ok = {"from_account_id": alpha, "to_account_id": bravo, "amount": 5}
    assert client.post("/transfers", json=ok).status_code == 422  # no Idempotency-Key
    assert send({**ok, "amount": 0}) == 422
    assert send({**ok, "amount": 50}, "too-much") == 422
    assert send({**ok, "to_account_id": alpha}, "same") == 400
    assert send({**ok, "to_account_id": 9999}, "missing") == 404
    assert send(ok, "reused") == 201
    assert send({**ok, "amount": 4}, "reused") == 409


def test_unknown_account_is_404(client: TestClient) -> None:
    assert client.get("/accounts/9999").status_code == 404
    assert client.get("/accounts/9999/entries").status_code == 404


def test_entries_are_paginated(client: TestClient, conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo")
    for n in range(1, 6):
        client.post(
            "/transfers",
            json={"from_account_id": alpha, "to_account_id": bravo, "amount": n},
            headers={"Idempotency-Key": f"page-{n}"},
        )

    seen: list[int] = []
    before_id = None
    while True:
        params = {"limit": 2} if before_id is None else {"limit": 2, "before_id": before_id}
        page = client.get(f"/accounts/{bravo}/entries", params=params).json()
        seen += [e["amount"] for e in page["entries"]]
        before_id = page["next_before_id"]
        if before_id is None:
            break

    assert seen == [5, 4, 3, 2, 1]


def test_reconcile_endpoint(client: TestClient, conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    assert client.get("/reconcile").json() == {"ok": True, "mismatches": []}

    conn.execute("UPDATE accounts SET balance = 99 WHERE id = %s", (alpha,))

    assert client.get("/reconcile").json() == {
        "ok": False,
        "mismatches": [{"account_id": alpha, "balance": 99, "ledger_sum": 100}],
    }
