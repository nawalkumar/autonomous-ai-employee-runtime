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
    ConstraintShape,
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

    Per-operation argument shapes that are incompatible (different constrained
    keys) become ``constraint_alternatives`` so each step must still fully match
    one explicit shape. Compatible shapes merge into a single AND map.
    """
    allowed: list[str] = []
    # operation -> list of per-step non-empty constraint maps
    shapes_by_op: dict[str, list[dict[str, ArgumentConstraint]]] = {}

    for step in steps:
        resolved = _step_operation(step)
        if resolved is None:
            continue
        tool, operation = resolved
        key = operation_key(tool, operation)
        if key not in allowed:
            allowed.append(key)

        shape: dict[str, ArgumentConstraint] = {}
        if contains_reference(step.arguments):
            shape.update(_reference_constraints(step.arguments))
            shape.update(_literal_constraints(tool, operation, step.arguments))
        else:
            shape.update(_literal_constraints(tool, operation, step.arguments))
        if shape:
            shapes_by_op.setdefault(key, []).append(shape)

    if extra_constraints:
        for key, args in extra_constraints.items():
            if key not in allowed:
                allowed.append(key)
            shapes_by_op.setdefault(key, []).append(dict(args))

    if not allowed:
        raise AuthorizationError(
            AuthorizationReason.MISSING_AUTHORIZATION,
            "Cannot build a scope from a plan with no recognized operations.",
        )

    constraints: dict[str, dict[str, ArgumentConstraint]] = {}
    alternatives: dict[str, list[ConstraintShape]] = {}
    for key, shapes in shapes_by_op.items():
        merged_or_alts = _merge_or_alternatives(shapes)
        if isinstance(merged_or_alts, list):
            alternatives[key] = [
                ConstraintShape(constraints=shape) for shape in merged_or_alts
            ]
        else:
            constraints[key] = merged_or_alts

    return AuthorizationScope(
        allowed_operations=allowed,
        constraints=constraints,
        constraint_alternatives=alternatives,
        budgets=budgets
        or default_budgets_for_plan(steps, max_recovery_attempts=max_recovery_attempts),
    )


def _shape_key_set(shape: dict[str, ArgumentConstraint]) -> frozenset[str]:
    return frozenset(shape)


def _merge_or_alternatives(
    shapes: list[dict[str, ArgumentConstraint]],
) -> dict[str, ArgumentConstraint] | list[dict[str, ArgumentConstraint]]:
    """Merge compatible shapes; otherwise return distinct non-empty alternatives."""
    if not shapes:
        return {}
    key_sets = {_shape_key_set(shape) for shape in shapes}
    if len(key_sets) == 1:
        merged: dict[str, ArgumentConstraint] = {}
        for shape in shapes:
            for name, constraint in shape.items():
                if name not in merged:
                    merged[name] = constraint
                else:
                    merged[name] = _widen(merged[name], constraint)
        return merged

    # Incompatible key sets: keep unique shapes as OR alternatives (no empty).
    unique: list[dict[str, ArgumentConstraint]] = []
    seen: set[str] = set()
    for shape in shapes:
        if not shape:
            continue
        marker = repr(sorted((k, c.model_dump(mode="json")) for k, c in shape.items()))
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(dict(shape))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        return {}
    return unique


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

    if operation == "list_tickets":
        filters = arguments.get("filters")
        if isinstance(filters, dict) and not contains_reference(filters):
            result["filters"] = ExactValueConstraint(value=dict(filters))

    return result
