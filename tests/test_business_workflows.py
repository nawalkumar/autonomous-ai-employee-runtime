"""Ten business workflows on the demo company (v0.3.0 milestone 2).

Only workflows 1 and 2 run from natural language: they are the two goals the
deterministic mock planner accepts. The other eight run as explicit plans
through the real runtime (policy gate, approvals, idempotent tools, retries and
independent verification). Where a workflow needs an earlier step's output in a
later step (dynamic binding), iteration or planning, the test supplies that
value itself between tasks; those parts are marked BLOCKED and are the subject
of milestones 3-6. No natural-language handler exists for workflows 3-10.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.llm.mock import parse_supported_goal
from app.models.enums import TaskStatus, VerificationStatus
from app.planning import PlanningPipeline
from app.llm import MockLLMClient
from tests.company_support import World, checks, make_world, step


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def plan_from_goal(world: World, goal: str):
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(goal)
    return world.runtime.create_task(
        user_goal=goal, interpreted_goal=planned.interpreted_goal, plan=planned.plan.steps
    )


def completed_and_verified(state) -> None:
    assert state.final_status is TaskStatus.COMPLETED, state.completion_summary
    assert state.verification_status is VerificationStatus.PASSED
    assert all(checks(state).values())


# 1 ---------------------------------------------------------------------------------


def test_w1_change_employee_title_from_natural_language(world: World) -> None:
    """SUPPORTED end to end: mock goal format A."""
    task = plan_from_goal(world, "Update employee E-17 title to Senior Engineer.")
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    completed_and_verified(world.runtime.approve_task(task.task_id))
    assert world.repo.get_employee("E-17").title == "Senior Engineer"


# 2 ---------------------------------------------------------------------------------


def test_w2_create_billing_ticket_from_natural_language(world: World) -> None:
    """SUPPORTED end to end: mock goal format B (creates T-10061 on the demo company)."""
    task = plan_from_goal(world, "Create a support ticket for Acme Corp about a $240 billing discrepancy.")
    completed_and_verified(world.runtime.run(task.task_id))
    assert world.repo.get_ticket("T-10061").customer_id == "C-1001"


# 3 ---------------------------------------------------------------------------------


def test_w3_transfer_employee_between_departments(world: World) -> None:
    """OPERATIONS SUPPORTED; natural language BLOCKED (no goal format; milestones 3-6)."""
    assert parse_supported_goal("Transfer employee E-61 to Engineering.") is None
    state = world.run(step("t", "update_employee", employee_id="E-61", fields={"department": "Engineering"}))
    completed_and_verified(state)
    employee = world.repo.get_employee("E-61")
    assert (employee.department, employee.title, employee.status) == ("Engineering", "Product Analyst", "active")


# 4 ---------------------------------------------------------------------------------


def test_w4_correct_employee_email(world: World) -> None:
    """OPERATIONS SUPPORTED; natural language BLOCKED. A duplicate email fails without change."""
    state = world.run(step("e", "update_employee", employee_id="E-71", fields={"email": "kwame.m@centralign.example"}))
    completed_and_verified(state)
    assert world.repo.get_employee("E-71").email == "kwame.m@centralign.example"

    before = world.snapshot()
    duplicate = world.run(step("e", "update_employee", employee_id="E-72", fields={"email": "kwame.m@centralign.example"}))
    assert duplicate.final_status is TaskStatus.FAILED
    assert world.snapshot()["company_employees"] == before["company_employees"]


# 5 ---------------------------------------------------------------------------------


def test_w5_resolve_then_close_ticket(world: World) -> None:
    """OPERATIONS SUPPORTED; natural language BLOCKED. Closing requires approval."""
    completed_and_verified(world.run(step("r", "update_ticket", ticket_id="T-10002", fields={"status": "resolved"})))
    paused = world.run(step("c", "update_ticket", ticket_id="T-10002", fields={"status": "closed"}), approve=False)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.pending_action.reason.startswith("Closing a ticket")
    assert world.repo.get_ticket("T-10002").status == "resolved"
    completed_and_verified(world.runtime.approve_task(paused.task_id, approver="lead"))
    assert world.repo.get_ticket("T-10002").status == "closed"
    invalid = world.run(step("x", "update_ticket", ticket_id="T-10001", fields={"status": "closed"}))
    assert invalid.final_status is TaskStatus.FAILED
    assert world.repo.get_ticket("T-10001").status == "open"


# 6 ---------------------------------------------------------------------------------


def test_w6_escalate_matching_customer_tickets(world: World) -> None:
    """PARTIAL. list_tickets and update_ticket are supported. BLOCKED: feeding the
    listing into one update per ticket (iteration + output binding) - here the
    test builds the follow-up plan from the listing itself."""
    listing = world.company.invoke(
        {"operation": "list_tickets", "filters": {"customer_id": "C-1002", "status": "open"}}
    ).data
    in_progress = world.company.invoke(
        {"operation": "list_tickets", "filters": {"customer_id": "C-1002", "status": "in_progress"}}
    ).data
    targets = [t["ticket_id"] for t in listing["tickets"] + in_progress["tickets"]]
    assert sorted(targets) == ["T-10005", "T-10006", "T-10007", "T-10008"]
    untouched = world.snapshot()["company_tickets"]

    state = world.run(
        *[step(f"esc_{n}", "update_ticket", ticket_id=t, fields={"priority": "high"}) for n, t in enumerate(targets)]
    )
    completed_and_verified(state)
    assert checks(state) == {"step_completed": True, "ticket_fields": True}
    assert len(state.verification_checks) == 2 * len(targets)
    after = {r[0]: r for r in world.snapshot()["company_tickets"]}
    for row in untouched:
        if row[0] not in targets:
            assert after[row[0]] == row
    assert {world.repo.get_ticket(t).priority for t in targets} == {"high"}


# 7 ---------------------------------------------------------------------------------


def test_w7_assign_ticket_to_support_engineer(world: World) -> None:
    """OPERATIONS SUPPORTED; natural language BLOCKED. Ineligible assignees are refused."""
    completed_and_verified(world.run(step("a", "assign_ticket", ticket_id="T-10003", employee_id="E-43")))
    assert world.repo.get_ticket("T-10003").assignee_id == "E-43"
    for employee_id in ("E-45", "E-50"):
        refused = world.run(step("a", "assign_ticket", ticket_id="T-10004", employee_id=employee_id))
        assert refused.final_status is TaskStatus.FAILED
        assert world.repo.get_ticket("T-10004").assignee_id is None


# 8 ---------------------------------------------------------------------------------


def test_w8_create_customer_and_welcome_ticket(world: World) -> None:
    """PARTIAL. create_customer (approval required) and create_ticket are supported.
    BLOCKED: using the new customer's ID in the ticket step (output binding) - here
    the test passes the ID from the first task into a second task."""
    created = world.run(
        step("c", "create_customer", name="Iris Novak", company="Lumen Studios", email="iris@lumen.example"),
        approve=False,
    )
    assert created.final_status is TaskStatus.NEEDS_APPROVAL
    created = world.runtime.approve_task(created.task_id, approver="sales-lead")
    completed_and_verified(created)
    customer_id = created.tool_calls[-1].result["data"]["customer"]["customer_id"]
    assert customer_id == "C-1011"

    welcome = world.run(
        step(
            "w", "create_ticket", customer_id=customer_id, subject="Welcome to CentrAlign",
            description="Onboarding call and account setup.", priority="low",
        )
    )
    completed_and_verified(welcome)
    assert world.rows("SELECT ticket_id FROM company_tickets WHERE customer_id = ?", customer_id) == [("T-10061",)]


# 9 ---------------------------------------------------------------------------------


def test_w9_daily_ticket_summary(world: World) -> None:
    """PARTIAL. Counts from list_tickets are checked against an independent SQL
    recount. BLOCKED: writing a summary whose content comes from the listing
    (output binding) - here the test composes the text and the file step verifies it."""
    summary = {}
    for status in ("open", "in_progress", "resolved", "closed"):
        listed = world.company.invoke({"operation": "list_tickets", "filters": {"status": status}, "limit": 1}).data
        summary[status] = listed["total"]
        assert listed["total"] == world.rows("SELECT COUNT(*) FROM company_tickets WHERE status = ?", status)[0][0]
    assert summary == {"open": 22, "in_progress": 11, "resolved": 16, "closed": 11}
    unassigned = world.company.invoke(
        {"operation": "list_tickets", "filters": {"unassigned": True, "priority": "urgent"}}
    ).data
    content = "Daily ticket summary\n" + "".join(f"{k}: {v}\n" for k, v in summary.items()) + (
        f"urgent unassigned: {', '.join(t['ticket_id'] for t in unassigned['tickets'])}\n"
    )
    state = world.run(step("f", "write", path="reports/daily_summary.txt", content=content))
    completed_and_verified(state)
    assert (world.workspace / "reports" / "daily_summary.txt").read_text(encoding="utf-8") == content
    assert "urgent unassigned: T-10001, T-10003" in content


# 10 --------------------------------------------------------------------------------


def test_w10_deactivate_employee_safely(world: World) -> None:
    """PARTIAL. Deactivation is refused while E-44 has open tickets; after the
    tickets are reassigned it succeeds with approval. BLOCKED: discovering the
    blocking tickets and reassigning them automatically (binding + planning)."""
    refused = world.run(step("d", "deactivate_employee", employee_id="E-44"))
    assert refused.final_status is TaskStatus.FAILED
    assert "T-10013, T-10014, T-10015" in refused.tool_calls[-1].error_message
    assert world.repo.get_employee("E-44").status == "active"

    blocking = world.repo.open_tickets_assigned_to("E-44")
    reassigned = world.run(
        *[step(f"r_{t}", "assign_ticket", ticket_id=t, employee_id="E-40") for t in blocking]
    )
    completed_and_verified(reassigned)

    paused = world.run(step("d", "deactivate_employee", employee_id="E-44"), approve=False)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    final = world.runtime.approve_task(paused.task_id, approver="hr-lead")
    completed_and_verified(final)
    assert world.repo.get_employee("E-44").status == "inactive"
    assert world.repo.open_tickets_assigned_to("E-44") == []
    assert world.rows("SELECT ticket_id FROM company_tickets WHERE assignee_id = 'E-40' AND ticket_id IN ('T-10013','T-10014','T-10015') ORDER BY 1") == [
        ("T-10013",), ("T-10014",), ("T-10015",)
    ]


def test_natural_language_is_still_limited_to_the_two_mock_goals() -> None:
    for goal in (
        "Escalate all open Globex tickets to high priority.",
        "Assign T-10003 to Tomasz Nowak.",
        "Deactivate employee E-44.",
        "Create a customer for Lumen Studios and open a welcome ticket.",
        "Write today's ticket summary.",
    ):
        assert parse_supported_goal(goal) is None, goal
