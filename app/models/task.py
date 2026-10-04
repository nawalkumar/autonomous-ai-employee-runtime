"""Task metadata contract."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.models.enums import TaskStatus
from app.models.ids import new_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Task(BaseModel):
    """User-submitted task identity and lifecycle status."""

    task_id: str = Field(default_factory=lambda: new_id("task_"))
    user_goal: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    status: TaskStatus = TaskStatus.QUEUED
