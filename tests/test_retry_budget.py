"""Regression tests: the per-step retry budget survives crashes and restarts."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.models import PlanStep, TaskStatus, ToolCallStatus
from app.models.enums import FailureType, PlanStepStatus
from app.runtime import EventType, ExecutionRuntime
from app.store import SQLiteStore
from app.tools import CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.tools.result import ToolResult
from app.world import CompanyRepository

TICKET_OP = "company_api.create_ticket"


class SimulatedCrash(BaseException):
    """Process death; not caught by the runtime's Exception guard."""


class ProbeTool(CompanyAPITool):
    """Company tool that can crash before its n-th invocation or after a commit.

    ``executions`` is shared across simulated processes and counts invocations
    that actually reached the company API.
    """

    def __init__(
        self,
        repo: CompanyRepository,
        injector: FailureInjector,
        executions: list[str],
        *,
        crash_before: int | None = None,
        crash_after_commit: bool = False,
        retry_safe: bool = True,
    ) -> None:
        super().__init__(repo, failure_injector=injector)
        self.executions = executions
        self.invocations = 0
        self.crash_before = crash_before
        self.crash_after_commit = crash_after_commit
        self._retry_safe = retry_safe

    def is_retry_safe(self, args: BaseModel | dict[str, Any] | None = None) -> bool:
        return self._retry_safe and super().is_retry_safe(args)

    def execute(self, args: BaseModel, *, idempotency_key: str | None = None) -> ToolResult:
        self.invocations += 1
        if self.crash_before is not None and self.invocations == self.crash_before:
            raise SimulatedCrash()
        self.executions.append(idempotency_key or "")
        result = super().execute(args, idempotency_key=idempotency_key)
        if self.crash_after_commit and result.ok:
            self.crash_after_commit = False
            raise SimulatedCrash()
        return result


def _ticket(step_id: str = "s", subject: str = "Billing $240") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="Create ticket",
        tool_name="company_api",
        arguments={
            "operation": "create_ticket",
            "customer_id": "C-1001",
            "subject": subject,
            "description": f"{subject} mismatch",
        },
    )


def _process(
    db_path: Path,
    workspace: Path,
    executions: list[str],
    *,
    max_recovery_attempts: int = 2,
    inject: str | None = None,
    **tool_options: Any,
) -> ExecutionRuntime:
    """A fresh 'process': new store, repository, tools, injector and runtime."""
    injector = FailureInjector()
    if inject == "always":
        injector.inject_always(TICKET_OP, FailureType.TRANSIENT.value, "503 Service Unavailable")
    elif inject == "once":
        injector.inject_once(TICKET_OP, FailureType.TRANSIENT.value, "503 Service Unavailable")
    registry = ToolRegistry()
    registry.register(
        ProbeTool(CompanyRepository(db_path), injector, executions, **tool_options)
    )
    registry.register(FileTool(workspace))
    return ExecutionRuntime(
        store=SQLiteStore(db_path),
        registry=registry,
        workspace_path=workspace,
        max_recovery_attempts=max_recovery_attempts,
    )


def _run_until_crash(runtime: ExecutionRuntime, task_id: str) -> bool:
    try:
        runtime.run(task_id)
    except SimulatedCrash:
        return True
    return False


def _step_calls(runtime: ExecutionRuntime, task_id: str, step_id: str = "s"):
    return [c for c in runtime.load(task_id).tool_calls if c.step_id == step_id]


def _tool_called_attempts(runtime: ExecutionRuntime, task_id: str) -> list[int]:
    return [
        e.result["attempt"]
        for e in runtime.store.get_events(task_id)
        if e.event_type == EventType.TOOL_CALLED
    ]


def _event_count(runtime: ExecutionRuntime, task_id: str, event_type: str) -> int:
    return sum(1 for e in runtime.store.get_events(task_id) if e.event_type == event_type)


# 1 + 3
def test_crash_before_retry_does_not_reset_budget(db_path: Path, workspace: Path) -> None:
    executions: list[str] = []
    first = _process(db_path, workspace, executions, inject="always", crash_before=2)
    task = first.create_task(user_goal="ticket", plan=[_ticket()])
    assert _run_until_crash(first, task.task_id)
    assert [c.status for c in _step_calls(first, task.task_id)] == [
        ToolCallStatus.FAILED,
        ToolCallStatus.RUNNING,
    ]

    second = _process(db_path, workspace, executions, inject="always")
    final = second.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert final.plan[0].status is PlanStepStatus.FAILED
    calls = _step_calls(second, task.task_id)
    assert len(calls) == 3
    assert [c.attempt for c in calls] == [1, 2, 3]
    assert calls[1].error_type == "interrupted"
    assert _tool_called_attempts(second, task.task_id) == [1, 2, 3]
    assert len(executions) <= 3
    assert _event_count(second, task.task_id, EventType.RECOVERY_EXHAUSTED) == 1


