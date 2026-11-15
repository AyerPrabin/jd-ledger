-- Accounts whose cached balance does not match the sum of their entries.
-- LEFT JOIN so an account with no entries but a non-zero balance shows up.
-- This should always be empty. sum() of bigint gives numeric, hence the cast.

CREATE VIEW reconcile_mismatches AS
SELECT a.id                       AS account_id,
       a.balance                  AS balance,
       COALESCE(sum(e.amount), 0)::bigint AS ledger_sum
FROM accounts a
LEFT JOIN ledger_entries e ON e.account_id = a.id
GROUP BY a.id, a.balance
HAVING a.balance <> COALESCE(sum(e.amount), 0);
