"""Fixed plan text for controlled integration tests.

Used only when ``AAE_TEST_MODE=1`` and ``AAE_TEST_SCRIPTED_PLAN`` is set.
The dashboard has no control that selects this client.
"""

from __future__ import annotations

from app.llm.base import LLMClient, LLMRequest, LLMResponse


class FixedPlanClient(LLMClient):
    """Returns one configured JSON document for every completion."""

    provider_name = "scripted-test"

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self.content, model="scripted-test", provider=self.provider_name)