# 2
@pytest.mark.parametrize("crash_before", [1, 2, 3])
def test_repeated_crashes_never_exceed_invocation_limit(
    db_path: Path, workspace: Path, crash_before: int
) -> None:
    executions: list[str] = []
    task = _process(db_path, workspace, executions).create_task(
        user_goal="ticket", plan=[_ticket()]
    )
    for _ in range(8):
        runtime = _process(
            db_path, workspace, executions, inject="always", crash_before=crash_before
        )
        _run_until_crash(runtime, task.task_id)

    final = runtime.load(task.task_id)
    assert final.final_status is TaskStatus.FAILED
    calls = _step_calls(runtime, task.task_id)
    assert len(calls) == 3
    assert [c.attempt for c in calls] == [1, 2, 3]
    assert len(executions) <= 3
    assert _tool_called_attempts(runtime, task.task_id) == [1, 2, 3]
    assert len(CompanyRepository(db_path).list_tickets()) == 0

    # Further runs of the terminal task never invoke the tool again.
    before = len(executions)
    _process(db_path, workspace, executions, inject="always").run(task.task_id)
    assert len(executions) == before


# 4
def test_crash_after_commit_replays_once_and_completes(db_path: Path, workspace: Path) -> None:
    executions: list[str] = []
    first = _process(db_path, workspace, executions, crash_after_commit=True)
    task = first.create_task(user_goal="ticket", plan=[_ticket()])
    assert _run_until_crash(first, task.task_id)
    assert len(CompanyRepository(db_path).list_tickets()) == 1

    second = _process(db_path, workspace, executions)
    final = second.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert [t.ticket_id for t in CompanyRepository(db_path).list_tickets()] == ["T-10001"]
    calls = _step_calls(second, task.task_id)
    assert [(c.attempt, c.status, c.error_type) for c in calls] == [
        (1, ToolCallStatus.FAILED, "interrupted"),
        (2, ToolCallStatus.SUCCEEDED, None),
    ]
    assert calls[1].result["data"]["idempotent_replay"] is True
    assert set(executions) == {f"{task.task_id}:s"}


# 5
def test_zero_budget_never_reinvokes_after_failure(db_path: Path, workspace: Path) -> None:
    executions: list[str] = []
    runtime = _process(db_path, workspace, executions, max_recovery_attempts=0, inject="always")
    task = runtime.create_task(user_goal="ticket", plan=[_ticket()])
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert [c.attempt for c in _step_calls(runtime, task.task_id)] == [1]
    assert len(executions) == 1


@pytest.mark.parametrize("crash", ["before_invoke", "after_commit"])
def test_zero_budget_interrupted_initial_attempt_is_not_reinvoked(
    db_path: Path, workspace: Path, crash: str
) -> None:
    executions: list[str] = []
    options = {"crash_before": 1} if crash == "before_invoke" else {"crash_after_commit": True}
    first = _process(db_path, workspace, executions, max_recovery_attempts=0, **options)
    task = first.create_task(user_goal="ticket", plan=[_ticket()])
    assert _run_until_crash(first, task.task_id)
    executed_before_restart = len(executions)

    second = _process(db_path, workspace, executions, max_recovery_attempts=0)
    final = second.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert final.plan[0].status is PlanStepStatus.FAILED
    calls = _step_calls(second, task.task_id)
    assert [(c.attempt, c.error_type) for c in calls] == [(1, "interrupted")]
    assert len(executions) == executed_before_restart
    assert any(f.failure_type is FailureType.REPEATED for f in final.failures)
    assert _event_count(second, task.task_id, EventType.RECOVERY_EXHAUSTED) == 1
    # Conservative policy: a committed write is not reconciled, and never duplicated.
    expected_tickets = 1 if crash == "after_commit" else 0
    assert len(CompanyRepository(db_path).list_tickets()) == expected_tickets


# 6
def test_retry_success_after_restart_emits_retry_succeeded(
    db_path: Path, workspace: Path
) -> None:
    executions: list[str] = []
    first = _process(db_path, workspace, executions, inject="once")
    original_emit = first._emit

    def crash_before_next_attempt(event: Any) -> None:
        original_emit(event)
        if event.event_type == EventType.RECOVERY_RETRY_STARTED:
            raise SimulatedCrash()

    first._emit = crash_before_next_attempt  # type: ignore[method-assign]
    task = first.create_task(user_goal="ticket", plan=[_ticket()])
    assert _run_until_crash(first, task.task_id)
    assert [c.attempt for c in _step_calls(first, task.task_id)] == [1]

    second = _process(db_path, workspace, executions)
    final = second.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    calls = _step_calls(second, task.task_id)
    assert [(c.attempt, c.status) for c in calls] == [
        (1, ToolCallStatus.FAILED),
        (2, ToolCallStatus.SUCCEEDED),
    ]
    assert _tool_called_attempts(second, task.task_id) == [1, 2]
    assert _event_count(second, task.task_id, EventType.RECOVERY_RETRY_SUCCEEDED) == 1


