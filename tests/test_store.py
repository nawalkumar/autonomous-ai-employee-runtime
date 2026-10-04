"""Unit tests for SQLite persistence (temporary DB only)."""

from pathlib import Path

import pytest

from app.models import (
    AuditEvent,
    Actor,
    ExecutionState,
    FailureRecord,
    FailureType,
    InterpretedGoal,
    Observation,
    PlanStep,
    SuccessCriterion,
    TaskStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from app.store import SQLiteStore


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "test_runtime.db")


def _sample_state(task_id: str = "task_persist_1") -> ExecutionState:
    return ExecutionState(
        task_id=task_id,
        user_goal="Create a support ticket for Acme Corp",
        interpreted_goal=InterpretedGoal(
            objective="Create billing ticket",
            entities={"customer": "Acme Corp"},
            success_criteria=[
                SuccessCriterion(
                    description="Ticket exists",
                    criterion_type="record_exists",
                    target="tickets",
                    expected_value={"customer": "Acme Corp"},
                )
            ],
        ),
        success_criteria=[
            SuccessCriterion(
                description="Ticket exists",
                criterion_type="record_exists",
                target="tickets",
            )
        ],
        plan=[
            PlanStep(step_id="step_1", description="Lookup customer", tool_hint="company_api"),
            PlanStep(step_id="step_2", description="Create ticket", tool_hint="company_api"),
        ],
        current_step=1,
        completed_steps=["step_1"],
        tool_calls=[
            ToolCallRecord(
                call_id="call_1",
                task_id=task_id,
                step_id="step_1",
                tool_name="company_api",
                arguments={"op": "lookup"},
                status=ToolCallStatus.SUCCEEDED,
                idempotency_key="idem-1",
            )
        ],
        observations=[
            Observation(
                observation_id="obs_1",
                source="company_api",
                ok=True,
                data={"customer_id": "C-42"},
            )
        ],
        failures=[
            FailureRecord(
                failure_id="fail_1",
                task_id=task_id,
                step_id="step_2",
                failure_type=FailureType.TRANSIENT,
                message="temporary 503",
                retryable=True,
                recovery_action="retry",
            )
        ],
        retry_count=1,
        extracted_information={"customer_id": "C-42"},
        final_status=TaskStatus.RUNNING,
    )


def test_database_initializes(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "runtime.db"
    store = SQLiteStore(db_path)
    assert db_path.exists()
    assert store.get_state("missing") is None
    assert store.get_events("missing") == []


def test_save_and_get_state_roundtrip(store: SQLiteStore) -> None:
    original = _sample_state()
    store.save_state(original)
    loaded = store.get_state(original.task_id)

    assert loaded is not None
    assert loaded.task_id == original.task_id
    assert loaded.user_goal == original.user_goal
    assert loaded.interpreted_goal is not None
    assert loaded.interpreted_goal.objective == "Create billing ticket"
    assert loaded.plan[0].step_id == "step_1"
    assert loaded.tool_calls[0].idempotency_key == "idem-1"
    assert loaded.observations[0].data["customer_id"] == "C-42"
    assert loaded.failures[0].failure_type is FailureType.TRANSIENT
    assert loaded.extracted_information["customer_id"] == "C-42"
    assert loaded.final_status is TaskStatus.RUNNING
    assert loaded.created_at == original.created_at
    assert loaded.updated_at == original.updated_at


def test_update_state_replaces_snapshot(store: SQLiteStore) -> None:
    state = _sample_state("task_update")
    store.save_state(state)

    updated = state.model_copy(
        update={
            "final_status": TaskStatus.COMPLETED,
            "completion_summary": "Ticket created",
            "retry_count": 2,
            "current_step": 2,
        }
    ).touch()
    store.save_state(updated)

    loaded = store.get_state("task_update")
    assert loaded is not None
    assert loaded.final_status is TaskStatus.COMPLETED
    assert loaded.completion_summary == "Ticket created"
    assert loaded.retry_count == 2
    assert loaded.current_step == 2


def test_unknown_task_returns_none(store: SQLiteStore) -> None:
    assert store.get_state("does-not-exist") is None


def test_append_and_get_events_ordered(store: SQLiteStore) -> None:
    task_id = "task_events"
    store.save_state(ExecutionState(task_id=task_id, user_goal="goal"))

    first = AuditEvent(
        event_id="evt_1",
        task_id=task_id,
        event_type="task.created",
        action="create",
        actor=Actor.SYSTEM,
    )
    second = AuditEvent(
        event_id="evt_2",
        task_id=task_id,
        event_type="tool.called",
        action="invoke",
        tool="company_api",
        arguments={"op": "lookup"},
        result={"ok": True},
        actor=Actor.AGENT,
    )
    third = AuditEvent(
        event_id="evt_3",
        task_id=task_id,
        event_type="failure.detected",
        failure={"type": "transient", "message": "503"},
        actor=Actor.SYSTEM,
    )

    store.append_event(first)
    store.append_event(second)
    store.append_event(third)

    events = store.get_events(task_id)
    assert [e.event_id for e in events] == ["evt_1", "evt_2", "evt_3"]
    assert events[1].tool == "company_api"
    assert events[1].arguments == {"op": "lookup"}
    assert events[2].failure == {"type": "transient", "message": "503"}


def test_events_are_append_only(store: SQLiteStore) -> None:
    event = AuditEvent(
        event_id="evt_unique",
        task_id="task_ao",
        event_type="task.created",
    )
    store.append_event(event)
    with pytest.raises(Exception):
        store.append_event(event)

    events = store.get_events("task_ao")
    assert len(events) == 1


def test_events_isolated_by_task(store: SQLiteStore) -> None:
    store.append_event(
        AuditEvent(event_id="a1", task_id="t1", event_type="x")
    )
    store.append_event(
        AuditEvent(event_id="a2", task_id="t2", event_type="y")
    )
    assert len(store.get_events("t1")) == 1
    assert store.get_events("t1")[0].event_id == "a1"
    assert store.get_events("unknown") == []
