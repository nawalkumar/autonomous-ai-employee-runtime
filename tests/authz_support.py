"""Test-only authorization helpers.

Build narrow scopes for explicit test plans. Production code must never import
this module — scopes for CLI/API come from ``authorization_for_supported_goal``.
"""

from __future__ import annotations

from typing import Any

from app.authz.errors import AuthorizationError, AuthorizationReason
from app.authz.models import (
    AllowedValuesConstraint,
    ArgumentConstraint,
    AuthorizationBudgets,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
)
from app.authz.origin import operation_key
from app.models.plan import PlanStep
from app.models.references import contains_reference, parse_reference
from app.tools.capabilities import CAPABILITIES, operation_of


def _step_operation(step: PlanStep) -> tuple[str, str] | None:
    tool = step.resolved_tool_name()
    if not tool:
        return None
    op = operation_of(step.arguments)
    if not isinstance(op, str) or not op:
        return None
    return tool, op


def _is_mutating(tool: str, operation: str) -> bool:
    capability = CAPABILITIES.find(tool, operation)
    return bool(capability and capability.mutating)


def default_budgets_for_plan(
    steps: list[PlanStep],
    *,
    max_recovery_attempts: int = 2,
) -> AuthorizationBudgets:
    """Budgets sized to the plan: steps, mutations, and retry-inclusive calls.

    Mutation and tool-call caps include the per-step recovery budget so a single
    transient failure + retry of an authorized mutating step does not exhaust
    authorization. Unlimited retries remain impossible because both caps are finite
    and authorization denials fail closed without creating further attempts.
    """
    mutating = 0
    for step in steps:
        resolved = _step_operation(step)
        if resolved and _is_mutating(*resolved):
            mutating += 1
    attempts_per_step = max(1, 1 + max(0, max_recovery_attempts))
    return AuthorizationBudgets(
        max_steps=max(1, len(steps)),
        max_mutations=max(0, mutating * attempts_per_step),
        max_tool_calls=max(1, len(steps) * attempts_per_step),
    )


def matching_scope_for_plan(
    steps: list[PlanStep],
    *,
    budgets: AuthorizationBudgets | None = None,
    max_recovery_attempts: int = 2,
    extra_constraints: dict[str, dict[str, ArgumentConstraint]] | None = None,
) -> AuthorizationScope:
    """Build a narrow scope from plan literals — for trusted internal callers only.

    This is the independently supplied scope for explicit test plans. It must
    never be invoked by the planner or on untrusted public input. Reference-
    bearing arguments are not turned into concrete allowlists; callers that
    need ``from_step_output`` (e.g. W8) must pass ``extra_constraints``.
    """
    allowed: list[str] = []
    constraints: dict[str, dict[str, ArgumentConstraint]] = {}
    for step in steps:
        resolved = _step_operation(step)
        if resolved is None:
            continue
        tool, operation = resolved
        key = operation_key(tool, operation)
        if key not in allowed:
            allowed.append(key)
        if contains_reference(step.arguments):
            # Narrowly authorize each top-level $ref as from_step_output of that
            # exact producer path — never a wildcard entity permission.
            ref_constraints = _reference_constraints(step.arguments)
            if ref_constraints:
                existing = constraints.setdefault(key, {})
                for name, constraint in ref_constraints.items():
                    if name not in existing:
                        existing[name] = constraint
            # Also constrain any sibling literal entity fields.
            for name, constraint in _literal_constraints(tool, operation, step.arguments).items():
                if name not in constraints.setdefault(key, {}):
                    constraints[key][name] = constraint
            continue
        arg_constraints = _literal_constraints(tool, operation, step.arguments)
        if arg_constraints:
            existing = constraints.setdefault(key, {})
            for name, constraint in arg_constraints.items():
                # Keep the first constraint; conflicting literals across steps
                # for the same operation widen via allowed_values when needed.
                if name not in existing:
                    existing[name] = constraint
                else:
                    existing[name] = _widen(existing[name], constraint)
    if extra_constraints:
        for key, args in extra_constraints.items():
            if key not in allowed:
                allowed.append(key)
            bucket = constraints.setdefault(key, {})
            bucket.update(args)
    if not allowed:
        raise AuthorizationError(
            AuthorizationReason.MISSING_AUTHORIZATION,
            "Cannot build a scope from a plan with no recognized operations.",
        )
    return AuthorizationScope(
        allowed_operations=allowed,
        constraints=constraints,
        budgets=budgets
        or default_budgets_for_plan(steps, max_recovery_attempts=max_recovery_attempts),
    )


