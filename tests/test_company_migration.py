"""Additive migration of v0.2.0 company databases."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from app.cli import build_runtime
from app.config import Settings
from app.world import CompanyRepository
from app.world.database import _COMPANY_SCHEMA, migrate_company_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
V020_COLUMNS = {
    "company_employees": ["employee_id", "name", "email", "title", "department", "updated_at"],
    "company_tickets": [
        "ticket_id", "customer_id", "subject", "description", "status", "priority",
        "created_at", "updated_at",
    ],
}


def make_v020_database(path: Path) -> None:
    """A database exactly as v0.2.0 left it: its schema, seed rows, a ticket,
    an extra customer and a stored idempotent create_ticket response."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(_COMPANY_SCHEMA)
        conn.executemany(
            "INSERT INTO company_employees VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("E-17", "Alex Morgan", "alex.morgan@centralign.example", "Senior Engineer", "Engineering", "2026-01-01T12:00:00+00:00"),
                ("E-23", "Priya Shah", "priya.shah@centralign.example", "Product Manager", "Product", "2026-01-01T12:00:00+00:00"),
                ("E-31", "Daniel Kim", "daniel.kim@centralign.example", "Support Engineer", "Support", "2026-01-01T12:00:00+00:00"),
            ],
        )
        conn.executemany(
            "INSERT INTO company_customers VALUES (?, ?, ?, ?, ?)",
            [
                ("C-1001", "Acme Billing Contact", "billing@acme.example", "Acme Corp", "2026-01-01T12:00:00+00:00"),
                ("C-1002", "Globex Ops", "ops@globex.example", "Globex", "2026-01-01T12:00:00+00:00"),
                ("C-1003", "Initech Support", "support@initech.example", "Initech", "2026-01-01T12:00:00+00:00"),
                ("C-1007", "Imported", "imported@example.example", "Imported Co", "2026-02-01T12:00:00+00:00"),
            ],
        )
        conn.execute(
            "INSERT INTO company_tickets VALUES ('T-10001', 'C-1001', 'Billing discrepancy $240', "
            "'Customer Acme Corp reported a $240 billing discrepancy.', 'open', 'high', "
            "'2026-03-01T10:00:00+00:00', '2026-03-01T10:00:00+00:00')"
        )
        conn.execute("INSERT INTO company_ticket_seq VALUES ('ticket', 10002)")
        conn.execute(
            "INSERT INTO company_idempotency VALUES ('task_old:step_2', 'create_ticket', 'hash', "
            "'{\"ticket_id\": \"T-10001\", \"customer_id\": \"C-1001\", \"subject\": \"Billing discrepancy $240\", "
            "\"description\": \"d\", \"status\": \"open\", \"priority\": \"high\", "
            "\"created_at\": \"2026-03-01T10:00:00+00:00\", \"updated_at\": \"2026-03-01T10:00:00+00:00\"}', "
            "'2026-03-01T10:00:00+00:00')"
        )
    conn.close()


def table_rows(path: Path, table: str, columns: list[str]) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1").fetchall()
    finally:
        conn.close()


def columns(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


def test_v020_database_is_upgraded_additively(tmp_path: Path) -> None:
    db = tmp_path / "data" / "runtime.db"
    make_v020_database(db)
    before = {t: table_rows(db, t, cols) for t, cols in V020_COLUMNS.items()}
    before["company_customers"] = table_rows(db, "company_customers", ["*"])
    before["company_idempotency"] = table_rows(db, "company_idempotency", ["*"])

    repo = CompanyRepository(db)

    assert columns(db, "company_employees") == V020_COLUMNS["company_employees"] + ["status"]
    assert columns(db, "company_tickets") == V020_COLUMNS["company_tickets"] + ["assignee_id"]
    for table, cols in V020_COLUMNS.items():
        assert table_rows(db, table, cols) == before[table]
    assert table_rows(db, "company_customers", ["*"]) == before["company_customers"]
    assert table_rows(db, "company_idempotency", ["*"]) == before["company_idempotency"]
    assert {e.status for e in repo.list_employees()} == {"active"}
    assert repo.get_ticket("T-10001").assignee_id is None
    assert repo.get_employee("E-17").title == "Senior Engineer"  # user data kept, not re-seeded
    assert table_rows(db, "company_ticket_seq", ["name", "next_value"]) == [
        ("customer", 1008),
        ("ticket", 10002),
    ]


def test_migration_is_idempotent_and_new_operations_work(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    make_v020_database(db)
    CompanyRepository(db)
    schema = table_rows(db, "sqlite_master", ["type", "name", "sql"])
    migrate_company_schema(db)
    CompanyRepository(db)
    assert table_rows(db, "sqlite_master", ["type", "name", "sql"]) == schema

    repo = CompanyRepository(db)
    customer, _ = repo.create_customer_idempotent(name="N", company="Co", email="n@co.example")
    assert customer.customer_id == "C-1008"
    ticket, _ = repo.create_ticket_idempotent(customer_id="C-1001", subject="s", description="d")
    assert ticket.ticket_id == "T-10002"
    assigned, _, _ = repo.assign_ticket_idempotent("T-10001", "E-31")
    assert assigned.assignee_id == "E-31"


def test_foreign_keys_reject_unknown_assignees(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    make_v020_database(db)
    CompanyRepository(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("UPDATE company_tickets SET assignee_id = 'E-404' WHERE ticket_id = 'T-10001'")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised
    conn = sqlite3.connect(db)
    try:
        bad_status = False
        conn.execute("UPDATE company_employees SET status = 'retired' WHERE employee_id = 'E-17'")
    except sqlite3.IntegrityError:
        bad_status = True
    finally:
        conn.close()
    assert bad_status


def test_stored_v020_idempotent_response_still_replays(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    make_v020_database(db)
    repo = CompanyRepository(db)
    conn = sqlite3.connect(db)
    from app.world.repository import _request_hash

    request_hash = _request_hash(
        "create_ticket",
        {"customer_id": "C-1001", "subject": "Billing discrepancy $240", "description": "d",
         "priority": "high", "status": "open"},
    )
    with conn:
        conn.execute("UPDATE company_idempotency SET request_hash = ?", (request_hash,))
    conn.close()
    ticket, replayed = repo.create_ticket_idempotent(
        customer_id="C-1001", subject="Billing discrepancy $240", description="d",
        priority="high", idempotency_key="task_old:step_2",
    )
    assert replayed is True and ticket.ticket_id == "T-10001" and ticket.assignee_id is None


def test_concurrent_processes_migrate_once(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    make_v020_database(db)
    code = "import sys; from app.world import CompanyRepository; CompanyRepository(sys.argv[1]); print('ok')"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(db)],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(6)
    ]
    outputs = [p.communicate(timeout=120) for p in procs]
    assert [p.returncode for p in procs] == [0] * 6, outputs
    assert columns(db, "company_tickets").count("assignee_id") == 1


def test_runtime_and_cli_open_an_upgraded_database(tmp_path: Path, capsys) -> None:
    from app.cli import main

    db = tmp_path / "data" / "runtime.db"
    make_v020_database(db)
    settings = Settings(_env_file=None, database_path=str(db), workspace_path=str(tmp_path / "ws"))
    rt = build_runtime(settings)
    assert rt.repository.get_ticket("T-10001").subject == "Billing discrepancy $240"
    goal = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
    assert main(["--json", "run", goal], settings=settings) == 0
    capsys.readouterr()
    assert [t.ticket_id for t in rt.repository.list_tickets()] == ["T-10001", "T-10002"]
