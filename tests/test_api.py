from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jd_ledger import auth
from jd_ledger.api import create_app
from jd_ledger.ledger import Conn
from tests.conftest import make_player

ADMIN = "test-admin-token"


@pytest.fixture
def client(conn: Conn, db_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(db_url, admin_token=ADMIN)) as c:
        yield c


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def player(conn: Conn, name: str, points: int = 0) -> tuple[int, dict[str, str]]:
    account_id = make_player(conn, name, points)
    return account_id, bearer(auth.issue_token(conn, account_id))


def transfer(
    client: TestClient, headers: dict[str, str], body: dict[str, int], key: str = "k"
) -> int:
    response = client.post("/transfers", json=body, headers={**headers, "Idempotency-Key": key})
    return response.status_code


def test_transfer_then_read_both_accounts(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 100)
    bravo, bravo_auth = player(conn, "player_bravo")

    r = client.post(
        "/transfers",
        json={"from_account_id": alpha, "to_account_id": bravo, "amount": 40},
        headers={**alpha_auth, "Idempotency-Key": "api-1"},
    )

    assert r.status_code == 201
    assert r.json()["kind"] == "transfer"
    assert client.get(f"/accounts/{alpha}", headers=alpha_auth).json()["balance"] == 60
    assert client.get(f"/accounts/{bravo}", headers=bravo_auth).json() == {
        "id": bravo,
        "name": "player_bravo",
        "balance": 40,
    }


def test_retried_transfer_returns_200_and_same_body(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 100)
    bravo, _ = player(conn, "player_bravo")
    body = {"from_account_id": alpha, "to_account_id": bravo, "amount": 40}
    headers = {**alpha_auth, "Idempotency-Key": "api-2"}

    first = client.post("/transfers", json=body, headers=headers)
    again = client.post("/transfers", json=body, headers=headers)

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json() == first.json()
    assert client.get(f"/accounts/{alpha}", headers=alpha_auth).json()["balance"] == 60


def test_transfer_errors(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 10)
    bravo, _ = player(conn, "player_bravo")
    ok = {"from_account_id": alpha, "to_account_id": bravo, "amount": 5}

    no_key = client.post("/transfers", json=ok, headers=alpha_auth)
    assert no_key.status_code == 422
    assert transfer(client, alpha_auth, {**ok, "amount": 0}) == 422
    assert transfer(client, alpha_auth, {**ok, "amount": 50}, "too-much") == 422
    assert transfer(client, alpha_auth, {**ok, "to_account_id": alpha}, "same") == 400
    assert transfer(client, alpha_auth, {**ok, "to_account_id": 9999}, "missing") == 404
    assert transfer(client, alpha_auth, ok, "reused") == 201
    assert transfer(client, alpha_auth, {**ok, "amount": 4}, "reused") == 409


def test_idempotency_keys_are_per_caller(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 100)
    bravo, bravo_auth = player(conn, "player_bravo", 100)

    a_to_b = {"from_account_id": alpha, "to_account_id": bravo, "amount": 10}
    b_to_a = {"from_account_id": bravo, "to_account_id": alpha, "amount": 30}

    assert transfer(client, alpha_auth, a_to_b, "shared-key") == 201
    assert transfer(client, bravo_auth, b_to_a, "shared-key") == 201


# --- authentication and access ------------------------------------------


def test_requests_without_a_token_get_401(client: TestClient, conn: Conn) -> None:
    alpha, _ = player(conn, "player_alpha", 100)
    bravo, _ = player(conn, "player_bravo")
    body = {"from_account_id": alpha, "to_account_id": bravo, "amount": 1}

    responses = [
        client.post("/transfers", json=body, headers={"Idempotency-Key": "x"}),
        client.get(f"/accounts/{alpha}"),
        client.get(f"/accounts/{alpha}/entries"),
        client.get("/reconcile"),
    ]

    assert [r.status_code for r in responses] == [401, 401, 401, 401]
    assert responses[0].headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer nonsense", "Bearer 2.wrong-secret", "Bearer 2.", "Basic abc", "Bearer x.y"],
)
def test_bad_tokens_get_401(client: TestClient, conn: Conn, header: str) -> None:
    alpha, _ = player(conn, "player_alpha", 100)
    assert alpha == 2
    assert client.get(f"/accounts/{alpha}", headers={"Authorization": header}).status_code == 401


