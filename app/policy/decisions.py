"""Policy decision contracts for HITL gates."""

from pydantic import BaseModel, Field

from app.tools.base import RiskLevel


class PolicyDecision(BaseModel):
    requires_approval: bool
    reason: str
    risk: RiskLevel = RiskLevel.WRITE
    operation: str | None = None
