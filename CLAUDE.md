# CLAUDE.md — Idempotent Payments Ledger API

## Who I am and why this project exists

I'm Shrivatsa, a third-year student preparing for a Software Engineering Intern interview at Visa.
This project must be **finished today**, and I must be able to **explain and defend every line** in the interview.

That means:
- Keep the code **simple, readable, and boring**. No clever abstractions, no extra layers.
- After each milestone, **explain what you built in plain language**: the key concepts, why each design choice was made, and what an interviewer might ask about it.
- Add short comments at the critical points (locking, idempotency, transactions) explaining **why**, not what.

## Project summary

A small payments backend: users create accounts, deposit money, transfer money between accounts, refund transfers, and view transaction history.
It demonstrates the core ideas of real payment systems:

1. **Idempotency keys**: a retried request never charges twice.
2. **Double-entry ledger**: every movement of money writes two entries that sum to zero.
3. **ACID transactions + row-level locking**: no double-spend under concurrent requests.
4. **Money as integers**: amounts stored in paise (`BIGINT`), never floats.

Single currency (INR) only. No authentication. Multi-currency, auth, and partial refunds are **out of scope**.

## Tech stack (do not add dependencies without asking me)

- Python 3.13
- FastAPI + Uvicorn
- PostgreSQL 16
- psycopg 3 (`psycopg[binary]`) + `psycopg_pool` — **raw parameterized SQL, no ORM** (I want to be able to explain the SQL)
- Pydantic v2 for request/response models
- pytest + httpx (FastAPI TestClient)
- ruff for linting
- Docker + Docker Compose
- GitHub Actions for CI
- Dashboard: one static HTML file with vanilla JavaScript (no frameworks, no build step)

## Repository layout

```
payments-ledger/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
├── db/
│   └── schema.sql            # tables, constraints, indexes, seed of the system account
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app + routes (thin: validate, call service, return)
│   ├── db.py                 # connection pool setup
│   ├── models.py             # Pydantic request/response models
│   ├── errors.py             # error codes + exception → HTTP response mapping
│   ├── idempotency.py        # request hashing helper
│   ├── service.py            # ALL business logic and SQL lives here
│   └── static/
│       └── index.html        # dashboard
├── tests/
│   ├── conftest.py
│   ├── test_accounts.py
│   ├── test_transfers.py
│   ├── test_idempotency.py
│   ├── test_refunds.py
│   └── test_concurrency.py
└── .github/
    └── workflows/
        └── ci.yml
```

## Database design (db/schema.sql)

### accounts
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| owner_name | TEXT NOT NULL | |
| is_system | BOOLEAN NOT NULL DEFAULT false | true only for the EXTERNAL_FUNDING account |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Seed one row: `owner_name = 'EXTERNAL_FUNDING', is_system = true`.
Deposits move money **from** this account, so every deposit is still a balanced double entry.
The system account is the **only** account allowed to have a negative balance.

### payments
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| idempotency_key | TEXT NOT NULL UNIQUE | max 255 chars |
| request_hash | TEXT NOT NULL | SHA-256 of endpoint + canonical request body |
| type | TEXT NOT NULL | CHECK IN ('DEPOSIT', 'TRANSFER', 'REFUND') |
| from_account_id | BIGINT NOT NULL REFERENCES accounts(id) | |
| to_account_id | BIGINT NOT NULL REFERENCES accounts(id) | |
| amount_paise | BIGINT NOT NULL | CHECK (amount_paise > 0) |
| status | TEXT NOT NULL | CHECK IN ('COMPLETED', 'FAILED') |
| failure_reason | TEXT NULL | e.g. 'INSUFFICIENT_FUNDS' |
| refund_of | BIGINT NULL REFERENCES payments(id) | set only for REFUND |
| response_code | INT NOT NULL | stored HTTP status, returned on replay |
| response_body | JSONB NOT NULL | stored response, returned on replay |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Also add: `CHECK (from_account_id <> to_account_id)` and a partial unique index so a transfer can have at most one completed refund:
`CREATE UNIQUE INDEX one_completed_refund ON payments(refund_of) WHERE refund_of IS NOT NULL AND status = 'COMPLETED';`

### ledger_entries
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PRIMARY KEY | |
| payment_id | BIGINT NOT NULL REFERENCES payments(id) | |
| account_id | BIGINT NOT NULL REFERENCES accounts(id) | |
| amount_paise | BIGINT NOT NULL | signed: negative = debit, positive = credit |
| created_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |

Index on `ledger_entries(account_id)`.

**Balance of an account = `SUM(amount_paise)` of its ledger entries.** There is no stored balance column; the ledger is the source of truth.

**Global invariant: the sum of ALL ledger entries is always exactly 0.**

## API specification

