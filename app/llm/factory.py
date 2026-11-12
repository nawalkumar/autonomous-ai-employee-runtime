"""Factory for constructing the configured LLM client."""

import os

from app.config import Settings, get_settings
from app.llm.base import LLMClient
from app.llm.mock import MockLLMClient
from app.llm.xai import XAILLMClient


def _test_scripted_plan() -> str | None:
    """A fixed plan for tests. Ignored unless the process is in test mode."""
    if os.environ.get("AAE_TEST_MODE") != "1":
        return None
    raw = os.environ.get("AAE_TEST_SCRIPTED_PLAN", "").strip()
    return raw or None


def create_llm_client(settings: Settings | None = None) -> LLMClient:
    scripted = _test_scripted_plan()
    if scripted is not None:
        from app.llm.scripted import FixedPlanClient

        return FixedPlanClient(scripted)
    cfg = settings or get_settings()
    if cfg.llm_provider == "mock":
        return MockLLMClient()
    if cfg.llm_provider == "xai":
        return XAILLMClient(
            api_key=cfg.xai_api_key,
            model=cfg.xai_model,
            base_url=cfg.xai_base_url,
            timeout_seconds=cfg.llm_request_timeout_seconds,
            max_response_bytes=cfg.llm_max_response_bytes,
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {cfg.llm_provider}")
