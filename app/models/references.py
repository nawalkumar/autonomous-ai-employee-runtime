"""Typed references from one plan step's arguments to an earlier step's output.

Syntax (anywhere inside a step's ``arguments``, except the ``operation`` key)::

    {"$ref": {"step_id": "create_customer", "path": ["customer", "customer_id"]}}

- The object has exactly one key, ``$ref``, whose value has exactly the keys
  ``step_id`` and ``path``. Any other shape containing ``$ref`` is rejected as
  ambiguous, so ``$ref`` is reserved as a dictionary key in arguments.
- ``path`` addresses the producer step's successful tool result ``data``: 1-8
  segments, each either an identifier (letters, digits, underscore; not starting
  with an underscore) for a dictionary key, or an integer 0-99 for a list index.
- Strings are never interpreted: ``"$ref"`` or ``"{{step_1.customer}}"`` stay
  literal text. There are no expressions, templates, functions or wildcards.
- Resolution substitutes the referenced JSON value unchanged (same type).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator
from typing import Any

from app.models.enums import FailureType, PlanStepStatus, ToolCallStatus

REF_KEY = "$ref"
MAX_PATH_LENGTH = 8
MAX_LIST_INDEX = 99
MAX_ARGUMENT_DEPTH = 16
STEP_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
PATH_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")

PathSegment = str | int
Location = tuple[PathSegment, ...]


class ReferenceSyntaxError(ValueError):
    """A malformed, ambiguous or out-of-bounds reference object."""


class StepOutputRef:
    """An immutable reference to ``path`` within step ``step_id``'s output data."""

    __slots__ = ("step_id", "path")

    def __init__(self, step_id: str, path: tuple[PathSegment, ...]) -> None:
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "path", path)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("StepOutputRef is immutable")

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StepOutputRef) and (self.step_id, self.path) == (
            other.step_id,
            other.path,
        )

    def __hash__(self) -> int:
        return hash((self.step_id, self.path))

    def __repr__(self) -> str:
        return f"StepOutputRef({self.step_id!r}, {list(self.path)!r})"

    def to_json(self) -> dict[str, Any]:
        return {REF_KEY: {"step_id": self.step_id, "path": list(self.path)}}


