"""Phase 5 recovery / bounded-retry tests."""

from pathlib import Path

from app.models import PlanStep, PlanStepStatus, TaskStatus, ToolCallStatus
from app.models.enums import FailureType
from app.runtime import EventType, ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.world import CompanyRepository


def _ticket_step(step_id: str = "step_ticket") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="Create ticket",
        tool_name="company_api",
        arguments={
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": "Billing discrepancy",
            "description": "$240 mismatch",
            "priority": "high",
        },
    )


def test_transient_failure_recovers(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    failure_injector: FailureInjector,
    workspace: Path,
) -> None:
    failure_injector.inject_once(
        "company_api.create_ticket",
        FailureType.TRANSIENT.value,
        "503 Service Unavailable",
    )
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(user_goal="Create ticket", plan=[_ticket_step()])
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.plan[0].status is PlanStepStatus.COMPLETED
    assert len(company_repo.list_tickets()) == 1
    assert company_repo.list_tickets()[0].ticket_id == "T-10001"
    assert len(final.tool_calls) == 2
    assert final.tool_calls[0].status is ToolCallStatus.FAILED
    assert final.tool_calls[1].status is ToolCallStatus.SUCCEEDED
    assert len(final.failures) == 1
    assert final.failures[0].recovery_action == "retry"


def test_recovery_events_persisted(
    store: SQLiteStore,
    registry: ToolRegistry,
    failure_injector: FailureInjector,
    workspace: Path,
) -> None:
    failure_injector.inject_once(
        "company_api.create_ticket",
        FailureType.TRANSIENT.value,
        "503 Service Unavailable",
    )
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(user_goal="Create ticket", plan=[_ticket_step()])
    runtime.run(task.task_id)

    types = [e.event_type for e in store.get_events(task.task_id)]
    assert EventType.FAILURE_DETECTED in types
    assert EventType.RECOVERY_EVALUATED in types
    assert EventType.RECOVERY_RETRY_STARTED in types
    assert EventType.RECOVERY_RETRY_SUCCEEDED in types
    assert EventType.TASK_COMPLETED in types


def test_non_transient_failure_not_retried(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Invalid ticket",
        plan=[
            PlanStep(
                step_id="bad",
                description="Invalid create",
                tool_name="company_api",
                arguments={
                    "operation": "create_ticket",
                    "customer_id": "C-1001",
                    # subject missing → validation failure
                    "description": "x",
                },
            )
        ],
    )
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert len(final.tool_calls) == 1
    assert company_repo.list_tickets() == []
    types = [e.event_type for e in store.get_events(task.task_id)]
    assert EventType.RECOVERY_RETRY_STARTED not in types


def test_retry_budget_is_bounded(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    failure_injector: FailureInjector,
    workspace: Path,
) -> None:
    failure_injector.inject_always(
        "company_api.create_ticket",
        FailureType.TRANSIENT.value,
        "503 Service Unavailable",
    )
    runtime = ExecutionRuntime(
        store=store,
        registry=registry,
        max_recovery_attempts=2,
        workspace_path=workspace,
    )
    task = runtime.create_task(user_goal="Create ticket", plan=[_ticket_step()])
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    # 1 initial + 2 recovery retries
    assert len(final.tool_calls) == 3
    assert all(c.status is ToolCallStatus.FAILED for c in final.tool_calls)
    assert company_repo.list_tickets() == []
    types = [e.event_type for e in store.get_events(task.task_id)]
    assert EventType.RECOVERY_EXHAUSTED in types


def test_recovered_step_allows_later_steps(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
    failure_injector: FailureInjector,
) -> None:
    failure_injector.inject_once(
        "company_api.create_ticket",
        FailureType.TRANSIENT.value,
        "503 Service Unavailable",
    )
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Find, ticket, confirm",
        plan=[
            PlanStep(
                step_id="s1",
                description="Find Acme",
                tool_name="company_api",
                arguments={"operation": "find_customer", "company": "Acme Corp"},
            ),
            _ticket_step("s2"),
            PlanStep(
                step_id="s3",
                description="Write confirmation",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "ticket_ok.txt",
                    "content": "ticket created",
                },
            ),
        ],
    )
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert [s.status for s in final.plan] == [
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
        PlanStepStatus.COMPLETED,
    ]
    assert len(company_repo.list_tickets()) == 1
    assert (workspace / "ticket_ok.txt").exists()


def test_recovery_history_persists_across_runtime_instances(
    db_path: Path,
    workspace: Path,
    failure_injector: FailureInjector,
) -> None:
    store1 = SQLiteStore(db_path)
    repo = CompanyRepository(db_path)
    registry1 = ToolRegistry()
    registry1.register(CompanyAPITool(repo, failure_injector=failure_injector))
    registry1.register(FileTool(workspace))
    failure_injector.inject_once(
        "company_api.create_ticket",
        FailureType.TRANSIENT.value,
        "503 Service Unavailable",
    )

    runtime1 = ExecutionRuntime(store=store1, registry=registry1, workspace_path=workspace)
    task = runtime1.create_task(user_goal="ticket", plan=[_ticket_step()])
    final1 = runtime1.run(task.task_id)
    assert final1.final_status is TaskStatus.COMPLETED

    store2 = SQLiteStore(db_path)
    loaded = store2.get_state(task.task_id)
    assert loaded is not None
    assert loaded.final_status is TaskStatus.COMPLETED
    assert len(loaded.failures) == 1
    assert loaded.failures[0].recovery_action == "retry"
    assert len(loaded.tool_calls) == 2
    events = store2.get_events(task.task_id)
    assert any(e.event_type == EventType.RECOVERY_RETRY_SUCCEEDED for e in events)
