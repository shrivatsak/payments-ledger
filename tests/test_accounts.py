from app.db import pool


def create_account(client, owner_name="Asha"):
    return client.post("/accounts", json={"owner_name": owner_name}).json()


def deposit(client, account_id, amount_paise, key):
    return client.post(
        "/deposits",
        json={"account_id": account_id, "amount_paise": amount_paise},
        headers={"Idempotency-Key": key},
    )


# Test 1: create account, deposit, balance is correct.
def test_create_account_then_deposit_updates_balance(client, assert_balanced, system_account_id):
    response = client.post("/accounts", json={"owner_name": "Asha"})
    assert response.status_code == 201
    account = response.json()
    assert account["owner_name"] == "Asha"
    assert account["balance_paise"] == 0

    response = deposit(client, account["id"], 50_000, "deposit-1")
    assert response.status_code == 201
    assert "Idempotent-Replayed" not in response.headers
    payment = response.json()
    assert payment["type"] == "DEPOSIT"
    assert payment["status"] == "COMPLETED"
    assert payment["to_account_id"] == account["id"]
    assert payment["from_account_id"] == system_account_id
    assert payment["amount_paise"] == 50_000

    assert client.get(f"/accounts/{account['id']}").json()["balance_paise"] == 50_000
    # The deposit is a balanced double entry: the system account funded it.
    assert client.get(f"/accounts/{system_account_id}").json()["balance_paise"] == -50_000
    assert_balanced()


def test_deposits_accumulate(client, assert_balanced):
    account = create_account(client)
    deposit(client, account["id"], 25_000, "deposit-1")
    deposit(client, account["id"], 10_500, "deposit-2")

    assert client.get(f"/accounts/{account['id']}").json()["balance_paise"] == 35_500
    assert_balanced()


# Test 7 (deposit-side): missing key -> 400, unknown account -> 404, bad amount -> 422.
# SAME_ACCOUNT (400) applies to transfers and is covered in M2.
def test_deposit_without_idempotency_key_is_400(client):
    account = create_account(client)
    response = client.post(
        "/deposits", json={"account_id": account["id"], "amount_paise": 1_000}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_deposit_with_empty_idempotency_key_is_400(client):
    account = create_account(client)
    response = deposit(client, account["id"], 1_000, "")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_deposit_with_overlong_idempotency_key_is_400(client):
    account = create_account(client)
    response = deposit(client, account["id"], 1_000, "k" * 256)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_deposit_to_unknown_account_is_404_and_stores_nothing(client, assert_balanced):
    response = deposit(client, 9999, 1_000, "deposit-unknown")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"

    with pool.connection() as conn:
        payments = conn.execute("SELECT count(*) AS n FROM payments").fetchone()["n"]
    assert payments == 0
    assert_balanced()


def test_deposit_with_non_positive_amount_is_422(client):
    account = create_account(client)
    assert deposit(client, account["id"], 0, "deposit-zero").status_code == 422
    assert deposit(client, account["id"], -500, "deposit-negative").status_code == 422


def test_deposit_above_maximum_amount_is_422(client):
    account = create_account(client)
    assert deposit(client, account["id"], 10_000_001, "deposit-too-big").status_code == 422


def test_create_account_requires_non_empty_owner_name(client):
    assert client.post("/accounts", json={"owner_name": ""}).status_code == 422


def test_get_unknown_account_is_404(client):
    response = client.get("/accounts/9999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_list_accounts_excludes_the_system_account(client):
    create_account(client, "Asha")
    create_account(client, "Bhavna")

    accounts = client.get("/accounts").json()
    assert [a["owner_name"] for a in accounts] == ["Asha", "Bhavna"]


def test_transactions_show_type_and_counterparty(client, system_account_id):
    account = create_account(client)
    deposit(client, account["id"], 7_500, "deposit-1")

    transactions = client.get(f"/accounts/{account['id']}/transactions").json()
    assert len(transactions) == 1
    assert transactions[0]["type"] == "DEPOSIT"
    assert transactions[0]["amount_paise"] == 7_500
    assert transactions[0]["counterparty_account_id"] == system_account_id
    assert transactions[0]["counterparty_owner_name"] == "EXTERNAL_FUNDING"


def test_transactions_for_unknown_account_is_404(client):
    assert client.get("/accounts/9999/transactions").status_code == 404
