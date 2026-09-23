"""The Deploy screen: `GET /deploy` draws the true current state of the
world on every request, and `POST /deploy` is the one button that starts
it - with JavaScript on or off.

Every decision this route makes is already a tested pure function
elsewhere (`deploy_screen.deploy_view` builds everything the template
draws, `addresses.authority_from_headers` reads the browser's own address);
this module's only job is reading the request, calling those functions,
and handing the result to a template or a redirect.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from marrquee import words
from marrquee.addresses import authority_from_headers
from marrquee.config import Settings
from marrquee.deploy import DeployManager
from marrquee.deploy_screen import deploy_view
from marrquee.state import load_state
from marrquee.wizard import WIZARD_STEPS

router = APIRouter()


@router.get("/deploy", response_class=HTMLResponse)
async def get_deploy(request: Request) -> Response:
    """Render the current frame - ready, running, wiring, finale or error -
    from the owner's saved choices and the engine's live snapshot.

    Nothing here is cached: a mid-deploy refresh draws whatever is true the
    moment the request lands, because the run itself keeps going on the
    server independent of any one browser tab.
    """
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates

    state = load_state(settings.config_dir)
    if state is None:
        return RedirectResponse("/setup/apps", status_code=303)

    manager: DeployManager = request.app.state.deploy
    authority = authority_from_headers(request.headers)
    view = deploy_view(state, manager.snapshot(), authority=authority)

    context = {"view": view, "steps": WIZARD_STEPS, "current_step": 3, "words": words}
    return templates.TemplateResponse(request, "deploy.html", context)


@router.post("/deploy")
async def post_deploy(request: Request) -> Response:
    """The one button on this whole screen - a real form post, so it works
    with JavaScript switched off.

    With nothing saved there is nothing to start; `DeployManager.start()`
    itself is the guard against a double-click starting a second run while
    one is already going.
    """
    settings: Settings = request.app.state.settings
    if load_state(settings.config_dir) is None:
        return RedirectResponse("/setup/apps", status_code=303)

    manager: DeployManager = request.app.state.deploy
    manager.start()
    return RedirectResponse("/deploy", status_code=303)
