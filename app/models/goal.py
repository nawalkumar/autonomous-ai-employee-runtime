"""Goal understanding contracts."""

from typing import Any

from pydantic import BaseModel, Field


class SuccessCriterion(BaseModel):
    """One independently verifiable condition for task completion."""

    description: str
    criterion_type: str = "generic"
    target: str | None = None
    expected_value: Any = None


class InterpretedGoal(BaseModel):
    """Structured understanding of a natural-language user goal."""

    objective: str
    entities: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
