"""Plan-time authorization: reject unauthorized plans before any side effects."""

from __future__ import annotations

from typing import Any

from app.authz.errors import AuthorizationError, AuthorizationReason
from app.authz.models import (
    AllowedValuesConstraint,
    ArgumentConstraint,
    AuthorizationScope,
    ExactValueConstraint,
    FieldKeysConstraint,
    FromStepOutputConstraint,
    IntRangeConstraint,
)
from app.authz.origin import operation_key, reference_targets_constraint
from app.models.plan import Plan, PlanStep
from app.models.references import contains_reference
from app.tools.capabilities import CAPABILITIES, operation_of


def authorize_plan(plan: Plan | list[PlanStep], scope: AuthorizationScope) -> None:
    """Validate every step against ``scope``. Raises AuthorizationError on denial.

    Operation and argument checks run before budget checks so an unauthorized
    extra action surfaces as ``PLAN_UNAUTHORIZED`` rather than a step-count miss.
    """
    steps = plan.steps if isinstance(plan, Plan) else list(plan)

    mutating_steps = 0
    for step in steps:
        tool = step.resolved_tool_name()
        if not tool:
            raise AuthorizationError(
                AuthorizationReason.PLAN_UNAUTHORIZED,
                f"Step '{step.step_id}' has no tool; plan is unauthorized.",
                details={"step_id": step.step_id},
            )
        operation = operation_of(step.arguments)
        if not isinstance(operation, str) or not operation:
            raise AuthorizationError(
                AuthorizationReason.UNKNOWN_OPERATION,
                f"Step '{step.step_id}' is missing a concrete operation.",
                details={"step_id": step.step_id, "operation": str(operation)},
            )
        if CAPABILITIES.find(tool, operation) is None:
            raise AuthorizationError(
                AuthorizationReason.UNKNOWN_OPERATION,
                f"Unknown operation {tool}.{operation} in step '{step.step_id}'.",
                details={"operation": operation_key(tool, operation), "step_id": step.step_id},
            )
        if not scope.allows_operation(tool, operation):
            raise AuthorizationError(
                AuthorizationReason.PLAN_UNAUTHORIZED,
                f"Operation {tool}.{operation} in step '{step.step_id}' is outside "
                "the task authorization scope.",
                details={
                    "operation": operation_key(tool, operation),
                    "step_id": step.step_id,
                    "allowed_operations": list(scope.allowed_operations),
                },
            )
        capability = CAPABILITIES.require(tool, operation)
        if capability.mutating:
            mutating_steps += 1

        _authorize_step_arguments(step, tool, operation, scope)

    if len(steps) > scope.budgets.max_steps:
        raise AuthorizationError(
            AuthorizationReason.BUDGET_EXCEEDED,
            f"Plan has {len(steps)} steps; authorization allows at most "
            f"{scope.budgets.max_steps}.",
            details={
                "budget": "max_steps",
                "limit": scope.budgets.max_steps,
                "observed": len(steps),
            },
        )

    if mutating_steps > scope.budgets.max_mutations:
        raise AuthorizationError(
            AuthorizationReason.BUDGET_EXCEEDED,
            f"Plan has {mutating_steps} mutating steps; authorization allows at most "
            f"{scope.budgets.max_mutations}.",
            details={
                "budget": "max_mutations",
                "limit": scope.budgets.max_mutations,
                "observed": mutating_steps,
            },
        )


