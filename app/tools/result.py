"""Normalized tool invocation result."""

from typing import Any

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    """Contract returned by every tool for the future runtime to observe."""

    ok: bool
    data: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        *,
        evidence: list[dict[str, Any]] | None = None,
    ) -> "ToolResult":
        return cls(ok=True, data=data or {}, evidence=evidence or [])

    @classmethod
    def failure(
        cls,
        error_type: str,
        error_message: str,
        *,
        data: dict[str, Any] | None = None,
        evidence: list[dict[str, Any]] | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            data=data,
            error_type=error_type,
            error_message=error_message,
            evidence=evidence or [],
        )
