"""Phase 3 deterministic execution runtime tests."""

from pathlib import Path

from app.models import (
    InterpretedGoal,
    PlanStep,
    PlanStepStatus,
    TaskStatus,
    ToolCallStatus,
)
from app.models.enums import FailureType
from app.runtime import EventType, ExecutionRuntime, idempotency_key_for
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.world import CompanyRepository


def test_success_update_employee_and_write_file(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Update employee E-17 title to Senior Engineer and write a note",
        interpreted_goal=InterpretedGoal(
            objective="Promote E-17 and record confirmation"
        ),
        plan=[
            PlanStep(
                step_id="step_1",
                description="Update employee title",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                    "fields": {"title": "Senior Engineer"},
                },
            ),
            PlanStep(
                step_id="step_2",
                description="Write confirmation file",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "employee_update.txt",
                    "content": "E-17 title updated to Senior Engineer",
                },
            ),
        ],
    )

    paused = runtime.run(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert company_repo.get_employee("E-17").title != "Senior Engineer"

    final = runtime.approve_task(state.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert all(s.status is PlanStepStatus.COMPLETED for s in final.plan)
    assert len(final.tool_calls) == 2
    assert all(c.status is ToolCallStatus.SUCCEEDED for c in final.tool_calls)
    assert len(final.observations) == 2
    assert all(o.ok for o in final.observations)

    employee = company_repo.get_employee("E-17")
    assert employee is not None
    assert employee.title == "Senior Engineer"
    note = workspace / "employee_update.txt"
    assert note.exists()
    assert "Senior Engineer" in note.read_text(encoding="utf-8")

    events = store.get_events(state.task_id)
    types = [e.event_type for e in events]
    assert EventType.TASK_CREATED in types
    assert EventType.PLAN_STARTED in types
    assert EventType.APPROVAL_REQUESTED in types
    assert EventType.APPROVAL_APPROVED in types
    assert EventType.TOOL_CALLED in types
    assert EventType.TOOL_SUCCEEDED in types
    assert EventType.STEP_SUCCEEDED in types
    assert EventType.TASK_COMPLETED in types


def test_validation_failure_is_not_retried(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Create a ticket with invalid args",
        plan=[
            PlanStep(
                step_id="step_ticket",
                description="Create ticket with missing subject",
                tool_name="company_api",
                arguments={
                    "operation": "create_ticket",
                    "customer_id": "C-1001",
                    "description": "$240 mismatch",
                    "priority": "high",
                },
            ),
            PlanStep(
                step_id="step_should_not_run",
                description="Must not execute after failure",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "should_not_exist.txt",
                    "content": "nope",
                },
            ),
        ],
    )

    final = runtime.run(state.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert final.plan[0].status is PlanStepStatus.FAILED
    assert final.plan[1].status is PlanStepStatus.PENDING
    assert len(final.tool_calls) == 1
    assert final.tool_calls[0].status is ToolCallStatus.FAILED
    assert final.failures[0].failure_type is FailureType.INVALID_ARGS
    assert company_repo.list_tickets() == []

    again = runtime.run(state.task_id)
    assert again.final_status is TaskStatus.FAILED
    assert len(again.tool_calls) == 1
    assert company_repo.list_tickets() == []


def test_multi_step_ticket_flow(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="Find Acme, create ticket, write confirmation",
        plan=[
            PlanStep(
                step_id="s1",
                description="Find Acme Corp",
                tool_name="company_api",
                arguments={"operation": "find_customer", "company": "Acme Corp"},
            ),
            PlanStep(
                step_id="s2",
                description="Create ticket",
                tool_name="company_api",
                arguments={
                    "operation": "create_ticket",
                    "customer_id": "C-1001",
                    "subject": "Billing discrepancy",
                    "description": "$240 mismatch",
                    "priority": "high",
                },
            ),
            PlanStep(
                step_id="s3",
                description="Write confirmation",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "ticket_confirmation.txt",
                    "content": "Created ticket for Acme Corp",
                },
            ),
        ],
    )

    final = runtime.run(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert [s.status for s in final.plan] == [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
    ]
    assert final.extracted_information.get("last_customer_id") == "C-1001"
    assert final.extracted_information.get("last_ticket_id") == "T-10001"
    assert company_repo.get_ticket("T-10001") is not None
    assert (workspace / "ticket_confirmation.txt").exists()
    assert len(final.observations) == 3


def test_persist_and_resume_does_not_rerun_completed_step(
    db_path: Path,
    workspace: Path,
    failure_injector: FailureInjector,
) -> None:
    store1 = SQLiteStore(db_path)
    repo = CompanyRepository(db_path)
    registry1 = ToolRegistry()
    registry1.register(
        CompanyAPITool(repo, failure_injector=failure_injector)
    )
    registry1.register(FileTool(workspace))

    runtime1 = ExecutionRuntime(store=store1, registry=registry1, workspace_path=workspace)
    state = runtime1.create_task(
        user_goal="Resume demo",
        plan=[
            PlanStep(
                step_id="r1",
                description="Update employee",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                    "fields": {"title": "Senior Engineer"},
                },
            ),
            PlanStep(
                step_id="r2",
                description="Write note",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "resume_note.txt",
                    "content": "step 2",
                },
            ),
            PlanStep(
                step_id="r3",
                description="Write second note",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "resume_note_2.txt",
                    "content": "step 3",
                },
            ),
        ],
    )

    paused = runtime1.execute_next(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.plan[0].status is PlanStepStatus.PENDING
    assert paused.approval_status.value == "pending"
    assert len(paused.tool_calls) == 0

    # Fresh runtime + store against the same DB — pending approval survives restart.
    store2 = SQLiteStore(db_path)
    registry2 = ToolRegistry()
    registry2.register(
        CompanyAPITool(
            CompanyRepository(db_path),
            failure_injector=failure_injector,
        )
    )
    registry2.register(FileTool(workspace))
    runtime2 = ExecutionRuntime(store=store2, registry=registry2, workspace_path=workspace)

    loaded = runtime2.load(state.task_id)
    assert loaded.final_status is TaskStatus.NEEDS_APPROVAL
    assert loaded.pending_action is not None

    final = runtime2.approve_task(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert [s.status for s in final.plan] == [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
    ]
    # Sensitive step executes once after approval; later steps continue.
    assert len(final.tool_calls) == 3
    assert final.tool_calls[0].step_id == "r1"
    assert final.tool_calls[1].step_id == "r2"
    assert final.tool_calls[2].step_id == "r3"
    assert (workspace / "resume_note.txt").exists()
    assert (workspace / "resume_note_2.txt").exists()


def test_idempotency_key_stable(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    state = runtime.create_task(
        user_goal="key check",
        plan=[
            PlanStep(
                step_id="step_key",
                description="Read employee",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-17"},
            )
        ],
    )
    final = runtime.run(state.task_id)
    expected = idempotency_key_for(state.task_id, "step_key")
    assert final.tool_calls[0].idempotency_key == expected
    assert expected == f"{state.task_id}:step_key"


def test_runtime_uses_registry_not_direct_tools(
    store: SQLiteStore, registry: ToolRegistry, workspace: Path
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    assert "company_api" in registry
    assert runtime.registry.get("company_api").name == "company_api"
    state = runtime.create_task(
        user_goal="registry path",
        plan=[
            PlanStep(
                step_id="s",
                description="get employee",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-31"},
            )
        ],
    )
    final = runtime.run(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.tool_calls[0].tool_name == "company_api"