def _authorize_step_arguments(
    step: PlanStep,
    tool: str,
    operation: str,
    scope: AuthorizationScope,
) -> None:
    constraints = scope.constraints_for(tool, operation)
    if not constraints:
        return
    arguments = step.arguments or {}
    has_refs = contains_reference(arguments)

    for arg_name, constraint in constraints.items():
        if "." in arg_name:
            # Nested field constraint (e.g. fields.title) — only when parent is concrete.
            root, _, nested = arg_name.partition(".")
            parent = arguments.get(root)
            if contains_reference({root: parent}) if parent is not None else False:
                # Defer nested checks until runtime after binding.
                continue
            if not isinstance(parent, dict):
                if has_refs and root not in arguments:
                    continue
                raise AuthorizationError(
                    AuthorizationReason.PLAN_UNAUTHORIZED,
                    f"Step '{step.step_id}' argument '{root}' must be an object for "
                    f"constraint '{arg_name}'.",
                    details={
                        "operation": operation_key(tool, operation),
                        "argument": arg_name,
                        "step_id": step.step_id,
                    },
                )
            if nested not in parent:
                # Field absent — fine for field-level exact constraints.
                continue
            if contains_reference(parent.get(nested)):
                continue
            _check_literal(
                step.step_id,
                tool,
                operation,
                arg_name,
                parent[nested],
                constraint,
                plan_time=True,
            )
            continue

        if arg_name not in arguments:
            # Optional args may be omitted; entity constraints on missing required
            # args are caught by schema validation elsewhere.
            continue

        value = arguments[arg_name]
        if contains_reference(value):
            if isinstance(constraint, FromStepOutputConstraint):
                if reference_targets_constraint(arguments, arg_name, constraint):
                    continue
                raise AuthorizationError(
                    AuthorizationReason.PLAN_UNAUTHORIZED,
                    f"Step '{step.step_id}' argument '{arg_name}' reference does not "
                    f"target authorized step '{constraint.step_id}' path {constraint.path}.",
                    details={
                        "operation": operation_key(tool, operation),
                        "argument": arg_name,
                        "step_id": step.step_id,
                    },
                )
            # Other constraints: do not treat the reference as an authorized concrete
            # value; runtime will authorize after binding.
            continue

        _check_literal(
            step.step_id, tool, operation, arg_name, value, constraint, plan_time=True
        )


def _check_literal(
    step_id: str,
    tool: str,
    operation: str,
    arg_name: str,
    value: Any,
    constraint: ArgumentConstraint,
    *,
    plan_time: bool,
) -> None:
    op = operation_key(tool, operation)
    details = {"operation": op, "argument": arg_name, "step_id": step_id}
    reason = (
        AuthorizationReason.PLAN_UNAUTHORIZED
        if plan_time
        else AuthorizationReason.ACTION_UNAUTHORIZED
    )

    if isinstance(constraint, ExactValueConstraint):
        if value != constraint.value:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' value is not authorized for {op}.",
                details=details,
            )
        return

    if isinstance(constraint, AllowedValuesConstraint):
        if value not in constraint.values:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' value is not in the authorized set for {op}.",
                details=details,
            )
        return

    if isinstance(constraint, IntRangeConstraint):
        if type(value) is not int:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' must be an integer for {op}.",
                details=details,
            )
        if constraint.min_value is not None and value < constraint.min_value:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' is below the authorized minimum for {op}.",
                details=details,
            )
        if constraint.max_value is not None and value > constraint.max_value:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' is above the authorized maximum for {op}.",
                details=details,
            )
        return

    if isinstance(constraint, FieldKeysConstraint):
        if not isinstance(value, dict):
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' must be an object for field-key constraints on {op}.",
                details=details,
            )
        extra = [key for key in value if key not in constraint.allowed_keys]
        if extra:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' includes unauthorized field(s) {extra} on {op}.",
                details=details,
            )
        return

    if isinstance(constraint, FromStepOutputConstraint):
        # Concrete literals are not authorized by a from_step_output constraint
        # at plan time; at runtime the resolved value must match the producer.
        if plan_time:
            raise AuthorizationError(
                reason,
                f"Argument '{arg_name}' must reference authorized step "
                f"'{constraint.step_id}' output; literal values are not permitted.",
                details=details,
            )
        return

    raise AuthorizationError(
        AuthorizationReason.UNSUPPORTED_CONSTRAINT,
        f"Unsupported constraint type on {op}.{arg_name}.",
        details={**details, "constraint_type": type(constraint).__name__},
    )
