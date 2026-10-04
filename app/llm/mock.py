"""Deterministic mock LLM for local development, demos, and tests."""

from __future__ import annotations

import json
import re

from app.llm.base import LLMClient, LLMRequest, LLMResponse

_SCENARIO_A = re.compile(
    r"update\s+employee\s+e-17.*senior\s+engineer",
    re.IGNORECASE | re.DOTALL,
)
_SCENARIO_B = re.compile(
    r"support\s+ticket.*acme|acme.*billing|\$\s*240",
    re.IGNORECASE | re.DOTALL,
)


def _scenario_a_goal(original: str) -> dict:
    return {
        "original_request": original,
        "objective": (
            "Update employee E-17 title to Senior Engineer and create a confirmation note."
        ),
        "entities": {
            "employee_id": "E-17",
            "title": "Senior Engineer",
        },
        "constraints": [],
        "required_information": [],
        "success_criteria": [
            {
                "description": "Employee E-17 has title Senior Engineer",
                "criterion_type": "employee_field",
                "target": "employees",
                "expected_value": {"employee_id": "E-17", "title": "Senior Engineer"},
            },
            {
                "description": "Confirmation note exists in workspace",
                "criterion_type": "file_exists",
                "target": "workspace",
                "expected_value": {"path": "employee_update.txt"},
            },
        ],
    }


def _scenario_a_plan() -> dict:
    return {
        "steps": [
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
            },
            {
                "step_id": "step_2",
                "description": "Write confirmation note to workspace",
                "tool_name": "file",
                "arguments": {
                    "operation": "write",
                    "path": "employee_update.txt",
                    "content": (
                        "Confirmation: Employee E-17 title updated to Senior Engineer."
                    ),
                },
                "expected_outcome": "Confirmation file written",
            },
        ]
    }


def _scenario_b_goal(original: str) -> dict:
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
        "success_criteria": [
            {
                "description": "Support ticket exists for Acme Corp billing discrepancy",
                "criterion_type": "ticket_exists",
                "target": "tickets",
                "expected_value": {
                    "company": "Acme Corp",
                    "subject_contains": "billing",
                },
            }
        ],
    }


def _scenario_b_plan() -> dict:
    return {
        "steps": [
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
    }


class MockLLMClient(LLMClient):
    """Keyword-routed deterministic responses for known demo scenarios."""

    provider_name = "mock"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kind = str(request.metadata.get("kind", "")).lower()
        user_text = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        # Prefer the original goal embedded in planner prompts.
        haystack = user_text
        for message in request.messages:
            haystack = f"{haystack}\n{message.content}"

        if kind == "interpret":
            payload = self._interpret_payload(user_text)
        elif kind == "plan":
            payload = self._plan_payload(haystack)
        else:
            payload = {"message": f"[mock] acknowledged: {user_text[:200]}"}

        return LLMResponse(
            content=json.dumps(payload),
            model="mock-llm",
            provider=self.provider_name,
            raw={"kind": kind, "scenario": payload.get("_scenario")},
        )

    def _interpret_payload(self, text: str) -> dict:
        if _SCENARIO_A.search(text):
            data = _scenario_a_goal(text.strip())
            data["_scenario"] = "A"
            return data
        if _SCENARIO_B.search(text):
            data = _scenario_b_goal(text.strip())
            data["_scenario"] = "B"
            return data
        raise ValueError(
            "MockLLM has no deterministic interpretation for this goal. "
            "Supported demos: employee E-17 title update; Acme Corp billing ticket."
        )

    def _plan_payload(self, text: str) -> dict:
        if _SCENARIO_A.search(text):
            data = _scenario_a_plan()
            data["_scenario"] = "A"
            return data
        if _SCENARIO_B.search(text):
            data = _scenario_b_plan()
            data["_scenario"] = "B"
            return data
        raise ValueError(
            "MockLLM has no deterministic plan for this goal. "
            "Supported demos: employee E-17 title update; Acme Corp billing ticket."
        )
