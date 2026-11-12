"""LLM-backed dynamic planner: proposes plans only; never authorizes or executes."""

from __future__ import annotations

import json
from typing import Any

from app.authz.errors import AuthorizationError
from app.authz.models import AuthorizationScope
from app.authz.plan_check import authorize_plan
from app.llm.base import LLMClient, LLMMessage, LLMRequest
from app.llm.errors import LLMProviderError
from app.llm.structured import StructuredOutputError, complete_structured
from app.models.goal import InterpretedGoal
from app.models.plan import Plan
from app.planning.capability_context import (
    REFERENCE_SYNTAX,
    capability_context_for_scope,
    planner_limits,
)
from app.planning.planner import PlanningError
from app.planning.prompts import DYNAMIC_PLANNER_SYSTEM_PROMPT
from app.planning.schemas import DynamicPlanDraft
from app.planning.validator import PlanValidationError, validate_plan
from app.tools.registry import ToolRegistry


class DynamicPlanner:
    """Provider-independent planner that treats model output as untrusted data."""

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        max_provider_retries: int = 1,
        max_tokens: int = 4096,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_provider_retries = max(0, max_provider_retries)
        self.max_tokens = max_tokens

    def build_request(
        self,
        goal: InterpretedGoal,
        scope: AuthorizationScope,
        *,
        intent_id: str | None = None,
    ) -> LLMRequest:
        payload: dict[str, Any] = {
            "goal": {
                "original_request": goal.original_request,
                "objective": goal.objective,
                "entities": goal.entities,
                "constraints": goal.constraints,
                "success_criteria": [c.model_dump(mode="json") for c in goal.success_criteria],
            },
            # Informational only — deterministic authorization remains authoritative.
            "allowed_operations": list(scope.allowed_operations),
            "plan_limits": planner_limits(scope),
            "capabilities": capability_context_for_scope(self.registry, scope),
            "reference_syntax": REFERENCE_SYNTAX,
        }
        if intent_id:
            payload["intent_id"] = intent_id
        return LLMRequest(
            messages=[
                LLMMessage(role="system", content=DYNAMIC_PLANNER_SYSTEM_PROMPT),
                LLMMessage(role="user", content=json.dumps(payload, indent=2)),
            ],
            temperature=0.0,
            max_tokens=self.max_tokens,
            metadata={
                "kind": "dynamic_plan",
                "intent_id": intent_id or "",
            },
        )

    async def plan_async(
        self,
        goal: InterpretedGoal,
        scope: AuthorizationScope,
        *,
        intent_id: str | None = None,
    ) -> Plan:
        request = self.build_request(goal, scope, intent_id=intent_id)
        attempts = 1 + self.max_provider_retries
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                draft = await complete_structured(self.llm, request, DynamicPlanDraft)
            except LLMProviderError as exc:
                last_error = exc
                # Missing credentials / auth / oversized are not worth retrying.
                if exc.kind in {"missing_credentials", "auth", "oversized", "rejected"}:
                    raise PlanningError(str(exc)) from exc
                if attempt + 1 >= attempts:
                    raise PlanningError(str(exc)) from exc
                continue
            except StructuredOutputError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise PlanningError(f"Dynamic plan failed schema validation: {exc}") from exc
                continue
            except Exception as exc:  # noqa: BLE001 - classify as provider failure
                last_error = exc
                if attempt + 1 >= attempts:
                    raise PlanningError(f"Dynamic planning failed: {exc}") from exc
                continue

            try:
                plan = validate_plan(draft.to_plan(), self.registry)
            except PlanValidationError as exc:
                # Deterministic validation failure — do not regenerate forever.
                raise PlanningError(str(exc)) from exc

            try:
                authorize_plan(plan, scope)
            except AuthorizationError as exc:
                # Never retry after an authorization rejection.
                raise PlanningError(f"Dynamic plan unauthorized: {exc}") from exc

            return plan

        raise PlanningError(f"Dynamic planning failed: {last_error}")
