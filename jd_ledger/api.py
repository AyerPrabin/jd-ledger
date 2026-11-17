"""A thin HTTP layer over jd_ledger.ledger.

Every request except /health needs "Authorization: Bearer <token>".
Player tokens come from python -m jd_ledger.auth and only give access to
that player's own account. /reconcile needs the ADMIN_TOKEN from the
environment and is switched off when it is not set.

Run with: uvicorn jd_ledger.api:app
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from jd_ledger import auth, ledger
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


POOL_SIZE = 10


# status code and message for each error a transfer can raise
ERRORS: dict[type[ledger.LedgerError], tuple[int, str]] = {
    ledger.AccountNotFound: (404, "account not found"),
    ledger.SameAccount: (400, "cannot transfer to the same account"),
    ledger.InvalidAmount: (400, "amount must be a positive whole number"),
    ledger.InsufficientFunds: (422, "insufficient funds"),
    ledger.IdempotencyConflict: (409, "idempotency key was already used for a different request"),
    ledger.SystemAccount: (403, "system accounts cannot be used here"),
}


def _bearer(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "missing bearer token", {"WWW-Authenticate": "Bearer"})
    return token.strip()


def create_app(url: str | None = None, admin_token: str | None = None) -> FastAPI:
    admin_token = admin_token or os.environ.get("ADMIN_TOKEN") or None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        with ConnectionPool(
            url or database_url(), min_size=1, max_size=POOL_SIZE, kwargs={"autocommit": True}
        ) as pool:
            app.state.pool = pool
            app.state.pool_slots = asyncio.Semaphore(POOL_SIZE)
            yield

    app = FastAPI(title="jd-ledger", lifespan=lifespan)

    async def get_conn(request: Request) -> AsyncIterator[Conn]:
        # Requests wait for a free connection here, on the event loop, and
        # only then take a worker thread to fetch it. If they waited inside
        # pool.getconn() instead, every worker thread could end up waiting
        # while the requests that hold the connections have no thread left
        # to finish on (the load test found this, see the README).
        pool: ConnectionPool[Conn] = request.app.state.pool
        async with request.app.state.pool_slots:
            conn = await run_in_threadpool(pool.getconn)
            try:
                yield conn
            finally:
                await run_in_threadpool(pool.putconn, conn)

    Db = Annotated[Conn, Depends(get_conn)]
    AuthHeader = Annotated[str | None, Header()]

    def current_account(conn: Db, authorization: AuthHeader = None) -> int:
        account_id = auth.account_for_token(conn, _bearer(authorization))
        if account_id is None:
            raise HTTPException(401, "invalid token", {"WWW-Authenticate": "Bearer"})
        return account_id

    def require_admin(authorization: AuthHeader = None) -> None:
        if not auth.is_admin(_bearer(authorization), admin_token):
            raise HTTPException(401, "admin token required", {"WWW-Authenticate": "Bearer"})

    Caller = Annotated[int, Depends(current_account)]

    def own_account(account_id: int, caller: Caller) -> int:
        # 403 for any account that is not yours, whether it exists or not
        if account_id != caller:
            raise HTTPException(403, "you can only see your own account")
        return account_id

    OwnAccount = Annotated[int, Depends(own_account)]

    @app.get("/health")
    def health(conn: Db) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.post("/transfers", status_code=201)
    def create_transfer(
        body: TransferIn,
        response: Response,
        conn: Db,
        caller: Caller,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=150)],
    ) -> OperationOut:
        if body.from_account_id != caller:
            raise HTTPException(403, "you can only send from your own account")
        try:
            op = ledger.transfer(
                conn,
                body.from_account_id,
                body.to_account_id,
                body.amount,
                # keys are per caller, so one player cannot collide with
                # (or probe for) another player's keys
                idempotency_key=f"account-{caller}:{idempotency_key}",
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
    def get_account(account_id: OwnAccount, conn: Db) -> AccountOut:
        account = ledger.get_account(conn, account_id)
        return AccountOut(id=account.id, name=account.name, balance=account.balance)

    @app.get("/accounts/{account_id}/entries")
    def get_entries(
        account_id: OwnAccount,
        conn: Db,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> EntriesPage:
        entries = ledger.list_entries(conn, account_id, limit=limit, before_id=before_id)
        return EntriesPage(
            entries=[EntryOut(**vars(e)) for e in entries],
            next_before_id=entries[-1].id if len(entries) == limit else None,
        )

    @app.get("/reconcile", dependencies=[Depends(require_admin)])
    def get_reconcile(conn: Db) -> ReconcileOut:
        mismatches = ledger.reconcile(conn)
        return ReconcileOut(
            ok=not mismatches,
            mismatches=[MismatchOut(**vars(m)) for m in mismatches],
        )

    return app


app = create_app()
