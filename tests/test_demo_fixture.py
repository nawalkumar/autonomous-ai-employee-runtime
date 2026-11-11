"""Demo-company fixture: integrity, determinism, repeatability and reset safety."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.cli import build_runtime, main as cli_main
from app.config import Settings
from app.world.demo_fixture import (
    EXPECTED_COUNTS,
    SCENARIOS,
    FixtureSafetyError,
    main,
    seed_demo_company,
)
from app.world.seed import SEED_CUSTOMERS, SEED_EMPLOYEES


@pytest.fixture
def demo_db(tmp_path: Path) -> Path:
    path = tmp_path / "demo.db"
    seed_demo_company(path)
    return path


def rows(db: Path, sql: str, *params) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def dump(db: Path) -> dict[str, list[tuple]]:
    tables = [r[0] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'company_%' OR name = 'demo_fixture_meta' ORDER BY 1")]
    return {t: rows(db, f"SELECT * FROM {t} ORDER BY 1") for t in tables}


def ids(db: Path, sql: str, *params) -> list[str]:
    return [r[0] for r in rows(db, sql, *params)]


def test_record_counts_and_departments(demo_db: Path) -> None:
    assert rows(demo_db, "SELECT COUNT(*) FROM company_employees") == [(EXPECTED_COUNTS["employees"],)]
    assert rows(demo_db, "SELECT COUNT(*) FROM company_customers") == [(EXPECTED_COUNTS["customers"],)]
    assert rows(demo_db, "SELECT COUNT(*) FROM company_tickets") == [(EXPECTED_COUNTS["tickets"],)]
    assert dict(rows(demo_db, "SELECT department, COUNT(*) FROM company_employees GROUP BY 1")) == {
        "Support": 7, "Engineering": 4, "Product": 3, "Finance": 3, "Operations": 3,
    }
    assert dict(rows(demo_db, "SELECT status, COUNT(*) FROM company_tickets GROUP BY 1")) == {
        "open": 22, "in_progress": 11, "resolved": 16, "closed": 11,
    }
    assert dict(rows(demo_db, "SELECT priority, COUNT(*) FROM company_tickets GROUP BY 1")) == {
        "low": 15, "medium": 18, "high": 14, "urgent": 13,
    }


def test_default_seed_records_are_unchanged(demo_db: Path) -> None:
    for employee in SEED_EMPLOYEES:
        assert rows(demo_db, "SELECT name, email, title, department, status FROM company_employees WHERE employee_id = ?", employee.employee_id) == [
            (employee.name, employee.email, employee.title, employee.department, "active")
        ]
    for customer in SEED_CUSTOMERS:
        assert rows(demo_db, "SELECT name, email, company FROM company_customers WHERE customer_id = ?", customer.customer_id) == [
            (customer.name, customer.email, customer.company)
        ]


def test_controlled_scenarios_match_the_documented_identifiers(demo_db: Path) -> None:
    assert ids(demo_db, "SELECT ticket_id FROM company_tickets WHERE assignee_id IS NULL AND priority IN ('high','urgent') AND status IN ('open','in_progress') ORDER BY 1") == SCENARIOS["high_priority_unassigned"]
    assert ids(demo_db, "SELECT ticket_id FROM company_tickets WHERE customer_id = ? AND status IN ('open','in_progress') ORDER BY 1", SCENARIOS["escalation_customer"]) == SCENARIOS["escalation_candidates"]
    assert set(ids(demo_db, "SELECT priority FROM company_tickets WHERE ticket_id IN ('T-10005','T-10006','T-10007','T-10008')")) <= {"low", "medium"}
    assert ids(demo_db, "SELECT status FROM company_tickets WHERE ticket_id IN ('T-10009','T-10010','T-10011','T-10012')") == ["resolved"] * 4
    assert ids(demo_db, "SELECT ticket_id FROM company_tickets WHERE assignee_id = ? AND status IN ('open','in_progress') ORDER BY 1", SCENARIOS["offboarding_employee"]) == SCENARIOS["offboarding_open_tickets"]
    subjects = ids(demo_db, "SELECT subject FROM company_tickets WHERE ticket_id IN ('T-10016','T-10017') ORDER BY ticket_id")
    assert subjects[0] != subjects[1] and subjects[0].startswith("Invoice PDF") and subjects[1].startswith("Invoice PDF")
    eligible = ids(demo_db, "SELECT employee_id FROM company_employees WHERE department = 'Support' AND status = 'active' ORDER BY 1")
    assert eligible == SCENARIOS["eligible_assignees"]
    assert rows(demo_db, "SELECT department, status FROM company_employees WHERE employee_id = ?", SCENARIOS["inactive_employee"]) == [("Support", "inactive")]
    assert rows(demo_db, "SELECT status FROM company_tickets WHERE assignee_id = ?", SCENARIOS["inactive_employee"]) == [("closed",)]
    assert min(r[0] for r in rows(demo_db, "SELECT COUNT(*) FROM company_tickets GROUP BY customer_id")) >= 5


def test_referential_integrity_and_fictional_data(demo_db: Path) -> None:
    assert rows(demo_db, "PRAGMA foreign_key_check") == []
    assert rows(demo_db, "SELECT COUNT(*) FROM company_tickets t LEFT JOIN company_customers c USING (customer_id) WHERE c.customer_id IS NULL") == [(0,)]
    assert rows(demo_db, "SELECT COUNT(*) FROM company_tickets t JOIN company_employees e ON t.assignee_id = e.employee_id WHERE e.department != 'Support'") == [(0,)]
    assert rows(demo_db, "SELECT COUNT(*) FROM company_tickets t JOIN company_employees e ON t.assignee_id = e.employee_id WHERE e.status = 'inactive' AND t.status IN ('open','in_progress')") == [(0,)]
    emails = ids(demo_db, "SELECT email FROM company_employees UNION ALL SELECT email FROM company_customers")
    assert all(e.endswith(".example") for e in emails)
    assert len(set(e.lower() for e in emails)) == len(emails)
    assert rows(demo_db, "SELECT name, next_value FROM company_ticket_seq ORDER BY 1") == [("customer", 1011), ("ticket", 10061)]


def test_seeding_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    seed_demo_company(first)
    seed_demo_company(second)
    assert dump(first) == dump(second)


def test_reseeding_is_a_no_op_and_reset_restores(demo_db: Path) -> None:
    original = dump(demo_db)
    summary = seed_demo_company(demo_db)
    assert summary.created is False and dump(demo_db) == original
    conn = sqlite3.connect(demo_db)
    with conn:
        conn.execute("UPDATE company_tickets SET status = 'closed' WHERE ticket_id = 'T-10001'")
    conn.close()
    assert seed_demo_company(demo_db).created is False
    assert dump(demo_db) != original
    assert seed_demo_company(demo_db, reset=True).created is True
    assert dump(demo_db) == original


def test_refuses_to_touch_a_non_demo_database(tmp_path: Path, capsys) -> None:
    settings = Settings(_env_file=None, database_path=str(tmp_path / "data" / "runtime.db"), workspace_path=str(tmp_path / "ws"))
    assert cli_main(["--json", "run", "Update employee E-17 title to Senior Engineer."], settings=settings) == 4
    capsys.readouterr()
    runtime_db = Path(settings.database_path)
    before = runtime_db.read_bytes()
    for reset in (False, True):
        with pytest.raises(FixtureSafetyError):
            seed_demo_company(runtime_db, reset=reset)
    assert runtime_db.read_bytes() == before
    junk = tmp_path / "notes.db"
    junk.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(FixtureSafetyError):
        seed_demo_company(junk, reset=True)
    assert junk.read_text(encoding="utf-8") == "not sqlite"


def test_command_line(tmp_path: Path, capsys) -> None:
    path = tmp_path / "demo.db"
    assert main(["--database", str(path)]) == 0
    assert "created" in capsys.readouterr().out
    assert main(["--database", str(path), "--json"]) == 0
    assert '"created": false' in capsys.readouterr().out
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    assert main(["--database", str(other), "--reset"]) == 2
    assert "not a disposable demo database" in capsys.readouterr().err


def test_default_scenarios_still_work_on_the_demo_company(tmp_path: Path, capsys) -> None:
    db = tmp_path / "data" / "runtime.db"
    db.parent.mkdir()
    seed_demo_company(db)
    settings = Settings(_env_file=None, database_path=str(db), workspace_path=str(tmp_path / "ws"))
    goal = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
    assert cli_main(["--json", "run", goal], settings=settings) == 0
    capsys.readouterr()
    assert build_runtime(settings).repository.get_ticket("T-10061").customer_id == "C-1001"