All money amounts are integers in paise. All errors use this shape:

```json
{ "error": { "code": "INSUFFICIENT_FUNDS", "message": "human-readable text" } }
```

| Method | Path | Idempotency-Key header | Description |
|---|---|---|---|
| GET | `/health` | no | returns `{"status": "ok"}` and checks the DB connection |
| POST | `/accounts` | no | body `{owner_name}` → 201 with account |
| GET | `/accounts` | no | list non-system accounts with balances |
| GET | `/accounts/{id}` | no | account with current balance |
| GET | `/accounts/{id}/transactions?limit=50` | no | ledger entries, newest first, with payment type and counterparty account |
| POST | `/deposits` | **required** | body `{account_id, amount_paise}` |
| POST | `/transfers` | **required** | body `{from_account_id, to_account_id, amount_paise}` |
| POST | `/payments/{id}/refund` | **required** | full refund of a completed TRANSFER |
| GET | `/payments/{id}` | no | payment details |
| GET | `/` | no | serves the dashboard |

Validation: `amount_paise` must be an integer, `> 0` and `<= 10_000_000`. Idempotency key: non-empty, max 255 chars.

### Status codes and error codes

| HTTP | code | stored & replayed? |
|---|---|---|
| 201 | (success) | yes |
| 400 | `MISSING_IDEMPOTENCY_KEY`, `SAME_ACCOUNT` | no |
| 402 | `INSUFFICIENT_FUNDS` | **yes**, payment saved with status FAILED |
| 404 | `ACCOUNT_NOT_FOUND`, `PAYMENT_NOT_FOUND` | no |
| 409 | `IDEMPOTENCY_KEY_MISMATCH` (same key, different request) | no |
| 409 | `REFUND_NOT_ALLOWED` (not a completed TRANSFER, or already refunded) | no |
| 422 | FastAPI's default request-validation error | no |

On a replay, return the **stored** status code and body, plus the header `Idempotent-Replayed: true`.

## Core algorithms (implement exactly like this)

### Idempotent money-moving request (deposits, transfers, refunds)

All steps run inside **one database transaction** (`with conn.transaction():`):

1. Validate the body (Pydantic). Check the referenced accounts exist → 404 if not (nothing stored).
2. Compute `request_hash = sha256(endpoint_path + canonical_json(body))`, where canonical JSON uses sorted keys.
3. `INSERT INTO payments (...) VALUES (...) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`
   - Insert with a placeholder status/response; it gets updated in step 5 inside the same transaction.
   - If a concurrent request with the same key is mid-transaction, Postgres makes this INSERT **wait** on the unique index until the other transaction commits. This is what makes simultaneous duplicates safe.
4. If no id is returned, the key already exists:
   - `SELECT request_hash, response_code, response_body FROM payments WHERE idempotency_key = %s`
   - If the hash differs → 409 `IDEMPOTENCY_KEY_MISMATCH`.
   - Otherwise return the stored response with `Idempotent-Replayed: true`. **Do nothing else.**
5. If an id was returned, this is a new request:
   - **Lock the accounts that will lose money** with `SELECT ... FOR UPDATE`. For transfers, lock **both** accounts in **ascending id order** to prevent deadlocks (A→B and B→A at the same time).
   - Compute the balance with `SUM(amount_paise)` **after** acquiring the lock.
   - Insufficient funds → update the payment to `FAILED` with `failure_reason`, store response 402, return 402.
   - Otherwise insert **two** ledger entries (−amount for the sender, +amount for the receiver), update the payment to `COMPLETED`, store response 201, return 201.
   - Deposits need no balance check or lock: money comes from the system account.

### Refunds

- Lock the original payment row with `SELECT ... FOR UPDATE` first, then lock accounts in ascending id order.
- Allowed only if the original is a `COMPLETED` `TRANSFER` with no `COMPLETED` refund yet → otherwise 409 `REFUND_NOT_ALLOWED`.
- The refund moves the full amount from the original receiver back to the original sender. The receiver needs enough balance → otherwise 402, stored as FAILED.
- The partial unique index is the safety net if the code check is ever bypassed.

### SQL rules

- **Always** use parameterized queries (`%s`). **Never** build SQL with f-strings or string concatenation (SQL injection).
- Isolation level: Postgres default READ COMMITTED + the explicit row locks above.
- Money is always `int` in Python and `BIGINT` in SQL. Never `float`, never `Decimal` for storage.

## Testing requirements

Tests run against a **real PostgreSQL** (locks and unique-index waits cannot be faked with SQLite or mocks).

