"""API tokens for player accounts.

A token looks like "<account id>.<random secret>". The database keeps only
sha256(secret), so the account id is used to find the row and then the
hashes are compared with hmac.compare_digest.

Issue a token with: python -m jd_ledger.auth <account id>
"""

import hashlib
import hmac
import secrets
import sys

from jd_ledger import ledger
from jd_ledger.db import connect
from jd_ledger.ledger import Conn


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def issue_token(conn: Conn, account_id: int) -> str:
    """Make a new token for the account. Any older token stops working."""
    account = ledger.get_account(conn, account_id)
    if account.allow_negative:
        raise ledger.SystemAccount("system accounts cannot have API tokens")
    secret = secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE accounts SET api_token_hash = %s WHERE id = %s", (_hash(secret), account_id)
    )
    return f"{account_id}.{secret}"


def account_for_token(conn: Conn, token: str) -> int | None:
    """Return the account id the token belongs to, or None."""
    account_part, _, secret = token.partition(".")
    if not secret or len(account_part) > 18:
        return None
    if not (account_part.isascii() and account_part.isdigit()):
        return None
    row = conn.execute(
        "SELECT api_token_hash FROM accounts WHERE id = %s", (int(account_part),)
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        return None
    if hmac.compare_digest(row[0].encode(), _hash(secret).encode()):
        return int(account_part)
    return None


def is_admin(token: str, admin_token: str | None) -> bool:
    if not admin_token:
        return False  # admin endpoints are off unless ADMIN_TOKEN is set
    return hmac.compare_digest(token.encode(), admin_token.encode())


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("usage: python -m jd_ledger.auth <account id>")
    with connect() as conn:
        print(issue_token(conn, int(sys.argv[1])))


if __name__ == "__main__":
    main()
