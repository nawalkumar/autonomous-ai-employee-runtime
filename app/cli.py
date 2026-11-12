"""Local developer CLI for the Autonomous AI Employee Runtime.

Runs real tasks through the existing planning pipeline, execution runtime,
SQLite store, approval workflow, and independent outcome verifier.

    python -m app.cli run "<goal>" [--inject-transient create_ticket]
    python -m app.cli status <task_id>
    python -m app.cli approve <task_id> [--step STEP_ID] [--yes]
    python -m app.cli reject <task_id> [--reason TEXT]
    python -m app.cli resume <task_id>
    python -m app.cli history <task_id>

By default the planner is the deterministic mock (two supported goal formats).
When ``AAE_DYNAMIC_PLANNING_ENABLED=true``, a trusted intent catalog may invoke
the configured LLM provider (default ``mock``) to propose structured plans that
still pass deterministic validation and independently derived authorization.
The three bounded collection grammars are an exception: their plans are built
deterministically and the provider is not asked to plan them. This CLI has no
replan command. If ``AAE_REPLANNING_ENABLED`` is set, the existing executor may
replan a supported business conflict; the default is off.
Mutating commands hold an exclusive, OS-backed per-task lock shared with the
HTTP API; this protects cooperating CLI and API processes, not code that uses
ExecutionRuntime directly.

Exit codes: 0 completed / successful read, 1 failed or aborted, 2 usage,
configuration or file-permission error, 3 goal rejected, 4 awaiting approval, 5 unknown task or
invalid approval, 6 task locked by another process, 70 unexpected internal error.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.authz import AuthorizationError, authorization_for_supported_goal
from app.config import Settings, database_inside_workspace
from app.llm.factory import create_llm_client
from app.llm.mock import SUPPORTED_GOALS, MockLLMClient
from app.models.enums import (
    ApprovalStatus,
    FailureType,
    PlanStepStatus,
    TaskStatus,
    ToolCallStatus,
)
from app.models.ids import new_id
from app.models.state import ExecutionState
from app.planning import (
    InterpretationError,
    PlanningError,
    PlanningPipeline,
    SUPPORTED_DYNAMIC_INTENTS,
)
from app.runtime import ApprovalError, ExecutionRuntime, pending_fingerprint
from app.runtime.locking import LOCK_BACKEND, LockBusyError, TaskLock  # noqa: F401
from app.store import SQLiteStore
from app.tools import BaseTool, CompanyAPITool, FailureInjector, FileTool, ToolRegistry
from app.world import CompanyRepository

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_USAGE = 2
EXIT_GOAL_REJECTED = 3
EXIT_NEEDS_APPROVAL = 4
EXIT_INVALID_TASK = 5
EXIT_LOCKED = 6
EXIT_INTERNAL = 70

_TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

ConfirmFn = Callable[[str], bool]


class CliError(Exception):
    """Expected failure with a user-facing message and exit code."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class TaskLockedError(CliError):
    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"Task {task_id} is locked by another CLI process or API request; "
            "nothing was changed.",
            EXIT_LOCKED,
        )


# --- Per-task locking ---------------------------------------------------------
# Shared with the HTTP API (app.runtime.locking), so CLI and API processes that
# use the same DATABASE_PATH exclude each other.


def validate_task_id(task_id: str) -> str:
    if not _TASK_ID.fullmatch(task_id or ""):
        raise CliError(f"Invalid task id: {task_id!r}", EXIT_INVALID_TASK)
    return task_id


@contextmanager
def task_lock(lock_dir: Path, task_id: str) -> Iterator[None]:
    """Exclusive, non-blocking OS lock for one task.

    While this lock is held, call ``ExecutionRuntime`` with
    ``execution_locked=True`` so the runtime does not re-acquire the same lock.
    """
    validate_task_id(task_id)
    lock = TaskLock(lock_dir, task_id)
    try:
        lock.acquire()
    except PermissionError:
        raise CliError(
            f"Permission denied opening lock file {lock.path}. "
            "Check that it is owned by the user running the CLI.",
            EXIT_USAGE,
        ) from None
    except LockBusyError as exc:
        raise TaskLockedError(task_id) from exc
    try:
        yield
    finally:
        lock.release()


