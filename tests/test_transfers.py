import pytest

from app.db import pool


@pytest.fixture
def funded(client):
    """Two accounts, with 100_000 paise deposited into the first."""
    sender = client.post("/accounts", json={"owner_name": "Asha"}).json()
    receiver = client.post("/accounts", json={"owner_name": "Bhavna"}).json()
    client.post(
        "/deposits",
        json={"account_id": sender["id"], "amount_paise": 100_000},
        headers={"Idempotency-Key": "fund-sender"},
    )
    return sender, receiver


def transfer(client, from_id, to_id, amount_paise, key):
    return client.post(
        "/transfers",
        json={
            "from_account_id": from_id,
            "to_account_id": to_id,
            "amount_paise": amount_paise,
        },
        headers={"Idempotency-Key": key},
    )


def balance(client, account_id):
    return client.get(f"/accounts/{account_id}").json()["balance_paise"]


def ledger_entries_for(payment_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT account_id, amount_paise FROM ledger_entries"
            " WHERE payment_id = %s ORDER BY amount_paise",
            (payment_id,),
        ).fetchall()


def payment_row(payment_id):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT status, failure_reason FROM payments WHERE id = %s", (payment_id,)
        ).fetchone()


# Test 2: transfer happy path.
def test_transfer_moves_money_and_writes_two_entries(client, funded, assert_balanced):
    sender, receiver = funded

    response = transfer(client, sender["id"], receiver["id"], 30_000, "transfer-1")
    assert response.status_code == 201
    payment = response.json()
    assert payment["type"] == "TRANSFER"
    assert payment["status"] == "COMPLETED"
    assert payment["failure_reason"] is None

    assert balance(client, sender["id"]) == 70_000
    assert balance(client, receiver["id"]) == 30_000

    # Exactly two entries, equal and opposite: the double entry.
    entries = ledger_entries_for(payment["id"])
    assert len(entries) == 2
    assert entries[0] == {"account_id": sender["id"], "amount_paise": -30_000}
    assert entries[1] == {"account_id": receiver["id"], "amount_paise": 30_000}
    assert_balanced()


def test_transfer_of_entire_balance_succeeds(client, funded, assert_balanced):
    sender, receiver = funded

    assert transfer(client, sender["id"], receiver["id"], 100_000, "t-all").status_code == 201
    assert balance(client, sender["id"]) == 0
    assert balance(client, receiver["id"]) == 100_000
    assert_balanced()


# Test 3: insufficient funds.
def test_insufficient_funds_is_402_and_writes_no_entries(client, funded, assert_balanced):
    sender, receiver = funded

    response = transfer(client, sender["id"], receiver["id"], 100_001, "transfer-too-big")
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "INSUFFICIENT_FUNDS"

    # The attempt is recorded as a payment, but no money moved.
    with pool.connection() as conn:
        payment = conn.execute(
            "SELECT id, status, failure_reason FROM payments WHERE idempotency_key = %s",
            ("transfer-too-big",),
        ).fetchone()
    assert payment["status"] == "FAILED"
    assert payment["failure_reason"] == "INSUFFICIENT_FUNDS"
    assert ledger_entries_for(payment["id"]) == []

    assert balance(client, sender["id"]) == 100_000
    assert balance(client, receiver["id"]) == 0
    assert_balanced()


def test_transfer_from_empty_account_is_402(client, funded, assert_balanced):
    sender, receiver = funded

    response = transfer(client, receiver["id"], sender["id"], 1, "transfer-from-empty")
    assert response.status_code == 402
    assert_balanced()


# Test 7 (transfer-side): same account -> 400, unknown account -> 404, bad input -> 422.
def test_transfer_to_same_account_is_400(client, funded):
    sender, _ = funded

    response = transfer(client, sender["id"], sender["id"], 1_000, "transfer-self")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SAME_ACCOUNT"

    with pool.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM payments WHERE type = 'TRANSFER'"
        ).fetchone()["n"] == 0


def test_transfer_to_unknown_account_is_404(client, funded, assert_balanced):
    sender, _ = funded

    assert transfer(client, sender["id"], 9999, 1_000, "t-unknown-to").status_code == 404
    assert transfer(client, 9999, sender["id"], 1_000, "t-unknown-from").status_code == 404
    assert_balanced()


def test_transfer_without_idempotency_key_is_400(client, funded):
    sender, receiver = funded

    response = client.post(
        "/transfers",
        json={
            "from_account_id": sender["id"],
            "to_account_id": receiver["id"],
            "amount_paise": 1_000,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_transfer_with_non_positive_amount_is_422(client, funded):
    sender, receiver = funded

    assert transfer(client, sender["id"], receiver["id"], 0, "t-zero").status_code == 422
    assert transfer(client, sender["id"], receiver["id"], -1, "t-negative").status_code == 422


def test_get_payment_returns_the_transfer(client, funded):
    sender, receiver = funded
    payment_id = transfer(client, sender["id"], receiver["id"], 5_000, "t-get").json()["id"]

    payment = client.get(f"/payments/{payment_id}").json()
    assert payment["type"] == "TRANSFER"
    assert payment["status"] == "COMPLETED"
    assert payment["amount_paise"] == 5_000
    assert payment["refund_of"] is None


def test_get_unknown_payment_is_404(client):
    response = client.get("/payments/9999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PAYMENT_NOT_FOUND"
