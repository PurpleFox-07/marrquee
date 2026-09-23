"""The JSON/SSE seam every later screen builds on.

A screen never composes its own technical string: it receives only the
shapes this module builds, or words a server-side page already rendered. A
no-JavaScript page may call `storage.py` or `install.py` functions directly
to build that rendering, but it renders only their plain-language result,
never a raw field like `StorageCheck.detail`. Every response here is built
from a small FastAPI (pydantic) model rather than handed the engine's own
dataclasses directly, so "no technical string reaches a screen" is a fact
about the schema, not a habit a future change could forget:
`Failure.technical`, `StorageCheck.detail`, `ContainerSnapshot.detail` and
`ComposeResult.output` have no field on any model below except
`/api/deploy/diagnostics`, which exists specifically to carry them.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from marrquee.catalog import CATALOG
from marrquee.config import Settings
from marrquee.deploy import (
    AppProgress,
    AppState,
    DeployManager,
    DeployPhase,
    DeploySnapshot,
    Failure,
    FailureCode,
)
from marrquee.install import install_apps
from marrquee.state import load_state
from marrquee.storage import StorageCheck, check_storage_root
from marrquee.wiring import WiringStep, WiringStepState
from marrquee.words import REFUSAL_NOTHING_CHOSEN, storage_check_message

router = APIRouter(prefix="/api")

# The same filename `DeployManager` writes failures to - kept here as a
# literal rather than importing a private constant from deploy.py, since
# this route's whole job is reading that one file back, nothing more.
_DIAGNOSTICS_FILE_NAME = "last-failure.txt"

# How often the event stream sends a comment line when nothing has changed,
# so a proxy or browser doesn't decide a quiet connection is a dead one.
_SSE_HEARTBEAT_SECONDS = 15.0

_MAX_PATH_LENGTH = 4096
_MAX_APP_ID_LENGTH = 64
_MAX_APP_COUNT = 50


# --- Request bodies: validated before a single line of business logic runs --


class StorageCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=_MAX_PATH_LENGTH)


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=_MAX_PATH_LENGTH)
    app_ids: list[Annotated[str, Field(max_length=_MAX_APP_ID_LENGTH)]] = Field(
        max_length=_MAX_APP_COUNT
    )


# --- Response shapes: every field a screen is allowed to see, and no other -


class CatalogAppOut(BaseModel):
    id: str
    name: str
    description: str
    port: int


class CatalogResponse(BaseModel):
    apps: list[CatalogAppOut]


class InstallResponse(BaseModel):
    saved: bool


class StorageCheckOut(BaseModel):
    """`StorageCheck` minus `detail` (a raw OS error string), plus `message`."""

    ok: bool
    host_path: str | None
    exists: bool
    is_dir: bool
    writable: bool
    free_bytes: int | None
    total_bytes: int | None
    reason: str | None
    suggestions: tuple[str, ...]
    message: str


class AppProgressOut(BaseModel):
    app_id: str
    name: str
    state: AppState
    chip: str
    line: str
    note: str | None
    port: int


class WiringStepOut(BaseModel):
    index: int
    total: int
    key: str
    line: str
    state: WiringStepState
    chip: str
    note: str | None
    involved: tuple[str, ...]


class FailureOut(BaseModel):
    """`Failure` minus `technical` - the one field this schema exists to drop."""

    code: FailureCode
    headline: str
    what_to_do: str


class DeploySnapshotOut(BaseModel):
    run_id: str
    phase: DeployPhase
    apps: list[AppProgressOut]
    headline: str
    detail: str | None
    failure: FailureOut | None
    started_at: str | None
    finished_at: str | None
    wiring: list[WiringStepOut]


# --- Converting the engine's own dataclasses into the shapes above ----------


def _app_progress_out(app: AppProgress) -> AppProgressOut:
    return AppProgressOut(
        app_id=app.app_id,
        name=app.name,
        state=app.state,
        chip=app.chip,
        line=app.line,
        note=app.note,
        port=app.port,
    )


def _wiring_step_out(step: WiringStep) -> WiringStepOut:
    return WiringStepOut(
        index=step.index,
        total=step.total,
        key=step.key,
        line=step.line,
        state=step.state,
        chip=step.chip,
        note=step.note,
        involved=step.involved,
    )


def _failure_out(failure: Failure | None) -> FailureOut | None:
    if failure is None:
        return None
    return FailureOut(code=failure.code, headline=failure.headline, what_to_do=failure.what_to_do)


def _snapshot_out(snapshot: DeploySnapshot) -> DeploySnapshotOut:
    return DeploySnapshotOut(
        run_id=snapshot.run_id,
        phase=snapshot.phase,
        apps=[_app_progress_out(app) for app in snapshot.apps],
        headline=snapshot.headline,
        detail=snapshot.detail,
        failure=_failure_out(snapshot.failure),
        started_at=snapshot.started_at,
        finished_at=snapshot.finished_at,
        wiring=[_wiring_step_out(step) for step in snapshot.wiring],
    )


def _storage_check_out(check: StorageCheck, path: str) -> StorageCheckOut:
    return StorageCheckOut(
        ok=check.ok,
        host_path=str(check.host_path) if check.host_path is not None else None,
        exists=check.exists,
        is_dir=check.is_dir,
        writable=check.writable,
        free_bytes=check.free_bytes,
        total_bytes=check.total_bytes,
        reason=check.reason,
        suggestions=check.suggestions,
        message=storage_check_message(check.reason, path),
    )


# --- Reading the two things every handler below needs -----------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _manager(request: Request) -> DeployManager:
    manager: DeployManager = request.app.state.deploy
    return manager


# --- Routes -------------------------------------------------------------------


@router.get("/catalog")
async def get_catalog() -> CatalogResponse:
    return CatalogResponse(
        apps=[
            CatalogAppOut(id=app.id, name=app.name, description=app.description, port=app.port)
            for app in CATALOG
        ]
    )


@router.post("/storage/check")
async def post_storage_check(body: StorageCheckRequest, request: Request) -> StorageCheckOut:
    check = check_storage_root(_settings(request), body.path)
    return _storage_check_out(check, body.path)


@router.post("/install")
async def post_install(body: InstallRequest, request: Request) -> InstallResponse:
    result = install_apps(_settings(request), body.path, list(body.app_ids))
    if not result.ok:
        status_code = 400 if result.kind == "invalid_input" else 409
        raise HTTPException(status_code=status_code, detail=result.message)
    return InstallResponse(saved=True)


@router.get("/deploy")
async def get_deploy(request: Request) -> DeploySnapshotOut:
    return _snapshot_out(_manager(request).snapshot())


@router.post("/deploy")
async def post_deploy(request: Request, response: Response) -> DeploySnapshotOut:
    settings = _settings(request)
    manager = _manager(request)

    if load_state(settings.config_dir) is None:
        raise HTTPException(status_code=409, detail=REFUSAL_NOTHING_CHOSEN)

    # Decided before calling start(): start() itself returns immediately,
    # before its background task has taken a single step, so its own return
    # value can't yet distinguish "just launched" from "was already going".
    already_running = manager.snapshot().phase in ("running", "wiring")
    snapshot = manager.start()
    response.status_code = 200 if already_running else 202
    return _snapshot_out(snapshot)


async def _event_stream(
    manager: DeployManager, *, heartbeat_seconds: float = _SSE_HEARTBEAT_SECONDS
) -> AsyncGenerator[bytes, None]:
    """The current snapshot immediately, then one `data:` event per change.

    Wrapping `manager.subscribe()` in its own `try`/`finally` (rather than
    trusting the caller to close it) is what guarantees a client that stops
    reading - a closed tab, a dropped connection - always reaches
    `subscribe()`'s own cleanup and is never left registered as a
    subscriber the run keeps trying to feed.
    """
    # `DeployManager.subscribe()` is declared as `AsyncIterator[DeploySnapshot]`
    # (its own contract's chosen shape), but the object it hands back is
    # always the async generator its body defines - which is what actually
    # has `aclose()`. This cast states that honestly instead of hiding the
    # call behind `Any`.
    subscription = cast(AsyncGenerator[DeploySnapshot, None], manager.subscribe())
    try:
        while True:
            try:
                snapshot = await asyncio.wait_for(
                    subscription.__anext__(), timeout=heartbeat_seconds
                )
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            except StopAsyncIteration:
                return
            payload = _snapshot_out(snapshot).model_dump()
            # json.dumps with no indent is a single line - a data: frame's
            # payload may never contain a raw newline.
            yield f"data: {json.dumps(payload)}\n\n".encode()
    finally:
        await subscription.aclose()


@router.get("/deploy/events")
async def get_deploy_events(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(_manager(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/deploy/diagnostics", response_class=PlainTextResponse)
async def get_deploy_diagnostics(request: Request) -> Response:
    diagnostics_path = _settings(request).config_dir / _DIAGNOSTICS_FILE_NAME
    try:
        content = diagnostics_path.read_text()
    except OSError:
        return Response(status_code=204)
    return PlainTextResponse(content)
