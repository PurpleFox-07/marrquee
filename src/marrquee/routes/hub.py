"""The Hub: `GET /`, the home page once a deploy has reached `finale`.

Nothing chosen yet still sends a new owner to the wizard, and anything
saved that hasn't finished a deploy still sends a returning owner to
`/deploy` - both unchanged from the wizard's old front door. What's new is
the third branch: once the saved deploy's own phase is `finale`, `/` draws
the Hub instead of looping back to it, built the same way `routes/deploy.py`
builds its own page - read the request, call the pure functions, hand the
result to a template.

`read_hub_view` is that build, and it's the only one: `GET /` and
`GET /api/hub/status` (routes/api.py) both call it, so an app or a link can
never look Up on the page and Down on the live poll.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from marrquee import words
from marrquee.addresses import authority_from_headers, proxy_suspected
from marrquee.config import Settings
from marrquee.deploy import DeployManager
from marrquee.docker_client import DockerEngine
from marrquee.health import LinkProbe, read_health, read_link_health
from marrquee.hub import HUB_ADD_POLL_MS, HUB_POLL_MS, HubPanel, HubView, hub_panel, hub_view
from marrquee.links import (
    LINK_COUNT_MAX,
    LINK_ID_RE,
    LinkCard,
    LinkCheck,
    check_link,
    load_links,
    new_link_id,
    save_links,
)
from marrquee.state import load_state

router = APIRouter()


def _form_value(form: FormData, key: str) -> str:
    """A plain-text form field's value, or "" for anything else (missing,
    an uploaded file where text was expected). Never raises on a malformed
    or malicious post.
    """
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _ready_for_link_edits(request: Request) -> bool:
    """Whether the Hub's own write routes may touch `links.json` at all -
    the same guard `GET /` applies before it draws the Hub, so a link can
    never be added, edited or removed from a Hub that isn't showing yet.
    """
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy
    if load_state(settings.config_dir) is None:
        return False
    return manager.snapshot().phase == "finale"


async def read_hub_view(request: Request) -> HubView:
    """Build the one `HubView` both `GET /` and `GET /api/hub/status` draw
    from, so an app or a link can never look Up on the page and Down on the
    live poll (or the reverse).

    The Docker read and the link checks run under one `asyncio.gather`, so
    a slow or unreachable link never adds its own wait on top of the Docker
    read - the same reasoning `read_health` and `read_link_health` each
    apply within their own gather.
    """
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy
    engine: DockerEngine = request.app.state.docker_engine
    link_probe: LinkProbe = request.app.state.link_probe

    snapshot = manager.snapshot()
    app_ids = tuple(app.app_id for app in snapshot.apps)
    links = load_links(settings.config_dir)

    healths, link_healths = await asyncio.gather(
        read_health(engine, app_ids),
        read_link_health(link_probe, links),
    )

    return hub_view(
        app_ids,
        healths,
        authority=authority_from_headers(request.headers),
        proxied=proxy_suspected(request.headers),
        now=datetime.now(UTC),
        links=links,
        link_healths=link_healths,
        adding=snapshot.adding,
        wiring_gaps=snapshot.wiring_gaps,
        busy=manager.is_busy(),
    )


@router.get("/", response_class=HTMLResponse)
async def get_hub(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    if load_state(settings.config_dir) is None:
        return RedirectResponse("/setup/apps", status_code=303)

    manager: DeployManager = request.app.state.deploy
    if manager.snapshot().phase != "finale":
        return RedirectResponse("/deploy", status_code=303)

    view = await read_hub_view(request)
    links = load_links(settings.config_dir)
    panel = hub_panel(request.query_params.get("panel"), request.query_params.get("link"), links)
    return _hub_response(request, view, panel)


def _hub_response(
    request: Request, view: HubView, panel: HubPanel, *, status_code: int = 200
) -> Response:
    templates: Jinja2Templates = request.app.state.templates
    context = {
        "view": view,
        "words": words,
        "poll_ms": HUB_POLL_MS,
        "add_poll_ms": HUB_ADD_POLL_MS,
        "panel": panel,
    }
    return templates.TemplateResponse(request, "hub.html", context, status_code=status_code)


async def _link_refusal(
    request: Request, *, mode: Literal["link", "edit"], edit: LinkCard | None, check: LinkCheck
) -> Response:
    """Re-render the live Hub with the panel open on `mode`, the owner's
    typed (invalid) values still in the boxes and the refusal's own
    sentence shown - nothing is ever saved on this path.
    """
    view = await read_hub_view(request)
    problem = check.problem
    panel = HubPanel(
        mode=mode,
        edit=edit,
        label=check.label,
        url=check.url,
        error=words.link_problem_message(problem) if problem is not None else None,
    )
    return _hub_response(request, view, panel, status_code=200)


@router.post("/hub/links", response_class=HTMLResponse)
async def post_hub_links(request: Request) -> Response:
    form = await request.form()
    label = _form_value(form, "label")
    url = _form_value(form, "url")

    if not _ready_for_link_edits(request):
        return RedirectResponse("/", status_code=303)

    settings: Settings = request.app.state.settings
    links = load_links(settings.config_dir)
    check = check_link(label, url)
    if check.ok and len(links) >= LINK_COUNT_MAX:
        check = LinkCheck(False, check.label, check.url, "too_many")
    if not check.ok:
        return await _link_refusal(request, mode="link", edit=None, check=check)

    save_links(settings.config_dir, (*links, LinkCard(new_link_id(), check.label, check.url)))
    return RedirectResponse("/", status_code=303)


@router.post("/hub/links/{link_id}", response_class=HTMLResponse)
async def post_hub_link_edit(link_id: str, request: Request) -> Response:
    form = await request.form()
    label = _form_value(form, "label")
    url = _form_value(form, "url")

    if not _ready_for_link_edits(request) or not LINK_ID_RE.match(link_id):
        return RedirectResponse("/", status_code=303)

    settings: Settings = request.app.state.settings
    links = load_links(settings.config_dir)
    index = next((i for i, card in enumerate(links) if card.id == link_id), None)
    if index is None:
        return RedirectResponse("/", status_code=303)

    check = check_link(label, url)
    if not check.ok:
        return await _link_refusal(request, mode="edit", edit=links[index], check=check)

    updated = list(links)
    updated[index] = LinkCard(link_id, check.label, check.url)
    save_links(settings.config_dir, updated)
    return RedirectResponse("/", status_code=303)


@router.post("/hub/links/{link_id}/remove")
async def post_hub_link_remove(link_id: str, request: Request) -> Response:
    if not _ready_for_link_edits(request) or not LINK_ID_RE.match(link_id):
        return RedirectResponse("/", status_code=303)

    settings: Settings = request.app.state.settings
    links = load_links(settings.config_dir)
    remaining = tuple(card for card in links if card.id != link_id)
    if len(remaining) != len(links):
        save_links(settings.config_dir, remaining)
    return RedirectResponse("/", status_code=303)


# --- Adding an app: Try again, Cancel and Connect again are form posts -------
#
# All three always answer 303 to `/`, whatever they did or didn't do - the
# Hub itself is the only place their result is ever shown, so there is
# nothing else worth redirecting to.


@router.post("/hub/apps/{app_id}/retry")
async def post_hub_app_retry(app_id: str, request: Request) -> Response:
    manager: DeployManager = request.app.state.deploy
    adding = manager.snapshot().adding
    if adding is not None and adding.app_id == app_id:
        manager.retry_add()
    return RedirectResponse("/", status_code=303)


@router.post("/hub/apps/{app_id}/cancel")
async def post_hub_app_cancel(app_id: str, request: Request) -> Response:
    manager: DeployManager = request.app.state.deploy
    adding = manager.snapshot().adding
    if adding is not None and adding.app_id == app_id:
        await manager.cancel_add()
    return RedirectResponse("/", status_code=303)


@router.post("/hub/apps/{app_id}/reconnect")
async def post_hub_app_reconnect(app_id: str, request: Request) -> Response:
    manager: DeployManager = request.app.state.deploy
    if any(progress.app_id == app_id for progress in manager.snapshot().apps):
        manager.reconnect(app_id)
    return RedirectResponse("/", status_code=303)
