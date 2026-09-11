import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import service
from app.db import pool


def run_concurrently(task, count):
    """Fire `count` calls at the same instant and return their results in order.

    The barrier holds every thread until all of them are ready, so the calls
    genuinely overlap instead of trickling through one at a time. Each call goes
    through the service layer and borrows its own connection from the pool.
    Any exception raised in a worker is re-raised here by future.result().
    """
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait(timeout=30)
        return task(index)

    with ThreadPoolExecutor(max_workers=count) as executor:
        futures = [executor.submit(run, index) for index in range(count)]
        return [future.result() for future in futures]


def balance(account_id):
    return service.get_account(account_id)["balance_paise"]


def scalar(sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()["value"]


@pytest.fixture
def accounts():
    sender = service.create_account("Asha")["id"]
    receiver = service.create_account("Bhavna")["id"]
    return sender, receiver


# Test 8: 50 concurrent transfers against a balance that only covers 10 of them.
def test_concurrent_transfers_cannot_overdraw(accounts, assert_balanced):
    sender, receiver = accounts
    service.create_deposit("fund", sender, 1_000)

    results = run_concurrently(
        lambda i: service.create_transfer(f"spend-{i}", sender, receiver, 100), count=50
    )

    statuses = [result.status_code for result in results]
    assert statuses.count(201) == 10
    assert statuses.count(402) == 40
    assert all(
        result.body["error"]["code"] == "INSUFFICIENT_FUNDS"
        for result in results
        if result.status_code == 402
    )

    # Every paise is accounted for and the sender never went below zero.
    assert balance(sender) == 0
    assert balance(receiver) == 1_000
    assert scalar("SELECT count(*) AS value FROM ledger_entries") == 22  # 1 deposit + 10
    assert_balanced()


# Test 9: 20 simultaneous retries of the same request.
def test_concurrent_duplicates_create_one_payment(accounts, assert_balanced):
    sender, receiver = accounts
    service.create_deposit("fund", sender, 10_000)

    results = run_concurrently(
        lambda i: service.create_transfer("same-key", sender, receiver, 1_000), count=20
    )

    assert all(result.status_code == 201 for result in results)
    # All 20 callers are told about the same payment.
    assert len({result.body["id"] for result in results}) == 1
    # Exactly one caller did the work; the other 19 were served the stored response.
    assert sum(1 for result in results if not result.replayed) == 1
    assert sum(1 for result in results if result.replayed) == 19

    payment_id = results[0].body["id"]
    assert scalar(
        "SELECT count(*) AS value FROM payments WHERE idempotency_key = %s", ("same-key",)
    ) == 1
    assert scalar(
        "SELECT count(*) AS value FROM ledger_entries WHERE payment_id = %s", (payment_id,)
    ) == 2

    # The money moved exactly once, not twenty times.
    assert balance(sender) == 9_000
    assert balance(receiver) == 1_000
    assert_balanced()


# Test 10: transfers in both directions at once must not deadlock.
def test_opposite_transfers_do_not_deadlock(accounts, assert_balanced):
    first, second = accounts
    service.create_deposit("fund-first", first, 100_000)
    service.create_deposit("fund-second", second, 100_000)

    def both_ways(index):
        # Half go first -> second, half go second -> first, interleaved.
        if index % 2 == 0:
            return service.create_transfer(f"a2b-{index}", first, second, 100)
        return service.create_transfer(f"b2a-{index}", second, first, 100)

    # Any deadlock would surface as psycopg.errors.DeadlockDetected raised here.
    results = run_concurrently(both_ways, count=100)

    assert all(result.status_code == 201 for result in results)
    # 50 each way at the same amount, so both balances come back to where they started.
    assert balance(first) == 100_000
    assert balance(second) == 100_000
    assert_balanced()


def test_concurrent_refunds_of_different_payments_do_not_deadlock(accounts, assert_balanced):
    """Refunds take the same account locks as transfers, so they are prone to the
    same lock-upgrade deadlock. Two refunds of *different* payments do not contend
    on the original payment row, which leaves the account locks as the only thing
    keeping them apart."""
    sender, receiver = accounts
    service.create_deposit("fund", sender, 100_000)
    payments = [
        service.create_transfer(f"t-{i}", sender, receiver, 10_000).body["id"]
        for i in range(10)
    ]

    results = run_concurrently(
        lambda i: service.create_refund(f"r-{i}", payments[i]), count=len(payments)
    )

    assert all(result.status_code == 201 for result in results)
    assert balance(sender) == 100_000
    assert balance(receiver) == 0
    assert_balanced()
