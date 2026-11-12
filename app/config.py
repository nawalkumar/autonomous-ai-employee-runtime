"""Application settings loaded from environment / .env."""

import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
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
    # Optional LLM-backed planning for a trusted intent catalog. Default off:
    # disabled mode preserves the two deterministic mock goal contracts only.
    aae_dynamic_planning_enabled: bool = False
    # Provider reliability bounds (dynamic planner / xAI adapter). Unused when
    # dynamic planning is disabled.
    llm_request_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    llm_max_tokens: int = Field(default=4096, ge=256, le=16_384)
    llm_max_response_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    llm_max_provider_retries: int = Field(default=1, ge=0, le=3)

    # Optional bounded replanning (Milestone 6). Default off; independent of
    # dynamic planning. Never expands the original trusted authorization scope.
    aae_replanning_enabled: bool = False
    aae_max_replan_rounds: int = Field(default=2, ge=0, le=5)

    # "local": one persistent SQLite database and workspace shared with the CLI,
    # served to localhost only. "public": per-browser-session sandboxes in
    # ephemeral storage, for an internet-facing demo of fictional data.
    app_mode: Literal["local", "public"] = "local"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    # Set by hosting providers such as Render; takes precedence over app_port.
    port: int | None = Field(default=None, ge=1, le=65535)
    log_level: str = "info"

    # Extra accepted Host header values (comma-separated). Local mode always
    # accepts localhost, 127.0.0.1 and [::1]; public mode also accepts
    # RENDER_EXTERNAL_HOSTNAME when Render sets it.
    allowed_hosts: str = ""
    render_external_hostname: str = ""
    # Use the last X-Forwarded-For hop as the client address (behind a proxy).
    trust_proxy_headers: bool = False

    max_request_bytes: int = Field(default=8192, ge=1024, le=1_048_576)
    api_worker_threads: int = Field(default=2, ge=1, le=16)
    api_max_pending_jobs: int = Field(default=16, ge=1, le=256)
    # Lets the dashboard inject one simulated transient 503 into create_ticket.
    allow_failure_injection: bool = True

    # Public demo mode only.
    public_data_root: str = ""
    public_access_code: str = ""
    public_max_sessions: int = Field(default=200, ge=1, le=10_000)
    public_session_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    public_max_tasks_per_session: int = Field(default=20, ge=1, le=1000)
    session_cookie_secure: bool = True
    rate_limit_per_minute: int = Field(default=240, ge=1)
    mutation_rate_limit_per_minute: int = Field(default=30, ge=1)
    session_rate_limit_per_minute: int = Field(default=6, ge=1)

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

    @property
    def listen_port(self) -> int:
        return self.port or self.app_port

    @property
    def public_root(self) -> Path:
        root = self.public_data_root or str(Path(tempfile.gettempdir()) / "aae-public-demo")
        return Path(root).expanduser().resolve()

    def host_allowlist(self) -> set[str]:
        hosts = {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}
        if self.app_mode == "local":
            hosts |= {"localhost", "127.0.0.1", "[::1]", "::1"}
        if self.render_external_hostname:
            hosts.add(self.render_external_hostname.strip().lower())
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
