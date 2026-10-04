"""Unit tests for Phase 1 domain contracts."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import (
    ApprovalStatus,
    EvidenceItem,
    ExecutionState,
    FailureRecord,
    FailureType,
    InterpretedGoal,
    Observation,
    PendingAction,
    PlanStep,
    PlanStepStatus,
    SuccessCriterion,
    Task,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
    VerificationStatus,
)


def test_execution_state_defaults() -> None:
    state = ExecutionState(user_goal="Create a support ticket for Acme")
    assert state.task_id.startswith("task_")
    assert state.final_status is TaskStatus.QUEUED
    assert state.approval_status is ApprovalStatus.NONE
    assert state.verification_status is VerificationStatus.PENDING
    assert state.plan == []
    assert state.observations == []
    assert state.tool_calls == []
    assert state.failures == []
    assert state.evidence == []
    assert state.extracted_information == {}
    assert state.retry_count == 0
    assert state.current_step == 0
    assert state.pending_action is None
    assert state.created_at.tzinfo is not None
    assert state.updated_at.tzinfo is not None


def test_nested_models_validate() -> None:
    criterion = SuccessCriterion(
        description="Ticket exists for Acme",
        criterion_type="record_exists",
        target="tickets",
        expected_value={"customer": "Acme"},
    )
    goal = InterpretedGoal(
        objective="Create billing ticket",
        entities={"customer": "Acme"},
        constraints=["do not charge card"],
        success_criteria=[criterion],
    )
    step = PlanStep(
        description="Lookup customer",
        tool_hint="company_api",
        expected_outcome="customer id resolved",
    )
    state = ExecutionState(
        user_goal="Create a support ticket for Acme",
        interpreted_goal=goal,
        success_criteria=[criterion],
        plan=[step],
        tool_calls=[
            ToolCallRecord(
                task_id="task_demo",
                tool_name="company_api",
                arguments={"op": "lookup", "name": "Acme"},
                status=ToolCallStatus.SUCCEEDED,
            )
        ],
        observations=[
            Observation(source="company_api", ok=True, data={"customer_id": "C-1"})
        ],
        failures=[
            FailureRecord(
                task_id="task_demo",
                failure_type=FailureType.TRANSIENT,
                message="503 from create_ticket",
                retryable=True,
            )
        ],
        evidence=[
            EvidenceItem(
                type="db_snapshot",
                description="Ticket created",
                data={"ticket_id": "T-1"},
            )
        ],
        pending_action=PendingAction(
            tool_name="company_api",
            arguments={"op": "create_ticket"},
            reason="write action",
        ),
        approval_status=ApprovalStatus.PENDING,
        final_status=TaskStatus.NEEDS_APPROVAL,
    )

    assert state.interpreted_goal is not None
    assert state.interpreted_goal.objective == "Create billing ticket"
    assert state.plan[0].status is PlanStepStatus.PENDING
    assert state.tool_calls[0].status is ToolCallStatus.SUCCEEDED
    assert state.failures[0].failure_type is FailureType.TRANSIENT
    assert state.pending_action is not None
    assert state.pending_action.tool_name == "company_api"


def test_invalid_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionState(user_goal="x", final_status="not-a-status")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        FailureRecord(
            task_id="t1",
            failure_type="unknown_failure",  # type: ignore[arg-type]
            message="boom",
        )

    with pytest.raises(ValidationError):
        Task(user_goal="x", status="done")  # type: ignore[arg-type]


def test_execution_state_json_roundtrip() -> None:
    state = ExecutionState(
        user_goal="Update employee title",
        interpreted_goal=InterpretedGoal(objective="Update title"),
        success_criteria=[
            SuccessCriterion(description="Title is Senior Engineer", target="employees")
        ],
        plan=[PlanStep(description="Update record", tool_hint="company_api")],
        final_status=TaskStatus.RUNNING,
        verification_status=VerificationStatus.PENDING,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, 3, 5, 0, tzinfo=timezone.utc),
    )
    payload = state.model_dump_json()
    restored = ExecutionState.model_validate_json(payload)

    assert restored == state
    assert restored.final_status is TaskStatus.RUNNING
    assert restored.plan[0].tool_hint == "company_api"
    assert restored.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_task_model() -> None:
    task = Task(user_goal="Do something")
    assert task.status is TaskStatus.QUEUED
    assert task.task_id.startswith("task_")


def test_touch_updates_timestamp() -> None:
    state = ExecutionState(user_goal="goal")
    original = state.updated_at
    touched = state.touch()
    assert touched.updated_at >= original
    assert touched.user_goal == state.user_goal
