"""Shared fixtures for runtime/tool integration tests."""

from pathlib import Path

import pytest

from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.world import CompanyRepository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "runtime.db"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def store(db_path: Path) -> SQLiteStore:
    return SQLiteStore(db_path)


@pytest.fixture
def company_repo(db_path: Path) -> CompanyRepository:
    return CompanyRepository(db_path)


@pytest.fixture
def failure_injector() -> FailureInjector:
    return FailureInjector()


@pytest.fixture
def registry(
    company_repo: CompanyRepository,
    workspace: Path,
    failure_injector: FailureInjector,
) -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(CompanyAPITool(company_repo, failure_injector=failure_injector))
    tools.register(FileTool(workspace))
    return tools
