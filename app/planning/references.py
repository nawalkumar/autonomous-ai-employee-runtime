"""Plan-time validation of step-output references (deterministic, offline).

Checked before execution:

- reference syntax (see app.models.references);
- the ``operation`` of a referencing step is a literal of a registered capability,
  references only appear under its declared inputs, and its required inputs are present;
- dependencies: the referenced step exists, is not the step itself, comes earlier
  in the plan, and the reference graph has no cycle;
- the referenced path exists in the producer capability's output schema;
- the producer's value type can satisfy the destination input type.

When a schema cannot decide a question (for example a free-form object), the
check is deferred to execution, where the resolved arguments are validated
against the tool's full argument schema before anything runs.
"""

from __future__ import annotations

from typing import Any

from app.models.plan import PlanStep
from app.models.references import (
    REF_KEY,
    Location,
    ReferenceSyntaxError,
    StepOutputRef,
    find_references,
    location_text,
)
from app.tools.capabilities import CAPABILITIES, Capability
from app.tools.registry import ToolRegistry


class PlanReferenceError(ValueError):
    """A reference that cannot be valid for this plan."""


_PY_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string", type(None): "null"}


def _deref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 16:
        name = str(schema["$ref"]).rsplit("/", 1)[-1]
        schema = root.get("$defs", {}).get(name, {})
        seen += 1
    return schema if isinstance(schema, dict) else {}


def json_types(schema: dict[str, Any], root: dict[str, Any]) -> frozenset[str] | None:
    """JSON types a schema admits, or None if the schema does not say."""
    schema = _deref(schema, root)
    if "$ref" in schema:
        return None
    if "const" in schema:
        return frozenset({_PY_TYPES.get(type(schema["const"]), "object")})
    if "enum" in schema:
        return frozenset(_PY_TYPES.get(type(v), "object") for v in schema["enum"])
    declared = schema.get("type")
    if isinstance(declared, str):
        return frozenset({declared})
    if isinstance(declared, list):
        return frozenset(declared)
    for combinator in ("anyOf", "oneOf"):
        if combinator in schema:
            union: set[str] = set()
            for branch in schema[combinator]:
                types = json_types(branch, root)
                if types is None:
                    return None
                union |= types
            return frozenset(union)
    if "properties" in schema:
        return frozenset({"object"})
    return None


def _non_null_branch(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any] | None:
    schema = _deref(schema, root)
    branches = schema.get("anyOf") or schema.get("oneOf")
    if not branches:
        return schema
    concrete = [b for b in branches if json_types(b, root) != frozenset({"null"})]
    return _deref(concrete[0], root) if len(concrete) == 1 else None


