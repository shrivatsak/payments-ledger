import os

from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://ledger:ledger@localhost:5432/ledger"
)

# Opened once at import time and reused for the life of the process; psycopg_pool
# handles borrowing/returning connections per-request.
pool = ConnectionPool(DATABASE_URL, open=True)