- `DATABASE_URL` env var; default `postgresql://ledger:ledger@localhost:5432/ledger`.
- A fixture resets the DB before each test: `TRUNCATE ledger_entries, payments, accounts RESTART IDENTITY CASCADE`, then re-seed the system account.
- HTTP behaviour: FastAPI `TestClient`.
- Concurrency tests: call the **service-layer functions directly** from a `ThreadPoolExecutor`, each call using its own pooled connection.
- After every money-moving test, assert the **global invariant** (sum of all ledger entries = 0).

Required test cases:

1. Create account; deposit; balance is correct.
2. Transfer happy path; both balances correct; exactly 2 ledger entries created.
3. Insufficient funds → 402, payment saved as FAILED, **zero** ledger entries for it.
4. Same key + same body sent twice → identical response, second has `Idempotent-Replayed: true`, only one payment row.
5. Same key + different body → 409 `IDEMPOTENCY_KEY_MISMATCH`.
6. Replaying a FAILED (402) request returns the same 402, even if the account has since been funded.
7. Missing Idempotency-Key → 400. Same from/to account → 400. Unknown account → 404. Amount ≤ 0 → 422.
8. **Double-spend test:** account with 1,000 paise; 50 concurrent transfers of 100 paise with different keys → exactly 10 succeed, 40 fail with INSUFFICIENT_FUNDS, final balance 0, never negative.
9. **Duplicate-retry test:** 20 concurrent requests with the **same** key → exactly one payment row, exactly 2 ledger entries, all 20 responses have the same payment id.
10. **Deadlock test:** 50 A→B and 50 B→A transfers concurrently → no errors or exceptions, total money conserved.
11. Refund works once; a second refund (new key) → 409; refunding a FAILED or DEPOSIT payment → 409.
12. Refund when the receiver has already spent the money → 402, stored as FAILED.

## Docker

- `Dockerfile`: `python:3.13-slim`, install requirements, run `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: services `db` (postgres:16 with a healthcheck, `db/schema.sql` mounted into `/docker-entrypoint-initdb.d/`) and `app` (depends on `db` being healthy, port 8000).
- For local testing: `docker compose up -d db`, then run `pytest` from the host.

## CI (.github/workflows/ci.yml)

On every push and pull request to `main`:
1. `ubuntu-latest` with a `postgres:16` service container (user/password/db = `ledger`), with health options.
2. Set up Python 3.13, `pip install -r requirements.txt`.
3. Apply `db/schema.sql` with `psql`.
4. `ruff check .`
5. `pytest -v`

## Dashboard (app/static/index.html)

One page, vanilla JS using `fetch`, clean and simple styling in a `<style>` tag:
- Accounts table with live balances + a "create account" form.
- Deposit and transfer forms. Each submission generates a fresh key with `crypto.randomUUID()`.
- Transaction history for a selected account.
- **"Simulate network retry" button**: sends the same transfer twice with the **same** idempotency key and shows side by side that the second response was replayed and the balance changed only once. This is the demo I'll show in the interview.
- Show error messages from the API clearly.

## README.md (write at the end)

- One-paragraph summary.
- Mermaid architecture diagram (browser → FastAPI → PostgreSQL).
- How to run with Docker Compose, and how to run tests.
- **Design decisions** section: idempotency keys, double-entry ledger, row locking with ordered locks, integer paise, why real Postgres in tests.
- Test results: number of tests and what the concurrency tests prove.
- CI badge.

## Milestones (do ONE at a time, then stop)

- [ ] **M0 — Scaffold:** layout, requirements.txt, .gitignore, .env.example, docker-compose with db only, `db.py`, `/health`.
- [ ] **M1 — Accounts & deposits:** schema.sql, accounts endpoints, deposits (with idempotency), balances, tests 1 and 7.
- [ ] **M2 — Transfers:** locking, double entry, insufficient funds, tests 2 and 3.
- [ ] **M3 — Idempotency hardening:** replay, mismatch, failed-replay, tests 4, 5, 6.
- [ ] **M4 — Refunds:** tests 11 and 12.
- [ ] **M5 — Concurrency tests:** tests 8, 9, 10. All must pass reliably (run them 3 times).
- [ ] **M6 — Docker + CI:** Dockerfile, full compose, GitHub Actions workflow, ruff clean.
- [ ] **M7 — Dashboard:** including the retry-simulation demo.
- [ ] **M8 — README.**

## How to work with me

- Work on **one milestone at a time**. When it's done: run the tests, show me the results, then **stop**.
- At the end of each milestone, give me:
  1. A short summary of the files you created or changed.
  2. A plain-language explanation of the key concepts, with 2–3 interview questions I might get and how to answer them.
  3. A suggested git commit message.
- **Do not run `git commit` or `git push` yourself.** I'll do the git commands so I learn them.
- Do not add features or dependencies outside this file without asking.
- If a test fails, fix the root cause. Never weaken or delete a test to make it pass.
- Update the milestone checklist in this file as each one is completed.
