"""Regression tests: idempotency keys are enforced and resume never duplicates writes."""

import threading
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.models import PlanStep, PlanStepStatus, TaskStatus, ToolCallStatus
from app.models.enums import ApprovalStatus, FailureType, VerificationStatus
from app.runtime import EventType, ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.tools.result import ToolResult
from app.world import CompanyRepository

TICKET_ARGS = {
    "operation": "create_ticket",
    "customer_id": "C-1001",
    "subject": "Billing discrepancy $240",
    "description": "$240 mismatch",
    "priority": "high",
}


class SimulatedCrash(BaseException):
    """Stands in for the process dying; not caught by the runtime's Exception guard."""


class CrashAfterCommitTool(CompanyAPITool):
    """Commits the write, then 'crashes' before the runtime can record the result."""

    def __init__(self, *args, crash_on: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.crash_on = crash_on
        self.crashed = False

    def execute(self, args: BaseModel, *, idempotency_key: str | None = None) -> ToolResult:
        result = super().execute(args, idempotency_key=idempotency_key)
        if getattr(args, "operation", None) == self.crash_on and not self.crashed:
            self.crashed = True
            raise SimulatedCrash()
        return result


def _registry(db_path: Path, workspace: Path, tool: CompanyAPITool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool or CompanyAPITool(CompanyRepository(db_path)))
    registry.register(FileTool(workspace))
    return registry


def test_same_key_creates_ticket_once_and_returns_original(
    company_repo: CompanyRepository,
) -> None:
    tool = CompanyAPITool(company_repo)
    first = tool.invoke(TICKET_ARGS, idempotency_key="task_x:step_1")
    second = tool.invoke(TICKET_ARGS, idempotency_key="task_x:step_1")

    assert first.ok and second.ok
    assert [t.ticket_id for t in company_repo.list_tickets()] == ["T-10001"]
    assert second.data["ticket"]["ticket_id"] == first.data["ticket"]["ticket_id"]
    assert second.data["idempotent_replay"] is True
    assert "idempotent_replay" not in first.data
    assert company_repo.get_idempotency_record("task_x:step_1") is not None


def test_different_keys_create_separate_tickets(company_repo: CompanyRepository) -> None:
    tool = CompanyAPITool(company_repo)
    tool.invoke(TICKET_ARGS, idempotency_key="k1")
    tool.invoke(TICKET_ARGS, idempotency_key="k2")
    assert len(company_repo.list_tickets()) == 2


def test_key_reuse_with_different_request_is_rejected(
    company_repo: CompanyRepository,
) -> None:
    tool = CompanyAPITool(company_repo)
    tool.invoke(TICKET_ARGS, idempotency_key="k")
    conflict = tool.invoke({**TICKET_ARGS, "subject": "Other"}, idempotency_key="k")

    assert conflict.ok is False
    assert conflict.error_type == FailureType.INVALID_ARGS.value
    assert len(company_repo.list_tickets()) == 1


def test_update_employee_replay_does_not_reapply(company_repo: CompanyRepository) -> None:
    tool = CompanyAPITool(company_repo)
    args = {
        "operation": "update_employee",
        "employee_id": "E-17",
        "fields": {"title": "Senior Engineer"},
    }
    first = tool.invoke(args, idempotency_key="upd")
    assert first.ok
    # Someone changes the record afterwards; replaying the key must not overwrite it.
    company_repo.update_employee("E-17", {"title": "Staff Engineer"})

    replay = tool.invoke(args, idempotency_key="upd")
    assert replay.ok is True
    assert replay.data["idempotent_replay"] is True
    assert replay.data["employee"]["title"] == "Senior Engineer"
    assert company_repo.get_employee("E-17").title == "Staff Engineer"


def test_failed_writes_record_no_key(company_repo: CompanyRepository) -> None:
    injector = FailureInjector()
    tool = CompanyAPITool(company_repo, failure_injector=injector)

    bad = tool.invoke({**TICKET_ARGS, "customer_id": "C-404"}, idempotency_key="bad")
    assert bad.ok is False
    assert company_repo.get_idempotency_record("bad") is None

    injector.inject_once("company_api.create_ticket", FailureType.TRANSIENT.value, "503")
    transient = tool.invoke(TICKET_ARGS, idempotency_key="k")
    assert transient.ok is False
    assert company_repo.get_idempotency_record("k") is None

    retry = tool.invoke(TICKET_ARGS, idempotency_key="k")
    assert retry.ok is True
    assert "idempotent_replay" not in retry.data
    assert len(company_repo.list_tickets()) == 1


def test_concurrent_calls_with_same_key_create_one_ticket(db_path: Path) -> None:
    CompanyRepository(db_path)
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            _, replayed = CompanyRepository(db_path, seed=False).create_ticket_idempotent(
                customer_id="C-1001",
                subject="s",
                description="d",
                idempotency_key="shared",
            )
            outcomes.append(replayed)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert outcomes.count(False) == 1
    assert len(CompanyRepository(db_path).list_tickets()) == 1


def test_interrupted_ticket_step_resumes_after_restart_without_duplicate(
    db_path: Path, workspace: Path
) -> None:
    crash_tool = CrashAfterCommitTool(CompanyRepository(db_path), crash_on="create_ticket")
    runtime1 = ExecutionRuntime(
        store=SQLiteStore(db_path),
        registry=_registry(db_path, workspace, crash_tool),
        workspace_path=workspace,
    )
    task = runtime1.create_task(
        user_goal="Ticket and note",
        plan=[
            PlanStep(
                step_id="s1",
                description="Find Acme",
                tool_name="company_api",
                arguments={"operation": "find_customer", "company": "Acme Corp"},
            ),
            PlanStep(
                step_id="s2",
                description="Create ticket",
                tool_name="company_api",
                arguments=dict(TICKET_ARGS),
            ),
            PlanStep(
                step_id="s3",
                description="Write note",
                tool_name="file",
                arguments={"operation": "write", "path": "note.txt", "content": "ok"},
            ),
        ],
    )
    with pytest.raises(SimulatedCrash):
        runtime1.run(task.task_id)

    crashed = SQLiteStore(db_path).get_state(task.task_id)
    assert crashed.plan[1].status is PlanStepStatus.RUNNING
    assert crashed.tool_calls[-1].status is ToolCallStatus.RUNNING
    assert len(CompanyRepository(db_path).list_tickets()) == 1

    # Restart: brand-new store, repository, tools, runtime against the same DB.
    store2 = SQLiteStore(db_path)
    runtime2 = ExecutionRuntime(
        store=store2, registry=_registry(db_path, workspace), workspace_path=workspace
    )
    final = runtime2.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert [t.ticket_id for t in CompanyRepository(db_path).list_tickets()] == ["T-10001"]

    step2_calls = [c for c in final.tool_calls if c.step_id == "s2"]
    assert [c.status for c in step2_calls] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCEEDED,
    ]
    assert step2_calls[0].error_type == "interrupted"
    assert step2_calls[1].result["data"]["idempotent_replay"] is True
    assert final.extracted_information["last_ticket_id"] == "T-10001"
    # Completed step 1 was not re-run.
    assert len([c for c in final.tool_calls if c.step_id == "s1"]) == 1
    types = [e.event_type for e in store2.get_events(task.task_id)]
    assert EventType.STEP_RESUMED in types


