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

In order to run the tests set-up the docker env, install the dependencies with pip and then run the test cases. 

```bash
docker compose up -d db
pip install .
pytest -v
```

