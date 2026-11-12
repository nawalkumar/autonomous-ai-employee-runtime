"""Runtime authorization after reference binding, immediately before tool use."""

from __future__ import annotations

import re
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
    parse_authorization_scope,
)
from app.authz.origin import legacy_authorization_for_state, operation_key
from app.authz.plan_check import _check_literal
from app.models.references import BindingError, contains_reference, producer_output, read_path
from app.models.state import ExecutionState
from app.tools.capabilities import CAPABILITIES, operation_of

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Argument names treated as entity identifiers for distinguishable reason codes.
_ENTITY_ARGUMENTS = frozenset(
    {
        "employee_id",
        "customer_id",
        "ticket_id",
        "assignee_id",
        "company",
        "email",
        "path",
    }
)


def validate_authorization_fingerprint(
    scope: AuthorizationScope, fingerprint: Any
) -> None:
    """Require a valid fingerprint that matches ``scope``. Never repair it."""
    if fingerprint is None or fingerprint == "":
        raise AuthorizationError(
            AuthorizationReason.MISSING_FINGERPRINT,
            "Authorization scope is present but its fingerprint is missing.",
            details={"fingerprint_present": False},
        )
    if not isinstance(fingerprint, str) or not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_FINGERPRINT,
            "Authorization fingerprint is malformed.",
            details={"fingerprint_present": True},
        )
    expected = scope.fingerprint()
    if fingerprint != expected:
        raise AuthorizationError(
            AuthorizationReason.FINGERPRINT_MISMATCH,
            "Persisted authorization fingerprint does not match the scope.",
            details={"fingerprint_present": True},
        )


def ensure_task_authorization(state: ExecutionState) -> AuthorizationScope:
    """Load and validate the task's persisted scope; map legacy tasks narrowly.

    A persisted scope without a valid matching fingerprint fails closed. Missing
    fingerprints are never reconstructed from the untrusted scope on resume.
    """
    raw = getattr(state, "authorization", None)
    fingerprint = getattr(state, "authorization_fingerprint", None)
    if raw is None:
        if fingerprint is not None and fingerprint != "":
            raise AuthorizationError(
                AuthorizationReason.MALFORMED_AUTHORIZATION,
                "Authorization fingerprint is present without a scope.",
                details={"fingerprint_present": True},
            )
        # Independently trusted legacy mapping only — never a permissive default.
        return legacy_authorization_for_state(state)
    try:
        scope = parse_authorization_scope(raw)
    except AuthorizationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_AUTHORIZATION,
            f"Persisted authorization scope could not be loaded: {exc}",
        ) from exc
    validate_authorization_fingerprint(scope, fingerprint)
    return scope


def authorize_action(
    state: ExecutionState,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    scope: AuthorizationScope | None = None,
    step_id: str | None = None,
) -> AuthorizationScope:
    """Authorize one concrete resolved action against the task scope.

    Must run after ``prepare_arguments`` and before approval / tool invocation.
    Counts budgets against durable ``tool_calls`` so restarts cannot reset spend.
    When ``scope`` is passed it must already be the trusted persisted scope; the
    fingerprint is still verified against the task state.
    """
    auth = ensure_task_authorization(state)
    if scope is not None and scope.fingerprint() != auth.fingerprint():
        raise AuthorizationError(
            AuthorizationReason.FINGERPRINT_MISMATCH,
            "Caller-supplied authorization does not match the persisted task scope.",
            details={"fingerprint_present": True},
        )
    if contains_reference(arguments):
        raise AuthorizationError(
            AuthorizationReason.REFERENCE_UNRESOLVED,
            "Resolved arguments still contain references; refusing to authorize.",
            details={"step_id": step_id or "", "operation": tool_name},
        )

    operation = operation_of(arguments)
    if not isinstance(operation, str) or not operation:
        raise AuthorizationError(
            AuthorizationReason.UNKNOWN_OPERATION,
            "Action is missing a concrete operation.",
            details={"step_id": step_id or "", "operation": str(operation)},
        )
    if CAPABILITIES.find(tool_name, operation) is None:
        raise AuthorizationError(
            AuthorizationReason.UNKNOWN_OPERATION,
            f"Unknown operation {tool_name}.{operation}.",
            details={"operation": operation_key(tool_name, operation), "step_id": step_id or ""},
        )
    if not auth.allows_operation(tool_name, operation):
        raise AuthorizationError(
            AuthorizationReason.OPERATION_UNAUTHORIZED,
            f"Operation {tool_name}.{operation} is outside the task authorization scope.",
            details={
                "operation": operation_key(tool_name, operation),
                "step_id": step_id or "",
                "allowed_operations": list(auth.allowed_operations),
                "kind": "operation",
            },
        )

    _check_budgets(state, auth, tool_name, operation)
    _authorize_resolved_arguments(state, auth, tool_name, operation, arguments, step_id)
    return auth


def _check_budgets(
    state: ExecutionState,
    scope: AuthorizationScope,
    tool_name: str,
    operation: str,
) -> None:
    calls = list(state.tool_calls or [])
    if len(calls) >= scope.budgets.max_tool_calls:
        raise AuthorizationError(
            AuthorizationReason.BUDGET_EXCEEDED,
            f"Tool-call budget exhausted ({scope.budgets.max_tool_calls}).",
            details={
                "budget": "max_tool_calls",
                "limit": scope.budgets.max_tool_calls,
                "observed": len(calls),
                "kind": "budget",
            },
        )
    capability = CAPABILITIES.find(tool_name, operation)
    if capability and capability.mutating:
        mutating_attempts = sum(
            1
            for call in calls
            if _call_is_mutating(call.tool_name, call.arguments)
        )
        if mutating_attempts >= scope.budgets.max_mutations:
            raise AuthorizationError(
                AuthorizationReason.BUDGET_EXCEEDED,
                f"Mutation budget exhausted ({scope.budgets.max_mutations}).",
                details={
                    "budget": "max_mutations",
                    "limit": scope.budgets.max_mutations,
                    "observed": mutating_attempts,
                    "kind": "budget",
                },
            )


