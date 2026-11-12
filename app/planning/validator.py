"""Plan validation against the tool registry before execution."""

from __future__ import annotations

from pydantic import ValidationError

from app.models.collections import parse_collection_item_ref
from app.models.plan import Plan, PlanStep
from app.models.references import ReferenceSyntaxError, contains_reference
from app.planning.references import PlanReferenceError, validate_plan_references
from app.tools.registry import ToolRegistry


class PlanValidationError(ValueError):
    """Raised when a generated plan is not executable."""


def validate_plan(plan: Plan, registry: ToolRegistry) -> Plan:
    """Validate tool names, unique step IDs, and argument schemas."""
    if not plan.steps:
        raise PlanValidationError("Plan must contain at least one step")

    seen_ids: set[str] = set()
    normalized_steps: list[PlanStep] = []

    for index, step in enumerate(plan.steps):
        step_id = step.step_id or f"step_{index + 1}"
        if step_id in seen_ids:
            raise PlanValidationError(f"Duplicate step_id: {step_id}")
        seen_ids.add(step_id)

        tool_name = step.resolved_tool_name()
        if not tool_name:
            raise PlanValidationError(
                f"Step '{step_id}' is missing tool_name"
            )
        if tool_name not in registry:
            raise PlanValidationError(
                f"Unsupported tool '{tool_name}' in step '{step_id}'"
            )

        tool = registry.get(tool_name)
        try:
            has_references = contains_reference(step.arguments)
            placeholders = _collection_placeholders(step.arguments)
        except ReferenceSyntaxError as exc:
            raise PlanValidationError(f"Step '{step_id}': {exc}") from exc
        except ValueError as exc:
            raise PlanValidationError(f"Step '{step_id}': {exc}") from exc
        if placeholders and step.collection is None:
            raise PlanValidationError(
                f"Step '{step_id}' uses a collection placeholder without collection work"
            )
        if step.collection is not None and not placeholders:
            raise PlanValidationError(
                f"Step '{step_id}' declares collection work without a collection placeholder"
            )
        if step.collection is not None:
            _validate_collection_work(step, normalized_steps, placeholders)
        if placeholders:
            try:
                tool.args_schema.model_validate(_placeholder_standins(step.arguments))
            except ValidationError as exc:
                raise PlanValidationError(
                    f"Invalid arguments for tool '{tool_name}' in step '{step_id}': "
                    f"{exc.errors()}"
                ) from exc
        elif not has_references:
            try:
                tool.args_schema.model_validate(step.arguments)
            except ValidationError as exc:
                raise PlanValidationError(
                    f"Invalid arguments for tool '{tool_name}' in step '{step_id}': "
                    f"{exc.errors()}"
                ) from exc
        # A step with references is checked by validate_plan_references below;
        # its complete arguments are validated after resolution, before it runs.

        normalized_steps.append(
            step.model_copy(
                update={
                    "step_id": step_id,
                    "tool_name": tool_name,
                }
            )
        )

    try:
        validate_plan_references(normalized_steps, registry)
    except PlanReferenceError as exc:
        raise PlanValidationError(str(exc)) from exc
    return Plan(steps=normalized_steps)


def _collection_placeholders(value: object) -> list[str]:
    found: list[str] = []

    def walk(item: object) -> None:
        if isinstance(item, dict):
            ref = parse_collection_item_ref(item)
            if ref is not None:
                found.append(ref.field)
                return
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found


def _placeholder_standins(value: object) -> object:
    if isinstance(value, dict):
        if parse_collection_item_ref(value) is not None:
            return "T-1"
        return {key: _placeholder_standins(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_placeholder_standins(child) for child in value]
    return value


def _validate_collection_work(
    step: PlanStep, prior: list[PlanStep], placeholders: list[str]
) -> None:
    work = step.collection
    if work is None:
        return
    if list(work.items_path) != ["tickets"] or work.item_id_field != "ticket_id":
        raise PlanValidationError(
            f"Step '{step.step_id}' collection source is not the registered ticket list"
        )
    if any(field != "ticket_id" for field in placeholders):
        raise PlanValidationError(
            f"Step '{step.step_id}' collection placeholder must read ticket_id"
        )
    producers = [item for item in prior if item.step_id == work.from_step]
    if len(producers) != 1:
        raise PlanValidationError(
            f"Step '{step.step_id}' collection source '{work.from_step}' "
            "is not an earlier step"
        )
    producer = producers[0]
    if (
        producer.resolved_tool_name() != "company_api"
        or producer.arguments.get("operation") != "list_tickets"
    ):
        raise PlanValidationError(
            f"Step '{step.step_id}' collection source is not company_api.list_tickets"
        )
    if (
        work.select.mode == "first_n"
        and work.select.n is not None
        and work.select.n > work.max_items
    ):
        raise PlanValidationError(
            f"Step '{step.step_id}' selects more items than its collection limit"
        )


def compact_tool_catalog(registry: ToolRegistry) -> dict[str, object]:
    """Build a compact tool catalog for planner prompts."""
    catalog: dict[str, object] = {}
    for tool in registry.list_tools():
        meta = tool.metadata()
        entry: dict[str, object] = {
            "description": tool.description,
            "risk_level": tool.risk_level.value,
        }
        if "operations" in meta:
            entry["operations"] = meta["operations"]
        else:
            entry["args_schema"] = meta.get("args_schema", {})
        catalog[tool.name] = entry
    return catalog
