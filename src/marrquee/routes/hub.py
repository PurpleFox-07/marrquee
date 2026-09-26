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
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from marrquee import words
from marrquee.addresses import authority_from_headers, proxy_suspected
from marrquee.catalog import RECYCLARR_APP_ID, get_app
from marrquee.config import Settings
from marrquee.deploy import AddStart, DeployManager, DeploySnapshot
from marrquee.docker_client import DockerEngine
from marrquee.hardlinks import HardlinkMonitor
from marrquee.health import LinkProbe, read_health, read_link_health
from marrquee.hub import (
    HUB_ADD_POLL_MS,
    HUB_POLL_MS,
    LOGIN_HELP_URL,
    HubPanel,
    HubView,
    LoginFormState,
    SeedingFormState,
    VpnFormState,
    WithoutVpnState,
    hub_panel,
    hub_view,
    login_view,
    running_without_vpn,
    vpn_prefill,
)
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
from marrquee.login import (
    CHANGE_STEP,
    LOGIN_RESET_STEP,
    LOGIN_STEP,
    load_login,
    login_status,
    password_matches,
    save_login,
)
from marrquee.questions import SEEDING_STEP, VPN_STEP, check_step, load_answers, save_step_answers
from marrquee.recyclarr import RecyclarrControl, SyncStatus
from marrquee.state import load_state
from marrquee.vpn import VPN_APP_ID, TunnelPlace
from marrquee.vpn_control import GluetunControl
from marrquee.without_vpn import (
    clear_without_vpn,
    next_stage,
    save_without_vpn,
    without_vpn_confirmed,
)

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
    live poll (or the reverse) - the drive check's own amber note included.

    The Docker read and the link checks run under one `asyncio.gather`, so
    a slow or unreachable link never adds its own wait on top of the Docker
    read - the same reasoning `read_health` and `read_link_health` each
    apply within their own gather. When a VPN is installed, Gluetun's own
    public-IP lookup joins that same gather as a third member, so a hung
    control server never adds its own wait either - its own client keeps a
    short timeout and never raises.

    `monitor.refresh_if_due()` is called exactly once here, whatever the
    saved result's age - the monitor's own dedupe (Chunk 2) is what keeps
    that to at most one check in flight, even though this function has
    several callers (the page, the poll, and every write route's own
    re-render).
    """
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy
    engine: DockerEngine = request.app.state.docker_engine
    link_probe: LinkProbe = request.app.state.link_probe
    monitor: HardlinkMonitor = request.app.state.hardlinks
    recyclarr: RecyclarrControl = request.app.state.recyclarr

    snapshot = manager.snapshot()
    app_ids = tuple(app.app_id for app in snapshot.apps)
    links = load_links(settings.config_dir)

    async def _sync_status() -> SyncStatus | None:
        # Asked for only when Recyclarr is actually installed - the same
        # "ask nothing you don't need to" rule `read_link_health` already
        # follows for a Hub with no saved links.
        if RECYCLARR_APP_ID not in app_ids:
            return None
        return await recyclarr.status()

    # Gluetun's own key only exists once the VPN is actually installed - a
    # deploy with no VPN never touches its control server at all, the same
    # "ask nothing you don't need to" rule `read_link_health` already
    # follows for a Hub with no saved links.
    install = load_state(settings.config_dir)
    vpn_key = install.api_keys.get("gluetun") if install is not None else None
    vpn_place: TunnelPlace | None
    if "gluetun" in app_ids and vpn_key:
        vpn_control: GluetunControl = request.app.state.vpn_control
        healths, link_healths, vpn_place, recyclarr_status = await asyncio.gather(
            read_health(engine, app_ids),
            read_link_health(link_probe, links),
            vpn_control.public_ip(vpn_key),
            _sync_status(),
        )
    else:
        vpn_place = None
        healths, link_healths, recyclarr_status = await asyncio.gather(
            read_health(engine, app_ids),
            read_link_health(link_probe, links),
            _sync_status(),
        )

    login = login_view(
        load_login(settings.config_dir),
        reset_value=settings.reset_login,
        installed=app_ids,
        running_line=manager.login_progress(),
    )

    monitor.refresh_if_due()

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
        login=login,
        vpn_place=vpn_place,
        without_vpn=without_vpn_confirmed(settings.config_dir),
        drive=monitor.latest(),
        recyclarr=recyclarr_status,
    )


@router.get("/", response_class=HTMLResponse)
async def get_hub(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    if load_state(settings.config_dir) is None:
        return RedirectResponse("/setup/apps", status_code=303)

    manager: DeployManager = request.app.state.deploy
    snapshot = manager.snapshot()
    if snapshot.phase != "finale":
        return RedirectResponse("/deploy", status_code=303)

    view = await read_hub_view(request)
    links = load_links(settings.config_dir)
    panel = hub_panel(request.query_params.get("panel"), request.query_params.get("link"), links)
    if panel.mode == "seeding" and not _qbittorrent_installed(snapshot):
        # `hub_panel` never sees the deploy snapshot - it only knows the
        # query string asked for the seeding pane, not whether qBittorrent
        # is actually there to change anything about.
        panel = hub_panel(None, None, links)
    if panel.mode == "vpn" and view.vpn_pane is None:
        panel = hub_panel(None, None, links)
    if panel.mode == "without-vpn" and not view.without_vpn_offer:
        panel = hub_panel(None, None, links)
    return _hub_response(request, view, panel)


def _qbittorrent_installed(snapshot: DeploySnapshot) -> bool:
    return any(progress.app_id == "qbittorrent" for progress in snapshot.apps)


def _hub_response(
    request: Request,
    view: HubView,
    panel: HubPanel,
    *,
    status_code: int = 200,
    login_form: LoginFormState | None = None,
    change_form: LoginFormState | None = None,
    seeding_form: SeedingFormState | None = None,
    vpn_form: VpnFormState | None = None,
    without_vpn_state: WithoutVpnState | None = None,
) -> Response:
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates
    # The choose banner and the reset banner ask the same three questions
    # through the same `check_step` - only the step's title/lede change, so
    # the template never has to choose between the two itself.
    login_step = (
        LOGIN_RESET_STEP if view.login is not None and view.login.banner == "reset" else LOGIN_STEP
    )
    if seeding_form is None:
        # A plain open (no post behind it) prefills the pane from whatever's
        # already saved - the same answer `seeding_preferences` would fall
        # back to reading on the next wiring run.
        saved = load_answers(settings.config_dir).get("qbittorrent", {})
        seeding_form = SeedingFormState(answers=saved, problem=None, problem_field=None)
    if vpn_form is None:
        # Same "prefill from whatever's already saved" idea as the seeding
        # pane above, kept to only the keys that are ever safe to show back.
        saved_vpn = load_answers(settings.config_dir).get(VPN_APP_ID, {})
        vpn_form = VpnFormState(answers=vpn_prefill(saved_vpn), problem=None, problem_field=None)
    if without_vpn_state is None:
        without_vpn_state = WithoutVpnState(stage=1, problem=None)
    context = {
        "view": view,
        "words": words,
        "poll_ms": HUB_POLL_MS,
        "add_poll_ms": HUB_ADD_POLL_MS,
        "panel": panel,
        "login_form": login_form,
        "change_form": change_form,
        "login_step": login_step,
        "change_step": CHANGE_STEP,
        "seeding_step": SEEDING_STEP,
        "seeding_form": seeding_form,
        "vpn_step": VPN_STEP,
        "vpn_form": vpn_form,
        "without_vpn_state": without_vpn_state,
        "login_help_url": LOGIN_HELP_URL,
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


# --- The one saved login: choose, change and retry ---------------------------


async def _login_choose_refusal(
    request: Request, *, username: str, problem: str, problem_field: str | None
) -> Response:
    """Re-render the live Hub with the choose/reset banner's own refusal -
    the username kept, every password box left blank, and nothing saved.
    """
    view = await read_hub_view(request)
    settings: Settings = request.app.state.settings
    links = load_links(settings.config_dir)
    panel = hub_panel(request.query_params.get("panel"), request.query_params.get("link"), links)
    login_form = LoginFormState(
        answers={"username": username}, problem=problem, problem_field=problem_field
    )
    return _hub_response(request, view, panel, login_form=login_form)


async def _login_change_refusal(
    request: Request, *, username: str, problem: str, problem_field: str | None
) -> Response:
    """Re-render the live Hub with the panel forced open on the login pane
    - a Change refusal always comes from that pane, whatever `?panel=` the
    request itself carried.
    """
    view = await read_hub_view(request)
    change_form = LoginFormState(
        answers={"username": username}, problem=problem, problem_field=problem_field
    )
    panel = HubPanel(mode="login", edit=None, label="", url="", error=None)
    return _hub_response(request, view, panel, change_form=change_form)


@router.post("/hub/login", response_class=HTMLResponse)
async def post_hub_login(request: Request) -> Response:
    """Choose the login for the first time, or answer an outstanding reset -
    the same step (`LOGIN_STEP`) either way, since neither one needs an old
    password to check against.
    """
    form = await request.form()
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy

    record = load_login(settings.config_dir)
    if login_status(record, settings.reset_login) not in ("none", "reset"):
        return RedirectResponse("/", status_code=303)

    if manager.is_busy():
        return await _login_choose_refusal(
            request,
            username=_form_value(form, "username"),
            problem=words.HUB_LOGIN_BUSY,
            problem_field=None,
        )

    posted = {field.name: _form_value(form, field.name) for field in LOGIN_STEP.fields}
    check = check_step(LOGIN_STEP, posted, {})
    if not check.ok:
        return await _login_choose_refusal(
            request,
            username=check.answers.get("username", _form_value(form, "username")),
            problem=check.problem or "",
            problem_field=check.field,
        )

    save_login(
        settings.config_dir,
        check.answers["username"],
        check.answers["password"],
        honor_reset=settings.reset_login,
    )
    manager.apply_login()
    return RedirectResponse("/", status_code=303)


@router.post("/hub/login/change", response_class=HTMLResponse)
async def post_hub_login_change(request: Request) -> Response:
    """Change the username and/or password - the current password is
    required, and a blank new password (and confirmation) keeps the one
    already saved.
    """
    form = await request.form()
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy

    record = load_login(settings.config_dir)
    if login_status(record, settings.reset_login) != "set" or record.login is None:
        return RedirectResponse("/", status_code=303)

    if manager.is_busy():
        return await _login_change_refusal(
            request,
            username=_form_value(form, "username"),
            problem=words.HUB_LOGIN_BUSY,
            problem_field=None,
        )

    posted = {field.name: _form_value(form, field.name) for field in CHANGE_STEP.fields}
    check = check_step(CHANGE_STEP, posted, {})
    if not check.ok:
        return await _login_change_refusal(
            request,
            username=check.answers.get("username", _form_value(form, "username")),
            problem=check.problem or "",
            problem_field=check.field,
        )

    if not password_matches(record.login, check.answers["current_password"]):
        return await _login_change_refusal(
            request,
            username=check.answers.get("username", _form_value(form, "username")),
            problem=words.CHANGE_PROBLEM_WRONG_CURRENT,
            problem_field="current_password",
        )

    new_username = check.answers["username"]
    new_password = check.answers["password"]
    if new_username == record.login.username and new_password == "":
        return await _login_change_refusal(
            request,
            username=new_username,
            problem=words.CHANGE_PROBLEM_NOTHING,
            problem_field=None,
        )

    save_login(
        settings.config_dir,
        new_username,
        new_password or record.login.password,
        honor_reset=None,
    )
    manager.apply_login()
    return RedirectResponse("/", status_code=303)


@router.post("/hub/login/retry")
async def post_hub_login_retry(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy
    record = load_login(settings.config_dir)
    if login_status(record, settings.reset_login) == "set" and not manager.is_busy():
        manager.apply_login()
    return RedirectResponse("/", status_code=303)


# --- qBittorrent's own seeding answer, changed straight from the Hub --------


async def _seeding_refusal(
    request: Request, *, answers: Mapping[str, str], problem: str, problem_field: str | None
) -> Response:
    """Re-render the live Hub with the panel forced open on the seeding pane
    - a refusal or a busy re-render always comes from that pane, whatever
    `?panel=` the request itself carried, and nothing is ever saved on this
    path.
    """
    view = await read_hub_view(request)
    seeding_form = SeedingFormState(answers=answers, problem=problem, problem_field=problem_field)
    panel = HubPanel(mode="seeding", edit=None, label="", url="", error=None)
    return _hub_response(request, view, panel, seeding_form=seeding_form)


@router.post("/hub/seeding", response_class=HTMLResponse)
async def post_hub_seeding(request: Request) -> Response:
    """Change how long qBittorrent keeps sharing - the same question the
    "+" panel already asks, answered again and re-applied everywhere the
    first answer was: `reconnect("qbittorrent")` re-runs qBittorrent's own
    settings step and both partners' download-client steps, so one saved
    answer is the one path that changes seeding anywhere at all.
    """
    form = await request.form()
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy

    snapshot = manager.snapshot()
    if not _qbittorrent_installed(snapshot):
        return RedirectResponse("/", status_code=303)

    posted = {field.name: _form_value(form, field.name) for field in SEEDING_STEP.fields}

    if manager.is_busy() or snapshot.adding is not None:
        return await _seeding_refusal(
            request, answers=posted, problem=words.HUB_SEEDING_BUSY, problem_field=None
        )

    saved = load_answers(settings.config_dir).get("qbittorrent", {})
    check = check_step(SEEDING_STEP, posted, saved)
    if not check.ok:
        return await _seeding_refusal(
            request,
            answers=check.answers,
            problem=check.problem or "",
            problem_field=check.field,
        )

    result = manager.reconnect("qbittorrent")
    if result != "started":
        return await _seeding_refusal(
            request, answers=check.answers, problem=words.HUB_SEEDING_BUSY, problem_field=None
        )

    # Saved right after `reconnect` starts, before this handler's next
    # `await` - the run only reads `answers.json` once it actually begins,
    # so the choice it applies is always this one, never a stale one still
    # on disk when the run was scheduled.
    save_step_answers(settings.config_dir, "qbittorrent", check.answers)
    return RedirectResponse("/", status_code=303)


# --- The VPN: "Add your VPN", "Change VPN", and the break-glass escape ------


async def _vpn_refusal(
    request: Request, *, answers: Mapping[str, str], problem: str, problem_field: str | None
) -> Response:
    """Re-render the live Hub with the panel forced open on the vpn pane -
    a refusal or a busy re-render always comes from that pane, whatever
    `?panel=` the request itself carried, and nothing is ever saved on this
    path. `answers` is filtered to the safe keys before it ever reaches a
    template, the same as a plain open.
    """
    view = await read_hub_view(request)
    vpn_form = VpnFormState(
        answers=vpn_prefill(answers), problem=problem, problem_field=problem_field
    )
    panel = HubPanel(mode="vpn", edit=None, label="", url="", error=None)
    return _hub_response(request, view, panel, vpn_form=vpn_form)


@router.post("/hub/vpn", response_class=HTMLResponse)
async def post_hub_vpn(request: Request) -> Response:
    """ "Add your VPN", "Change VPN", and a failed one of either's own retry
    (with a chance to fix the answers first) - all three post here, since
    all three ask the same question (`VPN_STEP`) before starting the same
    kind of run.

    Unlike the install endpoint, the run is started BEFORE the new answers
    are saved: a busy refusal must never overwrite a working login the
    owner hasn't actually asked to replace yet.
    """
    form = await request.form()
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy

    snapshot = manager.snapshot()
    if load_state(settings.config_dir) is None or snapshot.phase != "finale":
        return RedirectResponse("/", status_code=303)

    adding = snapshot.adding
    present_ids = {progress.app_id for progress in snapshot.apps}
    posted = {field.name: _form_value(form, field.name) for field in VPN_STEP.fields}

    branch: Literal["retry", "change", "add"]
    if adding is not None and adding.app_id == VPN_APP_ID and adding.state == "error":
        branch = "retry"
    elif VPN_APP_ID in present_ids:
        branch = "change"
    elif running_without_vpn(present_ids, adding):
        branch = "add"
    else:
        return RedirectResponse("/", status_code=303)

    if manager.is_busy() or (adding is not None and branch != "retry"):
        return await _vpn_refusal(
            request, answers=posted, problem=words.HUB_VPN_BUSY, problem_field=None
        )

    saved = load_answers(settings.config_dir).get(VPN_APP_ID, {})
    check = check_step(VPN_STEP, posted, saved)
    if not check.ok:
        return await _vpn_refusal(
            request, answers=check.answers, problem=check.problem or "", problem_field=check.field
        )

    result: AddStart
    if branch == "retry":
        result = manager.retry_add()
    elif branch == "change":
        result = manager.change_vpn()
    else:
        result = manager.add_app(VPN_APP_ID)

    if result != "started":
        # A deferred import: `routes/api.py` itself imports `read_hub_view`
        # from this module, so importing its helper back at module load
        # time would be a real cycle - by the time this function actually
        # runs, both modules are already fully loaded.
        from marrquee.routes.api import _add_start_refusal_message

        return await _vpn_refusal(
            request,
            answers=check.answers,
            problem=_add_start_refusal_message(result, get_app(VPN_APP_ID), manager),
            problem_field=None,
        )

    # Saved right after the run starts, before this handler's next `await` -
    # the same ordering `post_hub_seeding` uses, for the same reason: the
    # run only reads `answers.json` once it actually begins.
    save_step_answers(settings.config_dir, VPN_APP_ID, check.answers)
    return RedirectResponse("/", status_code=303)


@router.post("/hub/without-vpn", response_class=HTMLResponse)
async def post_hub_without_vpn(request: Request) -> Response:
    """Walk the break-glass three steps from the Hub's own qBittorrent row -
    the third, typed step is the only one that writes anything at all.
    """
    form = await request.form()
    settings: Settings = request.app.state.settings

    view = await read_hub_view(request)
    if not view.without_vpn_offer:
        return RedirectResponse("/", status_code=303)

    outcome = next_stage(_form_value(form, "stage"), _form_value(form, "typed"))
    if not outcome.confirmed:
        without_vpn_state = WithoutVpnState(stage=outcome.stage, problem=outcome.problem)
        panel = HubPanel(mode="without-vpn", edit=None, label="", url="", error=None)
        return _hub_response(request, view, panel, without_vpn_state=without_vpn_state)

    save_without_vpn(settings.config_dir, now=datetime.now(UTC))
    return RedirectResponse("/?panel=install#hub-panel", status_code=303)


@router.post("/hub/without-vpn/undo")
async def post_hub_without_vpn_undo(request: Request) -> Response:
    """Undo a saved break-glass confirmation, while qBittorrent still isn't
    installed and no add is in flight - the Hub's own "+" row offers this,
    never once qBittorrent exists (there's nothing left to undo by then).
    """
    settings: Settings = request.app.state.settings
    manager: DeployManager = request.app.state.deploy
    snapshot = manager.snapshot()
    installed_ids = {progress.app_id for progress in snapshot.apps}
    if "qbittorrent" not in installed_ids and snapshot.adding is None:
        clear_without_vpn(settings.config_dir)
    return RedirectResponse("/?panel=install#hub-panel", status_code=303)


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


@router.post("/hub/apps/{app_id}/sync")
async def post_hub_app_sync(app_id: str, request: Request) -> Response:
    manager: DeployManager = request.app.state.deploy
    installed = any(
        progress.app_id == app_id and get_app(app_id).kind == "sync"
        for progress in manager.snapshot().apps
    )
    if installed:
        recyclarr: RecyclarrControl = request.app.state.recyclarr
        recyclarr.request_sync()
    return RedirectResponse("/", status_code=303)
