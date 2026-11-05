"""Regression tests: tasks finalize only after every step completed, exactly once."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from app.models import ExecutionState, PlanStep, TaskStatus, ToolCallStatus
from app.models.enums import ApprovalStatus, PlanStepStatus, VerificationStatus
from app.runtime import EventType, ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FileTool, ToolRegistry
from app.tools.result import ToolResult
from app.world import CompanyRepository


def _create_ticket(step_id: str = "w") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="Create ticket",
        tool_name="company_api",
        arguments={
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": "Billing",
            "description": "$240",
        },
    )


def _read(step_id: str = "r", employee_id: str = "E-17") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="Read employee",
        tool_name="company_api",
        arguments={"operation": "get_employee", "employee_id": employee_id},
    )


def _update() -> PlanStep:
    return PlanStep(
        step_id="u",
        description="Update title",
        tool_name="company_api",
        arguments={
            "operation": "update_employee",
            "employee_id": "E-17",
            "fields": {"title": "Senior Engineer"},
        },
    )


def _runtime(store: SQLiteStore, registry: ToolRegistry, workspace: Path) -> ExecutionRuntime:
    return ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)


def _snapshot(runtime: ExecutionRuntime, task_id: str) -> tuple[str, int, int, int]:
    state = runtime.load(task_id)
    return (
        state.final_status.value,
        len(runtime.store.get_events(task_id)),
        len(state.evidence),
        len(state.tool_calls),
    )


class SimulatedCrash(BaseException):
    """Process death; not caught by the runtime's Exception guard."""


class CrashOnceTool(CompanyAPITool):
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


