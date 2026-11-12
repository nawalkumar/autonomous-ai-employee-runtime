"""Thin service layer between HTTP handlers and the existing execution runtime.

Every operation builds its runtime with ``app.cli.build_runtime`` (the same
assembly the CLI uses), so the API and CLI share the planner, approval policy,
executor, verifier, SQLite store and per-task OS locks. All functions here are
synchronous and must be called from a worker thread, never the event loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.api.errors import ApiError
from app.api.redaction import Redactor
from app.api.schemas import (
    CriterionOut,
    CustomerOut,
    EmployeeOut,
    EventOut,
    EvidenceOut,
    FailureOut,
    HistoryResponse,
    InterpretedGoalOut,
    PendingActionOut,
    PlanStepOut,
    TaskDetail,
    TaskListResponse,
    TaskSummary,
    TicketOut,
    ToolCallOut,
    VerificationCheckOut,
    VerificationResponse,
    WorkspaceFileContent,
    WorkspaceFileOut,
)
from app.authz import (
    AuthorizationError,
    authorization_for_supported_goal,
)
from app.cli import CliError, CliRuntime, build_runtime
from app.config import Settings
from app.models.enums import (
    Actor,
    ApprovalStatus,
    FailureType,
    PlanStepStatus,
    TaskStatus,
)
from app.models.ids import new_id
from app.models.records import PendingAction
from app.models.state import ExecutionState
from app.planning.pipeline import PlanningResult
from app.runtime import ApprovalError, StaleApprovalError, pending_fingerprint
from app.runtime.locking import LockBusyError, TaskLock, hold_task_lock
from app.runtime.transitions import make_event

logger = logging.getLogger("app.api")

INJECTED_FAILURE_MESSAGE = "503 Service Unavailable (simulated by dashboard demo control)"
DEMO_FAILURE_INJECTED = "demo.failure_injected"
TASK_LOCK_WAIT_SECONDS = 2.0
SUBMISSION_LOCK = "api-submissions"
SUBMISSION_LOCK_WAIT_SECONDS = 5.0
MAX_WORKSPACE_FILE_BYTES = 64 * 1024
MAX_HISTORY_EVENTS = 2000
WORKSPACE_PATH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9_-][A-Za-z0-9._-]{0,99}){0,7}")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{8,128}")

_TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED}


@dataclass(frozen=True)
class Tenant:
    """Whose data a request may touch: the local install, or one demo session."""

    tenant_id: str
    settings: Settings
    approver: str
    task_quota: int | None = None


# --- Background execution -------------------------------------------------------


class JobRunner:
    """Bounded thread pool for task execution; tracks which tasks are executing."""

    def __init__(self, max_workers: int, max_pending: int) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="aae-task")
        self._slots = threading.BoundedSemaphore(max_pending)
        self._active: set[tuple[str, str]] = set()
        self._guard = threading.Lock()

    def reserve(self) -> bool:
        return self._slots.acquire(blocking=False)

    def cancel_reservation(self) -> None:
        self._slots.release()

    def start(self, key: tuple[str, str], fn: Callable[[], None]) -> None:
        """Run ``fn`` on a reserved slot."""
        with self._guard:
            self._active.add(key)
        try:
            self._pool.submit(self._run, key, fn)
        except RuntimeError:
            self._finish(key)
            raise

    def _run(self, key: tuple[str, str], fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 - logged; task state stays as persisted
            logger.exception("Background job for task %s raised", key[1])
        finally:
            self._finish(key)

    def _finish(self, key: tuple[str, str]) -> None:
        with self._guard:
            self._active.discard(key)
        self._slots.release()

    def is_active(self, key: tuple[str, str]) -> bool:
        with self._guard:
            return key in self._active

    def tenant_busy(self, tenant_id: str) -> bool:
        with self._guard:
            return any(key[0] == tenant_id for key in self._active)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)


# --- Duplicate-submission protection ---------------------------------------------

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_task_submissions (
    idempotency_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class SubmissionLedger:
    """Maps an Idempotency-Key to the task it created (same SQLite file)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript(_LEDGER_SCHEMA)

    def lookup(self, key: str) -> tuple[str, str] | None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT request_hash, task_id FROM api_task_submissions "
                "WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def record(self, key: str, request_hash: str, task_id: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO api_task_submissions "
                "(idempotency_key, request_hash, task_id, created_at) VALUES (?, ?, ?, ?)",
                (key, request_hash, task_id, datetime.now(timezone.utc).isoformat()),
            )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Service --------------------------------------------------------------------


