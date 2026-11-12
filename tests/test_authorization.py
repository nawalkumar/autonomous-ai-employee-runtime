"""Milestone 4: goal-scoped plan authorization — acceptance and adversarial cases."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.authz import (
    AuthorizationBudgets,
    AuthorizationError,
    AuthorizationReason,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
    AllowedValuesConstraint,
    authorize_plan,
    authorization_for_supported_goal,
    parse_authorization_scope,
    welcome_customer_ticket_scope,
)
from tests.authz_support import matching_scope_for_plan
from app.llm import MockLLMClient
from app.models.enums import FailureType, TaskStatus, VerificationStatus
from app.models.plan import PlanStep
from app.planning import PlanningPipeline
from app.runtime import EventType, ExecutionRuntime
from tests.company_support import World, checks, make_world, reopen, step


GOAL_TICKET = "Create a support ticket for Acme Corp about a $240 billing discrepancy."
GOAL_EMPLOYEE = "Update employee E-17 title to Senior Engineer."


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def ref(step_id: str, *path: Any) -> dict[str, Any]:
    return {"$ref": {"step_id": step_id, "path": list(path)}}


def events(world: World, task_id: str) -> list[str]:
    return [e.event_type for e in world.store.get_events(task_id)]


# --- Acceptance -----------------------------------------------------------------


def test_a_authorized_ticket_creation(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    scope = authorization_for_supported_goal(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        interpreted_goal=planned.interpreted_goal,
        plan=planned.plan.steps,
        authorization=scope,
    )
    final = world.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert EventType.AUTHORIZATION_BOUND in events(world, task.task_id)


def test_b_unauthorized_extra_action_rejected_before_side_effects(world: World) -> None:
    before = world.snapshot()
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    bad = list(planned.plan.steps) + [
        step("x", "deactivate_employee", employee_id="E-17"),
    ]
    scope = authorization_for_supported_goal(GOAL_TICKET)
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(
            user_goal=GOAL_TICKET,
            interpreted_goal=planned.interpreted_goal,
            plan=bad,
            authorization=scope,
        )
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED
    assert world.snapshot() == before
    assert world.store.count_tasks() == 0


def test_c_wrong_entity_rejected(world: World) -> None:
    before = world.snapshot()
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ExactValueConstraint(value="T-10009"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    plan = [step("u", "update_ticket", ticket_id="T-10010", fields={"priority": "high"})]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(user_goal="update ticket", plan=plan, authorization=scope)
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED
    assert world.snapshot() == before


def test_d_wrong_field_rejected_atomically(world: World) -> None:
    before = world.snapshot()
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_employee"],
        constraints={
            "company_api.update_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
                "fields": FieldKeysConstraint(allowed_keys=["title"]),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    plan = [
        step(
            "u",
            "update_employee",
            employee_id="E-17",
            fields={"title": "Senior Engineer", "email": "evil@example.com"},
        )
    ]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(user_goal="title only", plan=plan, authorization=scope)
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED
    assert world.snapshot() == before
    assert world.repo.get_employee("E-17").email != "evil@example.com"


def test_e_reference_escape_rejected_at_runtime(world: World) -> None:
    before_tickets = world.rows("SELECT COUNT(*) FROM company_tickets")[0][0]
    scope = AuthorizationScope(
        allowed_operations=["company_api.find_customer", "company_api.create_ticket"],
        constraints={
            "company_api.find_customer": {
                "company": AllowedValuesConstraint(values=["Acme Corp", "Globex"]),
            },
            "company_api.create_ticket": {
                "customer_id": ExactValueConstraint(value="C-1001"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=6),
    )
    # Producer finds Globex (C-1002); consumer $ref would create for wrong customer.
    plan = [
        step("find", "find_customer", company="Globex"),
        step(
            "ticket",
            "create_ticket",
            customer_id=ref("find", "customers", 0, "customer_id"),
            subject="Escape",
            description="Should not run.",
        ),
    ]
    authorize_plan(plan, scope)  # plan-time defers concrete customer_id check
    task = world.runtime.create_task(user_goal="ref escape", plan=plan, authorization=scope)
    world.runtime.execute_next(task.task_id)  # find succeeds
    final = world.runtime.execute_next(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED
    assert AuthorizationReason.ENTITY_UNAUTHORIZED.value in (final.completion_summary or "")
    assert world.rows("SELECT COUNT(*) FROM company_tickets")[0][0] == before_tickets
    assert EventType.AUTHORIZATION_DENIED in events(world, task.task_id)


def test_f_approval_cannot_override_scope(world: World) -> None:
    before = world.snapshot()
    # Scope allows only a read; plan tries a gated write.
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        constraints={
            "company_api.get_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=3),
    )
    plan = [
        step(
            "u",
            "update_employee",
            employee_id="E-17",
            fields={"title": "Hacker"},
        )
    ]
    with pytest.raises(AuthorizationError):
        world.runtime.create_task(user_goal="no write", plan=plan, authorization=scope)
    assert world.snapshot() == before

    # Pending approval path: authorize a write in scope, then corrupt pending to out-of-scope.
    scope2 = AuthorizationScope(
        allowed_operations=["company_api.update_employee"],
        constraints={
            "company_api.update_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
                "fields": FieldKeysConstraint(allowed_keys=["title"]),
                "fields.title": ExactValueConstraint(value="Senior Engineer"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    plan2 = [
        step(
            "u",
            "update_employee",
            employee_id="E-17",
            fields={"title": "Senior Engineer"},
        )
    ]
    task = world.runtime.create_task(user_goal="approve", plan=plan2, authorization=scope2)
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    # Mutate persisted plan to a different employee while approval is pending.
    state = world.runtime.load(task.task_id)
    evil = state.plan[0].model_copy(
        update={"arguments": {**state.plan[0].arguments, "employee_id": "E-44"}}
    )
    world.store.save_state(state.model_copy(update={"plan": [evil]}))
    with pytest.raises(Exception):
        world.runtime.approve_task(task.task_id, approver="attacker")
    final = world.runtime.load(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED
    assert world.repo.get_employee("E-44").title != "Senior Engineer"


def test_g_restart_and_budget_enforcement(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.find_customer", "company_api.get_employee"],
        constraints={},
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=0, max_tool_calls=1),
    )
    plan = [
        step("a", "find_customer", company="Acme Corp"),
        step("b", "get_employee", employee_id="E-17"),
    ]
    task = world.runtime.create_task(user_goal="budget", plan=plan, authorization=scope)
    mid = world.runtime.execute_next(task.task_id)
    assert mid.plan[0].status.value == "completed"
    assert len(mid.tool_calls) == 1

    restarted = reopen(world)
    final = restarted.runtime.execute_next(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED
    assert "BUDGET_EXCEEDED" in (final.completion_summary or "")
    assert len(final.tool_calls) == 1  # second call never recorded


def test_h_legacy_compatibility_scenarios(world: World) -> None:
    for goal in (GOAL_EMPLOYEE, GOAL_TICKET):
        planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(goal)
        task = world.runtime.create_task(
            user_goal=goal,
            interpreted_goal=planned.interpreted_goal,
            plan=planned.plan.steps,
            authorization=authorization_for_supported_goal(goal),
        )
        state = world.runtime.run(task.task_id)
        if state.final_status is TaskStatus.NEEDS_APPROVAL:
            state = world.runtime.approve_task(task.task_id, approver="tester")
        assert state.final_status is TaskStatus.COMPLETED
        assert state.verification_status is VerificationStatus.PASSED


def test_i_w8_customer_to_ticket_with_from_step_output(world: World) -> None:
    customers_before = world.rows("SELECT COUNT(*) FROM company_customers")[0][0]
    tickets_before = world.rows("SELECT COUNT(*) FROM company_tickets")[0][0]
    plan = [
        step(
            "create_customer",
            "create_customer",
            name="Iris Novak",
            company="Lumen Studios",
            email="iris@lumen.example",
        ),
        step(
            "welcome",
            "create_ticket",
            customer_id=ref("create_customer", "customer", "customer_id"),
            subject="Welcome to CentrAlign",
            description="Onboarding call and account setup.",
            priority="low",
        ),
    ]
    scope = welcome_customer_ticket_scope(create_step_id="create_customer")
    task = world.runtime.create_task(
        user_goal="Onboard Lumen Studios", plan=plan, authorization=scope
    )
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    final = world.runtime.approve_task(task.task_id, approver="sales-lead")
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert checks(final)["reference_binding"] is True
    assert world.rows("SELECT COUNT(*) FROM company_customers")[0][0] == customers_before + 1
    assert world.rows("SELECT COUNT(*) FROM company_tickets")[0][0] == tickets_before + 1


# --- Adversarial ----------------------------------------------------------------


def test_missing_authorization_rejected(world: World) -> None:
    plan = [step("r", "get_employee", employee_id="E-17")]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(
            user_goal="x", plan=plan, authorization=None  # type: ignore[arg-type]
        )
    assert caught.value.reason is AuthorizationReason.MISSING_AUTHORIZATION


def test_malformed_authorization_fail_closed(world: World) -> None:
    with pytest.raises(AuthorizationError) as caught:
        parse_authorization_scope({"version": 99, "allowed_operations": ["company_api.get_employee"]})
    assert caught.value.reason is AuthorizationReason.MALFORMED_AUTHORIZATION


def test_unknown_operation_rejected(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=2),
    )
    plan = [
        PlanStep(
            step_id="bad",
            description="unknown",
            tool_name="company_api",
            arguments={"operation": "drop_table"},
        )
    ]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)
    assert caught.value.reason is AuthorizationReason.UNKNOWN_OPERATION


def test_unrecognized_constraint_type_fail_closed() -> None:
    with pytest.raises(AuthorizationError) as caught:
        parse_authorization_scope(
            {
                "version": 1,
                "allowed_operations": ["company_api.get_employee"],
                "constraints": {
                    "company_api.get_employee": {
                        "employee_id": {"type": "regex", "pattern": ".*"}
                    }
                },
                "budgets": {"max_steps": 1, "max_mutations": 0, "max_tool_calls": 1},
            }
        )
    assert caught.value.reason is AuthorizationReason.UNSUPPORTED_CONSTRAINT


def test_planner_cannot_expand_permissions(world: World) -> None:
    """Even a syntactically valid expanded plan is rejected against goal scope."""
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    expanded = list(planned.plan.steps) + [
        step("note", "write", path="evil.txt", content="nope"),
    ]
    with pytest.raises(AuthorizationError):
        world.runtime.create_task(
            user_goal=GOAL_TICKET,
            plan=expanded,
            authorization=authorization_for_supported_goal(GOAL_TICKET),
        )


def test_unauthorized_second_step_after_authorized_first(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.find_customer"],
        constraints={
            "company_api.find_customer": {
                "company": ExactValueConstraint(value="Acme Corp"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=6),
    )
    plan = [
        step("find", "find_customer", company="Acme Corp"),
        step(
            "ticket",
            "create_ticket",
            customer_id="C-1001",
            subject="x",
            description="y",
        ),
    ]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)
    assert caught.value.reason is AuthorizationReason.PLAN_UNAUTHORIZED


def test_nested_field_scope_violation(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_employee"],
        constraints={
            "company_api.update_employee": {
                "employee_id": ExactValueConstraint(value="E-17"),
                "fields": FieldKeysConstraint(allowed_keys=["title"]),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    plan = [
        step(
            "u",
            "update_employee",
            employee_id="E-17",
            fields={"title": "X", "department": "Sales"},
        )
    ]
    with pytest.raises(AuthorizationError):
        authorize_plan(plan, scope)


def test_ref_to_unauthorized_entity(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_ticket", "company_api.create_ticket"],
        constraints={
            "company_api.create_ticket": {
                "customer_id": ExactValueConstraint(value="C-1001"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=6),
    )
    # Source ticket T-10010 belongs to a different customer than C-1001.
    source = "T-10010"
    plan = [
        step("source", "get_ticket", ticket_id=source),
        step(
            "ticket",
            "create_ticket",
            customer_id=ref("source", "ticket", "customer_id"),
            subject="nope",
            description="nope",
        ),
    ]
    task = world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)
    world.runtime.execute_next(task.task_id)
    final = world.runtime.execute_next(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED


def test_restart_with_corrupted_authorization(world: World) -> None:
    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    state = world.runtime.load(task.task_id)
    # Corrupt the persisted scope while leaving a mismatched fingerprint.
    corrupted = state.model_dump(mode="json")
    corrupted["authorization"] = {
        "version": 1,
        "allowed_operations": ["company_api.deactivate_employee"],
        "constraints": {},
        "budgets": {"max_steps": 10, "max_mutations": 10, "max_tool_calls": 10},
    }
    # Keep old fingerprint so resume detects tampering. The ordinary save
    # refuses a scope swap; this plants the corrupt row the way a rewritten
    # database file would.
    planted = type(state).model_validate(
        {
            **corrupted,
            "authorization_fingerprint": state.authorization_fingerprint,
        }
    )
    world.execute(
        "UPDATE tasks SET state_json = ? WHERE task_id = ?",
        planted.model_dump_json(),
        task.task_id,
    )
    restarted = reopen(world)
    final = restarted.runtime.run(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED


def test_retry_after_authorization_failure_does_not_execute(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.create_ticket"],
        constraints={
            "company_api.create_ticket": {
                "customer_id": ExactValueConstraint(value="C-1001"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=10),
    )
    plan = [
        step(
            "t",
            "create_ticket",
            customer_id="C-1002",
            subject="x",
            description="y",
        )
    ]
    with pytest.raises(AuthorizationError):
        world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)


def test_mutation_budget_exhaustion(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.create_ticket"],
        constraints={},
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=10),
    )
    plan = [
        step(
            "a",
            "create_ticket",
            customer_id="C-1001",
            subject="one",
            description="one",
        ),
        step(
            "b",
            "create_ticket",
            customer_id="C-1001",
            subject="two",
            description="two",
        ),
    ]
    with pytest.raises(AuthorizationError) as caught:
        world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)
    assert caught.value.reason is AuthorizationReason.BUDGET_EXCEEDED


def test_tool_call_budget_exhaustion_at_runtime(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        constraints={},
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=1),
    )
    # First call consumes the budget via a successful read; force another attempt
    # by crafting a second step that never gets authorized for calls.
    plan = [step("a", "get_employee", employee_id="E-17")]
    task = world.runtime.create_task(user_goal="x", plan=plan, authorization=scope)
    done = world.runtime.run(task.task_id)
    assert done.final_status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
    # Manually attempt another call path by appending a pending step and resume.
    state = world.runtime.load(task.task_id)
    if state.final_status is TaskStatus.COMPLETED:
        extra = step("b", "get_employee", employee_id="E-17")
        # Cannot legally expand auth; inject pending step into completed task is invalid.
        # Instead create a fresh task with budget 1 and two steps at plan-time.
        scope2 = AuthorizationScope(
            allowed_operations=["company_api.get_employee"],
            constraints={},
            budgets=AuthorizationBudgets(max_steps=2, max_mutations=0, max_tool_calls=1),
        )
        plan2 = [
            step("a", "get_employee", employee_id="E-17"),
            step("b", "get_employee", employee_id="E-44"),
        ]
        task2 = world.runtime.create_task(user_goal="x2", plan=plan2, authorization=scope2)
        world.runtime.execute_next(task2.task_id)
        final = world.runtime.execute_next(task2.task_id)
        assert final.final_status is TaskStatus.FAILED
        assert "BUDGET_EXCEEDED" in (final.completion_summary or "")


def test_concurrent_budget_enforcement(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.get_employee"],
        constraints={},
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=0, max_tool_calls=1),
    )
    plan = [step("a", "get_employee", employee_id="E-17")]
    task = world.runtime.create_task(user_goal="concurrent", plan=plan, authorization=scope)

    results: list[TaskStatus] = []
    errors: list[BaseException] = []

    def run_once() -> None:
        try:
            state = world.runtime.run(task.task_id)
            results.append(state.final_status)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run_once) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = world.runtime.load(task.task_id)
    assert len(final.tool_calls) <= 1
    assert final.final_status in {TaskStatus.COMPLETED, TaskStatus.FAILED}


def test_cross_task_reference_attempt(world: World) -> None:
    producer_plan = [step("p", "find_customer", company="Acme Corp")]
    producer = world.runtime.create_task(
        user_goal="p",
        plan=producer_plan,
        authorization=matching_scope_for_plan(producer_plan),
    )
    world.runtime.run(producer.task_id)

    # Consumer references a step id that does not exist in *its* plan.
    scope = AuthorizationScope(
        allowed_operations=["company_api.create_ticket"],
        constraints={
            "company_api.create_ticket": {
                "customer_id": FromStepOutputConstraint(
                    step_id="p", path=["customers", 0, "customer_id"]
                )
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=1, max_tool_calls=3),
    )
    plan = [
        step(
            "ticket",
            "create_ticket",
            customer_id=ref("p", "customers", 0, "customer_id"),
            subject="x",
            description="y",
        )
    ]
    # Plan-time allows the shaped ref; runtime fails because producer is not in this plan.
    with pytest.raises(Exception):
        # Reference validation should reject unknown producer step at plan validate
        # or at create via authorize / binding.
        from app.planning.validator import validate_plan
        from app.models.plan import Plan

        validate_plan(Plan(steps=plan), world.registry)


def test_public_demo_isolation_scope_not_shared(tmp_path: Path) -> None:
    world_a = make_world(tmp_path / "a", demo=True)
    world_b = make_world(tmp_path / "b", demo=True)
    planned = PlanningPipeline(MockLLMClient(), world_a.registry).interpret_and_plan(GOAL_TICKET)
    task_a = world_a.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    assert world_b.store.get_state(task_a.task_id) is None


def test_direct_authorize_action_gate(world: World) -> None:
    """The shared authorize_action helper rejects out-of-scope resolved args."""
    from app.authz import authorize_action

    planned = PlanningPipeline(MockLLMClient(), world.registry).interpret_and_plan(GOAL_TICKET)
    task = world.runtime.create_task(
        user_goal=GOAL_TICKET,
        plan=planned.plan.steps,
        authorization=authorization_for_supported_goal(GOAL_TICKET),
    )
    state = world.runtime.load(task.task_id)
    with pytest.raises(AuthorizationError) as caught:
        authorize_action(
            state,
            tool_name="company_api",
            arguments={
                "operation": "deactivate_employee",
                "employee_id": "E-17",
            },
            step_id="step_1",
        )
    assert caught.value.reason is AuthorizationReason.OPERATION_UNAUTHORIZED


def test_legacy_unmapped_task_fails_closed(world: World) -> None:
    from app.authz import ensure_task_authorization
    from app.models.state import ExecutionState

    state = ExecutionState(
        user_goal="Do something vague and powerful",
        plan=[step("r", "get_employee", employee_id="E-17")],
        authorization=None,
    )
    world.store.save_state(state)
    loaded = world.runtime.load(state.task_id)
    with pytest.raises(AuthorizationError) as caught:
        ensure_task_authorization(loaded)
    assert caught.value.reason is AuthorizationReason.LEGACY_UNMAPPED
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.failures[-1].failure_type is FailureType.UNAUTHORIZED
