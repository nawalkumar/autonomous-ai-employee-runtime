"""Phase 6 independent verification + evidence tests."""

from pathlib import Path

from app.models import (
    InterpretedGoal,
    PlanStep,
    SuccessCriterion,
    TaskStatus,
    VerificationStatus,
)
from app.models.enums import FailureType, ToolCallStatus
from app.runtime import EventType, ExecutionRuntime
from app.store import SQLiteStore
from app.tools import FailureInjector, ToolRegistry
from app.verify import OutcomeVerifier
from app.world import CompanyRepository


def _ticket_plan() -> list[PlanStep]:
    return [
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
            arguments={
                "operation": "create_ticket",
                "customer_id": "C-1001",
                "subject": "Billing discrepancy $240",
                "description": "Acme Corp reported a $240 billing discrepancy.",
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
                "content": "Created ticket for Acme Corp $240",
            },
        ),
    ]


def test_successful_ticket_independently_verified(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Create Acme ticket for $240",
        interpreted_goal=InterpretedGoal(
            objective="Create billing ticket",
            entities={"company": "Acme Corp", "amount": "$240"},
            success_criteria=[
                SuccessCriterion(
                    description="Ticket exists for Acme with $240",
                    criterion_type="ticket_exists",
                    target="tickets",
                    expected_value={
                        "customer_id": "C-1001",
                        "company": "Acme Corp",
                        "text_contains": "240",
                    },
                ),
                SuccessCriterion(
                    description="Confirmation file exists",
                    criterion_type="file_exists",
                    target="workspace",
                    expected_value={
                        "path": "ticket_confirmation.txt",
                        "content_contains": "Acme Corp",
                    },
                ),
            ],
        ),
        plan=_ticket_plan(),
    )
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.verification_checks
    assert any(e.type.startswith("ticket") for e in final.evidence)
    assert company_repo.get_ticket("T-10001") is not None


def test_tool_success_but_missing_ticket_fails_verification(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
    verifier: OutcomeVerifier,
) -> None:
    runtime = ExecutionRuntime(
        store=store, registry=registry, verifier=verifier, workspace_path=workspace
    )
    task = runtime.create_task(
        user_goal="Create ticket",
        success_criteria=[
            SuccessCriterion(
                description="Ticket must exist",
                criterion_type="ticket_exists",
                expected_value={"customer_id": "C-1001", "text_contains": "240"},
            )
        ],
        plan=[
            PlanStep(
                step_id="s1",
                description="Create ticket",
                tool_name="company_api",
                arguments={
                    "operation": "create_ticket",
                    "customer_id": "C-1001",
                    "subject": "Billing discrepancy $240",
                    "description": "$240 mismatch",
                    "priority": "high",
                },
            ),
            # Keeps the task from verifying until after we can remove the ticket.
            PlanStep(
                step_id="s2",
                description="Read employee",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-17"},
            ),
        ],
    )
    mid = runtime.execute_next(task.task_id)
    assert mid.plan[0].status.value == "completed"
    assert mid.tool_calls[-1].status is ToolCallStatus.SUCCEEDED
    ticket_id = mid.extracted_information["last_ticket_id"]
    assert company_repo.get_ticket(ticket_id) is not None

    # Tool claimed success, but actual world state is now missing the ticket.
    assert company_repo.delete_ticket(ticket_id) is True
    assert company_repo.get_ticket(ticket_id) is None

    final = runtime.run(task.task_id)
    assert mid.tool_calls[-1].status is ToolCallStatus.SUCCEEDED  # action success
    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED
    assert final.final_status is not TaskStatus.COMPLETED
    assert any(
        f.failure_type is FailureType.VERIFICATION_FAILED for f in final.failures
    )


def test_wrong_customer_fails_verification(
    store: SQLiteStore,
    company_repo: CompanyRepository,
    workspace: Path,
    verifier: OutcomeVerifier,
) -> None:
    # Create ticket for Globex while expecting Acme.
    ticket = company_repo.create_ticket(
        customer_id="C-1002",
        subject="Billing discrepancy $240",
        description="$240 mismatch",
        priority="high",
    )
    from app.models import ExecutionState

    state = ExecutionState(
        user_goal="Acme ticket",
        success_criteria=[
            SuccessCriterion(
                description="Acme ticket",
                criterion_type="ticket_exists",
                expected_value={
                    "ticket_id": ticket.ticket_id,
                    "customer_id": "C-1001",
                    "company": "Acme Corp",
                },
            )
        ],
        extracted_information={"last_ticket_id": ticket.ticket_id},
        final_status=TaskStatus.RUNNING,
    )
    result = verifier.verify(state)
    assert result.passed is False
    assert result.status is VerificationStatus.FAILED
    assert "mismatch" in (result.failure_reason or "").lower()


