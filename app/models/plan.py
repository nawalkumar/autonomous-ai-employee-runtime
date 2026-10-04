"""Planning contracts (structure only — no planner logic)."""

from pydantic import BaseModel, Field

from app.models.enums import PlanStepStatus
from app.models.ids import new_id


class PlanStep(BaseModel):
    """One planned step in an execution plan."""

    step_id: str = Field(default_factory=lambda: new_id("step_"))
    description: str
    tool_hint: str | None = None
    expected_outcome: str = ""
    status: PlanStepStatus = PlanStepStatus.PENDING
