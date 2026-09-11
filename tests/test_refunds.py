import pytest

from app.db import pool


@pytest.fixture
def transferred(client):
    """Asha sent 40_000 paise to Bhavna, out of a 100_000 paise balance."""
    sender = client.post("/accounts", json={"owner_name": "Asha"}).json()
    receiver = client.post("/accounts", json={"owner_name": "Bhavna"}).json()
    deposit(client, sender["id"], 100_000, "fund-sender")
    payment = transfer(client, sender["id"], receiver["id"], 40_000, "original").json()
    return sender, receiver, payment


def deposit(client, account_id, amount_paise, key):
    return client.post(
        "/deposits",
        json={"account_id": account_id, "amount_paise": amount_paise},
        headers={"Idempotency-Key": key},
    )


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


def refund(client, payment_id, key):
    return client.post(
        f"/payments/{payment_id}/refund", headers={"Idempotency-Key": key}
    )


def balance(client, account_id):
    return client.get(f"/accounts/{account_id}").json()["balance_paise"]


def scalar(sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()["value"]


# Test 11: refund works once; a second refund with a new key is rejected.
def test_refund_returns_the_money(client, transferred, assert_balanced):
    sender, receiver, payment = transferred

    response = refund(client, payment["id"], "refund-1")
    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "REFUND"
    assert body["status"] == "COMPLETED"
    assert body["refund_of"] == payment["id"]
    assert body["amount_paise"] == 40_000
    # The refund runs the transfer backwards: receiver pays sender.
    assert body["from_account_id"] == receiver["id"]
    assert body["to_account_id"] == sender["id"]

    assert balance(client, sender["id"]) == 100_000
    assert balance(client, receiver["id"]) == 0
    assert scalar(
        "SELECT count(*) AS value FROM ledger_entries WHERE payment_id = %s", (body["id"],)
    ) == 2
    assert_balanced()


def test_second_refund_with_a_new_key_is_409(client, transferred, assert_balanced):
    sender, receiver, payment = transferred
    assert refund(client, payment["id"], "refund-1").status_code == 201

    response = refund(client, payment["id"], "refund-2")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFUND_NOT_ALLOWED"

    # The rejected attempt stored nothing and moved nothing.
    assert scalar("SELECT count(*) AS value FROM payments WHERE type = 'REFUND'") == 1
    assert balance(client, sender["id"]) == 100_000
    assert_balanced()


def test_refund_retry_with_the_same_key_replays(client, transferred, assert_balanced):
    sender, receiver, payment = transferred

    first = refund(client, payment["id"], "refund-1")
    second = refund(client, payment["id"], "refund-1")

    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert balance(client, sender["id"]) == 100_000
    assert_balanced()


def test_refunding_a_deposit_is_409(client, transferred, assert_balanced):
    sender, _, _ = transferred
    deposit_id = deposit(client, sender["id"], 5_000, "a-deposit").json()["id"]

    response = refund(client, deposit_id, "refund-deposit")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFUND_NOT_ALLOWED"
    assert_balanced()


def test_refunding_a_failed_payment_is_409(client, transferred, assert_balanced):
    sender, receiver, _ = transferred
    failed = transfer(client, receiver["id"], sender["id"], 999_999, "doomed")
    assert failed.status_code == 402

    failed_id = scalar(
        "SELECT id AS value FROM payments WHERE idempotency_key = %s", ("doomed",)
    )
    response = refund(client, failed_id, "refund-failed")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFUND_NOT_ALLOWED"
    assert_balanced()


def test_refunding_a_refund_is_409(client, transferred, assert_balanced):
    _, _, payment = transferred
    refund_id = refund(client, payment["id"], "refund-1").json()["id"]

    response = refund(client, refund_id, "refund-the-refund")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "REFUND_NOT_ALLOWED"
    assert_balanced()


def test_refunding_an_unknown_payment_is_404(client):
    response = refund(client, 9999, "refund-ghost")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PAYMENT_NOT_FOUND"


def test_refund_without_idempotency_key_is_400(client, transferred):
    _, _, payment = transferred

    response = client.post(f"/payments/{payment['id']}/refund")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


# Test 12: the receiver already spent the money.
def test_refund_is_402_when_receiver_already_spent_the_money(
    client, transferred, assert_balanced
):
    sender, receiver, payment = transferred
    third = client.post("/accounts", json={"owner_name": "Chetan"}).json()

    # Bhavna moves the money on before Asha asks for it back.
    assert transfer(client, receiver["id"], third["id"], 40_000, "spent-it").status_code == 201
    assert balance(client, receiver["id"]) == 0

    response = refund(client, payment["id"], "refund-broke")
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "INSUFFICIENT_FUNDS"

    refund_row = scalar(
        "SELECT id AS value FROM payments WHERE idempotency_key = %s", ("refund-broke",)
    )
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT status, failure_reason, refund_of FROM payments WHERE id = %s",
            (refund_row,),
        ).fetchone()
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "INSUFFICIENT_FUNDS"
    assert row["refund_of"] == payment["id"]
    assert scalar(
        "SELECT count(*) AS value FROM ledger_entries WHERE payment_id = %s", (refund_row,)
    ) == 0

    assert balance(client, sender["id"]) == 60_000
    assert balance(client, receiver["id"]) == 0
    assert_balanced()


def test_failed_refund_can_be_retried_once_the_money_is_back(client, transferred, assert_balanced):
    sender, receiver, payment = transferred
    third = client.post("/accounts", json={"owner_name": "Chetan"}).json()
    transfer(client, receiver["id"], third["id"], 40_000, "spent-it")

    assert refund(client, payment["id"], "refund-broke").status_code == 402

    # A failed refund does not consume the right to refund: only a COMPLETED one does.
    transfer(client, third["id"], receiver["id"], 40_000, "paid-back")
    assert refund(client, payment["id"], "refund-retry").status_code == 201

    assert balance(client, sender["id"]) == 100_000
    assert balance(client, receiver["id"]) == 0
    assert_balanced()
