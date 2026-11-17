"""Create the accounts for the load test and write their tokens for k6.

There is no HTTP endpoint for creating accounts or loading points (on
purpose, see the README), so this uses the ledger code directly.

Writes load/out/seed.json. That file holds live API tokens, so it is
gitignored and never uploaded as an artifact.

Run with: python -m load.seed   (needs DATABASE_URL, an empty database)
"""

import json
import sys
from pathlib import Path

from jd_ledger import auth, ledger
from jd_ledger.db import connect
from jd_ledger.migrate import migrate

OUT = Path(__file__).resolve().parent / "out"

# A few "hot" accounts that most transfers go between, in both directions,
# so many requests queue on the same row locks. They hold far more than the
# test can move out of them, so insufficient funds should not happen there.
HOT_ACCOUNTS = 4
HOT_POINTS = 1_000_000
# Quieter accounts that only some transfers touch.
COLD_ACCOUNTS = 46
COLD_POINTS = 100_000


def main() -> None:
    with connect() as conn:
        migrate(conn)
        row = conn.execute("SELECT count(*) FROM operations").fetchone()
        if row is None or row[0] != 0:
            sys.exit("seed needs an empty database (operations is not empty)")

        def make(name: str, points: int) -> dict[str, object]:
            account_id = ledger.create_account(conn, name)
            ledger.credit(conn, account_id, points, idempotency_key=f"seed-{name}")
            return {"id": account_id, "token": auth.issue_token(conn, account_id)}

        hot = [make(f"load_hot_{i}", HOT_POINTS) for i in range(1, HOT_ACCOUNTS + 1)]
        cold = [make(f"load_cold_{i}", COLD_POINTS) for i in range(1, COLD_ACCOUNTS + 1)]

        row = conn.execute(
            "SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()"
        ).fetchone()
        deadlocks_before = int(str(row[0])) if row else 0

    OUT.mkdir(exist_ok=True)
    seed = {
        "hot": hot,
        "cold": cold,
        "player_total": HOT_ACCOUNTS * HOT_POINTS + COLD_ACCOUNTS * COLD_POINTS,
        "players": HOT_ACCOUNTS + COLD_ACCOUNTS,
        "deadlocks_before": deadlocks_before,
    }
    (OUT / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    print(f"seeded {seed['players']} accounts, {seed['player_total']} points in total")


if __name__ == "__main__":
    main()
