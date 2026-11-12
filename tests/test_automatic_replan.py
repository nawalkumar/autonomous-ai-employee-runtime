"""Controlled automatic replanning: durable commits and executor recovery."""

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
)
from app.authz.intent_scopes import authorization_for_trusted_intent
from app.config import get_settings
from app.llm.base import LLMClient, LLMRequest, LLMResponse
from app.llm.errors import LLMProviderError
from app.models.enums import TaskStatus, ToolCallStatus, VerificationStatus
from app.models.events import AuditEvent
from app.models.goal import SuccessCriterion
from app.planning.collection_plans import plan_for_collection_intent
from app.planning.intents import parse_trusted_intent
from app.runtime.replan import (
    ReplanEnablement,
    ReplanResult,
    ReplanTrigger,
    Replanner,
    apply_replan,
    commit_replan,
    propose_and_commit,
)
from app.runtime.replan_triggers import recoverable_trigger
from app.runtime.transitions import EventType
from app.store.revision_guard import (
    RevisionPersistenceError,
    StaleRevisionError,
    plan_structure_token,
)
from app.tools.result import ToolResult
from tests.company_support import SimulatedCrash, World, make_world, reopen, step


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


@pytest.fixture
def replan_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AAE_REPLANNING_ENABLED", "true")
    monkeypatch.setenv("AAE_MAX_REPLAN_ROUNDS", "2")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class ScriptedLLM(LLMClient):
    provider_name = "scripted"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content, model="scripted", provider="scripted")


class TimeoutLLM(LLMClient):
    provider_name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        raise LLMProviderError("timeout")


def _enabled(rounds: int = 2) -> ReplanEnablement:
    return ReplanEnablement(enabled=True, max_rounds=rounds)


def _scope(
    *statuses: str,
    tickets: tuple[str, ...] = ("T-10009",),
    steps: int = 2,
    mutations: int = 6,
    calls: int = 6,
) -> AuthorizationScope:
    ticket_constraint = (
        ExactValueConstraint(value=tickets[0])
        if len(tickets) == 1
        else AllowedValuesConstraint(values=list(tickets))
    )
    return AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ticket_constraint,
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": AllowedValuesConstraint(values=list(statuses)),
            }
        },
        budgets=AuthorizationBudgets(
            max_steps=steps, max_mutations=mutations, max_tool_calls=calls
        ),
    )


def _proposal(step_id: str, status: str, ticket_id: str = "T-10009") -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "step_id": step_id,
                    "description": "replacement",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "update_ticket",
                        "ticket_id": ticket_id,
                        "fields": {"status": status},
                    },
                    "expected_outcome": status,
                }
            ]
        }
    )


def _two_steps(first: str, second: str) -> str:
    def one(step_id: str, status: str) -> dict:
        return {
            "step_id": step_id,
            "description": status,
            "tool_name": "company_api",
            "arguments": {
                "operation": "update_ticket",
                "ticket_id": "T-10009",
                "fields": {"status": status},
            },
            "expected_outcome": status,
        }

    return json.dumps({"steps": [one("a", first), one("b", second)]})


def _deactivate() -> str:
    return json.dumps(
        {
            "steps": [
                {
                    "step_id": "evil",
                    "description": "deactivate",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "deactivate_employee",
                        "employee_id": "E-17",
                    },
                    "expected_outcome": "gone",
                }
            ]
        }
    )


def _replan_events(world: World, task_id: str) -> list:
    return [
        event
        for event in world.store.get_events(task_id)
        if str(event.event_type).startswith("replan.")
    ]


