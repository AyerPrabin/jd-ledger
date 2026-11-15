import threading

import pytest

from jd_ledger import ledger
from jd_ledger.db import connect
from jd_ledger.ledger import Conn
from tests.conftest import make_player


def test_retry_with_same_key_moves_points_once(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo")

    first = ledger.transfer(conn, alpha, bravo, 30, idempotency_key="transfer-001")
    second = ledger.transfer(conn, alpha, bravo, 30, idempotency_key="transfer-001")

    assert second.id == first.id
    assert second.entries == first.entries
    assert not first.replayed
    assert second.replayed
    assert ledger.get_account(conn, alpha).balance == 70
    assert ledger.get_account(conn, bravo).balance == 30


def test_retried_credit_does_not_double_credit(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha")
    for _ in range(3):
        ledger.credit(conn, alpha, 250, idempotency_key="load-abc")
    assert ledger.get_account(conn, alpha).balance == 250


def test_same_key_with_different_amount_is_a_conflict(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    bravo = make_player(conn, "player_bravo")
    ledger.transfer(conn, alpha, bravo, 30, idempotency_key="transfer-002")

    with pytest.raises(ledger.IdempotencyConflict):
        ledger.transfer(conn, alpha, bravo, 31, idempotency_key="transfer-002")

    assert ledger.get_account(conn, bravo).balance == 30


def test_same_key_for_a_different_kind_is_a_conflict(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    ledger.debit(conn, alpha, 10, idempotency_key="op-1")

    with pytest.raises(ledger.IdempotencyConflict):
        ledger.credit(conn, alpha, 10, idempotency_key="op-1")


def test_failed_request_does_not_use_up_the_key(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 10)
    bravo = make_player(conn, "player_bravo")

    with pytest.raises(ledger.InsufficientFunds):
        ledger.transfer(conn, alpha, bravo, 50, idempotency_key="transfer-003")

    ledger.credit(conn, alpha, 40, idempotency_key="top-up")
    op = ledger.transfer(conn, alpha, bravo, 50, idempotency_key="transfer-003")
    assert not op.replayed
    assert ledger.get_account(conn, bravo).balance == 50


def test_refund_retry_returns_the_same_refund(conn: Conn) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    fee = ledger.debit(conn, alpha, 40, idempotency_key="fee-1")

    first = ledger.refund(conn, fee.id, idempotency_key="refund-1")
    again = ledger.refund(conn, fee.id, idempotency_key="refund-1")

    assert again.id == first.id
    assert again.replayed
    assert ledger.get_account(conn, alpha).balance == 100


def test_parallel_retries_with_same_key_apply_once(conn: Conn, db_url: str) -> None:
    alpha = make_player(conn, "player_alpha", 1000)
    bravo = make_player(conn, "player_bravo")
    threads = 10
    barrier = threading.Barrier(threads)
    results: list[ledger.Operation] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def send() -> None:
        try:
            with connect(db_url) as c:
                barrier.wait()
                op = ledger.transfer(c, alpha, bravo, 100, idempotency_key="same-key")
            with lock:
                results.append(op)
        except BaseException as exc:
            with lock:
                failures.append(exc)

    workers = [threading.Thread(target=send) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert failures == []
    assert len({op.id for op in results}) == 1
    assert sum(not op.replayed for op in results) == 1
    assert ledger.get_account(conn, bravo).balance == 100
    assert ledger.reconcile(conn) == []
