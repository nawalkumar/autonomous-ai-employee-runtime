"""Factory for constructing the configured LLM client."""

from app.config import Settings, get_settings
from app.llm.base import LLMClient
from app.llm.mock import MockLLMClient
from app.llm.xai import XAILLMClient


def create_llm_client(settings: Settings | None = None) -> LLMClient:
    cfg = settings or get_settings()
    if cfg.llm_provider == "mock":
        return MockLLMClient()
    if cfg.llm_provider == "xai":
        return XAILLMClient(
            api_key=cfg.xai_api_key,
            model=cfg.xai_model,
            base_url=cfg.xai_base_url,
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {cfg.llm_provider}")