def test_ordinary_save_rejects_an_applied_revision(world: World) -> None:
    scope = _scope("open", "in_progress", steps=2, mutations=4, calls=4)
    state = world.runtime.create_task(
        user_goal="bypass",
        plan=[step("u1", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=scope,
    )
    result = ReplanResult(
        accepted=True,
        revision=1,
        trigger=ReplanTrigger.TICKET_STATE_CONFLICT.value,
        reason="accepted",
        replacement_steps=[
            step("u2", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})
        ],
        prior_plan_step_ids=["u1"],
    )
    spliced = apply_replan(state, result)
    forged = AuditEvent(
        task_id=state.task_id,
        event_type=EventType.REPLAN_ACCEPTED,
        action="replan_accepted",
    )
    with pytest.raises(RevisionPersistenceError):
        world.store.save_state(spliced)
    with pytest.raises(RevisionPersistenceError):
        world.store.save_state_and_events(spliced, [forged])
    stored = world.store.get_state(state.task_id)
    assert stored is not None
    assert [item.step_id for item in stored.plan] == ["u1"]
    assert stored.replan_count == 0
    assert _replan_events(world, state.task_id) == []


def test_concurrent_commits_install_one_revision(world: World) -> None:
    scope = _scope("open", "in_progress", steps=2, mutations=4, calls=4)
    state = world.runtime.create_task(
        user_goal="race",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=scope,
    )
    result = ReplanResult(
        accepted=True,
        revision=1,
        trigger=ReplanTrigger.TICKET_STATE_CONFLICT.value,
        reason="accepted",
        replacement_steps=[
            step("u2", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})
        ],
        prior_plan_step_ids=["u"],
    )
    barrier = threading.Barrier(2)
    original = world.store.commit_checked_revision

    def wait_then_commit(*args, **kwargs):
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    world.store.commit_checked_revision = wait_then_commit  # type: ignore[method-assign]
    outcomes: list[ReplanResult] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        try:
            committed, _ = commit_replan(
                world.store, state, result, scope=scope, enablement=_enabled()
            )
            outcomes.append(committed)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    reasons = sorted(item.reason for item in outcomes)
    assert reasons == ["accepted", "rejected:stale_revision"]
    stored = world.store.get_state(state.task_id)
    assert stored is not None
    assert stored.replan_count == 1
    assert [item.step_id for item in stored.plan] == ["u2"]
    accepted = [
        event
        for event in _replan_events(world, state.task_id)
        if event.event_type == EventType.REPLAN_ACCEPTED
    ]
    assert len(accepted) == 1
    world.store.commit_checked_revision = original  # type: ignore[method-assign]
    with pytest.raises(StaleRevisionError):
        world.store.commit_checked_revision(
            stored.model_copy(update={"replan_count": 2}),
            [],
            expected_replan_count=0,
            expected_structure=plan_structure_token(state.plan),
        )
    assert world.store.get_state(state.task_id).replan_count == 1


def test_revision_audit_failure_rolls_back(world: World) -> None:
    scope = _scope("open", steps=1, mutations=3, calls=3)
    state = world.runtime.create_task(
        user_goal="audit",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=scope,
    )
    event = AuditEvent(
        task_id=state.task_id,
        event_type=EventType.REPLAN_ACCEPTED,
        action="replan_accepted",
    )
    revised = state.model_copy(
        update={"replan_count": 1, "last_replan_trigger": "ticket_state_conflict"}
    )
    with pytest.raises(sqlite3.IntegrityError):
        world.store.commit_checked_revision(
            revised,
            [event, event],
            expected_replan_count=0,
            expected_structure=plan_structure_token(state.plan),
        )
    stored = world.store.get_state(state.task_id)
    assert stored is not None
    assert stored.replan_count == 0
    assert [item.step_id for item in stored.plan] == ["u"]
    assert _replan_events(world, state.task_id) == []


def test_identical_proposal_makes_no_progress(world: World) -> None:
    scope = _scope("open", steps=1, mutations=3, calls=3)
    state = world.runtime.create_task(
        user_goal="same",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=scope,
    )
    result, updated = asyncio.run(
        propose_and_commit(
            world.store,
            Replanner(ScriptedLLM(_proposal("u", "open")), world.registry),
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="cannot change from resolved to open",
            enablement=_enabled(),
        )
    )
    assert result.accepted is False
    assert result.reason == "rejected:no_progress"
    assert updated.replan_count == 1
    assert [item.step_id for item in updated.plan] == ["u"]


def test_flag_off_does_not_replan(world: World) -> None:
    llm = ScriptedLLM(_proposal("fix", "in_progress"))
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="hold",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.replan_count == 0
    assert llm.calls == 0
    assert world.repo.get_ticket("T-10009").status == "resolved"
    assert _replan_events(world, state.task_id) == []


def test_supported_conflict_recovers(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_proposal("fix", "in_progress"))
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="recover",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
        success_criteria=[
            SuccessCriterion(
                description="ticket moved",
                criterion_type="ticket_fields",
                expected_value={"ticket_id": "T-10009", "status": "in_progress"},
            )
        ],
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.replan_count == 1
    assert llm.calls == 1
    assert world.repo.get_ticket("T-10009").status == "in_progress"
    assert [event.event_type for event in _replan_events(world, state.task_id)] == [
        EventType.REPLAN_PROPOSED,
        EventType.REPLAN_ACCEPTED,
    ]
    succeeded = [
        call
        for call in final.tool_calls
        if call.status is ToolCallStatus.SUCCEEDED
        and call.arguments.get("fields", {}).get("status") == "in_progress"
    ]
    assert len(succeeded) == 1


