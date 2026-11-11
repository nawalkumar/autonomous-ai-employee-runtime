"""Derive and evaluate deterministic verification criteria from execution state."""

from __future__ import annotations

import json
from typing import Any

from app.models.enums import ToolCallStatus
from app.models.goal import SuccessCriterion
from app.models.records import ToolCallRecord
from app.models.state import ExecutionState
from app.tools.capabilities import CAPABILITIES

# (tool_name, operation) pairs that change world state and must be proven done.
MUTATING_OPERATIONS: frozenset[tuple[str, str]] = CAPABILITIES.mutating_keys()


def ticket_id_from_call(call: ToolCallRecord) -> str | None:
    """Ticket ID reported by this call's own result, or None if absent/malformed."""
    result = call.result if isinstance(call.result, dict) else {}
    data = result.get("data")
    ticket = data.get("ticket") if isinstance(data, dict) else None
    ticket_id = ticket.get("ticket_id") if isinstance(ticket, dict) else None
    return ticket_id if isinstance(ticket_id, str) and ticket_id else None


def is_successful_create_ticket(call: ToolCallRecord) -> bool:
    return (
        call.tool_name == "company_api"
        and (call.arguments or {}).get("operation") == "create_ticket"
        and call.status is ToolCallStatus.SUCCEEDED
    )


def created_ticket_ids(state: ExecutionState) -> list[str]:
    """Tickets created by successful create_ticket calls in this task (reads excluded)."""
    ids: list[str] = []
    for call in state.tool_calls:
        if not is_successful_create_ticket(call):
            continue
        ticket_id = ticket_id_from_call(call)
        if ticket_id and ticket_id not in ids:
            ids.append(ticket_id)
    return ids


def collect_criteria(state: ExecutionState) -> list[SuccessCriterion]:
    """Union of explicit goal criteria and criteria derived from the plan/execution.

    Explicit criteria capture what the user asked for; derived criteria capture
    what the plan said it would change. Both must hold for verification to pass.
    """
    explicit: list[SuccessCriterion] = list(state.success_criteria)
    if state.interpreted_goal is not None:
        explicit.extend(state.interpreted_goal.success_criteria)

    derived = derive_plan_coverage_criteria(state) + derive_criteria_from_execution(
        state
    )
    return _dedupe([*explicit, *derived])


def derive_plan_coverage_criteria(state: ExecutionState) -> list[SuccessCriterion]:
    """Every mutating plan step must have completed with a successful tool call."""
    criteria: list[SuccessCriterion] = []
    for step in state.plan:
        tool_name = step.resolved_tool_name()
        operation = step.arguments.get("operation")
        if (tool_name, operation) not in MUTATING_OPERATIONS:
            continue
        criteria.append(
            SuccessCriterion(
                description=f"Planned step '{step.step_id}' completed",
                criterion_type="step_completed",
                target="plan",
                expected_value={
                    "step_id": step.step_id,
                    "tool": tool_name,
                    "operation": operation,
                },
            )
        )
    return criteria


def derive_criteria_from_execution(state: ExecutionState) -> list[SuccessCriterion]:
    """Build verifiable outcomes from successful mutating tool calls."""
    criteria: list[SuccessCriterion] = []
    for call in state.tool_calls:
        if call.status is not ToolCallStatus.SUCCEEDED:
            continue
        args = call.arguments or {}
        if call.tool_name == "company_api":
            op = args.get("operation")
            if op == "create_ticket":
                # Bound to this call's own result; None fails verification.
                expected: dict[str, Any] = {
                    "customer_id": args.get("customer_id"),
                    "ticket_id": ticket_id_from_call(call),
                    "source_call_id": call.call_id,
                    "source_step_id": call.step_id,
                }
                subject = args.get("subject") or ""
                description = args.get("description") or ""
                if subject:
                    expected["subject_contains"] = subject
                if description:
                    expected["description_contains"] = description
                # Amount often appears only in free text for this schema.
                for token in ("240", "$240"):
                    if token in subject or token in description:
                        expected["text_contains"] = "240"
                        break
                criteria.append(
                    SuccessCriterion(
                        description="Support ticket exists with expected customer/details",
                        criterion_type="ticket_exists",
                        target="tickets",
                        expected_value=expected,
                    )
                )
            elif op == "update_employee":
                fields = args.get("fields") or {}
                criteria.append(
                    SuccessCriterion(
                        description="Employee fields match expected update",
                        criterion_type="employee_field",
                        target="employees",
                        expected_value={
                            "employee_id": args.get("employee_id"),
                            **fields,
                        },
                    )
                )
        elif call.tool_name == "file" and args.get("operation") == "write":
            criteria.append(
                SuccessCriterion(
                    description="Confirmation file exists with expected content",
                    criterion_type="file_exists",
                    target="workspace",
                    expected_value={
                        "path": args.get("path"),
                        "content_contains": args.get("content"),
                    },
                )
            )
    return criteria


def _dedupe(criteria: list[SuccessCriterion]) -> list[SuccessCriterion]:
    seen: set[str] = set()
    unique: list[SuccessCriterion] = []
    for criterion in criteria:
        key = json.dumps(
            [criterion.criterion_type, criterion.expected_value],
            sort_keys=True,
            default=str,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(criterion)
    return unique
