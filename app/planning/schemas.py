"""LLM-facing structured output schemas for planning."""

from typing import Any

from pydantic import BaseModel, Field

from app.models.goal import InterpretedGoal, SuccessCriterion
from app.models.plan import Plan, PlanStep


class InterpretedGoalDraft(BaseModel):
    """Structured interpreter output before domain enrichment."""

    original_request: str = ""
    objective: str
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    required_information: list[str] = Field(default_factory=list)

    def to_interpreted_goal(self, original_request: str) -> InterpretedGoal:
        return InterpretedGoal(
            original_request=self.original_request or original_request,
            objective=self.objective,
            entities=self.entities,
            constraints=self.constraints,
            success_criteria=self.success_criteria,
            required_information=self.required_information,
        )


class PlanDraft(BaseModel):
    """Structured planner output."""

    steps: list[PlanStep] = Field(default_factory=list)

    def to_plan(self) -> Plan:
        return Plan(steps=self.steps)


__all__ = [
    "InterpretedGoalDraft",
    "PlanDraft",
    "Plan",
    "PlanStep",
    "InterpretedGoal",
]
