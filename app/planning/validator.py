"""Plan validation against the tool registry before execution."""

from __future__ import annotations

from pydantic import ValidationError

from app.models.plan import Plan, PlanStep
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
            tool.args_schema.model_validate(step.arguments)
        except ValidationError as exc:
            raise PlanValidationError(
                f"Invalid arguments for tool '{tool_name}' in step '{step_id}': "
                f"{exc.errors()}"
            ) from exc

        normalized_steps.append(
            step.model_copy(
                update={
                    "step_id": step_id,
                    "tool_name": tool_name,
                }
            )
        )

    return Plan(steps=normalized_steps)


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
