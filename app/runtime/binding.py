"""Resolve a step's declared arguments into the concrete arguments it runs with.

Literal-only steps are returned unchanged and unvalidated here (the tool
validates them on invocation, exactly as before). For a step with references,
every reference is read from an earlier completed step's persisted output, the
values are substituted into a copy of the arguments, and the complete result is
validated against the tool's argument schema. The plan itself is never changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.models.enums import FailureType
from app.models.plan import PlanStep
from app.models.references import (
    BindingError,
    ReferenceSyntaxError,
    contains_reference,
    find_references,
    location_text,
    producer_output,
    read_path,
    substitute,
)
from app.models.state import ExecutionState
from app.tools.base import BaseTool


@dataclass(frozen=True)
class PreparedArguments:
    arguments: dict[str, Any]
    # [{"location": [...], "step_id": "...", "path": [...]}]; values live in arguments.
    references: list[dict[str, Any]] = field(default_factory=list)


def prepare_arguments(
    state: ExecutionState, step: PlanStep, tool: BaseTool | None
) -> PreparedArguments:
    try:
        references = find_references(step.arguments)
    except ReferenceSyntaxError as exc:
        raise BindingError(FailureType.INVALID_ARGS, str(exc)) from None
    if not references:
        return PreparedArguments(dict(step.arguments))

    order = {s.step_id: i for i, s in enumerate(state.plan)}
    position = order.get(step.step_id, len(state.plan))
    values: dict[tuple, Any] = {}
    described: list[dict[str, Any]] = []
    for location, reference in references:
        if location[:1] == ("operation",):
            raise BindingError(FailureType.INVALID_ARGS, "operation cannot be a reference")
        if order.get(reference.step_id, position) >= position:
            raise BindingError(
                FailureType.INVALID_ARGS,
                f"{location_text(location)} references '{reference.step_id}', "
                "which is not an earlier step",
            )
        data = producer_output(state, reference.step_id)
        values[location] = read_path(data, reference.path, f"Output of '{reference.step_id}'")
        described.append(
            {"location": list(location), "step_id": reference.step_id, "path": list(reference.path)}
        )

    resolved = substitute(step.arguments, values)
    try:
        leftover = contains_reference(resolved)
    except ReferenceSyntaxError:
        leftover = True
    if leftover:
        raise BindingError(
            FailureType.INVALID_ARGS, "A resolved value is itself a reference object"
        )
    if tool is not None:
        try:
            tool.args_schema.model_validate(resolved)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e.get('loc', ())))}: {e.get('msg', '')}" for e in exc.errors()
            )
            raise BindingError(
                FailureType.INVALID_ARGS, f"Resolved arguments are invalid: {problems}"
            ) from None
    return PreparedArguments(resolved, described)
