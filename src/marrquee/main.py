"""The FastAPI application factory.

`create_app` is the seam every later story's tests build on: pass a fake
`Settings` and a fake `DockerEngine` and get back a fully wired app that
never touches a real filesystem path or Docker socket.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from marrquee.config import Settings
from marrquee.docker_client import DockerEngine, SocketDockerEngine
from marrquee.routes.alive import router as alive_router

# Resolved from the installed package, not the repository: the runtime image
# copies only the built venv (no `src/` tree survives), so a path built from
# the repo root would work on a dev machine and break the moment it ships.
_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


def create_app(settings: Settings | None = None, engine: DockerEngine | None = None) -> FastAPI:
    """Build the Marrquee app.

    `settings=None` reads the real environment; `engine=None` talks to the
    real Docker socket named by those settings. Tests pass both explicitly,
    so the test suite never touches a real environment variable or socket.
    """
    if settings is None:
        settings = Settings.from_env()
    if engine is None:
        engine = SocketDockerEngine(settings.docker_socket)

    app = FastAPI(title="Marrquee")
    app.state.settings = settings
    app.state.docker_engine = engine
    app.state.templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(alive_router)

    return app
