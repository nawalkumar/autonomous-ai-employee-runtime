"""Deterministic mock LLM for local development and tests."""

from app.llm.base import LLMClient, LLMRequest, LLMResponse


class MockLLMClient(LLMClient):
    provider_name = "mock"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == "user"),
            "",
        )
        return LLMResponse(
            content=f"[mock] acknowledged: {last_user[:200]}",
            model="mock-llm",
            provider=self.provider_name,
            raw={"echo": last_user},
        )
