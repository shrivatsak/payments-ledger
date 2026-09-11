from fastapi import FastAPI

from app.db import pool

app = FastAPI(title="Idempotent Payments Ledger API")


@app.get("/health")
def health():
    # Round-trips a query through the pool so /health fails if Postgres is down,
    # not just if the process is up.
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}
