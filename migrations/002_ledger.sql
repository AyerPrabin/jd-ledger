-- One row per operation (credit, debit, transfer or refund). The
-- idempotency key makes a retried request return the first result instead
-- of moving points twice.

CREATE TABLE operations (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('credit', 'debit', 'transfer', 'refund')),
    idempotency_key  TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) BETWEEN 1 AND 200),
    -- sha256 of the request, so the same key with a different body is caught
    request_hash     TEXT NOT NULL,
    -- A refund points at the operation it reverses. UNIQUE means an
    -- operation can be refunded at most once, even under concurrency.
    refund_of        BIGINT UNIQUE REFERENCES operations (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT operations_refund_of_only_for_refunds
        CHECK ((kind = 'refund') = (refund_of IS NOT NULL))
);

-- Double entry: every operation writes two or more entries that add up to
-- zero. Positive amount = points in, negative = points out.
CREATE TABLE ledger_entries (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operation_id   BIGINT NOT NULL REFERENCES operations (id),
    account_id     BIGINT NOT NULL REFERENCES accounts (id),
    amount         BIGINT NOT NULL CHECK (amount <> 0),
    balance_after  BIGINT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ledger_entries_account_id_idx ON ledger_entries (account_id, id);
CREATE INDEX ledger_entries_operation_id_idx ON ledger_entries (operation_id);

-- The ledger is append-only. Mistakes are fixed with a new refund
-- operation, never by editing history.
CREATE FUNCTION reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION reject_change();

CREATE TRIGGER operations_append_only
    BEFORE UPDATE OR DELETE ON operations
    FOR EACH ROW EXECUTE FUNCTION reject_change();

-- Checked at COMMIT, after all entries of the operation are in.
CREATE FUNCTION check_operation_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    total BIGINT;
BEGIN
    SELECT sum(amount) INTO total FROM ledger_entries WHERE operation_id = NEW.operation_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'operation % does not balance (sum is %)', NEW.operation_id, total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER ledger_entries_balanced
    AFTER INSERT ON ledger_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION check_operation_balanced();
