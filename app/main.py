"""FastAPI application: API v1 and the dashboard, served from one origin."""

from __future__ import annotations

import ipaddress
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.api.redaction import Redactor
from app.api.security import RateLimiter, RequestGuard
from app.api.service import JobRunner, TaskService
from app.api.sessions import SessionManager
from app.api.v1 import AppContext
from app.api.v1 import router as v1_router
from app.config import Settings, get_settings

logger = logging.getLogger("app.api")

STATIC_DIR = Path(__file__).resolve().parent / "web"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    public = settings.app_mode == "public"
    allowed_hosts = settings.host_allowlist()
    if public and not allowed_hosts:
        raise ValueError(
            "Public mode requires ALLOWED_HOSTS (or Render's RENDER_EXTERNAL_HOSTNAME) "
            "so that requests for other host names are refused."
        )

    jobs = JobRunner(settings.api_worker_threads, settings.api_max_pending_jobs)
    service = TaskService(
        jobs=jobs,
        redactor=Redactor([settings.xai_api_key, settings.public_access_code]),
        allow_failure_injection=settings.allow_failure_injection,
    )
    sessions = (
        SessionManager(
            settings.public_root,
            max_sessions=settings.public_max_sessions,
            ttl_seconds=settings.public_session_ttl_seconds,
        )
        if public
        else None
    )
    ctx = AppContext(
        settings=settings,
        service=service,
        sessions=sessions,
        boot_id=secrets.token_hex(8),
        started_at=datetime.now(timezone.utc),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if sessions is not None:
            sessions.reset_storage()
            logger.info("Public demo mode: ephemeral storage reset at %s", sessions.root)
        try:
            yield
        finally:
            jobs.shutdown()

    application = FastAPI(
        title="Autonomous AI Employee Runtime",
        version=__version__,
        description=(
            "HTTP API and dashboard for the execution runtime. Tasks come only from "
            "goals accepted by the deterministic mock planner; there is no endpoint "
            "for arbitrary tools, plans, shell commands or file writes."
        ),
        lifespan=lifespan,
        docs_url=None if public else "/docs",
        redoc_url=None,
        openapi_url=None if public else "/openapi.json",
    )
    application.state.aae = ctx
    install_error_handlers(application)
    application.include_router(health_router)
    application.include_router(v1_router)

    @application.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    limiter = (
        RateLimiter(
            {
                "read": settings.rate_limit_per_minute,
                "mutate": settings.mutation_rate_limit_per_minute,
                "session": settings.session_rate_limit_per_minute,
            }
        )
        if public
        else None
    )
    application.add_middleware(
        RequestGuard,
        allowed_hosts=allowed_hosts,
        max_body_bytes=settings.max_request_bytes,
        rate_limiter=limiter,
        trust_proxy_headers=settings.trust_proxy_headers,
        hsts=public,
        csp_exempt_docs=not public,
    )
    return application


app = create_app()


def run() -> None:
    settings = get_settings()
    if settings.app_mode == "local" and not _is_loopback(settings.app_host):
        logger.warning(
            "Local mode is bound to %s, not a loopback address. Requests are still "
            "accepted only for localhost host names; use APP_MODE=public for a "
            "network-facing deployment.",
            settings.app_host,
        )
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.listen_port,
        log_level=settings.log_level,
        proxy_headers=False,
        reload=False,
    )


if __name__ == "__main__":
    run()