def schema_at(
    schema: dict[str, Any], path: Location, *, what: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(sub-schema, root) at ``path``; None if undecidable; raises if impossible."""
    root, current = schema, schema
    for depth, segment in enumerate(path):
        node = _non_null_branch(current, root)
        if node is None:
            return None
        shown = location_text(path[: depth + 1])
        types = json_types(node, root)
        if isinstance(segment, int):
            if types is not None and "array" not in types:
                raise PlanReferenceError(f"{what} has no list at {shown}")
            if "items" not in node:
                return None
            current = node["items"]
            continue
        if types is not None and "object" not in types:
            raise PlanReferenceError(f"{what} has no field at {shown}")
        properties = node.get("properties")
        if properties is None:
            extra = node.get("additionalProperties")
            if extra is False:
                raise PlanReferenceError(f"{what} has no field at {shown}")
            if not isinstance(extra, dict):
                return None
            current = extra
        elif segment in properties:
            current = properties[segment]
        else:
            raise PlanReferenceError(f"{what} has no field at {shown}")
    return current, root


def _compatible(produced: frozenset[str] | None, accepted: frozenset[str] | None) -> bool:
    if produced is None or accepted is None:
        return True
    values = (produced - {"null"}) or produced
    if values & accepted:
        return True
    return "integer" in values and "number" in accepted


def _capability(step: PlanStep, label: str) -> Capability:
    operation = step.arguments.get("operation")
    if not isinstance(operation, str):
        raise PlanReferenceError(f"{label}: operation must be a literal string")
    capability = CAPABILITIES.find(step.resolved_tool_name() or "", operation)
    if capability is None:
        raise PlanReferenceError(
            f"{label}: {step.resolved_tool_name()}.{operation} is not a registered capability"
        )
    return capability


def validate_reference_step(
    step: PlanStep, references: list[tuple[Location, StepOutputRef]]
) -> Capability:
    """Structural checks for one step that contains references."""
    label = f"Step '{step.step_id}'"
    for location, _ in references:
        if location[:1] == ("operation",):
            raise PlanReferenceError(f"{label}: operation cannot be a reference")
    capability = _capability(step, label)
    declared = set(capability.input_fields)
    for location, _ in references:
        if location[0] not in declared:
            raise PlanReferenceError(
                f"{label}: {location_text(location)} is not an input of {capability.operation}"
            )
    provided = set(step.arguments) - {"operation"}
    missing = [name for name in capability.required_inputs if name not in provided]
    if missing:
        raise PlanReferenceError(f"{label}: missing required input(s) {', '.join(missing)}")
    if capability.any_of_inputs and not provided & set(capability.any_of_inputs):
        raise PlanReferenceError(
            f"{label}: needs one of {', '.join(capability.any_of_inputs)}"
        )
    if capability.strict_inputs and provided - declared:
        raise PlanReferenceError(
            f"{label}: unsupported input(s) {', '.join(sorted(map(str, provided - declared)))}"
        )
    return capability


def _cycle(edges: dict[str, set[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    trail: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        trail.append(node)
        for target in sorted(edges.get(node, ())):
            if state.get(target) == 1:
                return trail[trail.index(target):] + [target]
            if target not in state:
                found = visit(target)
                if found:
                    return found
        trail.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if node not in state:
            found = visit(node)
            if found:
                return found
    return None


def validate_plan_references(steps: list[PlanStep], registry: ToolRegistry) -> None:
    """Dependency and type checks across the whole plan."""
    index = {step.step_id: i for i, step in enumerate(steps)}
    found: dict[str, list[tuple[Location, StepOutputRef]]] = {}
    edges: dict[str, set[str]] = {}
    for step in steps:
        try:
            references = find_references(step.arguments)
        except ReferenceSyntaxError as exc:
            raise PlanReferenceError(f"Step '{step.step_id}': {exc}") from exc
        found[step.step_id] = references
        for _, reference in references:
            if reference.step_id == step.step_id:
                raise PlanReferenceError(f"Step '{step.step_id}' references itself")
            if reference.step_id not in index:
                raise PlanReferenceError(
                    f"Step '{step.step_id}' references unknown step '{reference.step_id}'"
                )
            edges.setdefault(step.step_id, set()).add(reference.step_id)

    cycle = _cycle(edges)
    if cycle:
        raise PlanReferenceError(f"Circular step references: {' -> '.join(cycle)}")

    for step in steps:
        references = found[step.step_id]
        if not references:
            continue
        consumer = validate_reference_step(step, references)
        model = registry.get(consumer.tool).args_schema
        input_schema = consumer.input_schema(model)
        for location, reference in references:
            if index[reference.step_id] > index[step.step_id]:
                raise PlanReferenceError(
                    f"Step '{step.step_id}' references later step '{reference.step_id}'"
                )
            producer = _capability(steps[index[reference.step_id]], f"Step '{reference.step_id}'")
            output = producer.output_schema()
            produced = (
                schema_at(
                    output,
                    reference.path,
                    what=f"Output of '{reference.step_id}' ({producer.tool}.{producer.operation})",
                )
                if output
                else None
            )
            accepted = schema_at(
                input_schema, location, what=f"Input of {consumer.tool}.{consumer.operation}"
            )
            produced_types = json_types(*produced) if produced else None
            accepted_types = json_types(*accepted) if accepted else None
            if not _compatible(produced_types, accepted_types):
                raise PlanReferenceError(
                    f"Step '{step.step_id}': {location_text(location)} accepts "
                    f"{sorted(accepted_types or [])} but '{reference.step_id}' "
                    f"{location_text(reference.path)} is {sorted(produced_types or [])}"
                )


__all__ = [
    "REF_KEY",
    "PlanReferenceError",
    "json_types",
    "schema_at",
    "validate_plan_references",
    "validate_reference_step",
]
