"""Deterministic failure injection for demos and tests."""

from __future__ import annotations

from dataclasses import dataclass

from app.tools.result import ToolResult


@dataclass
class InjectedFailure:
    error_type: str
    error_message: str
    remaining: int | None = 1  # None means always


class FailureInjector:
    """Injects deterministic failures for named operations before mutation."""

    def __init__(self) -> None:
        self._pending: dict[str, InjectedFailure] = {}

    def inject_once(
        self,
        operation: str,
        error_type: str,
        error_message: str,
    ) -> None:
        self.inject_times(operation, 1, error_type, error_message)

    def inject_times(
        self,
        operation: str,
        times: int,
        error_type: str,
        error_message: str,
    ) -> None:
        if times < 1:
            raise ValueError("times must be >= 1")
        self._pending[operation] = InjectedFailure(
            error_type=error_type,
            error_message=error_message,
            remaining=times,
        )

    def inject_always(
        self,
        operation: str,
        error_type: str,
        error_message: str,
    ) -> None:
        self._pending[operation] = InjectedFailure(
            error_type=error_type,
            error_message=error_message,
            remaining=None,
        )

    def consume(self, operation: str) -> ToolResult | None:
        """If a failure is queued for operation, consume one and return a failure result."""
        failure = self._pending.get(operation)
        if failure is None:
            return None
        if failure.remaining is None:
            return ToolResult.failure(failure.error_type, failure.error_message)
        if failure.remaining <= 1:
            self._pending.pop(operation, None)
        else:
            failure.remaining -= 1
        return ToolResult.failure(failure.error_type, failure.error_message)

    def clear(self) -> None:
        self._pending.clear()

    def pending_operations(self) -> list[str]:
        return sorted(self._pending)
