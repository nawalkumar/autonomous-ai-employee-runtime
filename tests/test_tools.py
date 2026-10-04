"""Tests for tool abstraction, registry, company/file tools, and failure injection."""

from pathlib import Path

import pytest

from app.models.enums import FailureType
from app.tools import (
    CompanyAPITool,
    FailureInjector,
    FileTool,
    RiskLevel,
    ToolRegistry,
    ToolResult,
)
from app.world import CompanyRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime.db"


@pytest.fixture
def repo(db_path: Path) -> CompanyRepository:
    return CompanyRepository(db_path)


@pytest.fixture
def injector() -> FailureInjector:
    return FailureInjector()


@pytest.fixture
def company_tool(repo: CompanyRepository, injector: FailureInjector) -> CompanyAPITool:
    return CompanyAPITool(repo, failure_injector=injector)


@pytest.fixture
def file_tool(tmp_path: Path) -> FileTool:
    return FileTool(tmp_path / "workspace")


def test_tool_metadata_and_risk(company_tool: CompanyAPITool, file_tool: FileTool) -> None:
    company_meta = company_tool.metadata()
    assert company_meta["name"] == "company_api"
    assert company_tool.risk_for("get_employee") is RiskLevel.READ
    assert company_tool.risk_for("create_ticket") is RiskLevel.WRITE
    assert company_tool.risk_for("update_employee") is RiskLevel.WRITE

    assert file_tool.risk_for("read") is RiskLevel.READ
    assert file_tool.risk_for("write") is RiskLevel.WRITE
    assert file_tool.metadata()["name"] == "file"


def test_normalized_success_and_failure_result() -> None:
    ok = ToolResult.success({"x": 1}, evidence=[{"type": "demo"}])
    assert ok.ok is True
    assert ok.data == {"x": 1}
    assert ok.error_type is None
    assert ok.error_message is None
    assert ok.evidence[0]["type"] == "demo"

    err = ToolResult.failure("transient", "down")
    assert err.ok is False
    assert err.data is None
    assert err.error_type == "transient"
    assert err.error_message == "down"


def test_registry_register_get_list_and_duplicate(
    company_tool: CompanyAPITool, file_tool: FileTool
) -> None:
    registry = ToolRegistry()
    registry.register(company_tool)
    registry.register(file_tool)

    assert registry.get("company_api") is company_tool
    assert registry.get("file") is file_tool
    names = [t.name for t in registry.list_tools()]
    assert names == ["company_api", "file"]

    with pytest.raises(ValueError, match="already registered"):
        registry.register(company_tool)


def test_company_tool_operations(company_tool: CompanyAPITool, repo: CompanyRepository) -> None:
    employee = company_tool.invoke(
        {"operation": "get_employee", "employee_id": "E-17"}
    )
    assert employee.ok is True
    assert employee.data is not None
    assert employee.data["employee"]["name"] == "Alex Morgan"

    customers = company_tool.invoke(
        {"operation": "find_customer", "company": "Acme Corp"}
    )
    assert customers.ok is True
    assert customers.data is not None
    assert customers.data["count"] == 1

    created = company_tool.invoke(
        {
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": "Billing",
            "description": "$240",
            "priority": "high",
        }
    )
    assert created.ok is True
    assert created.data is not None
    ticket_id = created.data["ticket"]["ticket_id"]
    assert ticket_id == "T-10001"

    fetched = company_tool.invoke(
        {"operation": "get_ticket", "ticket_id": ticket_id}
    )
    assert fetched.ok is True

    updated = company_tool.invoke(
        {
            "operation": "update_employee",
            "employee_id": "E-17",
            "fields": {"title": "Senior Engineer"},
        }
    )
    assert updated.ok is True
    assert repo.get_employee("E-17").title == "Senior Engineer"  # type: ignore[union-attr]


def test_company_tool_invalid_args(company_tool: CompanyAPITool) -> None:
    result = company_tool.invoke({"operation": "get_employee"})
    assert result.ok is False
    assert result.error_type == FailureType.INVALID_ARGS.value


def test_file_tool_write_read_nested(file_tool: FileTool) -> None:
    write = file_tool.invoke(
        {
            "operation": "write",
            "path": "notes/summary.txt",
            "content": "ticket created",
        }
    )
    assert write.ok is True

    read = file_tool.invoke({"operation": "read", "path": "notes/summary.txt"})
    assert read.ok is True
    assert read.data is not None
    assert read.data["content"] == "ticket created"

    listing = file_tool.invoke({"operation": "list", "path": ""})
    assert listing.ok is True
    assert listing.data is not None
    assert "notes/summary.txt" in listing.data["files"]


def test_file_tool_rejects_traversal(file_tool: FileTool, tmp_path: Path) -> None:
    # Place a secret outside the workspace to ensure it cannot be reached.
    secret = tmp_path / ".env"
    secret.write_text("SECRET=1", encoding="utf-8")

    cases = [
        {"operation": "read", "path": "../.env"},
        {"operation": "read", "path": "../../.env"},
        {"operation": "write", "path": "../escape.txt", "content": "x"},
        {"operation": "read", "path": "/etc/passwd"},
        {"operation": "read", "path": str(secret.resolve())},
    ]
    for args in cases:
        result = file_tool.invoke(args)
        assert result.ok is False, args
        assert result.error_type == FailureType.POLICY_BLOCKED.value


def test_failure_injection_create_ticket_once(
    company_tool: CompanyAPITool,
    injector: FailureInjector,
    repo: CompanyRepository,
) -> None:
    injector.inject_once(
        operation="company_api.create_ticket",
        error_type=FailureType.TRANSIENT.value,
        error_message="Mock company API temporarily unavailable",
    )

    first = company_tool.invoke(
        {
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": "Billing discrepancy",
            "description": "$240 mismatch",
            "priority": "high",
        }
    )
    assert first.ok is False
    assert first.error_type == FailureType.TRANSIENT.value
    assert "unavailable" in (first.error_message or "").lower()
    assert repo.list_tickets() == []

    second = company_tool.invoke(
        {
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": "Billing discrepancy",
            "description": "$240 mismatch",
            "priority": "high",
        }
    )
    assert second.ok is True
    assert second.data is not None
    assert second.data["ticket"]["ticket_id"] == "T-10001"
    assert len(repo.list_tickets()) == 1
