CREATE TABLE accounts (
    id         BIGSERIAL PRIMARY KEY,
    owner_name TEXT NOT NULL,
    is_system  BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE payments (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE
                    CHECK (char_length(idempotency_key) BETWEEN 1 AND 255),
    request_hash    TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('DEPOSIT', 'TRANSFER', 'REFUND')),
    from_account_id BIGINT NOT NULL REFERENCES accounts(id),
    to_account_id   BIGINT NOT NULL REFERENCES accounts(id),
    amount_paise    BIGINT NOT NULL CHECK (amount_paise > 0),
    status          TEXT NOT NULL CHECK (status IN ('COMPLETED', 'FAILED')),
    failure_reason  TEXT,
    refund_of       BIGINT REFERENCES payments(id),
    response_code   INT NOT NULL,
    response_body   JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (from_account_id <> to_account_id)
);

-- Database-level guarantee that a payment can never have two successful refunds,
-- even if the application check is bypassed or races.
CREATE UNIQUE INDEX one_completed_refund ON payments(refund_of)
    WHERE refund_of IS NOT NULL AND status = 'COMPLETED';

CREATE TABLE ledger_entries (
    id           BIGSERIAL PRIMARY KEY,
    payment_id   BIGINT NOT NULL REFERENCES payments(id),
    account_id   BIGINT NOT NULL REFERENCES accounts(id),
    amount_paise BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ledger_entries_account_id_idx ON ledger_entries(account_id);

-- Deposits move money out of this account, so every deposit is still a balanced
-- double entry. It is the only account allowed to hold a negative balance.
INSERT INTO accounts (owner_name, is_system) VALUES ('EXTERNAL_FUNDING', true);
