"""xAI / Grok OpenAI-compatible chat client."""

from __future__ import annotations

import httpx

from app.llm.base import LLMClient, LLMRequest, LLMResponse


class XAILLMClient(LLMClient):
    """Minimal chat-completions client for structured planning responses."""

    provider_name = "xai"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError(
                "XAI_API_KEY is not configured. Set LLM_PROVIDER=mock "
                "for offline/deterministic mode, or provide XAI_API_KEY."
            )

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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"xAI provider request failed: {exc}") from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Unexpected xAI response shape; expected choices[0].message.content"
            ) from exc

        return LLMResponse(
            content=content or "",
            model=str(body.get("model", self.model)),
            provider=self.provider_name,
            raw={"id": body.get("id"), "usage": body.get("usage")},
        )
