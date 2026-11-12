"""Milestone 6: bounded replanning behind AAE_REPLANNING_ENABLED."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.authz import (
    AllowedValuesConstraint,
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
)
from app.config import Settings
from app.llm.base import LLMClient, LLMRequest, LLMResponse
from app.models.enums import PlanStepStatus, TaskStatus
from app.models.plan import PlanStep
from app.runtime.replan import (
    NON_REPLANABLE,
    ReplanEnablement,
    ReplanTrigger,
    Replanner,
    apply_replan,
)
from tests.company_support import World, make_world, step


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path, demo=True)


class ScriptedLLM(LLMClient):
    provider_name = "scripted"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content, model="scripted", provider="scripted")


def _enabled(rounds: int = 2) -> ReplanEnablement:
    return ReplanEnablement(enabled=True, max_rounds=rounds)


def test_flag_defaults_off(tmp_path: Path) -> None:
    settings = Settings(
        database_path=str(tmp_path / "db.sqlite"),
        workspace_path=str(tmp_path / "ws"),
    )
    assert settings.aae_replanning_enabled is False
    assert settings.aae_max_replan_rounds == 2


@pytest.mark.parametrize("category", sorted(NON_REPLANABLE))
def test_non_replanable_categories_denied(world: World, category: str) -> None:
    state = world.runtime.create_task(
        user_goal="g",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
    )
    state = state.model_copy(update={"replan_limit": 2})
    decision = Replanner(ScriptedLLM("{}"), world.registry).evaluate_trigger(
        category=category, state=state, enablement=_enabled()
    )
    assert decision.allowed is False


def test_replan_accepts_authorized_remaining_work(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ExactValueConstraint(value="T-10009"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": AllowedValuesConstraint(
                    values=["in_progress", "resolved"]
                ),
            }
        },
        budgets=AuthorizationBudgets(max_steps=2, max_mutations=6, max_tool_calls=6),
    )
    plan = [
        step("u1", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"}),
        step("u2", "update_ticket", ticket_id="T-10009", fields={"status": "resolved"}),
    ]
    # First step completed; remaining needs revision to resolved-only.
    state = world.runtime.create_task(user_goal="replan", plan=plan, authorization=scope)
    # Manually mark first completed for replanner context.
    plan_done = list(state.plan)
    plan_done[0] = plan_done[0].model_copy(update={"status": PlanStepStatus.COMPLETED})
    state = state.model_copy(
        update={
            "plan": plan_done,
            "completed_steps": ["u1"],
            "replan_limit": 2,
            "replan_count": 0,
        }
    )
    proposal = json.dumps(
        {
            "steps": [
                {
                    "step_id": "u2b",
                    "description": "resolve",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "update_ticket",
                        "ticket_id": "T-10009",
                        "fields": {"status": "resolved"},
                    },
                    "expected_outcome": "resolved",
                }
            ]
        }
    )
    result = asyncio.run(
        Replanner(ScriptedLLM(proposal), world.registry).propose_async(
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="ticket already in_progress",
            enablement=_enabled(),
        )
    )
    assert result.accepted
    updated = apply_replan(state, result)
    assert updated.replan_count == 1
    assert [s.step_id for s in updated.plan] == ["u1", "u2b"]
    assert updated.plan[0].status is PlanStepStatus.COMPLETED


def test_replan_rejects_unauthorized_operation(world: World) -> None:
    scope = AuthorizationScope(
        allowed_operations=["company_api.update_ticket"],
        constraints={
            "company_api.update_ticket": {
                "ticket_id": ExactValueConstraint(value="T-10009"),
                "fields": FieldKeysConstraint(allowed_keys=["status"]),
                "fields.status": ExactValueConstraint(value="in_progress"),
            }
        },
        budgets=AuthorizationBudgets(max_steps=1, max_mutations=3, max_tool_calls=3),
    )
    state = world.runtime.create_task(
        user_goal="deny",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
        authorization=scope,
    )
    state = state.model_copy(update={"replan_limit": 2, "replan_count": 0})
    evil = json.dumps(
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
    result = asyncio.run(
        Replanner(ScriptedLLM(evil), world.registry).propose_async(
            state,
            scope,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="conflict",
            enablement=_enabled(),
        )
    )
    assert result.accepted is False
    assert "rejected" in result.reason or "unauthorized" in result.reason.lower()


def test_replan_limit_exhausted(world: World) -> None:
    state = world.runtime.create_task(
        user_goal="g",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
    )
    state = state.model_copy(update={"replan_count": 2, "replan_limit": 2})
    decision = Replanner(ScriptedLLM("{}"), world.registry).evaluate_trigger(
        category=ReplanTrigger.TICKET_STATE_CONFLICT.value,
        state=state,
        enablement=_enabled(),
    )
    assert decision.allowed is False
    assert "limit" in decision.reason


def test_direct_call_without_enablement_fails_closed(world: World) -> None:
    llm = ScriptedLLM("{}")
    state = world.runtime.create_task(
        user_goal="g",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
    )
    decision = Replanner(llm, world.registry).evaluate_trigger(
        category=ReplanTrigger.TICKET_STATE_CONFLICT.value, state=state
    )
    assert decision.allowed is False
    assert decision.reason == "replanning_disabled"
    result = asyncio.run(
        Replanner(llm, world.registry).propose_async(
            state,
            state.authorization,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="conflict",
        )
    )
    assert result.accepted is False
    assert result.reason == "replanning_disabled"
    assert llm.calls == 0


def test_runtime_settings_disable_replanning_by_default(world: World) -> None:
    from app.runtime.replan import enablement_for_runtime

    settings = Settings(
        database_path=str(world.db),
        workspace_path=str(world.workspace),
        aae_replanning_enabled=False,
    )
    enablement = enablement_for_runtime(settings)
    assert enablement.enabled is False
    llm = ScriptedLLM('{"steps":[]}')
    state = world.runtime.create_task(
        user_goal="g",
        plan=[step("u", "update_ticket", ticket_id="T-10009", fields={"status": "in_progress"})],
    )
    result = asyncio.run(
        Replanner(llm, world.registry).propose_async(
            state,
            state.authorization,
            trigger=ReplanTrigger.TICKET_STATE_CONFLICT,
            failure_summary="conflict",
            enablement=enablement,
        )
    )
    assert result.accepted is False
    assert llm.calls == 0
