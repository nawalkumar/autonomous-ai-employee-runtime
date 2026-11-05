"""Regression tests: omitted requested operations must not verify as PASSED."""

from pathlib import Path

from app.llm import MockLLMClient
from app.models import (
    PlanStep,
    SuccessCriterion,
    TaskStatus,
    VerificationStatus,
)
from app.models.enums import FailureType
from app.planning import PlanningPipeline
from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import FailureInjector, ToolRegistry
from app.verify import OutcomeVerifier
from app.verify.rules import collect_criteria
from app.world import CompanyRepository

DEMO_GOAL = (
    "Create a support ticket for Acme Corp about a billing discrepancy of $240 "
    "and save a summary note."
)


def test_requested_summary_note_is_planned_and_verified(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    failure_injector: FailureInjector,
    workspace: Path,
) -> None:
    result = PlanningPipeline(MockLLMClient(), registry).interpret_and_plan(DEMO_GOAL)
    assert any(
        c.criterion_type == "file_exists" for c in result.interpreted_goal.success_criteria
    )
    ops = [(s.tool_name, s.arguments.get("operation")) for s in result.plan.steps]
    assert ops == [
        ("company_api", "find_customer"),
        ("company_api", "create_ticket"),
        ("file", "write"),
    ]

    failure_injector.inject_once(
        "company_api.create_ticket",
        FailureType.TRANSIENT.value,
        "503 Service Unavailable",
    )
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal=DEMO_GOAL,
        interpreted_goal=result.interpreted_goal,
        plan=result.plan.steps,
    )
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.retry_count == 1
    assert (workspace / "acme_billing_summary.txt").exists()
    assert len(company_repo.list_tickets()) == 1


def test_partial_plan_omitting_requested_note_fails_verification(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    result = PlanningPipeline(MockLLMClient(), registry).interpret_and_plan(DEMO_GOAL)
    partial = [s for s in result.plan.steps if s.tool_name != "file"]
    assert len(partial) == 2

    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal=DEMO_GOAL,
        interpreted_goal=result.interpreted_goal,
        plan=partial,
    )
    final = runtime.run(task.task_id)

    # The ticket really exists, but the requested note was never written.
    assert len(company_repo.list_tickets()) == 1
    assert not (workspace / "acme_billing_summary.txt").exists()
    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED
    assert "acme_billing_summary.txt" in (final.verification_summary or "")


def test_explicit_criteria_do_not_hide_derived_outcomes(
    store: SQLiteStore,
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Ticket plus note",
        success_criteria=[
            SuccessCriterion(
                description="Ticket exists",
                criterion_type="ticket_exists",
                expected_value={"customer_id": "C-1001", "subject_contains": "billing"},
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
                },
            ),
            PlanStep(
                step_id="s2",
                description="Write note",
                tool_name="file",
                arguments={"operation": "write", "path": "note.txt", "content": "done"},
            ),
            PlanStep(
                step_id="s3",
                description="Read employee",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-17"},
            ),
        ],
    )
    runtime.execute_next(task.task_id)
    runtime.execute_next(task.task_id)
    (workspace / "note.txt").unlink()

    final = runtime.run(task.task_id)
    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED


def test_unexecuted_mutating_step_fails_plan_coverage(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    # A matching ticket already exists, so the explicit criterion alone would pass.
    company_repo.create_ticket(
        customer_id="C-1001",
        subject="Billing discrepancy",
        description="pre-existing",
    )
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Create ticket",
        success_criteria=[
            SuccessCriterion(
                description="Ticket exists",
                criterion_type="ticket_exists",
                expected_value={"customer_id": "C-1001", "subject_contains": "billing"},
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
                    "subject": "Billing discrepancy",
                    "description": "new",
                },
            )
        ],
    )

    # The lifecycle forbids finalizing an unexecuted task; it stays QUEUED.
    assert runtime.verify_task(task.task_id).final_status is TaskStatus.QUEUED

    # The verifier itself still rejects the unexecuted mutating step.
    result = OutcomeVerifier(company_repo, workspace).verify(runtime.load(task.task_id))
    assert result.status is VerificationStatus.FAILED
    assert any(c.name == "step_completed" and not c.passed for c in result.checks)


def test_collect_criteria_merges_and_dedupes(
    store: SQLiteStore,
    registry: ToolRegistry,
    workspace: Path,
) -> None:
    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal="Write note",
        success_criteria=[
            SuccessCriterion(
                description="Note exists",
                criterion_type="file_exists",
                expected_value={"path": "n.txt", "content_contains": "hello"},
            )
        ],
        plan=[
            PlanStep(
                step_id="w",
                description="Write",
                tool_name="file",
                arguments={"operation": "write", "path": "n.txt", "content": "hello"},
            )
        ],
    )
    final = runtime.run(task.task_id)
    assert final.verification_status is VerificationStatus.PASSED

    criteria = collect_criteria(final)
    types = [c.criterion_type for c in criteria]
    # Explicit file criterion equals the derived one, so it appears once.
    assert types.count("file_exists") == 1
    assert types.count("step_completed") == 1
