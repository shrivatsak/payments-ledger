from dataclasses import dataclass

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.db import pool
from app.errors import (
    ACCOUNT_NOT_FOUND,
    IDEMPOTENCY_KEY_MISMATCH,
    INSUFFICIENT_FUNDS,
    PAYMENT_NOT_FOUND,
    REFUND_NOT_ALLOWED,
    SAME_ACCOUNT,
    LedgerError,
)
from app.idempotency import request_hash


@dataclass
class PaymentResult:
    """What a money-moving call produced: the HTTP status and body to send, and
    whether it came from a stored response instead of doing real work."""

    status_code: int
    body: dict
    replayed: bool = False


# --- accounts ---------------------------------------------------------------

# Balance is always derived from the ledger with SUM(); there is no stored balance
# column that could drift out of sync with the entries.
_ACCOUNT_COLUMNS = """
    SELECT a.id,
           a.owner_name,
           a.created_at,
           COALESCE(SUM(l.amount_paise), 0)::BIGINT AS balance_paise
    FROM accounts a
    LEFT JOIN ledger_entries l ON l.account_id = a.id
"""


def create_account(owner_name: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO accounts (owner_name) VALUES (%s)"
            " RETURNING id, owner_name, created_at",
            (owner_name,),
        ).fetchone()
    return {**row, "balance_paise": 0}


def list_accounts() -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            _ACCOUNT_COLUMNS + " WHERE a.is_system = false GROUP BY a.id ORDER BY a.id"
        ).fetchall()


def get_account(account_id: int) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            _ACCOUNT_COLUMNS + " WHERE a.id = %s GROUP BY a.id", (account_id,)
        ).fetchone()
    if row is None:
        raise LedgerError(404, ACCOUNT_NOT_FOUND, f"Account {account_id} does not exist.")
    return row


