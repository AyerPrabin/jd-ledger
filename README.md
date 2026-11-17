<p align="center"><img src="assets/jd-logo.png" alt="JD Esports Arena logo" width="96"></p>

# jd-ledger

[![CI](https://github.com/AyerPrabin/jd-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/AyerPrabin/jd-ledger/actions/workflows/ci.yml)

A small points-wallet ledger written in Python 3.12 and PostgreSQL 16. Players load points, pay event entry fees, get refunds and receive prizes, and every balance has to equal the sum of its ledger rows.

It is a rebuild of the wallet core of JD Esports Arena, the Free Fire tournament platform I run. All the accounts in this repo are made up.

## Why I built it

On my platform I check that every wallet balance equals its ledger total. I wanted to rebuild that core properly, on its own, and test the hard parts: a phone that retries a payment because the network dropped, two requests hitting the same wallet at the same moment, and a refund being clicked twice. The tests run against a real PostgreSQL database in CI, not a mock, because most of the rules live in the database.

## How it fits together

```
migrations/         plain SQL, applied in order by jd_ledger/migrate.py
jd_ledger/ledger.py credit, debit, transfer, refund, reconcile
jd_ledger/auth.py   API tokens
jd_ledger/api.py    small FastAPI layer on top
tests/              pytest + Hypothesis, against PostgreSQL
load/               k6 load test, seed script and database check
```

Three tables:

- `accounts`: one row per wallet, with a cached `balance`.
- `operations`: one row per credit, debit, transfer or refund. Holds the idempotency key.
- `ledger_entries`: the actual movements. Every operation writes two entries that add up to zero (double entry).

Points are loaded from a system account called `issuance`, which is the only account allowed to go below zero. Because every operation balances, the sum of all balances in the table is always exactly zero, which makes "no points were created or lost" easy to check.

## Rules the database enforces

I wanted the important rules to hold even if someone bypasses the Python code and writes SQL by hand.

| Rule | How |
| --- | --- |
| No negative balances | `CHECK (allow_negative OR balance >= 0)` on `accounts` |
| History cannot be edited | a `BEFORE UPDATE OR DELETE` trigger on `ledger_entries` and `operations` raises an error. Mistakes are fixed with a new refund, not by changing old rows |
| Every operation balances | a deferred constraint trigger checks at `COMMIT` that an operation's entries sum to zero |
| A request is applied once | `operations.idempotency_key` is `UNIQUE` |
| An operation is refunded at most once | `operations.refund_of` is `UNIQUE` |
| Balance equals ledger sum | the `reconcile_mismatches` view lists any account where it does not. It should always be empty |

The tests check each of these directly, for example by trying `UPDATE ledger_entries` and expecting an error.

## A transfer, step by step

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant DB as PostgreSQL
    C->>A: POST /transfers (Idempotency-Key: abc)
    A->>DB: BEGIN
    A->>DB: INSERT INTO operations ... ON CONFLICT (idempotency_key) DO NOTHING
    alt key already used
        A->>DB: read the stored operation
        A-->>C: 200, same body as the first time
    else new key
        A->>DB: SELECT ... FROM accounts WHERE id = ANY(...) ORDER BY id FOR UPDATE
        A->>DB: check balance, UPDATE accounts, INSERT two ledger_entries
        A->>DB: COMMIT (deferred check: entries sum to zero)
        A-->>C: 201
    end
```

All of this is one transaction. If anything fails, such as insufficient funds, the whole thing rolls back, including the operation row.

## Concurrency and lock ordering

Two transfers that read a balance at the same time and both write it back would lose one of the updates. To stop that, each operation locks the account rows it touches with `SELECT ... FOR UPDATE` and only reads the balance after it holds the lock.

Locking alone is not enough. If transfer 1 goes A to B and locks A first, while transfer 2 goes B to A and locks B first, each one ends up waiting for the other and PostgreSQL has to kill one of them with a deadlock error. So the code always locks in the same order, lowest account id first, whatever the direction of the transfer. Two transactions can then only ever wait in one direction.

The concurrency tests start lots of threads at the same moment (with a `threading.Barrier`), each on its own database connection:

- 50 random transfers between four accounts: no errors other than an expected "insufficient funds", the total is unchanged and reconcile is clean
- 20 transfers A to B racing 20 transfers B to A: no deadlocks, and the final balances are exactly what the arithmetic says
- 50 parallel credits of 1 point to one account: the balance ends at 50, so no update was lost
- 8 parallel refunds of the same payment: exactly one succeeds

## Idempotency

Every operation needs an idempotency key. Over HTTP it is the `Idempotency-Key` header.

- The first request with a key does the work and stores the key with a sha256 hash of the request.
- A retry with the same key and the same request gets the stored result back (`200` with `Idempotent-Replayed: true` instead of `201`), and nothing moves.
- The same key with a different request (say a different amount) is refused with `409`, so a bug in a client cannot quietly reuse an old key.
- If two copies of a request arrive at the same moment, the second one's `INSERT ... ON CONFLICT` waits for the first to finish, then returns its result. There is a test that sends ten copies in parallel and checks that the transfer happened once.
- A request that failed (for example insufficient funds) rolls back completely, so its key is not used up and the client can retry it later.

## Refunds

`refund(operation_id)` reverses a debit or a transfer by writing a new operation with the opposite entries and `refund_of` pointing at the original. The `UNIQUE` constraint on `refund_of` is what really stops a double refund. The Python code turns the constraint error into a clear `AlreadyRefunded`. A refund can fail with insufficient funds if the receiver has already spent the points. In that case nothing is written, and the refund can be tried again later.

## Security

The first version of the API I pushed had no authentication. A review pointed out that anyone could then send a transfer from the `issuance` account and create points out of nothing. I fixed it like this:

- `transfer()` refuses any account with `allow_negative`, in the ledger code itself, not just in the API. Points can only be created through `credit()`, which is not exposed over HTTP.
- Each player account can have one API token, sent as `Authorization: Bearer <token>`. The token is `<account id>.<random secret>`. The database stores only the sha256 hash of the secret, and the check uses `hmac.compare_digest`. A new token replaces the old one. System accounts cannot have a token (there is a `CHECK` constraint for that too).
- You can only send from your own account and only read your own account and entries. Anything else gets `403`, including accounts that do not exist, so the API does not reveal which ids are real.
- `/reconcile` shows every account, so it needs a separate `ADMIN_TOKEN` from the environment. If that is not set, the endpoint is switched off.
- Idempotency keys sent over HTTP are stored per caller, so two players can use the same key without clashing, and one player cannot probe for another player's keys.

This is still simple. There is no token expiry and no rate limiting, and in a real deployment it would sit behind HTTPS and the platform's normal login rather than long-lived tokens.

## Running it with Docker Compose

```
docker compose up -d --build
docker compose exec api python -m jd_ledger.demo
```

The api container applies the migrations when it starts. The demo script creates four made-up accounts with some points and prints a token for each one:

```
  2  player_alpha       1000  2.xxxxxxxx
  3  player_bravo        500  3.xxxxxxxx
  ...
```

Then, using player_alpha's token:

```
curl -H "Authorization: Bearer 2.xxxxxxxx" http://localhost:8000/accounts/2

curl -X POST http://localhost:8000/transfers \
  -H "Authorization: Bearer 2.xxxxxxxx" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-first-transfer" \
  -d '{"from_account_id": 2, "to_account_id": 3, "amount": 25}'

curl -H "Authorization: Bearer 2.xxxxxxxx" "http://localhost:8000/accounts/2/entries?limit=10"
```

Run the POST twice and the second response is a `200` with the same body, and the balance only drops once.

To use `/reconcile`, start the stack with an admin token:

```
ADMIN_TOKEN=pick-something-long docker compose up -d
curl -H "Authorization: Bearer pick-something-long" http://localhost:8000/reconcile
```

The API docs are at http://localhost:8000/docs.

### Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/transfers` | needs `Idempotency-Key` (up to 150 characters). `201` new, `200` replay, `403` not your account or a system account, `409` key reused with a different body, `422` insufficient funds |
| GET | `/accounts/{id}` | your own account only |
| GET | `/accounts/{id}/entries` | newest first, `limit` (max 200) and `before_id` for the next page |
| GET | `/reconcile` | admin token only. `{"ok": true, "mismatches": []}` when everything adds up |
| GET | `/health` | no auth, used by the Docker health check |

Paging uses `before_id` (keyset paging) rather than an offset, so new entries arriving while you page do not shift the results.

## Tests

The tests need a PostgreSQL database they are allowed to wipe:

```
python -m venv .venv
.venv\Scripts\activate          (Windows)
source .venv/bin/activate       (macOS / Linux)
pip install -r requirements-dev.txt

docker run -d --name ledger-test -p 5432:5432 -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=jd_ledger_test postgres:16
set TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/jd_ledger_test      (Windows)
export TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/jd_ledger_test   (macOS / Linux)

pytest
ruff check .
ruff format --check .
mypy
```

Without `TEST_DATABASE_URL` the database tests are skipped on your machine. In CI a missing database is an error rather than a skip, so a green CI run means the tests really ran.

There are 61 test cases (some are parametrised):

- `test_ledger.py`: each operation, the error cases, and the database rules above (append-only trigger, check constraint, unbalanced operation rejected at commit, reconcile finding a tampered balance)
- `test_idempotency.py`: retries, key reuse with a different body, failed requests not using up a key, ten parallel copies of one request
- `test_concurrency.py`: the four thread tests described above
- `test_properties.py`: a Hypothesis test that makes up random sequences of credits, debits, transfers, refunds and retries, runs them against the database and against a plain Python dict of expected balances, and checks after every step that the two agree. At the end the total must be zero, no player can be negative and reconcile must be empty
- `test_api.py`: the HTTP layer, including the auth and access checks

GitHub Actions runs ruff, `mypy --strict` and pytest against a `postgres:16` service container on every push. A second job builds the image, starts the stack with `docker compose up --wait`, loads the demo accounts and calls the endpoints with curl.

## Load test: 21,781 transfers from 200 concurrent users

The tests above use threads inside one Python process. I also wanted to see the whole stack under load over HTTP, so there is a [k6](https://k6.io) load test in `load/` and a separate [Load test workflow](.github/workflows/load.yml). It runs on demand and whenever `load/` changes.

What one run does:

1. `load/seed.py` creates 50 accounts, each with an API token. The 4 "hot" accounts get 1,000,000 points each and the other 46 get 100,000 each. The API has no endpoint for creating accounts or loading points, so this goes through the ledger code.
2. uvicorn serves the API as it ships: one worker, a connection pool of 10.
3. `load/transfers.js` ramps k6 up to 200 virtual users over 10 seconds and holds them for 45 seconds. They send transfers as fast as the answers come back, with no pause between requests.
   - About 70% of transfers go between the 4 hot accounts, in a random direction. A to B and B to A run at the same moment and queue on the same row locks, which is the case the lock ordering is there for.
   - About 10% of iterations send 3 identical copies of one transfer at the same moment, with the same token and the same `Idempotency-Key`. k6 checks that exactly one copy gets `201`, that the others get `200` with `Idempotent-Replayed: true`, and that all copies return the same operation id.
4. `load/verify.py` then checks the database and compares it with k6's counts.

### Results

From [run 44011591187](https://github.com/AyerPrabin/jd-ledger/actions/runs/44011591187) (commit `cbe2b09`, 17 Nov 2026). The full k6 summary is attached to the run as an artifact.

| | |
| --- | --- |
| Peak virtual users | 200 |
| HTTP requests | 26,043 in 60 s |
| Requests per second | 434 |
| Latency p50 / p95 / p99 | 543 / 578 / 591 ms |
| Latency max | 683 ms |
| Failed requests | 0.00% (0 errors, 0 timeouts) |
| New transfers (`201`) | 21,781 |
| Idempotent replays (`200`) | 4,262 |
| Duplicate groups sent / applied exactly once | 2,131 / 2,131 |
| PostgreSQL deadlocks | 0 |
| Retries | none. The API does not retry, and it did not need to |
| k6 checks passed | 28,174 of 28,174 |

Latency is mostly time spent queueing. 200 users always waiting on one Python worker means each request waits about 200 / 434 = 0.46 s, which is close to the median. The p99 is only 48 ms above the median, so no request got stuck behind a lock for long.

Every database check passed after the run:

- every operation's entries sum to zero, and every operation has exactly 2 entries
- no player balance is below zero
- `reconcile_mismatches` is empty, so every balance equals its ledger sum
- on every account, `balance_after` is a correct running total in entry order. A lost update would break this.
- the sum of all balances is 0, and the players still hold exactly the 8,600,000 points they were seeded with
- the only credits are the 50 seed credits, so no points were created
- no idempotency key appears twice
- the database holds exactly 21,781 transfers, the same as the number of `201` responses k6 received
- the duplicate keys produced exactly 2,131 operations, one for each group

### What the first run found

The first run ([44011129018](https://github.com/AyerPrabin/jd-ledger/actions/runs/44011129018)) failed, on the same runner type and with the same script. It managed only 14 requests a second. 80 requests returned `500` with `PoolTimeout`, 76 got no answer within k6's 60 s timeout, and p95 latency was 60 s. Even so, every database check passed: 0 deadlocks and no money lost. 4 transfers committed after the client had already given up waiting (709 rows against 705 `201` responses). That is exactly the case where a client retries with the same `Idempotency-Key` and gets the stored result back.

The cause was in the API layer, not the locks. `get_conn` was a plain `def` dependency, so FastAPI ran it on its pool of about 40 worker threads, and each request waited for a database connection inside `pool.getconn()` on one of those threads. With 200 users, all 40 threads were soon waiting there. The 10 requests that did hold connections then had no thread left to finish on, so nothing moved until the 30 s pool timeout fired. Now requests wait for a pool slot on an `asyncio.Semaphore`, on the event loop, and only take a worker thread once a connection is free (commit `cbe2b09`). The pool size and everything else stayed the same. The second run above is after the fix.

### Limits of this test

- It is a single run on one GitHub-hosted `ubuntu-latest` runner. The job printed 4 vCPU, 15 GB RAM, Intel Xeon 6975P-C. Shared runners vary, so treat the numbers as a rough guide, not a benchmark.
- k6, the API and PostgreSQL all run on that one machine and compete for the same CPUs. There is no network latency between them.
- One uvicorn worker and a pool of 10 connections, as shipped. More workers would very likely raise throughput, but I have not measured that.
- The database is fresh and small, with 50 accounts and about 22,000 operations. It does not show how things behave with millions of rows or a cold cache.
- Only `POST /transfers` is tested. The hot accounts hold so much that the insufficient-funds path (`422`) never triggered.
- Auth is part of every request (a token lookup and a sha256), so the numbers include it.

## What I'd add next

- An `entry_fee` / `prize` operation for whole events, so paying out 50/30/20 to three teams is one transaction
- Token expiry and a proper login, or reuse the platform's existing auth
- Rate limiting on `POST /transfers`
- A scheduled job that runs reconcile and alerts if it is ever not empty
- A load test against a single event pool account receiving hundreds of entry fees at once, and with more uvicorn workers, to see where the row locks start to hurt

## Limitations

- The `balance` column is a cache that the Python code keeps in step with the ledger. A database trigger could maintain it instead. I kept it in the code so the locking is easy to follow, and the reconcile view catches any drift.
- Credits and debits always go against the single `issuance` account. A real system would have separate accounts for top-ups, fees and prizes.
- Points are whole numbers (`BIGINT`). There are no currencies, decimals or exchange rates.
- Old idempotency keys are kept forever. A real service would expire them after a while.
- The append-only trigger stops `UPDATE` and `DELETE`, but not `TRUNCATE` or a superuser who disables triggers. The tests rely on `TRUNCATE` to reset the database. In a real deployment the app's database user would not own the tables.
- Everything runs in one PostgreSQL database, so there are no distributed transactions to worry about.

## Licence

MIT, see [LICENSE](LICENSE).
