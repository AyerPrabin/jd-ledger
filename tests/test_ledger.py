import pytest
from psycopg import errors

from jd_ledger import auth, ledger
from jd_ledger.ledger import Conn
from tests.conftest import key, make_player, total_points


def balance(conn: Conn, account_id: int) -> int:
    return ledger.get_account(conn, account_id).balance


def operation_count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) FROM operations").fetchone()
    assert row is not None
    return int(row[0])


# --- credit and debit ---------------------------------------------------


def test_credit_adds_points_and_writes_two_entries(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha")

    op = ledger.credit(conn, alpha, 500, idempotency_key=key())

    assert balance(conn, alpha) == 500
    assert op.kind == "credit"
    assert sorted(e.amount for e in op.entries) == [-500, 500]
    player_entry = next(e for e in op.entries if e.account_id == alpha)
    assert player_entry.balance_after == 500
    assert total_points(conn) == 0


def test_debit_takes_points_off(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 300)

    ledger.debit(conn, alpha, 120, idempotency_key=key())

    assert balance(conn, alpha) == 180
    assert total_points(conn) == 0


def test_debit_more_than_balance_fails_and_writes_nothing(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    before = operation_count(conn)

    with pytest.raises(ledger.InsufficientFunds):
        ledger.debit(conn, alpha, 101, idempotency_key=key())

    assert balance(conn, alpha) == 100
    assert operation_count(conn) == before


@pytest.mark.parametrize("amount", [0, -5, True])
def test_bad_amounts_are_rejected(conn: Conn, amount: int) -> None:
    alpha = make_player(conn, "player_alpha")
    with pytest.raises(ledger.InvalidAmount):
        ledger.credit(conn, alpha, amount, idempotency_key=key())


def test_credit_to_unknown_account(conn: Conn) -> None:
    with pytest.raises(ledger.AccountNotFound):
        ledger.credit(conn, 9999, 10, idempotency_key=key())
    assert operation_count(conn) == 0


# --- transfer -----------------------------------------------------------


def test_transfer_moves_points(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 200)
    bravo = make_player(conn, "player_bravo", 50)

    op = ledger.transfer(conn, alpha, bravo, 75, idempotency_key=key())

    assert balance(conn, alpha) == 125
    assert balance(conn, bravo) == 125
    assert {(e.account_id, e.amount) for e in op.entries} == {(alpha, -75), (bravo, 75)}


def test_transfer_with_insufficient_funds(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 10)
    bravo = make_player(conn, "player_bravo")

    with pytest.raises(ledger.InsufficientFunds):
        ledger.transfer(conn, alpha, bravo, 11, idempotency_key=key())

    assert balance(conn, alpha) == 10
    assert balance(conn, bravo) == 0


def test_transfer_to_same_account(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 10)
    with pytest.raises(ledger.SameAccount):
        ledger.transfer(conn, alpha, alpha, 5, idempotency_key=key())


def test_transfer_to_unknown_account(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 10)
    before = operation_count(conn)

    with pytest.raises(ledger.AccountNotFound):
        ledger.transfer(conn, alpha, 9999, 5, idempotency_key=key())

    assert balance(conn, alpha) == 10
    assert operation_count(conn) == before


# --- refund -------------------------------------------------------------


def test_refund_reverses_a_transfer(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    pool = make_player(conn, "event_pool_1")
    entry_fee = ledger.transfer(conn, alpha, pool, 40, idempotency_key=key())

    op = ledger.refund(conn, entry_fee.id, idempotency_key=key())

    assert op.refund_of == entry_fee.id
    assert balance(conn, alpha) == 100
    assert balance(conn, pool) == 0


def test_refund_reverses_a_debit(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    fee = ledger.debit(conn, alpha, 30, idempotency_key=key())

    ledger.refund(conn, fee.id, idempotency_key=key())

    assert balance(conn, alpha) == 100
    assert total_points(conn) == 0


def test_refund_only_once(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    fee = ledger.debit(conn, alpha, 30, idempotency_key=key())
    ledger.refund(conn, fee.id, idempotency_key=key())

    with pytest.raises(ledger.AlreadyRefunded):
        ledger.refund(conn, fee.id, idempotency_key=key())

    assert balance(conn, alpha) == 100


def test_credit_and_refund_cannot_be_refunded(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha")
    load = ledger.credit(conn, alpha, 50, idempotency_key=key())
    fee = ledger.debit(conn, alpha, 20, idempotency_key=key())
    back = ledger.refund(conn, fee.id, idempotency_key=key())

    with pytest.raises(ledger.NotRefundable):
        ledger.refund(conn, load.id, idempotency_key=key())
    with pytest.raises(ledger.NotRefundable):
        ledger.refund(conn, back.id, idempotency_key=key())


def test_refund_of_unknown_operation(conn: Conn) -> None:
    with pytest.raises(ledger.OperationNotFound):
        ledger.refund(conn, 12345, idempotency_key=key())


def test_refund_fails_if_receiver_already_spent_it(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo")
    sent = ledger.transfer(conn, alpha, bravo, 60, idempotency_key=key())
    ledger.debit(conn, bravo, 50, idempotency_key=key())

    with pytest.raises(ledger.InsufficientFunds):
        ledger.refund(conn, sent.id, idempotency_key=key())

    # the failed refund did not use up the one allowed refund
    ledger.credit(conn, bravo, 50, idempotency_key=key())
    ledger.refund(conn, sent.id, idempotency_key=key())
    assert balance(conn, alpha) == 100


# --- rules the database enforces on its own ------------------------------


def test_ledger_entries_cannot_be_updated(conn: Conn) -> None:
    make_player(conn, "player_alpha", 100)
    with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
        conn.execute("UPDATE ledger_entries SET amount = 1")


def test_ledger_entries_cannot_be_deleted(conn: Conn) -> None:
    make_player(conn, "player_alpha", 100)
    with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
        conn.execute("DELETE FROM ledger_entries")


def test_operations_cannot_be_deleted(conn: Conn) -> None:
    make_player(conn, "player_alpha", 100)
    with pytest.raises(errors.InsufficientPrivilege, match="append-only"):
        conn.execute("DELETE FROM operations")


def test_balance_check_constraint(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 5)
    with pytest.raises(errors.CheckViolation):
        conn.execute("UPDATE accounts SET balance = -1 WHERE id = %s", (alpha,))


def test_unbalanced_operation_is_rejected_at_commit(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha")
    with pytest.raises(errors.CheckViolation, match="does not balance"), conn.transaction():
        op = conn.execute(
            "INSERT INTO operations (kind, idempotency_key, request_hash)"
            " VALUES ('credit', 'by-hand', 'x') RETURNING id"
        ).fetchone()
        assert op is not None
        conn.execute(
            "INSERT INTO ledger_entries (operation_id, account_id, amount, balance_after)"
            " VALUES (%s, %s, 10, 10)",
            (op[0], alpha),
        )
    assert operation_count(conn) == 0


# --- reconcile and reading ----------------------------------------------


def test_reconcile_is_clean_after_normal_use(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo", 100)
    fee = ledger.transfer(conn, alpha, bravo, 30, idempotency_key=key())
    ledger.refund(conn, fee.id, idempotency_key=key())
    ledger.debit(conn, bravo, 10, idempotency_key=key())

    assert ledger.reconcile(conn) == []


def test_reconcile_finds_a_tampered_balance(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    conn.execute("UPDATE accounts SET balance = balance + 5 WHERE id = %s", (alpha,))

    assert ledger.reconcile(conn) == [
        ledger.Mismatch(account_id=alpha, balance=105, ledger_sum=100)
    ]


def test_reconcile_finds_balance_with_no_entries(conn: Conn) -> None:
    bravo = make_player(conn, "player_bravo")
    conn.execute("UPDATE accounts SET balance = 7 WHERE id = %s", (bravo,))

    assert ledger.reconcile(conn) == [ledger.Mismatch(account_id=bravo, balance=7, ledger_sum=0)]


def test_list_entries_pages_newest_first(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha")
    for points in range(1, 6):
        ledger.credit(conn, alpha, points, idempotency_key=key())

    first = ledger.list_entries(conn, alpha, limit=2)
    second = ledger.list_entries(conn, alpha, limit=2, before_id=first[-1].id)
    third = ledger.list_entries(conn, alpha, limit=2, before_id=second[-1].id)

    assert [e.amount for e in first + second + third] == [5, 4, 3, 2, 1]


def test_list_entries_for_unknown_account(conn: Conn) -> None:
    with pytest.raises(ledger.AccountNotFound):
        ledger.list_entries(conn, 9999)


# --- system accounts and tokens -------------------------------------------


def test_transfers_cannot_touch_the_issuance_account(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    issuance = 1

    with pytest.raises(ledger.SystemAccount):
        ledger.transfer(conn, issuance, alpha, 1_000_000, idempotency_key=key())
    with pytest.raises(ledger.SystemAccount):
        ledger.transfer(conn, alpha, issuance, 10, idempotency_key=key())

    assert balance(conn, alpha) == 100
    assert total_points(conn) == 0


def test_tokens_are_stored_hashed(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha")
    token = auth.issue_token(conn, alpha)
    secret = token.split(".", 1)[1]

    row = conn.execute("SELECT api_token_hash FROM accounts WHERE id = %s", (alpha,)).fetchone()
    assert row is not None
    assert secret not in row[0]
    assert auth.account_for_token(conn, token) == alpha
    assert auth.account_for_token(conn, token + "x") is None
    # a full-width "2" passes str.isdigit() but is not a valid id
    assert auth.account_for_token(conn, "２." + token.split(".", 1)[1]) is None


def test_system_accounts_cannot_get_a_token(conn: Conn) -> None:
    with pytest.raises(ledger.SystemAccount):
        auth.issue_token(conn, 1)
    with pytest.raises(errors.CheckViolation):
        conn.execute("UPDATE accounts SET api_token_hash = 'x' WHERE id = 1")
