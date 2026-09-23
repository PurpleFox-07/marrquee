"""The alive page and its health check.

Two handlers, two line-builders and one dataclass - kept small on purpose so
a wording change is a one-line diff in the copy tables below, not a hunt
through template logic.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from marrquee import __version__, words
from marrquee.config import ConfigDirStatus, Settings, ensure_config_dir
from marrquee.deploy import read_last_failure
from marrquee.docker_client import DockerEngine, DockerFailure, DockerStatus

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


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """The container's HEALTHCHECK target.

    Never touches the Docker engine: if health depended on Docker, a Docker
    hiccup on the NAS would restart-loop the one page that could explain it.
    """
    return {"status": "ok", "app": "marrquee", "version": __version__}


@router.get("/diagnostics", response_class=HTMLResponse)
async def alive(request: Request) -> Response:
    """Render the diagnostics page, re-running both checks on every request.

    Nothing here is cached, so "Check again" is a real re-check rather than a
    page that could keep saying "broken" after the owner fixes it. This page
    used to answer at "/", before the setup wizard existed to take that
    address instead.
    """
    settings: Settings = request.app.state.settings
    engine: DockerEngine = request.app.state.docker_engine
    templates: Jinja2Templates = request.app.state.templates

    docker_status = await engine.status()
    config_status = ensure_config_dir(settings.config_dir)
    last_problem = read_last_failure(settings.config_dir)

    lines = [docker_status_line(docker_status), config_status_line(config_status)]
    context = {
        "lines": lines,
        "version": __version__,
        "words": words,
        "last_problem": last_problem,
    }
    return templates.TemplateResponse(request, "alive.html", context)
