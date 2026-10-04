"""Deterministic one-shot failure injection for demos and tests."""

from __future__ import annotations

from dataclasses import dataclass

from app.tools.result import ToolResult


@dataclass
class InjectedFailure:
    error_type: str
    error_message: str


class FailureInjector:
    """Injects a single deterministic failure for a named operation."""

    def __init__(self) -> None:
        self._pending: dict[str, InjectedFailure] = {}

    def inject_once(
        self,
        operation: str,
        error_type: str,
        error_message: str,
    ) -> None:
        self._pending[operation] = InjectedFailure(
            error_type=error_type,
            error_message=error_message,
        )

    def consume(self, operation: str) -> ToolResult | None:
        """If a failure is queued for operation, consume it and return a failure result."""
        failure = self._pending.pop(operation, None)
        if failure is None:
            return None
        return ToolResult.failure(failure.error_type, failure.error_message)

    def clear(self) -> None:
        self._pending.clear()

    def pending_operations(self) -> list[str]:
        return sorted(self._pending)
