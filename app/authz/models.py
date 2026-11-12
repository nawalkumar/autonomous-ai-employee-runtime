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


class FromCollectionConstraint(BaseModel):
    """Argument must be an item id from a sealed collection snapshot.

    The collection is observed from ``source_step_id``; membership is checked
    against the durable snapshot, not against live DB queries. This is not a
    wildcard over all tickets.
    """

    type: Literal["from_collection"] = "from_collection"
    source_step_id: str = Field(min_length=1, max_length=128)


ArgumentConstraint = Annotated[
    Union[
        ExactValueConstraint,
        AllowedValuesConstraint,
        IntRangeConstraint,
        FieldKeysConstraint,
        FromStepOutputConstraint,
        FromCollectionConstraint,
    ],
    Field(discriminator="type"),
]


class ConstraintShape(BaseModel):
    """One complete argument-shape alternative for an operation.

    A step matches a shape only when every listed constraint is present and
    satisfied on that step. Shapes are OR alternatives: a step may match any
    one shape. Empty shapes are forbidden — use an absent alternatives entry
    (and empty flat constraints) for unconstrained allowlisted operations.
    """

    constraints: dict[str, ArgumentConstraint] = Field(min_length=1)


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
    # operation key ("tool.operation") -> argument name -> constraint (AND).
    # Every executable step of the operation must satisfy the full map.
    constraints: dict[str, dict[str, ArgumentConstraint]] = Field(default_factory=dict)
    # Optional OR of complete shapes when an operation legitimately has more
    # than one argument form (e.g. find_customer by company vs by customer_id).
    # When present for an operation, ``constraints`` for that key is ignored.
    constraint_alternatives: dict[str, list[ConstraintShape]] = Field(default_factory=dict)
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

    @model_validator(mode="after")
    def _alternatives_nonempty(self) -> AuthorizationScope:
        for op_key, shapes in self.constraint_alternatives.items():
            if not shapes:
                raise ValueError(
                    f"constraint_alternatives for {op_key!r} must list at least one shape"
                )
        return self

    def allows_operation(self, tool: str, operation: str) -> bool:
        return f"{tool}.{operation}" in self.allowed_operations

    def constraints_for(self, tool: str, operation: str) -> dict[str, ArgumentConstraint]:
        """Flat AND constraints for ``tool.operation`` (empty when alternatives apply)."""
        key = f"{tool}.{operation}"
        if key in self.constraint_alternatives:
            return {}
        return dict(self.constraints.get(key) or {})

    def shapes_for(self, tool: str, operation: str) -> list[dict[str, ArgumentConstraint]]:
        """Constraint shapes a step of this operation must match (OR).

        - Alternatives present → each alternative's constraint map.
        - Else flat ``constraints`` → a single shape (possibly empty = unconstrained).
        """
        key = f"{tool}.{operation}"
        alts = self.constraint_alternatives.get(key)
        if alts:
            return [dict(shape.constraints) for shape in alts]
        flat = self.constraints.get(key)
        if flat:
            return [dict(flat)]
        return [{}]

    def fingerprint(self) -> str:
        """Stable digest used to detect silent scope changes on resume."""
        import hashlib
        import json

        payload = self.model_dump(mode="json")
        # Omit empty alternatives so scopes persisted before this field keep
        # the same fingerprint after reload.
        if not payload.get("constraint_alternatives"):
            payload.pop("constraint_alternatives", None)
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
    _validate_constraint_maps(raw.get("constraints"), label="constraints")
    alternatives = raw.get("constraint_alternatives")
    if alternatives is not None:
        if not isinstance(alternatives, dict):
            raise AuthorizationError(
                AuthorizationReason.MALFORMED_AUTHORIZATION,
                "Authorization constraint_alternatives must be an object.",
            )
        for op_key, shapes in alternatives.items():
            if not isinstance(shapes, list) or not shapes:
                raise AuthorizationError(
                    AuthorizationReason.MALFORMED_AUTHORIZATION,
                    f"constraint_alternatives for {op_key!r} must be a non-empty list.",
                )
            for index, shape in enumerate(shapes):
                if not isinstance(shape, dict):
                    raise AuthorizationError(
                        AuthorizationReason.MALFORMED_AUTHORIZATION,
                        f"constraint_alternatives[{op_key!r}][{index}] must be an object.",
                    )
                shape_constraints = shape.get("constraints", shape)
                # Accept either {"constraints": {...}} or a bare constraint map.
                if "constraints" in shape:
                    _validate_constraint_maps(
                        {op_key: shape["constraints"]},
                        label=f"constraint_alternatives[{op_key}][{index}]",
                    )
                else:
                    _validate_constraint_maps(
                        {op_key: shape_constraints},
                        label=f"constraint_alternatives[{op_key}][{index}]",
                    )
    try:
        # Normalize bare alternative maps into ConstraintShape objects.
        normalized = dict(raw)
        if isinstance(alternatives, dict):
            fixed: dict[str, list[dict[str, Any]]] = {}
            for op_key, shapes in alternatives.items():
                fixed_shapes: list[dict[str, Any]] = []
                for shape in shapes:
                    if isinstance(shape, dict) and "constraints" in shape:
                        fixed_shapes.append(shape)
                    elif isinstance(shape, dict):
                        fixed_shapes.append({"constraints": shape})
                    else:
                        fixed_shapes.append(shape)
                fixed[op_key] = fixed_shapes
            normalized["constraint_alternatives"] = fixed
        return AuthorizationScope.model_validate(normalized)
    except ValidationError as exc:
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_AUTHORIZATION,
            f"Authorization scope failed validation: {exc.error_count()} error(s).",
        ) from exc


_CONSTRAINT_TYPES = frozenset(
    {
        "exact",
        "allowed_values",
        "int_range",
        "field_keys",
        "from_step_output",
        "from_collection",
    }
)


def _validate_constraint_maps(constraints: Any, *, label: str) -> None:
    if constraints is None:
        return
    if not isinstance(constraints, dict):
        raise AuthorizationError(
            AuthorizationReason.MALFORMED_AUTHORIZATION,
            f"Authorization {label} must be an object.",
        )
    for op_key, args in constraints.items():
        if not isinstance(args, dict):
            raise AuthorizationError(
                AuthorizationReason.MALFORMED_AUTHORIZATION,
                f"{label} for {op_key!r} must be an object.",
            )
        for arg_name, constraint in args.items():
            if not isinstance(constraint, dict) or "type" not in constraint:
                raise AuthorizationError(
                    AuthorizationReason.MALFORMED_AUTHORIZATION,
                    f"Constraint for {op_key}.{arg_name} is malformed.",
                )
            ctype = constraint.get("type")
            if ctype not in _CONSTRAINT_TYPES:
                raise AuthorizationError(
                    AuthorizationReason.UNSUPPORTED_CONSTRAINT,
                    f"Unsupported constraint type {ctype!r} on {op_key}.{arg_name}.",
                    details={
                        "operation": str(op_key),
                        "argument": str(arg_name),
                        "constraint_type": ctype,
                    },
                )
