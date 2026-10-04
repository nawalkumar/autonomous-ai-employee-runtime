"""Application settings loaded from environment / .env."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: Literal["xai", "mock"] = "mock"
    xai_api_key: str = ""
    xai_model: str = "grok-2-latest"
    xai_base_url: str = "https://api.x.ai/v1"

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "info"

    # SQLite path for ExecutionState + audit events + company world tables
    database_path: str = "workspace/runtime.db"

    # Sandboxed filesystem root for FileTool
    workspace_path: str = "workspace"


@lru_cache
def get_settings() -> Settings:
    return Settings()
