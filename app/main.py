from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse

from app import service
from app.db import pool
from app.errors import LedgerError, ledger_error_handler
from app.idempotency import validate_key
from app.models import (
    AccountResponse,
    CreateAccountRequest,
    DepositRequest,
    PaymentResponse,
    TransactionResponse,
    TransferRequest,
)
from app.service import PaymentResult

app = FastAPI(title="Idempotent Payments Ledger API")
app.add_exception_handler(LedgerError, ledger_error_handler)


@app.get("/health")
def health():
    # Round-trips a query through the pool so /health fails if Postgres is down,
    # not just if the process is up.
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/accounts", status_code=201, response_model=AccountResponse)
def create_account(request: CreateAccountRequest):
    return service.create_account(request.owner_name)


@app.get("/accounts", response_model=list[AccountResponse])
def list_accounts():
    return service.list_accounts()


@app.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: int):
    return service.get_account(account_id)


@app.get("/accounts/{account_id}/transactions", response_model=list[TransactionResponse])
def list_transactions(account_id: int, limit: int = Query(50, ge=1, le=200)):
    return service.list_transactions(account_id, limit)


@app.post("/deposits")
def create_deposit(
    request: DepositRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    key = validate_key(idempotency_key)
    result = service.create_deposit(key, request.account_id, request.amount_paise)
    return _payment_response(result)


@app.post("/transfers")
def create_transfer(
    request: TransferRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    key = validate_key(idempotency_key)
    result = service.create_transfer(
        key, request.from_account_id, request.to_account_id, request.amount_paise
    )
    return _payment_response(result)


@app.get("/payments/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: int):
    return service.get_payment(payment_id)


def _payment_response(result: PaymentResult) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if result.replayed else None
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)
