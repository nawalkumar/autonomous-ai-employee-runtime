"""Regression tests: ticket criteria bind to this task's own creation results."""

from pathlib import Path

from app.models import (
    ExecutionState,
    PlanStep,
    SuccessCriterion,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
    VerificationStatus,
)
from app.models.enums import PlanStepStatus
from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import ToolRegistry
from app.verify import OutcomeVerifier
from app.verify.rules import collect_criteria
from app.world import CompanyRepository


def _create(step_id: str, subject: str) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=f"Create ticket {subject}",
        tool_name="company_api",
        arguments={
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": subject,
            "description": subject,
        },
    )


def _read_employee(step_id: str = "read") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="Read employee",
        tool_name="company_api",
        arguments={"operation": "get_employee", "employee_id": "E-17"},
    )


def _billing_criterion(**extra: str) -> SuccessCriterion:
    return SuccessCriterion(
        description="A billing ticket was created for Acme",
        criterion_type="ticket_exists",
        expected_value={"customer_id": "C-1001", "subject_contains": "billing", **extra},
    )


def _runtime(store: SQLiteStore, registry: ToolRegistry, workspace: Path) -> ExecutionRuntime:
    return ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)


# --- H1: each criterion is bound to its own create_ticket result ----------------


def test_two_tickets_with_different_subjects_both_verify(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Two tickets",
        plan=[_create("a", "Login issue"), _create("b", "Billing dispute")],
    )
    final = runtime.run(task.task_id)

    assert [t.ticket_id for t in company_repo.list_tickets()] == ["T-10001", "T-10002"]
    assert final.verification_status is VerificationStatus.PASSED
    assert final.final_status is TaskStatus.COMPLETED


def test_deleting_first_ticket_fails_even_if_second_matches_its_subject(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Two tickets",
        plan=[_create("a", "Billing"), _create("b", "Billing follow-up"), _read_employee()],
    )
    runtime.execute_next(task.task_id)
    runtime.execute_next(task.task_id)
    assert company_repo.delete_ticket("T-10001") is True

    final = runtime.run(task.task_id)
    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED
    assert "T-10001" in (final.verification_summary or "")


def test_each_ticket_criterion_is_bound_to_its_own_call(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Two tickets",
        plan=[_create("a", "Billing"), _create("b", "Billing follow-up"), _read_employee()],
    )
    runtime.execute_next(task.task_id)
    state = runtime.execute_next(task.task_id)

    calls = {c.step_id: c for c in state.tool_calls}
    ticket_criteria = [
        c.expected_value for c in collect_criteria(state) if c.criterion_type == "ticket_exists"
    ]
    assert sorted((e["source_step_id"], e["ticket_id"]) for e in ticket_criteria) == [
        ("a", "T-10001"),
        ("b", "T-10002"),
    ]
    for e in ticket_criteria:
        assert e["source_call_id"] == calls[e["source_step_id"]].call_id

    company_repo.delete_ticket("T-10001")
    result = OutcomeVerifier(company_repo, workspace).verify(state)
    by_id = {
        (c.expected or {}).get("ticket_id"): c.passed
        for c in result.checks
        if isinstance(c.expected, dict) and "source_call_id" in c.expected
    }
    assert by_id == {"T-10001": False, "T-10002": True}


def test_failed_or_interrupted_create_calls_cannot_satisfy_ticket_criteria(
    company_repo: CompanyRepository, workspace: Path
) -> None:
    existing = company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy", description="older task"
    )
    ticket_payload = {"data": {"ticket": existing.model_dump(mode="json")}}
    args = {
        "operation": "create_ticket",
        "customer_id": "C-1001",
        "subject": "Billing discrepancy",
        "description": "older task",
    }
    state = ExecutionState(
        user_goal="Create a billing ticket",
        success_criteria=[_billing_criterion()],
        plan=[_create("s1", "Billing discrepancy")],
        tool_calls=[
            ToolCallRecord(
                task_id="t",
                step_id="s1",
                tool_name="company_api",
                arguments=args,
                status=ToolCallStatus.FAILED,
                result=ticket_payload,
            ),
            ToolCallRecord(
                task_id="t",
                step_id="s1",
                tool_name="company_api",
                arguments=args,
                status=ToolCallStatus.FAILED,
                error_type="interrupted",
            ),
        ],
        extracted_information={"last_ticket_id": existing.ticket_id},
    )
    result = OutcomeVerifier(company_repo, workspace).verify(state)
    assert result.passed is False
    assert not any(c.passed for c in result.checks if c.name.startswith("ticket"))


