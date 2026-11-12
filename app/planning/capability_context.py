"""Safe capability descriptions for LLM planner prompts.

Exposes only operation names, descriptions, input schemas and reference syntax.
Never includes secrets, paths, credentials or authorization overrides.
"""

from __future__ import annotations

from typing import Any

from app.authz.models import AuthorizationScope
from app.tools.capabilities import CAPABILITIES
from app.tools.registry import ToolRegistry

REFERENCE_SYNTAX = {
    "form": {"$ref": {"step_id": "<earlier_step_id>", "path": ["field", "..."]}},
    "rules": [
        "References may only target outputs of earlier steps in the same plan.",
        "Forward references are forbidden.",
        "Use references for values produced by earlier steps (for example customer_id).",
        "Do not invent identifiers when a reference is required by the goal.",
    ],
}


def capability_context_for_scope(
    registry: ToolRegistry,
    scope: AuthorizationScope,
) -> list[dict[str, Any]]:
    """Capability entries limited to operations allowed by ``scope``."""
    allowed = set(scope.allowed_operations)
    entries: list[dict[str, Any]] = []
    for capability in CAPABILITIES:
        key = f"{capability.tool}.{capability.operation}"
        if key not in allowed:
            continue
        if capability.tool not in registry:
            continue
        entry: dict[str, Any] = {
            "operation": key,
            "tool": capability.tool,
            "name": capability.operation,
            "description": capability.description,
            "risk": capability.risk.value,
            "mutating": capability.mutating,
            "requires_approval": bool(capability.requires_approval),
            "required_inputs": list(capability.required_inputs),
            "optional_inputs": list(capability.optional_inputs),
        }
        if capability.input_constraints:
            entry["input_schemas"] = {
                name: dict(schema) for name, schema in capability.input_constraints.items()
            }
        if capability.preconditions:
            entry["preconditions"] = list(capability.preconditions)
        if capability.output_model is not None:
            entry["output_fields"] = sorted(capability.output_model.model_fields)
        entries.append(entry)
    return entries


def planner_limits(scope: AuthorizationScope) -> dict[str, int]:
    """Informational plan limits. The trusted scope remains authoritative."""
    return {
        "max_steps": scope.budgets.max_steps,
        "max_mutations": scope.budgets.max_mutations,
    }
