import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://ledger:ledger@localhost:5432/ledger"
)

# Opened once at import time and reused for the life of the process; psycopg_pool
# handles borrowing/returning connections per-request. dict_row makes query results
# read like `row["balance_paise"]` instead of `row[3]`.
pool = ConnectionPool(DATABASE_URL, kwargs={"row_factory": dict_row}, open=True)
