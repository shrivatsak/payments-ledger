from fastapi import Request
from fastapi.responses import JSONResponse

# Every error code this API can return.
MISSING_IDEMPOTENCY_KEY = "MISSING_IDEMPOTENCY_KEY"
SAME_ACCOUNT = "SAME_ACCOUNT"
INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
IDEMPOTENCY_KEY_MISMATCH = "IDEMPOTENCY_KEY_MISMATCH"
REFUND_NOT_ALLOWED = "REFUND_NOT_ALLOWED"


class LedgerError(Exception):
    """Raised anywhere in the service layer; mapped to the API error shape below."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
