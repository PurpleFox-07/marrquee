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
from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import FormData

from marrquee import words
from marrquee.catalog import CATALOG, get_app, unavailable_reason
from marrquee.config import Settings
from marrquee.deploy import DeployManager
from marrquee.docker_client import DockerEngine, detect_host_kind
from marrquee.install import install_apps
from marrquee.questions import (
    QuestionStep,
    check_step,
    find_step,
    load_answers,
    missing_step,
    question_steps_for,
    save_step_answers,
)
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
    step_number,
    timezone_choice,
    timezone_groups,
    wizard_steps,
)

router = APIRouter()


def _hub_exists(request: Request) -> bool:
    """Whether a deploy has already reached its finale.

    Every `/setup/...` screen answers 303 "/" once this is true - a stale
    tab re-saving choices after setup is done must never resurrect the
    wizard or, worse, silently wipe the Hub `return_to_ready()` would
    otherwise trigger. `phase == "error"` (a first deploy that never
    finished) is deliberately NOT covered - that owner still needs the
    wizard.
    """
    manager: DeployManager = request.app.state.deploy
    return manager.snapshot().phase == "finale"


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
    if _hub_exists(request):
        return RedirectResponse("/", status_code=303)
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates

    selected = parse_app_ids(request.query_params.get("apps", ""))
    if not selected:
        state = load_state(settings.config_dir)
        selected = state.app_ids if state is not None else DEFAULT_APP_IDS

    context = await _apps_context(request, selected=selected, refusal=None)
    return templates.TemplateResponse(request, "wizard_apps.html", context)


def _first_unavailable_ticked_app(selected: tuple[str, ...]) -> str | None:
    """The refusal sentence for the first ticked app whose own rules aren't
    met by the *other* ticked apps, or `None` when every ticked app is fine.

    Checked in the same catalog order `selected` is already sorted in, so
    the refusal an owner sees never depends on click order.
    """
    for app_id in selected:
        app = get_app(app_id)
        other_ids = tuple(candidate for candidate in selected if candidate != app_id)
        reason = unavailable_reason(app, other_ids)
        if reason is not None:
            return words.wizard_app_unavailable(app.name, reason)
    return None


@router.post("/setup/apps", response_class=HTMLResponse)
async def post_setup_apps(request: Request) -> Response:
    if _hub_exists(request):
        return RedirectResponse("/", status_code=303)
    templates: Jinja2Templates = request.app.state.templates
    form = await request.form()
    submitted = [value for value in form.getlist("apps") if isinstance(value, str)]
    selected = parse_app_ids(submitted)

    if not selected:
        context = await _apps_context(request, selected=(), refusal=words.WIZARD_PICK_AT_LEAST_ONE)
        return templates.TemplateResponse(request, "wizard_apps.html", context)

    refusal = _first_unavailable_ticked_app(selected)
    if refusal is not None:
        context = await _apps_context(request, selected=selected, refusal=refusal)
        return templates.TemplateResponse(request, "wizard_apps.html", context)

    apps_csv = ",".join(selected)
    first_question = question_steps_for(selected)
    if first_question:
        step = first_question[0]
        return RedirectResponse(
            f"/setup/questions/{step.app_id}/{step.step_id}?apps={apps_csv}", status_code=303
        )
    return RedirectResponse(f"/setup/drive?apps={apps_csv}", status_code=303)


# --- Screen one-and-a-half: each ticked app's own questions ------------------
#
# One page per registered `QuestionStep`, in `question_steps_for`'s order -
# the same order `wizard_steps` turns into pills. Every page posts to
# itself; a pass walks forward to the next step (or to drive, once there
# isn't one); nothing here is reachable for an app that isn't ticked or a
# step nobody registered.


def _step_index(app_ids: tuple[str, ...], app_id: str, step_id: str) -> int:
    """Where `(app_id, step_id)` falls in `question_steps_for(app_ids)`, or
    -1 when it isn't there at all (an app that fell off the ticked list
    between one page and the next).
    """
    registered = question_steps_for(app_ids)
    for index, candidate in enumerate(registered):
        if candidate.app_id == app_id and candidate.step_id == step_id:
            return index
    return -1


async def _questions_context(
    request: Request,
    *,
    app_ids: tuple[str, ...],
    step: QuestionStep,
    answers: Mapping[str, str],
    problem: str | None,
    problem_field: str | None,
) -> dict[str, object]:
    apps_csv = ",".join(app_ids)
    steps = wizard_steps(app_ids)
    key = f"q:{step.app_id}:{step.step_id}"
    index = _step_index(app_ids, step.app_id, step.step_id)
    registered = question_steps_for(app_ids)
    if index > 0:
        previous = registered[index - 1]
        back_url = f"/setup/questions/{previous.app_id}/{previous.step_id}?apps={apps_csv}"
    else:
        back_url = f"/setup/apps?apps={apps_csv}"
    return {
        "steps": steps,
        "current_step": step_number(steps, key),
        "step": step,
        "answers": answers,
        "problem": problem,
        "problem_field": problem_field,
        "apps_csv": apps_csv,
        "back_url": back_url,
        "warning": await _platform_warning(request),
        "words": words,
    }


