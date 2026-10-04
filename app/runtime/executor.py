"""Deterministic plan execution engine with bounded recovery."""

from __future__ import annotations

import logging
from typing import Any

from pathlib import Path

from app.config import get_settings
from app.models.enums import (
    Actor,
    ApprovalStatus,
    FailureType,
    PlanStepStatus,
    TaskStatus,
    ToolCallStatus,
    VerificationStatus,
)
from app.models.goal import InterpretedGoal
from app.models.plan import PlanStep
from app.models.records import (
    EvidenceItem,
    FailureRecord,
    Observation,
    PendingAction,
    ToolCallRecord,
    utc_now,
)
from app.models.state import ExecutionState
from app.policy import ApprovalPolicy
from app.runtime.recovery import (
    RecoveryAction,
    classify_failure,
    evaluate_recovery,
)
from app.runtime.transitions import (
    EventType,
    idempotency_key_for,
    make_event,
    observe_tool_result,
)
from app.store.sqlite_store import SQLiteStore
from app.tools.base import BaseTool
from app.tools.registry import ToolRegistry
from app.tools.result import ToolResult
from app.verify.verifier import OutcomeVerifier
from app.world.repository import CompanyRepository

logger = logging.getLogger(__name__)

_TERMINAL = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.ABORTED,
}


class ApprovalError(ValueError):
    """Raised for invalid approval/rejection operations."""


