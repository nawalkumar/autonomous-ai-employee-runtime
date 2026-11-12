"""Step-output references: syntax and plan-time validation (milestone 3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.cli import build_runtime
from app.config import Settings
from app.llm import MockLLMClient
from app.models.plan import Plan, PlanStep
from app.models.references import (
    ReferenceSyntaxError,
    StepOutputRef,
    find_references,
    parse_reference,
    substitute,
)
from app.planning import PlanningPipeline
from app.planning.validator import PlanValidationError, validate_plan
from app.tools import ToolRegistry


def ref(step_id: str, *path: Any) -> dict[str, Any]:
    return {"$ref": {"step_id": step_id, "path": list(path)}}


def step(step_id: str, operation: str, **arguments: Any) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=step_id,
        tool_name="company_api",
        arguments={"operation": operation, **arguments},
    )


CUSTOMER = step("customer", "create_customer", name="Iris Novak", company="Lumen", email="iris@lumen.example")


def ticket(customer_id: Any, step_id: str = "ticket", **extra: Any) -> PlanStep:
    return step(
        step_id, "create_ticket", customer_id=customer_id, subject="Welcome",
        description="Onboarding", **extra,
    )


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    return build_runtime(
        Settings(_env_file=None, database_path=str(tmp_path / "d" / "runtime.db"), workspace_path=str(tmp_path / "w"))
    ).registry


def check(registry: ToolRegistry, *steps: PlanStep) -> Plan:
    return validate_plan(Plan(steps=list(steps)), registry)


# --- Syntax -----------------------------------------------------------------------


def test_reference_parsing_and_substitution_preserve_json_types() -> None:
    arguments = {
        "a": ref("p", "x"),
        "nested": {"list": [1, ref("p", "items", 0, "id")], "literal": "$ref"},
    }
    found = find_references(arguments)
    assert found == [
        (("a",), StepOutputRef("p", ("x",))),
        (("nested", "list", 1), StepOutputRef("p", ("items", 0, "id"))),
    ]
    resolved = substitute(arguments, {("a",): {"k": [True, 2.5]}, ("nested", "list", 1): 7})
    assert resolved == {"a": {"k": [True, 2.5]}, "nested": {"list": [1, 7], "literal": "$ref"}}
    assert arguments["a"] == ref("p", "x")  # input untouched


@pytest.mark.parametrize(
    "value",
    [
        "$ref",
        "{{customer.customer.customer_id}}",
        '{"$ref": {"step_id": "customer", "path": ["customer"]}}',
        "${customer.customer_id}",
        {"ref": {"step_id": "customer", "path": ["customer"]}},
        ["$ref", "customer"],
    ],
)
def test_reference_shaped_values_stay_literal(value: Any) -> None:
    assert parse_reference(value) is None
    assert find_references({"x": value}) == []


@pytest.mark.parametrize(
    "value",
    [
        {"$ref": {"step_id": "p", "path": ["a"]}, "other": 1},
        {"$ref": {"step_id": "p"}},
        {"$ref": {"path": ["a"]}},
        {"$ref": {"step_id": "p", "path": ["a"], "default": "x"}},
        {"$ref": "p.a"},
        {"$ref": {"step_id": 3, "path": ["a"]}},
        {"$ref": {"step_id": "../p", "path": ["a"]}},
        {"$ref": {"step_id": "p", "path": "a.b"}},
        {"$ref": {"step_id": "p", "path": []}},
        {"$ref": {"step_id": "p", "path": ["a"] * 9}},
        {"$ref": {"step_id": "p", "path": ["__class__"]}},
        {"$ref": {"step_id": "p", "path": ["_private"]}},
        {"$ref": {"step_id": "p", "path": ["a.b"]}},
        {"$ref": {"step_id": "p", "path": [""]}},
        {"$ref": {"step_id": "p", "path": ["os", "environ"] + ["x" * 65]}},
        {"$ref": {"step_id": "p", "path": [-1]}},
        {"$ref": {"step_id": "p", "path": [100]}},
        {"$ref": {"step_id": "p", "path": [True]}},
        {"$ref": {"step_id": "p", "path": [1.5]}},
        {"$ref": {"step_id": "p", "path": [None]}},
    ],
)
def test_malformed_references_are_rejected(value: Any) -> None:
    with pytest.raises(ReferenceSyntaxError):
        find_references({"x": value})


def test_references_are_immutable() -> None:
    reference = StepOutputRef("p", ("a",))
    with pytest.raises(AttributeError):
        reference.step_id = "q"  # type: ignore[misc]


# --- Plan-time validation -----------------------------------------------------------


def test_literal_plans_and_mock_scenarios_are_unchanged(registry: ToolRegistry) -> None:
    pipeline = PlanningPipeline(MockLLMClient(), registry)
    for goal in (
        "Update employee E-17 title to Senior Engineer and confirm the change in a note file.",
        "Create a support ticket for Acme Corp about a billing discrepancy of $240 and save a summary note.",
    ):
        plan = pipeline.interpret_and_plan(goal).plan
        assert find_references([s.arguments for s in plan.steps]) == []
        assert check(registry, *plan.steps).steps == plan.steps


def test_valid_scalar_reference(registry: ToolRegistry) -> None:
    plan = check(registry, CUSTOMER, ticket(ref("customer", "customer", "customer_id")))
    assert plan.steps[1].arguments["customer_id"] == ref("customer", "customer", "customer_id")


def test_valid_nested_and_indexed_references(registry: ToolRegistry) -> None:
    check(
        registry,
        step("source", "get_ticket", ticket_id="T-10018"),
        step("update", "update_ticket", ticket_id="T-10009", fields={"status": ref("source", "ticket", "status")}),
    )
    check(
        registry,
        step("find", "find_customer", company="Acme Corp"),
        ticket(ref("find", "customers", 0, "customer_id")),
    )


@pytest.mark.parametrize(
    "steps,message",
    [
        ([CUSTOMER, ticket(ref("missing", "customer", "customer_id"))], "unknown step 'missing'"),
        ([ticket(ref("customer", "customer", "customer_id")), CUSTOMER], "later step 'customer'"),
        ([ticket(ref("ticket", "ticket", "customer_id"))], "references itself"),
        ([CUSTOMER, ticket(ref("customer", "customer", "phone"))], "no field at customer.phone"),
        ([CUSTOMER, ticket(ref("customer", "customer"))], "accepts ['null', 'string']"),
        ([CUSTOMER, ticket(ref("customer", "customer", 0))], "no list at customer.0"),
        ([CUSTOMER, ticket(ref("customer", "idempotent_replay"))], "accepts ['null', 'string']"),
        (
            [step("find", "find_customer", company="Acme"), ticket(ref("find", "count", "x"))],
            "no field at count.x",
        ),
        (
            [CUSTOMER, step("x", "list_tickets", filters={"customer_id": ref("customer", "customer", "customer_id")}, limit=ref("customer", "customer", "name"))],
            "limit accepts ['integer']",
        ),
    ],
)
def test_invalid_reference_plans_are_rejected(registry: ToolRegistry, steps, message) -> None:
    with pytest.raises(PlanValidationError) as raised:
        check(registry, *steps)
    assert message in str(raised.value)


def test_circular_references_are_reported(registry: ToolRegistry) -> None:
    a = step("a", "get_ticket", ticket_id=ref("b", "ticket", "ticket_id"))
    b = step("b", "get_ticket", ticket_id=ref("a", "ticket", "ticket_id"))
    with pytest.raises(PlanValidationError, match="Circular step references: a -> b -> a"):
        check(registry, a, b)


@pytest.mark.parametrize(
    "consumer,message",
    [
        (step("t", ref("customer", "customer", "customer_id")), "operation"),
        (step("t", "assign_ticket", ticket_id="T-10001", employee_id="E-40", note=ref("customer", "customer", "name")), "note is not an input"),
        (step("t", "create_ticket", customer_id=ref("customer", "customer", "customer_id"), subject="s"), "missing required input(s) description"),
        (step("t", "assign_ticket", ticket_id=ref("customer", "customer", "customer_id"), employee_id="E-40", extra=1), "unsupported input(s) extra"),
    ],
)
def test_reference_steps_must_match_their_capability(registry: ToolRegistry, consumer, message) -> None:
    with pytest.raises(PlanValidationError) as raised:
        check(registry, CUSTOMER, consumer)
    assert message in str(raised.value)


def test_malformed_reference_in_a_plan_is_rejected(registry: ToolRegistry) -> None:
    with pytest.raises(PlanValidationError, match="must not have other keys"):
        check(registry, CUSTOMER, ticket({"$ref": {"step_id": "customer", "path": ["customer"]}, "x": 1}))


def test_reference_shaped_strings_stay_literal_in_plans(registry: ToolRegistry) -> None:
    literal = step(
        "ticket", "create_ticket", customer_id="{{customer.customer_id}}", subject="$ref",
        description='{"$ref": {"step_id": "customer", "path": ["customer"]}}',
    )
    plan = check(registry, literal)
    assert plan.steps[0].arguments["customer_id"] == "{{customer.customer_id}}"
    assert plan.steps[0].arguments["subject"] == "$ref"


def test_undecidable_types_are_deferred_to_execution(registry: ToolRegistry) -> None:
    """list_tickets 'filters' output is a free-form object: the plan is accepted
    and the value is validated when the step runs."""
    check(
        registry,
        step("listing", "list_tickets", filters={"status": "open"}),
        step("get", "get_ticket", ticket_id=ref("listing", "filters", "status")),
    )