def test_malformed_create_result_fails_without_fallback(
    company_repo: CompanyRepository, workspace: Path
) -> None:
    existing = company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy", description="older task"
    )
    plan_step = _create("s1", "Billing discrepancy").model_copy(
        update={"status": PlanStepStatus.COMPLETED}
    )
    state = ExecutionState(
        user_goal="Create a billing ticket",
        plan=[plan_step],
        tool_calls=[
            ToolCallRecord(
                task_id="t",
                step_id="s1",
                tool_name="company_api",
                arguments=dict(plan_step.arguments),
                status=ToolCallStatus.SUCCEEDED,
                result={"ok": True, "data": {"unexpected": "shape"}},
            )
        ],
        extracted_information={"last_ticket_id": existing.ticket_id},
    )
    result = OutcomeVerifier(company_repo, workspace).verify(state)
    assert result.passed is False
    assert "no ticket_id" in (result.failure_reason or "")


# --- H2: only this task's create_ticket calls prove ticket creation -------------


def test_get_ticket_of_existing_ticket_does_not_prove_creation(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    old = company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy $240", description="earlier task"
    )
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Create a billing ticket for Acme",
        success_criteria=[
            SuccessCriterion(
                description="Billing ticket created for Acme",
                criterion_type="ticket_exists",
                expected_value={"company": "Acme Corp", "subject_contains": "billing"},
            )
        ],
        plan=[
            PlanStep(
                step_id="g",
                description="Read existing ticket",
                tool_name="company_api",
                arguments={"operation": "get_ticket", "ticket_id": old.ticket_id},
            )
        ],
    )
    final = runtime.run(task.task_id)

    assert final.extracted_information.get("last_ticket_id") == old.ticket_id
    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED


def test_pre_existing_customer_ticket_does_not_prove_creation(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy", description="earlier task"
    )
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Create a billing ticket for Acme",
        success_criteria=[_billing_criterion()],
        plan=[
            PlanStep(
                step_id="f",
                description="Find Acme",
                tool_name="company_api",
                arguments={"operation": "find_customer", "company": "Acme Corp"},
            )
        ],
    )
    final = runtime.run(task.task_id)

    assert final.verification_status is VerificationStatus.FAILED
    assert final.final_status is TaskStatus.FAILED


def test_creation_criterion_without_create_step_fails(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy", description="earlier task"
    )
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Create a billing ticket and save a note",
        success_criteria=[_billing_criterion()],
        plan=[
            PlanStep(
                step_id="w",
                description="Write note only",
                tool_name="file",
                arguments={"operation": "write", "path": "note.txt", "content": "done"},
            )
        ],
    )
    final = runtime.run(task.task_id)

    assert final.verification_status is VerificationStatus.FAILED
    assert any(
        c["name"] == "ticket_created" and not c["passed"] for c in final.verification_checks
    )


def test_current_task_ticket_passes_only_while_it_exists(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy", description="earlier task"
    )
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal="Create a billing ticket",
        success_criteria=[_billing_criterion()],
        plan=[_create("s1", "Billing discrepancy $240"), _read_employee()],
    )
    mid = runtime.execute_next(task.task_id)
    new_id = mid.tool_calls[-1].result["data"]["ticket"]["ticket_id"]
    assert new_id == "T-10002"

    passed = OutcomeVerifier(company_repo, workspace).verify(runtime.load(task.task_id))
    assert passed.passed is True
    verified_ids = {
        e.data.get("ticket_id") for e in passed.evidence if e.type == "ticket_details"
    }
    assert verified_ids == {new_id}

    company_repo.delete_ticket(new_id)
    final = runtime.run(task.task_id)
    # The earlier task's matching ticket must not stand in for the deleted one.
    assert company_repo.get_ticket("T-10001") is not None
    assert final.verification_status is VerificationStatus.FAILED


def test_read_only_get_ticket_goal_still_verifies(
    store: SQLiteStore, registry: ToolRegistry, company_repo: CompanyRepository, workspace: Path
) -> None:
    old = company_repo.create_ticket(
        customer_id="C-1001", subject="Billing discrepancy", description="earlier task"
    )
    runtime = _runtime(store, registry, workspace)
    task = runtime.create_task(
        user_goal=f"Look up ticket {old.ticket_id}",
        success_criteria=[
            SuccessCriterion(
                description="Requested ticket exists",
                criterion_type="ticket_exists",
                expected_value={"ticket_id": old.ticket_id, "customer_id": "C-1001"},
            )
        ],
        plan=[
            PlanStep(
                step_id="g",
                description="Read ticket",
                tool_name="company_api",
                arguments={"operation": "get_ticket", "ticket_id": old.ticket_id},
            )
        ],
    )
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert len(company_repo.list_tickets()) == 1
