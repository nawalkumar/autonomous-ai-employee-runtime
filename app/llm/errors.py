"""Provider error classification without leaking credentials."""

from __future__ import annotations

from typing import Literal

LLMErrorKind = Literal[
    "timeout",
    "unavailable",
    "auth",
    "invalid_response",
    "oversized",
    "missing_credentials",
    "rejected",
]


class LLMProviderError(RuntimeError):
    """Explicit provider failure. ``str(self)`` never includes secrets."""

    def __init__(self, kind: LLMErrorKind, message: str | None = None) -> None:
        self.kind = kind
        safe = message or {
            "timeout": "LLM provider request timed out",
            "unavailable": "LLM provider is unavailable",
            "auth": "LLM provider authentication failed",
            "invalid_response": "LLM provider returned an invalid response",
            "oversized": "LLM provider response exceeded the configured size limit",
            "missing_credentials": "LLM provider credentials are not configured",
            "rejected": "LLM provider rejected the request",
        }.get(kind, "LLM provider error")
        super().__init__(safe)

    def __repr__(self) -> str:
        return f"LLMProviderError(kind={self.kind!r})"
