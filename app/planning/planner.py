"""InterpretedGoal → validated executable Plan."""

from __future__ import annotations

import asyncio
import json

from app.llm.base import LLMClient, LLMMessage, LLMRequest
from app.llm.structured import StructuredOutputError, complete_structured
from app.models.goal import InterpretedGoal
from app.models.plan import Plan
from app.planning.prompts import PLANNER_SYSTEM_PROMPT
from app.planning.schemas import PlanDraft
from app.planning.validator import (
    PlanValidationError,
    compact_tool_catalog,
    validate_plan,
)
from app.tools.registry import ToolRegistry


class PlanningError(ValueError):
    """Raised when planning fails."""


class Planner:
    """LLM-backed planner (plans only — never executes tools)."""

    def __init__(self, llm: LLMClient, registry: ToolRegistry) -> None:
        self.llm = llm
        self.registry = registry

    async def plan_async(self, goal: InterpretedGoal) -> Plan:
        catalog = compact_tool_catalog(self.registry)
        user_payload = {
            "interpreted_goal": goal.model_dump(mode="json"),
            "available_tools": catalog,
        }
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
                LLMMessage(
                    role="user",
                    content=json.dumps(user_payload, indent=2),
                ),
            ],
            temperature=0.0,
            metadata={"kind": "plan"},
        )
        try:
            draft = await complete_structured(self.llm, request, PlanDraft)
        except StructuredOutputError as exc:
            raise PlanningError(str(exc)) from exc
        except Exception as exc:
            raise PlanningError(f"Planning failed: {exc}") from exc

        try:
            return validate_plan(draft.to_plan(), self.registry)
        except PlanValidationError as exc:
            raise PlanningError(str(exc)) from exc

    def plan(self, goal: InterpretedGoal) -> Plan:
        return asyncio.run(self.plan_async(goal))