def _parse_path(raw: Any) -> tuple[PathSegment, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PATH_LENGTH:
        raise ReferenceSyntaxError(
            f"$ref path must be a list of 1-{MAX_PATH_LENGTH} segments, got {raw!r}"
        )
    segments: list[PathSegment] = []
    for segment in raw:
        if isinstance(segment, bool):
            raise ReferenceSyntaxError(f"$ref path segment {segment!r} is not a key or index")
        if isinstance(segment, int):
            if not 0 <= segment <= MAX_LIST_INDEX:
                raise ReferenceSyntaxError(
                    f"$ref list index {segment} is outside 0-{MAX_LIST_INDEX}"
                )
        elif not isinstance(segment, str) or not PATH_KEY_RE.fullmatch(segment):
            raise ReferenceSyntaxError(
                f"$ref path key {segment!r} must be an identifier not starting with '_'"
            )
        segments.append(segment)
    return tuple(segments)


def parse_reference(value: Any) -> StepOutputRef | None:
    """The reference ``value`` denotes, None if it is not a reference object.

    Raises ReferenceSyntaxError for any dictionary that contains ``$ref`` but is
    not exactly a well-formed reference.
    """
    if not isinstance(value, dict) or REF_KEY not in value:
        return None
    if set(value) != {REF_KEY}:
        raise ReferenceSyntaxError("A $ref object must not have other keys")
    body = value[REF_KEY]
    if not isinstance(body, dict) or set(body) != {"step_id", "path"}:
        raise ReferenceSyntaxError("$ref must be an object with exactly 'step_id' and 'path'")
    step_id = body["step_id"]
    if not isinstance(step_id, str) or not STEP_ID_RE.fullmatch(step_id):
        raise ReferenceSyntaxError(f"$ref step_id {step_id!r} is not a valid step id")
    return StepOutputRef(step_id, _parse_path(body["path"]))


def iter_references(arguments: Any) -> Iterator[tuple[Location, StepOutputRef]]:
    """Every (location, reference) in ``arguments``, in a stable order."""

    def walk(value: Any, location: Location) -> Iterator[tuple[Location, StepOutputRef]]:
        if len(location) > MAX_ARGUMENT_DEPTH:
            raise ReferenceSyntaxError("Arguments are nested too deeply")
        reference = parse_reference(value)
        if reference is not None:
            yield location, reference
        elif isinstance(value, dict):
            for key in sorted(value, key=str):
                yield from walk(value[key], (*location, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, (*location, index))

    yield from walk(arguments, ())


def find_references(arguments: Any) -> list[tuple[Location, StepOutputRef]]:
    return list(iter_references(arguments))


def contains_reference(arguments: Any) -> bool:
    return any(True for _ in iter_references(arguments))


def substitute(arguments: Any, values: dict[Location, Any]) -> Any:
    """A deep copy of ``arguments`` with each reference location replaced."""

    def walk(value: Any, location: Location) -> Any:
        if location in values:
            return copy.deepcopy(values[location])
        if isinstance(value, dict):
            return {key: walk(item, (*location, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item, (*location, index)) for index, item in enumerate(value)]
        return copy.deepcopy(value)

    return walk(arguments, ())


def location_text(location: Location) -> str:
    return ".".join(str(part) for part in location) or "<arguments>"


class BindingError(Exception):
    """A reference that cannot be resolved; carries the FailureType to record."""

    def __init__(self, failure_type: FailureType, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.message = message


def producer_output(state: Any, step_id: str) -> dict[str, Any]:
    """The persisted result ``data`` of a completed step's successful tool call.

    Only a step whose status is completed and which has exactly one successful
    call can be read; failed, interrupted, running or pending attempts never are.
    """
    step = next((s for s in state.plan if s.step_id == step_id), None)
    if step is None:
        raise BindingError(FailureType.MISSING_INFO, f"Referenced step '{step_id}' is not in the plan")
    if step.status is not PlanStepStatus.COMPLETED:
        raise BindingError(
            FailureType.MISSING_INFO,
            f"Referenced step '{step_id}' has not completed (status {step.status.value})",
        )
    calls = [
        c for c in state.tool_calls if c.step_id == step_id and c.status is ToolCallStatus.SUCCEEDED
    ]
    if len(calls) != 1:
        raise BindingError(
            FailureType.MISSING_INFO if not calls else FailureType.ENVIRONMENT_UNEXPECTED,
            f"Referenced step '{step_id}' has {len(calls)} successful tool calls; expected 1",
        )
    result = calls[0].result if isinstance(calls[0].result, dict) else {}
    data = result.get("data")
    if not isinstance(data, dict):
        raise BindingError(
            FailureType.MISSING_INFO, f"Referenced step '{step_id}' has no persisted output"
        )
    return data


def read_path(value: Any, path: tuple[PathSegment, ...], label: str) -> Any:
    """The value at ``path``; never returns null or an empty string."""
    for depth, segment in enumerate(path):
        shown = f"{label} {location_text(path[: depth + 1])}"
        if isinstance(segment, int):
            if not isinstance(value, list):
                raise BindingError(FailureType.INVALID_ARGS, f"{shown}: not a list")
            if segment >= len(value):
                raise BindingError(FailureType.MISSING_INFO, f"{shown}: no such item")
            value = value[segment]
        else:
            if not isinstance(value, dict):
                raise BindingError(FailureType.INVALID_ARGS, f"{shown}: not an object")
            if segment not in value:
                raise BindingError(FailureType.MISSING_INFO, f"{shown}: no such field")
            value = value[segment]
    if value is None or value == "":
        raise BindingError(FailureType.MISSING_INFO, f"{label} {location_text(path)}: value is empty")
    return copy.deepcopy(value)
