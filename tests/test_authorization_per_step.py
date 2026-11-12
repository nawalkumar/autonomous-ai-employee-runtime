"""Stage 6.0: every executable step must independently satisfy authorization."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.authz import (
    AuthorizationBudgets,
    AuthorizationError,
    AuthorizationReason,
    AuthorizationScope,
    ConstraintShape,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
    authorize_plan,
    parse_authorization_scope,
)
from app.models.enums import TaskStatus
from app.models.plan import PlanStep
from tests.company_support import World, make_world, step
from tests.authz_support import matching_scope_for_plan


def ref(step_id: str, *path: object) -> dict:
    return {"$ref": {"step_id": step_id, "path": list(path)}}


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def test_sibling_omits_required_filters_rejected() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(
                    value={"status": "open", "priority": "high"}
                )
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=0, max_tool_calls=6),
    )
    plan = [
        PlanStep(
            step_id="a",
            description="authorized listing",
            tool_name="company_api",
            arguments={
                "operation": "list_tickets",
                "filters": {"status": "open", "priority": "high"},
            },
        ),
        PlanStep(
            step_id="b",
            description="unrestricted sibling",
            tool_name="company_api",
            arguments={"operation": "list_tickets"},
        ),
    ]
    with pytest.raises(AuthorizationError) as caught:
        authorize_plan(plan, scope)
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED
    assert "b" in caught.value.message or "shape" in caught.value.message.lower()


def test_sibling_targets_other_entity_rejected() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ExactValueConstraint(value="T-10001"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": ExactValueConstraint(value="in_progress"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=2, max_tool_calls=6),
    )
    plan = [
        step("a", "update_ticket", ticket_id="T-10001", fields={"status": "in_progress"}),
        step("b", "update_ticket", ticket_id="T-10002", fields={"status": "in_progress"}),
    ]
    with pytest.raises(AuthorizationError) as caught:
        authorize_plan(plan, scope)
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED


def test_two_updates_different_fields_rejected() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_employee"],
        constraints={
            "company_api.update_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
                "fields": FieldKeysConstraint(allowed_keys=["title"]),
                "fields.title": ExactValueConstraint(value="Senior Engineer"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=2, max_tool_calls=6),
    )
    plan = [
        step("a", "update_employee", employee_id="E-17", fields={"title": "Senior Engineer"}),
        step("b", "update_employee", employee_id="E-17", fields={"department": "Engineering"}),
    ]
    with pytest.raises(AuthorizationError):
        authorize_plan(plan, scope)


def test_nested_field_constraint_omitted_rejected() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ExactValueConstraint(value="T-10009"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": ExactValueConstraint(value="in_progress"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    plan = [
        step("u", "update_ticket", ticket_id="T-10009", fields={"priority": "high"}),
    ]
    with pytest.raises(AuthorizationError) as caught:
        authorize_plan(plan, scope)
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED
    blob = caught.value.message + str(caught.value.details)
    assert "fields" in blob


def test_ref_resolving_outside_authorized_entity_rejected(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.find_customer", "company_api.create_ticket"],
        constraints={
            "company_api.find_customer": {
                "company": ExactValueConstraint(value="Globex"),
            },
            "company_api.create_ticket": {
                "customer_id": ExactValueConstraint(value="C-1001"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=6),
    )
    plan = [
        step("find", "find_customer", company="Globex"),
        step(
            "ticket",
            "create_ticket",
            customer_id=ref("find", "customers", 0, "customer_id"),
            subject="x",
            description="x",
        ),
    ]
    authorize_plan(plan, scope)  # $ref defers ExactValue to runtime
    task = world.runtime.create_task(user_goal="escape", plan=plan, authorization=scope)
    world.runtime.execute_next(task.task_id)
    final = world.runtime.execute_next(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert AuthorizationReason.ENTITY_UNAUTHORIZED.value in (final.completion_summary or "")


def test_legitimate_multi_shape_find_customer(world: World) -> None:
    plan = [
        step("find", "find_customer", company="Acme Corp"),
        step("again", "find_customer", customer_id=ref("find", "customers", 0, "customer_id")),
    ]
    scope = matching_scope_for_plan(plan)
    assert "company_api.find_customer" in scope.constraint_alternatives
    authorize_plan(plan, scope)
    final = world.runtime.create_task(user_goal="multi-shape", plan=plan, authorization=scope)
    done = world.runtime.run(final.task_id)
    assert done.final_status is TaskStatus.COMPLETED
    assert done.tool_calls[-1].arguments["customer_id"] == "C-1001"


def test_explicit_alternatives_require_full_shape_match() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.find_customer"],
        constraint_alternatives={
            "company_api.find_customer": [
                ConstraintShape(
                    constraints={"company": ExactValueConstraint(value="Acme Corp")}
                ),
                ConstraintShape(
                    constraints={
                        "customer_id": ExactValueConstraint(value="C-1001"),
                    }
                ),
            ]
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=3),
    )
    authorize_plan(
        [step("a", "find_customer", company="Acme Corp")],
        scope,
    )
    authorize_plan(
        [step("b", "find_customer", customer_id="C-1001")],
        scope,
    )
    with pytest.raises(AuthorizationError):
        authorize_plan(
            [step("c", "find_customer", company="Globex")],
            scope,
        )


def test_welcome_customer_ticket_still_authorized(world: World) -> None:
    from app.authz import welcome_customer_ticket_scope

    plan = [
        step(
            "create_customer",
            "create_customer",
            name="Pat Lee",
            company="Nimbus",
            email="pat@nimbus.example",
        ),
        step(
            "create_ticket",
            "create_ticket",
            customer_id=ref("create_customer", "customer", "customer_id"),
            subject="Welcome",
            description="Welcome",
        ),
    ]
    scope = welcome_customer_ticket_scope(create_step_id="create_customer")
    # Intent-style narrowing of create_customer identity:
    scope = scope.model_copy(
        update={
            "constraints": {
                **scope.constraints,
                "company_api.create_customer": {
                    "name": ExactValueConstraint(value="Pat Lee"),
                    "company": ExactValueConstraint(value="Nimbus"),
                    "email": ExactValueConstraint(value="pat@nimbus.example"),
                },
            }
        }
    )
    authorize_plan(plan, scope)
    task = world.runtime.create_task(user_goal="welcome", plan=plan, authorization=scope)
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    finished = world.runtime.approve_task(task.task_id)
    assert finished.final_status is TaskStatus.COMPLETED


def test_authorization_survives_restart_and_approval_resume(world: World) -> None:
    plan = [
        step(
            "create_customer",
            "create_customer",
            name="Sam Rivera",
            company="Helio",
            email="sam@helio.example",
        ),
        step(
            "create_ticket",
            "create_ticket",
            customer_id=ref("create_customer", "customer", "customer_id"),
            subject="Welcome",
            description="Welcome",
        ),
    ]
    from app.authz import welcome_customer_ticket_scope

    scope = welcome_customer_ticket_scope()
    scope = scope.model_copy(
        update={
            "constraints": {
                **scope.constraints,
                "company_api.create_customer": {
                    "name": ExactValueConstraint(value="Sam Rivera"),
                    "company": ExactValueConstraint(value="Helio"),
                    "email": ExactValueConstraint(value="sam@helio.example"),
                },
            }
        }
    )
    task = world.runtime.create_task(user_goal="resume", plan=plan, authorization=scope)
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    # Simulate process restart: reload state and continue approval.
    reloaded = world.runtime.load(task.task_id)
    assert reloaded.authorization_fingerprint == scope.fingerprint()
    parsed = parse_authorization_scope(reloaded.authorization)
    assert parsed.fingerprint() == scope.fingerprint()
    finished = world.runtime.approve_task(reloaded.task_id)
    assert finished.final_status is TaskStatus.COMPLETED


def test_fingerprint_stable_without_empty_alternatives() -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "open"})
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=1),
    )
    raw = scope.model_dump(mode="json")
    raw.pop("constraint_alternatives", None)
    restored = parse_authorization_scope(raw)
    assert restored.fingerprint() == scope.fingerprint()


def test_coverage_no_longer_permits_omit_after_sibling() -> None:
    """Regression: plan-level coverage must not authorize a bare sibling step."""
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"customer_id": "C-1001"})
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=0, max_tool_calls=6),
    )
    with pytest.raises(AuthorizationError):
        authorize_plan(
            [
                step("ok", "list_tickets", filters={"customer_id": "C-1001"}),
                PlanStep(
                    step_id="bad",
                    description="omit filters",
                    tool_name="company_api",
                    arguments={"operation": "list_tickets", "limit": 10},
                ),
            ],
            scope,
        )
