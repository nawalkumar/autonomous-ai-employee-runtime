"""Shared fixtures for runtime/tool integration tests."""

from pathlib import Path

import pytest

from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.verify import OutcomeVerifier
from app.world import CompanyRepository
from tests.authz_support import matching_scope_for_plan


@pytest.fixture(autouse=True)
def _inject_matching_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trusted test boundary: when a test omits ``authorization``, supply a
    narrow scope derived from the explicit plan. Production callers (CLI/API)
    always pass an independently derived scope. Tests that need a missing or
    narrower scope pass ``authorization=`` explicitly.
    """
    original = ExecutionRuntime.create_task

    def create_task(self, *args, **kwargs):  # noqa: ANN001
        if "authorization" not in kwargs:
            plan = kwargs.get("plan")
            if plan is None and len(args) >= 1:
                # Positional use is not part of the public signature; ignore.
                plan = None
            if plan is not None:
                kwargs["authorization"] = matching_scope_for_plan(list(plan))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ExecutionRuntime, "create_task", create_task)


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


@pytest.fixture
def verifier(company_repo: CompanyRepository, workspace: Path) -> OutcomeVerifier:
    return OutcomeVerifier(company_repo, workspace)


@pytest.fixture
def runtime(
    store: SQLiteStore,
    registry: ToolRegistry,
    verifier: OutcomeVerifier,
    db_path: Path,
) -> ExecutionRuntime:
    return ExecutionRuntime(
        store=store,
        registry=registry,
        verifier=verifier,
        lock_dir=db_path.parent / "locks",
    )
