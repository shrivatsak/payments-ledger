import pytest
from fastapi.testclient import TestClient

from app.db import pool
from app.main import app


@pytest.fixture(autouse=True)
def reset_db():
    """Every test starts from an empty ledger with only the system account seeded."""
    with pool.connection() as conn:
        conn.execute("TRUNCATE ledger_entries, payments, accounts RESTART IDENTITY CASCADE")
        conn.execute("INSERT INTO accounts (owner_name, is_system) VALUES (%s, true)",
                     ("EXTERNAL_FUNDING",))


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def assert_balanced():
    """The global invariant: money is only ever moved, never created or destroyed,
    so every ledger entry in the system must sum to exactly zero."""

    def _assert():
        with pool.connection() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM ledger_entries"
            ).fetchone()["total"]
        assert total == 0, f"ledger entries sum to {total}, expected 0"

    return _assert


@pytest.fixture
def system_account_id():
    with pool.connection() as conn:
        return conn.execute("SELECT id FROM accounts WHERE is_system = true").fetchone()["id"]
