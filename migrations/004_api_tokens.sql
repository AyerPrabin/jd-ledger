-- Each player account can have one API token. Only a sha256 hash of the
-- secret part is stored, so a copy of the database does not give anyone
-- working tokens. System accounts (allow_negative) never get a token.

ALTER TABLE accounts ADD COLUMN api_token_hash TEXT;

ALTER TABLE accounts ADD CONSTRAINT accounts_no_token_for_system
    CHECK (NOT (allow_negative AND api_token_hash IS NOT NULL));