def _call_is_mutating(tool_name: str, arguments: dict[str, Any] | None) -> bool:
    op = operation_of(arguments or {})
    capability = CAPABILITIES.find(tool_name, op)
    return bool(capability and capability.mutating)


def _authorize_resolved_arguments(
    state: ExecutionState,
    scope: AuthorizationScope,
    tool: str,
    operation: str,
    arguments: dict[str, Any],
    step_id: str | None,
) -> None:
    constraints = scope.constraints_for(tool, operation)
    for arg_name, constraint in constraints.items():
        if "." in arg_name:
            root, _, nested = arg_name.partition(".")
            parent = arguments.get(root)
            if not isinstance(parent, dict):
                raise AuthorizationError(
                    AuthorizationReason.FIELD_UNAUTHORIZED,
                    f"Argument '{root}' must be an object for constraint '{arg_name}'.",
                    details={
                        "operation": operation_key(tool, operation),
                        "argument": arg_name,
                        "step_id": step_id or "",
                        "kind": "field",
                    },
                )
            if nested not in parent:
                continue
            _apply_constraint(
                state,
                scope,
                tool,
                operation,
                arg_name,
                parent[nested],
                constraint,
                step_id=step_id,
            )
            continue

        if arg_name not in arguments:
            continue
        _apply_constraint(
            state,
            scope,
            tool,
            operation,
            arg_name,
            arguments[arg_name],
            constraint,
            step_id=step_id,
        )


def _violation_reason(arg_name: str, constraint: ArgumentConstraint) -> AuthorizationReason:
    if isinstance(constraint, FieldKeysConstraint) or "." in arg_name:
        return AuthorizationReason.FIELD_UNAUTHORIZED
    root = arg_name.split(".", 1)[0]
    if root in _ENTITY_ARGUMENTS:
        return AuthorizationReason.ENTITY_UNAUTHORIZED
    return AuthorizationReason.ACTION_UNAUTHORIZED


def _apply_constraint(
    state: ExecutionState,
    scope: AuthorizationScope,
    tool: str,
    operation: str,
    arg_name: str,
    value: Any,
    constraint: ArgumentConstraint,
    *,
    step_id: str | None,
) -> None:
    if isinstance(constraint, FromStepOutputConstraint):
        try:
            data = producer_output(state, constraint.step_id)
            expected = read_path(
                data, tuple(constraint.path), f"step '{constraint.step_id}' output"
            )
        except BindingError as exc:
            raise AuthorizationError(
                AuthorizationReason.ENTITY_UNAUTHORIZED,
                f"Cannot authorize '{arg_name}' from step '{constraint.step_id}': {exc.message}",
                details={
                    "operation": operation_key(tool, operation),
                    "argument": arg_name,
                    "step_id": step_id or "",
                    "kind": "entity",
                },
            ) from exc
        producer = next((s for s in state.plan if s.step_id == constraint.step_id), None)
        if producer is None:
            raise AuthorizationError(
                AuthorizationReason.ENTITY_UNAUTHORIZED,
                f"Authorized producer step '{constraint.step_id}' is missing.",
                details={
                    "step_id": step_id or "",
                    "argument": arg_name,
                    "kind": "entity",
                },
            )
        producer_tool = producer.resolved_tool_name() or ""
        producer_op = operation_of(producer.arguments)
        if not isinstance(producer_op, str) or not scope.allows_operation(
            producer_tool, producer_op
        ):
            raise AuthorizationError(
                AuthorizationReason.OPERATION_UNAUTHORIZED,
                f"Producer step '{constraint.step_id}' is not within authorization scope.",
                details={"step_id": step_id or "", "argument": arg_name, "kind": "operation"},
            )
        if value != expected:
            raise AuthorizationError(
                AuthorizationReason.ENTITY_UNAUTHORIZED,
                f"Argument '{arg_name}' does not match the authorized producer output.",
                details={
                    "operation": operation_key(tool, operation),
                    "argument": arg_name,
                    "step_id": step_id or "",
                    "kind": "entity",
                },
            )
        return

    if isinstance(
        constraint,
        (
            ExactValueConstraint,
            AllowedValuesConstraint,
            IntRangeConstraint,
            FieldKeysConstraint,
        ),
    ):
        try:
            _check_literal(
                step_id or "",
                tool,
                operation,
                arg_name,
                value,
                constraint,
                plan_time=False,
            )
        except AuthorizationError as exc:
            reason = _violation_reason(arg_name, constraint)
            kind = (
                "field"
                if reason is AuthorizationReason.FIELD_UNAUTHORIZED
                else "entity"
                if reason is AuthorizationReason.ENTITY_UNAUTHORIZED
                else "value"
            )
            raise AuthorizationError(
                reason,
                exc.message,
                details={
                    **exc.details,
                    "kind": kind,
                    "argument": arg_name,
                    "operation": operation_key(tool, operation),
                    "step_id": step_id or "",
                },
            ) from exc
        return

    raise AuthorizationError(
        AuthorizationReason.UNSUPPORTED_CONSTRAINT,
        f"Unsupported constraint type on {operation_key(tool, operation)}.{arg_name}.",
        details={
            "operation": operation_key(tool, operation),
            "argument": arg_name,
            "constraint_type": type(constraint).__name__,
            "step_id": step_id or "",
        },
    )
