"""Planning contracts (structure only — no planner logic)."""

from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import PlanStepStatus
from app.models.ids import new_id


class PlanStep(BaseModel):
    """One planned step in an execution plan."""

    step_id: str = Field(default_factory=lambda: new_id("step_"))
    description: str
    tool_name: str | None = None
    tool_hint: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING

    def resolved_tool_name(self) -> str | None:
        """Prefer explicit tool_name; fall back to tool_hint for compatibility."""
        return self.tool_name or self.tool_hint
