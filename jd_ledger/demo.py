"""Create a few made-up accounts with some points so the API has data.

Safe to run more than once: accounts are only created if missing and the
credits use fixed idempotency keys, so a second run moves nothing. Each
run prints a fresh API token per account (the old ones stop working).

Run with: python -m jd_ledger.demo
"""

from jd_ledger import auth, ledger
from jd_ledger.db import connect

DEMO_ACCOUNTS = {
    "player_alpha": 1000,
    "player_bravo": 500,
    "player_charlie": 250,
    "event_pool_demo": 0,
}


def main() -> None:
    with connect() as conn:
        for name, points in DEMO_ACCOUNTS.items():
            row = conn.execute("SELECT id FROM accounts WHERE name = %s", (name,)).fetchone()
            account_id = ledger.create_account(conn, name) if row is None else int(str(row[0]))
            if points:
                ledger.credit(conn, account_id, points, idempotency_key=f"demo-load-{name}")
            token = auth.issue_token(conn, account_id)
            balance = ledger.get_account(conn, account_id).balance
            print(f"{account_id:>3}  {name:<16} {balance:>6}  {token}")


if __name__ == "__main__":
    main()
