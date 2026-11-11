"""Deterministic approval policy for sensitive tool operations."""

from __future__ import annotations

from typing import Any

from app.policy.decisions import PolicyDecision
from app.tools.base import BaseTool, RiskLevel
from app.tools.capabilities import CAPABILITIES


class ApprovalPolicy:
    """Decide whether a planned tool invocation requires human approval.

    Operations that always need approval are declared in the capability
    registry. Otherwise the tool's per-operation risk applies; tools raise for
    unregistered operations, which then fall back to the tool's own (write)
    risk level instead of being treated as reads.
    """

    def evaluate(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        tool: BaseTool | None = None,
    ) -> PolicyDecision:
        operation = arguments.get("operation")
        capability = CAPABILITIES.find(tool_name, operation)
        if capability is not None and capability.requires_approval:
            return PolicyDecision(
                requires_approval=True,
                reason=capability.approval_reason or "This operation requires human approval.",
                risk=capability.risk,
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
