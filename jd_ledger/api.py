"""A thin HTTP layer over jd_ledger.ledger.

Run with: uvicorn jd_ledger.api:app
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from jd_ledger import ledger
from jd_ledger.db import database_url
from jd_ledger.ledger import Conn


class TransferIn(BaseModel):
    from_account_id: int
    to_account_id: int
    amount: int = Field(gt=0)


class EntryOut(BaseModel):
    id: int
    operation_id: int
    account_id: int
    amount: int
    balance_after: int


class OperationOut(BaseModel):
    id: int
    kind: str
    entries: list[EntryOut]


class AccountOut(BaseModel):
    id: int
    name: str
    balance: int


class EntriesPage(BaseModel):
    entries: list[EntryOut]
    # pass this as before_id to get the next (older) page; null on the last page
    next_before_id: int | None


class MismatchOut(BaseModel):
    account_id: int
    balance: int
    ledger_sum: int


class ReconcileOut(BaseModel):
    ok: bool
    mismatches: list[MismatchOut]


# status code and message for each error a transfer can raise
ERRORS: dict[type[ledger.LedgerError], tuple[int, str]] = {
    ledger.AccountNotFound: (404, "account not found"),
    ledger.SameAccount: (400, "cannot transfer to the same account"),
    ledger.InvalidAmount: (400, "amount must be a positive whole number"),
    ledger.InsufficientFunds: (422, "insufficient funds"),
    ledger.IdempotencyConflict: (409, "idempotency key was already used for a different request"),
}


def create_app(url: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = ConnectionPool(
            url or database_url(), min_size=1, max_size=10, kwargs={"autocommit": True}
        )
        app.state.pool = pool
        try:
            yield
        finally:
            pool.close()

    app = FastAPI(title="jd-ledger", lifespan=lifespan)

    def get_conn(request: Request) -> Iterator[Conn]:
        pool: ConnectionPool[Conn] = request.app.state.pool
        with pool.connection() as conn:
            yield conn

    Db = Annotated[Conn, Depends(get_conn)]

    @app.get("/health")
    def health(conn: Db) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.post("/transfers", status_code=201)
    def create_transfer(
        body: TransferIn,
        response: Response,
        conn: Db,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> OperationOut:
        try:
            op = ledger.transfer(
                conn,
                body.from_account_id,
                body.to_account_id,
                body.amount,
                idempotency_key=idempotency_key,
            )
        except ledger.LedgerError as exc:
            status, message = ERRORS.get(type(exc), (400, "request failed"))
            raise HTTPException(status, message) from exc
        if op.replayed:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return OperationOut(
            id=op.id,
            kind=op.kind,
            entries=[EntryOut(**vars(e)) for e in op.entries],
        )

    @app.get("/accounts/{account_id}")
    def get_account(account_id: int, conn: Db) -> AccountOut:
        try:
            account = ledger.get_account(conn, account_id)
        except ledger.AccountNotFound as exc:
            raise HTTPException(404, "account not found") from exc
        return AccountOut(id=account.id, name=account.name, balance=account.balance)

    @app.get("/accounts/{account_id}/entries")
    def get_entries(
        account_id: int,
        conn: Db,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> EntriesPage:
        try:
            entries = ledger.list_entries(conn, account_id, limit=limit, before_id=before_id)
        except ledger.AccountNotFound as exc:
            raise HTTPException(404, "account not found") from exc
        return EntriesPage(
            entries=[EntryOut(**vars(e)) for e in entries],
            next_before_id=entries[-1].id if len(entries) == limit else None,
        )

    @app.get("/reconcile")
    def get_reconcile(conn: Db) -> ReconcileOut:
        mismatches = ledger.reconcile(conn)
        return ReconcileOut(
            ok=not mismatches,
            mismatches=[MismatchOut(**vars(m)) for m in mismatches],
        )

    return app


app = create_app()