def test_interrupted_approved_update_resumes_once(db_path: Path, workspace: Path) -> None:
    crash_tool = CrashAfterCommitTool(CompanyRepository(db_path), crash_on="update_employee")
    runtime1 = ExecutionRuntime(
        store=SQLiteStore(db_path),
        registry=_registry(db_path, workspace, crash_tool),
        workspace_path=workspace,
    )
    task = runtime1.create_task(
        user_goal="Promote E-17",
        plan=[
            PlanStep(
                step_id="upd",
                description="Update title",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                    "fields": {"title": "Senior Engineer"},
                },
            ),
            PlanStep(
                step_id="note",
                description="Write note",
                tool_name="file",
                arguments={"operation": "write", "path": "e17.txt", "content": "done"},
            ),
        ],
    )
    assert runtime1.run(task.task_id).final_status is TaskStatus.NEEDS_APPROVAL
    with pytest.raises(SimulatedCrash):
        runtime1.approve_task(task.task_id)

    runtime2 = ExecutionRuntime(
        store=SQLiteStore(db_path),
        registry=_registry(db_path, workspace),
        workspace_path=workspace,
    )
    final = runtime2.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.approval_status is ApprovalStatus.NONE
    assert final.pending_action is None
    assert CompanyRepository(db_path).get_employee("E-17").title == "Senior Engineer"
    upd_calls = [c for c in final.tool_calls if c.step_id == "upd"]
    assert [c.status for c in upd_calls] == [ToolCallStatus.FAILED, ToolCallStatus.SUCCEEDED]
    assert upd_calls[1].result["data"]["idempotent_replay"] is True


def test_tool_exception_fails_step_instead_of_succeeding(
    store: SQLiteStore, company_repo: CompanyRepository, workspace: Path
) -> None:
    class BrokenTool(CompanyAPITool):
        def execute(self, args, *, idempotency_key=None):
            raise RuntimeError("disk I/O error")

    registry = ToolRegistry()
    registry.register(BrokenTool(company_repo))
    registry.register(FileTool(workspace))
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="ticket",
        plan=[
            PlanStep(
                step_id="s1",
                description="Create ticket",
                tool_name="company_api",
                arguments=dict(TICKET_ARGS),
            )
        ],
    )
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert final.plan[0].status is PlanStepStatus.FAILED
    assert all(c.status is ToolCallStatus.FAILED for c in final.tool_calls)
    assert "disk I/O error" in (final.tool_calls[0].error_message or "")
