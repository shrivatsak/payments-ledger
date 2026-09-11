# payments-ledger

[![CI](https://github.com/shrivatsak/payments-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/shrivatsak/payments-ledger/actions/workflows/ci.yml)

A small payments API I built to understand how real payment systems avoid double charging and double spending. You can create accounts, deposit money, transfer between accounts, refund a transfer and see history. The interesting bits are idempotency keys (retrying a request never charges twice), a double-entry ledger, and row locks so concurrent transfers can't overdraw an account. Money is stored as integer paise, never floats.

FastAPI + psycopg 3 with plain SQL

## Run it

```bash
docker compose up --build
```

Open http://localhost:8000. There's a "Simulate network retry" button that sends the same transfer twice with the same key, so you can see the second one get replayed and the balance only change once.

Endpoints:

| | | |
|---|---|---|
| `POST /accounts` | | create account |
| `GET /accounts`, `GET /accounts/{id}` | | accounts with balances |
| `GET /accounts/{id}/transactions` | | ledger history |
| `POST /deposits` | needs `Idempotency-Key` | |
| `POST /transfers` | needs `Idempotency-Key` | |
| `POST /payments/{id}/refund` | needs `Idempotency-Key` | full refund of a transfer |
| `GET /payments/{id}` | | |
| `GET /health` | | pings the db |

Errors look like `{"error": {"code": "INSUFFICIENT_FUNDS", "message": "..."}}`. A replayed request comes back with the original status and body plus an `Idempotent-Replayed: true` header.

## Tests

You need a real Postgres running (the tests depend on actual locking behaviour, so mocking it would be pointless):

```bash
docker compose up -d db
pip install .
pytest -v
```

## Design notes

**Idempotency keys.** Every request that moves money has to send an `Idempotency-Key`. Inside the transaction the first thing I do is `INSERT INTO payments ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. If I get an id back it's a new request. If not, the key was already used, so I return the response stored on that row and don't touch anything. This includes failures: if a transfer failed with 402 and you retry it with the same key, you get the same 402 back even if the account has money now. I also store a hash of the request body, so reusing a key with a different body gives a 409 instead of a wrong answer.

If two requests with the same key arrive at the same time, the second INSERT just blocks on the unique index until the first commits, then sees the key is taken. Postgres does that part, not me.

**Double-entry ledger.** There's no balance column. Every payment writes two rows to `ledger_entries` (minus on one account, plus on the other) and balance is `SUM(amount_paise)`. Deposits come out of a system account called `EXTERNAL_FUNDING` that's allowed to go negative, so even deposits are balanced. Every money test asserts that the sum of all ledger entries is 0.

**Locking.** Before reading a sender's balance, the transfer does `SELECT ... FOR UPDATE` on both account rows, lowest id first. The lock makes concurrent transfers from the same account wait for each other, and the fixed order means A→B and B→A can't deadlock.

The bit I got wrong first: I took those locks *after* the idempotency INSERT. But `payments` has foreign keys to `accounts`, so the INSERT was already taking a weaker `KEY SHARE` lock on both accounts, and upgrading to `FOR UPDATE` from two transactions at once deadlocks. The concurrency tests caught it. Locks now go before the INSERT.

**Integer paise.** Amounts are `int` / `BIGINT`. Floats can't represent 0.1 exactly and a ledger that's off by a paise is broken. Rupees only appear in the dashboard, computed with integer division.

## Test results

48 tests, all passing on every push (CI runs them against a `postgres:16` container).

- `test_accounts.py` (14), `test_transfers.py` (10), `test_idempotency.py` (10), `test_refunds.py` (10): the normal API behaviour.
- `test_concurrency.py` (4): these call the service layer from a thread pool with a barrier so everything fires at once.
  - 50 transfers of 100 paise from an account with 1,000 → exactly 10 succeed, balance ends at 0.
  - 20 requests with the same key at once → one payment row, two ledger entries, all 20 get the same payment id.
  - 50 A→B and 50 B→A at once → no deadlock, money conserved.
  - 10 refunds of different payments between the same two accounts at once → all succeed.
