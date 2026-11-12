"""Scenario discovery must not seed, migrate, or create the company database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.world.demo_fixture import seed_demo_company
from app.world.seed import SEED_CUSTOMERS, SEED_EMPLOYEES

DEFAULT_EMPLOYEES = {employee.employee_id for employee in SEED_EMPLOYEES}
DEFAULT_CUSTOMERS = {customer.customer_id for customer in SEED_CUSTOMERS}


def _client(tmp_path: Path, database: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=str(database),
        workspace_path=str(tmp_path / "workspace"),
        allowed_hosts="testserver",
        aae_dynamic_planning_enabled=True,
    )
    return TestClient(create_app(settings))


def _scenarios(client: TestClient) -> dict:
    response = client.get("/api/v1/scenarios")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["scenarios"], list)
    assert body["dynamic_planning_enabled"] is True
    return body


def _ids(body: dict) -> list[str]:
    return [scenario["scenario_id"] for scenario in body["scenarios"]]


def _rows(database: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(database) as conn:
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }


def _sidecars(database: Path) -> list[str]:
    return sorted(
        path.name
        for suffix in ("-journal", "-wal", "-shm")
        if (path := Path(f"{database}{suffix}")).exists()
    )


def test_scenarios_do_not_seed_a_custom_database(tmp_path: Path) -> None:
    database = tmp_path / "custom.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE company_employees (
                employee_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                title TEXT NOT NULL,
                department TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE company_customers (
                customer_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                company TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE company_tickets (
                ticket_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO company_employees VALUES
                ('E-99', 'Custom Person', 'custom@example.test', 'Analyst', 'Research',
                 '2026-01-01T00:00:00+00:00', 'active');
            INSERT INTO company_customers VALUES
                ('C-9', 'Custom Contact', 'custom-co@example.test', 'Custom Co',
                 '2026-01-01T00:00:00+00:00');
            INSERT INTO company_tickets VALUES
                ('T-9', 'C-9', 'Custom subject', 'Custom description', 'open', 'low',
                 '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )
    before = database.read_bytes()
    before_rows = _rows(database)
    with _client(tmp_path, database) as client:
        body = _scenarios(client)
    assert database.read_bytes() == before
    assert _rows(database) == before_rows
    assert _sidecars(database) == []
    employee_ids = {row[0] for row in _rows(database)["company_employees"]}
    customer_ids = {row[0] for row in _rows(database)["company_customers"]}
    assert employee_ids == {"E-99"}
    assert customer_ids == {"C-9"}
    assert employee_ids.isdisjoint(DEFAULT_EMPLOYEES)
    assert customer_ids.isdisjoint(DEFAULT_CUSTOMERS)
    assert "assign_bounded" not in _ids(body)
    assert "batch_status" not in _ids(body)
    assert "list_bounded" in _ids(body)


def test_scenarios_do_not_create_a_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "absent" / "runtime.db"
    with _client(tmp_path, database) as client:
        body = _scenarios(client)
    assert not database.exists()
    assert not database.parent.exists()
    assert _ids(body) == ["ticket", "employee", "list_bounded"]
    assert body["replanning_enabled"] is False


def test_seeded_demo_database_offers_examples_without_writes(tmp_path: Path) -> None:
    database = tmp_path / "demo.db"
    seed_demo_company(database)
    before = database.read_bytes()
    before_rows = _rows(database)
    with _client(tmp_path, database) as client:
        body = _scenarios(client)
    assert database.read_bytes() == before
    assert _rows(database) == before_rows
    assert _ids(body) == ["ticket", "employee", "list_bounded", "assign_bounded", "batch_status"]


def test_repeated_scenario_reads_do_not_mutate(tmp_path: Path) -> None:
    database = tmp_path / "demo.db"
    seed_demo_company(database)
    before = database.read_bytes()
    before_rows = _rows(database)
    with _client(tmp_path, database) as client:
        bodies = [_scenarios(client) for _ in range(4)]
    assert database.read_bytes() == before
    assert _rows(database) == before_rows
    assert all(_ids(body) == _ids(bodies[0]) for body in bodies)
    assert _ids(bodies[0]) == [
        "ticket",
        "employee",
        "list_bounded",
        "assign_bounded",
        "batch_status",
    ]


def test_incomplete_or_invalid_database_is_left_unchanged(tmp_path: Path) -> None:
    incomplete = tmp_path / "incomplete.db"
    with sqlite3.connect(incomplete) as conn:
        conn.execute(
            """
            CREATE TABLE company_employees (
                employee_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                department TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO company_employees VALUES ('E-99', 'Custom Person', 'Research')"
        )
        schema_before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    before = incomplete.read_bytes()
    with _client(tmp_path, incomplete) as client:
        incomplete_body = _scenarios(client)
    assert incomplete.read_bytes() == before
    with sqlite3.connect(incomplete) as conn:
        schema_after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(company_employees)")]
    assert schema_after == schema_before
    assert columns == ["employee_id", "name", "department"]
    assert "assign_bounded" not in _ids(incomplete_body)
    assert "batch_status" not in _ids(incomplete_body)

    invalid = tmp_path / "invalid.db"
    invalid.write_bytes(b"this is not a sqlite database, and it must stay that way")
    invalid_before = invalid.read_bytes()
    with _client(tmp_path, invalid) as client:
        invalid_body = _scenarios(client)
    assert invalid.read_bytes() == invalid_before
    assert _sidecars(invalid) == []
    assert _ids(invalid_body) == ["ticket", "employee", "list_bounded"]
