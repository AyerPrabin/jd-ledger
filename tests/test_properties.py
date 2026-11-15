"""Property tests: random sequences of operations against a simple model.

Hypothesis makes up lists of credits, debits, transfers, refunds and
retries. Each step is run against the database and against a plain dict of
expected balances. After every step the two must agree, and at the end the
total must still be zero and reconcile must find nothing.
"""

import uuid
from dataclasses import dataclass

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jd_ledger import ledger
from jd_ledger.ledger import Conn
from tests.conftest import reset, total_points

PLAYERS = 3

amounts = st.integers(min_value=1, max_value=300)
player = st.integers(min_value=0, max_value=PLAYERS - 1)
steps = st.one_of(
    st.tuples(st.just("credit"), player, amounts),
    st.tuples(st.just("debit"), player, amounts),
    st.tuples(st.just("transfer"), player, player, amounts),
    st.tuples(st.just("refund"), st.integers(min_value=0, max_value=50)),
    st.tuples(st.just("retry"), st.integers(min_value=0, max_value=50)),
)


@dataclass
class Done:
    """An operation that went through, kept so it can be refunded or retried."""

    op: ledger.Operation
    call: tuple[object, ...]
    refunded: bool = False


@settings(
    max_examples=40,
    deadline=None,
    # the conn fixture is shared by all examples; reset() runs at the
    # start of each one so they do not see each other's data
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(st.lists(steps, max_size=25))
def test_random_operations_keep_the_books_straight(
    conn: Conn, operations: list[tuple[object, ...]]
) -> None:
    reset(conn)
    ids = [ledger.create_account(conn, f"player_{n}") for n in range(PLAYERS)]
    row = conn.execute("SELECT id FROM accounts WHERE name = %s", (ledger.ISSUANCE_ACCOUNT,))
    issuance = row.fetchone()[0]
    expected = {account_id: 0 for account_id in ids}
    expected[issuance] = 0
    done: list[Done] = []

    def run(call: tuple[object, ...], k: str) -> ledger.Operation:
        match call:
            case ("credit", a, n):
                return ledger.credit(conn, a, n, idempotency_key=k)
            case ("debit", a, n):
                return ledger.debit(conn, a, n, idempotency_key=k)
            case ("transfer", a, b, n):
                return ledger.transfer(conn, a, b, n, idempotency_key=k)
            case ("refund", op_id):
                return ledger.refund(conn, op_id, idempotency_key=k)
        raise AssertionError(call)

    for step in operations:
        kind = step[0]
        k = str(uuid.uuid4())

        if kind == "retry":
            if not done:
                continue
            target = done[int(step[1]) % len(done)]
            again = run(target.call, target.op.idempotency_key)
            assert again.replayed
            assert again.id == target.op.id

        elif kind == "refund":
            candidates = [d for d in done if d.op.kind in ("debit", "transfer")]
            if not candidates:
                continue
            target = candidates[int(step[1]) % len(candidates)]
            reversal = {e.account_id: -e.amount for e in target.op.entries}
            can_pay = all(
                expected[a] + delta >= 0 or a == issuance for a, delta in reversal.items()
            )
            call = ("refund", target.op.id)
            try:
                op = run(call, k)
            except ledger.AlreadyRefunded:
                assert target.refunded
            except ledger.InsufficientFunds:
                assert not target.refunded and not can_pay
            else:
                assert not target.refunded and can_pay
                target.refunded = True
                for a, delta in reversal.items():
                    expected[a] += delta
                done.append(Done(op, call))

        else:
            if kind == "transfer":
                _, src, dst, n = step
                call = ("transfer", ids[src], ids[dst], n)
                moves = {ids[src]: -n, ids[dst]: n}
            else:
                _, who, n = step
                sign = 1 if kind == "credit" else -1
                call = (kind, ids[who], n)
                moves = {ids[who]: sign * n, issuance: -sign * n}
            try:
                op = run(call, k)
            except ledger.SameAccount:
                assert kind == "transfer" and step[1] == step[2]
            except ledger.InsufficientFunds:
                assert any(expected[a] + d < 0 and a != issuance for a, d in moves.items())
            else:
                for a, d in moves.items():
                    expected[a] += d
                done.append(Done(op, call))

        for account_id, points in expected.items():
            assert ledger.get_account(conn, account_id).balance == points

    assert total_points(conn) == 0
    assert all(expected[a] >= 0 for a in ids)
    assert ledger.reconcile(conn) == []
