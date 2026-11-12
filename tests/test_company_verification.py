"""Independent verification of the v0.3.0 company operations: re-reads persisted
state, detects tampering and side effects, and cannot be bypassed by criteria."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.enums import TaskStatus, VerificationStatus
from app.models.goal import SuccessCriterion
from tests.company_support import World, checks, make_world, step


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def reverify(world: World, task_id: str):
    return world.runtime.verifier.verify(world.runtime.load(task_id))


def failed_messages(result) -> str:
    return " | ".join(c.message for c in result.checks if not c.passed)


def test_ticket_update_verifies_persisted_fields(world: World) -> None:
    state = world.run(step("u", "update_ticket", ticket_id="T-10009", fields={"status": "closed"}))
    assert state.verification_status is VerificationStatus.PASSED
    assert checks(state) == {"step_completed": True, "ticket_fields": True}
    world.execute("UPDATE company_tickets SET status = 'resolved' WHERE ticket_id = 'T-10009'")
    result = reverify(world, state.task_id)
    assert result.status is VerificationStatus.FAILED
    assert "mismatch" in failed_messages(result)


def test_ticket_update_detects_unintended_side_effects(world: World) -> None:
    state = world.run(step("u", "update_ticket", ticket_id="T-10005", fields={"priority": "high"}))
    assert state.verification_status is VerificationStatus.PASSED
    world.execute("UPDATE company_tickets SET subject = 'tampered' WHERE ticket_id = 'T-10005'")
    result = reverify(world, state.task_id)
    assert "Unintended ticket changes" in failed_messages(result) and "subject" in failed_messages(result)
    evidence_types = {e.type for e in result.evidence}
    assert {"ticket_record", "ticket_before_state"} <= evidence_types


def test_assignment_verifies_assignee_and_eligibility(world: World) -> None:
    state = world.run(step("a", "assign_ticket", ticket_id="T-10002", employee_id="E-42"))
    assert checks(state) == {"step_completed": True, "ticket_assignment": True}
    world.execute("UPDATE company_employees SET department = 'Finance' WHERE employee_id = 'E-42'")
    assert "not an active Support employee" in failed_messages(reverify(world, state.task_id))
    world.execute("UPDATE company_employees SET department = 'Support' WHERE employee_id = 'E-42'")
    world.execute("UPDATE company_tickets SET assignee_id = 'E-43' WHERE ticket_id = 'T-10002'")
    assert "assigned to E-43" in failed_messages(reverify(world, state.task_id))


def test_customer_creation_verifies_record_and_unique_identity(world: World) -> None:
    state = world.run(step("c", "create_customer", name="Pat Lee", company="Nimbus", email="Pat@Nimbus.example"))
    assert checks(state) == {"step_completed": True, "customer_record": True}
    world.execute(
        "INSERT INTO company_customers VALUES ('C-2000', 'Copy', 'PAT@nimbus.example', 'Nimbus', '2026-01-01')"
    )
    assert "shared by" in failed_messages(reverify(world, state.task_id))
    world.execute("DELETE FROM company_customers WHERE customer_id IN ('C-2000', 'C-1011')")
    assert "not found" in failed_messages(reverify(world, state.task_id))


def test_deactivation_verifies_status_and_open_ticket_constraint(world: World) -> None:
    state = world.run(step("d", "deactivate_employee", employee_id="E-70"))
    assert checks(state) == {"step_completed": True, "employee_status": True}
    world.execute("UPDATE company_tickets SET assignee_id = 'E-70' WHERE ticket_id = 'T-10001'")
    assert "still has open tickets" in failed_messages(reverify(world, state.task_id))
    world.execute("UPDATE company_tickets SET assignee_id = NULL WHERE ticket_id = 'T-10001'")
    world.execute("UPDATE company_employees SET title = 'Changed' WHERE employee_id = 'E-70'")
    assert "Unintended employee changes" in failed_messages(reverify(world, state.task_id))


def test_listing_is_never_reported_as_verified_business_change(world: World) -> None:
    state = world.run(step("l", "list_tickets", filters={"unassigned": True}))
    assert state.verification_status is VerificationStatus.UNVERIFIED
    assert state.final_status is TaskStatus.COMPLETED


def test_explicit_criteria_cannot_bypass_derived_checks(world: World) -> None:
    """A planner-supplied criterion that trivially passes does not replace the
    checks derived from the mutating call."""
    trivial = SuccessCriterion(
        description="Employee exists", criterion_type="employee", expected_value={"employee_id": "E-17"}
    )
    state = world.runtime.create_task(
        user_goal="close ticket",
        plan=[step("u", "update_ticket", ticket_id="T-10010", fields={"status": "closed"})],
        success_criteria=[trivial],
    )
    state = world.runtime.run(state.task_id)
    state = world.runtime.approve_task(state.task_id)
    assert state.verification_status is VerificationStatus.PASSED
    world.execute("UPDATE company_tickets SET status = 'resolved' WHERE ticket_id = 'T-10010'")
    result = reverify(world, state.task_id)
    assert result.status is VerificationStatus.FAILED
    assert {c.name: c.passed for c in result.checks}["employee_exists"] is True


@pytest.mark.parametrize(
    "ctype,expected",
    [
        ("ticket_fields", {"ticket_id": "T-10001", "subject": "x"}),
        ("ticket_fields", {"ticket_id": "T-10001"}),
        ("ticket_assignment", {"ticket_id": "T-10001", "employee_id": "E-40", "queue": "a"}),
        ("customer_record", {"name": "Acme"}),
        ("employee_status", {"employee_id": "E-17", "status": "on_leave"}),
        ("employee_status", {"employee_id": "E-17", "status": "inactive", "reason": "x"}),
    ],
)
def test_unsupported_company_criteria_fail_explicitly(world: World, ctype, expected) -> None:
    state = world.runtime.create_task(
        user_goal="probe",
        plan=[step("r", "get_employee", employee_id="E-17")],
        success_criteria=[SuccessCriterion(description="probe", criterion_type=ctype, expected_value=expected)],
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.verification_status is VerificationStatus.FAILED
    (check,) = final.verification_checks
    assert check["name"] == "unsupported_criterion" and check["passed"] is False


def test_criterion_bound_to_a_failed_call_fails(world: World) -> None:
    criterion = SuccessCriterion(
        description="bound",
        criterion_type="ticket_fields",
        expected_value={"ticket_id": "T-10001", "priority": "urgent", "source_call_id": "call_missing"},
    )
    state = world.runtime.create_task(
        user_goal="probe", plan=[step("r", "get_ticket", ticket_id="T-10001")], success_criteria=[criterion]
    )
    final = world.runtime.run(state.task_id)
    assert final.verification_status is VerificationStatus.FAILED
    assert "did not succeed" in final.verification_checks[0]["message"]
