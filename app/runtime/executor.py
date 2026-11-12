"""Deterministic plan execution engine with bounded recovery."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.authz import (
    AuthorizationError,
    AuthorizationReason,
    AuthorizationScope,
    authorize_action,
    authorize_plan,
    ensure_task_authorization,
)
from app.config import database_inside_workspace, get_settings
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
from app.models.references import BindingError, contains_reference
from app.runtime.binding import prepare_arguments
from app.runtime.collections import sync_collection_progress
from app.runtime.recovery import (
    RecoveryAction,
    classify_failure,
    evaluate_recovery,
)
from app.runtime.replan import (
    enablement_for_runtime,
    propose_and_commit,
    task_scope,
)
from app.runtime.replan_triggers import recoverable_trigger
from app.runtime.transitions import (
    EventType,
    action_fingerprint,
    idempotency_key_for,
    make_event,
    observe_tool_result,
    pending_fingerprint,
    pending_matches,
)
from app.runtime.locking import LockBusyError, hold_task_lock
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

# How long a direct runtime caller waits for a busy task lock.
_EXECUTION_LOCK_TIMEOUT_SECONDS = 30.0


class ApprovalError(ValueError):
    """Raised for invalid approval/rejection operations."""


class StaleApprovalError(ApprovalError):
    """The approval does not match the exact action that would run."""


class TaskExecutionLockError(RuntimeError):
    """Another process or runtime holds the exclusive execution lock for this task."""


class ExecutionRuntime:
    """Executes a predefined plan against tools via the registry.

    Public execution and approval paths take the OS-backed per-task lock under
    ``lock_dir`` (default ``<database_parent>/locks``) so concurrent direct
    runtime callers cannot overspend authorization budgets. CLI/API callers that
    already hold the same lock must pass ``execution_locked=True`` to avoid
    deadlock (fcntl locks are not re-entrant across descriptors).
    """

    def __init__(
        self,
        store: SQLiteStore,
        registry: ToolRegistry,
        *,
        max_recovery_attempts: int | None = None,
        verifier: OutcomeVerifier | None = None,
        workspace_path: str | Path | None = None,
        policy: ApprovalPolicy | None = None,
        lock_dir: str | Path | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.policy = policy or ApprovalPolicy()
        self.max_recovery_attempts = (
            max_recovery_attempts
            if max_recovery_attempts is not None
            else get_settings().max_recovery_attempts
        )
        self.lock_dir = Path(lock_dir) if lock_dir is not None else Path(store.db_path).parent / "locks"
        if workspace_path is not None and database_inside_workspace(
            store.db_path, workspace_path
        ):
            raise ValueError(
                f"Runtime database {store.db_path} must be outside the workspace "
                f"{workspace_path}"
            )
        if verifier is not None:
            self.verifier = verifier
        else:
            ws = workspace_path or get_settings().workspace_path
            self.verifier = OutcomeVerifier(
                CompanyRepository(store.db_path),
                ws,
            )
        # Tests inject a scripted client. Production builds one only when a
        # supported failure is actually replanned.
        self._replan_llm: Any = None

    @contextmanager
    def _maybe_execution_lock(self, task_id: str, execution_locked: bool):
        """Acquire the task execution lock, or nest when already held by the caller."""
        if execution_locked:
            yield None
            return
        try:
            with hold_task_lock(
                self.lock_dir, task_id, timeout=_EXECUTION_LOCK_TIMEOUT_SECONDS
            ) as held:
                yield held
        except LockBusyError as exc:
            raise TaskExecutionLockError(
                f"Task {task_id} is locked by another execution; budgets were not consumed."
            ) from exc

    def create_task(
        self,
        *,
        user_goal: str,
        plan: list[PlanStep],
        authorization: AuthorizationScope,
        interpreted_goal: InterpretedGoal | None = None,
        success_criteria: list | None = None,
        task_id: str | None = None,
    ) -> ExecutionState:
        """Create and persist a queued ExecutionState with a predefined plan.

        ``authorization`` must come from a trusted origin (deterministic mock
        goal derivation or an independently supplied internal scope). The plan
        is checked against that scope before the task is persisted; an
        unauthorized plan never starts.
        """
        if not plan:
            raise ValueError("plan must contain at least one step")
        not_pending = [s for s in plan if s.status is not PlanStepStatus.PENDING]
        if not_pending:
            raise ValueError(
                "New tasks must start with all plan steps PENDING; got "
                + ", ".join(f"'{s.step_id}'={s.status.value}" for s in not_pending)
            )
        if authorization is None:
            raise AuthorizationError(
                AuthorizationReason.MISSING_AUTHORIZATION,
                "create_task requires an independently granted authorization scope.",
            )
        authorize_plan(plan, authorization)

        criteria = list(success_criteria or [])
        if not criteria and interpreted_goal is not None:
            criteria = list(interpreted_goal.success_criteria)

        fingerprint = authorization.fingerprint()
        # Round cap comes from validated settings, never from caller arguments.
        # Automatic replanning stays off unless AAE_REPLANNING_ENABLED is set.
        replan_limit = get_settings().aae_max_replan_rounds
        kwargs: dict[str, Any] = {
            "user_goal": user_goal,
            "interpreted_goal": interpreted_goal,
            "success_criteria": criteria,
            "plan": plan,
            "authorization": authorization,
            "authorization_fingerprint": fingerprint,
            "final_status": TaskStatus.QUEUED,
            "current_step": 0,
            "replan_count": 0,
            "replan_limit": replan_limit,
        }
        if task_id is not None:
            kwargs["task_id"] = task_id

        state = ExecutionState(**kwargs)
        state = self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TASK_CREATED,
                action="create_task",
                result={
                    "plan_steps": len(plan),
                    "user_goal": user_goal,
                    "replan_limit": replan_limit,
                },
            )
        )
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.AUTHORIZATION_BOUND,
                action="authorization_bound",
                result={
                    "fingerprint": fingerprint,
                    "allowed_operations": list(authorization.allowed_operations),
                    "budgets": authorization.budgets.model_dump(mode="json"),
                    "version": authorization.version,
                },
            )
        )
        return state

    def load(self, task_id: str) -> ExecutionState:
        state = self.store.get_state(task_id)
        if state is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        # Item progress is a projection of step/approval/verification state, so a
        # snapshot saved by an older build (or mid-flight) still reads correctly.
        return sync_collection_progress(state)

    def run(self, task_id: str, *, execution_locked: bool = False) -> ExecutionState:
        """Execute pending steps until complete, failed, aborted, or approval required.

        Takes the OS-backed per-task lock unless ``execution_locked`` is true
        (caller already holds the same lock via CLI/API or an outer runtime call).
        """
        with self._maybe_execution_lock(task_id, execution_locked):
            return self._run_locked(task_id)

    def _run_locked(self, task_id: str) -> ExecutionState:
        state = self.load(task_id)
        if state.final_status in _TERMINAL:
            return state
        if state.final_status is TaskStatus.NEEDS_APPROVAL:
            return state

        if state.final_status is TaskStatus.QUEUED:
            state = self._mark_running(state)

        while True:
            # Reload under the held lock so budget checks see durable tool_calls.
            state = self.load(task_id)
            if state.final_status in _TERMINAL:
                return state
            if state.final_status is TaskStatus.NEEDS_APPROVAL:
                return state
            advanced = self.execute_next(task_id, execution_locked=True)
            if advanced.final_status in _TERMINAL:
                return advanced
            if advanced.final_status is TaskStatus.NEEDS_APPROVAL:
                return advanced
            if advanced.current_step == state.current_step and all(
                s.status is not PlanStepStatus.PENDING for s in advanced.plan
            ):
                return advanced

    def execute_next(self, task_id: str, *, execution_locked: bool = False) -> ExecutionState:
        """Execute exactly one pending plan step (with bounded recovery), then return."""
        with self._maybe_execution_lock(task_id, execution_locked):
            return self._execute_next_locked(task_id)

    def _execute_next_locked(self, task_id: str) -> ExecutionState:
        state = self.load(task_id)
        if state.final_status in _TERMINAL:
            return state
        if state.final_status is TaskStatus.NEEDS_APPROVAL:
            return state

        if state.final_status is TaskStatus.QUEUED:
            state = self._mark_running(state)

        step_index = self._next_pending_index(state)
        if step_index is None:
            blocker = self._finalization_blocker(state)
            if blocker is not None:
                # Nothing left to run, yet the plan is not fully completed
                # (empty, FAILED/SKIPPED steps, stale approval): invalid stored state.
                return self._fail_invalid_state(state, blocker)
            return self._complete_task(state)

        return self._execute_step(state, step_index)

    def approve_task(
        self,
        task_id: str,
        *,
        approver: str | None = None,
        expected_fingerprint: str | None = None,
        execution_locked: bool = False,
    ) -> ExecutionState:
        """Approve the pending action and resume execution from that step.

        ``approver`` is recorded on the approval event when the caller knows who
        approved (for example an authenticated dashboard session).
        ``expected_fingerprint`` is the action the approver was shown; approval
        is refused without changes if the pending action is a different one.
        If the planned step no longer matches the pending action, the stale
        request is replaced by a fresh one and StaleApprovalError is raised.
        """
        with self._maybe_execution_lock(task_id, execution_locked):
            state = self.load(task_id)
            pending = self._require_pending(state, expected_fingerprint)

            located = self._pending_step(state)
            if located is not None:
                step_index, step = located
                tool_name = step.resolved_tool_name() or ""
                try:
                    arguments = prepare_arguments(state, step, self._tool(tool_name)).arguments
                except BindingError as exc:
                    raise ApprovalError(
                        f"The pending step's arguments cannot be resolved ({exc.message}); "
                        "nothing was approved."
                    ) from None
                try:
                    authorize_action(
                        state,
                        tool_name=tool_name,
                        arguments=arguments,
                        step_id=step.step_id,
                    )
                except AuthorizationError as exc:
                    self._authorize_or_fail(
                        state, step_index, step, tool_name, arguments, error=exc
                    )
                    raise ApprovalError(
                        f"Pending action is outside the task authorization scope "
                        f"({exc.reason.value}); nothing was approved."
                    ) from exc
                if not pending_matches(pending, tool_name, arguments):
                    decision = self.policy.evaluate(
                        tool_name=tool_name, arguments=arguments, tool=self._tool(tool_name)
                    )
                    self._request_approval(
                        state, step_index, step, tool_name, decision, arguments, stale=pending
                    )
                    raise StaleApprovalError(
                        "The pending action no longer matches the planned step; a new "
                        "approval was requested for the current action. Nothing was executed."
                    )

            pending = pending.model_copy(
                update={"resolved_at": utc_now(), "fingerprint": pending_fingerprint(pending)}
            )
            state = state.model_copy(
                update={
                    "approval_status": ApprovalStatus.APPROVED,
                    "pending_action": pending,
                    "final_status": TaskStatus.RUNNING,
                }
            ).touch()
            state = self._save(state)
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
                        "fingerprint": pending.fingerprint,
                        **({"approver": approver} if approver else {}),
                    },
                    actor=Actor.HUMAN,
                )
            )
            return self.run(task_id, execution_locked=True)

    def reject_task(
        self,
        task_id: str,
        reason: str = "Rejected by human",
        *,
        approver: str | None = None,
        expected_fingerprint: str | None = None,
        execution_locked: bool = False,
    ) -> ExecutionState:
        """Reject the pending action and terminate without executing the tool.

        Rejection never executes anything, so it is allowed for a stale pending
        action; ``expected_fingerprint`` only guards against rejecting an action
        other than the one the caller was shown.
        """
        with self._maybe_execution_lock(task_id, execution_locked):
            state = self.load(task_id)
            pending = self._require_pending(state, expected_fingerprint)
            pending = pending.model_copy(
                update={"resolved_at": utc_now(), "fingerprint": pending_fingerprint(pending)}
            )
            state = state.model_copy(
                update={
                    "approval_status": ApprovalStatus.REJECTED,
                    "pending_action": pending,
                    "final_status": TaskStatus.ABORTED,
                    "completion_summary": reason,
                }
            ).touch()
            state = self._save(state)
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
                        "fingerprint": pending.fingerprint,
                        **({"approver": approver} if approver else {}),
                    },
                    actor=Actor.HUMAN,
                )
            )
            return state

    @staticmethod
    def _require_pending(
        state: ExecutionState, expected_fingerprint: str | None
    ) -> PendingAction:
        if (
            state.final_status is not TaskStatus.NEEDS_APPROVAL
            or state.approval_status is not ApprovalStatus.PENDING
            or state.pending_action is None
        ):
            raise ApprovalError("no pending approval")
        pending = state.pending_action
        if expected_fingerprint is not None and expected_fingerprint != pending_fingerprint(
            pending
        ):
            raise StaleApprovalError(
                "The pending action is not the one that was reviewed; nothing was changed."
            )
        return pending

    @staticmethod
    def _pending_step(state: ExecutionState) -> tuple[int, PlanStep] | None:
        pending = state.pending_action
        if pending is None:
            return None
        for index, step in enumerate(state.plan):
            if step.step_id == pending.step_id:
                return index, step
        return None

    def _tool(self, tool_name: str) -> BaseTool | None:
        try:
            return self.registry.get(tool_name)
        except KeyError:
            return None

    def _approval_still_valid(
        self,
        state: ExecutionState,
        step: PlanStep,
        tool_name: str,
        tool: BaseTool | None,
        arguments: dict[str, Any],
    ) -> tuple[bool, Any]:
        """Re-check, immediately before a tool call, that a gated action is approved."""
        decision = self.policy.evaluate(tool_name=tool_name, arguments=arguments, tool=tool)
        if not decision.requires_approval:
            return True, decision
        pending = state.pending_action
        valid = (
            state.approval_status is ApprovalStatus.APPROVED
            and pending is not None
            and pending.step_id == step.step_id
            and pending_matches(pending, tool_name, arguments)
        )
        return valid, decision

    @staticmethod
    def _changed_action(
        state: ExecutionState, step: PlanStep, tool_name: str, arguments: dict[str, Any]
    ) -> str | None:
        """Why running ``arguments`` now would reuse the step's idempotency key for a
        different action than an earlier attempt, or None if it would not."""
        current = action_fingerprint(tool_name, arguments)
        for call in state.tool_calls:
            if call.step_id == step.step_id and action_fingerprint(
                call.tool_name, call.arguments
            ) != current:
                return (
                    f"Step '{step.step_id}' would now run a different action than its "
                    f"earlier attempt {call.attempt or '?'} ({call.call_id}); refusing to "
                    f"reuse idempotency key {idempotency_key_for(state.task_id, step.step_id)}"
                )
        return None

    def _authorize_or_fail(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        error: AuthorizationError | None = None,
    ) -> ExecutionState | None:
        """Authorize the resolved action; on denial, fail closed without invoking tools."""
        try:
            if error is None:
                ensure_task_authorization(state)
                authorize_action(
                    state,
                    tool_name=tool_name,
                    arguments=arguments,
                    step_id=step.step_id,
                )
            else:
                raise error
        except AuthorizationError as exc:
            message = f"{exc.reason.value}: {exc.message}"
            plan = list(state.plan)
            plan[step_index] = step.model_copy(update={"status": PlanStepStatus.FAILED})
            # Clear any pending approval so a scope violation cannot be continued.
            state = state.model_copy(
                update={
                    "plan": plan,
                    "current_step": step_index,
                    "final_status": TaskStatus.FAILED,
                    "completion_summary": message,
                    "approval_status": ApprovalStatus.NONE,
                    "pending_action": None,
                    "failures": [
                        *state.failures,
                        FailureRecord(
                            task_id=state.task_id,
                            step_id=step.step_id,
                            failure_type=FailureType.UNAUTHORIZED,
                            message=message,
                            retryable=False,
                            recovery_action=RecoveryAction.FAIL.value,
                        ),
                    ],
                }
            ).touch()
            state = self._save(state)
            failure = {
                "failure_type": FailureType.UNAUTHORIZED.value,
                "message": message,
                "step_id": step.step_id,
                **exc.audit_payload(),
            }
            for kind, action in (
                (EventType.AUTHORIZATION_DENIED, "authorization_denied"),
                (EventType.STEP_FAILED, "step_failed"),
                (EventType.TASK_FAILED, "task_failed"),
            ):
                self._emit(
                    make_event(
                        task_id=state.task_id,
                        event_type=kind,
                        action=action,
                        tool=tool_name,
                        arguments=dict(arguments),
                        failure=failure,
                    )
                )
            return state
        return None

    def _fail_before_call(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        tool_name: str | None,
        *,
        failure_type: FailureType,
        message: str,
        event_type: str,
    ) -> ExecutionState:
        """Fail a step whose tool must not be invoked (nothing is executed)."""
        plan = list(state.plan)
        plan[step_index] = step.model_copy(update={"status": PlanStepStatus.FAILED})
        state = state.model_copy(
            update={
                "plan": plan,
                "current_step": step_index,
                "final_status": TaskStatus.FAILED,
                "completion_summary": message,
                "failures": [
                    *state.failures,
                    FailureRecord(
                        task_id=state.task_id,
                        step_id=step.step_id,
                        failure_type=failure_type,
                        message=message,
                        retryable=False,
                        recovery_action=RecoveryAction.FAIL.value,
                    ),
                ],
            }
        ).touch()
        state = self._save(state)
        failure = {"failure_type": failure_type.value, "message": message, "step_id": step.step_id}
        for kind, action in (
            (event_type, event_type.replace(".", "_")),
            (EventType.STEP_FAILED, "step_failed"),
            (EventType.TASK_FAILED, "task_failed"),
        ):
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=kind,
                    action=action,
                    tool=tool_name,
                    failure=failure,
                )
            )
        return state

    def _mark_running(self, state: ExecutionState) -> ExecutionState:
        state = state.model_copy(update={"final_status": TaskStatus.RUNNING}).touch()
        state = self._save(state)
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
        if step.collection is not None:
            return self._expand_collection_and_continue(state, step_index, step)
        tool_name = step.resolved_tool_name()
        if not tool_name:
            return self._fail_missing_tool(state, step_index, step, None)

        try:
            tool = self.registry.get(tool_name)
        except KeyError:
            tool = None

        # References are resolved before authorization and the policy gate, so
        # both see the concrete action (never a bare $ref as an authorized value).
        try:
            prepared = prepare_arguments(state, step, tool)
        except BindingError as exc:
            return self._fail_before_call(
                state, step_index, step, tool_name,
                failure_type=exc.failure_type, message=exc.message,
                event_type=EventType.ARGUMENTS_UNRESOLVED,
            )

        denied = self._authorize_or_fail(
            state, step_index, step, tool_name, prepared.arguments
        )
        if denied is not None:
            return denied

        # Policy gate BEFORE any tool mutation or step RUNNING transition.
        # Authorization already passed; approval cannot expand scope.
        paused_or_ready = self._apply_approval_gate(
            state, step_index, step, tool_name, tool, prepared.arguments
        )
        if paused_or_ready.final_status is TaskStatus.NEEDS_APPROVAL:
            return paused_or_ready
        state = paused_or_ready

        if step.status is PlanStepStatus.RUNNING:
            state = self._mark_interrupted_calls(state, step, tool_name)

        # Attempts already made for this step in any process (interrupted ones
        # included), so a restart never resets the retry budget.
        prior_attempts = self._prior_attempts(state, step.step_id)
        if prior_attempts > 0:
            refused = self._refuse_resumed_attempt(
                state, step_index, step, tool_name, tool, prior_attempts
            )
            if refused is not None:
                return refused

        state = self._update_step(
            state,
            step_index,
            status=PlanStepStatus.RUNNING,
            current_step=step_index,
        )
        state = self._save(state)
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
        if prepared.references:
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.ARGUMENTS_RESOLVED,
                    action="arguments_resolved",
                    tool=tool_name,
                    arguments=dict(prepared.arguments),
                    result={
                        "step_id": step.step_id,
                        "references": prepared.references,
                        "fingerprint": action_fingerprint(tool_name, prepared.arguments),
                    },
                )
            )

        attempt = prior_attempts
        while True:
            # Re-resolved before every attempt (retries included) from the persisted
            # outputs of completed steps; unchanged outputs give identical arguments.
            try:
                prepared = prepare_arguments(state, step, tool)
            except BindingError as exc:
                return self._fail_before_call(
                    state, step_index, step, tool_name,
                    failure_type=exc.failure_type, message=exc.message,
                    event_type=EventType.ARGUMENTS_UNRESOLVED,
                )
            arguments = prepared.arguments
            # Re-authorize the exact resolved action on every attempt (retries
            # and resumed executions included) before approval or invocation.
            denied = self._authorize_or_fail(
                state, step_index, step, tool_name, arguments
            )
            if denied is not None:
                return denied
            # Immediately before each invocation: the approved action must still
            # be exactly the action about to run.
            approved, decision = self._approval_still_valid(
                state, step, tool_name, tool, arguments
            )
            if not approved:
                return self._request_approval(
                    state, step_index, step, tool_name, decision, arguments,
                    stale=state.pending_action,
                )
            changed = self._changed_action(state, step, tool_name, arguments)
            if changed:
                return self._fail_before_call(
                    state, step_index, step, tool_name,
                    failure_type=FailureType.ENVIRONMENT_UNEXPECTED, message=changed,
                    event_type=EventType.ACTION_CHANGED,
                )
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
                arguments=dict(arguments),
                status=ToolCallStatus.RUNNING,
                idempotency_key=key,
                attempt=attempt,
                references=prepared.references or None,
                approval_required=decision.requires_approval,
                approval_fingerprint=(
                    pending_fingerprint(state.pending_action)
                    if decision.requires_approval and state.pending_action is not None
                    else None
                ),
            )
            state = state.model_copy(
                update={"tool_calls": [*state.tool_calls, call]}
            ).touch()
            state = self._save(state)
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.TOOL_CALLED,
                    action="tool_called",
                    tool=tool_name,
                    arguments=dict(arguments),
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
                try:
                    result = tool.invoke(arguments, idempotency_key=key)
                except Exception as exc:
                    result = ToolResult.failure(
                        FailureType.ENVIRONMENT_UNEXPECTED.value,
                        f"Tool '{tool_name}' raised {type(exc).__name__}: {exc}",
                    )

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
            else:
                replanned = self._replan_failed_step(
                    state,
                    step_index,
                    step,
                    call,
                    result,
                    decision.failure_type,
                )
                if replanned is not None:
                    return replanned
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
        state = self._save(state)

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
        completion_summary: str | None = None,
    ) -> ExecutionState:
        step = state.plan[step_index]
        summary = completion_summary or observe_tool_result(call.tool_name, result)
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
        state = self._save(state)

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
        state = self._save(state)

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
            attempt=self._prior_attempts(state, step.step_id) + 1,
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
            attempt=call.attempt or 1,
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
        """Verify and finalize a fully executed RUNNING task.

        Queued, incomplete, approval-pending, invalid, and terminal tasks are
        returned unchanged: no status change, events, or evidence.
        """
        state = self.load(task_id)
        return self._verify_and_finalize(state)

    @staticmethod
    def _finalization_blocker(state: ExecutionState) -> str | None:
        """Why the task may not be finalized by verification, or None if it may."""
        if state.final_status is not TaskStatus.RUNNING:
            return f"task status is {state.final_status.value}, not running"
        if not state.plan:
            return "plan has no steps"
        if state.approval_status is ApprovalStatus.PENDING:
            return "an approval is pending"
        for step in state.plan:
            if step.status is not PlanStepStatus.COMPLETED:
                return f"step '{step.step_id}' is {step.status.value}, not completed"
        return None

    def _fail_invalid_state(self, state: ExecutionState, reason: str) -> ExecutionState:
        message = f"Invalid task state; cannot finalize: {reason}"
        failure = FailureRecord(
            task_id=state.task_id,
            failure_type=FailureType.ENVIRONMENT_UNEXPECTED,
            message=message,
            retryable=False,
            recovery_action="fail",
        )
        state = state.model_copy(
            update={
                "final_status": TaskStatus.FAILED,
                "completion_summary": message,
                "failures": [*state.failures, failure],
            }
        ).touch()
        state = self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.TASK_FAILED,
                action="task_failed",
                failure={
                    "failure_type": FailureType.ENVIRONMENT_UNEXPECTED.value,
                    "message": message,
                },
            )
        )
        logger.warning("[LIFECYCLE] task=%s failed: %s", state.task_id, message)
        return state

    def _verify_and_finalize(self, state: ExecutionState) -> ExecutionState:
        blocker = self._finalization_blocker(state)
        if blocker is not None:
            logger.info(
                "[LIFECYCLE] task=%s not finalized: %s", state.task_id, blocker
            )
            return state

        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.VERIFICATION_STARTED,
                action="verification_started",
                result={"completed_steps": state.completed_steps},
            )
        )
        state = self._save(state.touch())

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
        state = self._save(state)

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
            verified = result.status is VerificationStatus.PASSED
            outcome = "verification passed" if verified else "not independently verified"
            summary = (
                f"Completed {len(state.completed_steps)} plan step(s); "
                f"{outcome}. {result.summary}"
            )
            state = state.model_copy(
                update={
                    "final_status": TaskStatus.COMPLETED,
                    "completion_summary": summary,
                    "verification_status": result.status,
                }
            ).touch()
            state = self._save(state)
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=(
                        EventType.VERIFICATION_PASSED
                        if verified
                        else EventType.VERIFICATION_UNVERIFIED
                    ),
                    action="verification_passed" if verified else "verification_unverified",
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
                        "verification": result.status.value,
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
        state = self._save(state)
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
        arguments: dict[str, Any],
    ) -> ExecutionState:
        """Pause before sensitive tools unless this exact (resolved) action was approved."""
        decision = self.policy.evaluate(
            tool_name=tool_name,
            arguments=arguments,
            tool=tool,
        )
        if not decision.requires_approval:
            return state

        pending = state.pending_action
        if pending is not None and pending.step_id == step.step_id:
            # Human already approved this exact action — allow tool execution.
            # Keep pending_action until the step succeeds so a crash mid-resume
            # still remembers the approval and does not re-prompt or re-mutate twice.
            if state.approval_status is ApprovalStatus.APPROVED:
                if pending_matches(pending, tool_name, arguments):
                    return state.model_copy(update={"current_step": step_index}).touch()
                return self._request_approval(
                    state, step_index, step, tool_name, decision, arguments, stale=pending
                )
            # Already waiting on this step.
            if (
                state.final_status is TaskStatus.NEEDS_APPROVAL
                and state.approval_status is ApprovalStatus.PENDING
            ):
                if pending_matches(pending, tool_name, arguments):
                    return state
                return self._request_approval(
                    state, step_index, step, tool_name, decision, arguments, stale=pending
                )

        return self._request_approval(state, step_index, step, tool_name, decision, arguments)

    def _request_approval(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        tool_name: str,
        decision: Any,
        arguments: dict[str, Any],
        *,
        stale: PendingAction | None = None,
    ) -> ExecutionState:
        """Persist a pending approval for exactly this step's current (resolved) action.

        ``stale`` is a previous request or approval for the step that no longer
        matches its action; it is recorded as invalidated, never executed.
        """
        if contains_reference(arguments):
            raise ValueError("Refusing to request approval for unresolved references")
        arguments = dict(arguments)
        fingerprint = action_fingerprint(tool_name, arguments)
        if stale is not None:
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.APPROVAL_INVALIDATED,
                    action="approval_invalidated",
                    tool=tool_name,
                    arguments=arguments,
                    result={
                        "step_id": step.step_id,
                        "previous_status": state.approval_status.value,
                        "previous_fingerprint": pending_fingerprint(stale),
                        "current_fingerprint": fingerprint,
                        "reason": (
                            "The action changed after approval was requested or "
                            "granted; a fresh approval is required."
                        ),
                    },
                    actor=Actor.SYSTEM,
                )
            )
            logger.warning(
                "[POLICY] stale approval invalidated step=%s tool=%s", step.step_id, tool_name
            )

        key = idempotency_key_for(state.task_id, step.step_id)
        pending = PendingAction(
            tool_name=tool_name,
            arguments=arguments,
            step_id=step.step_id,
            idempotency_key=key,
            reason=decision.reason,
            risk=decision.risk.value,
            fingerprint=fingerprint,
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
        state = self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.APPROVAL_REQUESTED,
                action="approval_requested",
                tool=tool_name,
                arguments=arguments,
                result={
                    "step_id": step.step_id,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                    "operation": decision.operation,
                    "fingerprint": fingerprint,
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

    @staticmethod
    def _prior_attempts(state: ExecutionState, step_id: str) -> int:
        """Invocation attempts already recorded for a step, across all processes.

        Every persisted call for the step counts (failed, interrupted, or running),
        because an interrupted call may have reached the tool before a crash.
        Records persisted without ``attempt`` are numbered by their position in
        the step's ordered call history.
        """
        highest = 0
        position = 0
        for call in state.tool_calls:
            if call.step_id != step_id:
                continue
            position += 1
            highest = max(highest, call.attempt or position)
        return highest

    def _refuse_resumed_attempt(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        tool_name: str,
        tool: BaseTool | None,
        prior_attempts: int,
    ) -> ExecutionState | None:
        """Fail the step instead of invoking when another attempt is not allowed.

        ``max_recovery_attempts`` counts retries after the initial attempt, so a
        step may be invoked at most ``1 + max_recovery_attempts`` times over the
        task's lifetime. With ``max_recovery_attempts=0`` an interrupted initial
        invocation is not re-invoked, even for idempotent writes: the step fails
        and a write that may have committed is not reconciled automatically.
        """
        max_attempts = 1 + self.max_recovery_attempts
        if prior_attempts >= max_attempts:
            failure_type = FailureType.REPEATED
            message = (
                f"Retry budget exhausted for step '{step.step_id}': "
                f"{prior_attempts} of {max_attempts} allowed attempts already used "
                "(interrupted attempts count)"
            )
            self._emit(
                make_event(
                    task_id=state.task_id,
                    event_type=EventType.RECOVERY_EXHAUSTED,
                    action="recovery_exhausted",
                    tool=tool_name,
                    failure={
                        "step_id": step.step_id,
                        "attempt": prior_attempts,
                        "max_attempts": max_attempts,
                        "reason": message,
                    },
                )
            )
        elif tool is not None and not tool.is_retry_safe(step.arguments):
            failure_type = FailureType.ENVIRONMENT_UNEXPECTED
            message = (
                f"Step '{step.step_id}' has a previous attempt whose outcome is "
                f"unknown and '{tool_name}' is not retry-safe; it will not be "
                "invoked again"
            )
        else:
            return None

        logger.info("[RECOVERY] step=%s not re-invoked: %s", step.step_id, message)
        last_call = next(
            c for c in reversed(state.tool_calls) if c.step_id == step.step_id
        )
        state = state.model_copy(
            update={
                "failures": [
                    *state.failures,
                    FailureRecord(
                        task_id=state.task_id,
                        step_id=step.step_id,
                        failure_type=failure_type,
                        message=message,
                        retryable=False,
                        recovery_action=RecoveryAction.FAIL.value,
                    ),
                ]
            }
        ).touch()
        return self._finalize_step_failure(
            state=state,
            step_index=step_index,
            call=last_call,
            result=ToolResult.failure(failure_type.value, message),
            failure_type=failure_type,
            recovery_action=RecoveryAction.FAIL.value,
        )

    def _mark_interrupted_calls(
        self, state: ExecutionState, step: PlanStep, tool_name: str
    ) -> ExecutionState:
        """A step left RUNNING means the process stopped mid-call; its outcome is unknown.

        Dangling calls are recorded as failed (never as succeeded). The step is then
        re-invoked with the same idempotency key, so a write that did commit is
        replayed by the tool instead of being applied twice.
        """
        interrupted: list[str] = []
        tool_calls = []
        for call in state.tool_calls:
            if call.step_id == step.step_id and call.status is ToolCallStatus.RUNNING:
                interrupted.append(call.call_id)
                call = call.model_copy(
                    update={
                        "status": ToolCallStatus.FAILED,
                        "completed_at": utc_now(),
                        "error_type": "interrupted",
                        "error_message": (
                            "Execution stopped before the call completed; "
                            "re-invoking with the same idempotency key"
                        ),
                    }
                )
            tool_calls.append(call)
        state = state.model_copy(update={"tool_calls": tool_calls}).touch()
        state = self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.STEP_RESUMED,
                action="step_resumed",
                tool=tool_name,
                result={
                    "step_id": step.step_id,
                    "interrupted_call_ids": interrupted,
                    "idempotency_key": idempotency_key_for(state.task_id, step.step_id),
                },
            )
        )
        logger.info(
            "[RESUME] step=%s interrupted_calls=%s", step.step_id, len(interrupted)
        )
        return state

    def _expand_collection_and_continue(
        self,
        state: ExecutionState,
        step_index: int,
        template: PlanStep,
    ) -> ExecutionState:
        """Seal a collection snapshot, expand the template, re-authorize, continue.

        The snapshot is saved before the template is replaced, so a restart in
        between resumes from the sealed snapshot and does not observe twice.
        The expanded plan is authorized before that replacement is saved.
        """
        from app.models.collections import CollectionExecutionState
        from app.runtime.collections import (
            CollectionError,
            build_ticket_collection_snapshot,
            expand_collection_step,
            progress_records,
            select_collection_items,
        )

        work = template.collection
        assert work is not None
        try:
            snapshots = dict(state.collections.snapshots or {})
            snapshot = snapshots.get(work.from_step)
            if snapshot is None:
                snapshot = build_ticket_collection_snapshot(
                    state,
                    source_step_id=work.from_step,
                    max_items=work.max_items,
                    item_id_field=work.item_id_field,
                    items_path=list(work.items_path),
                )
                snapshots[work.from_step] = snapshot
                state = state.model_copy(
                    update={
                        "collections": state.collections.model_copy(
                            update={"snapshots": snapshots}
                        )
                    }
                ).touch()
                state = self._save(state)
                self._emit(
                    make_event(
                        task_id=state.task_id,
                        event_type=EventType.COLLECTION_OBSERVED,
                        action="collection_observed",
                        result={
                            "source_step_id": work.from_step,
                            "collection_id": snapshot.collection_id,
                            "count": len(snapshot.items),
                            "truncated": snapshot.truncated,
                            "empty": snapshot.empty,
                        },
                    )
                )

            selected = select_collection_items(
                snapshot, work.select, max_items=work.max_items
            )
            expanded = expand_collection_step(template, work, selected)
        except CollectionError as exc:
            return self._fail_before_call(
                state,
                step_index,
                template,
                template.resolved_tool_name() or "",
                failure_type=FailureType.ENVIRONMENT_UNEXPECTED,
                message=str(exc),
                event_type=EventType.STEP_FAILED,
            )

        if not expanded:
            # Empty selection: skip the template without side effects.
            plan = list(state.plan)
            plan[step_index] = template.model_copy(
                update={"status": PlanStepStatus.SKIPPED, "collection": None}
            )
            state = state.model_copy(
                update={"plan": plan, "current_step": step_index + 1}
            ).touch()
            state = self._save(state)
            return self._execute_next_locked(state.task_id)

        # Authorize the replacement before it becomes the durable plan.
        plan = list(state.plan)
        new_plan = [*plan[:step_index], *expanded, *plan[step_index + 1 :]]
        try:
            scope = ensure_task_authorization(state)
            authorize_plan(new_plan, scope)
        except AuthorizationError as exc:
            return self._fail_before_call(
                state,
                step_index,
                template,
                template.resolved_tool_name() or "",
                failure_type=FailureType.UNAUTHORIZED,
                message=f"Expanded collection plan unauthorized: {exc.reason.value}",
                event_type=EventType.AUTHORIZATION_DENIED,
            )

        expanded_from = dict(state.collections.expanded_from or {})
        for child in expanded:
            expanded_from[child.step_id] = template.step_id
        progress = list(state.collections.progress or [])
        progress.extend(progress_records(snapshot.collection_id, expanded, selected))
        collections = CollectionExecutionState(
            snapshots=dict(state.collections.snapshots),
            progress=progress,
            expanded_from=expanded_from,
        )
        state = state.model_copy(
            update={
                "plan": new_plan,
                "collections": collections,
                "current_step": step_index,
            }
        ).touch()

        state = self._save(state)
        self._emit(
            make_event(
                task_id=state.task_id,
                event_type=EventType.COLLECTION_EXPANDED,
                action="collection_expanded",
                result={
                    "template_step_id": template.step_id,
                    "collection_id": snapshot.collection_id,
                    "item_ids": [item.item_id for item in selected],
                    "expanded_step_ids": [step.step_id for step in expanded],
                },
            )
        )
        return self._execute_next_locked(state.task_id)

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

    def _replan_failed_step(
        self,
        state: ExecutionState,
        step_index: int,
        step: PlanStep,
        call: ToolCallRecord,
        result: ToolResult,
        failure_type: FailureType,
    ) -> ExecutionState | None:
        """Commit one bounded revision for a supported business conflict.

        Returns None when replanning does not apply, so the caller fails the
        step exactly as before. An accepted revision resumes under the lock
        already held by this execution. A rejected revision fails the task
        with the rejection reason and does not run the proposed steps.
        """
        enablement = enablement_for_runtime()
        if not enablement.enabled:
            return None
        if state.approval_status is ApprovalStatus.REJECTED:
            return None
        trigger = recoverable_trigger(result, failure_type)
        if trigger is None:
            return None
        fresh = self.load(state.task_id)
        scope = task_scope(fresh)
        if scope is None:
            return self._finalize_step_failure(
                state=fresh,
                step_index=step_index,
                call=call,
                result=result,
                failure_type=failure_type,
                recovery_action=RecoveryAction.FAIL.value,
                completion_summary="Replan refused: missing authorization",
            )
        from app.runtime.replan import Replanner

        replanner = Replanner(
            self._replan_client(),
            self.registry,
            max_tokens=get_settings().llm_max_tokens,
        )
        outcome, revised = asyncio.run(
            propose_and_commit(
                self.store,
                replanner,
                fresh,
                scope,
                trigger=trigger,
                failure_summary=(result.error_message or "")[:500],
                enablement=enablement,
            )
        )
        if outcome.accepted:
            return self._execute_next_locked(revised.task_id)
        reloaded = self.load(revised.task_id)
        index = next(
            (i for i, item in enumerate(reloaded.plan) if item.step_id == step.step_id),
            step_index,
        )
        return self._finalize_step_failure(
            state=reloaded,
            step_index=index,
            call=call,
            result=result,
            failure_type=failure_type,
            recovery_action=RecoveryAction.FAIL.value,
            completion_summary=f"Replan {outcome.reason}",
        )

    def _replan_client(self) -> Any:
        if self._replan_llm is not None:
            return self._replan_llm
        from app.llm.factory import create_llm_client

        self._replan_llm = create_llm_client(get_settings())
        return self._replan_llm

    def _save(self, state: ExecutionState) -> ExecutionState:
        state = sync_collection_progress(state)
        self.store.save_state(state)
        return state

    def _emit(self, event: Any) -> None:
        self.store.append_event(event)
