"""xAI / Grok OpenAI-compatible chat client."""

from __future__ import annotations

import httpx

from app.llm.base import LLMClient, LLMRequest, LLMResponse
from app.llm.errors import LLMProviderError


class XAILLMClient(LLMClient):
    """Minimal chat-completions client for structured planning responses."""

    provider_name = "xai"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 65_536,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.api_key:
            raise LLMProviderError("missing_credentials")

        payload = {
            "model": self.model,
            "temperature": request.temperature,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            # Prefer JSON object responses when supported by the provider.
            "response_format": {"type": "json_object"},
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        # Authorization header is never interpolated into exception messages.
        request_headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(url, headers=request_headers, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMProviderError("timeout") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError("unavailable") from exc

        if response.status_code in {401, 403}:
            raise LLMProviderError("auth")
        if response.status_code >= 500:
            raise LLMProviderError("unavailable")
        if response.status_code >= 400:
            raise LLMProviderError("rejected")

        raw_bytes = response.content or b""
        if len(raw_bytes) > self.max_response_bytes:
            raise LLMProviderError("oversized")

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMProviderError("invalid_response") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("invalid_response") from exc

        if not isinstance(content, str):
            raise LLMProviderError("invalid_response")
        if len(content.encode("utf-8")) > self.max_response_bytes:
            raise LLMProviderError("oversized")

        return LLMResponse(
            content=content,
            model=str(body.get("model", self.model)),
            provider=self.provider_name,
            raw={"id": body.get("id"), "usage": body.get("usage")},
        )
