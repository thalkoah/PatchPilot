"""HTTP application. Request handlers enqueue or read state; they do not run agents."""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from patchpilot import __version__
from patchpilot.config import get_settings
from patchpilot.readiness import database_is_ready


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="PatchPilot", version=__version__)

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
