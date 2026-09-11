import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://ledger:ledger@localhost:5432/ledger"
)

# Opened once at import time and reused for the life of the process; psycopg_pool
# handles borrowing/returning connections per-request. dict_row makes query results
# read like `row["balance_paise"]` instead of `row[3]`.
# max_size caps how many requests can be inside the database at once: a request
# waiting on a row lock is still holding its connection, so the pool has to be
# wider than one or concurrent transfers would queue on connections rather than
# on the locks we actually want to test.
pool = ConnectionPool(
    DATABASE_URL,
    kwargs={"row_factory": dict_row},
    min_size=4,
    max_size=20,
    open=True,
)
