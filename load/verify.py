"""Check the database after the k6 run and write a short report.

Every check here is a rule the ledger promises. The k6 counters from
load/out/summary.json are compared with what is actually in the database.
The report goes to stdout and, in GitHub Actions, to the job summary.
Exits 1 if any check fails.

Run with: python -m load.verify   (needs DATABASE_URL)
"""

import json
import os
import sys
from pathlib import Path

from jd_ledger.db import connect
from jd_ledger.ledger import Conn

OUT = Path(__file__).resolve().parent / "out"


def scalar(conn: Conn, sql: str) -> int:
    row = conn.execute(sql.encode()).fetchone()
    return int(str(row[0] if row and row[0] is not None else 0))


def main() -> None:
    seed = json.loads((OUT / "seed.json").read_text(encoding="utf-8"))
    summary_path = OUT / "summary.json"
    k6 = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None

    def k6_value(metric: str, stat: str) -> float:
        if k6 is None or metric not in k6["metrics"]:
            return 0
        return float(k6["metrics"][metric]["values"].get(stat, 0))

    with connect() as conn:

        def q(sql: str) -> int:
            return scalar(conn, sql)

        transfers = q("SELECT count(*) FROM operations WHERE kind = 'transfer'")
        dup_ops = q(
            "SELECT count(*) FROM operations"
            " WHERE kind = 'transfer' AND idempotency_key LIKE 'account-%:dup-%'"
        )
        deadlocks = (
            q("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
            - seed["deadlocks_before"]
        )
        checks = [
            (
                "every operation's entries sum to zero",
                q(
                    "SELECT count(*) FROM (SELECT operation_id FROM ledger_entries"
                    " GROUP BY operation_id HAVING sum(amount) <> 0) x"
                )
                == 0,
            ),
            (
                "every operation has exactly 2 entries",
                q(
                    "SELECT count(*) FROM operations o WHERE (SELECT count(*) FROM ledger_entries e"
                    " WHERE e.operation_id = o.id) <> 2"
                )
                == 0,
            ),
            (
                "no player balance below zero",
                q("SELECT count(*) FROM accounts WHERE NOT allow_negative AND balance < 0") == 0,
            ),
            (
                "every balance equals its ledger sum (reconcile view empty)",
                q("SELECT count(*) FROM reconcile_mismatches") == 0,
            ),
            (
                "balance_after is a correct running total on every account (no lost update)",
                q(
                    "SELECT count(*) FROM (SELECT balance_after, sum(amount) OVER"
                    " (PARTITION BY account_id ORDER BY id) AS running FROM ledger_entries) x"
                    " WHERE balance_after <> running"
                )
                == 0,
            ),
            ("sum of all balances is zero", q("SELECT sum(balance) FROM accounts") == 0),
            (
                f"player total unchanged ({seed['player_total']:,} points)",
                q("SELECT sum(balance) FROM accounts WHERE NOT allow_negative")
                == seed["player_total"],
            ),
            (
                "no points created: only the seed credits, no other credit or debit",
                q("SELECT count(*) FROM operations WHERE kind <> 'transfer'") == seed["players"],
            ),
            (
                "each idempotency key used once",
                q(
                    "SELECT count(*) FROM (SELECT idempotency_key FROM operations"
                    " GROUP BY idempotency_key HAVING count(*) > 1) x"
                )
                == 0,
            ),
            ("no deadlocks reported by PostgreSQL", deadlocks == 0),
        ]
        if k6 is not None:
            created = int(k6_value("transfers_created", "count"))
            unknown = int(k6_value("transfers_no_response", "count"))
            dup_applied = int(k6_value("dup_groups_applied", "count"))
            checks += [
                (
                    f"transfers in the database ({transfers:,}) match k6's 201 responses"
                    f" ({created:,}, plus {unknown} with no response)",
                    created <= transfers <= created + unknown,
                ),
                (
                    f"duplicate groups applied once: {dup_ops:,} operations for"
                    f" {dup_applied:,} groups that got one 201",
                    dup_applied <= dup_ops <= dup_applied + unknown,
                ),
            ]

    lines = ["## Load test", ""]
    if k6 is not None:
        dur = "http_req_duration"
        lines += [
            "| k6 metric | value |",
            "| --- | --- |",
            f"| peak virtual users | {int(k6_value('vus_max', 'value'))} |",
            f"| HTTP requests | {int(k6_value('http_reqs', 'count')):,} |",
            f"| requests per second | {k6_value('http_reqs', 'rate'):.1f} |",
            f"| latency p50 / p95 / p99 | {k6_value(dur, 'med'):.0f} /"
            f" {k6_value(dur, 'p(95)'):.0f} / {k6_value(dur, 'p(99)'):.0f} ms |",
            f"| latency max | {k6_value(dur, 'max'):.0f} ms |",
            f"| failed requests (not 200/201/422) | {k6_value('http_req_failed', 'rate'):.2%} |",
            f"| 201 new transfers | {int(k6_value('transfers_created', 'count')):,} |",
            f"| 200 idempotent replays | {int(k6_value('transfers_replayed', 'count')):,} |",
            f"| 422 insufficient funds | {int(k6_value('transfers_insufficient', 'count')):,} |",
            f"| unexpected status | {int(k6_value('unexpected_status', 'count')):,} |",
            f"| no response | {int(k6_value('transfers_no_response', 'count')):,} |",
            f"| duplicate groups sent (3 copies each) | {int(k6_value('dup_groups', 'count')):,} |",
            f"| k6 checks passed | {k6_value('checks', 'rate'):.2%} |",
            "",
        ]
    else:
        lines += ["No k6 summary found, so only the database was checked.", ""]
    lines += [
        f"Deadlocks: {deadlocks}. Retries: none (the API does not retry).",
        "",
        "| invariant | result |",
        "| --- | --- |",
    ]
    lines += [f"| {name} | {'pass' if ok else '**FAIL**'} |" for name, ok in checks]
    report = "\n".join(lines) + "\n"
    print(report)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(report)
    if not all(ok for _, ok in checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
