"""Helpers for the v0.3.0 company-operation, verification and workflow tests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models.enums import TaskStatus
from app.models.plan import PlanStep
from app.models.state import ExecutionState
from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.world import CompanyRepository
from app.world.demo_fixture import seed_demo_company


@dataclass
class World:
    db: Path
    workspace: Path
    repo: CompanyRepository
    injector: FailureInjector
    registry: ToolRegistry
    store: SQLiteStore
    runtime: ExecutionRuntime

    @property
    def company(self) -> CompanyAPITool:
        return self.registry.get("company_api")  # type: ignore[return-value]

    def rows(self, sql: str, *params: Any) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def execute(self, sql: str, *params: Any) -> None:
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def snapshot(self) -> dict[str, list[tuple]]:
        return {
            table: self.rows(f"SELECT * FROM {table} ORDER BY 1")
            for table in (
                "company_employees",
                "company_customers",
                "company_tickets",
                "company_idempotency",
                "company_ticket_seq",
            )
        }

    def run(self, *steps: PlanStep, approve: bool = True, user_goal: str = "test") -> ExecutionState:
        """Execute an explicit plan, approving every gated step if ``approve``."""
        state = self.runtime.create_task(user_goal=user_goal, plan=list(steps))
        state = self.runtime.run(state.task_id)
        while approve and state.final_status is TaskStatus.NEEDS_APPROVAL:
            state = self.runtime.approve_task(state.task_id, approver="test-approver")
        return state


def make_world(tmp_path: Path, *, demo: bool) -> World:
    db = tmp_path / "data" / "runtime.db"
    workspace = tmp_path / "workspace"
    if demo:
        db.parent.mkdir(parents=True, exist_ok=True)
        seed_demo_company(db)
    repo = CompanyRepository(db)
    injector = FailureInjector()
    registry = ToolRegistry()
    registry.register(CompanyAPITool(repo, failure_injector=injector))
    registry.register(FileTool(workspace, protected_paths=[db]))
    store = SQLiteStore(db)
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    return World(db, workspace, repo, injector, registry, store, runtime)


def reopen(world: World) -> World:
    """A new runtime over the same database and workspace (a process restart)."""
    repo = CompanyRepository(world.db)
    injector = FailureInjector()
    registry = ToolRegistry()
    registry.register(CompanyAPITool(repo, failure_injector=injector))
    registry.register(FileTool(world.workspace, protected_paths=[world.db]))
    store = SQLiteStore(world.db)
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=world.workspace)
    return World(world.db, world.workspace, repo, injector, registry, store, runtime)


class SimulatedCrash(BaseException):
    """Escapes the executor's exception handling, like a killed process."""


def crash_during(world: World, operation: str, *, after_commit: bool) -> None:
    """Make the next ``operation`` call crash, before or after its database commit."""
    tool = world.company
    original = tool.execute
    pending = {"crash": True}

    def execute(args, *, idempotency_key=None):
        if args.operation == operation and pending["crash"]:
            pending["crash"] = False
            if after_commit:
                original(args, idempotency_key=idempotency_key)
            raise SimulatedCrash(operation)
        return original(args, idempotency_key=idempotency_key)

    tool.execute = execute  # type: ignore[method-assign]


def step(step_id: str, operation: str, **arguments: Any) -> PlanStep:
    tool = "file" if operation in ("read", "write", "list") else "company_api"
    return PlanStep(
        step_id=step_id,
        description=f"{operation} ({step_id})",
        tool_name=tool,
        arguments={"operation": operation, **arguments},
    )


def checks(state: ExecutionState) -> dict[str, bool]:
    return {c["name"]: c["passed"] for c in state.verification_checks}
