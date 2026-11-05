"""Application settings loaded from environment / .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def database_inside_workspace(database_path: str | Path, workspace_path: str | Path) -> bool:
    """True when the SQLite file would be reachable through the FileTool sandbox."""
    db = Path(database_path).resolve()
    workspace = Path(workspace_path).resolve()
    return db == workspace or workspace in db.parents


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

    # SQLite path for ExecutionState + audit events + company world tables.
    # Must live outside workspace_path so FileTool can never reach it.
    database_path: str = "data/runtime.db"

    # Sandboxed filesystem root for FileTool
    workspace_path: str = "workspace"

    # Bounded recovery retries after the first failed attempt of a step
    max_recovery_attempts: int = 2

    @model_validator(mode="after")
    def _database_outside_workspace(self) -> "Settings":
        if database_inside_workspace(self.database_path, self.workspace_path):
            raise ValueError(
                "DATABASE_PATH must be outside WORKSPACE_PATH so the FileTool "
                f"cannot read or overwrite it (got DATABASE_PATH={self.database_path!r}, "
                f"WORKSPACE_PATH={self.workspace_path!r})"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
