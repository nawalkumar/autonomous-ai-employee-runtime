"""Append-only audit event contracts."""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import Actor
from app.models.ids import new_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditEvent(BaseModel):
    """One immutable audit/timeline entry for a task."""

    event_id: str = Field(default_factory=lambda: new_id("evt_"))
    task_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    event_type: str
    action: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    verification_result: dict[str, Any] | None = None
    actor: Actor = Actor.SYSTEM
