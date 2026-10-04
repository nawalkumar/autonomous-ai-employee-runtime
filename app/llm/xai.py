"""xAI / Grok client placeholder — no network calls in Phase 0."""

from app.llm.base import LLMClient, LLMRequest, LLMResponse


class XAILLMClient(LLMClient):
    """OpenAI-compatible chat completions client for xAI.

    Implementation intentionally incomplete until API keys and runtime
    integration are wired in a later phase. Do not call complete() yet
    expecting a live response.
    """

    provider_name = "xai"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError(
            "XAILLMClient.complete is a Phase 0 placeholder; "
            "live xAI calls are not enabled yet."
        )
