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


def deposit(client, account_id, amount_paise, key):
    return client.post(
        "/deposits",
        json={"account_id": account_id, "amount_paise": amount_paise},
        headers={"Idempotency-Key": key},
    )


def balance(client, account_id):
    return client.get(f"/accounts/{account_id}").json()["balance_paise"]


def scalar(sql, params=()):
    """Run a query that selects a single value aliased as `value`."""
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()["value"]


# Test 4: same key + same body twice -> identical response, second is a replay.
def test_repeated_transfer_replays_and_moves_money_once(client, funded, assert_balanced):
    sender, receiver = funded
    first = transfer(client, sender["id"], receiver["id"], 30_000, "retry-me")
    second = transfer(client, sender["id"], receiver["id"], 30_000, "retry-me")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert "Idempotent-Replayed" not in first.headers
    assert second.headers["Idempotent-Replayed"] == "true"

    # The retry did no work at all: one payment, one double entry, one debit.
    assert scalar(
        "SELECT count(*) AS value FROM payments WHERE idempotency_key = %s", ("retry-me",)
    ) == 1
    assert scalar(
        "SELECT count(*) AS value FROM ledger_entries WHERE payment_id = %s",
        (first.json()["id"],),
    ) == 2
    assert balance(client, sender["id"]) == 70_000
    assert balance(client, receiver["id"]) == 30_000
    assert_balanced()


def test_repeated_deposit_replays_and_credits_once(client, assert_balanced):
    account = client.post("/accounts", json={"owner_name": "Asha"}).json()

    first = deposit(client, account["id"], 25_000, "dep-retry")
    second = deposit(client, account["id"], 25_000, "dep-retry")

    assert first.json() == second.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert balance(client, account["id"]) == 25_000
    assert_balanced()


def test_replay_is_insensitive_to_json_key_order(client, funded):
    sender, receiver = funded
    first = transfer(client, sender["id"], receiver["id"], 10_000, "order-key")

    # Same fields, different order on the wire: still the same request.
    second = client.post(
        "/transfers",
        json={
            "amount_paise": 10_000,
            "to_account_id": receiver["id"],
            "from_account_id": sender["id"],
        },
        headers={"Idempotency-Key": "order-key"},
    )
    assert second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert second.json() == first.json()


def test_different_keys_are_independent_payments(client, funded, assert_balanced):
    sender, receiver = funded
    first = transfer(client, sender["id"], receiver["id"], 10_000, "key-a")
    second = transfer(client, sender["id"], receiver["id"], 10_000, "key-b")

    # Same body, different key: a genuinely new payment, not a duplicate.
    assert first.json()["id"] != second.json()["id"]
    assert balance(client, sender["id"]) == 80_000
    assert_balanced()


# Test 5: same key + different body -> 409.
def test_same_key_different_amount_is_409(client, funded, assert_balanced):
    sender, receiver = funded
    transfer(client, sender["id"], receiver["id"], 30_000, "reused")

    response = transfer(client, sender["id"], receiver["id"], 40_000, "reused")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"

    # The original payment is untouched and no second one was created.
    assert scalar(
        "SELECT count(*) AS value FROM payments WHERE idempotency_key = %s", ("reused",)
    ) == 1
    assert balance(client, sender["id"]) == 70_000
    assert_balanced()


def test_same_key_different_accounts_is_409(client, funded):
    sender, receiver = funded
    third = client.post("/accounts", json={"owner_name": "Chetan"}).json()
    transfer(client, sender["id"], receiver["id"], 5_000, "reused-accounts")

    response = transfer(client, sender["id"], third["id"], 5_000, "reused-accounts")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


def test_same_key_on_a_different_endpoint_is_409(client, funded):
    sender, receiver = funded
    deposit(client, sender["id"], 5_000, "cross-endpoint")

    # The hash covers the endpoint path, so a deposit key cannot be reused for a
    # transfer even if the amounts happen to match.
    response = transfer(client, sender["id"], receiver["id"], 5_000, "cross-endpoint")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


# Test 6: replaying a FAILED request returns the stored 402, even after funding.
def test_failed_payment_replays_402_after_account_is_funded(client, assert_balanced):
    sender = client.post("/accounts", json={"owner_name": "Asha"}).json()
    receiver = client.post("/accounts", json={"owner_name": "Bhavna"}).json()
    deposit(client, sender["id"], 1_000, "small-funding")

    first = transfer(client, sender["id"], receiver["id"], 5_000, "too-poor")
    assert first.status_code == 402
    assert first.json()["error"]["code"] == "INSUFFICIENT_FUNDS"

    # The account now has plenty of money...
    deposit(client, sender["id"], 100_000, "big-funding")

    # ...but the retry replays the stored decision instead of re-evaluating it.
    second = transfer(client, sender["id"], receiver["id"], 5_000, "too-poor")
    assert second.status_code == 402
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"

    payment_id = scalar(
        "SELECT id AS value FROM payments WHERE idempotency_key = %s", ("too-poor",)
    )
    assert scalar(
        "SELECT count(*) AS value FROM ledger_entries WHERE payment_id = %s", (payment_id,)
    ) == 0
    assert balance(client, sender["id"]) == 101_000
    assert balance(client, receiver["id"]) == 0
    assert_balanced()


def test_failed_payment_key_cannot_be_reused_with_a_different_body(client):
    sender = client.post("/accounts", json={"owner_name": "Asha"}).json()
    receiver = client.post("/accounts", json={"owner_name": "Bhavna"}).json()

    assert transfer(client, sender["id"], receiver["id"], 5_000, "poor").status_code == 402

    response = transfer(client, sender["id"], receiver["id"], 1, "poor")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_MISMATCH"


def test_rejected_requests_do_not_consume_the_key(client, funded, assert_balanced):
    sender, receiver = funded

    # A 404 stores nothing, so the same key is still free for a real request.
    assert transfer(client, sender["id"], 9999, 5_000, "recycled").status_code == 404

    response = transfer(client, sender["id"], receiver["id"], 5_000, "recycled")
    assert response.status_code == 201
    assert "Idempotent-Replayed" not in response.headers
    assert balance(client, receiver["id"]) == 5_000
    assert_balanced()
