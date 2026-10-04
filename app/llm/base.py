"""LLM provider abstraction (contracts only for Phase 0)."""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMRequest(BaseModel):
    messages: list[LLMMessage]
    temperature: float = 0.0
    max_tokens: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    content: str
    model: str
    provider: str
    raw: dict[str, Any] = Field(default_factory=dict)


class LLMClient(ABC):
    """Provider-agnostic LLM interface used by the future runtime."""

    provider_name: str

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a completion for the given chat-style request."""