def list_transactions(account_id: int, limit: int) -> list[dict]:
    with pool.connection() as conn:
        _require_account(conn, account_id)
        return conn.execute(
            """
            SELECT l.id,
                   l.payment_id,
                   p.type,
                   l.amount_paise,
                   c.id AS counterparty_account_id,
                   c.owner_name AS counterparty_owner_name,
                   l.created_at
            FROM ledger_entries l
            JOIN payments p ON p.id = l.payment_id
            JOIN accounts c ON c.id = CASE WHEN l.account_id = p.from_account_id
                                           THEN p.to_account_id
                                           ELSE p.from_account_id END
            WHERE l.account_id = %s
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            (account_id, limit),
        ).fetchall()


# --- deposits ---------------------------------------------------------------


def create_deposit(idempotency_key: str, account_id: int, amount_paise: int) -> PaymentResult:
    body_hash = request_hash(
        "/deposits", {"account_id": account_id, "amount_paise": amount_paise}
    )

    with pool.connection() as conn:
        # One transaction for the whole request: the payment row, both ledger entries
        # and the stored response either all commit together, or none of them do.
        with conn.transaction():
            system_id = _system_account_id(conn)
            _require_account(conn, account_id)

            payment = _claim_idempotency_key(
                conn,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
                payment_type="DEPOSIT",
                from_account_id=system_id,
                to_account_id=account_id,
                amount_paise=amount_paise,
            )
            if payment is None:
                return _replay(conn, idempotency_key, body_hash)

            # Deposits need no balance check and no lock: the money comes from the
            # system account, which is the one account allowed to go negative.
            _write_double_entry(conn, payment["id"], system_id, account_id, amount_paise)
            return _complete(conn, payment)


# --- transfers --------------------------------------------------------------


def create_transfer(
    idempotency_key: str, from_account_id: int, to_account_id: int, amount_paise: int
) -> PaymentResult:
    if from_account_id == to_account_id:
        raise LedgerError(
            400, SAME_ACCOUNT, "A transfer must be between two different accounts."
        )

    body_hash = request_hash(
        "/transfers",
        {
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "amount_paise": amount_paise,
        },
    )

    with pool.connection() as conn:
        with conn.transaction():
            _require_account(conn, from_account_id)
            _require_account(conn, to_account_id)

            payment = _claim_idempotency_key(
                conn,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
                payment_type="TRANSFER",
                from_account_id=from_account_id,
                to_account_id=to_account_id,
                amount_paise=amount_paise,
            )
            if payment is None:
                return _replay(conn, idempotency_key, body_hash)

            _lock_accounts(conn, from_account_id, to_account_id)

            # Read the balance only after the lock is held. Any other transfer out of
            # this account is stuck waiting on the same row, so the number we read
            # cannot be undercut by an uncommitted debit -> no double-spend.
            balance = _balance(conn, from_account_id)
            if balance < amount_paise:
                return _fail_insufficient_funds(conn, payment, balance)

            _write_double_entry(
                conn, payment["id"], from_account_id, to_account_id, amount_paise
            )
            return _complete(conn, payment)


# --- refunds ----------------------------------------------------------------


def create_refund(idempotency_key: str, payment_id: int) -> PaymentResult:
    """Full refund of a completed transfer: the same amount, sent back the other way."""
    # There is no request body, so the payment being refunded *is* the request.
    body_hash = request_hash(f"/payments/{payment_id}/refund", {})

    with pool.connection() as conn:
        with conn.transaction():
            # Lock the original payment before doing anything else. Every refund
            # attempt on this payment queues here, so two of them can never both
            # look, both see no refund yet, and both decide they are allowed.
            # Locking before the idempotency INSERT also matters: inserting a row
            # that references this payment takes a FOR KEY SHARE lock on it, and
            # two transactions each holding KEY SHARE while asking for FOR UPDATE
            # would deadlock.
            original = _lock_payment(conn, payment_id)

            refund = _claim_idempotency_key(
                conn,
                idempotency_key=idempotency_key,
                body_hash=body_hash,
                payment_type="REFUND",
                from_account_id=original["to_account_id"],
                to_account_id=original["from_account_id"],
                amount_paise=original["amount_paise"],
                refund_of=original["id"],
            )
            # Checked after the claim, so retrying a successful refund replays the
            # stored 201 instead of being told the payment is already refunded.
            if refund is None:
                return _replay(conn, idempotency_key, body_hash)

            _assert_refundable(conn, original)

            _lock_accounts(conn, refund["from_account_id"], refund["to_account_id"])

            # The money is refunded out of the original receiver, who may well have
            # spent it already.
            balance = _balance(conn, refund["from_account_id"])
            if balance < refund["amount_paise"]:
                return _fail_insufficient_funds(conn, refund, balance)

            _write_double_entry(
                conn,
                refund["id"],
                refund["from_account_id"],
                refund["to_account_id"],
                refund["amount_paise"],
            )
            return _complete(conn, refund)


def _lock_payment(conn: Connection, payment_id: int) -> dict:
    row = conn.execute(
        "SELECT id, type, status, from_account_id, to_account_id, amount_paise"
        " FROM payments WHERE id = %s FOR UPDATE",
        (payment_id,),
    ).fetchone()
    if row is None:
        raise LedgerError(404, PAYMENT_NOT_FOUND, f"Payment {payment_id} does not exist.")
    return row


def _assert_refundable(conn: Connection, original: dict) -> None:
    """Only a completed transfer that has not been refunded yet can be refunded.

    The caller must already hold the lock on `original`. The partial unique index
    on payments(refund_of) WHERE status = 'COMPLETED' is the database-level
    backstop if this check is ever bypassed.
    """
    if original["type"] != "TRANSFER" or original["status"] != "COMPLETED":
        raise LedgerError(
            409,
            REFUND_NOT_ALLOWED,
            f"Only a completed transfer can be refunded; payment {original['id']} is"
            f" a {original['status']} {original['type']}.",
        )

    already_refunded = conn.execute(
        "SELECT 1 FROM payments WHERE refund_of = %s AND status = 'COMPLETED'",
        (original["id"],),
    ).fetchone()
    if already_refunded:
        raise LedgerError(
            409,
            REFUND_NOT_ALLOWED,
            f"Payment {original['id']} has already been refunded.",
        )


# --- payments ---------------------------------------------------------------


def get_payment(payment_id: int) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, type, from_account_id, to_account_id, amount_paise, status,"
            " failure_reason, refund_of, created_at FROM payments WHERE id = %s",
            (payment_id,),
        ).fetchone()
    if row is None:
        raise LedgerError(404, PAYMENT_NOT_FOUND, f"Payment {payment_id} does not exist.")
    return row


# --- shared idempotency machinery -------------------------------------------


def _claim_idempotency_key(
    conn: Connection,
    *,
    idempotency_key: str,
    body_hash: str,
    payment_type: str,
    from_account_id: int,
    to_account_id: int,
    amount_paise: int,
    refund_of: int | None = None,
) -> dict | None:
    """Try to claim the key by inserting the payment row. Returns the new row, or
    None if the key is already taken (this request is a duplicate).

    The row is inserted with a placeholder status/response that gets overwritten
    later in the same transaction, so the placeholder is never visible to anyone.
    Claiming the key first is what makes simultaneous duplicates safe: if another
    transaction holds the same key and has not committed yet, Postgres makes this
    INSERT wait on the unique index instead of letting both requests move money.
    """
    return conn.execute(
        """
        INSERT INTO payments (idempotency_key, request_hash, type, from_account_id,
                              to_account_id, amount_paise, status, refund_of,
                              response_code, response_body)
        VALUES (%s, %s, %s, %s, %s, %s, 'FAILED', %s, 0, '{}'::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id, type, from_account_id, to_account_id, amount_paise, refund_of,
                  created_at
        """,
        (
            idempotency_key,
            body_hash,
            payment_type,
            from_account_id,
            to_account_id,
            amount_paise,
            refund_of,
        ),
    ).fetchone()


def _replay(conn: Connection, idempotency_key: str, body_hash: str) -> PaymentResult:
    """The key already exists: return exactly what the first request returned."""
    row = conn.execute(
        "SELECT request_hash, response_code, response_body FROM payments"
        " WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()

    # Same key with a different request body is a client bug, not a retry, so we
    # refuse it rather than silently returning an unrelated payment's response.
    if row["request_hash"] != body_hash:
        raise LedgerError(
            409,
            IDEMPOTENCY_KEY_MISMATCH,
            "This Idempotency-Key was already used with a different request body.",
        )

    return PaymentResult(row["response_code"], row["response_body"], replayed=True)


def _lock_accounts(conn: Connection, *account_ids: int) -> None:
    """Take a row lock on each account, always lowest id first.

    The consistent order is what prevents deadlock: if A->B locked A then B while
    B->A locked B then A, each would hold the row the other is waiting for and
    Postgres would have to kill one of them. Locks are released at commit.
    """
    for account_id in sorted(set(account_ids)):
        conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,))


def _balance(conn: Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0)::BIGINT AS balance"
        " FROM ledger_entries WHERE account_id = %s",
        (account_id,),
    ).fetchone()["balance"]


def _fail_insufficient_funds(
    conn: Connection, payment: dict, balance_paise: int
) -> PaymentResult:
    """A rejected payment is still a recorded fact: the row stays, with no ledger
    entries, so a retry with the same key replays the 402 instead of re-checking."""
    body = {
        "error": {
            "code": INSUFFICIENT_FUNDS,
            "message": (
                f"Account {payment['from_account_id']} holds {balance_paise} paise,"
                f" less than the {payment['amount_paise']} paise required."
            ),
        }
    }
    conn.execute(
        "UPDATE payments SET status = 'FAILED', failure_reason = %s, response_code = 402,"
        " response_body = %s WHERE id = %s",
        (INSUFFICIENT_FUNDS, Jsonb(body), payment["id"]),
    )
    return PaymentResult(402, body)


def _write_double_entry(
    conn: Connection, payment_id: int, from_account_id: int, to_account_id: int, amount_paise: int
) -> None:
    """Every movement of money writes exactly two entries that sum to zero."""
    conn.execute(
        "INSERT INTO ledger_entries (payment_id, account_id, amount_paise)"
        " VALUES (%s, %s, %s), (%s, %s, %s)",
        (payment_id, from_account_id, -amount_paise, payment_id, to_account_id, amount_paise),
    )


def _complete(conn: Connection, payment: dict) -> PaymentResult:
    body = _payment_body(payment, status="COMPLETED")
    conn.execute(
        "UPDATE payments SET status = 'COMPLETED', response_code = 201,"
        " response_body = %s WHERE id = %s",
        (Jsonb(body), payment["id"]),
    )
    return PaymentResult(201, body)


def _payment_body(payment: dict, status: str, failure_reason: str | None = None) -> dict:
    return {
        "id": payment["id"],
        "type": payment["type"],
        "from_account_id": payment["from_account_id"],
        "to_account_id": payment["to_account_id"],
        "amount_paise": payment["amount_paise"],
        "status": status,
        "failure_reason": failure_reason,
        "refund_of": payment["refund_of"],
        "created_at": payment["created_at"].isoformat(),
    }


def _system_account_id(conn: Connection) -> int:
    return conn.execute("SELECT id FROM accounts WHERE is_system = true").fetchone()["id"]


def _require_account(conn: Connection, account_id: int) -> None:
    if conn.execute("SELECT 1 FROM accounts WHERE id = %s", (account_id,)).fetchone() is None:
        raise LedgerError(404, ACCOUNT_NOT_FOUND, f"Account {account_id} does not exist.")
