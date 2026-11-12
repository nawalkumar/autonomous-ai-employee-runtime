"""Milestone 7: collection restart, replan budgets, audit and concurrency."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from app.authz import (
    AllowedValuesConstraint,
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromCollectionConstraint,
)
from app.config import get_settings
from app.models.collections import CollectionSelect, CollectionWork
from app.models.enums import PlanStepStatus, TaskStatus, ToolCallStatus
from app.models.plan import PlanStep
from app.models.records import ToolCallRecord
from app.runtime.replan import (
    ReplanEnablement,
    ReplanResult,
    ReplanTrigger,
    Replanner,
    commit_replan,
    enablement_for_runtime,
    historical_mutating_attempts,
    propose_and_commit,
    task_scope,
    validate_revision,
)
from app.runtime.transitions import EventType, make_event
from app.tools.capabilities import (
    CAPABILITIES,
    Capability,
    CapabilityRegistry,
    IdempotencyStrategy,
    RiskLevel,
    StateScope,
    VerificationStrategy,
)
from tests.company_support import SimulatedCrash, World, crash_during, make_world, reopen, step
from tests.test_replanning import ScriptedLLM

_LEGACY_MUTATING_NAMES = frozenset(
    {
        "update_employee",
        "create_ticket",
        "update_ticket",
        "assign_ticket",
        "create_customer",
        "deactivate_employee",
        "write",
    }
)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


def _enabled(rounds: int = 2) -> ReplanEnablement:
    return ReplanEnablement(enabled=True, max_rounds=rounds)


def _update_scope(
    *,
    max_steps: int = 4,
    max_mutations: int = 6,
    max_tool_calls: int = 8,
) -> AuthorizationScope:
    return AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ExactValueConstraint(value="T-10009"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": AllowedValuesConstraint(values=["in_progress", "resolved"]),
            }
        },
        budgets=AuthorizationBudgets(
            max_steps=max_steps,
            max_mutations=max_mutations,
            max_tool_calls=max_tool_calls,
        ),
    )


def _proposal(*steps: dict) -> str:
    return json.dumps({"steps": list(steps)})


def _update_step(step_id: str, status: str = "resolved") -> dict:
    return {
        "step_id": step_id,
        "description": "revise",
        "tool_name": "company_api",
        "arguments": {
            "operation": "update_ticket",
            "ticket_id": "T-10009",
            "fields": {"status": status},
        },
        "expected_outcome": status,
    }


def _task(world: World, scope: AuthorizationScope, *plan: PlanStep):
    return world.runtime.create_task(user_goal="replan", plan=list(plan), authorization=scope)


def _mark_completed(state, step_id: str):
    plan = []
    for step in state.plan:
        if step.step_id == step_id:
            plan.append(step.model_copy(update={"status": PlanStepStatus.COMPLETED}))
        else:
            plan.append(step)
    completed = list(dict.fromkeys([*state.completed_steps, step_id]))
    return state.model_copy(update={"plan": plan, "completed_steps": completed})


def _mutating_call(state, step_id: str, *, status: ToolCallStatus = ToolCallStatus.SUCCEEDED):
    return ToolCallRecord(
        task_id=state.task_id,
        step_id=step_id,
        tool_name="company_api",
        arguments={
            "operation": "update_ticket",
            "ticket_id": "T-10009",
            "fields": {"status": "in_progress"},
        },
        status=status,
        idempotency_key=f"{state.task_id}:{step_id}",
    )


def test_create_task_persists_settings_replan_limit(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AAE_MAX_REPLAN_ROUNDS", "1")
    get_settings.cache_clear()
    try:
        scope = _update_scope()
        state = _task(
            world,
            scope,
            step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
        )
        assert state.replan_limit == 1
        assert state.replan_count == 0
        loaded = world.runtime.load(state.task_id)
        assert loaded.replan_limit == 1
        assert loaded.replan_count == 0
    finally:
        get_settings.cache_clear()


def test_caller_cannot_pass_a_replan_limit(world: World) -> None:
    scope = _update_scope()
    with pytest.raises(TypeError):
        world.runtime.create_task(
            user_goal="g",
            plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
            authorization=scope,
            replan_limit=9,  # type: ignore[call-arg]
        )


def test_untrusted_state_cannot_raise_replan_limit(world: World) -> None:
    scope = _update_scope()
    state = _task(
        world,
        scope,
        step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
    )
    tampered = state.model_copy(update={"replan_limit": 9, "replan_count": 2})
    decision = Replanner(ScriptedLLM("{}"), world.registry).evaluate_trigger(
        category=ReplanTrigger.TICKET_STATE_CONFLICT.value,
        state=tampered,
        enablement=_enabled(2),
    )
    assert decision.allowed is False
    assert decision.reason == "replan_limit_exhausted"

    lowered = state.model_copy(update={"replan_limit": 1, "replan_count": 1})
    decision = Replanner(ScriptedLLM("{}"), world.registry).evaluate_trigger(
        category=ReplanTrigger.TICKET_STATE_CONFLICT.value,
        state=lowered,
        enablement=_enabled(5),
    )
    assert decision.allowed is False
    assert decision.reason == "replan_limit_exhausted"


def test_wider_caller_scope_cannot_expand_task_budget(world: World) -> None:
    scope = _update_scope(max_steps=2, max_mutations=2, max_tool_calls=4)
    state = _task(
        world,
        scope,
        step("u1", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
        step("u2", "update_ticket", ticket_id="T-10009", fields={"status": "resolved"}),
    )
    state = _mark_completed(state, "u1")
    wide = _update_scope(max_steps=50, max_mutations=50, max_tool_calls=50)
    proposal = _proposal(_update_step("a"), _update_step("b", "in_progress"))
    result = asyncio.run(
        Replanner(ScriptedLLM(proposal), world.registry).propose_async(
            state,
            wide,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="conflict",
            enablement=_enabled(),
        )
    )
    assert result.accepted is False
    assert result.reason == "budget_exceeded:max_steps"
    assert task_scope(state) is not None
    assert task_scope(state).budgets.max_steps == 2  # type: ignore[union-attr]


def test_completed_plus_replacement_cannot_exceed_max_steps(world: World) -> None:
    scope = _update_scope(max_steps=2, max_mutations=6, max_tool_calls=6)
    state = _task(
        world,
        scope,
        step("u1", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
        step("u2", "update_ticket", ticket_id="T-10009", fields={"status": "resolved"}),
    )
    state = _mark_completed(state, "u1")
    proposal = _proposal(_update_step("a"), _update_step("b", "in_progress"))
    result = asyncio.run(
        Replanner(ScriptedLLM(proposal), world.registry).propose_async(
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="conflict",
            enablement=_enabled(),
        )
    )
    assert result.accepted is False
    assert result.reason == "budget_exceeded:max_steps"
    final, updated = commit_replan(
        world.store, state, result, scope=scope, enablement=_enabled()
    )
    assert final.accepted is False
    assert [step.step_id for step in updated.plan] == ["u1", "u2"]
    assert updated.tool_calls == []


def test_historical_mutations_follow_registry_not_legacy_names() -> None:
    omitted = Capability(
        tool="company_api",
        operation="annotate_ticket",
        description="Mutating operation absent from the old hardcoded set.",
        risk=RiskLevel.WRITE,
        requires_approval=False,
        retry_safe=True,
        idempotency=IdempotencyStrategy.KEYED_TRANSACTION,
        mutates=StateScope.COMPANY,
        verification=VerificationStrategy.NONE,
    )
    assert omitted.operation not in _LEGACY_MUTATING_NAMES
    registry = CapabilityRegistry((*tuple(CAPABILITIES), omitted))
    omitted_call = ToolCallRecord(
        task_id="task",
        tool_name="company_api",
        arguments={"operation": "annotate_ticket", "ticket_id": "T-1"},
        status=ToolCallStatus.FAILED,
    )
    read_call = ToolCallRecord(
        task_id="task",
        tool_name="company_api",
        arguments={"operation": "get_ticket", "ticket_id": "T-1"},
        status=ToolCallStatus.SUCCEEDED,
    )
    assert historical_mutating_attempts([omitted_call, read_call], registry=registry) == 1
    known = ToolCallRecord(
        task_id="task",
        tool_name="company_api",
        arguments={"operation": "update_ticket", "ticket_id": "T-1", "fields": {"status": "open"}},
        status=ToolCallStatus.FAILED,
    )
    assert historical_mutating_attempts([known, read_call]) == 1


def test_multiple_replans_consume_remaining_mutation_budget(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AAE_MAX_REPLAN_ROUNDS", "5")
    get_settings.cache_clear()
    scope = _update_scope(max_steps=4, max_mutations=2, max_tool_calls=6)
    try:
        state = _task(
            world,
            scope,
            step("u0", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
        )
    finally:
        get_settings.cache_clear()
    assert state.replan_limit == 5
    llm = ScriptedLLM(_proposal(_update_step("u1")))
    result, state = asyncio.run(
        propose_and_commit(
            world.store,
            Replanner(llm, world.registry),
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="conflict",
            enablement=_enabled(5),
        )
    )
    assert result.accepted is True
    assert state.replan_count == 1
    assert state.replan_limit == 5

    state = _mark_completed(state, "u1")
    state = state.model_copy(update={"tool_calls": [_mutating_call(state, "u1")]})
    world.store.save_state(state)
    llm.content = _proposal(_update_step("u2", "in_progress"))
    result, state = asyncio.run(
        propose_and_commit(
            world.store,
            Replanner(llm, world.registry),
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="again",
            enablement=_enabled(5),
        )
    )
    assert result.accepted is True
    assert state.replan_count == 2
    assert len(state.tool_calls) == 1

    state = _mark_completed(state, "u2")
    state = state.model_copy(
        update={"tool_calls": [*state.tool_calls, _mutating_call(state, "u2")]}
    )
    world.store.save_state(state)
    resumed = reopen(world)
    loaded = resumed.runtime.load(state.task_id)
    assert loaded.replan_count == 2
    assert loaded.replan_limit == 5
    assert historical_mutating_attempts(list(loaded.tool_calls)) == 2
    llm.content = _proposal(_update_step("u3"))
    result, updated = asyncio.run(
        propose_and_commit(
            resumed.store,
            Replanner(llm, resumed.registry),
            loaded,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="third",
            enablement=_enabled(5),
        )
    )
    assert result.accepted is False
    assert result.reason == "budget_exceeded:max_mutations"
    assert [step.step_id for step in updated.plan] == [step.step_id for step in loaded.plan]
    assert len(updated.tool_calls) == 2
    assert updated.replan_limit == 5
    get_settings.cache_clear()


def test_collection_expansion_cannot_bypass_step_budget(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.assign_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "open", "unassigned": True})
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=3, max_mutations=10, max_tool_calls=10),
    )
    list_step = step(
        "list", "list_tickets", filters={"status": "open", "unassigned": True}, limit=5
    )
    state = _task(world, scope, list_step)
    state = _mark_completed(state, "list")
    template = PlanStep(
        step_id="assign",
        description="assign many",
        tool_name="company_api",
        arguments={
            "operation": "assign_ticket",
            "ticket_id": {"$collection_item": {"field": "ticket_id"}},
            "employee_id": "E-40",
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=3),
            max_items=3,
        ),
    )
    verdict = validate_revision(state, scope, [template])
    assert verdict.ok is False
    assert verdict.reason == "budget_exceeded:max_steps"
    assert verdict.budgets["effective_steps"] == 4

    before_ids = [step.step_id for step in state.plan]
    forged = ReplanResult(
        accepted=True,
        revision=99,
        trigger=ReplanTrigger.TICKET_STATE_CONFLICT.value,
        reason="accepted",
        replacement_steps=[template],
    )
    final, updated = commit_replan(
        world.store, state, forged, scope=scope, enablement=_enabled()
    )
    assert final.accepted is False
    assert final.reason == "budget_exceeded:max_steps"
    assert [step.step_id for step in updated.plan] == before_ids
    assert updated.replan_count == state.replan_count + 1
    assert updated.replan_count == 1
    kinds = [event.event_type for event in world.store.get_events(state.task_id)]
    assert EventType.REPLAN_ACCEPTED not in kinds
    assert EventType.REPLAN_REJECTED in kinds


def test_unauthorized_replan_has_no_side_effects(world: World) -> None:
    scope = _update_scope(max_steps=1, max_mutations=3, max_tool_calls=3)
    state = _task(
        world,
        scope,
        step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
    )
    before = world.snapshot()
    plan_ids = [step.step_id for step in state.plan]
    evil = _proposal(
        {
            "step_id": "evil",
            "description": "deactivate",
            "tool_name": "company_api",
            "arguments": {"operation": "deactivate_employee", "employee_id": "E-17"},
            "expected_outcome": "gone",
        }
    )
    canary = "CANARY-SECRET-do-not-persist"
    result, updated = asyncio.run(
        propose_and_commit(
            world.store,
            Replanner(ScriptedLLM(evil), world.registry),
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary=canary,
            enablement=_enabled(),
        )
    )
    assert result.accepted is False
    assert "rejected" in result.reason
    assert [step.step_id for step in updated.plan] == plan_ids
    assert updated.tool_calls == []
    assert world.snapshot() == before
    events = world.store.get_events(state.task_id)
    assert EventType.REPLAN_ACCEPTED not in [event.event_type for event in events]
    rejected = [event for event in events if event.event_type == EventType.REPLAN_REJECTED]
    assert rejected
    blob = json.dumps(rejected[-1].result)
    assert canary not in blob
    assert "E-17" not in blob
    assert rejected[-1].result["authorization"] == "denied"
    assert rejected[-1].result["task_id"] == state.task_id


def test_accepted_replan_audit_event_matches_persisted_revision(world: World) -> None:
    scope = _update_scope(max_steps=2, max_mutations=4, max_tool_calls=4)
    state = _task(
        world,
        scope,
        step("u1", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
        step("u2", "update_ticket", ticket_id="T-10009", fields={"status": "resolved"}),
    )
    state = _mark_completed(state, "u1")
    canary = "CANARY-PROMPT-do-not-audit"
    result, updated = asyncio.run(
        propose_and_commit(
            world.store,
            Replanner(ScriptedLLM(_proposal(_update_step("u2b"))), world.registry),
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary=canary,
            enablement=_enabled(),
        )
    )
    assert result.accepted is True
    assert [step.step_id for step in updated.plan] == ["u1", "u2b"]
    assert updated.plan[1].status is PlanStepStatus.PENDING
    events = [
        event
        for event in world.store.get_events(state.task_id)
        if str(event.event_type).startswith("replan.")
    ]
    kinds = [event.event_type for event in events]
    assert kinds == [EventType.REPLAN_PROPOSED, EventType.REPLAN_ACCEPTED]
    body = events[-1].result
    assert body["outcome"] == "accepted"
    assert body["authorization"] == "authorized"
    assert body["revision"] == updated.replan_count
    assert body["trigger"] == ReplanTrigger.TICKET_STATE_CONFLICT.value
    assert body["previous_plan_id"]
    assert body["proposed_plan_id"]
    assert body["accepted_plan_id"]
    assert body["budgets"]["mutations_consumed"] == 0
    assert canary not in json.dumps(body)
    reloaded = world.runtime.load(state.task_id)
    assert reloaded.replan_count == updated.replan_count
    assert [step.step_id for step in reloaded.plan] == ["u1", "u2b"]


def test_replan_state_and_events_commit_together(tmp_path: Path) -> None:
    from app.models.state import ExecutionState
    from app.store.sqlite_store import SQLiteStore

    store = SQLiteStore(tmp_path / "runtime.db")
    state = ExecutionState(user_goal="original", replan_limit=2)
    event = make_event(task_id=state.task_id, event_type=EventType.REPLAN_REJECTED, action="replan_rejected")
    store.save_state_and_events(state, [event])
    mutated = state.model_copy(update={"user_goal": "should-roll-back"})
    with pytest.raises(sqlite3.IntegrityError):
        store.save_state_and_events(mutated, [event])
    assert store.get_state(state.task_id).user_goal == "original"


def test_repeated_replan_failures_stop_at_the_round_limit(world: World) -> None:
    scope = _update_scope(max_steps=1, max_mutations=3, max_tool_calls=3)
    state = _task(
        world,
        scope,
        step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
    )
    state = state.model_copy(update={"replan_limit": 2, "replan_count": 0})
    evil = _proposal(
        {
            "step_id": "evil",
            "description": "deactivate",
            "tool_name": "company_api",
            "arguments": {"operation": "deactivate_employee", "employee_id": "E-17"},
            "expected_outcome": "gone",
        }
    )
    llm = ScriptedLLM(evil)
    for _ in range(2):
        result, state = asyncio.run(
            propose_and_commit(
                world.store,
                Replanner(llm, world.registry),
                state,
                scope,
                trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
                failure_summary="same failure",
                enablement=_enabled(2),
            )
        )
        assert result.accepted is False
    assert llm.calls == 2
    assert state.replan_count == 2
    result, state = asyncio.run(
        propose_and_commit(
            world.store,
            Replanner(llm, world.registry),
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="same failure",
            enablement=_enabled(2),
        )
    )
    assert result.accepted is False
    assert result.reason == "replan_limit_exhausted"
    assert llm.calls == 2
    kinds = [event.event_type for event in world.store.get_events(state.task_id)]
    assert EventType.REPLAN_EXHAUSTED in kinds
    assert EventType.REPLAN_ACCEPTED not in kinds


def test_executor_does_not_replan_when_flag_is_on(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AAE_REPLANNING_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert enablement_for_runtime().enabled is True
        world.injector.inject_once("company_api.update_ticket", "invalid_args", "boom")
        state = world.run(
            step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
            authorization=_update_scope(),
            approve=False,
        )
        assert state.replan_count == 0
        kinds = [event.event_type for event in world.store.get_events(state.task_id)]
        assert not any(str(kind).startswith("replan.") for kind in kinds)
    finally:
        get_settings.cache_clear()


def test_no_replan_after_approval_rejection(world: World) -> None:
    from app.models.collections import COLLECTION_REF_KEY

    list_step = step("list", "list_tickets", filters={"status": "resolved"}, limit=5)
    template = PlanStep(
        step_id="close",
        description="close",
        tool_name="company_api",
        arguments={
            "operation": "update_ticket",
            "ticket_id": {COLLECTION_REF_KEY: {"field": "ticket_id"}},
            "fields": {"status": "closed"},
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=1),
            max_items=1,
        ),
    )
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.update_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "resolved"})
            },
            "company_api.update_ticket": {
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": ExactValueConstraint(value="closed"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=1, max_tool_calls=6),
    )
    task = _task(world, scope, list_step, template)
    paused = world.runtime.run(task.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    rejected = world.runtime.reject_task(task.task_id, "no")
    assert rejected.replan_count == 0
    before = world.snapshot()
    llm = ScriptedLLM(_proposal(_update_step("after")))
    result = asyncio.run(
        Replanner(llm, world.registry).propose_async(
            rejected,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="please continue",
            enablement=_enabled(),
        )
    )
    assert result.accepted is False
    assert result.reason == "non_replanable:approval_rejection"
    assert llm.calls == 0
    assert world.snapshot() == before
    assert world.runtime.load(task.task_id).replan_count == 0


def test_concurrent_collection_run_does_not_double_assign(world: World) -> None:
    from app.models.collections import COLLECTION_REF_KEY

    n = 2
    list_step = step(
        "list", "list_tickets", filters={"status": "open", "unassigned": True}, limit=10
    )
    template = PlanStep(
        step_id="assign",
        description="assign",
        tool_name="company_api",
        arguments={
            "operation": "assign_ticket",
            "ticket_id": {COLLECTION_REF_KEY: {"field": "ticket_id"}},
            "employee_id": "E-40",
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=n),
            max_items=n,
        ),
    )
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.assign_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "open", "unassigned": True})
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(max_steps=1 + n, max_mutations=n, max_tool_calls=(1 + n) * 3),
    )
    task = _task(world, scope, list_step, template)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            world.runtime.run(task.task_id)
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            errors.append(exc)

    workers = [threading.Thread(target=run), threading.Thread(target=run)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    assert errors == []
    assert not any(worker.is_alive() for worker in workers)
    final = world.runtime.load(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assigns = [call for call in final.tool_calls if call.arguments.get("operation") == "assign_ticket"]
    assert len(assigns) == n
    assert len({call.arguments["ticket_id"] for call in assigns}) == n


def test_restart_after_committed_assign_does_not_duplicate(world: World) -> None:
    from app.models.collections import COLLECTION_REF_KEY

    n = 2
    list_step = step(
        "list", "list_tickets", filters={"status": "open", "unassigned": True}, limit=10
    )
    template = PlanStep(
        step_id="assign",
        description="assign",
        tool_name="company_api",
        arguments={
            "operation": "assign_ticket",
            "ticket_id": {COLLECTION_REF_KEY: {"field": "ticket_id"}},
            "employee_id": "E-40",
        },
        collection=CollectionWork(
            from_step="list",
            select=CollectionSelect(mode="first_n", n=n),
            max_items=n,
        ),
    )
    scope = AuthorizationScope(
        allowed_operations=["company_api.list_tickets", "company_api.assign_ticket"],
        constraints={
            "company_api.list_tickets": {
                "filters": ExactValueConstraint(value={"status": "open", "unassigned": True})
            },
            "company_api.assign_ticket": {
                "employee_id": ExactValueConstraint(value="E-40"),
                "ticket_id": FromCollectionConstraint(source_step_id="list"),
            },
        },
        budgets=AuthorizationBudgets(
            max_steps=1 + n,
            # Interrupted attempts count. The resume replays the committed assign
            # instead of applying it twice, and still has to fit in this cap.
            max_mutations=n * 3,
            max_tool_calls=(1 + n) * 4,
        ),
    )
    task = _task(world, scope, list_step, template)
    world.runtime.execute_next(task.task_id)
    crash_during(world, "assign_ticket", after_commit=True)
    with pytest.raises(SimulatedCrash):
        world.runtime.execute_next(task.task_id)
    final = reopen(world).runtime.run(task.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assigns = [
        call
        for call in final.tool_calls
        if call.arguments.get("operation") == "assign_ticket"
        and call.status is ToolCallStatus.SUCCEEDED
    ]
    ticket_ids = [call.arguments["ticket_id"] for call in assigns]
    assert len(ticket_ids) == n
    assert len(set(ticket_ids)) == n
    for ticket_id in ticket_ids:
        assert world.repo.get_ticket(ticket_id).assignee_id == "E-40"
