"""Derive and evaluate deterministic verification criteria from execution state."""

from __future__ import annotations

from typing import Any

from app.models.enums import ToolCallStatus
from app.models.goal import SuccessCriterion
from app.models.state import ExecutionState


def collect_criteria(state: ExecutionState) -> list[SuccessCriterion]:
    """Use explicit success criteria when present; otherwise derive from plan/tool calls."""
    if state.success_criteria:
        return list(state.success_criteria)
    if state.interpreted_goal and state.interpreted_goal.success_criteria:
        return list(state.interpreted_goal.success_criteria)
    return derive_criteria_from_execution(state)


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
                expected: dict[str, Any] = {
                    "customer_id": args.get("customer_id"),
                    "ticket_id": state.extracted_information.get("last_ticket_id"),
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