class TaskService:
    def __init__(self, *, jobs: JobRunner, redactor: Redactor, allow_failure_injection: bool):
        self.jobs = jobs
        self.redact = redactor
        self.allow_failure_injection = allow_failure_injection

    # Runtime assembly and locking

    def open(self, tenant: Tenant) -> CliRuntime:
        try:
            return build_runtime(tenant.settings)
        except CliError as exc:
            logger.error("Runtime storage unavailable (tenant %s): %s", tenant.tenant_id, exc)
            raise ApiError(
                503, "storage_unavailable", "Runtime storage is unavailable."
            ) from None

    @contextmanager
    def _locked(self, rt: CliRuntime, name: str, timeout: float, busy: ApiError) -> Iterator[None]:
        try:
            with hold_task_lock(rt.lock_dir, name, timeout=timeout):
                yield
        except LockBusyError:
            raise busy from None

    def _task_lock(self, rt: CliRuntime, task_id: str) -> Any:
        return self._locked(
            rt,
            task_id,
            TASK_LOCK_WAIT_SECONDS,
            ApiError(
                409,
                "task_locked",
                "The task is being executed or modified by another request or CLI "
                "process; nothing was changed. Retry shortly.",
                headers={"Retry-After": "2"},
            ),
        )

    @staticmethod
    def _load(rt: CliRuntime, task_id: str) -> ExecutionState:
        try:
            return rt.runtime.load(task_id)
        except KeyError:
            raise ApiError(404, "task_not_found", f"Unknown task: {task_id}") from None

    # Submission

    def submit(
        self,
        tenant: Tenant,
        *,
        goal: str,
        planned: PlanningResult,
        inject_failure: str | None,
        idempotency_key: str | None,
    ) -> TaskDetail:
        if inject_failure is not None:
            if not self.allow_failure_injection:
                raise ApiError(403, "injection_disabled", "Failure injection is disabled.")
            if not any(
                step.arguments.get("operation") == inject_failure for step in planned.plan.steps
            ):
                raise ApiError(
                    422,
                    "injection_not_applicable",
                    f"The plan for this goal has no {inject_failure} step to inject into.",
                )

        rt = self.open(tenant)
        request_hash = _sha256(json.dumps({"goal": goal, "inject": inject_failure}))
        busy = ApiError(
            503, "busy", "Another submission is in progress; retry shortly.",
            headers={"Retry-After": "2"},
        )
        with self._locked(rt, SUBMISSION_LOCK, SUBMISSION_LOCK_WAIT_SECONDS, busy):
            ledger = SubmissionLedger(rt.database_path)
            if idempotency_key:
                prior = ledger.lookup(idempotency_key)
                if prior is not None:
                    prior_hash, prior_task = prior
                    if prior_hash != request_hash:
                        raise ApiError(
                            409,
                            "idempotency_conflict",
                            "This Idempotency-Key was already used for a different request.",
                        )
                    state = self._load(rt, prior_task)
                    return self.detail(tenant, state, idempotent_replay=True)

            if tenant.task_quota is not None and rt.store.count_tasks() >= tenant.task_quota:
                raise ApiError(
                    429,
                    "task_quota_exceeded",
                    f"This demo session reached its limit of {tenant.task_quota} tasks. "
                    "Start a new session to continue.",
                )
            if not self.jobs.reserve():
                raise ApiError(
                    503,
                    "busy",
                    "Too many tasks are executing; nothing was created. Retry shortly.",
                    headers={"Retry-After": "5"},
                )

            try:
                authorization = authorization_for_supported_goal(goal)
            except AuthorizationError as exc:
                raise ApiError(
                    422,
                    "goal_rejected",
                    f"Goal rejected; no task was created. {exc}",
                ) from None

            task_id = new_id("task_")
            lock = TaskLock(rt.lock_dir, task_id)
            try:
                lock.acquire()
                try:
                    state = rt.runtime.create_task(
                        task_id=task_id,
                        user_goal=goal,
                        interpreted_goal=planned.interpreted_goal,
                        plan=planned.plan.steps,
                        authorization=authorization,
                    )
                except AuthorizationError as exc:
                    raise ApiError(
                        422,
                        "plan_unauthorized",
                        f"Plan unauthorized; no task was created. {exc}",
                    ) from None
                if inject_failure is not None:
                    rt.store.append_event(
                        make_event(
                            task_id=task_id,
                            event_type=DEMO_FAILURE_INJECTED,
                            action="demo_failure_injected",
                            tool="company_api",
                            result={
                                "operation": inject_failure,
                                "failure_type": FailureType.TRANSIENT.value,
                                "message": INJECTED_FAILURE_MESSAGE,
                                "simulated": True,
                                "requested_by": tenant.approver,
                            },
                            actor=Actor.HUMAN,
                        )
                    )
                if idempotency_key:
                    ledger.record(idempotency_key, request_hash, task_id)
            except BaseException:
                lock.release()
                self.jobs.cancel_reservation()
                raise

            self.jobs.start(
                (tenant.tenant_id, task_id),
                lambda: self._execute(rt, task_id, lock, inject_failure),
            )
        return self.detail(tenant, state, executing=True)

    @staticmethod
    def _execute(rt: CliRuntime, task_id: str, lock: TaskLock, inject: str | None) -> None:
        try:
            if inject is not None:
                rt.injector.inject_once(
                    f"company_api.{inject}",
                    FailureType.TRANSIENT.value,
                    INJECTED_FAILURE_MESSAGE,
                )
            # OS lock is already held on ``lock``; skip re-acquire (cross-thread).
            rt.runtime.run(task_id, execution_locked=True)
        finally:
            lock.release()

    # Human approval and resume

    @staticmethod
    def _pending(state: ExecutionState) -> PendingAction:
        if (
            state.final_status is not TaskStatus.NEEDS_APPROVAL
            or state.approval_status is not ApprovalStatus.PENDING
            or state.pending_action is None
        ):
            raise ApiError(
                409,
                "no_pending_approval",
                f"Task {state.task_id} has no pending approval "
                f"(status {state.final_status.value}); nothing was changed.",
            )
        return state.pending_action

    def _check_step(self, state: ExecutionState, step_id: str) -> None:
        pending = self._pending(state)
        if pending.step_id != step_id:
            raise ApiError(
                409,
                "step_mismatch",
                f"The pending step is {pending.step_id!r}, not {step_id!r}; "
                "nothing was changed.",
            )

    def approve(
        self, tenant: Tenant, task_id: str, step_id: str, fingerprint: str | None = None
    ) -> TaskDetail:
        rt = self.open(tenant)
        self._load(rt, task_id)
        with self._task_lock(rt, task_id):
            self._check_step(self._load(rt, task_id), step_id)
            try:
                state = rt.runtime.approve_task(
                    task_id,
                    approver=tenant.approver,
                    expected_fingerprint=fingerprint,
                    execution_locked=True,
                )
            except StaleApprovalError as exc:
                raise ApiError(409, "stale_approval", str(exc)) from None
            except ApprovalError as exc:
                if isinstance(exc.__cause__, AuthorizationError):
                    raise ApiError(409, "unauthorized", str(exc)) from None
                raise ApiError(409, "no_pending_approval", "No pending approval.") from None
        return self.detail(tenant, state)

    def reject(
        self,
        tenant: Tenant,
        task_id: str,
        step_id: str,
        reason: str,
        fingerprint: str | None = None,
    ) -> TaskDetail:
        rt = self.open(tenant)
        self._load(rt, task_id)
        with self._task_lock(rt, task_id):
            self._check_step(self._load(rt, task_id), step_id)
            try:
                state = rt.runtime.reject_task(
                    task_id,
                    reason=reason,
                    approver=tenant.approver,
                    expected_fingerprint=fingerprint,
                    execution_locked=True,
                )
            except StaleApprovalError as exc:
                raise ApiError(409, "stale_approval", str(exc)) from None
            except ApprovalError:
                raise ApiError(409, "no_pending_approval", "No pending approval.") from None
        return self.detail(tenant, state)

    def resume(self, tenant: Tenant, task_id: str) -> TaskDetail:
        rt = self.open(tenant)
        self._load(rt, task_id)
        with self._task_lock(rt, task_id):
            state = self._load(rt, task_id)
            if state.final_status is TaskStatus.NEEDS_APPROVAL:
                raise ApiError(
                    409, "awaiting_approval", "The task is awaiting approval; approve or reject it."
                )
            if state.final_status in _TERMINAL:
                raise ApiError(
                    409,
                    "task_terminal",
                    f"The task already ended with status {state.final_status.value}; "
                    "nothing was executed.",
                )
            state = rt.runtime.run(task_id, execution_locked=True)
        return self.detail(tenant, state)

    # Read-only views

    def list_tasks(self, tenant: Tenant, *, limit: int, offset: int) -> TaskListResponse:
        rt = self.open(tenant)
        rows = rt.store.list_tasks(limit=limit, offset=offset)
        summaries = []
        for row in rows:
            state = row.state
            summaries.append(
                TaskSummary(
                    task_id=row.task_id,
                    status=row.status,
                    user_goal=self.redact.text(state.user_goal) if state else None,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    approval_status=state.approval_status.value if state else None,
                    verification_status=state.verification_status.value if state else None,
                    retry_count=state.retry_count if state else None,
                    steps_completed=(
                        sum(1 for s in state.plan if s.status is PlanStepStatus.COMPLETED)
                        if state
                        else None
                    ),
                    steps_total=len(state.plan) if state else None,
                    readable=state is not None,
                )
            )
        return TaskListResponse(
            tasks=summaries, total=rt.store.count_tasks(), limit=limit, offset=offset
        )

    def get(self, tenant: Tenant, task_id: str) -> TaskDetail:
        return self.detail(tenant, self._load(self.open(tenant), task_id))

    def history(self, tenant: Tenant, task_id: str) -> HistoryResponse:
        rt = self.open(tenant)
        self._load(rt, task_id)
        events = rt.store.get_events(task_id)[:MAX_HISTORY_EVENTS]
        return HistoryResponse(
            task_id=task_id,
            count=len(events),
            events=[
                EventOut(
                    event_id=e.event_id,
                    timestamp=e.timestamp,
                    event_type=e.event_type,
                    action=e.action,
                    tool=e.tool,
                    actor=e.actor.value,
                    arguments=self.redact(e.arguments),
                    result=self.redact(e.result),
                    observation=self.redact(e.observation),
                    failure=self.redact(e.failure),
                    verification_result=self.redact(e.verification_result),
                )
                for e in events
            ],
        )

    def verification(self, tenant: Tenant, task_id: str) -> VerificationResponse:
        state = self._load(self.open(tenant), task_id)
        checks = [VerificationCheckOut(**self.redact(c)) for c in state.verification_checks]
        return VerificationResponse(
            task_id=task_id,
            task_status=state.final_status.value,
            status=state.verification_status.value,
            summary=state.verification_summary,
            checks_passed=sum(1 for c in checks if c.passed),
            checks_total=len(checks),
            checks=checks,
            failure_reasons=[
                f.message
                for f in state.failures
                if f.failure_type is FailureType.VERIFICATION_FAILED
            ],
            evidence=[
                EvidenceOut(
                    evidence_id=e.evidence_id,
                    type=e.type,
                    description=self.redact.text(e.description),
                    data=self.redact(e.data),
                    timestamp=e.timestamp,
                )
                for e in state.evidence
            ],
        )

    def detail(
        self,
        tenant: Tenant,
        state: ExecutionState,
        *,
        executing: bool | None = None,
        idempotent_replay: bool = False,
    ) -> TaskDetail:
        if executing is None:
            executing = self.jobs.is_active((tenant.tenant_id, state.task_id))
        attempts: dict[str, int] = {}
        for call in state.tool_calls:
            if call.step_id:
                attempts[call.step_id] = attempts.get(call.step_id, 0) + 1
        goal = state.interpreted_goal
        pending = state.pending_action
        checks = state.verification_checks
        terminal = state.final_status in _TERMINAL
        return TaskDetail(
            task_id=state.task_id,
            status=state.final_status.value,
            user_goal=self.redact.text(state.user_goal),
            created_at=state.created_at,
            updated_at=state.updated_at,
            interpreted_goal=(
                InterpretedGoalOut(
                    objective=goal.objective,
                    entities=self.redact(goal.entities),
                    constraints=goal.constraints,
                    required_information=goal.required_information,
                    success_criteria=[
                        CriterionOut(
                            description=c.description,
                            criterion_type=c.criterion_type,
                            target=c.target,
                            expected_value=self.redact(c.expected_value),
                        )
                        for c in goal.success_criteria
                    ],
                )
                if goal is not None
                else None
            ),
            plan=[
                PlanStepOut(
                    step_id=s.step_id,
                    description=s.description,
                    tool=s.resolved_tool_name(),
                    operation=s.arguments.get("operation"),
                    arguments=self.redact(s.arguments),
                    expected_outcome=s.expected_outcome,
                    status=s.status.value,
                    attempts=attempts.get(s.step_id, 0),
                )
                for s in state.plan
            ],
            current_step=state.current_step,
            tool_calls=[
                ToolCallOut(
                    call_id=c.call_id,
                    step_id=c.step_id,
                    tool=c.tool_name,
                    operation=(c.arguments or {}).get("operation"),
                    arguments=self.redact(c.arguments),
                    status=c.status.value,
                    attempt=c.attempt,
                    started_at=c.started_at,
                    completed_at=c.completed_at,
                    error_type=c.error_type,
                    error_message=self.redact.text(c.error_message) if c.error_message else None,
                )
                for c in state.tool_calls
            ],
            retry_count=state.retry_count,
            failures=[
                FailureOut(
                    failure_id=f.failure_id,
                    step_id=f.step_id,
                    failure_type=f.failure_type.value,
                    message=self.redact.text(f.message),
                    retryable=f.retryable,
                    recovery_action=f.recovery_action,
                    timestamp=f.timestamp,
                )
                for f in state.failures
            ],
            approval_status=state.approval_status.value,
            pending_action=(
                PendingActionOut(
                    step_id=pending.step_id,
                    tool=pending.tool_name,
                    operation=pending.arguments.get("operation"),
                    arguments=self.redact(pending.arguments),
                    risk=pending.risk,
                    reason=pending.reason,
                    fingerprint=pending_fingerprint(pending),
                    created_at=pending.created_at,
                )
                if pending is not None and state.approval_status is ApprovalStatus.PENDING
                else None
            ),
            verification_status=state.verification_status.value,
            verification_summary=state.verification_summary,
            verification_checks_passed=sum(1 for c in checks if c.get("passed")),
            verification_checks_total=len(checks),
            evidence_count=len(state.evidence),
            completion_summary=(
                self.redact.text(state.completion_summary) if state.completion_summary else None
            ),
            terminal=terminal,
            executing=executing,
            resumable=(
                state.final_status in (TaskStatus.QUEUED, TaskStatus.RUNNING) and not executing
            ),
            idempotent_replay=idempotent_replay,
        )

    # Mock company and workspace (read-only)

    def employees(self, tenant: Tenant) -> list[EmployeeOut]:
        repo = self.open(tenant).repository
        return [EmployeeOut(**e.model_dump()) for e in repo.list_employees()]

    def customers(self, tenant: Tenant) -> list[CustomerOut]:
        repo = self.open(tenant).repository
        return [CustomerOut(**c.model_dump()) for c in repo.list_customers()]

    def tickets(self, tenant: Tenant) -> list[TicketOut]:
        repo = self.open(tenant).repository
        return [TicketOut(**t.model_dump()) for t in repo.list_tickets()]

    def _listed_files(self, rt: CliRuntime) -> dict[str, Path]:
        result = rt.registry.get("file").invoke({"operation": "list", "path": ""})
        if not result.ok:
            return {}
        listed: dict[str, Path] = {}
        for entry in (result.data or {}).get("files", []):
            rel = Path(entry).as_posix()
            listed[rel] = rt.workspace_path / Path(rel)
        return listed

    def workspace_files(self, tenant: Tenant) -> list[WorkspaceFileOut]:
        rt = self.open(tenant)
        files = []
        for rel, path in sorted(self._listed_files(rt).items()):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append(WorkspaceFileOut(path=rel, size_bytes=size))
        return files

    def workspace_file(self, tenant: Tenant, path: str) -> WorkspaceFileContent:
        if not WORKSPACE_PATH.fullmatch(path) or ".." in path.split("/"):
            raise ApiError(
                400,
                "invalid_path",
                "Only plain relative workspace paths are accepted "
                "(letters, digits, '.', '_', '-' and '/').",
            )
        rt = self.open(tenant)
        listed = self._listed_files(rt)
        if path not in listed:
            raise ApiError(404, "file_not_found", "No such file in the workspace.")
        size = listed[path].stat().st_size
        if size > MAX_WORKSPACE_FILE_BYTES:
            raise ApiError(413, "file_too_large", "The file is too large to display.")
        try:
            result = rt.registry.get("file").invoke({"operation": "read", "path": path})
        except UnicodeDecodeError:
            raise ApiError(415, "not_text", "The file is not UTF-8 text.") from None
        if not result.ok:
            if result.error_type == FailureType.POLICY_BLOCKED.value:
                raise ApiError(403, "path_not_allowed", "Access to this path is not allowed.")
            raise ApiError(404, "file_not_found", "No such file in the workspace.")
        content = (result.data or {}).get("content", "")
        return WorkspaceFileContent(path=path, size_bytes=size, content=self.redact.text(content))