def test_wrong_amount_details_fail_verification(
    store: SQLiteStore,
    company_repo: CompanyRepository,
    verifier: OutcomeVerifier,
) -> None:
    ticket = company_repo.create_ticket(
        customer_id="C-1001",
        subject="Billing discrepancy $999",
        description="Wrong amount",
        priority="high",
    )
    from app.models import ExecutionState

    state = ExecutionState(
        user_goal="Acme $240 ticket",
        success_criteria=[
            SuccessCriterion(
                description="Amount 240",
                criterion_type="ticket_exists",
                expected_value={
                    "ticket_id": ticket.ticket_id,
                    "customer_id": "C-1001",
                    "text_contains": "240",
                },
            )
        ],
        extracted_information={"last_ticket_id": ticket.ticket_id},
    )
    result = verifier.verify(state)
    assert result.passed is False
    assert "240" in (result.failure_reason or "")


def test_confirmation_file_verified(
    store: SQLiteStore,
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Write note",
        plan=[
            PlanStep(
                step_id="f1",
                description="Write confirmation",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "notes/ok.txt",
                    "content": "confirmation complete",
                },
            )
        ],
    )
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert (workspace / "notes/ok.txt").exists()


def test_missing_confirmation_file_fails_verification(
    store: SQLiteStore,
    registry: ToolRegistry,
    workspace: Path,
    verifier: OutcomeVerifier,
) -> None:
    runtime = ExecutionRuntime(
        store=store, registry=registry, verifier=verifier, workspace_path=workspace
    )
    task = runtime.create_task(
        user_goal="Write note",
        plan=[
            PlanStep(
                step_id="f1",
                description="Write confirmation",
                tool_name="file",
                arguments={
                    "operation": "write",
                    "path": "will_delete.txt",
                    "content": "temporary",
                },
            ),
            PlanStep(
                step_id="f2",
                description="Read employee",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-17"},
            ),
        ],
    )
    mid = runtime.execute_next(task.task_id)
    assert mid.tool_calls[-1].status is ToolCallStatus.SUCCEEDED
    (workspace / "will_delete.txt").unlink()

    final = runtime.run(task.task_id)
    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED


def test_evidence_persists_across_restart(
    db_path: Path,
    workspace: Path,
    failure_injector: FailureInjector,
) -> None:
    from app.tools import CompanyAPITool, FileTool, ToolRegistry

    store1 = SQLiteStore(db_path)
    repo = CompanyRepository(db_path)
    registry = ToolRegistry()
    registry.register(CompanyAPITool(repo, failure_injector=failure_injector))
    registry.register(FileTool(workspace))
    runtime1 = ExecutionRuntime(
        store=store1, registry=registry, workspace_path=workspace
    )
    task = runtime1.create_task(user_goal="ticket+file", plan=_ticket_plan())
    final1 = runtime1.run(task.task_id)
    assert final1.final_status is TaskStatus.COMPLETED
    assert final1.evidence

    store2 = SQLiteStore(db_path)
    loaded = store2.get_state(task.task_id)
    assert loaded is not None
    assert loaded.verification_status is VerificationStatus.PASSED
    assert loaded.evidence
    assert loaded.verification_checks


def test_verification_audit_events(
    store: SQLiteStore,
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(user_goal="ticket+file", plan=_ticket_plan())
    runtime.run(task.task_id)
    types = [e.event_type for e in store.get_events(task.task_id)]
    assert EventType.VERIFICATION_STARTED in types
    assert EventType.VERIFICATION_CHECK in types
    assert EventType.VERIFICATION_PASSED in types
    assert EventType.EVIDENCE_RECORDED in types
    assert EventType.TASK_COMPLETED in types


def test_recovery_then_verification_completes(
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
    task = runtime.create_task(user_goal="recover+verify", plan=_ticket_plan())
    final = runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert len(company_repo.list_tickets()) == 1
    types = [e.event_type for e in store.get_events(task.task_id)]
    assert EventType.RECOVERY_RETRY_SUCCEEDED in types
    assert EventType.VERIFICATION_PASSED in types
