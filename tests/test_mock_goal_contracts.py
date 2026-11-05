"""Regression tests: the mock accepts only its allowlisted goal contracts, fail-closed."""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.llm import MockLLMClient
from app.models import InterpretedGoal, SuccessCriterion, TaskStatus, VerificationStatus
from app.models.enums import ApprovalStatus, FailureType
from app.planning import GoalInterpreter, InterpretationError, Planner, PlanningError, PlanningPipeline
from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.world import CompanyRepository

B_TICKET = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
B_TICKET_NOTE = (
    "Create a support ticket for Acme Corp about a billing discrepancy of $240 "
    "and save a summary note."
)
A_TEST = "Update employee E-17's title to Senior Engineer and write a confirmation note."
A_DEMO = "Update employee E-17 title to Senior Engineer and confirm the change in a note file."
A_NO_NOTE = "Update employee E-17 title to Senior Engineer."


@dataclass
class World:
    db_path: Path
    workspace: Path
    repo: CompanyRepository
    injector: FailureInjector
    registry: ToolRegistry
    store: SQLiteStore

    def runtime(self) -> ExecutionRuntime:
        return ExecutionRuntime(
            store=self.store, registry=self.registry, workspace_path=self.workspace
        )

    def task_rows(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def event_rows(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@pytest.fixture
def world(tmp_path: Path) -> World:
    db_path = tmp_path / "data" / "runtime.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = CompanyRepository(db_path)
    injector = FailureInjector()
    registry = ToolRegistry()
    registry.register(CompanyAPITool(repo, failure_injector=injector))
    registry.register(FileTool(workspace))
    return World(db_path, workspace, repo, injector, registry, SQLiteStore(db_path))


def _attempt(world: World, goal: str):
    """Plan, then create and run a task only if planning succeeds (CLI-like flow)."""
    try:
        result = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(goal)
    except (InterpretationError, PlanningError) as exc:
        return None, None, exc
    runtime = world.runtime()
    task = runtime.create_task(
        user_goal=goal, interpreted_goal=result.interpreted_goal, plan=result.plan.steps
    )
    return result, runtime.run(task.task_id), None


# --- The 23 audit probes ------------------------------------------------------

REJECTED_PROBES = [
    ("3", B_TICKET[:-1] + ". Note that the customer is upset.", "does not match"),
    ("4", B_TICKET[:-1] + ". Do not create a summary.", "negated"),
    ("5", B_TICKET[:-1] + " and save the details to a file.", "does not match"),
    (
        "6",
        B_TICKET[:-1] + ", write a summary note, and email the customer.",
        "does not match",
    ),
    (
        "7",
        "Create a support ticket for Acme Corp about the $240 billing discrepancy. "
        "Then save a summary memo. Then escalate it to finance.",
        "does not match",
    ),
    ("8a", "Look up Acme Corp's billing history.", "read-only"),
    ("8b", "Show me the $240 invoice for Acme Corp.", "read-only"),
    ("8c", "What is the current title of employee E-17?", "read-only"),
    ("9a", "Delete employee E-17.", "does not match"),
    ("9b", "Refund $240 to Acme Corp.", "does not match"),
    ("10", "Maybe open something for Acme billing if it seems necessary.", "ambiguous"),
    (
        "11",
        "Update employee E-17 title to Senior Engineer and create a support ticket "
        "for Acme Corp about a $240 billing discrepancy.",
        "multiple scenarios",
    ),
    ("12a", "Open a helpdesk case for Acme Corp regarding a $240 invoice mismatch.", "does not match"),
    ("12b", "Promote E-17 to Senior Engineer.", "does not match"),
    ("12c", "File a support ticket on behalf of Acme for their billing problem.", "does not match"),
    ("X1", "Create a support ticket for Globex about a $240 billing discrepancy.", "does not match"),
    ("X2", "Create a support ticket for Acme Corp about a $500 billing discrepancy.", "does not match"),
    ("X3", "Update employee E-17 title to Senior Engineer. Do not write any note.", "negated"),
    ("X4", "Update employee E-17 title to Senior Engineer Manager.", "does not match"),
    (
        "X6",
        "Do not create a support ticket for Acme Corp about the $240 billing discrepancy.",
        "negated",
    ),
]


@pytest.mark.parametrize(
    ("probe", "goal", "reason"), REJECTED_PROBES, ids=[p[0] for p in REJECTED_PROBES]
)
def test_unsafe_or_unsupported_probe_is_rejected_without_side_effects(
    world: World, probe: str, goal: str, reason: str
) -> None:
    result, final, error = _attempt(world, goal)

    assert isinstance(error, InterpretationError), probe
    assert reason in str(error)
    assert "only accepts these exact goal formats" in str(error)
    assert result is None and final is None
    assert world.task_rows() == 0
    assert world.event_rows() == 0
    assert world.repo.list_tickets() == []
    assert world.repo.get_employee("E-17").title == "Software Engineer"
    assert os.listdir(world.workspace) == []


def test_probe_1_ticket_only(world: World) -> None:
    result, final, error = _attempt(world, B_TICKET)

    assert error is None
    assert result.interpreted_goal.entities == {
        "company": "Acme Corp",
        "amount": "$240",
        "issue": "billing discrepancy",
    }
    steps = result.plan.steps
    assert [(s.tool_name, s.arguments["operation"]) for s in steps] == [
        ("company_api", "find_customer"),
        ("company_api", "create_ticket"),
    ]
    assert steps[1].arguments["customer_id"] == "C-1001"
    assert "$240" in steps[1].arguments["subject"]
    assert [c.criterion_type for c in result.interpreted_goal.success_criteria] == [
        "ticket_exists"
    ]
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    tickets = world.repo.list_tickets()
    assert [(t.ticket_id, t.customer_id) for t in tickets] == [("T-10001", "C-1001")]
    assert os.listdir(world.workspace) == []


def test_probe_2_ticket_and_summary_note(world: World) -> None:
    result, final, error = _attempt(world, B_TICKET_NOTE)

    assert error is None
    steps = result.plan.steps
    assert [(s.tool_name, s.arguments["operation"]) for s in steps] == [
        ("company_api", "find_customer"),
        ("company_api", "create_ticket"),
        ("file", "write"),
    ]
    assert steps[2].arguments["path"] == "acme_billing_summary.txt"
    assert [
        (c.criterion_type, c.expected_value.get("path"))
        for c in result.interpreted_goal.success_criteria
    ] == [("ticket_exists", None), ("file_exists", "acme_billing_summary.txt")]
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert len(world.repo.list_tickets()) == 1
    assert os.listdir(world.workspace) == ["acme_billing_summary.txt"]
    assert "Acme Corp" in (world.workspace / "acme_billing_summary.txt").read_text()


def test_ticket_and_note_still_recovers_from_injected_503(world: World) -> None:
    world.injector.inject_once(
        "company_api.create_ticket", FailureType.TRANSIENT.value, "503 Service Unavailable"
    )
    _, final, error = _attempt(world, B_TICKET_NOTE)

    assert error is None
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.retry_count == 1
    assert len(world.repo.list_tickets()) == 1


@pytest.mark.parametrize("goal", [A_DEMO, A_TEST], ids=["X5-demo", "test-goal"])
def test_employee_update_with_note_requires_approval_then_verifies(
    world: World, goal: str
) -> None:
    result, paused, error = _attempt(world, goal)

    assert error is None
    assert result.interpreted_goal.entities == {"employee_id": "E-17", "title": "Senior Engineer"}
    assert [(s.tool_name, s.arguments["operation"]) for s in result.plan.steps] == [
        ("company_api", "update_employee"),
        ("file", "write"),
    ]
    assert result.plan.steps[0].arguments["fields"] == {"title": "Senior Engineer"}
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.pending_action.arguments["fields"] == {"title": "Senior Engineer"}
    assert world.repo.get_employee("E-17").title == "Software Engineer"

    final = world.runtime().approve_task(paused.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert world.repo.get_employee("E-17").title == "Senior Engineer"
    assert os.listdir(world.workspace) == ["employee_update.txt"]
    assert world.repo.list_tickets() == []


def test_employee_update_without_note_writes_no_file(world: World) -> None:
    result, paused, error = _attempt(world, A_NO_NOTE)

    assert error is None
    assert [s.arguments["operation"] for s in result.plan.steps] == ["update_employee"]
    assert [c.criterion_type for c in result.interpreted_goal.success_criteria] == [
        "employee_field"
    ]
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL

    final = world.runtime().approve_task(paused.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert world.repo.get_employee("E-17").title == "Senior Engineer"
    assert os.listdir(world.workspace) == []


def test_rejecting_supported_employee_update_still_mutates_nothing(world: World) -> None:
    _, paused, _ = _attempt(world, A_DEMO)
    rejected = world.runtime().reject_task(paused.task_id, reason="No")

    assert rejected.final_status is TaskStatus.ABORTED
    assert rejected.approval_status is ApprovalStatus.REJECTED
    assert world.repo.get_employee("E-17").title == "Software Engineer"
    assert os.listdir(world.workspace) == []


# --- Exact grammar, not coincidental matches ----------------------------------

@pytest.mark.parametrize(
    "goal",
    [
        "create a support ticket for ACME CORP about a $240 billing discrepancy",
        "  Create  a support ticket for Acme Corp about the $240 billing discrepancy.  ",
        "Create a support ticket for Acme Corp about a $240 billing discrepancy and write a summary note.",
        "Update employee E-17\u2019s title to Senior Engineer and write a confirmation note.",
        "UPDATE EMPLOYEE E-17 TITLE TO SENIOR ENGINEER",
    ],
)
def test_formatting_variants_of_supported_goals_are_accepted(world: World, goal: str) -> None:
    result, _, error = _attempt(world, goal)
    assert error is None
    assert result.plan.steps


@pytest.mark.parametrize(
    "goal",
    [
        "Create a support ticket for Acme Corp about a $2400 billing discrepancy.",
        "Create a support ticket for Acme Corp about a $24 billing discrepancy.",
        "Create a support ticket for Acme Corporation about a $240 billing discrepancy.",
        "Create a support ticket for Acme Corp about a $240 billing discrepancy and a summary note.",
        "Create a support ticket for Acme Corp about a $240 billing discrepancy and save a summary note and email the customer.",
        "Create a support ticket for Acme Corp about a $240 billing discrepancy and save a summary note to /etc/passwd.",
        "Create two support tickets for Acme Corp about a $240 billing discrepancy.",
        "Update employee E-171 title to Senior Engineer.",
        "Update employee E-17 title to Senior Engineer II.",
        "Update employee E-17 title to Senior Engineer and write a confirmation note twice.",
        "Update employee E-23 title to Senior Engineer.",
        "Please update employee E-17 title to Senior Engineer.",
    ],
)
def test_near_miss_goals_are_rejected(world: World, goal: str) -> None:
    _, _, error = _attempt(world, goal)
    assert isinstance(error, InterpretationError)
    assert world.task_rows() == 0
    assert world.repo.list_tickets() == []
    assert os.listdir(world.workspace) == []


# --- Planner re-validates the request and the interpreted criteria ------------

def _planner(world: World) -> Planner:
    return Planner(MockLLMClient(), world.registry)


def test_planner_rejects_unsupported_original_request(world: World) -> None:
    goal = GoalInterpreter(MockLLMClient()).interpret(B_TICKET).model_copy(
        update={"original_request": "Refund $240 to Acme Corp."}
    )
    with pytest.raises(PlanningError, match="rejected the goal"):
        _planner(world).plan(goal)


def test_planner_rejects_missing_original_request(world: World) -> None:
    goal = InterpretedGoal(
        objective="Create a support ticket for Acme Corp about a $240 billing discrepancy."
    )
    with pytest.raises(PlanningError):
        _planner(world).plan(goal)


def test_planner_rejects_criteria_requiring_an_unrequested_note(world: World) -> None:
    goal = GoalInterpreter(MockLLMClient()).interpret(B_TICKET)
    goal = goal.model_copy(
        update={
            "success_criteria": [
                *goal.success_criteria,
                SuccessCriterion(
                    description="note",
                    criterion_type="file_exists",
                    expected_value={"path": "acme_billing_summary.txt"},
                ),
            ]
        }
    )
    with pytest.raises(PlanningError, match="do not match the supported request"):
        _planner(world).plan(goal)


def test_planner_rejects_criteria_missing_a_requested_note(world: World) -> None:
    goal = GoalInterpreter(MockLLMClient()).interpret(B_TICKET_NOTE)
    goal = goal.model_copy(
        update={
            "success_criteria": [
                c for c in goal.success_criteria if c.criterion_type != "file_exists"
            ]
        }
    )
    with pytest.raises(PlanningError, match="do not match the supported request"):
        _planner(world).plan(goal)
