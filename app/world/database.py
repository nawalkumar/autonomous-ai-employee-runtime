"""SQLite connection helpers for the mock company world."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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
