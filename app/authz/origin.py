"""Trusted origins for authorization scopes.

Authorization is never taken from planner output, tool observations, LLM text,
or a public API body. For this milestone the trusted origins are:

1. Deterministic mock goal grammars (``parse_supported_goal``) — CLI/API path.
2. An independently supplied scope at the internal ``create_task`` boundary
   (tests supply scopes via ``tests.authz_support``, never via this module's
   plan-matching helper). The public HTTP API never accepts a caller scope.
3. Narrow legacy mapping for persisted tasks that predate this field, using
   the same mock grammars and persisted ``user_goal`` / interpreted entities.
   Anything else fails closed.
"""

from __future__ import annotations

from typing import Any

from app.authz.errors import AuthorizationError, AuthorizationReason
from app.authz.models import (
    ArgumentConstraint,
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
)
from app.llm.mock import SupportedGoal, parse_supported_goal
from app.models.references import find_references, parse_reference
from app.models.state import ExecutionState


def operation_key(tool: str, operation: str) -> str:
    return f"{tool}.{operation}"


def authorization_for_supported_goal(
    goal_text: str,
    *,
    note_requested: bool | None = None,
    max_recovery_attempts: int = 2,
) -> AuthorizationScope:
    """Derive scope from a mock-grammar goal — independent of planner output."""
    matched = parse_supported_goal(goal_text)
    if matched is None:
        raise AuthorizationError(
            AuthorizationReason.LEGACY_UNMAPPED,
            "Goal is not a supported deterministic contract; no authorization granted.",
        )
    note = matched.note_requested if note_requested is None else note_requested
    if matched.scenario == "A":
        return _scenario_a_scope(note, max_recovery_attempts=max_recovery_attempts)
    return _scenario_b_scope(note, max_recovery_attempts=max_recovery_attempts)


def _scenario_a_scope(note: bool, *, max_recovery_attempts: int) -> AuthorizationScope:
    allowed = ["company_api.update_employee"]
    constraints: dict[str, dict[str, ArgumentConstraint]] = {
        "company_api.update_employee": {
            "employee_id": ExactValueConstraint(value="E-17"),
            "fields": FieldKeysConstraint(allowed_keys=["title"]),
            "fields.title": ExactValueConstraint(value="Senior Engineer"),
        }
    }
    mutations = 1
    steps = 1
    if note:
        allowed.append("file.write")
        constraints["file.write"] = {
            "path": ExactValueConstraint(value="employee_update.txt"),
        }
        mutations += 1
        steps += 1
    attempts = max(1, 1 + max_recovery_attempts)
    return AuthorizationScope(
        allowed_operations=allowed,
        constraints=constraints,
        budgets=AuthorizationBudgets(
            max_steps=steps,
            max_mutations=mutations * attempts,
            max_tool_calls=steps * attempts,
        ),
    )


def _scenario_b_scope(note: bool, *, max_recovery_attempts: int) -> AuthorizationScope:
    allowed = ["company_api.find_customer", "company_api.create_ticket"]
    constraints: dict[str, dict[str, ArgumentConstraint]] = {
        "company_api.find_customer": {
            "company": ExactValueConstraint(value="Acme Corp"),
        },
        "company_api.create_ticket": {
            "customer_id": ExactValueConstraint(value="C-1001"),
        },
    }
    mutations = 1
    steps = 2
    if note:
        allowed.append("file.write")
        constraints["file.write"] = {
            "path": ExactValueConstraint(value="acme_billing_summary.txt"),
        }
        mutations += 1
        steps += 1
    attempts = max(1, 1 + max_recovery_attempts)
    return AuthorizationScope(
        allowed_operations=allowed,
        constraints=constraints,
        budgets=AuthorizationBudgets(
            max_steps=steps,
            max_mutations=mutations * attempts,
            max_tool_calls=steps * attempts,
        ),
    )


def welcome_customer_ticket_scope(
    *,
    create_step_id: str = "create_customer",
    max_recovery_attempts: int = 2,
) -> AuthorizationScope:
    """Narrow W8 scope: one create_customer and a ticket for that step's customer_id."""
    attempts = max(1, 1 + max_recovery_attempts)
    return AuthorizationScope(
        allowed_operations=[
            "company_api.create_customer",
            "company_api.create_ticket",
        ],
        constraints={
            "company_api.create_ticket": {
                "customer_id": FromStepOutputConstraint(
                    step_id=create_step_id,
                    path=["customer", "customer_id"],
                )
            }
        },
        budgets=AuthorizationBudgets(
            max_steps=2,
            max_mutations=2 * attempts,
            max_tool_calls=2 * attempts,
        ),
    )


def legacy_authorization_for_state(state: ExecutionState) -> AuthorizationScope:
    """Map a pre-authorization task to a trusted scope, or fail closed."""
    matched = parse_supported_goal(state.user_goal)
    if matched is not None:
        return authorization_for_supported_goal(state.user_goal)
    interpreted = state.interpreted_goal
    if interpreted is not None:
        original = interpreted.original_request or state.user_goal
        if parse_supported_goal(original) is not None:
            return authorization_for_supported_goal(original)
        entities = interpreted.entities or {}
        if entities.get("employee_id") == "E-17" and entities.get("title") == "Senior Engineer":
            note = any(
                (c.expected_value or {}).get("path") == "employee_update.txt"
                if isinstance(c.expected_value, dict)
                else False
                for c in (interpreted.success_criteria or [])
            )
            return _scenario_a_scope(note, max_recovery_attempts=2)
        if entities.get("company") == "Acme Corp":
            note = any(
                (c.expected_value or {}).get("path") == "acme_billing_summary.txt"
                if isinstance(c.expected_value, dict)
                else False
                for c in (interpreted.success_criteria or [])
            )
            return _scenario_b_scope(note, max_recovery_attempts=2)
    raise AuthorizationError(
        AuthorizationReason.LEGACY_UNMAPPED,
        "Legacy task cannot be mapped to a trusted authorization scope.",
    )


def reference_targets_constraint(
    arguments: dict[str, Any],
    argument_name: str,
    constraint: FromStepOutputConstraint,
) -> bool:
    """True when the planned argument is a $ref to exactly the constrained output."""
    value = arguments.get(argument_name)
    try:
        refs = list(find_references(value)) if value is not None else []
    except Exception:  # noqa: BLE001 - treat malformed as not matching
        return False
    if len(refs) != 1:
        try:
            ref = parse_reference(value) if isinstance(value, dict) else None
        except Exception:  # noqa: BLE001
            return False
        if ref is None:
            return False
        return ref.step_id == constraint.step_id and list(ref.path) == list(constraint.path)
    try:
        ref = parse_reference(value)
    except Exception:  # noqa: BLE001
        return False
    return ref is not None and ref.step_id == constraint.step_id and list(ref.path) == list(
        constraint.path
    )


def describe_supported_goal(goal: SupportedGoal) -> str:
    return f"scenario_{goal.scenario}_note={goal.note_requested}"