# --- Runtime assembly ---------------------------------------------------------


@dataclass
class CliRuntime:
    database_path: Path
    workspace_path: Path
    lock_dir: Path
    repository: CompanyRepository
    injector: FailureInjector
    registry: ToolRegistry
    store: SQLiteStore
    runtime: ExecutionRuntime
    settings: Settings

    def planning(self) -> PlanningPipeline:
        # Disabled: always the offline mock (no provider calls).
        # Enabled: configured provider (default mock) behind the feature flag.
        if self.settings.aae_dynamic_planning_enabled:
            llm = create_llm_client(self.settings)
        else:
            llm = MockLLMClient()
        return PlanningPipeline(llm, self.registry, settings=self.settings)


def _require_writable_dir(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError(
            f"Cannot create {label} directory {path}: {exc.strerror or exc}", EXIT_USAGE
        ) from None
    if not os.access(path, os.W_OK | os.X_OK):
        raise CliError(
            f"{label.capitalize()} directory is not writable by the current user: {path}",
            EXIT_USAGE,
        )


def _require_writable_file(path: Path, label: str) -> None:
    if path.exists() and not os.access(path, os.R_OK | os.W_OK):
        raise CliError(
            f"{label.capitalize()} file is not readable and writable by the current user: "
            f"{path}",
            EXIT_USAGE,
        )


# Tool classes that build_runtime registers; capability discovery lists only these.
RUNTIME_TOOLS: tuple[type[BaseTool], ...] = (CompanyAPITool, FileTool)


def build_runtime(settings: Settings) -> CliRuntime:
    database_path = Path(settings.database_path).expanduser().resolve()
    workspace_path = Path(settings.workspace_path).expanduser().resolve()
    lock_dir = database_path.parent / "locks"
    if database_inside_workspace(database_path, workspace_path):
        raise CliError(
            f"Database {database_path} must be outside workspace {workspace_path}",
            EXIT_USAGE,
        )
    if database_inside_workspace(lock_dir, workspace_path):
        raise CliError(
            f"Lock directory {lock_dir} must be outside workspace {workspace_path}",
            EXIT_USAGE,
        )
    _require_writable_dir(database_path.parent, "data")
    _require_writable_file(database_path, "database")
    _require_writable_dir(workspace_path, "workspace")
    _require_writable_dir(lock_dir, "lock")

    repository = CompanyRepository(database_path)
    injector = FailureInjector()
    registry = ToolRegistry()
    registry.register(CompanyAPITool(repository, failure_injector=injector))
    registry.register(FileTool(workspace_path, protected_paths=[database_path, lock_dir]))
    store = SQLiteStore(database_path)
    runtime = ExecutionRuntime(
        store=store,
        registry=registry,
        workspace_path=workspace_path,
        max_recovery_attempts=settings.max_recovery_attempts,
        lock_dir=lock_dir,
    )
    return CliRuntime(
        database_path,
        workspace_path,
        lock_dir,
        repository,
        injector,
        registry,
        store,
        runtime,
        settings,
    )


# --- Output -------------------------------------------------------------------


class Output:
    def __init__(self, as_json: bool, secrets: Sequence[str]) -> None:
        self.as_json = as_json
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def line(self, text: str = "") -> None:
        if not self.as_json:
            print(self.redact(text))

    def json(self, payload: dict[str, Any]) -> None:
        if self.as_json:
            print(self.redact(json.dumps(payload, indent=2, default=str)))

    def error(self, message: str, exit_code: int) -> None:
        message = self.redact(message)
        if self.as_json:
            print(json.dumps({"error": message, "exit_code": exit_code}, indent=2))
        else:
            print(f"Error: {message}", file=sys.stderr)


def _environment(cli: CliRuntime) -> dict[str, Any]:
    provider = (
        cli.settings.llm_provider
        if cli.settings.aae_dynamic_planning_enabled
        else "mock"
    )
    return {
        "provider": provider,
        "dynamic_planning": cli.settings.aae_dynamic_planning_enabled,
        "database": str(cli.database_path),
        "workspace": str(cli.workspace_path),
    }


def _print_environment(out: Output, cli: CliRuntime, *, show_formats: bool) -> None:
    if cli.settings.aae_dynamic_planning_enabled:
        out.line(
            f"Provider:  {cli.settings.llm_provider} "
            "(dynamic planning enabled; trusted intents only; "
            "collection workflows are deterministic and do not call the provider)"
        )
    else:
        out.line("Provider:  mock (deterministic; accepts only its supported goal formats)")
    if show_formats:
        for goal in SUPPORTED_GOALS:
            out.line(f"           - {goal}")
        if cli.settings.aae_dynamic_planning_enabled:
            out.line("           Dynamic intents:")
            for goal in SUPPORTED_DYNAMIC_INTENTS:
                out.line(f"           - {goal}")
    out.line(f"Database:  {cli.database_path}")
    out.line(f"Workspace: {cli.workspace_path}")
    out.line()


def task_summary(state: ExecutionState) -> dict[str, Any]:
    checks = state.verification_checks
    pending = state.pending_action
    return {
        "task_id": state.task_id,
        "status": state.final_status.value,
        "user_goal": state.user_goal,
        "steps_completed": sum(1 for s in state.plan if s.status is PlanStepStatus.COMPLETED),
        "steps_total": len(state.plan),
        "steps": [
            {
                "step_id": s.step_id,
                "description": s.description,
                "tool": s.resolved_tool_name(),
                "operation": s.arguments.get("operation"),
                "status": s.status.value,
            }
            for s in state.plan
        ],
        "retry_count": state.retry_count,
        "approval_status": state.approval_status.value,
        "pending_action": (
            {
                "step_id": pending.step_id,
                "tool": pending.tool_name,
                "arguments": pending.arguments,
                "risk": pending.risk,
                "reason": pending.reason,
            }
            if pending is not None and state.approval_status is ApprovalStatus.PENDING
            else None
        ),
        "verification_status": state.verification_status.value,
        "verification_checks_passed": sum(1 for c in checks if c.get("passed")),
        "verification_checks_total": len(checks),
        "failed_checks": [
            {"name": c.get("name"), "message": c.get("message")}
            for c in checks
            if not c.get("passed")
        ],
        "evidence_count": len(state.evidence),
        "completion_summary": state.completion_summary,
        "collection_progress": [
            {
                "item_id": item.item_id,
                "step_id": item.step_id,
                "status": item.status,
                "error": item.error,
            }
            for item in state.collections.progress
        ],
        "observations": _list_observations(state),
    }


def _list_observations(state: ExecutionState) -> list[dict[str, Any]]:
    """Ticket ids actually returned by successful bounded list calls."""
    observations: list[dict[str, Any]] = []
    for call in state.tool_calls:
        if call.status is not ToolCallStatus.SUCCEEDED:
            continue
        if (call.arguments or {}).get("operation") != "list_tickets":
            continue
        data = (call.result or {}).get("data")
        if not isinstance(data, dict):
            continue
        tickets = data.get("tickets")
        if not isinstance(tickets, list):
            continue
        observations.append(
            {
                "step_id": call.step_id,
                "count": data.get("count"),
                "total": data.get("total"),
                "ticket_ids": [
                    ticket.get("ticket_id")
                    for ticket in tickets
                    if isinstance(ticket, dict) and ticket.get("ticket_id")
                ],
            }
        )
    return observations


def _print_pending(out: Output, pending: dict[str, Any]) -> None:
    out.line("Pending approval:")
    out.line(f"  Step:      {pending['step_id']}")
    out.line(f"  Tool:      {pending['tool']}")
    out.line(f"  Arguments: {json.dumps(pending['arguments'], sort_keys=True)}")
    out.line(f"  Risk:      {pending['risk']}")
    out.line(f"  Reason:    {pending['reason']}")


def _print_task(out: Output, state: ExecutionState) -> None:
    summary = task_summary(state)
    out.line(f"Task:          {summary['task_id']}")
    out.line(f"Status:        {summary['status'].upper()}")
    out.line(f"Steps:         {summary['steps_completed']}/{summary['steps_total']} completed")
    for step in summary["steps"]:
        out.line(
            f"  - {step['step_id']}: {step['tool']}.{step['operation']} [{step['status']}]"
        )
    if summary["collection_progress"]:
        out.line("Collection:")
        for item in summary["collection_progress"]:
            detail = f"  - {item['item_id']} [{item['status']}] step {item['step_id']}"
            if item.get("error"):
                detail += f" ({item['error']})"
            out.line(detail)
    if summary["observations"]:
        out.line("Observations:")
        for observed in summary["observations"]:
            ids = ", ".join(observed["ticket_ids"])
            out.line(
                f"  - {observed['step_id']}: {observed['count']} of {observed['total']} ({ids})"
            )
    out.line(f"Retries:       {summary['retry_count']}")
    if summary["pending_action"]:
        _print_pending(out, summary["pending_action"])
    if summary["verification_checks_total"]:
        out.line(
            f"Verification:  {summary['verification_status'].upper()} "
            f"({summary['verification_checks_passed']}/"
            f"{summary['verification_checks_total']} checks passed, "
            f"{summary['evidence_count']} evidence items)"
        )
        for failed in summary["failed_checks"]:
            out.line(f"  FAILED {failed['name']}: {failed['message']}")
    else:
        out.line(f"Verification:  {summary['verification_status'].upper()} (not run)")
    if state.final_status in (TaskStatus.FAILED, TaskStatus.ABORTED):
        out.line(f"Reason:        {summary['completion_summary']}")


def _report(out: Output, cli: CliRuntime, state: ExecutionState) -> None:
    _print_task(out, state)
    out.json({**_environment(cli), "task": task_summary(state)})


def _exit_for(state: ExecutionState) -> int:
    if state.final_status is TaskStatus.COMPLETED:
        return EXIT_OK
    if state.final_status is TaskStatus.NEEDS_APPROVAL:
        return EXIT_NEEDS_APPROVAL
    return EXIT_TASK_FAILED


def _load(cli: CliRuntime, task_id: str) -> ExecutionState:
    try:
        return cli.runtime.load(task_id)
    except KeyError:
        raise CliError(f"Unknown task: {task_id}", EXIT_INVALID_TASK) from None


# --- Commands -----------------------------------------------------------------


def cmd_run(args: argparse.Namespace, cli: CliRuntime, out: Output, _: Any) -> int:
    _print_environment(out, cli, show_formats=True)
    if args.inject_transient:
        cli.injector.inject_once(
            f"company_api.{args.inject_transient}",
            FailureType.TRANSIENT.value,
            "503 Service Unavailable (simulated by --inject-transient)",
        )
        out.line(f"Simulated: one transient 503 on company_api.{args.inject_transient}")
        out.line()
    try:
        pipeline = cli.planning()
        planned = pipeline.interpret_and_plan(args.goal)
        authorization = planned.authorization
        if authorization is None:
            authorization = authorization_for_supported_goal(
                args.goal, max_recovery_attempts=cli.settings.max_recovery_attempts
            )
    except (InterpretationError, PlanningError, AuthorizationError) as exc:
        raise CliError(f"Goal rejected; no task was created. {exc}", EXIT_GOAL_REJECTED) from None

    task_id = new_id("task_")
    with task_lock(cli.lock_dir, task_id):
        try:
            cli.runtime.create_task(
                task_id=task_id,
                user_goal=args.goal,
                interpreted_goal=planned.interpreted_goal,
                plan=planned.plan.steps,
                authorization=authorization,
            )
        except AuthorizationError as exc:
            raise CliError(
                f"Plan unauthorized; no task was created. {exc}",
                EXIT_GOAL_REJECTED,
            ) from None
        state = cli.runtime.run(task_id, execution_locked=True)
    _report(out, cli, state)
    return _exit_for(state)


def cmd_status(args: argparse.Namespace, cli: CliRuntime, out: Output, _: Any) -> int:
    _print_environment(out, cli, show_formats=False)
    state = _load(cli, validate_task_id(args.task_id))
    _report(out, cli, state)
    return EXIT_OK


def _cli_approver() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no passwd entry (e.g. arbitrary container UID)
        return "cli"


def _pending_or_fail(state: ExecutionState) -> None:
    if (
        state.final_status is not TaskStatus.NEEDS_APPROVAL
        or state.approval_status is not ApprovalStatus.PENDING
        or state.pending_action is None
    ):
        raise CliError(
            f"Task {state.task_id} has no pending approval "
            f"(status {state.final_status.value}).",
            EXIT_INVALID_TASK,
        )


def cmd_approve(
    args: argparse.Namespace, cli: CliRuntime, out: Output, confirm: ConfirmFn | None
) -> int:
    _print_environment(out, cli, show_formats=False)
    task_id = validate_task_id(args.task_id)
    with task_lock(cli.lock_dir, task_id):
        state = _load(cli, task_id)
        _pending_or_fail(state)
        pending = task_summary(state)["pending_action"]
        _print_pending(out, pending)
        out.line()
        if args.step is not None and args.step != pending["step_id"]:
            raise CliError(
                f"Refusing approval: pending step is {pending['step_id']!r}, "
                f"not {args.step!r}. Nothing was changed.",
                EXIT_INVALID_TASK,
            )
        if not args.yes:
            if confirm is None:
                raise CliError(
                    "Refusing to approve non-interactively without --yes. "
                    "Nothing was changed.",
                    EXIT_INVALID_TASK,
                )
            if not confirm(
                f"Approve {pending['tool']} {pending['arguments'].get('operation')} "
                f"for step {pending['step_id']}? [y/N] "
            ):
                raise CliError("Approval declined. Nothing was changed.", EXIT_INVALID_TASK)
        try:
            state = cli.runtime.approve_task(
                task_id,
                approver=_cli_approver(),
                expected_fingerprint=pending_fingerprint(state.pending_action),
                execution_locked=True,
            )
        except ApprovalError as exc:
            raise CliError(f"Approval failed: {exc}", EXIT_INVALID_TASK) from None
    _report(out, cli, state)
    return _exit_for(state)


def cmd_reject(args: argparse.Namespace, cli: CliRuntime, out: Output, _: Any) -> int:
    _print_environment(out, cli, show_formats=False)
    task_id = validate_task_id(args.task_id)
    with task_lock(cli.lock_dir, task_id):
        state = _load(cli, task_id)
        _pending_or_fail(state)
        try:
            state = cli.runtime.reject_task(
                task_id,
                reason=args.reason,
                approver=_cli_approver(),
                execution_locked=True,
            )
        except ApprovalError as exc:
            raise CliError(f"Rejection failed: {exc}", EXIT_INVALID_TASK) from None
    _report(out, cli, state)
    return _exit_for(state)


def cmd_resume(args: argparse.Namespace, cli: CliRuntime, out: Output, _: Any) -> int:
    _print_environment(out, cli, show_formats=False)
    task_id = validate_task_id(args.task_id)
    with task_lock(cli.lock_dir, task_id):
        state = _load(cli, task_id)
        if state.final_status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            state = cli.runtime.run(task_id, execution_locked=True)
        elif state.final_status is TaskStatus.NEEDS_APPROVAL:
            out.line("Task is awaiting approval; use 'approve' or 'reject'.")
        else:
            out.line("Task is already terminal; nothing was executed.")
    _report(out, cli, state)
    return _exit_for(state)


def cmd_history(args: argparse.Namespace, cli: CliRuntime, out: Output, _: Any) -> int:
    _print_environment(out, cli, show_formats=False)
    task_id = validate_task_id(args.task_id)
    _load(cli, task_id)
    events = cli.store.get_events(task_id)
    out.line(f"History for {task_id} ({len(events)} events):")
    for event in events:
        detail = event.failure or event.result or event.observation or {}
        brief = json.dumps(detail, sort_keys=True, default=str)
        if len(brief) > 120:
            brief = brief[:117] + "..."
        out.line(
            f"  {event.timestamp.isoformat(timespec='seconds')}  "
            f"{event.event_type:<26} {event.actor.value:<6} "
            f"{event.tool or '-':<12} {brief}"
        )
    out.json(
        {
            **_environment(cli),
            "task_id": task_id,
            "events": [e.model_dump(mode="json") for e in events],
        }
    )
    return EXIT_OK


# --- Entry point --------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        raise CliError(message, EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m app.cli",
        description="Run real tasks on the Autonomous AI Employee Runtime (mock planner).",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--provider",
        choices=["mock", "xai"],
        default="mock",
        help="planner provider; only 'mock' is enabled in this CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    run = sub.add_parser("run", help="interpret, plan, execute and verify a goal")
    run.add_argument("goal")
    run.add_argument(
        "--inject-transient",
        choices=["create_ticket"],
        help="simulate one transient 503 on this company operation (demo only)",
    )
    run.set_defaults(handler=cmd_run)

    for name, handler, help_text in (
        ("status", cmd_status, "show persisted task state"),
        ("resume", cmd_resume, "continue a queued or interrupted task"),
        ("history", cmd_history, "show stored audit events in order"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("task_id")
        cmd.set_defaults(handler=handler)

    approve = sub.add_parser("approve", help="approve the pending action and resume")
    approve.add_argument("task_id")
    approve.add_argument("--step", help="approve only if this is the pending step")
    approve.add_argument("--yes", action="store_true", help="confirm without prompting")
    approve.set_defaults(handler=cmd_approve)

    reject = sub.add_parser("reject", help="reject the pending action (task is aborted)")
    reject.add_argument("task_id")
    reject.add_argument("--reason", default="Rejected via CLI")
    reject.set_defaults(handler=cmd_reject)
    return parser


def _interactive_confirm() -> ConfirmFn | None:
    if not (sys.stdin and sys.stdin.isatty()):
        return None

    def confirm(prompt: str) -> bool:
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return sys.stdin.readline().strip().lower() in {"y", "yes"}

    return confirm


_DEFAULT = object()

_SQLITE_ACCESS_ERRORS = ("readonly database", "unable to open database file")


def _is_sqlite_access_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(fragment in message for fragment in _SQLITE_ACCESS_ERRORS)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    confirm: ConfirmFn | None | object = _DEFAULT,
) -> int:
    as_json = "--json" in (sys.argv[1:] if argv is None else argv)
    out = Output(as_json, secrets=[])
    try:
        args = build_parser().parse_args(argv)
        if args.provider != "mock":
            raise CliError(
                "The xAI provider is disabled in the CLI: live-provider goal grounding "
                "has not been validated. Use the default mock provider.",
                EXIT_USAGE,
            )
        try:
            settings = settings or Settings()
        except ValidationError as exc:
            messages = "; ".join(err.get("msg", "") for err in exc.errors())
            raise CliError(f"Invalid configuration: {messages}", EXIT_USAGE) from None
        out = Output(as_json, secrets=[settings.xai_api_key])
        cli = build_runtime(settings)
        confirm_fn = _interactive_confirm() if confirm is _DEFAULT else confirm
        return args.handler(args, cli, out, confirm_fn)
    except CliError as exc:
        out.error(str(exc), exc.exit_code)
        return exc.exit_code
    except PermissionError as exc:
        target = exc.filename or "a runtime file"
        out.error(f"Permission denied: {target}", EXIT_USAGE)
        return EXIT_USAGE
    except sqlite3.OperationalError as exc:
        if not _is_sqlite_access_error(exc):
            out.error(f"Unexpected error (OperationalError): {exc}", EXIT_INTERNAL)
            return EXIT_INTERNAL
        out.error(f"Database is not writable or cannot be opened: {exc}", EXIT_USAGE)
        return EXIT_USAGE
    except KeyboardInterrupt:
        out.error("Interrupted.", 130)
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort sanitized diagnostic
        out.error(f"Unexpected error ({type(exc).__name__}): {exc}", EXIT_INTERNAL)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
