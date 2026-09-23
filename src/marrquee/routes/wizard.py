"""The two no-JavaScript screens of the setup wizard - pick your apps, then
where's your big drive and your time zone.

Every decision either route makes (which apps are ticked, which sentence to
show, whether a path is usable) is already a tested pure function in
`wizard.py`, `install.py` or `storage.py`; this module's only job is reading
the request, calling those functions, and handing the result to a template.
No JavaScript ships with either screen - every `POST` here is a real form
post, answered with a real redirect or a real re-render.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import FormData

from marrquee import words
from marrquee.catalog import CATALOG
from marrquee.config import Settings
from marrquee.docker_client import DockerEngine, detect_host_kind
from marrquee.install import install_apps
from marrquee.state import load_state
from marrquee.storage import shared_roots
from marrquee.wizard import (
    DEFAULT_APP_IDS,
    TIMEZONE_ALIASES,
    WIZARD_STEPS,
    DriveMessage,
    assess,
    default_timezone,
    parse_app_ids,
    platform_warning,
    timezone_choice,
    timezone_groups,
)

router = APIRouter()

# Mirrors the limits `/api/storage/check` already validates against - the
# live check is the same kind of request, just made far more often, and a
# request this size was never a real folder path to begin with.
_MAX_PATH_LENGTH = 4096
_MAX_APP_ID_LENGTH = 64
_MAX_APP_COUNT = 50


def _form_value(form: FormData, key: str) -> str:
    """A plain-text form field's value, or "" for anything else (missing,
    an uploaded file where text was expected). Never raises on a malformed
    or malicious post.
    """
    value = form.get(key)
    return value if isinstance(value, str) else ""


async def _platform_warning(request: Request) -> str | None:
    engine: DockerEngine = request.app.state.docker_engine
    status = await engine.status()
    return platform_warning(detect_host_kind(status))


async def _apps_context(
    request: Request, *, selected: tuple[str, ...], refusal: str | None
) -> dict[str, object]:
    return {
        "steps": WIZARD_STEPS,
        "current_step": 1,
        "apps": CATALOG,
        "selected": selected,
        "refusal": refusal,
        "warning": await _platform_warning(request),
        "words": words,
    }


@router.get("/setup/apps", response_class=HTMLResponse)
async def get_setup_apps(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates

    selected = parse_app_ids(request.query_params.get("apps", ""))
    if not selected:
        state = load_state(settings.config_dir)
        selected = state.app_ids if state is not None else DEFAULT_APP_IDS

    context = await _apps_context(request, selected=selected, refusal=None)
    return templates.TemplateResponse(request, "wizard_apps.html", context)


@router.post("/setup/apps", response_class=HTMLResponse)
async def post_setup_apps(request: Request) -> Response:
    form = await request.form()
    submitted = [value for value in form.getlist("apps") if isinstance(value, str)]
    selected = parse_app_ids(submitted)

    if not selected:
        templates: Jinja2Templates = request.app.state.templates
        context = await _apps_context(request, selected=(), refusal=words.WIZARD_PICK_AT_LEAST_ONE)
        return templates.TemplateResponse(request, "wizard_apps.html", context)

    return RedirectResponse(f"/setup/drive?apps={','.join(selected)}", status_code=303)


# --- Screen two: where's your big drive, and your time zone -----------------

# Fixed for the process's whole lifetime (TIMEZONE_ALIASES is built once at
# import), so this is serialised once rather than on every request. The
# select's `data-timezone-aliases` attribute carries it to the browser, so a
# script can translate an old zone name it reads from the browser into the
# current one before looking for a matching `<option>` - the dropdown itself
# never lists the old name.
_TIMEZONE_ALIASES_JSON = json.dumps(TIMEZONE_ALIASES)


async def _drive_context(
    request: Request,
    *,
    app_ids: tuple[str, ...],
    path: str,
    message: DriveMessage | None,
    refusal: str | None,
    timezone_value: str,
    timezone_source: str,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    return {
        "steps": WIZARD_STEPS,
        "current_step": 2,
        "apps_csv": ",".join(app_ids),
        "path": path,
        "hint": words.wizard_path_hint([str(root) for root in shared_roots(settings)]),
        "message": message,
        "refusal": refusal,
        "timezone_groups": timezone_groups(),
        "timezone_value": timezone_value,
        "timezone_source": timezone_source,
        "timezone_aliases_json": _TIMEZONE_ALIASES_JSON,
        "warning": await _platform_warning(request),
        "words": words,
    }


@router.get("/setup/drive", response_class=HTMLResponse)
async def get_setup_drive(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates

    app_ids = parse_app_ids(request.query_params.get("apps", ""))
    if not app_ids:
        return RedirectResponse("/setup/apps", status_code=303)

    state = load_state(settings.config_dir)
    timezone_value, timezone_source = default_timezone(settings, state)

    context = await _drive_context(
        request,
        app_ids=app_ids,
        path="",
        message=None,
        refusal=None,
        timezone_value=timezone_value,
        timezone_source=timezone_source,
    )
    return templates.TemplateResponse(request, "wizard_drive.html", context)


@router.post("/setup/drive", response_class=HTMLResponse)
async def post_setup_drive(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates
    form = await request.form()

    app_ids = parse_app_ids(_form_value(form, "apps"))
    if not app_ids:
        return RedirectResponse("/setup/apps", status_code=303)

    typed_path = _form_value(form, "path")
    use_suggestion = _form_value(form, "use_suggestion")
    path = use_suggestion or typed_path

    default_zone, _default_source = default_timezone(settings, load_state(settings.config_dir))
    posted_timezone = form.get("timezone")
    timezone_value = timezone_choice(
        posted_timezone if isinstance(posted_timezone, str) else None, default_zone
    )

    usable, message = assess(settings, path, app_ids)
    if not usable:
        context = await _drive_context(
            request,
            app_ids=app_ids,
            path=path,
            message=message,
            refusal=None,
            timezone_value=timezone_value,
            timezone_source="posted",
        )
        return templates.TemplateResponse(request, "wizard_drive.html", context)

    result = install_apps(settings, path, list(app_ids), timezone=timezone_value)
    if not result.ok:
        context = await _drive_context(
            request,
            app_ids=app_ids,
            path=path,
            message=None,
            refusal=result.message,
            timezone_value=timezone_value,
            timezone_source="posted",
        )
        return templates.TemplateResponse(request, "wizard_drive.html", context)

    return RedirectResponse("/deploy", status_code=303)


# --- Live check: the in-browser script's own answer, JSON not HTML ----------


class DriveCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=_MAX_PATH_LENGTH)
    apps: list[Annotated[str, Field(max_length=_MAX_APP_ID_LENGTH)]] = Field(
        max_length=_MAX_APP_COUNT
    )


class DriveCheckResponse(BaseModel):
    """`DriveMessage` as JSON - the exact shape `wizard.js` renders, and
    nothing else: no `StorageCheck.detail`, no field a screen didn't ask for.
    """

    tone: Literal["ok", "problem", "unknown"]
    glyph: str
    text: str
    suggestion: str | None
    guidance: str | None


@router.post("/setup/drive/check")
async def post_setup_drive_check(body: DriveCheckRequest, request: Request) -> DriveCheckResponse:
    """The same check `POST /setup/drive` runs, answered as JSON for the
    in-browser script instead of a re-rendered page.

    Calls `assess` exactly as the form post does, so a typed path never
    earns a different verdict depending on whether JavaScript asked or a
    real submit did. Touches nothing on disk beyond `check_storage_root`'s
    own writability probe - no folder is built and nothing is saved here.
    """
    settings: Settings = request.app.state.settings
    app_ids = parse_app_ids(body.apps)

    _usable, message = assess(settings, body.path, app_ids)
    return DriveCheckResponse(
        tone=message.tone,
        glyph=message.glyph,
        text=message.text,
        suggestion=message.suggestion,
        guidance=message.guidance,
    )


# --- The front door: land on the wizard, or on the deploy screen ------------


@router.get("/")
async def get_front_door(request: Request) -> Response:
    """Nothing chosen yet sends a new owner straight to the first screen;
    anything saved sends a returning owner on to deploy - the wizard's own
    job is done either way.
    """
    settings: Settings = request.app.state.settings
    if load_state(settings.config_dir) is None:
        return RedirectResponse("/setup/apps", status_code=303)
    return RedirectResponse("/deploy", status_code=303)