class ExecutionRuntime:
    """Executes a predefined plan against tools via the registry."""

    def __init__(
        self,
        store: SQLiteStore,
        registry: ToolRegistry,
        *,
        max_recovery_attempts: int | None = None,
        verifier: OutcomeVerifier | None = None,
        workspace_path: str | Path | None = None,
        policy: ApprovalPolicy | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.policy = policy or ApprovalPolicy()
        self.max_recovery_attempts = (
            max_recovery_attempts
            if max_recovery_attempts is not None
            else get_settings().max_recovery_attempts
        )
        if verifier is not None:
            self.verifier = verifier
        else:
            ws = workspace_path or get_settings().workspace_path
            self.verifier = OutcomeVerifier(
                CompanyRepository(store.db_path),
                ws,
            )

    def create_task(
        self,
        *,
        user_goal: str,
        plan: list[PlanStep],
        interpreted_goal: InterpretedGoal | None = None,
        success_criteria: list | None = None,
        task_id: str | None = None,
    ) -> ExecutionState:
        """Create and persist a queued ExecutionState with a predefined plan."""
        if not plan:
            raise ValueError("plan must contain at least one step")

        criteria = list(success_criteria or [])
        if not criteria and interpreted_goal is not None:
            criteria = list(interpreted_goal.success_criteria)

        kwargs: dict[str, Any] = {
            "user_goal": user_goal,
            "interpreted_goal": interpreted_goal,
            "success_criteria": criteria,
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
        """Execute pending steps until complete, failed, aborted, or approval required."""
        state = self.load(task_id)
        if state.final_status in _TERMINAL:
            return state
        if state.final_status is TaskStatus.NEEDS_APPROVAL:
            return state

        if state.final_status is TaskStatus.QUEUED:
            state = self._mark_running(state)

        while True:
            state = self.load(task_id)
            if state.final_status in _TERMINAL:
                return state
            if state.final_status is TaskStatus.NEEDS_APPROVAL:
                return state
            advanced = self.execute_next(task_id)
            if advanced.final_status in _TERMINAL:
                return advanced
            if advanced.final_status is TaskStatus.NEEDS_APPROVAL:
                return advanced
            if advanced.current_step == state.current_step and all(
                s.status is not PlanStepStatus.PENDING for s in advanced.plan
            ):
                return advanced

    def execute_next(self, task_id: str) -> ExecutionState:
        """Execute exactly one pending plan step (with bounded recovery), then return."""
        state = self.load(task_id)
        if state.final_status in _TERMINAL:
            return state
        if state.final_status is TaskStatus.NEEDS_APPROVAL:
            return state

        if state.final_status is TaskStatus.QUEUED:
            state = self._mark_running(state)

        step_index = self._next_pending_index(state)
        if step_index is None:
            return self._complete_task(state)

        return self._execute_step(state, step_index)

    def approve_task(self, task_id: str) -> ExecutionState:
        """Approve the pending action and resume execution from that step."""
        state = self.load(task_id)
        if (
            state.final_status is not TaskStatus.NEEDS_APPROVAL
            or state.approval_status is not ApprovalStatus.PENDING
            or state.pending_action is None
        ):
            raise ApprovalError("no pending approval")

        pending = state.pending_action.model_copy(
            update={"resolved_at": utc_now()}
        )
        state = state.model_copy(
            update={
                "approval_status": ApprovalStatus.APPROVED,
                "pending_action": pending,
                "final_status": TaskStatus.RUNNING,
            }
        ).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.APPROVAL_APPROVED,
                action="approval_approved",
                tool=pending.tool_name,
                arguments=dict(pending.arguments),
                result={
                    "step_id": pending.step_id,
                    "risk": pending.risk,
                },
                actor=Actor.HUMAN,
            )
        )
        return self.run(task_id)

    def reject_task(self, task_id: str, reason: str = "Rejected by human") -> ExecutionState:
        """Reject the pending action and terminate without executing the tool."""
        state = self.load(task_id)
        if (
            state.final_status is not TaskStatus.NEEDS_APPROVAL
            or state.approval_status is not ApprovalStatus.PENDING
            or state.pending_action is None
        ):
            raise ApprovalError("no pending approval")

        pending = state.pending_action.model_copy(
            update={"resolved_at": utc_now()}
        )
        state = state.model_copy(
            update={
                "approval_status": ApprovalStatus.REJECTED,
                "pending_action": pending,
                "final_status": TaskStatus.ABORTED,
                "completion_summary": reason,
            }
        ).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.APPROVAL_REJECTED,
                action="approval_rejected",
                tool=pending.tool_name,
                arguments=dict(pending.arguments),
                result={
                    "step_id": pending.step_id,
                    "risk": pending.risk,
                    "reason": reason,
                },
                actor=Actor.HUMAN,
            )
        )
        return state

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
                return index
        return None

    def _execute_step(self, state: ExecutionState, step_index: int) -> ExecutionState:
        step = state.plan[step_index]
        tool_name = step.resolved_tool_name()
        if not tool_name:
            return self._fail_missing_tool(state, step_index, step, None)

        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            tool = None

        # Policy gate BEFORE any tool mutation or step RUNNING transition.
        paused_or_ready = self._apply_approval_gate(
            state, step_index, step, tool_name, tool
        )
        if paused_or_ready.final_status is TaskStatus.NEEDS_APPROVAL:
            return paused_or_ready
        state = paused_or_ready

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

        attempt = 0
        while True:
            attempt += 1
            key = idempotency_key_for(state.task_id, step.step_id)
            logger.info(
                "[EXECUTE] step=%s tool=%s attempt=%s",
                step.step_id,
                tool_name,
                attempt,
            )

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
                    result={
                        "idempotency_key": key,
                        "step_id": step.step_id,
                        "attempt": attempt,
                    },
                    actor=Actor.AGENT,
                )
            )

            if tool is None:
                result = ToolResult.failure(
                    FailureType.ENVIRONMENT_UNEXPECTED.value,
                    f"Unknown tool: {tool_name}",
                )
            else:
                result = tool.invoke(step.arguments, idempotency_key=key)

            if result.ok:
                if attempt > 1:
                    self._emit(
                        make_event(
                            task_id=state.task_id,
                            event_type=EventType.RECOVERY_RETRY_SUCCEEDED,
                            action="recovery_retry_succeeded",
                            tool=tool_name,
                            result={
                                "step_id": step.step_id,
                                "attempt": attempt,
                            },
                        )
                    )
                    logger.info("[RECOVERY] recovered step=%s attempt=%s", step.step_id, attempt)
                return self._finish_successful_step(state, step_index, call, result)

            state, decision = self._handle_failed_attempt(
                state=state,
                step_index=step_index,
                call=call,
                result=result,
                tool=tool,
                attempt=attempt,
            )
            if decision.action is RecoveryAction.RETRY:
                self._emit(
                    make_event(
                        task_id=state.task_id,
                        event_type=EventType.RECOVERY_RETRY_STARTED,
                        action="recovery_retry_started",
                        tool=tool_name,
                        result={
                            "step_id": step.step_id,
                            "attempt": attempt,
                            "next_attempt": attempt + 1,
                            "reason": decision.reason,
                        },
                    )
                )
                logger.info(
                    "[RECOVERY] type=%s action=RETRY attempt=%s",
                    decision.failure_type.value,
                    attempt,
                )
                state = self.load(state.task_id)
                step = state.plan[step_index]
                continue

            if decision.failure_type is FailureType.REPEATED or (
                decision.reason.endswith("budget exhausted")
            ):
                self._emit(
                    make_event(
                        task_id=state.task_id,
                        event_type=EventType.RECOVERY_EXHAUSTED,
                        action="recovery_exhausted",
                        tool=tool_name,
                        failure={
                            "step_id": step.step_id,
                            "attempt": attempt,
                            "reason": decision.reason,
                        },
                    )
                )
            return self._finalize_step_failure(
                state=state,
                step_index=step_index,
                call=call,
                result=result,
                failure_type=decision.failure_type,
                recovery_action=decision.action.value,
            )

    def _handle_failed_attempt(
        self,
        *,
        state: ExecutionState,
        step_index: int,
        call: ToolCallRecord,
        result: ToolResult,
        tool: BaseTool | None,
        attempt: int,
    ) -> tuple[ExecutionState, Any]:
        step = state.plan[step_index]
        summary = observe_tool_result(call.tool_name, result)
        completed_at = utc_now()
        failure_type = classify_failure(result)
        logger.info(
            "[OBSERVE] failure step=%s type=%s message=%s",
            step.step_id,
            failure_type.value,
            result.error_message,
        )

        updated_call = call.model_copy(
            update={
                "status": ToolCallStatus.FAILED,
                "completed_at": completed_at,
                "result": result.model_dump(),
                "error_type": failure_type.value,
                "error_message": result.error_message,
            }
        )
        observation = Observation(
            source=call.tool_name,
            ok=False,
            summary=summary,
            data={**(result.data or {}), "attempt": attempt},
            error_type=failure_type.value,
            error_message=result.error_message,
        )
        # attempt 1 → 0 recovery retries used; attempt 2 → 1; etc.
        retries_used = max(0, attempt - 1)

        decision = evaluate_recovery(
            failure_type=failure_type,
            tool=tool,
            tool_args=dict(step.arguments),
            attempt=attempt,
            retries_used=retries_used,
            max_recovery_attempts=self.max_recovery_attempts,
        )

        failure = FailureRecord(
            task_id=state.task_id,
            step_id=step.step_id,
            failure_type=failure_type,
            message=result.error_message or summary,
            retryable=decision.action is RecoveryAction.RETRY,
            recovery_action=decision.action.value,
        )

        tool_calls = [
            updated_call if c.call_id == call.call_id else c for c in state.tool_calls
        ]
        state = state.model_copy(
            update={
                "tool_calls": tool_calls,
                "observations": [*state.observations, observation],
                "failures": [*state.failures, failure],
                "retry_count": state.retry_count
                + (1 if decision.action is RecoveryAction.RETRY else 0),
                "current_step": step_index,
            }
        ).touch()
        # Keep step RUNNING while recovery may continue.
        self._save(state)

        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TOOL_FAILED,
                action="tool_failed",
                tool=call.tool_name,
                result={**result.model_dump(), "attempt": attempt},
                failure={
                    "failure_type": failure_type.value,
                    "message": failure.message,
                    "attempt": attempt,
                },
                actor=Actor.AGENT,
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.FAILURE_DETECTED,
                action="failure_detected",
                tool=call.tool_name,
                failure={
                    "failure_type": failure_type.value,
                    "message": failure.message,
                    "step_id": step.step_id,
                    "attempt": attempt,
                },
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.RECOVERY_EVALUATED,
                action="recovery_evaluated",
                tool=call.tool_name,
                result={
                    "step_id": step.step_id,
                    "failure_type": decision.failure_type.value,
                    "action": decision.action.value,
                    "attempt": attempt,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                    "max_recovery_attempts": self.max_recovery_attempts,
                },
            )
        )
        return state, decision

    def _finalize_step_failure(
        self,
        *,
        state: ExecutionState,
        step_index: int,
        call: ToolCallRecord,
        result: ToolResult,
        failure_type: FailureType,
        recovery_action: str,
    ) -> ExecutionState:
        step = state.plan[step_index]
        summary = observe_tool_result(call.tool_name, result)
        plan = list(state.plan)
        plan[step_index] = step.model_copy(update={"status": PlanStepStatus.FAILED})
        state = state.model_copy(
            update={
                "plan": plan,
                "current_step": step_index,
                "final_status": TaskStatus.FAILED,
                "completion_summary": summary,
            }
        ).touch()
        self._save(state)

        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.STEP_FAILED,
                action="step_failed",
                tool=call.tool_name,
                observation={"summary": summary, "step_id": step.step_id},
                failure={
                    "failure_type": failure_type.value,
                    "message": result.error_message or summary,
                    "recovery_action": recovery_action,
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
                    "message": result.error_message or summary,
                    "step_id": step.step_id,
                },
            )
        )
        return state

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
                    data=(
                        item.get("data", item)
                        if isinstance(item.get("data", item), dict)
                        else {"value": item}
                    ),
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

        updates: dict[str, Any] = {
            "plan": plan,
            "tool_calls": tool_calls,
            "observations": [*state.observations, observation],
            "evidence": evidence,
            "completed_steps": completed_steps,
            "extracted_information": extracted,
            "current_step": step_index + 1,
        }
        if (
            state.pending_action is not None
            and state.pending_action.step_id == step.step_id
            and state.approval_status is ApprovalStatus.APPROVED
        ):
            updates["pending_action"] = None
            updates["approval_status"] = ApprovalStatus.NONE

        state = state.model_copy(update=updates).touch()
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
        state, decision = self._handle_failed_attempt(
            state=state,
            step_index=step_index,
            call=call,
            result=result,
            tool=None,
            attempt=1,
        )
        return self._finalize_step_failure(
            state=state,
            step_index=step_index,
            call=call,
            result=result,
            failure_type=decision.failure_type,
            recovery_action=decision.action.value,
        )

    def _complete_task(self, state: ExecutionState) -> ExecutionState:
        """Run independent verification before declaring COMPLETED."""
        return self._verify_and_finalize(state)

    def verify_task(self, task_id: str) -> ExecutionState:
        """Public entrypoint to verify a fully executed task against world state."""
        state = self.load(task_id)
        return self._verify_and_finalize(state)

    def _verify_and_finalize(self, state: ExecutionState) -> ExecutionState:
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.VERIFICATION_STARTED,
                action="verification_started",
                result={"completed_steps": state.completed_steps},
            )
        )
        self._save(state.touch())

        result = self.verifier.verify(state)
        for check in result.checks:
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.VERIFICATION_CHECK,
                    action="verification_check",
                    result=check.model_dump(mode="json"),
                )
            )

        evidence = list(state.evidence) + list(result.evidence)
        state = state.model_copy(
            update={
                "verification_status": result.status,
                "verification_summary": result.summary,
                "verification_checks": [
                    c.model_dump(mode="json") for c in result.checks
                ],
                "evidence": evidence,
            }
        ).touch()
        self._save(state)

        for item in result.evidence:
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.EVIDENCE_RECORDED,
                    action="evidence_recorded",
                    result={
                        "evidence_id": item.evidence_id,
                        "type": item.type,
                        "description": item.description,
                        "data": item.data,
                    },
                )
            )

        if result.passed:
            summary = (
                f"Completed {len(state.completed_steps)} plan step(s); "
                f"verification passed. {result.summary}"
            )
            state = state.model_copy(
                update={
                    "final_status": TaskStatus.COMPLETED,
                    "completion_summary": summary,
                    "verification_status": VerificationStatus.PASSED,
                }
            ).touch()
            self._save(state)
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.VERIFICATION_PASSED,
                    action="verification_passed",
                    verification_result=result.model_dump(mode="json"),
                    result={"summary": result.summary},
                )
            )
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.TASK_COMPLETED,
                    action="task_completed",
                    result={
                        "completed_steps": state.completed_steps,
                        "summary": summary,
                        "verification": "passed",
                    },
                )
            )
            return state

        failure = FailureRecord(
            task_id=state.task_id,
            failure_type=FailureType.VERIFICATION_FAILED,
            message=result.failure_reason or result.summary,
            retryable=False,
            recovery_action="fail",
        )
        state = state.model_copy(
            update={
                "final_status": TaskStatus.FAILED,
                "completion_summary": result.summary,
                "verification_status": VerificationStatus.FAILED,
                "failures": [*state.failures, failure],
            }
        ).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.VERIFICATION_FAILED,
                action="verification_failed",
                failure={
                    "failure_type": FailureType.VERIFICATION_FAILED.value,
                    "message": result.failure_reason or result.summary,
                },
                verification_result=result.model_dump(mode="json"),
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TASK_FAILED,
                action="task_failed",
                failure={
                    "failure_type": FailureType.VERIFICATION_FAILED.value,
                    "message": result.failure_reason or result.summary,
                },
            )
        )
        return state

    def _apply_approval_gate(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        tool_name: str,
        tool: BaseTool | None,
    ) -> ExecutionState:
        """Pause before sensitive tools unless this step was already approved."""
        decision = self.policy.evaluate(
            tool_name=tool_name,
            arguments=dict(step.arguments),
            tool=tool,
        )
        if not decision.requires_approval:
            return state

        # Human already approved this exact pending step — allow tool execution.
        # Keep pending_action until the step succeeds so a crash mid-resume
        # still remembers the approval and does not re-prompt or re-mutate twice.
        if (
            state.approval_status is ApprovalStatus.APPROVED
            and state.pending_action is not None
            and state.pending_action.step_id == step.step_id
            and state.pending_action.tool_name == tool_name
        ):
            return state.model_copy(update={"current_step": step_index}).touch()

        # Already waiting on this step.
        if (
            state.final_status is TaskStatus.NEEDS_APPROVAL
            and state.approval_status is ApprovalStatus.PENDING
            and state.pending_action is not None
            and state.pending_action.step_id == step.step_id
        ):
            return state

        key = idempotency_key_for(state.task_id, step.step_id)
        pending = PendingAction(
            tool_name=tool_name,
            arguments=dict(step.arguments),
            step_id=step.step_id,
            idempotency_key=key,
            reason=decision.reason,
            risk=decision.risk.value,
        )
        # Keep the plan step PENDING — sensitive tool must not run yet.
        plan = list(state.plan)
        plan[step_index] = step.model_copy(update={"status": PlanStepStatus.PENDING})
        state = state.model_copy(
            update={
                "plan": plan,
                "current_step": step_index,
                "pending_action": pending,
                "approval_status": ApprovalStatus.PENDING,
                "final_status": TaskStatus.NEEDS_APPROVAL,
                "completion_summary": decision.reason,
            }
        ).touch()
        self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.APPROVAL_REQUESTED,
                action="approval_requested",
                tool=tool_name,
                arguments=dict(step.arguments),
                result={
                    "step_id": step.step_id,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                    "operation": decision.operation,
                },
                actor=Actor.SYSTEM,
            )
        )
        logger.info(
            "[POLICY] approval required step=%s tool=%s reason=%s",
            step.step_id,
            tool_name,
            decision.reason,
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
