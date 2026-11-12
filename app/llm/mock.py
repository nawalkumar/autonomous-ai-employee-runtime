"""Deterministic mock LLM for local development, demos, and tests.

The mock supports exactly two goal contracts and fails closed on everything else.
A goal is accepted only if the *entire* request matches one of the allowlisted
grammars below (after case, whitespace and final-period normalization). There is
no keyword matching: unrecognized clauses, negations, questions, conditions,
other entities, extra actions, or paraphrases do not match and are rejected
before any task can be created.

Scenario A (employee update, requires human approval):
    Update employee E-17['s] title to Senior Engineer
    [and write|save|create a confirmation note | and confirm the change in a note file]

Scenario B (billing ticket):
    Create a support ticket for Acme Corp about
    a|the $240 billing discrepancy | a billing discrepancy of $240
    [and save|write|create a summary note]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.llm.base import LLMClient, LLMRequest, LLMResponse

_GOAL_A = re.compile(
    r"update employee e-17(?:'s)? title to senior engineer"
    r"(?: and (?P<note>(?:write|save|create) a confirmation note"
    r"|confirm the change in a note file))?"
)
_GOAL_B = re.compile(
    r"create a support ticket for acme corp about "
    r"(?:(?:a|the) \$240 billing discrepancy|a billing discrepancy of \$240)"
    r"(?: and (?P<note>(?:save|write|create) a summary note))?"
)

SUPPORTED_GOALS = (
    "Update employee E-17 title to Senior Engineer "
    "[and write a confirmation note | and confirm the change in a note file]",
    "Create a support ticket for Acme Corp about a $240 billing discrepancy "
    "[and save a summary note]",
)

_SCENARIO_A_NOTE_PATH = "employee_update.txt"
_SCENARIO_B_NOTE_PATH = "acme_billing_summary.txt"
_SCENARIO_B_NOTE_CONTENT = (
    "Summary: Support ticket created for Acme Corp regarding a $240 billing discrepancy."
)

# Used only to explain a rejection; acceptance is decided by the grammars above.
_REJECTION_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:do not|don't|dont|never|without|not)\b"), "negated instruction"),
    (
        re.compile(r"\b(?:maybe|perhaps|if|unless|might|possibly|whether)\b"),
        "ambiguous or conditional request",
    ),
    (
        re.compile(r"\?$|^(?:what|who|how|show|look up|find|list|get|check|tell)\b"),
        "read-only or question request",
    ),
)


@dataclass(frozen=True)
class SupportedGoal:
    scenario: str  # "A" or "B"
    note_requested: bool


def _normalize(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.replace("\u2019", "'")).strip().lower()
    if normalized.endswith("."):
        normalized = normalized[:-1].rstrip()
    return normalized


def parse_supported_goal(text: str) -> SupportedGoal | None:
    """Return the supported contract the whole request matches, or None."""
    normalized = _normalize(text)
    match = _GOAL_A.fullmatch(normalized)
    if match:
        return SupportedGoal("A", match.group("note") is not None)
    match = _GOAL_B.fullmatch(normalized)
    if match:
        return SupportedGoal("B", match.group("note") is not None)
    return None


def _rejection_reason(text: str) -> str:
    normalized = _normalize(text)
    if "e-17" in normalized and "acme" in normalized:
        return "multiple scenarios in one request are not supported"
    for pattern, reason in _REJECTION_HINTS:
        if pattern.search(normalized):
            return reason
    return "request does not match a supported goal format"


def _unsupported(text: str) -> ValueError:
    return ValueError(
        f"MockLLM rejected the goal ({_rejection_reason(text)}). "
        "The mock only accepts these exact goal formats: "
        + " | ".join(SUPPORTED_GOALS)
    )


def _scenario_a_goal(original: str, note: bool) -> dict:
    criteria: list[dict] = [
        {
            "description": "Employee E-17 has title Senior Engineer",
            "criterion_type": "employee_field",
            "target": "employees",
            "expected_value": {"employee_id": "E-17", "title": "Senior Engineer"},
        }
    ]
    if note:
        criteria.append(
            {
                "description": "Confirmation note exists in workspace",
                "criterion_type": "file_exists",
                "target": "workspace",
                "expected_value": {"path": _SCENARIO_A_NOTE_PATH},
            }
        )
    return {
        "original_request": original,
        "objective": (
            "Update employee E-17 title to Senior Engineer and create a confirmation note."
            if note
            else "Update employee E-17 title to Senior Engineer."
        ),
        "entities": {
            "employee_id": "E-17",
            "title": "Senior Engineer",
        },
        "constraints": [],
        "required_information": [],
        "success_criteria": criteria,
    }


def _scenario_a_plan(note: bool) -> dict:
    steps: list[dict] = [
        {
            "step_id": "step_1",
            "description": "Update employee E-17 title to Senior Engineer",
            "tool_name": "company_api",
            "arguments": {
                "operation": "update_employee",
                "employee_id": "E-17",
                "fields": {"title": "Senior Engineer"},
            },
            "expected_outcome": "Employee title updated",
        }
    ]
    if note:
        steps.append(
            {
                "step_id": "step_2",
                "description": "Write confirmation note to workspace",
                "tool_name": "file",
                "arguments": {
                    "operation": "write",
                    "path": _SCENARIO_A_NOTE_PATH,
                    "content": (
                        "Confirmation: Employee E-17 title updated to Senior Engineer."
                    ),
                },
                "expected_outcome": "Confirmation file written",
            }
        )
    return {"steps": steps}


def _scenario_b_goal(original: str, note: bool) -> dict:
    criteria: list[dict] = [
        {
            "description": "Support ticket exists for Acme Corp billing discrepancy",
            "criterion_type": "ticket_exists",
            "target": "tickets",
            "expected_value": {
                "company": "Acme Corp",
                "subject_contains": "billing",
            },
        }
    ]
    if note:
        criteria.append(
            {
                "description": "Summary note exists in workspace",
                "criterion_type": "file_exists",
                "target": "workspace",
                "expected_value": {
                    "path": _SCENARIO_B_NOTE_PATH,
                    "content_contains": "Acme Corp",
                },
            }
        )
    return {
        "original_request": original,
        "objective": (
            "Create a support ticket for Acme Corp about a $240 billing discrepancy."
        ),
        "entities": {
            "company": "Acme Corp",
            "amount": "$240",
            "issue": "billing discrepancy",
        },
        "constraints": [],
        "required_information": ["customer_id for Acme Corp"],
        "success_criteria": criteria,
    }


def _scenario_b_plan(note: bool) -> dict:
    steps: list[dict] = [
        {
            "step_id": "step_1",
            "description": "Find customer Acme Corp",
            "tool_name": "company_api",
            "arguments": {
                "operation": "find_customer",
                "company": "Acme Corp",
            },
            "expected_outcome": "Customer C-1001 resolved",
        },
        {
            "step_id": "step_2",
            "description": "Create billing discrepancy ticket",
            "tool_name": "company_api",
            "arguments": {
                "operation": "create_ticket",
                "customer_id": "C-1001",
                "subject": "Billing discrepancy $240",
                "description": (
                    "Customer Acme Corp reported a $240 billing discrepancy."
                ),
                "priority": "high",
            },
            "expected_outcome": "Ticket created",
        },
    ]
    if note:
        steps.append(
            {
                "step_id": "step_3",
                "description": "Save billing discrepancy summary note to workspace",
                "tool_name": "file",
                "arguments": {
                    "operation": "write",
                    "path": _SCENARIO_B_NOTE_PATH,
                    "content": _SCENARIO_B_NOTE_CONTENT,
                },
                "expected_outcome": "Summary note written",
            }
        )
    return {"steps": steps}


def _interpreted_goal_from_messages(messages_text: list[str]) -> dict[str, Any] | None:
    """The interpreted goal the planner prompt carries, if present."""
    for content in messages_text:
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        goal = payload.get("interpreted_goal") if isinstance(payload, dict) else None
        if isinstance(goal, dict):
            return goal
    return None


def _file_criterion_paths(goal: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for criterion in goal.get("success_criteria") or []:
        expected = criterion.get("expected_value") or {}
        if criterion.get("criterion_type") == "file_exists" and expected.get("path"):
            paths.add(str(expected["path"]))
    return paths


def _dynamic_plan_payload(intent_id: str, entities: dict[str, Any]) -> dict[str, Any]:
    """Canned valid plans for trusted dynamic intents (tests / offline mode)."""
    if intent_id == "list_open_high_priority_tickets":
        return {
            "steps": [
                {
                    "step_id": "list_tickets",
                    "description": "List open high-priority tickets",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "list_tickets",
                        "filters": {"status": "open", "priority": "high"},
                    },
                    "expected_outcome": "Matching tickets listed",
                }
            ]
        }
    if intent_id == "list_tickets_for_customer":
        customer_id = str(entities["customer_id"])
        return {
            "steps": [
                {
                    "step_id": "list_tickets",
                    "description": f"List tickets for {customer_id}",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "list_tickets",
                        "filters": {"customer_id": customer_id},
                    },
                    "expected_outcome": "Customer tickets listed",
                }
            ]
        }
    if intent_id == "create_ticket_for_customer":
        customer_id = str(entities["customer_id"])
        subject = str(entities["subject"])
        description = str(entities.get("description") or subject)
        return {
            "steps": [
                {
                    "step_id": "create_ticket",
                    "description": f"Create support ticket for {customer_id}",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "create_ticket",
                        "customer_id": customer_id,
                        "subject": subject,
                        "description": description,
                        "priority": "medium",
                    },
                    "expected_outcome": "Ticket created",
                }
            ]
        }
    if intent_id == "update_ticket_status":
        ticket_id = str(entities["ticket_id"])
        status = str(entities["status"])
        return {
            "steps": [
                {
                    "step_id": "update_ticket",
                    "description": f"Set {ticket_id} status to {status}",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "update_ticket",
                        "ticket_id": ticket_id,
                        "fields": {"status": status},
                    },
                    "expected_outcome": "Ticket status updated",
                }
            ]
        }
    if intent_id == "assign_ticket":
        ticket_id = str(entities["ticket_id"])
        employee_id = str(entities["employee_id"])
        return {
            "steps": [
                {
                    "step_id": "assign_ticket",
                    "description": f"Assign {ticket_id} to {employee_id}",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "assign_ticket",
                        "ticket_id": ticket_id,
                        "employee_id": employee_id,
                    },
                    "expected_outcome": "Ticket assigned",
                }
            ]
        }
    if intent_id == "update_employee_title":
        employee_id = str(entities["employee_id"])
        title = str(entities["title"])
        return {
            "steps": [
                {
                    "step_id": "update_employee",
                    "description": f"Update {employee_id} title to {title}",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "update_employee",
                        "employee_id": employee_id,
                        "fields": {"title": title},
                    },
                    "expected_outcome": "Employee title updated",
                }
            ]
        }
    if intent_id == "create_customer_and_welcome_ticket":
        name = str(entities["name"])
        company = str(entities["company"])
        email = str(entities["email"])
        subject = str(entities.get("subject") or "Welcome")
        description = str(
            entities.get("description") or f"Welcome ticket for {name} at {company}."
        )
        return {
            "steps": [
                {
                    "step_id": "create_customer",
                    "description": f"Create customer {name}",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "create_customer",
                        "name": name,
                        "company": company,
                        "email": email,
                    },
                    "expected_outcome": "Customer created",
                },
                {
                    "step_id": "create_ticket",
                    "description": "Create welcome support ticket",
                    "tool_name": "company_api",
                    "arguments": {
                        "operation": "create_ticket",
                        "customer_id": {
                            "$ref": {
                                "step_id": "create_customer",
                                "path": ["customer", "customer_id"],
                            }
                        },
                        "subject": subject,
                        "description": description,
                        "priority": "low",
                    },
                    "expected_outcome": "Welcome ticket created",
                },
            ]
        }
    raise ValueError(f"MockLLM has no canned plan for intent {intent_id!r}")


class MockLLMClient(LLMClient):
    """Deterministic responses for mock scenarios and trusted dynamic intents."""

    provider_name = "mock"

    def __init__(self, *, call_log: list[str] | None = None) -> None:
        self.call_log = call_log if call_log is not None else []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kind = str(request.metadata.get("kind", "")).lower()
        self.call_log.append(kind)
        user_text = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )

        if kind == "interpret":
            payload = self._interpret_payload(user_text)
        elif kind == "plan":
            payload = self._plan_payload(
                _interpreted_goal_from_messages(
                    [m.content for m in request.messages if m.role == "user"]
                )
            )
        elif kind == "dynamic_plan":
            payload = self._dynamic_plan_from_request(request, user_text)
        else:
            payload = {"message": f"[mock] acknowledged: {user_text[:200]}"}

        return LLMResponse(
            content=json.dumps(payload),
            model="mock-llm",
            provider=self.provider_name,
            raw={"kind": kind, "scenario": payload.get("_scenario")},
        )

    def _dynamic_plan_from_request(self, request: LLMRequest, user_text: str) -> dict[str, Any]:
        intent_id = str(request.metadata.get("intent_id") or "")
        entities: dict[str, Any] = {}
        try:
            payload = json.loads(user_text)
        except (TypeError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            intent_id = intent_id or str(payload.get("intent_id") or "")
            goal = payload.get("goal") if isinstance(payload.get("goal"), dict) else {}
            if isinstance(goal, dict) and isinstance(goal.get("entities"), dict):
                entities = dict(goal["entities"])
        if not intent_id:
            raise ValueError("MockLLM dynamic planner requires intent_id")
        return _dynamic_plan_payload(intent_id, entities)

    def _interpret_payload(self, text: str) -> dict:
        goal = parse_supported_goal(text)
        if goal is None:
            raise _unsupported(text)
        if goal.scenario == "A":
            data = _scenario_a_goal(text.strip(), goal.note_requested)
        else:
            data = _scenario_b_goal(text.strip(), goal.note_requested)
        data["_scenario"] = goal.scenario
        return data

    def _plan_payload(self, interpreted: dict[str, Any] | None) -> dict:
        """Plan from the original request, re-validated with the same grammar."""
        if interpreted is None:
            raise ValueError("MockLLM planner requires an interpreted goal")
        original = str(interpreted.get("original_request") or "")
        goal = parse_supported_goal(original)
        if goal is None:
            raise _unsupported(original)

        note_path = _SCENARIO_A_NOTE_PATH if goal.scenario == "A" else _SCENARIO_B_NOTE_PATH
        file_paths = _file_criterion_paths(interpreted)
        expected_paths = {note_path} if goal.note_requested else set()
        if file_paths != expected_paths:
            raise ValueError(
                "MockLLM planner: interpreted success criteria do not match the "
                f"supported request (file criteria {sorted(file_paths)}, "
                f"expected {sorted(expected_paths)})"
            )

        if goal.scenario == "A":
            data = _scenario_a_plan(goal.note_requested)
        else:
            data = _scenario_b_plan(goal.note_requested)
        data["_scenario"] = goal.scenario
        return data
