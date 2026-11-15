"""Many transfers at once, each on its own connection.

If the row locks were missing or taken in a different order per request,
these would show lost updates (balances that do not add up) or deadlock
errors from PostgreSQL.
"""

import random
import threading
from collections.abc import Callable

from jd_ledger import ledger
from jd_ledger.db import connect
from jd_ledger.ledger import Conn
from tests.conftest import key, make_player, total_points


def run_in_parallel(db_url: str, jobs: list[Callable[[Conn], object]]) -> list[BaseException]:
    """Start every job at the same moment and return whatever they raised."""
    barrier = threading.Barrier(len(jobs))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(job: Callable[[Conn], object]) -> None:
        try:
            with connect(db_url) as c:
                barrier.wait()
                job(c)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(job,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_fifty_parallel_transfers_keep_the_books_straight(conn: Conn, db_url: str) -> None:
    names = ["player_alpha", "player_bravo", "player_charlie", "player_delta"]
    accounts = [make_player(conn, name, 500) for name in names]
    start_total = sum(ledger.get_account(conn, a).balance for a in accounts)
    rng = random.Random(42)

    jobs: list[Callable[[Conn], object]] = []
    for _ in range(50):
        src, dst = rng.sample(accounts, 2)
        amount = rng.randint(1, 120)

        def job(c: Conn, src: int = src, dst: int = dst, amount: int = amount) -> object:
            try:
                return ledger.transfer(c, src, dst, amount, idempotency_key=key())
            except ledger.InsufficientFunds:
                return None  # allowed: the sender ran out first

        jobs.append(job)

    errors = run_in_parallel(db_url, jobs)

    assert errors == []  # no deadlocks, no serialisation failures
    assert sum(ledger.get_account(conn, a).balance for a in accounts) == start_total
    assert all(ledger.get_account(conn, a).balance >= 0 for a in accounts)
    assert total_points(conn) == 0
    assert ledger.reconcile(conn) == []


def test_opposite_direction_transfers_do_not_deadlock(conn: Conn, db_url: str) -> None:
    alpha = make_player(conn, "player_alpha", 1000)
    bravo = make_player(conn, "player_bravo", 1000)

    def a_to_b(c: Conn) -> object:
        return ledger.transfer(c, alpha, bravo, 7, idempotency_key=key())

    def b_to_a(c: Conn) -> object:
        return ledger.transfer(c, bravo, alpha, 5, idempotency_key=key())

    errors = run_in_parallel(db_url, [a_to_b] * 20 + [b_to_a] * 20)

    assert errors == []
    assert ledger.get_account(conn, alpha).balance == 1000 - 20 * 7 + 20 * 5
    assert ledger.get_account(conn, bravo).balance == 1000 + 20 * 7 - 20 * 5
    assert ledger.reconcile(conn) == []


def test_parallel_credits_lose_no_updates(conn: Conn, db_url: str) -> None:
    alpha = make_player(conn, "player_alpha")

    def load_one(c: Conn) -> object:
        return ledger.credit(c, alpha, 1, idempotency_key=key())

    errors = run_in_parallel(db_url, [load_one] * 50)

    assert errors == []
    assert ledger.get_account(conn, alpha).balance == 50
    assert ledger.reconcile(conn) == []


def test_parallel_refunds_of_one_operation_succeed_once(conn: Conn, db_url: str) -> None:
    alpha = make_player(conn, "player_alpha", 100)
    fee = ledger.debit(conn, alpha, 60, idempotency_key=key())
    outcomes: list[str] = []
    lock = threading.Lock()

    def try_refund(c: Conn) -> object:
        try:
            ledger.refund(c, fee.id, idempotency_key=key())
            result = "refunded"
        except ledger.AlreadyRefunded:
            result = "already"
        with lock:
            outcomes.append(result)
        return None

    errors = run_in_parallel(db_url, [try_refund] * 8)

    assert errors == []
    assert outcomes.count("refunded") == 1
    assert outcomes.count("already") == 7
    assert ledger.get_account(conn, alpha).balance == 100