def test_unsupported_conflict_does_not_replan(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_proposal("fix", "in_progress"))
    world.runtime._replan_llm = llm
    world.injector.inject_once("company_api.update_ticket", "invalid_args", "boom")
    state = world.runtime.create_task(
        user_goal="boom",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
        authorization=_scope("in_progress"),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert final.replan_count == 0
    assert llm.calls == 0
    assert _replan_events(world, state.task_id) == []


def test_unauthorized_replacement_is_rejected(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_deactivate())
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="deny",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert "rejected" in (final.completion_summary or "")
    assert final.replan_count == 1
    assert world.repo.get_employee("E-17").status == "active"
    assert world.repo.get_ticket("T-10009").status == "resolved"
    assert EventType.REPLAN_ACCEPTED not in {event.event_type for event in _replan_events(world, state.task_id)}


def test_replacement_over_budget_is_rejected(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_two_steps("in_progress", "resolved"))
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="budget",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress", "resolved", steps=1, mutations=3, calls=3),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert "budget_exceeded:max_steps" in (final.completion_summary or "")
    assert [item.step_id for item in final.plan] == ["u"]
    assert final.replan_count == 1
    assert world.repo.get_ticket("T-10009").status == "resolved"


def test_completed_mutation_is_not_replayed(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_proposal("fix", "in_progress"))
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="two",
        plan=[
            step("first", "update_ticket", ticket_id="T-10002", fields={"status": "in_progress"}),
            step("second", "update_ticket", ticket_id="T-10009", fields={"status": "open"}),
        ],
        authorization=_scope(
            "open",
            "in_progress",
            tickets=("T-10002", "T-10009"),
            steps=2,
            mutations=6,
            calls=6,
        ),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert world.repo.get_ticket("T-10002").status == "in_progress"
    assert world.repo.get_ticket("T-10009").status == "in_progress"
    assert final.plan[0].step_id == "first"
    progressed = [
        call
        for call in final.tool_calls
        if call.status is ToolCallStatus.SUCCEEDED
        and call.arguments.get("ticket_id") == "T-10002"
        and call.arguments.get("fields", {}).get("status") == "in_progress"
    ]
    assert len(progressed) == 1
    assert llm.calls == 1


def test_crash_before_revision_commit_keeps_original_plan(world: World, replan_on: None) -> None:
    world.runtime._replan_llm = ScriptedLLM(_proposal("fix", "in_progress"))

    def crash(*_args, **_kwargs):
        raise SimulatedCrash()

    world.store.commit_checked_revision = crash  # type: ignore[method-assign]
    state = world.runtime.create_task(
        user_goal="crash-before",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
    )
    with pytest.raises(SimulatedCrash):
        world.runtime.run(state.task_id)
    stored = world.store.get_state(state.task_id)
    assert stored is not None
    assert stored.replan_count == 0
    assert [item.step_id for item in stored.plan] == ["u"]
    assert world.repo.get_ticket("T-10009").status == "resolved"
    assert _replan_events(world, state.task_id) == []


def test_crash_after_revision_commit_resumes_once(world: World, replan_on: None) -> None:
    world.runtime._replan_llm = ScriptedLLM(_proposal("fix", "in_progress"))
    original = world.runtime._execute_next_locked

    def crash_after(task_id: str):
        loaded = world.runtime.load(task_id)
        if loaded.replan_count > 0:
            raise SimulatedCrash()
        return original(task_id)

    world.runtime._execute_next_locked = crash_after  # type: ignore[method-assign]
    state = world.runtime.create_task(
        user_goal="crash-after",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
        success_criteria=[
            SuccessCriterion(
                description="ticket moved",
                criterion_type="ticket_fields",
                expected_value={"ticket_id": "T-10009", "status": "in_progress"},
            )
        ],
    )
    with pytest.raises(SimulatedCrash):
        world.runtime.run(state.task_id)
    parked = world.store.get_state(state.task_id)
    assert parked is not None
    assert parked.replan_count == 1
    assert [item.step_id for item in parked.plan] == ["fix"]
    assert world.repo.get_ticket("T-10009").status == "resolved"

    restarted = reopen(world)
    final = restarted.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.verification_status is VerificationStatus.PASSED
    assert final.replan_count == 1
    assert world.repo.get_ticket("T-10009").status == "in_progress"
    accepted = [
        event
        for event in restarted.store.get_events(state.task_id)
        if event.event_type == EventType.REPLAN_ACCEPTED
    ]
    assert len(accepted) == 1


def test_approval_rejection_does_not_bypass_replan(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_proposal("close", "closed"))
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="close",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "closed"),
    )
    paused = world.runtime.run(state.task_id)
    assert paused.final_status is TaskStatus.NEEDS_APPROVAL
    assert paused.pending_action is not None
    assert paused.pending_action.arguments["fields"]["status"] == "closed"
    assert world.repo.get_ticket("T-10009").status == "resolved"
    rejected = world.runtime.reject_task(state.task_id, reason="no")
    assert rejected.final_status is TaskStatus.ABORTED
    assert rejected.replan_count == 1
    assert llm.calls == 1
    assert world.repo.get_ticket("T-10009").status == "resolved"


