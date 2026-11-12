"""LLM-facing structured output schemas for planning."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    """Structured planner output (legacy mock planner)."""

    steps: list[PlanStep] = Field(default_factory=list)

    def to_plan(self) -> Plan:
        return Plan(steps=self.steps)


class DynamicPlanStepDraft(BaseModel):
    """Strict step shape for untrusted LLM plan proposals."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=500)
    tool_name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = Field(default="", max_length=500)

    def to_plan_step(self) -> PlanStep:
        return PlanStep(
            step_id=self.step_id,
            description=self.description,
            tool_name=self.tool_name,
            arguments=self.arguments,
            expected_outcome=self.expected_outcome,
        )


class DynamicPlanDraft(BaseModel):
    """Strict plan shape for untrusted LLM plan proposals."""

    model_config = ConfigDict(extra="forbid")

    steps: list[DynamicPlanStepDraft] = Field(default_factory=list)

    def to_plan(self) -> Plan:
        return Plan(steps=[step.to_plan_step() for step in self.steps])


__all__ = [
    "InterpretedGoalDraft",
    "PlanDraft",
    "DynamicPlanDraft",
    "DynamicPlanStepDraft",
    "Plan",
    "PlanStep",
    "InterpretedGoal",
]
