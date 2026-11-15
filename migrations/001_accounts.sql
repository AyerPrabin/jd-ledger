-- Accounts hold a cached balance. The ledger is the source of truth and
-- the balance must always equal the sum of the account's ledger entries
-- (see the reconcile view in 003).

CREATE TABLE accounts (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    -- Only system accounts (the one that issues points) may go below zero.
    allow_negative  BOOLEAN NOT NULL DEFAULT FALSE,
    balance         BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT accounts_balance_not_negative CHECK (allow_negative OR balance >= 0)
);

-- Points are loaded onto player accounts from this account, so the sum of
-- every balance in the table is always zero.
INSERT INTO accounts (name, allow_negative) VALUES ('issuance', TRUE);