def test_provider_timeout_fails_without_a_revision(world: World, replan_on: None) -> None:
    llm = TimeoutLLM()
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="timeout",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "open"})],
        authorization=_scope("open", "in_progress"),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert llm.calls == 1
    assert "rejected:provider" in (final.completion_summary or "")
    assert [item.step_id for item in final.plan] == ["u"]
    assert world.repo.get_ticket("T-10009").status == "resolved"
    dumped = json.dumps([event.model_dump(mode="json") for event in _replan_events(world, state.task_id)])
    assert "sk-" not in dumped
    assert "api_key" not in dumped


def test_collection_conflict_does_not_expand_scope(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_deactivate())
    world.runtime._replan_llm = llm
    goal = "Assign up to 2 open high-priority tickets to employee E-40."
    intent = parse_trusted_intent(goal)
    assert intent is not None
    plan = plan_for_collection_intent(intent)
    scope = authorization_for_trusted_intent(intent)
    before_rows, _ = world.repo.query_tickets(status="open", priority="high", limit=20)
    before = {ticket.ticket_id: ticket.assignee_id for ticket in before_rows}
    world.injector.inject_once(
        "company_api.assign_ticket",
        "policy_blocked",
        "Ticket T-10002 is resolved; only open or in-progress tickets can be assigned",
    )
    state = world.runtime.create_task(
        user_goal=goal, plan=plan.steps, authorization=scope
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert "rejected" in (final.completion_summary or "")
    assert world.repo.get_employee("E-17").status == "active"
    after_rows, _ = world.repo.query_tickets(status="open", priority="high", limit=20)
    after = {ticket.ticket_id: ticket.assignee_id for ticket in after_rows}
    assert after == before
    assert "deactivate_employee" not in scope.allowed_operations
    assert final.authorization is not None
    assert "deactivate_employee" not in final.authorization.allowed_operations
    assert any(item.step_id == "list" and item.step_id in final.completed_steps for item in final.plan)


def test_supported_triggers_are_narrow() -> None:
    conflict = ToolResult(
        ok=False,
        error_type="invalid_args",
        error_message="Ticket T-10009 cannot change from resolved to open (allowed: closed)",
    )
    assert (
        recoverable_trigger(conflict, conflict_type("invalid_args"))
        is ReplanTrigger.TICKET_STATE_CONFLICT
    )
    generic = ToolResult(ok=False, error_type="invalid_args", error_message="boom")
    assert recoverable_trigger(generic, conflict_type("invalid_args")) is None
    inactive = ToolResult(
        ok=False,
        error_type="policy_blocked",
        error_message="Employee E-45 is inactive and cannot be assigned",
    )
    assert recoverable_trigger(inactive, conflict_type("policy_blocked")) is None
    missing = ToolResult(ok=False, error_type="environment_unexpected", error_message="Ticket not found: T-9")
    assert (
        recoverable_trigger(missing, conflict_type("environment_unexpected"))
        is ReplanTrigger.ENTITY_MISSING_TRANSIENT
    )
    employee = ToolResult(
        ok=False, error_type="environment_unexpected", error_message="Employee not found: E-1"
    )
    assert recoverable_trigger(employee, conflict_type("environment_unexpected")) is None


def conflict_type(name: str):
    from app.models.enums import FailureType

    return {
        "invalid_args": FailureType.INVALID_ARGS,
        "policy_blocked": FailureType.POLICY_BLOCKED,
        "environment_unexpected": FailureType.ENVIRONMENT_UNEXPECTED,
    }[name]


def test_legacy_update_is_unchanged_when_replanning_is_on(world: World, replan_on: None) -> None:
    llm = ScriptedLLM(_proposal("fix", "resolved", ticket_id="T-10002"))
    world.runtime._replan_llm = llm
    state = world.runtime.create_task(
        user_goal="legacy",
        plan=[step("u", "update_ticket", ticket_id="T-10002", fields={"status": "in_progress"})],
        authorization=_scope(
            "in_progress", tickets=("T-10002",), steps=1, mutations=3, calls=3
        ),
    )
    final = world.runtime.run(state.task_id)
    assert final.final_status is TaskStatus.COMPLETED
    assert final.replan_count == 0
    assert llm.calls == 0
    assert world.repo.get_ticket("T-10002").status == "in_progress"
    assert _replan_events(world, state.task_id) == []
