"""Wallet operations: credit, debit, transfer and refund.

Every operation runs in one explicit transaction and follows the same steps:

1. Insert the operation row. If the idempotency key is already there, this
   is a retry, so return the stored result and move nothing.
2. Lock the account rows it touches with SELECT ... FOR UPDATE, always in
   id order, so two transfers between the same accounts cannot deadlock.
3. Check the new balances, update them and append the ledger entries.

The connection must be in autocommit mode (see db.connect) so that
conn.transaction() is a real transaction and not a savepoint.
"""

import hashlib
import json
from dataclasses import dataclass

import psycopg
from psycopg import errors

ISSUANCE_ACCOUNT = "issuance"

Conn = psycopg.Connection[tuple[object, ...]]


class LedgerError(Exception):
    """Base class for errors a caller is expected to handle."""


class InvalidAmount(LedgerError):
    pass


class AccountNotFound(LedgerError):
    pass


class SameAccount(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    """The key was used before for a different request."""


class OperationNotFound(LedgerError):
    pass


class NotRefundable(LedgerError):
    pass


class AlreadyRefunded(LedgerError):
    pass


@dataclass(frozen=True)
class Entry:
    id: int
    operation_id: int
    account_id: int
    amount: int
    balance_after: int


@dataclass(frozen=True)
class Operation:
    id: int
    kind: str
    idempotency_key: str
    refund_of: int | None
    entries: list[Entry]
    # True when this call was a retry and nothing new was written
    replayed: bool = False


@dataclass(frozen=True)
class Account:
    id: int
    name: str
    balance: int
    allow_negative: bool


@dataclass(frozen=True)
class Mismatch:
    account_id: int
    balance: int
    ledger_sum: int


def create_account(conn: Conn, name: str, *, allow_negative: bool = False) -> int:
    row = conn.execute(
        "INSERT INTO accounts (name, allow_negative) VALUES (%s, %s) RETURNING id",
        (name, allow_negative),
    ).fetchone()
    assert row is not None
    return _int(row[0])


def get_account(conn: Conn, account_id: int) -> Account:
    row = conn.execute(
        "SELECT id, name, balance, allow_negative FROM accounts WHERE id = %s", (account_id,)
    ).fetchone()
    if row is None:
        raise AccountNotFound(account_id)
    return Account(
        id=_int(row[0]), name=str(row[1]), balance=_int(row[2]), allow_negative=bool(row[3])
    )


def list_entries(
    conn: Conn, account_id: int, *, limit: int = 50, before_id: int | None = None
) -> list[Entry]:
    """Newest first. Pass the last id you got as before_id for the next page."""
    get_account(conn, account_id)
    rows = conn.execute(
        "SELECT id, operation_id, account_id, amount, balance_after FROM ledger_entries"
        " WHERE account_id = %s AND (%s::bigint IS NULL OR id < %s)"
        " ORDER BY id DESC LIMIT %s",
        (account_id, before_id, before_id, limit),
    ).fetchall()
    return [_entry(r) for r in rows]


def reconcile(conn: Conn) -> list[Mismatch]:
    rows = conn.execute(
        "SELECT account_id, balance, ledger_sum FROM reconcile_mismatches ORDER BY account_id"
    ).fetchall()
    return [
        Mismatch(account_id=_int(r[0]), balance=_int(r[1]), ledger_sum=_int(r[2])) for r in rows
    ]


def credit(conn: Conn, account_id: int, amount: int, *, idempotency_key: str) -> Operation:
    """Load points onto an account (they come out of the issuance account)."""
    _check_amount(amount)
    issuance = _issuance_id(conn)
    return _apply(
        conn,
        kind="credit",
        key=idempotency_key,
        request={"account_id": account_id, "amount": amount},
        legs=[(issuance, -amount), (account_id, amount)],
    )


def debit(conn: Conn, account_id: int, amount: int, *, idempotency_key: str) -> Operation:
    """Take points off an account (they go back to the issuance account)."""
    _check_amount(amount)
    issuance = _issuance_id(conn)
    return _apply(
        conn,
        kind="debit",
        key=idempotency_key,
        request={"account_id": account_id, "amount": amount},
        legs=[(account_id, -amount), (issuance, amount)],
    )


def transfer(
    conn: Conn, from_id: int, to_id: int, amount: int, *, idempotency_key: str
) -> Operation:
    _check_amount(amount)
    if from_id == to_id:
        raise SameAccount(from_id)
    return _apply(
        conn,
        kind="transfer",
        key=idempotency_key,
        request={"from": from_id, "to": to_id, "amount": amount},
        legs=[(from_id, -amount), (to_id, amount)],
    )


def refund(conn: Conn, operation_id: int, *, idempotency_key: str) -> Operation:
    """Reverse a debit or transfer. Each operation can be refunded once."""
    original = conn.execute("SELECT kind FROM operations WHERE id = %s", (operation_id,)).fetchone()
    if original is None:
        raise OperationNotFound(operation_id)
    if original[0] not in ("debit", "transfer"):
        raise NotRefundable(f"cannot refund a {original[0]}")
    rows = conn.execute(
        "SELECT account_id, amount FROM ledger_entries WHERE operation_id = %s ORDER BY id",
        (operation_id,),
    ).fetchall()
    legs = [(_int(account_id), -_int(amount)) for account_id, amount in rows]
    try:
        return _apply(
            conn,
            kind="refund",
            key=idempotency_key,
            request={"refund_of": operation_id},
            legs=legs,
            refund_of=operation_id,
        )
    except errors.UniqueViolation as exc:
        # Another refund of the same operation got there first. The UNIQUE
        # constraint on refund_of is what stops the double refund.
        if exc.diag.constraint_name == "operations_refund_of_key":
            raise AlreadyRefunded(operation_id) from exc
        raise


def _apply(
    conn: Conn,
    *,
    kind: str,
    key: str,
    request: dict[str, int],
    legs: list[tuple[int, int]],
    refund_of: int | None = None,
) -> Operation:
    request_hash = _hash(kind, request)
    with conn.transaction():
        # If another transaction is inserting the same key right now, this
        # waits for it to commit or roll back before deciding.
        row = conn.execute(
            "INSERT INTO operations (kind, idempotency_key, request_hash, refund_of)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (kind, key, request_hash, refund_of),
        ).fetchone()
        if row is None:
            return _replay(conn, key, request_hash)
        operation_id = _int(row[0])

        # Lock every account involved, lowest id first.
        account_ids = sorted({account_id for account_id, _ in legs})
        locked = conn.execute(
            "SELECT id, balance, allow_negative FROM accounts"
            " WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (account_ids,),
        ).fetchall()
        balances = {_int(r[0]): _int(r[1]) for r in locked}
        allow_negative = {_int(r[0]): bool(r[2]) for r in locked}
        missing = [a for a in account_ids if a not in balances]
        if missing:
            raise AccountNotFound(missing[0])

        entries: list[Entry] = []
        for account_id, amount in legs:
            new_balance = balances[account_id] + amount
            if new_balance < 0 and not allow_negative[account_id]:
                raise InsufficientFunds(
                    f"account {account_id} has {balances[account_id]}, needs {-amount}"
                )
            balances[account_id] = new_balance
            conn.execute(
                "UPDATE accounts SET balance = %s WHERE id = %s", (new_balance, account_id)
            )
            entry = conn.execute(
                "INSERT INTO ledger_entries (operation_id, account_id, amount, balance_after)"
                " VALUES (%s, %s, %s, %s)"
                " RETURNING id, operation_id, account_id, amount, balance_after",
                (operation_id, account_id, amount, new_balance),
            ).fetchone()
            assert entry is not None
            entries.append(_entry(entry))

    return Operation(
        id=operation_id, kind=kind, idempotency_key=key, refund_of=refund_of, entries=entries
    )


def _replay(conn: Conn, key: str, request_hash: str) -> Operation:
    row = conn.execute(
        "SELECT id, kind, request_hash, refund_of FROM operations WHERE idempotency_key = %s",
        (key,),
    ).fetchone()
    assert row is not None
    if row[2] != request_hash:
        raise IdempotencyConflict(key)
    operation_id = _int(row[0])
    rows = conn.execute(
        "SELECT id, operation_id, account_id, amount, balance_after FROM ledger_entries"
        " WHERE operation_id = %s ORDER BY id",
        (operation_id,),
    ).fetchall()
    return Operation(
        id=operation_id,
        kind=str(row[1]),
        idempotency_key=key,
        refund_of=None if row[3] is None else _int(row[3]),
        entries=[_entry(r) for r in rows],
        replayed=True,
    )


def _issuance_id(conn: Conn) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE name = %s", (ISSUANCE_ACCOUNT,)).fetchone()
    if row is None:
        raise AccountNotFound(ISSUANCE_ACCOUNT)
    return _int(row[0])


def _check_amount(amount: int) -> None:
    # bool is a subclass of int, so rule it out on purpose
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidAmount(amount)


def _hash(kind: str, request: dict[str, int]) -> str:
    body = json.dumps({"kind": kind, **request}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _entry(row: tuple[object, ...]) -> Entry:
    return Entry(
        id=_int(row[0]),
        operation_id=_int(row[1]),
        account_id=_int(row[2]),
        amount=_int(row[3]),
        balance_after=_int(row[4]),
    )


def _int(value: object) -> int:
    assert isinstance(value, int)
    return value
