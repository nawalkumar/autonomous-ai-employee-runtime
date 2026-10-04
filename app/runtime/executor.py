"""Deterministic plan execution engine (no LLM, no retry, no recovery)."""

from __future__ import annotations

from typing import Any

from app.models.enums import (
    Actor,
    FailureType,
    PlanStepStatus,
    TaskStatus,
    ToolCallStatus,
)
from app.models.goal import InterpretedGoal
from app.models.plan import PlanStep
from app.models.records import (
    EvidenceItem,
    FailureRecord,
    Observation,
    ToolCallRecord,
    utc_now,
)
from app.models.state import ExecutionState
from app.runtime.transitions import (
    EventType,
    idempotency_key_for,
    make_event,
    observe_tool_result,
)
from app.store.sqlite_store import SQLiteStore
from app.tools.registry import ToolRegistry
from app.tools.result import ToolResult


class ExecutionRuntime:
    """Executes a predefined plan against tools via the registry."""

    def __init__(self, store: SQLiteStore, registry: ToolRegistry) -> None:
        self.store = store
        self.registry = registry

    def create_task(
        self,
        *,
        user_goal: str,
        plan: list[PlanStep],
        interpreted_goal: InterpretedGoal | None = None,
        task_id: str | None = None,
    ) -> ExecutionState:
        """Create and persist a queued ExecutionState with a predefined plan."""
        if not plan:
            raise ValueError("plan must contain at least one step")

        kwargs: dict[str, Any] = {
            "user_goal": user_goal,
            "interpreted_goal": interpreted_goal,
            "success_criteria": (
                list(interpreted_goal.success_criteria) if interpreted_goal else []
            ),
            "plan": plan,
            "final_status": TaskStatus.QUEUED,
            "current_step": 0,
        }
        if task_id is not None:
            kwargs["task_id"] = task_id

        state = ExecutionState(**kwargs)
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TASK_CREATED,
                action="create_task",
                result={"plan_steps": len(plan), "user_goal": user_goal},
            )
        )
        return state

    def load(self, task_id: str) -> ExecutionState:
        state = self.store.get_state(task_id)
        if state is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        return state

    def run(self, task_id: str) -> ExecutionState:
        """Execute pending steps until the task completes or fails. No retries."""
        state = self.load(task_id)
        if state.final_status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.ABORTED,
        }:
            return state

        if state.final_status is TaskStatus.QUEUED:
            state = self._mark_running(state)

        while True:
            state = self.load(task_id)
            if state.final_status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.ABORTED,
            }:
                return state
            advanced = self.execute_next(task_id)
            if advanced.final_status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.ABORTED,
            }:
                return advanced
            # Safety: if nothing progressed, stop rather than loop forever.
            if advanced.current_step == state.current_step and all(
                s.status is not PlanStepStatus.PENDING for s in advanced.plan
            ):
                return advanced

    def execute_next(self, task_id: str) -> ExecutionState:
        """Execute exactly one pending plan step, then persist and return."""
        state = self.load(task_id)
        if state.final_status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.ABORTED,
        }:
            return state

        if state.final_status is TaskStatus.QUEUED:
            state = self._mark_running(state)

        step_index = self._next_pending_index(state)
        if step_index is None:
            return self._complete_task(state)

        return self._execute_step(state, step_index)

    def _mark_running(self, state: ExecutionState) -> ExecutionState:
        state = state.model_copy(update={"final_status": TaskStatus.RUNNING}).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.PLAN_STARTED,
                action="plan_started",
                result={"current_step": state.current_step},
            )
        )
        return state

    def _next_pending_index(self, state: ExecutionState) -> int | None:
        for index, step in enumerate(state.plan):
            if step.status is PlanStepStatus.PENDING:
                return index
            if step.status is PlanStepStatus.RUNNING:
                # Incomplete step after interruption: allow one re-attempt.
                return index
        return None

    def _execute_step(self, state: ExecutionState, step_index: int) -> ExecutionState:
        step = state.plan[step_index]
        tool_name = step.resolved_tool_name()
        if not tool_name:
            return self._fail_missing_tool(state, step_index, step, None)

        # Mark step running
        state = self._update_step(
            state,
            step_index,
            status=PlanStepStatus.RUNNING,
            current_step=step_index,
        )
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.STEP_STARTED,
                action="step_started",
                tool=tool_name,
                arguments={
                    "step_id": step.step_id,
                    "description": step.description,
                    **step.arguments,
                },
            )
        )

        key = idempotency_key_for(state.task_id, step.step_id)
        call = ToolCallRecord(
            task_id=state.task_id,
            step_id=step.step_id,
            tool_name=tool_name,
            arguments=dict(step.arguments),
            status=ToolCallStatus.RUNNING,
            idempotency_key=key,
        )
        state = state.model_copy(
            update={"tool_calls": [*state.tool_calls, call]}
        ).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TOOL_CALLED,
                action="tool_called",
                tool=tool_name,
                arguments=dict(step.arguments),
                result={"idempotency_key": key, "step_id": step.step_id},
                actor=Actor.AGENT,
            )
        )

        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            result = ToolResult.failure(
                FailureType.ENVIRONMENT_UNEXPECTED.value,
                f"Unknown tool: {tool_name}",
            )
            return self._finish_failed_step(state, step_index, call, result)

        result = tool.invoke(step.arguments, idempotency_key=key)
        if result.ok:
            return self._finish_successful_step(state, step_index, call, result)
        return self._finish_failed_step(state, step_index, call, result)

    def _finish_successful_step(
        self,
        state: ExecutionState,
        step_index: int,
        call: ToolCallRecord,
        result: ToolResult,
    ) -> ExecutionState:
        step = state.plan[step_index]
        summary = observe_tool_result(call.tool_name, result)
        completed_at = utc_now()

        updated_call = call.model_copy(
            update={
                "status": ToolCallStatus.SUCCEEDED,
                "completed_at": completed_at,
                "result": result.model_dump(),
                "error_type": None,
                "error_message": None,
            }
        )
        observation = Observation(
            source=call.tool_name,
            ok=True,
            summary=summary,
            data=result.data or {},
        )
        evidence = list(state.evidence)
        for item in result.evidence:
            evidence.append(
                EvidenceItem(
                    type=str(item.get("type", "tool_evidence")),
                    description=summary,
                    data=item.get("data", item) if isinstance(item.get("data", item), dict) else {"value": item},
                )
            )

        tool_calls = [
            updated_call if c.call_id == call.call_id else c for c in state.tool_calls
        ]
        plan = list(state.plan)
        plan[step_index] = step.model_copy(update={"status": PlanStepStatus.COMPLETED})
        completed_steps = [*state.completed_steps, step.step_id]
        extracted = dict(state.extracted_information)
        extracted.update(self._extract_info(result))

        state = state.model_copy(
            update={
                "plan": plan,
                "tool_calls": tool_calls,
                "observations": [*state.observations, observation],
                "evidence": evidence,
                "completed_steps": completed_steps,
                "extracted_information": extracted,
                "current_step": step_index + 1,
            }
        ).touch()
        self._save(state)

        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TOOL_SUCCEEDED,
                action="tool_succeeded",
                tool=call.tool_name,
                result=result.model_dump(),
                actor=Actor.AGENT,
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.STEP_SUCCEEDED,
                action="step_succeeded",
                tool=call.tool_name,
                observation={"summary": summary, "step_id": step.step_id},
            )
        )

        if self._next_pending_index(state) is None:
            return self._complete_task(state)
        return state

    def _finish_failed_step(
        self,
        state: ExecutionState,
        step_index: int,
        call: ToolCallRecord,
        result: ToolResult,
    ) -> ExecutionState:
        step = state.plan[step_index]
        summary = observe_tool_result(call.tool_name, result)
        completed_at = utc_now()
        error_type = result.error_type or FailureType.ENVIRONMENT_UNEXPECTED.value

        updated_call = call.model_copy(
            update={
                "status": ToolCallStatus.FAILED,
                "completed_at": completed_at,
                "result": result.model_dump(),
                "error_type": error_type,
                "error_message": result.error_message,
            }
        )
        observation = Observation(
            source=call.tool_name,
            ok=False,
            summary=summary,
            data=result.data or {},
            error_type=error_type,
            error_message=result.error_message,
        )
        try:
            failure_type = FailureType(error_type)
        except ValueError:
            failure_type = FailureType.ENVIRONMENT_UNEXPECTED

        failure = FailureRecord(
            task_id=state.task_id,
            step_id=step.step_id,
            failure_type=failure_type,
            message=result.error_message or summary,
            retryable=failure_type is FailureType.TRANSIENT,
            recovery_action=None,
        )

        tool_calls = [
            updated_call if c.call_id == call.call_id else c for c in state.tool_calls
        ]
        plan = list(state.plan)
        plan[step_index] = step.model_copy(update={"status": PlanStepStatus.FAILED})

        state = state.model_copy(
            update={
                "plan": plan,
                "tool_calls": tool_calls,
                "observations": [*state.observations, observation],
                "failures": [*state.failures, failure],
                "current_step": step_index,
                "final_status": TaskStatus.FAILED,
                "completion_summary": summary,
            }
        ).touch()
        self._save(state)

        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TOOL_FAILED,
                action="tool_failed",
                tool=call.tool_name,
                result=result.model_dump(),
                failure={
                    "failure_type": failure_type.value,
                    "message": failure.message,
                },
                actor=Actor.AGENT,
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.STEP_FAILED,
                action="step_failed",
                tool=call.tool_name,
                observation={"summary": summary, "step_id": step.step_id},
                failure={
                    "failure_type": failure_type.value,
                    "message": failure.message,
                },
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TASK_FAILED,
                action="task_failed",
                failure={
                    "failure_type": failure_type.value,
                    "message": failure.message,
                    "step_id": step.step_id,
                },
            )
        )
        return state

    def _fail_missing_tool(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        tool_name: str | None,
    ) -> ExecutionState:
        call = ToolCallRecord(
            task_id=state.task_id,
            step_id=step.step_id,
            tool_name=tool_name or "unknown",
            arguments=dict(step.arguments),
            status=ToolCallStatus.FAILED,
            idempotency_key=idempotency_key_for(state.task_id, step.step_id),
            error_type=FailureType.INVALID_ARGS.value,
            error_message="Plan step is missing tool_name/tool_hint",
            completed_at=utc_now(),
        )
        result = ToolResult.failure(
            FailureType.INVALID_ARGS.value,
            "Plan step is missing tool_name/tool_hint",
        )
        state = state.model_copy(
            update={
                "tool_calls": [*state.tool_calls, call],
                "current_step": step_index,
            }
        ).touch()
        return self._finish_failed_step(state, step_index, call, result)

    def _complete_task(self, state: ExecutionState) -> ExecutionState:
        summary = (
            f"Completed {len(state.completed_steps)} plan step(s) successfully."
        )
        state = state.model_copy(
            update={
                "final_status": TaskStatus.COMPLETED,
                "completion_summary": summary,
            }
        ).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TASK_COMPLETED,
                action="task_completed",
                result={
                    "completed_steps": state.completed_steps,
                    "summary": summary,
                },
            )
        )
        return state

    def _update_step(
        self,
        state: ExecutionState,
        step_index: int,
        *,
        status: PlanStepStatus,
        current_step: int,
    ) -> ExecutionState:
        plan = list(state.plan)
        plan[step_index] = plan[step_index].model_copy(update={"status": status})
        return state.model_copy(
            update={"plan": plan, "current_step": current_step}
        ).touch()

    def _extract_info(self, result: ToolResult) -> dict[str, Any]:
        data = result.data or {}
        extracted: dict[str, Any] = {}
        if "ticket" in data and isinstance(data["ticket"], dict):
            extracted["last_ticket_id"] = data["ticket"].get("ticket_id")
            extracted["last_customer_id"] = data["ticket"].get("customer_id")
        if "employee" in data and isinstance(data["employee"], dict):
            extracted["last_employee_id"] = data["employee"].get("employee_id")
            extracted["last_employee_title"] = data["employee"].get("title")
        if "customers" in data and data["customers"]:
            first = data["customers"][0]
            if isinstance(first, dict):
                extracted["last_customer_id"] = first.get("customer_id")
                extracted["last_customer_company"] = first.get("company")
        if "path" in data:
            extracted["last_file_path"] = data.get("path")
        return extracted

    def _save(self, state: ExecutionState) -> None:
        self.store.save_state(state)

    def _emit(self, event: Any) -> None:
        self.store.append_event(event)
