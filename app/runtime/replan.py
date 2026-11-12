"""Bounded replanning of remaining work (Milestone 6).

The replanner may only propose replacements for unfinished steps when a
recognized recoverable business-state condition occurs. It never rewrites
history, resets budgets, expands authorization, or auto-approves mutations.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.authz.models import AuthorizationScope
from app.authz.plan_check import authorize_plan
from app.llm.base import LLMClient, LLMMessage, LLMRequest
from app.llm.errors import LLMProviderError
from app.llm.structured import StructuredOutputError, complete_structured
from app.models.plan import Plan, PlanStep
from app.models.state import ExecutionState
from app.planning.schemas import DynamicPlanDraft
from app.planning.validator import PlanValidationError, validate_plan
from app.tools.registry import ToolRegistry


class ReplanTrigger(str, Enum):
    """Recognized recoverable conditions. Everything else is non-replanable."""

    TICKET_STATE_CONFLICT = "ticket_state_conflict"
    ENTITY_MISSING_TRANSIENT = "entity_missing_transient"


# Denials and security failures — never replan.
NON_REPLANABLE = frozenset(
    {
        "authorization_denial",
        "missing_authorization",
        "approval_rejection",
        "budget_exhaustion",
        "invalid_operation",
        "malformed_plan",
        "policy_violation",
        "unknown_verification",
        "ambiguous_entity",
    }
)


class ReplanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed: bool
    trigger: ReplanTrigger | None = None
    reason: str = ""
    remaining_step_ids: list[str] = Field(default_factory=list)


class ReplanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    revision: int
    trigger: str
    reason: str
    replacement_steps: list[PlanStep] = Field(default_factory=list)
    prior_plan_step_ids: list[str] = Field(default_factory=list)


class Replanner:
    """Propose remaining-work revisions under the original authorization scope."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        max_tokens: int = 4096,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_tokens = max_tokens

    def evaluate_trigger(
        self,
        *,
        category: str,
        state: ExecutionState,
    ) -> ReplanDecision:
        if category in NON_REPLANABLE:
            return ReplanDecision(allowed=False, reason=f"non_replanable:{category}")
        try:
            trigger = ReplanTrigger(category)
        except ValueError:
            return ReplanDecision(allowed=False, reason=f"unknown_trigger:{category}")
        if state.replan_count >= state.replan_limit:
            return ReplanDecision(allowed=False, reason="replan_limit_exhausted")
        remaining = [
            step.step_id
            for step in state.plan
            if step.status.value in {"pending", "running"}
        ]
        completed = set(state.completed_steps)
        remaining = [sid for sid in remaining if sid not in completed]
        return ReplanDecision(
            allowed=True,
            trigger=trigger,
            reason="recoverable",
            remaining_step_ids=remaining,
        )

    async def propose_async(
        self,
        state: ExecutionState,
        scope: AuthorizationScope,
        *,
        trigger: ReplanTrigger,
        failure_summary: str,
    ) -> ReplanResult:
        decision = self.evaluate_trigger(category=trigger.value, state=state)
        if not decision.allowed:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count,
                trigger=trigger.value,
                reason=decision.reason,
                prior_plan_step_ids=[s.step_id for s in state.plan],
            )

        completed = [
            {
                "step_id": step.step_id,
                "tool": step.resolved_tool_name(),
                "status": step.status.value,
            }
            for step in state.plan
            if step.step_id in set(state.completed_steps)
        ]
        remaining = [
            step.model_dump(mode="json")
            for step in state.plan
            if step.step_id in set(decision.remaining_step_ids)
        ]
        payload = {
            "goal": state.user_goal,
            "trigger": trigger.value,
            "failure_summary": failure_summary[:500],
            "completed_steps": completed,
            "remaining_steps": remaining,
            "allowed_operations": list(scope.allowed_operations),
            "remaining_budgets": {
                "max_steps": scope.budgets.max_steps,
                "max_mutations": scope.budgets.max_mutations,
                "max_tool_calls": scope.budgets.max_tool_calls,
                "tool_calls_used": len(state.tool_calls or []),
            },
        }
        request = LLMRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "Propose a JSON plan for REMAINING work only. Do not repeat "
                        "completed steps. Do not expand permissions or budgets. "
                        "Return {\"steps\":[...]} using only allowed operations."
                    ),
                ),
                LLMMessage(role="user", content=str(payload)),
            ],
            temperature=0.0,
            max_tokens=self.max_tokens,
            metadata={"kind": "replan", "trigger": trigger.value},
        )
        try:
            draft = await complete_structured(self.llm, request, DynamicPlanDraft)
            plan = validate_plan(draft.to_plan(), self.registry)
            authorize_plan(plan, scope)
        except (LLMProviderError, StructuredOutputError, PlanValidationError, Exception) as exc:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count + 1,
                trigger=trigger.value,
                reason=f"rejected:{exc}",
                prior_plan_step_ids=[s.step_id for s in state.plan],
            )

        # Remaining budgets: expanded plan must fit original caps considering history.
        mutating = sum(
            1
            for step in plan.steps
            if (step.arguments or {}).get("operation")
            in {
                "update_employee",
                "create_ticket",
                "update_ticket",
                "assign_ticket",
                "create_customer",
                "deactivate_employee",
                "write",
            }
        )
        if len(plan.steps) > scope.budgets.max_steps:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count + 1,
                trigger=trigger.value,
                reason="budget_exceeded:max_steps",
                prior_plan_step_ids=[s.step_id for s in state.plan],
            )
        used_mutations = sum(
            1
            for call in state.tool_calls or []
            if (call.arguments or {}).get("operation")
            in {
                "update_employee",
                "create_ticket",
                "update_ticket",
                "assign_ticket",
                "create_customer",
                "deactivate_employee",
                "write",
            }
        )
        if used_mutations + mutating > scope.budgets.max_mutations:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count + 1,
                trigger=trigger.value,
                reason="budget_exceeded:max_mutations",
                prior_plan_step_ids=[s.step_id for s in state.plan],
            )
        if len(state.tool_calls or []) >= scope.budgets.max_tool_calls:
            return ReplanResult(
                accepted=False,
                revision=state.replan_count + 1,
                trigger=trigger.value,
                reason="budget_exceeded:max_tool_calls",
                prior_plan_step_ids=[s.step_id for s in state.plan],
            )

        return ReplanResult(
            accepted=True,
            revision=state.replan_count + 1,
            trigger=trigger.value,
            reason="accepted",
            replacement_steps=list(plan.steps),
            prior_plan_step_ids=[s.step_id for s in state.plan],
        )


def apply_replan(state: ExecutionState, result: ReplanResult) -> ExecutionState:
    """Replace unfinished steps with an accepted revision; keep completed history."""
    if not result.accepted:
        return state.model_copy(
            update={
                "replan_count": result.revision,
                "last_replan_trigger": result.trigger,
            }
        ).touch()

    completed_ids = set(state.completed_steps)
    kept = [step for step in state.plan if step.step_id in completed_ids]
    # Preserve completed step objects in order; append replacement for remaining work.
    new_plan = [*kept, *result.replacement_steps]
    return state.model_copy(
        update={
            "plan": new_plan,
            "replan_count": result.revision,
            "last_replan_trigger": result.trigger,
            "current_step": len(kept),
            "final_status": state.final_status,
        }
    ).touch()
