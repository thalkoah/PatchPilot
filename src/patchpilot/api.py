"""HTTP application. Request handlers enqueue or read state; they do not run agents."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from patchpilot import __version__
from patchpilot.config import get_settings
from patchpilot.db.session import create_engine, create_session_factory
from patchpilot.readiness import database_is_ready
from patchpilot.routes.tasks import router as tasks_router
from patchpilot.web.routes import STATIC_DIR
from patchpilot.web.routes import router as dashboard_router


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        _app.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="PatchPilot", version=__version__, lifespan=lifespan)
    app.include_router(tasks_router, prefix="/api/v1")
    app.include_router(dashboard_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="dashboard-static")

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        if database_is_ready(settings):
            return JSONResponse(content={"status": "ok", "database": "ready"})
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unavailable"},
        )

    return app