# 7
def test_retry_budget_is_per_step(db_path: Path, workspace: Path) -> None:
    executions: list[str] = []
    runtime = _process(db_path, workspace, executions, max_recovery_attempts=1, inject="once")
    task = runtime.create_task(
        user_goal="two tickets", plan=[_ticket("a", "Billing"), _ticket("b", "Login")]
    )
    runtime.execute_next(task.task_id)

    # Restart between steps; step b gets its own retry.
    second = _process(db_path, workspace, executions, max_recovery_attempts=1, inject="once")
    final = second.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    for step_id in ("a", "b"):
        assert [(c.attempt, c.status) for c in _step_calls(second, task.task_id, step_id)] == [
            (1, ToolCallStatus.FAILED),
            (2, ToolCallStatus.SUCCEEDED),
        ]
    assert final.retry_count == 2  # task-wide statistic, not a budget
    assert len(CompanyRepository(db_path).list_tickets()) == 2


# 8
def test_interrupted_non_retry_safe_step_is_not_reinvoked(
    db_path: Path, workspace: Path
) -> None:
    executions: list[str] = []
    first = _process(db_path, workspace, executions, retry_safe=False, crash_after_commit=True)
    task = first.create_task(user_goal="ticket", plan=[_ticket()])
    assert _run_until_crash(first, task.task_id)

    second = _process(db_path, workspace, executions, retry_safe=False)
    final = second.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert len(executions) == 1
    assert [(c.attempt, c.error_type) for c in _step_calls(second, task.task_id)] == [
        (1, "interrupted")
    ]
    assert "not retry-safe" in (final.completion_summary or "")
    assert len(CompanyRepository(db_path).list_tickets()) == 1


# Backward compatibility
def test_legacy_records_without_attempt_are_numbered_by_position(
    db_path: Path, workspace: Path
) -> None:
    executions: list[str] = []
    first = _process(db_path, workspace, executions, inject="always", crash_before=2)
    task = first.create_task(user_goal="ticket", plan=[_ticket()])
    assert _run_until_crash(first, task.task_id)

    # Rewrite the persisted snapshot as an older version would have stored it.
    with sqlite3.connect(db_path) as conn:
        (raw,) = conn.execute(
            "SELECT state_json FROM tasks WHERE task_id = ?", (task.task_id,)
        ).fetchone()
        payload = json.loads(raw)
        for call in payload["tool_calls"]:
            call.pop("attempt", None)
        conn.execute(
            "UPDATE tasks SET state_json = ? WHERE task_id = ?",
            (json.dumps(payload), task.task_id),
        )
    legacy = SQLiteStore(db_path).get_state(task.task_id)
    assert [c.attempt for c in legacy.tool_calls] == [None, None]

    second = _process(db_path, workspace, executions, inject="always")
    final = second.run(task.task_id)

    calls = _step_calls(second, task.task_id)
    assert final.final_status is TaskStatus.FAILED
    assert [c.attempt for c in calls] == [None, None, 3]
    assert len(calls) == 3


# 9: existing behaviour
@pytest.mark.parametrize("max_recovery_attempts", [0, 1, 2])
def test_permanent_transient_exhausts_at_configured_maximum(
    db_path: Path, workspace: Path, max_recovery_attempts: int
) -> None:
    executions: list[str] = []
    runtime = _process(
        db_path, workspace, executions,
        max_recovery_attempts=max_recovery_attempts, inject="always",
    )
    task = runtime.create_task(user_goal="ticket", plan=[_ticket()])
    final = runtime.run(task.task_id)

    expected = list(range(1, max_recovery_attempts + 2))
    assert final.final_status is TaskStatus.FAILED
    assert [c.attempt for c in _step_calls(runtime, task.task_id)] == expected
    assert _tool_called_attempts(runtime, task.task_id) == expected
    assert len(executions) == max_recovery_attempts + 1
    assert _event_count(runtime, task.task_id, EventType.RECOVERY_EXHAUSTED) == 1


def test_transient_failure_then_success_records_attempts(db_path: Path, workspace: Path) -> None:
    executions: list[str] = []
    runtime = _process(db_path, workspace, executions, inject="once")
    task = runtime.create_task(user_goal="ticket", plan=[_ticket()])
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.COMPLETED
    assert [(c.attempt, c.status) for c in _step_calls(runtime, task.task_id)] == [
        (1, ToolCallStatus.FAILED),
        (2, ToolCallStatus.SUCCEEDED),
    ]
    assert _event_count(runtime, task.task_id, EventType.RECOVERY_RETRY_SUCCEEDED) == 1
    assert final.retry_count == 1


def test_non_retryable_failure_makes_one_attempt(db_path: Path, workspace: Path) -> None:
    executions: list[str] = []
    runtime = _process(db_path, workspace, executions)
    bad = _ticket().model_copy(
        update={"arguments": {**_ticket().arguments, "customer_id": "C-404"}}
    )
    task = runtime.create_task(user_goal="ticket", plan=[bad])
    final = runtime.run(task.task_id)

    assert final.final_status is TaskStatus.FAILED
    assert [c.attempt for c in _step_calls(runtime, task.task_id)] == [1]
    assert len(executions) == 1
    assert _event_count(runtime, task.task_id, EventType.RECOVERY_RETRY_STARTED) == 0
