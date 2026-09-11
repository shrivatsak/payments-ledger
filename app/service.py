from dataclasses import dataclass

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.db import pool
from app.errors import ACCOUNT_NOT_FOUND, IDEMPOTENCY_KEY_MISMATCH, LedgerError
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
