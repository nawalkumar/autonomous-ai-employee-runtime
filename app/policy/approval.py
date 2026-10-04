"""Deterministic approval policy for sensitive tool operations."""

from __future__ import annotations

from typing import Any

from app.policy.decisions import PolicyDecision
from app.tools.base import BaseTool, RiskLevel

# Explicit high-risk operations that always require human approval.
_APPROVAL_REQUIRED: set[tuple[str, str]] = {
    ("company_api", "update_employee"),
}


class ApprovalPolicy:
    """Decide whether a planned tool invocation requires human approval."""

    def evaluate(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tool: BaseTool | None = None,
    ) -> PolicyDecision:
        operation = arguments.get("operation")
        if isinstance(operation, str):
            key = (tool_name, operation)
            if key in _APPROVAL_REQUIRED:
                return PolicyDecision(
                    requires_approval=True,
                    reason="Employee record mutation requires human approval.",
                    risk=RiskLevel.WRITE,
                    operation=operation,
                )

        risk = RiskLevel.WRITE
        if tool is not None:
            if hasattr(tool, "risk_for") and isinstance(operation, str):
                try:
                    risk = tool.risk_for(operation)  # type: ignore[attr-defined]
                except Exception:
                    risk = tool.risk_level
            else:
                risk = tool.risk_level

        if risk is RiskLevel.DESTRUCTIVE:
            return PolicyDecision(
                requires_approval=True,
                reason="Destructive tool operations require human approval.",
                risk=risk,
                operation=operation if isinstance(operation, str) else None,
            )

        return PolicyDecision(
            requires_approval=False,
            reason="Operation is not classified as requiring human approval.",
            risk=risk if isinstance(risk, RiskLevel) else RiskLevel.READ,
            operation=operation if isinstance(operation, str) else None,
        )