def test_old_token_stops_working_after_a_new_one(client: TestClient, conn: Conn) -> None:
    alpha, old = player(conn, "player_alpha")
    new = bearer(auth.issue_token(conn, alpha))

    assert client.get(f"/accounts/{alpha}", headers=old).status_code == 401
    assert client.get(f"/accounts/{alpha}", headers=new).status_code == 200


def test_cannot_read_another_players_account(client: TestClient, conn: Conn) -> None:
    _, alpha_auth = player(conn, "player_alpha", 100)
    bravo, _ = player(conn, "player_bravo", 100)

    assert client.get(f"/accounts/{bravo}", headers=alpha_auth).status_code == 403
    assert client.get(f"/accounts/{bravo}/entries", headers=alpha_auth).status_code == 403
    # an account that does not exist looks the same as someone else's
    assert client.get("/accounts/9999", headers=alpha_auth).status_code == 403


def test_cannot_send_from_another_players_account(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 100)
    bravo, _ = player(conn, "player_bravo", 100)

    body = {"from_account_id": bravo, "to_account_id": alpha, "amount": 50}
    assert transfer(client, alpha_auth, body) == 403
    assert client.get(f"/accounts/{alpha}", headers=alpha_auth).json()["balance"] == 100


def test_issuance_account_cannot_be_used_over_http(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 100)
    issuance = 1

    to_issuance = {"from_account_id": alpha, "to_account_id": issuance, "amount": 10}
    from_issuance = {"from_account_id": issuance, "to_account_id": alpha, "amount": 10}

    assert transfer(client, alpha_auth, to_issuance, "a") == 403
    assert transfer(client, alpha_auth, from_issuance, "b") == 403
    assert client.get(f"/accounts/{alpha}", headers=alpha_auth).json()["balance"] == 100


def test_entries_are_paginated(client: TestClient, conn: Conn) -> None:
    alpha, alpha_auth = player(conn, "player_alpha", 100)
    bravo, bravo_auth = player(conn, "player_bravo")
    for n in range(1, 6):
        body = {"from_account_id": alpha, "to_account_id": bravo, "amount": n}
        assert transfer(client, alpha_auth, body, f"page-{n}") == 201

    seen: list[int] = []
    before_id = None
    while True:
        params = {"limit": 2} if before_id is None else {"limit": 2, "before_id": before_id}
        page = client.get(f"/accounts/{bravo}/entries", params=params, headers=bravo_auth).json()
        seen += [e["amount"] for e in page["entries"]]
        before_id = page["next_before_id"]
        if before_id is None:
            break

    assert seen == [5, 4, 3, 2, 1]


def test_reconcile_needs_the_admin_token(client: TestClient, conn: Conn) -> None:
    _, alpha_auth = player(conn, "player_alpha", 100)

    assert client.get("/reconcile", headers=alpha_auth).status_code == 401
    assert client.get("/reconcile", headers=bearer("wrong")).status_code == 401
    assert client.get("/reconcile", headers=bearer(ADMIN)).status_code == 200


def test_reconcile_is_off_without_an_admin_token(conn: Conn, db_url: str) -> None:
    with TestClient(create_app(db_url, admin_token="")) as c:
        assert c.get("/reconcile", headers=bearer("")).status_code == 401
        assert c.get("/reconcile", headers=bearer("anything")).status_code == 401


def test_reconcile_endpoint(client: TestClient, conn: Conn) -> None:
    alpha, _ = player(conn, "player_alpha", 100)
    admin = bearer(ADMIN)
    assert client.get("/reconcile", headers=admin).json() == {"ok": True, "mismatches": []}

    conn.execute("UPDATE accounts SET balance = 99 WHERE id = %s", (alpha,))

    assert client.get("/reconcile", headers=admin).json() == {
        "ok": False,
        "mismatches": [{"account_id": alpha, "balance": 99, "ledger_sum": 100}],
    }
