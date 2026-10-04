"""Goal understanding contracts."""

from typing import Any

from pydantic import BaseModel, Field

from app.models.ids import new_id


class SuccessCriterion(BaseModel):
    """One independently verifiable condition for task completion."""

    description: str
    criterion_type: str = "generic"
    target: str | None = None
    expected_value: Any = None


class InterpretedGoal(BaseModel):
    """Structured understanding of a natural-language user goal."""

    goal_id: str = Field(default_factory=lambda: new_id("goal_"))
    original_request: str = ""
    objective: str
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    required_information: list[str] = Field(default_factory=list)
