from datetime import datetime

from pydantic import BaseModel, Field

MAX_AMOUNT_PAISE = 10_000_000


class CreateAccountRequest(BaseModel):
    owner_name: str = Field(min_length=1, max_length=255)


class AccountResponse(BaseModel):
    id: int
    owner_name: str
    balance_paise: int
    created_at: datetime


class DepositRequest(BaseModel):
    account_id: int = Field(gt=0)
    amount_paise: int = Field(gt=0, le=MAX_AMOUNT_PAISE)


class TransactionResponse(BaseModel):
    id: int
    payment_id: int
    type: str
    amount_paise: int
    counterparty_account_id: int
    counterparty_owner_name: str
    created_at: datetime
