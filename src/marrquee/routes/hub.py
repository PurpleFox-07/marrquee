"""The Hub: `GET /`, the home page once a deploy has reached `finale`.

Nothing chosen yet still sends a new owner to the wizard, and anything
saved that hasn't finished a deploy still sends a returning owner to
`/deploy` - both unchanged from the wizard's old front door. What's new is
the third branch: once the saved deploy's own phase is `finale`, `/` draws
the Hub instead of looping back to it, built the same way `routes/deploy.py`
builds its own page - read the request, call the pure functions, hand the
result to a template.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from marrquee import words
from marrquee.addresses import authority_from_headers, proxy_suspected
from marrquee.config import Settings
from marrquee.deploy import DeployManager
from marrquee.docker_client import DockerEngine
from marrquee.health import read_health
from marrquee.hub import HUB_POLL_MS, hub_view
from marrquee.state import load_state

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def get_hub(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    if load_state(settings.config_dir) is None:
        return RedirectResponse("/setup/apps", status_code=303)

    manager: DeployManager = request.app.state.deploy
    snapshot = manager.snapshot()
    if snapshot.phase != "finale":
        return RedirectResponse("/deploy", status_code=303)

    engine: DockerEngine = request.app.state.docker_engine
    templates: Jinja2Templates = request.app.state.templates

    app_ids = tuple(app.app_id for app in snapshot.apps)
    view = hub_view(
        app_ids,
        await read_health(engine, app_ids),
        authority=authority_from_headers(request.headers),
        proxied=proxy_suspected(request.headers),
        now=datetime.now(UTC),
    )

    context = {"view": view, "words": words, "poll_ms": HUB_POLL_MS}
    return templates.TemplateResponse(request, "hub.html", context)
