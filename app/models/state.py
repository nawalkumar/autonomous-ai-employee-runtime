"""Central ExecutionState contract for pause/resume/inspect/recover/verify."""

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.authz.models import AuthorizationScope
from app.models.collections import CollectionExecutionState
from app.models.enums import ApprovalStatus, TaskStatus, VerificationStatus
from app.models.goal import InterpretedGoal, SuccessCriterion
from app.models.ids import new_id
from app.models.plan import PlanStep
from app.models.records import (
    EvidenceItem,
    FailureRecord,
    Observation,
    PendingAction,
    ToolCallRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionState(BaseModel):
    """Domain-level runtime state — independent of LangGraph or any orchestrator."""

    task_id: str = Field(default_factory=lambda: new_id("task_"))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    user_goal: str
    interpreted_goal: InterpretedGoal | None = None
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)

    # Goal-scoped authorization. Set at trusted task creation and immutable
    # thereafter (except via a separately trusted re-authorization path, which
    # this milestone does not expose). Legacy snapshots may omit it; the runtime
    # maps only known deterministic scenarios or fails closed.
    authorization: AuthorizationScope | None = None
    authorization_fingerprint: str | None = None

    plan: list[PlanStep] = Field(default_factory=list)
    current_step: int = 0
    completed_steps: list[str] = Field(default_factory=list)

    # Bounded collection snapshots and per-item progress (Milestone 6).
    collections: CollectionExecutionState = Field(default_factory=CollectionExecutionState)
    # Bounded replanning (Milestone 6); retained across resume.
    replan_count: int = 0
    replan_limit: int = 2
    last_replan_trigger: str | None = None

    observations: list[Observation] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)

    retry_count: int = 0
    extracted_information: dict[str, Any] = Field(default_factory=dict)

    approval_status: ApprovalStatus = ApprovalStatus.NONE
    pending_action: PendingAction | None = None

    verification_status: VerificationStatus = VerificationStatus.PENDING
    verification_summary: str | None = None
    verification_checks: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    final_status: TaskStatus = TaskStatus.QUEUED
    completion_summary: str | None = None

    def touch(self) -> "ExecutionState":
        """Return a copy with updated_at set to now (immutable-friendly)."""
        return self.model_copy(update={"updated_at": utc_now()})