def _widen(
    left: ArgumentConstraint, right: ArgumentConstraint
) -> ArgumentConstraint:
    if isinstance(left, ExactValueConstraint) and isinstance(right, ExactValueConstraint):
        if left.value == right.value:
            return left
        return AllowedValuesConstraint(values=[left.value, right.value])
    if isinstance(left, AllowedValuesConstraint) and isinstance(right, ExactValueConstraint):
        if right.value in left.values:
            return left
        return AllowedValuesConstraint(values=[*left.values, right.value])
    if isinstance(left, ExactValueConstraint) and isinstance(right, AllowedValuesConstraint):
        return _widen(right, left)
    if isinstance(left, AllowedValuesConstraint) and isinstance(right, AllowedValuesConstraint):
        merged = list(left.values)
        for value in right.values:
            if value not in merged:
                merged.append(value)
        return AllowedValuesConstraint(values=merged)
    if isinstance(left, FieldKeysConstraint) and isinstance(right, FieldKeysConstraint):
        keys = list(dict.fromkeys([*left.allowed_keys, *right.allowed_keys]))
        return FieldKeysConstraint(allowed_keys=keys)
    # Incompatible widen: keep left (fail-closed later if plan violates).
    return left


def _reference_constraints(arguments: dict[str, Any]) -> dict[str, ArgumentConstraint]:
    """Map top-level argument $ref objects to from_step_output constraints."""
    result: dict[str, ArgumentConstraint] = {}
    for name, value in arguments.items():
        if name == "operation":
            continue
        try:
            ref = parse_reference(value)
        except Exception:  # noqa: BLE001
            continue
        if ref is None:
            continue
        result[name] = FromStepOutputConstraint(
            step_id=ref.step_id, path=list(ref.path)
        )
    return result


def _literal_constraints(
    tool: str, operation: str, arguments: dict[str, Any]
) -> dict[str, ArgumentConstraint]:
    """Entity/field constraints inferred from literal arguments for trusted helpers."""
    result: dict[str, ArgumentConstraint] = {}
    if tool != "company_api":
        if operation == "write" and isinstance(arguments.get("path"), str):
            result["path"] = ExactValueConstraint(value=arguments["path"])
        return result

    if operation in {"update_employee", "get_employee", "deactivate_employee"}:
        employee_id = arguments.get("employee_id")
        if isinstance(employee_id, str):
            result["employee_id"] = ExactValueConstraint(value=employee_id)
        fields = arguments.get("fields")
        if (
            operation == "update_employee"
            and isinstance(fields, dict)
            and not contains_reference(fields)
        ):
            # Field-key allowlist only: exact field *values* are for trusted goal
            # derivation (scenario A), not the generic matching helper. Stale-approval
            # tests may change a title while remaining on the same authorized fields.
            result["fields"] = FieldKeysConstraint(allowed_keys=sorted(str(k) for k in fields))

    if operation in {"create_ticket", "find_customer"}:
        customer_id = arguments.get("customer_id")
        if isinstance(customer_id, str):
            result["customer_id"] = ExactValueConstraint(value=customer_id)
        company = arguments.get("company")
        if isinstance(company, str) and operation == "find_customer":
            result["company"] = ExactValueConstraint(value=company)

    if operation in {"get_ticket", "update_ticket", "assign_ticket"}:
        ticket_id = arguments.get("ticket_id")
        if isinstance(ticket_id, str):
            result["ticket_id"] = ExactValueConstraint(value=ticket_id)
        fields = arguments.get("fields")
        if (
            operation == "update_ticket"
            and isinstance(fields, dict)
            and not contains_reference(fields)
        ):
            result["fields"] = FieldKeysConstraint(allowed_keys=sorted(str(k) for k in fields))

    if operation == "assign_ticket":
        assignee_id = arguments.get("assignee_id")
        if isinstance(assignee_id, str):
            result["assignee_id"] = ExactValueConstraint(value=assignee_id)

    return result


