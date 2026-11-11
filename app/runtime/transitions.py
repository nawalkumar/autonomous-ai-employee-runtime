"""Deterministic audit event types and observation helpers for the runtime."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.models.enums import Actor
from app.models.events import AuditEvent
from app.models.records import PendingAction
from app.tools.result import ToolResult


class EventType:
    TASK_CREATED = "TASK_CREATED"
    PLAN_STARTED = "PLAN_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_RESUMED = "step.resumed"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_SUCCEEDED = "TOOL_SUCCEEDED"
    TOOL_FAILED = "TOOL_FAILED"
    STEP_SUCCEEDED = "STEP_SUCCEEDED"
    STEP_FAILED = "STEP_FAILED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    FAILURE_DETECTED = "failure.detected"
    RECOVERY_EVALUATED = "recovery.evaluated"
    RECOVERY_RETRY_STARTED = "recovery.retry_started"
    RECOVERY_RETRY_SUCCEEDED = "recovery.retry_succeeded"
    RECOVERY_EXHAUSTED = "recovery.exhausted"
    VERIFICATION_STARTED = "verification.started"
    VERIFICATION_CHECK = "verification.check"
    VERIFICATION_PASSED = "verification.passed"
    VERIFICATION_FAILED = "verification.failed"
    VERIFICATION_UNVERIFIED = "verification.unverified"
    EVIDENCE_RECORDED = "evidence.recorded"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_REJECTED = "approval.rejected"
    APPROVAL_INVALIDATED = "approval.invalidated"


def make_event(
    *,
    task_id: str,
    event_type: str,
    action: str | None = None,
    tool: str | None = None,
    arguments: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    observation: dict[str, Any] | None = None,
    failure: dict[str, Any] | None = None,
    verification_result: dict[str, Any] | None = None,
    actor: Actor = Actor.SYSTEM,
) -> AuditEvent:
    return AuditEvent(
        task_id=task_id,
        event_type=event_type,
        action=action,
        tool=tool,
        arguments=arguments,
        result=result,
        observation=observation,
        failure=failure,
        verification_result=verification_result,
        actor=actor,
    )


def observe_tool_result(tool_name: str, result: ToolResult) -> str:
    """Build a human-readable observation summary from a ToolResult."""
    if result.ok:
        data = result.data or {}
        if tool_name == "company_api":
            if "tickets" in data:
                return (
                    f"Company API listed {data.get('count', 0)} of {data.get('total', 0)} "
                    "matching ticket(s)."
                )
            if "customer" in data:
                return f"Company API created customer {data['customer'].get('customer_id')}."
            if "previous" in data and "ticket" in data:
                ticket = data["ticket"]
                return (
                    f"Company API updated ticket {ticket.get('ticket_id')} "
                    f"(status {ticket.get('status')}, priority {ticket.get('priority')}, "
                    f"assignee {ticket.get('assignee_id')})."
                )
            if "previous" in data and "employee" in data:
                employee = data["employee"]
                return (
                    f"Company API set employee {employee.get('employee_id')} "
                    f"status to {employee.get('status')}."
                )
            if "ticket" in data:
                ticket = data["ticket"]
                return (
                    f"Company API created/retrieved ticket {ticket.get('ticket_id')} "
                    f"for customer {ticket.get('customer_id')}."
                )
            if "employee" in data:
                employee = data["employee"]
                return (
                    f"Company API returned employee {employee.get('employee_id')} "
                    f"with title '{employee.get('title')}'."
                )
            if "customers" in data:
                count = data.get("count", len(data.get("customers", [])))
                return f"Company API customer search returned {count} match(es)."
        if tool_name == "file":
            path = data.get("path")
            if "content" in data:
                return f"File tool read '{path}'."
            if "bytes_written" in data:
                return f"File tool wrote confirmation to '{path}'."
            if "files" in data:
                return f"File tool listed {data.get('count', 0)} file(s)."
        return f"Tool '{tool_name}' succeeded."

    error_type = result.error_type or "unknown"
    message = result.error_message or "unknown error"
    return (
        f"Tool '{tool_name}' failed with {error_type}: {message}"
    )


def idempotency_key_for(task_id: str, step_id: str) -> str:
    return f"{task_id}:{step_id}"


def action_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    """Deterministic identity of an exact action: tool, operation and arguments.

    Canonical JSON (sorted keys, no whitespace) so that equal actions always
    produce the same fingerprint, across processes and restarts.
    """
    canonical = json.dumps(
        {"tool": tool_name, "operation": arguments.get("operation"), "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def pending_fingerprint(pending: PendingAction) -> str:
    """The action a pending approval is bound to.

    Records persisted before fingerprints existed are bound to the arguments
    they recorded, which are the arguments shown to the approver.
    """
    return pending.fingerprint or action_fingerprint(pending.tool_name, pending.arguments)


def pending_matches(pending: PendingAction, tool_name: str, arguments: dict[str, Any]) -> bool:
    """True only if the approved/requested action is exactly the action about to run."""
    current = action_fingerprint(tool_name, arguments)
    recorded = action_fingerprint(pending.tool_name, pending.arguments)
    return recorded == current and pending.fingerprint in (None, current)
