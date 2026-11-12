"""Versioned authorization contract persisted with each task.

Small typed constraints only — no regex, expressions, or policy languages.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.authz.errors import AuthorizationError, AuthorizationReason

SCOPE_VERSION = 1


class ExactValueConstraint(BaseModel):
    """Argument must equal ``value`` exactly (JSON-comparable)."""

    type: Literal["exact"] = "exact"
    value: Any


class AllowedValuesConstraint(BaseModel):
    """Argument must be one of the listed values."""

    type: Literal["allowed_values"] = "allowed_values"
    values: list[Any] = Field(min_length=1)

    @field_validator("values")
    @classmethod
    def _unique(cls, values: list[Any]) -> list[Any]:
        # Preserve order; reject empties already handled by min_length.
        return list(values)


class IntRangeConstraint(BaseModel):
    """Integer argument must fall within an inclusive range."""

    type: Literal["int_range"] = "int_range"
    min_value: int | None = None
    max_value: int | None = None

    @model_validator(mode="after")
    def _bounds(self) -> IntRangeConstraint:
        if self.min_value is None and self.max_value is None:
            raise ValueError("int_range requires min_value and/or max_value")
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value > self.max_value
        ):
            raise ValueError("int_range min_value cannot exceed max_value")
        return self


class FieldKeysConstraint(BaseModel):
    """Dictionary argument may contain only the listed keys (e.g. update fields)."""

    type: Literal["field_keys"] = "field_keys"
    allowed_keys: list[str] = Field(min_length=1)

    @field_validator("allowed_keys")
    @classmethod
    def _keys(cls, keys: list[str]) -> list[str]:
        cleaned = [k for k in keys if isinstance(k, str) and k]
        if not cleaned:
            raise ValueError("field_keys requires at least one non-empty key")
        return cleaned


class FromStepOutputConstraint(BaseModel):
    """Argument must equal the successful output of an earlier authorized step.

    Used for narrowly scoped workflows such as creating a ticket for the
    customer ID produced by an authorized ``create_customer`` step — not a
    wildcard over all customer IDs.
    """

    type: Literal["from_step_output"] = "from_step_output"
    step_id: str = Field(min_length=1, max_length=128)
    path: list[str | int] = Field(min_length=1, max_length=8)


ArgumentConstraint = Annotated[
    Union[
        ExactValueConstraint,
        AllowedValuesConstraint,
        IntRangeConstraint,
        FieldKeysConstraint,
        FromStepOutputConstraint,
    ],
    Field(discriminator="type"),
]


class AuthorizationBudgets(BaseModel):
    """Hard caps on plan shape and actual tool invocations.

    Semantics:
    - ``max_steps``: number of plan steps (checked at plan authorization).
    - ``max_mutations``: number of mutating plan steps (plan-time) and number of
      mutating tool-call *attempts* including failures and retries (runtime).
      Read-only attempts do not count. Authorization denials before a tool
      attempt do not count.
    - ``max_tool_calls``: number of actual tool-call attempts recorded on the
      task (every RUNNING→SUCCEEDED/FAILED record), including reads, writes,
      failures and retries. Rejected authorization attempts that never create a
      tool-call record do not count; the task fails closed instead of retrying.
    """

    max_steps: int = Field(ge=1, le=100)
    max_mutations: int = Field(ge=0, le=100)
    max_tool_calls: int = Field(ge=1, le=500)


class AuthorizationScope(BaseModel):
    """Immutable authorization contract bound to a task after creation."""

    version: Literal[1] = SCOPE_VERSION
    allowed_operations: list[str] = Field(min_length=1)
    # operation key ("tool.operation") -> argument name -> constraint
    constraints: dict[str, dict[str, ArgumentConstraint]] = Field(default_factory=dict)
    budgets: AuthorizationBudgets

    @field_validator("allowed_operations")
    @classmethod
    def _ops(cls, ops: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for op in ops:
            if not isinstance(op, str) or "." not in op or op in seen:
                continue
            tool, _, operation = op.partition(".")
            if not tool or not operation or "." in operation:
                continue
            seen.add(op)
            cleaned.append(op)
        if not cleaned:
            raise ValueError("allowed_operations must list at least one tool.operation")
        return cleaned

    def allows_operation(self, tool: str, operation: str) -> bool:
        return f"{tool}.{operation}" in self.allowed_operations

    def constraints_for(self, tool: str, operation: str) -> dict[str, ArgumentConstraint]:
        return dict(self.constraints.get(f"{tool}.{operation}") or {})

    def fingerprint(self) -> str:
        """Stable digest used to detect silent scope changes on resume."""
        import hashlib
        import json

        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


def parse_authorization_scope(raw: Any) -> AuthorizationScope:
    """Parse a persisted scope; fail closed on unknown versions or shapes."""
    if raw is None:
        raise AuthorizationError(
            AuthorizationReason.MISSING_AUTHORIZATION,
            "Task has no authorization scope.",
        )
    if isinstance(raw, AuthorizationScope):
        return raw
    if not isinstance(raw, dict):
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_AUTHORIZATION,
            "Authorization scope is not an object.",
        )
    version = raw.get("version")
    if version != SCOPE_VERSION:
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_AUTHORIZATION,
            f"Unsupported authorization scope version: {version!r}",
            details={"version": version},
        )
    # Reject unknown constraint types before pydantic unions soften the error.
    constraints = raw.get("constraints")
    if constraints is not None:
        if not isinstance(constraints, dict):
            raise AuthorizationError(
                AuthorizationReason.MALFORMED_AUTHORIZATION,
                "Authorization constraints must be an object.",
            )
        for op_key, args in constraints.items():
            if not isinstance(args, dict):
                raise AuthorizationError(
                    AuthorizationReason.MALFORMED_AUTHORIZATION,
                    f"Constraints for {op_key!r} must be an object.",
                )
            for arg_name, constraint in args.items():
                if not isinstance(constraint, dict) or "type" not in constraint:
                    raise AuthorizationError(
                        AuthorizationReason.MALFORMED_AUTHORIZATION,
                        f"Constraint for {op_key}.{arg_name} is malformed.",
                    )
                ctype = constraint.get("type")
                if ctype not in {
                    "exact",
                    "allowed_values",
                    "int_range",
                    "field_keys",
                    "from_step_output",
                }:
                    raise AuthorizationError(
                        AuthorizationReason.UNSUPPORTED_CONSTRAINT,
                        f"Unsupported constraint type {ctype!r} on {op_key}.{arg_name}.",
                        details={
                            "operation": str(op_key),
                            "argument": str(arg_name),
                            "constraint_type": ctype,
                        },
                    )
    try:
        return AuthorizationScope.model_validate(raw)
    except ValidationError as exc:
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_AUTHORIZATION,
            f"Authorization scope failed validation: {exc.error_count()} error(s).",
        ) from exc
