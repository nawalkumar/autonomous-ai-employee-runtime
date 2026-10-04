"""FastAPI application entrypoint (Phase 0: health only)."""

import uvicorn
from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.config import get_settings


def create_app() -> FastAPI:
    application = FastAPI(
        title="Autonomous AI Employee Runtime",
        version=__version__,
        description="Foundation setup — agent runtime not yet implemented.",
    )
    application.include_router(health_router)
    return application


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level,
        reload=False,
    )


if __name__ == "__main__":
    run()
