"""The alive page and its health check.

Two handlers and three line-builders (Docker, the settings folder, and the
drive) - kept small on purpose so a wording change is a one-line diff in the
copy tables below, not a hunt through template logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from marrquee import __version__, words
from marrquee.config import ConfigDirStatus, Settings, ensure_config_dir
from marrquee.deploy import read_last_failure
from marrquee.docker_client import DockerEngine, DockerFailure, DockerStatus
from marrquee.hardlinks import HardlinkMonitor, HardlinkReason, HardlinkResult

router = APIRouter()


@dataclass(frozen=True)
class StatusLine:
    """One row on the alive page: a check's result in words the owner can act on.

    `detail` never carries the raw string from the check it summarizes - see
    `docker_status_line` and `config_status_line`, which read `connected` and
    `failure`/`ok` only, never a status object's `detail` field.
    """

    ok: bool
    title: str
    detail: str


# Keyed by DockerFailure so a wording change is a one-line diff, and so the
# type checker flags it if a new DockerFailure value is ever added without
# copy to go with it.
_DOCKER_FAILURE_COPY: dict[DockerFailure, tuple[str, str]] = {
    DockerFailure.SOCKET_MISSING: (
        "Marrquee can't see Docker",
        "The install command needs the line that shares Docker with Marrquee. "
        "Add it, start Marrquee again, then choose Check again.",
    ),
    DockerFailure.PERMISSION_DENIED: (
        "Marrquee isn't allowed to talk to Docker",
        "Marrquee found Docker but was turned away. On most machines this is "
        "fixed by letting Marrquee run as the administrator user.",
    ),
    DockerFailure.NO_ANSWER: (
        "Docker didn't answer",
        "Check that Docker is running on this machine, then choose Check again.",
    ),
    DockerFailure.BAD_RESPONSE: (
        "Docker answered in a way Marrquee didn't understand",
        "This usually means a very old or very new version of Docker. Choose "
        "Check again, and if it keeps happening the project page has a place "
        "to report it.",
    ),
}


def docker_status_line(status: DockerStatus) -> StatusLine:
    """Plain-language copy for the Docker row, chosen from `connected`/`failure` only."""
    if status.connected:
        return StatusLine(
            ok=True,
            title="Talking to Docker",
            detail=f"Docker {status.version} on this machine.",
        )

    # `failure` is always set alongside `connected=False` in practice; falling
    # back to NO_ANSWER's copy rather than raising keeps this function total.
    failure = status.failure or DockerFailure.NO_ANSWER
    title, detail = _DOCKER_FAILURE_COPY[failure]
    return StatusLine(ok=False, title=title, detail=detail)


def config_status_line(status: ConfigDirStatus) -> StatusLine:
    """Plain-language copy for the settings-folder row, chosen from `ok` only."""
    if status.ok:
        return StatusLine(
            ok=True,
            title="Settings folder ready",
            detail="Marrquee can save your choices.",
        )
    return StatusLine(
        ok=False,
        title="Marrquee can't save its settings",
        detail=(
            "The folder Marrquee keeps its settings in can't be written to. "
            "Check the install command's settings folder line, then choose "
            "Check again."
        ),
    )


@dataclass(frozen=True)
class DriveLine:
    """One row for the "Your drive" section.

    `state` carries the icon and its colour; `what_to_do` and `technical`
    are unset (None) for every outcome that isn't a plain failure -
    `works`, `not_needed`, and "no result yet" all have nothing to fix and
    nothing technical to show.
    """

    state: Literal["ok", "idle", "warn"]
    title: str
    detail: str
    what_to_do: str | None
    technical: str | None


# Keyed by HardlinkReason so a new reason the probe ever returns fails type
# checking here until this table is taught its sentence and its fix - the
# same guarantee `_DOCKER_FAILURE_COPY` gives the Docker row above.
_DRIVE_REASON_COPY: dict[HardlinkReason, tuple[Callable[[str], str], str]] = {
    "different_drives": (words.drive_reason_different_drives, words.DRIVE_TODO_SAME_DRIVE),
    "no_hard_links": (words.drive_reason_no_hard_links, words.DRIVE_TODO_NATIVE_DRIVE),
    "not_allowed": (words.drive_reason_not_allowed, words.DRIVE_TODO_PERMISSIONS),
    "drive_full": (words.drive_reason_drive_full, words.DRIVE_TODO_FREE_SPACE),
    "folder_missing": (words.drive_reason_folder_missing, words.DRIVE_TODO_REBUILD_FOLDER),
    "folder_elsewhere": (words.drive_reason_folder_elsewhere, words.DRIVE_TODO_REAL_FOLDER),
    "unexpected": (words.drive_reason_unexpected, words.DRIVE_TODO_ASK_FOR_HELP),
}


def drive_status_line(result: HardlinkResult | None) -> DriveLine:
    """Plain-language copy for the "Your drive" row, chosen from
    `outcome`/`reason` only.

    `result` is None while `check_now` is still waiting on a slow drive -
    that reads exactly like `not_needed` to the owner (nothing to fix) but
    says so honestly, rather than claiming a check that hasn't finished.
    """
    if result is None:
        return DriveLine(
            state="idle",
            title=words.DRIVE_STILL_CHECKING_TITLE,
            detail=words.DRIVE_STILL_CHECKING_DETAIL,
            what_to_do=None,
            technical=None,
        )
    if result.outcome == "works":
        return DriveLine(
            state="ok",
            title=words.DRIVE_WORKS_TITLE,
            detail=words.DRIVE_WORKS_DETAIL,
            what_to_do=None,
            technical=None,
        )
    if result.outcome == "not_needed":
        return DriveLine(
            state="idle",
            title=words.DRIVE_NOT_NEEDED_TITLE,
            detail=words.DRIVE_NOT_NEEDED_DETAIL,
            what_to_do=None,
            technical=None,
        )

    # Only `copies` and `couldnt_check` remain, and `HardlinkResult`'s own
    # invariant gives both of those a reason - "unexpected" is a defensive
    # fallback for a hand-edited saved file, never a case this probe itself
    # produces without a reason attached.
    reason_copy, what_to_do = _DRIVE_REASON_COPY[result.reason or "unexpected"]
    title = (
        words.DRIVE_COPIES_TITLE if result.outcome == "copies" else words.DRIVE_COULDNT_CHECK_TITLE
    )
    return DriveLine(
        state="warn",
        title=title,
        detail=reason_copy(result.folder or ""),
        what_to_do=what_to_do,
        technical=words.drive_technical(result.technical) if result.technical else None,
    )


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """The container's HEALTHCHECK target.

    Never touches the Docker engine: if health depended on Docker, a Docker
    hiccup on the NAS would restart-loop the one page that could explain it.
    """
    return {"status": "ok", "app": "marrquee", "version": __version__}


@router.get("/diagnostics", response_class=HTMLResponse)
async def alive(request: Request) -> Response:
    """Render the diagnostics page, re-running every check on every request.

    Nothing here is cached, so "Check again" is a real re-check rather than a
    page that could keep saying "broken" after the owner fixes it. This page
    used to answer at "/", before the setup wizard existed to take that
    address instead.

    Opening this page also runs the drive check for real, against the
    owner's own folders (`HardlinkMonitor.check_now`) - a burst of GETs
    never runs more than one probe at once, because `check_now` joins
    whichever check is already in flight rather than starting a new one.
    """
    settings: Settings = request.app.state.settings
    engine: DockerEngine = request.app.state.docker_engine
    hardlinks: HardlinkMonitor = request.app.state.hardlinks
    templates: Jinja2Templates = request.app.state.templates

    docker_status = await engine.status()
    config_status = ensure_config_dir(settings.config_dir)
    last_problem = read_last_failure(settings.config_dir)
    drive_result = await hardlinks.check_now()

    lines = [docker_status_line(docker_status), config_status_line(config_status)]
    context = {
        "lines": lines,
        "version": __version__,
        "words": words,
        "last_problem": last_problem,
        "drive": drive_status_line(drive_result),
    }
    return templates.TemplateResponse(request, "alive.html", context)
