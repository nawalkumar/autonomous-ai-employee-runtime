"""Tests for the deterministic mock company world."""

from pathlib import Path

import pytest

from app.world import CompanyRepository


@pytest.fixture
def repo(tmp_path: Path) -> CompanyRepository:
    return CompanyRepository(tmp_path / "runtime.db")


def test_schema_and_seed(repo: CompanyRepository) -> None:
    assert repo.count_employees() == 3
    assert repo.count_customers() == 3
    employee = repo.get_employee("E-17")
    assert employee is not None
    assert employee.name == "Alex Morgan"
    assert employee.title == "Software Engineer"


def test_seed_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    first = CompanyRepository(db)
    second = CompanyRepository(db)
    assert first.count_employees() == 3
    assert second.count_employees() == 3
    assert first.count_customers() == 3


def test_get_employee(repo: CompanyRepository) -> None:
    employee = repo.get_employee("E-23")
    assert employee is not None
    assert employee.name == "Priya Shah"
    assert repo.get_employee("E-999") is None


def test_find_customer(repo: CompanyRepository) -> None:
    by_id = repo.find_customers(customer_id="C-1001")
    assert len(by_id) == 1
    assert by_id[0].company == "Acme Corp"

    by_company = repo.find_customers(company="Globex")
    assert len(by_company) == 1
    assert by_company[0].customer_id == "C-1002"

    by_email = repo.find_customers(email="support@initech.example")
    assert len(by_email) == 1
    assert by_email[0].company == "Initech"


def test_update_employee(repo: CompanyRepository) -> None:
    updated = repo.update_employee("E-17", {"title": "Senior Engineer"})
    assert updated is not None
    assert updated.title == "Senior Engineer"
    reloaded = repo.get_employee("E-17")
    assert reloaded is not None
    assert reloaded.title == "Senior Engineer"


def test_create_and_get_ticket(repo: CompanyRepository) -> None:
    ticket = repo.create_ticket(
        customer_id="C-1001",
        subject="Billing discrepancy",
        description="$240 mismatch",
        priority="high",
    )
    assert ticket.ticket_id == "T-10001"
    assert ticket.status == "open"

    loaded = repo.get_ticket("T-10001")
    assert loaded is not None
    assert loaded.subject == "Billing discrepancy"
    assert loaded.customer_id == "C-1001"

    second = repo.create_ticket(
        customer_id="C-1002",
        subject="Login issue",
        description="Cannot sign in",
    )
    assert second.ticket_id == "T-10002"