# 1
def test_verify_task_with_pending_read_step_stays_running(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(user_goal="ticket then read", plan=[_create_ticket(), _read()])
    runtime.execute_next(task.task_id)
    before = _snapshot(runtime, task.task_id)

    after = runtime.verify_task(task.task_id)

    assert after.final_status is TaskStatus.RUNNING
    assert [s.status for s in after.plan] == [PlanStepStatus.COMPLETED, PlanStepStatus.PENDING]
    assert after.verification_status is VerificationStatus.PENDING
    assert _snapshot(runtime, task.task_id) == before

    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert len(final.tool_calls) == 2


# 2
def test_verify_task_on_queued_task_is_a_no_op(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(user_goal="read only", plan=[_read()])
    before = _snapshot(runtime, task.task_id)

    after = runtime.verify_task(task.task_id)

    assert after.final_status is TaskStatus.QUEUED
    assert after.plan[0].status is PlanStepStatus.PENDING
    assert _snapshot(runtime, task.task_id) == before


# 3
def test_verify_task_preserves_pending_approval(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(user_goal="promote", plan=[_update(), _read()])
    paused = runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    before = _snapshot(runtime, task.task_id)

    after = runtime.verify_task(task.task_id)

    assert after.final_status is TaskStatus.NEEDS_APPROVAL
    assert after.approval_status is ApprovalStatus.PENDING
    assert after.pending_action == paused.pending_action
    assert _snapshot(runtime, task.task_id) == before
    assert company_repo.get_employee("E-17").title == "Software Engineer"

    final = runtime.approve_task(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert company_repo.get_employee("E-17").title == "Senior Engineer"
    assert len([c for c in final.tool_calls if c.step_id == "u"]) == 1


# 4
def test_verify_task_keeps_failed_step_task_failed(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="ticket then bad read", plan=[_create_ticket(), _read("r", "E-999")]
    )
    failed = runtime.run(task.task_id)
    assert failed.final_status is TaskStatus.FAILED
    assert failed.plan[1].status is PlanStepStatus.FAILED
    before = _snapshot(runtime, task.task_id)

    after = runtime.verify_task(task.task_id)

    assert after.final_status is TaskStatus.FAILED
    assert _snapshot(runtime, task.task_id) == before


# 5
def test_verify_task_leaves_interrupted_step_resumable(db_path: Path, workspace: Path) -> None:
    registry1 = ToolRegistry()
    registry1.register(CrashOnceTool(CompanyRepository(db_path), crash_on="get_employee"))
    registry1.register(FileTool(workspace))
    runtime1 = ExecutionRuntime(
        store=SQLiteStore(db_path), registry=registry1, workspace_path=workspace
    )
    task = runtime1.create_task(user_goal="ticket then read", plan=[_create_ticket(), _read()])
    with pytest.raises(SimulatedCrash):
        runtime1.run(task.task_id)

    registry2 = ToolRegistry()
    registry2.register(CompanyAPITool(CompanyRepository(db_path)))
    registry2.register(FileTool(workspace))
    runtime2 = ExecutionRuntime(
        store=SQLiteStore(db_path), registry=registry2, workspace_path=workspace
    )
    before = _snapshot(runtime2, task.task_id)
    after = runtime2.verify_task(task.task_id)
    assert after.final_status is TaskStatus.RUNNING
    assert after.plan[1].status is PlanStepStatus.RUNNING
    assert _snapshot(runtime2, task.task_id) == before

    final = runtime2.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert [c.status for c in final.tool_calls] == [
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.SUCCEEDED,
    ]
    assert final.tool_calls[1].error_type == "interrupted"
    assert len(CompanyRepository(db_path).list_tickets()) == 1


# 6
def test_repeated_verify_task_on_completed_task_is_idempotent(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(user_goal="ticket then read", plan=[_create_ticket(), _read()])
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    before = _snapshot(runtime, task.task_id)

    runtime.verify_task(task.task_id)
    runtime.verify_task(task.task_id)
    assert _snapshot(runtime, task.task_id) == before
    completed_events = [
        e for e in store.get_events(task.task_id) if e.event_type == EventType.TASK_COMPLETED
    ]
    assert len(completed_events) == 1

    # World drift after completion does not silently rewrite a terminal status.
    company_repo.delete_ticket("T-10001")
    assert runtime.verify_task(task.task_id).final_status is TaskStatus.COMPLETED
    assert _snapshot(runtime, task.task_id) == before


# 7
def test_repeated_verify_task_on_verification_failed_task_stays_failed(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="note then read",
        plan=[
            PlanStep(
                step_id="f",
                description="Write note",
                tool_name="file",
                arguments={"operation": "write", "path": "n.txt", "content": "x"},
            ),
            _read(),
        ],
    )
    runtime.execute_next(task.task_id)
    (workspace / "n.txt").unlink()
    failed = runtime.run(task.task_id)
    assert failed.final_status is TaskStatus.FAILED
    assert failed.verification_status is VerificationStatus.FAILED
    before = _snapshot(runtime, task.task_id)

    # Even if the outcome is repaired by hand, FAILED is not reversible.
    (workspace / "n.txt").write_text("x", encoding="utf-8")
    runtime.verify_task(task.task_id)
    after = runtime.verify_task(task.task_id)
    assert after.final_status is TaskStatus.FAILED
    assert _snapshot(runtime, task.task_id) == before


# 8
def test_verify_task_keeps_rejected_task_aborted(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(user_goal="promote", plan=[_update()])
    runtime.run(task.task_id)
    rejected = runtime.reject_task(task.task_id, reason="Not approved")
    before = _snapshot(runtime, task.task_id)

    after = runtime.verify_task(task.task_id)

    assert after.final_status is TaskStatus.ABORTED
    assert after.approval_status is ApprovalStatus.REJECTED
    assert after.completion_summary == rejected.completion_summary
    assert _snapshot(runtime, task.task_id) == before
    assert runtime.run(task.task_id).final_status is TaskStatus.ABORTED
    assert company_repo.get_employee("E-17").title == "Software Engineer"


# 9
@pytest.mark.parametrize(
    "status",
    [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.FAILED,
        PlanStepStatus.SKIPPED,
        PlanStepStatus.RUNNING,
    ],
)
def test_create_task_rejects_non_pending_initial_steps(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path, status: PlanStepStatus
) -> None:
    runtime = _runtime(store, registry, workspace)
    with pytest.raises(ValueError, match="must start with all plan steps PENDING"):
        runtime.create_task(
            user_goal="preset statuses",
            task_id="task_preset",
            plan=[_read("ok"), _read("bad").model_copy(update={"status": status})],
        )
    assert store.get_state("task_preset") is None


# 10
@pytest.mark.parametrize("status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_empty_plan_state_never_completes(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path, status: TaskStatus
) -> None:
    runtime = _runtime(store, registry, workspace)

    verify_only = ExecutionState(user_goal="empty", plan=[], final_status=status)
    store.save_state(verify_only)
    checked = runtime.verify_task(verify_only.task_id)
    assert checked.final_status is status
    assert checked.verification_status is not VerificationStatus.PASSED
    assert store.get_events(verify_only.task_id) == []

    run_state = ExecutionState(user_goal="empty", plan=[], final_status=status)
    store.save_state(run_state)
    ran = runtime.run(run_state.task_id)
    assert ran.final_status is TaskStatus.FAILED
    assert ran.verification_status is not VerificationStatus.PASSED
    assert "plan has no steps" in (ran.completion_summary or "")
    types = [e.event_type for e in store.get_events(run_state.task_id)]
    assert EventType.TASK_COMPLETED not in types
    assert EventType.VERIFICATION_STARTED not in types


def test_legacy_state_with_failed_or_skipped_steps_never_completes(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    legacy = ExecutionState(
        user_goal="legacy",
        plan=[
            _read("a").model_copy(update={"status": PlanStepStatus.FAILED}),
            _read("b").model_copy(update={"status": PlanStepStatus.SKIPPED}),
        ],
        final_status=TaskStatus.RUNNING,
    )
    store.save_state(legacy)

    assert runtime.verify_task(legacy.task_id).final_status is TaskStatus.RUNNING
    final = runtime.run(legacy.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.tool_calls == []
    assert "step 'a' is failed" in (final.completion_summary or "")
