"""Deterministic audit event types and observation helpers for the runtime."""

from __future__ import annotations

from typing import Any

from app.models.enums import Actor
from app.models.events import AuditEvent
from app.tools.result import ToolResult


class EventType:
    TASK_CREATED = "TASK_CREATED"
    PLAN_STARTED = "PLAN_STARTED"
    STEP_STARTED = "STEP_STARTED"
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
    EVIDENCE_RECORDED = "evidence.recorded"


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
