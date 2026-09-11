# Payments Ledger

[![CI](https://github.com/shrivatsak/payments-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/shrivatsak/payments-ledger/actions/workflows/ci.yml)

A small payments backend that gets the hard parts right. Users create accounts, deposit money, transfer between accounts, refund transfers and view history. Every request that moves money carries an `Idempotency-Key`, so a retried request never charges twice; every movement is recorded as a balanced double-entry in an append-only ledger; concurrent transfers are serialised with row-level locks so an account can never be overdrawn; and amounts are integer paise stored as `BIGINT`, never floats. Single currency (INR), no authentication, no ORM: FastAPI on top of raw parameterised SQL against PostgreSQL 16.

## Architecture

```mermaid
flowchart LR
    B["Browser<br/>static dashboard, vanilla JS"]
    A["FastAPI + Uvicorn<br/>main.py: routes<br/>service.py: SQL + business logic"]
    P[("PostgreSQL 16<br/>accounts · payments · ledger_entries")]
    B -- "fetch() + Idempotency-Key" --> A
    A -- "psycopg 3 pool<br/>one transaction per request" --> P
```

## Running it

```bash
docker compose up --build
```

Then open <http://localhost:8000>. The dashboard has a **Simulate network retry** button that sends the same transfer twice with the same key and shows, side by side, that the second response was replayed and the balance changed once.

The API is also usable directly:

```bash
curl -X POST localhost:8000/accounts -H 'Content-Type: application/json' -d '{"owner_name":"Asha"}'
curl -X POST localhost:8000/deposits -H 'Content-Type: application/json' \
     -H 'Idempotency-Key: 4c1e...' -d '{"account_id":2,"amount_paise":50000}'
```

| Method | Path | Idempotency-Key | Purpose |
|---|---|---|---|
| `POST` | `/accounts` | — | create an account |
| `GET` | `/accounts`, `/accounts/{id}` | — | accounts with live balances |
| `GET` | `/accounts/{id}/transactions` | — | ledger entries, newest first |
| `POST` | `/deposits` | required | fund an account |
| `POST` | `/transfers` | required | move money between accounts |
| `POST` | `/payments/{id}/refund` | required | full refund of a completed transfer |
| `GET` | `/payments/{id}` | — | payment details |
| `GET` | `/health` | — | checks the database connection |

Errors are always `{"error": {"code": "...", "message": "..."}}`. A replayed request returns the stored status and body plus the header `Idempotent-Replayed: true`.

## Running the tests

The tests need a real PostgreSQL (see [why](#why-real-postgres-in-tests)):

```bash
docker compose up -d db
pip install .
pytest -v
```

`DATABASE_URL` defaults to `postgresql://ledger:ledger@localhost:5432/ledger`. CI runs the same steps against a `postgres:16` service container.

## Design decisions

### Idempotency keys

Every money-moving request must carry an `Idempotency-Key`. The first thing the handler does inside its transaction is `INSERT INTO payments ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. If a row comes back, the request is new and the work proceeds. If nothing comes back, the key is taken: the handler reads the stored `response_code` and `response_body` from that row and returns them unchanged, without touching balances. The stored response includes failures, so a transfer that was rejected with `402 INSUFFICIENT_FUNDS` keeps returning that 402 on retry even after the account is funded: one key, one outcome, forever.

The same key with a different body is a client bug, not a retry. A SHA-256 of the endpoint path plus the canonical (sorted-key) JSON body is stored alongside the key, and a mismatch is rejected with `409 IDEMPOTENCY_KEY_MISMATCH`.

The concurrent case, where the retry arrives while the original is still mid-transaction, is handled by Postgres rather than by application code: the second `INSERT` blocks on the unique index until the first transaction commits, then finds the key taken and replays.

### Double-entry ledger

There is no balance column. `ledger_entries` is append-only, every payment writes exactly two rows that sum to zero (a debit on the payer, a credit on the payee), and an account's balance is `SUM(amount_paise)` over its rows. Deposits are not an exception: they move money *from* a seeded `EXTERNAL_FUNDING` system account, the only account allowed to go negative, whose balance is simply the total money that has entered the system. A refund is a new payment that runs the original backwards, not an edit to the original.

This gives one invariant that every money-moving test asserts: **the sum of all ledger entries is always exactly zero.** Money is moved, never created or destroyed.

### Row locking with ordered locks

A transfer reads the sender's balance and then writes ledger entries. Without a lock, two concurrent transfers can both read a sufficient balance and both write, overdrawing the account. So before reading the balance, the handler takes `SELECT ... FOR UPDATE` on both account rows. The row is not holding the balance; it is acting as a mutex for that account. A second transfer out of the same account blocks inside the database until the first commits, and its balance read then sees the committed debit.

Locks are always taken in ascending account id. Two opposite transfers (A→B and B→A) that each locked their own sender first would each hold the row the other needs. A fixed global order makes that cycle impossible.

One non-obvious detail: the locks are taken *before* the idempotency `INSERT`, not after. `payments` has foreign keys to `accounts`, and inserting a payment row silently takes a `FOR KEY SHARE` lock on both referenced accounts. Asking for `FOR UPDATE` afterwards is a lock upgrade, and two transactions each holding `KEY SHARE` while waiting to upgrade deadlock. Lock ordering only works if you account for every lock a transaction takes, including the ones the database takes on your behalf. Refunds lock the original payment row first, so two refunds of the same payment serialise, and a partial unique index (`payments(refund_of) WHERE status = 'COMPLETED'`) backstops the application check.

### Integer paise

Amounts are `int` in Python and `BIGINT` in SQL, denominated in paise. `0.1 + 0.2 != 0.3` in floating point, and a payments system that can be off by a paise is wrong. Conversion to rupees happens only for display, and even there with integer division and remainder.

### Why real Postgres in tests

Every property this project claims — the unique-index wait that dedupes concurrent retries, the row lock that prevents overdraw, the deadlock that ordered locking avoids, the rollback that keeps rejected requests out of the ledger — is database behaviour. A mock returns whatever it is told to and cannot deadlock, so it cannot prove a deadlock was fixed. The concurrency tests below caught a real lock-upgrade deadlock during development that no mock would have surfaced.

## Test results

**48 tests, all passing**, run against PostgreSQL 16 on every push by [GitHub Actions](.github/workflows/ci.yml).

| File | Tests | Covers |
|---|---:|---|
| `test_accounts.py` | 14 | account creation, deposits, balances, history, validation, dashboard |
| `test_transfers.py` | 10 | happy path, insufficient funds stored as `FAILED`, same-account and unknown-account rejection |
| `test_idempotency.py` | 10 | replay with identical response, key/body mismatch, cross-endpoint reuse, failed-response replay |
| `test_refunds.py` | 10 | refund once, second refund rejected, refunding deposits/failures/refunds rejected, refund when money already spent |
| `test_concurrency.py` | 4 | the four properties below |

The concurrency tests call the service layer directly from a `ThreadPoolExecutor`, with a barrier releasing every thread at the same instant and each thread on its own pooled connection:

- **Double-spend**: an account holding 1,000 paise receives 50 concurrent 100-paise transfers with distinct keys. Exactly 10 succeed, 40 fail with `INSUFFICIENT_FUNDS`, the balance ends at exactly 0 and never goes negative.
- **Duplicate retry**: 20 concurrent requests with the *same* key. Exactly one payment row, exactly two ledger entries, all 20 responses carry the same payment id, and exactly 19 are marked replayed.
- **Deadlock**: 50 A→B and 50 B→A transfers interleaved. No exceptions, all succeed, both balances return to their starting values.
- **Concurrent refunds**: 10 refunds of 10 different payments between the same two accounts, the shape with no payment-row contention to hide behind. All succeed.

All four assert the global invariant afterwards.

## Layout

```
app/
  main.py         routes: validate, call service, return
  service.py      all SQL and business logic
  idempotency.py  key validation and request hashing
  errors.py       error codes and the exception → HTTP mapping
  models.py       Pydantic request/response models
  db.py           connection pool
  static/index.html
db/schema.sql     tables, constraints, indexes, seeded system account
tests/
```