@router.get("/setup/questions/{app_id}/{step_id}", response_class=HTMLResponse)
async def get_setup_question(app_id: str, step_id: str, request: Request) -> Response:
    if _hub_exists(request):
        return RedirectResponse("/", status_code=303)
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates

    app_ids = parse_app_ids(request.query_params.get("apps", ""))
    step = find_step(app_id, step_id) if app_id in app_ids else None
    if step is None:
        return RedirectResponse("/setup/apps", status_code=303)

    saved = load_answers(settings.config_dir).get(app_id, {})
    context = await _questions_context(
        request, app_ids=app_ids, step=step, answers=saved, problem=None, problem_field=None
    )
    return templates.TemplateResponse(request, "wizard_questions.html", context)


@router.post("/setup/questions/{app_id}/{step_id}", response_class=HTMLResponse)
async def post_setup_question(app_id: str, step_id: str, request: Request) -> Response:
    if _hub_exists(request):
        return RedirectResponse("/", status_code=303)
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates
    form = await request.form()

    app_ids = parse_app_ids(_form_value(form, "apps"))
    step = find_step(app_id, step_id) if app_id in app_ids else None
    if step is None:
        return RedirectResponse("/setup/apps", status_code=303)

    saved = load_answers(settings.config_dir).get(app_id, {})
    posted = {field.name: _form_value(form, field.name) for field in step.fields}
    check = check_step(step, posted, saved)
    if not check.ok:
        context = await _questions_context(
            request,
            app_ids=app_ids,
            step=step,
            answers=posted,
            problem=check.problem,
            problem_field=check.field,
        )
        return templates.TemplateResponse(request, "wizard_questions.html", context)

    save_step_answers(settings.config_dir, app_id, check.answers)

    apps_csv = ",".join(app_ids)
    index = _step_index(app_ids, app_id, step_id)
    registered = question_steps_for(app_ids)
    if 0 <= index < len(registered) - 1:
        next_step = registered[index + 1]
        return RedirectResponse(
            f"/setup/questions/{next_step.app_id}/{next_step.step_id}?apps={apps_csv}",
            status_code=303,
        )
    return RedirectResponse(f"/setup/drive?apps={apps_csv}", status_code=303)


# --- Screen two: where's your big drive, and your time zone -----------------

# Fixed for the process's whole lifetime (TIMEZONE_ALIASES is built once at
# import), so this is serialised once rather than on every request. The
# select's `data-timezone-aliases` attribute carries it to the browser, so a
# script can translate an old zone name it reads from the browser into the
# current one before looking for a matching `<option>` - the dropdown itself
# never lists the old name.
_TIMEZONE_ALIASES_JSON = json.dumps(TIMEZONE_ALIASES)


def _drive_back_url(app_ids: tuple[str, ...], apps_csv: str) -> str:
    """The last registered question step's own page when one exists, else
    the apps screen these ids came from - so Back never skips a step the
    owner is walking forward through.
    """
    registered = question_steps_for(app_ids)
    if registered:
        last = registered[-1]
        return f"/setup/questions/{last.app_id}/{last.step_id}?apps={apps_csv}"
    return f"/setup/apps?apps={apps_csv}"


def _missing_step_redirect(settings: Settings, app_ids: tuple[str, ...]) -> Response | None:
    """A 303 to the first unanswered question step, or `None` when every
    registered step for `app_ids` already has its answers saved.

    Both `/setup/drive` handlers call this before doing anything else, so
    `install_apps` can never run with a question this app still needs to
    ask left unanswered.
    """
    blocking = missing_step(app_ids, load_answers(settings.config_dir))
    if blocking is None:
        return None
    apps_csv = ",".join(app_ids)
    return RedirectResponse(
        f"/setup/questions/{blocking.app_id}/{blocking.step_id}?apps={apps_csv}",
        status_code=303,
    )


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
    apps_csv = ",".join(app_ids)
    steps = wizard_steps(app_ids)
    return {
        "steps": steps,
        "current_step": step_number(steps, "drive"),
        "apps_csv": apps_csv,
        "back_url": _drive_back_url(app_ids, apps_csv),
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
    if _hub_exists(request):
        return RedirectResponse("/", status_code=303)
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates

    app_ids = parse_app_ids(request.query_params.get("apps", ""))
    if not app_ids:
        return RedirectResponse("/setup/apps", status_code=303)

    blocked = _missing_step_redirect(settings, app_ids)
    if blocked is not None:
        return blocked

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
    if _hub_exists(request):
        return RedirectResponse("/", status_code=303)
    settings: Settings = request.app.state.settings
    templates: Jinja2Templates = request.app.state.templates
    form = await request.form()

    app_ids = parse_app_ids(_form_value(form, "apps"))
    if not app_ids:
        return RedirectResponse("/setup/apps", status_code=303)

    blocked = _missing_step_redirect(settings, app_ids)
    if blocked is not None:
        return blocked

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

    # A finished or failed deploy from before these choices were saved would
    # otherwise leave the owner staring at that old finale with no Deploy
    # button to press. A run still in flight is left alone.
    request.app.state.deploy.return_to_ready()
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
