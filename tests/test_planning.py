"""Phase 4 goal interpretation + planning tests (MockLLM only)."""

from pathlib import Path

import pytest

from app.llm import LLMResponse, MockLLMClient, StructuredOutputError, parse_structured
from app.llm.base import LLMClient, LLMRequest, LLMResponse as Resp
from app.models import Plan, PlanStep, TaskStatus
from app.models.enums import FailureType, PlanStepStatus
from app.planning import (
    GoalInterpreter,
    InterpretationError,
    PlanValidationError,
    Planner,
    PlanningError,
    PlanningPipeline,
    validate_plan,
)
from app.planning.schemas import InterpretedGoalDraft
from app.runtime import ExecutionRuntime
from app.store import SQLiteStore
from app.tools import FailureInjector, ToolRegistry
from app.world import CompanyRepository


GOAL_A = (
    "Update employee E-17's title to Senior Engineer and write a confirmation note."
)
GOAL_B = (
    "Create a support ticket for Acme Corp about a $240 billing discrepancy."
)


def test_interpreter_scenario_a() -> None:
    interpreter = GoalInterpreter(MockLLMClient())
    goal = interpreter.interpret(GOAL_A)
    assert "E-17" in goal.objective or goal.entities.get("employee_id") == "E-17"
    assert goal.original_request
    assert len(goal.success_criteria) >= 2
    assert any("Senior Engineer" in c.description for c in goal.success_criteria)


def test_interpreter_scenario_b() -> None:
    goal = GoalInterpreter(MockLLMClient()).interpret(GOAL_B)
    assert "Acme" in goal.objective or goal.entities.get("company") == "Acme Corp"
    assert goal.success_criteria
    assert any("customer" in item.lower() for item in goal.required_information)


def test_interpreter_malformed_structured_output() -> None:
    class BadLLM(LLMClient):
        provider_name = "bad"

        async def complete(self, request: LLMRequest) -> Resp:
            return Resp(
                content="this is not json at all",
                model="bad",
                provider="bad",
            )

    with pytest.raises(InterpretationError):
        GoalInterpreter(BadLLM()).interpret(GOAL_A)


def test_parse_structured_validation_failure() -> None:
    response = LLMResponse(
        content='{"objective": 123}',
        model="x",
        provider="mock",
    )
    with pytest.raises(StructuredOutputError):
        parse_structured(response, InterpretedGoalDraft)


def test_planner_scenario_a(registry: ToolRegistry) -> None:
    pipeline = PlanningPipeline(MockLLMClient(), registry)
    result = pipeline.interpret_and_plan(GOAL_A)
    plan = result.plan
    assert len(plan.steps) == 2
    assert plan.steps[0].tool_name == "company_api"
    assert plan.steps[0].arguments["operation"] == "update_employee"
    assert plan.steps[0].arguments["employee_id"] == "E-17"
    assert plan.steps[0].arguments["fields"]["title"] == "Senior Engineer"
    assert plan.steps[1].tool_name == "file"
    assert plan.steps[1].arguments["operation"] == "write"
    assert plan.steps[1].arguments["path"] == "employee_update.txt"


def test_planner_scenario_b(registry: ToolRegistry) -> None:
    plan = PlanningPipeline(MockLLMClient(), registry).build_plan(GOAL_B)
    assert [s.arguments.get("operation") for s in plan.steps] == [
        "find_customer",
        "create_ticket",
    ]
    assert plan.steps[0].arguments["company"] == "Acme Corp"
    assert plan.steps[1].arguments["customer_id"] == "C-1001"


def test_validate_plan_rejects_unknown_tool(registry: ToolRegistry) -> None:
    plan = Plan(
        steps=[
            PlanStep(
                step_id="s1",
                description="bad",
                tool_name="delete_database",
                arguments={},
            )
        ]
    )
    with pytest.raises(PlanValidationError, match="Unsupported tool"):
        validate_plan(plan, registry)


def test_validate_plan_rejects_duplicate_step_ids(registry: ToolRegistry) -> None:
    plan = Plan(
        steps=[
            PlanStep(
                step_id="dup",
                description="one",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-17"},
            ),
            PlanStep(
                step_id="dup",
                description="two",
                tool_name="company_api",
                arguments={"operation": "get_employee", "employee_id": "E-23"},
            ),
        ]
    )
    with pytest.raises(PlanValidationError, match="Duplicate step_id"):
        validate_plan(plan, registry)


def test_validate_plan_rejects_empty(registry: ToolRegistry) -> None:
    with pytest.raises(PlanValidationError, match="at least one step"):
        validate_plan(Plan(steps=[]), registry)


def test_validate_plan_rejects_bad_arguments(registry: ToolRegistry) -> None:
    plan = Plan(
        steps=[
            PlanStep(
                step_id="s1",
                description="missing fields",
                tool_name="company_api",
                arguments={
                    "operation": "update_employee",
                    "employee_id": "E-17",
                },
            )
        ]
    )
    with pytest.raises(PlanValidationError, match="Invalid arguments"):
        validate_plan(plan, registry)


def test_end_to_end_employee_update(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    workspace: Path,
) -> None:
    planning = PlanningPipeline(MockLLMClient(), registry)
    result = planning.interpret_and_plan(GOAL_A)

    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(
        user_goal=GOAL_A,
        interpreted_goal=result.interpreted_goal,
        plan=result.plan.steps,
    )
    paused = runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    final = runtime.approve_task(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert all(s.status is PlanStepStatus.COMPLETED for s in final.plan)
    employee = company_repo.get_employee("E-17")
    assert employee is not None
    assert employee.title == "Senior Engineer"
    note = workspace / "employee_update.txt"
    assert note.exists()
    assert "Senior Engineer" in note.read_text(encoding="utf-8")


def test_ticket_plan_recovers_from_injected_503(
    store: SQLiteStore,
    registry: ToolRegistry,
    company_repo: CompanyRepository,
    failure_injector: FailureInjector,
    workspace: Path,
) -> None:
    plan = PlanningPipeline(MockLLMClient(), registry).build_plan(GOAL_B)
    failure_injector.inject_once(
        operation="company_api.create_ticket",
        error_type=FailureType.TRANSIENT.value,
        error_message="Mock company API temporarily unavailable",
    )

    runtime = ExecutionRuntime(store=store, registry=registry, workspace_path=workspace)
    task = runtime.create_task(user_goal=GOAL_B, plan=plan.steps)
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert final.plan[0].status is PlanStepStatus.COMPLETED
    assert final.plan[1].status is PlanStepStatus.COMPLETED
    assert len(company_repo.list_tickets()) == 1
    # find + failed create + successful create
    assert len(final.tool_calls) == 3


def test_planner_unknown_goal_errors(registry: ToolRegistry) -> None:
    with pytest.raises((InterpretationError, PlanningError)):
        PlanningPipeline(MockLLMClient(), registry).interpret_and_plan(
            "Launch a rocket to Mars tomorrow"
        )
