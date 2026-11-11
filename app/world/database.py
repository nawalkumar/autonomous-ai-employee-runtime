"""SQLite connection helpers for the mock company world."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

_COMPANY_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_employees (
    employee_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    department TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    company TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_tickets (
    ticket_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES company_customers (customer_id)
);

CREATE TABLE IF NOT EXISTS company_ticket_seq (
    name TEXT PRIMARY KEY,
    next_value INTEGER NOT NULL
);

-- One row per committed write; inserted in the same transaction as the mutation.
CREATE TABLE IF NOT EXISTS company_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_customers_company
    ON company_customers (company);
CREATE INDEX IF NOT EXISTS idx_company_tickets_customer
    ON company_tickets (customer_id);
"""


def ensure_parent_dir(db_path: Path) -> None:
    if db_path.parent and str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)


def connect(db_path: Path) -> sqlite3.Connection:
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def write_transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Serialized write transaction: BEGIN IMMEDIATE ... COMMIT, ROLLBACK on error."""
    ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def init_company_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_COMPANY_SCHEMA)


# --- Additive migrations (v0.3.0) ---------------------------------------------------
#
# The CREATE TABLE statements above are the v0.2.0 schema and never change, so a
# fresh database and an upgraded v0.2.0 database go through the same steps and
# end up identical. Every step only adds: existing rows, identifiers and columns
# are untouched, and new columns have defaults that describe existing rows
# correctly (every existing employee is active; no existing ticket is assigned).
#
# Rollback: there is no down-migration. The v0.2.0 code can still open an
# upgraded database (it selects named columns or ignores unknown ones), but it
# would not enforce employee status or ticket assignment rules.

_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "company_employees",
        "status",
        "status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive'))",
    ),
    (
        # Employees are deactivated, never deleted, so a plain reference
        # (no ON DELETE action) keeps every assignment pointing at a real row.
        "company_tickets",
        "assignee_id",
        "assignee_id TEXT DEFAULT NULL REFERENCES company_employees (employee_id)",
    ),
)

_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_company_tickets_status ON company_tickets (status);
CREATE INDEX IF NOT EXISTS idx_company_tickets_assignee ON company_tickets (assignee_id);
"""

CUSTOMER_SEQ_FLOOR = 1004


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _missing_columns(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [m for m in _COLUMN_MIGRATIONS if m[1] not in _columns(conn, m[0])]


def _next_customer_number(conn: sqlite3.Connection) -> int:
    highest = CUSTOMER_SEQ_FLOOR - 1
    for (customer_id,) in conn.execute("SELECT customer_id FROM company_customers"):
        prefix, _, number = str(customer_id).partition("-")
        if prefix == "C" and number.isdigit():
            highest = max(highest, int(number))
    return highest + 1


def migrate_company_schema(db_path: Path) -> None:
    """Apply the additive v0.3.0 migrations; safe to run any number of times."""
    with closing(sqlite3.connect(db_path, timeout=30.0)) as probe:
        has_customer_seq = probe.execute(
            "SELECT 1 FROM company_ticket_seq WHERE name = 'customer'"
        ).fetchone()
        if not _missing_columns(probe) and has_customer_seq:
            return
    # Re-checked under the write lock so concurrent processes migrate once.
    with write_transaction(db_path) as conn:
        for table, _column, definition in _missing_columns(conn):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")
        for statement in filter(None, (s.strip() for s in _MIGRATION_INDEXES.split(";"))):
            conn.execute(statement)
        # company_ticket_seq is the sequence table (one row per sequence, despite
        # its name). Customer IDs continue after the highest existing C-<n>.
        conn.execute(
            "INSERT OR IGNORE INTO company_ticket_seq (name, next_value) VALUES ('customer', ?)",
            (_next_customer_number(conn),),
        )
