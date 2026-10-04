"""Execution records: tool calls, observations, failures, evidence."""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import FailureType, ToolCallStatus
from app.models.ids import new_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ToolCallRecord(BaseModel):
    """An attempted tool invocation (no tool implementation here)."""

    call_id: str = Field(default_factory=lambda: new_id("call_"))
    task_id: str
    step_id: str | None = None
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    status: ToolCallStatus = ToolCallStatus.RUNNING
    idempotency_key: str | None = None


class Observation(BaseModel):
    """Normalized information observed after an action."""

    observation_id: str = Field(default_factory=lambda: new_id("obs_"))
    source: str
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)


class FailureRecord(BaseModel):
    """A failure encountered during execution (recovery not implemented)."""

    failure_id: str = Field(default_factory=lambda: new_id("fail_"))
    task_id: str
    step_id: str | None = None
    failure_type: FailureType
    message: str
    retryable: bool = False
    timestamp: datetime = Field(default_factory=utc_now)
    recovery_action: str | None = None


class EvidenceItem(BaseModel):
    """Evidence supporting an eventual completion claim."""

    evidence_id: str = Field(default_factory=lambda: new_id("ev_"))
    type: str
    description: str
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class PendingAction(BaseModel):
    """Action awaiting human approval before execution."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    step_id: str | None = None
    idempotency_key: str | None = None
    reason: str | None = None
