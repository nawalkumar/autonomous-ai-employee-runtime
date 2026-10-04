"""Verification result contracts."""

from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import VerificationStatus
from app.models.records import EvidenceItem


class VerificationCheckResult(BaseModel):
    """One independent check against actual world state."""

    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    message: str = ""


class VerificationResult(BaseModel):
    """Aggregate outcome of independent verification."""

    status: VerificationStatus
    passed: bool
    summary: str
    checks: list[VerificationCheckResult] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    failure_reason: str | None = None
