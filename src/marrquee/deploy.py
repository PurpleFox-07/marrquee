"""The deploy engine: the part that does the real work.

One `DeployManager` per process takes the owner's saved install choices and
turns them into the killer moment - folders built, a readable compose file
written, apps started one at a time, each moving `waiting -> starting ->
done` with a plain-language line at every step. The run happens on a
server-side `asyncio.Task`, independent of any browser: `snapshot()` is
synchronous and always the current truth, and every change is persisted to
disk so a Marrquee restart mid-deploy resumes instead of lying about what
happened.

Every non-deterministic dependency - the Docker engine, the HTTP readiness
probe, the clock, the sleep function, the wiring runner - is an injected
parameter, which is what lets the whole engine be driven end to end in
tests without a real Docker daemon and without a single real second of
waiting.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Protocol, cast

import httpx

from marrquee.catalog import CatalogApp, apps_in_order, get_app, unavailable_reason
from marrquee.compose import build_stack_plan, write_compose
from marrquee.config import Settings
from marrquee.docker_client import ComposeResult, DockerEngine
from marrquee.install import with_app_added, with_app_removed
from marrquee.state import InstallState, load_state, save_state, write_json_atomic
from marrquee.storage import (
    FreshnessCheck,
    StorageCheck,
    build_folders,
    check_fresh_start,
    check_storage_root,
    read_marker,
    write_marker,
)
from marrquee.wiring import NoWiringYet, WiringRunner, WiringStep, WiringStepState
from marrquee.words import (
    FAILURE_DOCKER_UNREACHABLE,
    PHASE_HEADLINE_FINALE,
    PHASE_HEADLINE_READY,
    PHASE_HEADLINE_RUNNING,
    PHASE_HEADLINE_WIRING,
    STATUS_CHIP_DONE,
    STATUS_CHIP_ERROR,
    STATUS_CHIP_STARTING,
    STATUS_CHIP_WAITING,
    app_headline_done,
    app_headline_starting,
    app_line_done,
    app_line_downloading,
    app_line_error,
    app_line_starting,
    app_line_warming_up,
    app_note_slow_start,
    failure_compose_failed,
    failure_download_failed,
    failure_never_became_ready,
    failure_port_in_use,
    hub_cancel_failed,
    hub_line_connecting,
    refusal_name_clash,
    refusal_not_a_folder,
    refusal_not_shared,
    refusal_not_writable,
    refusal_path_missing,
    refusal_populated_target,
    refusal_system_path,
    wiring_finale_note,
)

logger = logging.getLogger(__name__)

AppState = Literal["waiting", "starting", "done", "error"]
DeployPhase = Literal["ready", "running", "wiring", "finale", "error"]
FailureCode = Literal[
    "docker_unreachable",
    "storage_refused",
    "name_clash",
    "image_download_failed",
    "compose_failed",
    "never_became_ready",
    "port_in_use",
]

# An add's own tiny state machine - never "done": once wiring finishes, the
# app moves into `DeploySnapshot.apps` and `adding` goes back to `None`,
# exactly the way a full deploy never keeps a `done` app around as
# `adding` either.
AddState = Literal["starting", "wiring", "error"]
# "add" is a brand new app; "reconnect" re-runs only the wiring steps for an
# app that is already `done` in `apps` - it never touches Docker at all.
AddPurpose = Literal["add", "reconnect"]
AddStart = Literal[
    "started", "busy", "not_ready", "unknown_app", "already_installed", "unavailable"
]

_DEPLOY_FILE_NAME = "deploy.json"
_DIAGNOSTICS_FILE_NAME = "last-failure.txt"
_REDACTED_PLACEHOLDER = "<redacted-api-key>"


@dataclass(frozen=True)
class AppProgress:
    """One app's place in the deploy, in words a beginner understands.

    Carries the app's published port only - the snapshot is built
    server-side with no browser request attached, so no full address in it
    can ever be right for a second device on the network. A page builds the
    clickable link itself, per request, from the host it was reached on.
    """

    app_id: str
    name: str
    state: AppState
    chip: str
    line: str
    note: str | None
    port: int


@dataclass(frozen=True)
class Failure:
    """A real failure, split into what the owner sees and what a developer needs.

    `technical` is written to the log and the diagnostics file only, and
    never becomes part of a rendered field - the same split the rest of this
    codebase uses for `DockerStatus.detail` and `StorageCheck.detail`.
    """

    code: FailureCode
    headline: str
    what_to_do: str
    technical: str


@dataclass(frozen=True)
class AppAdd:
    """One app being added (or reconnected) to an already-finale deploy.

    Lives entirely inside `DeploySnapshot.adding`, never in `apps` - the Hub
    builds its posters from `apps`, so a half-added app stays invisible to
    every reader of that list until it is genuinely `done`. `compose_ran`
    is what lets Cancel tell "Docker was actually asked to create this
    container" apart from "we refused before ever touching Docker" (a name
    clash) - removing a container in the second case would delete someone
    else's.
    """

    app_id: str
    purpose: AddPurpose
    state: AddState
    line: str
    note: str | None
    failure: Failure | None
    wiring: tuple[WiringStep, ...]
    compose_ran: bool
    started_at: str


@dataclass(frozen=True)
class WiringGap:
    """One already-installed app whose wiring didn't fully finish.

    `app_id` is the app whose add or reconnect ran the steps that produced
    these lines - the tile that gets the amber note and the "Connect
    again" button.
    """

    app_id: str
    failed_lines: tuple[str, ...]


@dataclass(frozen=True)
class DeploySnapshot:
    """The whole truth about the current (or most recent) deploy, right now."""

    run_id: str
    phase: DeployPhase
    apps: tuple[AppProgress, ...]
    headline: str
    detail: str | None
    failure: Failure | None
    started_at: str | None
    finished_at: str | None
    wiring: tuple[WiringStep, ...] = ()
    adding: AppAdd | None = None
    wiring_gaps: tuple[WiringGap, ...] = ()


class ReadinessProbe(Protocol):
    """Answers "has this app finished booting and accepted our key?"."""

    async def check(self, host: str, port: int, api_base: str, api_key: str) -> bool: ...


class HttpReadinessProbe:
    """Probes `GET http://<host>:<port>/<api_base>/system/status` with our key.

    `done` means this returns 200 - not merely that the container is
    running. A connection-level failure (refused, timed out) is the normal
    state for the first several seconds of a fresh container, so it is
    treated as "not ready yet" rather than as an error; only a real answer
    decides readiness.
    """

    def __init__(
        self, *, timeout: float = 3.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def check(self, host: str, port: int, api_base: str, api_key: str) -> bool:
        url = f"http://{host}:{port}/{api_base}/system/status"
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.get(url, headers={"X-Api-Key": api_key})
        except httpx.HTTPError:
            return False
        return response.status_code == 200


class FakeReadinessProbe:
    """A scriptable ReadinessProbe for tests - no network involved.

    `responses` holds a queue of answers per `(host, port)`, drained in
    order; once a queue is empty (or was never given), `default` answers
    every further call - the shape that lets a test say "not ready a few
    times, then ready" without any real waiting.
    """

    def __init__(
        self,
        *,
        responses: Mapping[tuple[str, int], Iterable[bool]] | None = None,
        default: bool = True,
    ) -> None:
        self._queues: dict[tuple[str, int], list[bool]] = {
            key: list(values) for key, values in (responses or {}).items()
        }
        self._default = default
        self.calls: list[tuple[str, int, str, str]] = []

    async def check(self, host: str, port: int, api_base: str, api_key: str) -> bool:
        self.calls.append((host, port, api_base, api_key))
        queue = self._queues.get((host, port))
        if queue:
            return queue.pop(0)
        return self._default


class DeployManager:
    """Runs, watches and remembers exactly one deploy per Marrquee process.

    The run itself lives entirely inside `_run` and its private helpers;
    everything else here is bookkeeping - starting the background task,
    persisting every change, and serving the truth to however many
    subscribers are watching, without ever letting a slow or vanished one
    stall the run.
    """

    # The mockup's own hiccup beat sets the expectation that slowness is
    # normal, and a first arr start on a NAS with a cold database routinely
    # takes longer than a minute - 300s means a timeout is a genuine problem.
    POLL_INTERVAL_SECONDS: ClassVar[float] = 2.0
    REASSURANCE_AFTER_SECONDS: ClassVar[float] = 45.0
    NEVER_READY_AFTER_SECONDS: ClassVar[float] = 300.0

    def __init__(
        self,
        settings: Settings,
        engine: DockerEngine,
        *,
        probe: ReadinessProbe = HttpReadinessProbe(),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wiring: WiringRunner = NoWiringYet(),
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._probe = probe
        self._clock = clock
        self._sleep = sleep
        self._wiring = wiring
        self._task: asyncio.Task[None] | None = None
        self._subscribers: list[asyncio.Queue[DeploySnapshot]] = []
        self._deploy_file = settings.config_dir / _DEPLOY_FILE_NAME
        self._diagnostics_file = settings.config_dir / _DIAGNOSTICS_FILE_NAME
        # Read back whatever the last process persisted - this is what lets
        # a NAS restart mid-deploy still answer "finale" (or "running", so
        # `resume_if_interrupted` knows to re-enter it) instead of lying
        # that nothing ever happened.
        self._snapshot = self._load_persisted_snapshot() or _resting_snapshot()

    # --- The truth, right now -------------------------------------------

    def snapshot(self) -> DeploySnapshot:
        """The current truth. Synchronous, and always accurate.

        While nothing has ever run, this reflects the owner's saved choices
        (every chosen app, `waiting`) rather than an empty screen - reading
        it fresh on every call is what keeps it accurate even if the owner
        changes their install choices before ever pressing deploy.
        """
        if self._snapshot.phase == "ready":
            return self._ready_snapshot()
        return self._snapshot

    def _ready_snapshot(self) -> DeploySnapshot:
        install = load_state(self._settings.config_dir)
        apps = (
            ()
            if install is None
            else tuple(_waiting_progress(app) for app in apps_in_order(install.app_ids))
        )
        return replace(self._snapshot, apps=apps)

    # --- Starting and resuming -------------------------------------------

    def start(self) -> DeploySnapshot:
        """Start a deploy if none is running; otherwise, hand back the one already going.

        A NEW run clears the diagnostics file first, so "Last problem" on
        the Diagnostics page can only ever show the most recent run's own
        problem, never one an earlier, unrelated deploy left behind.
        `resume_if_interrupted` never does this - a restart mid-deploy is
        the same run continuing, and its evidence should survive it.
        """
        if self._is_running():
            return self.snapshot()
        install = load_state(self._settings.config_dir)
        if install is None:
            return self.snapshot()
        self._clear_diagnostics()
        self._task = asyncio.create_task(self._run(install))
        return self.snapshot()

    def _clear_diagnostics(self) -> None:
        try:
            self._diagnostics_file.unlink(missing_ok=True)
        except OSError:
            pass  # a locked or otherwise unremovable file must never stop a deploy

    async def resume_if_interrupted(self) -> DeploySnapshot:
        """Re-enter a deploy that was still going when this process last stopped.

        Every step the run takes is idempotent (folders, marker, compose
        file, `compose up --no-recreate`, network connect - joined again per
        app, which also self-heals a Marrquee container recreated by this
        very restart, no longer being on the network at all), so re-running
        the whole sequence is a repeat, not a rollback - reporting `error`
        here instead would tell the owner their deploy failed while their
        containers are visibly fine.
        """
        if self._is_running():
            return self.snapshot()
        if self._snapshot.phase == "finale":
            return self._resume_add_if_interrupted()
        if self._snapshot.phase not in ("running", "wiring"):
            return self.snapshot()
        install = load_state(self._settings.config_dir)
        if install is None:
            return self.snapshot()
        self._task = asyncio.create_task(self._run(install))
        return self.snapshot()

    def _resume_add_if_interrupted(self) -> DeploySnapshot:
        """A `finale` deploy whose `adding` was still `starting`/`wiring` when
        this process last stopped - the add-path equivalent of the branch
        just above.

        An `adding` already `error` needs an owner's "Try again", not an
        automatic resume - the same reason a full deploy's own `error`
        phase is never auto-resumed either.
        """
        adding = self._snapshot.adding
        if adding is None or adding.state not in ("starting", "wiring"):
            return self.snapshot()
        install = load_state(self._settings.config_dir)
        if install is None:
            return self.snapshot()
        try:
            app = get_app(adding.app_id)
        except KeyError:
            return self.snapshot()
        if adding.purpose == "add":
            self._task = asyncio.create_task(self._run_add(app, install))
        else:
            self._task = asyncio.create_task(self._run_reconnect(app, install))
        return self.snapshot()

    def _is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_busy(self) -> bool:
        """Whether a run (a full deploy, an add, a retry or a reconnect) is
        actively going right now - the Hub's own "one thing at a time" flag,
        as opposed to `adding is not None`, which also stays true while a
        failed add just sits there waiting for "Try again" or "Cancel".
        """
        return self._is_running()

    def return_to_ready(self) -> DeploySnapshot:
        """Bring back the Deploy button after new choices are saved.

        Called once, from the wizard's own success path, right after new
        choices land on disk - the persisted "finale" or "error" from an
        earlier deploy would otherwise send the owner straight back to the
        old finale with no way to press Deploy again. A run still in
        flight is left alone: saving new choices in a second tab must
        never interrupt one already going.
        """
        if self._is_running():
            return self.snapshot()
        if self._snapshot.phase in ("finale", "error"):
            self._emit(_resting_snapshot())
        return self.snapshot()

    # --- Adding, retrying, cancelling and reconnecting one app -----------

    def add_app(self, app_id: str) -> AddStart:
        """Start adding one app to an already-finale deploy - never a second
        full deploy.

        Synchronous, and refuses before ever touching Docker: only one add
        (or one waiting failed add) is allowed at a time, so a second
        `add_app` while the first is still going, or still sitting there
        failed, is refused as `"busy"` rather than silently queued or
        silently ignored. Nothing awaits between the refusal checks and
        `create_task` - a second call arriving before the first `await`
        point inside the new task could otherwise slip past `adding is not
        None` and start a second add for real.
        """
        install = load_state(self._settings.config_dir)
        snapshot = self.snapshot()
        if install is None or snapshot.phase != "finale":
            return "not_ready"
        if self._is_running() or snapshot.adding is not None:
            return "busy"
        try:
            app = get_app(app_id)
        except KeyError:
            return "unknown_app"
        if any(progress.app_id == app_id for progress in snapshot.apps):
            return "already_installed"
        present_ids = tuple(progress.app_id for progress in snapshot.apps)
        if unavailable_reason(app, present_ids) is not None:
            return "unavailable"

        started_at = _now_iso()
        adding = AppAdd(
            app_id=app_id,
            purpose="add",
            state="starting",
            line=app_line_starting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=False,
            started_at=started_at,
        )
        self._emit(replace(snapshot, adding=adding))
        self._task = asyncio.create_task(self._run_add(app, install))
        return "started"

    def retry_add(self) -> AddStart:
        """Re-run a failed add from scratch - the add-path "press Deploy again"."""
        if self._is_running():
            return "busy"
        current = self._snapshot.adding
        if current is None or current.state != "error" or current.purpose != "add":
            return "busy"
        install = load_state(self._settings.config_dir)
        if install is None:
            return "not_ready"
        app = get_app(current.app_id)

        started_at = _now_iso()
        adding = AppAdd(
            app_id=app.id,
            purpose="add",
            state="starting",
            line=app_line_starting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=current.compose_ran,
            started_at=started_at,
        )
        self._emit(self._replace_finale(adding=adding))
        self._task = asyncio.create_task(self._run_add(app, install))
        return "started"

    async def cancel_add(self) -> bool:
        """Give up on a failed add: remove the container it created (if any),
        drop the app from install.json, and put it back in the "+" list.

        Only ever removes a container `compose_ran` says WE asked Docker to
        create - a name-clash refusal never got that far, so Cancel after
        one never touches a container it didn't create. Never deletes a
        folder: the app's API key and every folder Marrquee built for it
        are left exactly where they are, ready for a later re-add.

        Refuses for a failed `reconnect` too, even though its `AppAdd`
        always carries `compose_ran=True` (a reconnect never calls Docker
        at all, so that flag alone can't tell "we created this" apart from
        "this was already installed and running"). A failed reconnect is
        already-installed, working app - Cancel must never remove it;
        `reconnect(app_id)` is the only recovery this offers.
        """
        current = self._snapshot.adding
        if current is None or current.state != "error" or current.purpose != "add":
            return False

        app = get_app(current.app_id)
        if current.compose_ran:
            result = await self._engine.remove_container(app.id)
            if not result.ok:
                failed = replace(current, line=hub_cancel_failed(app.name))
                self._emit(self._replace_finale(adding=failed))
                return False

        install = load_state(self._settings.config_dir)
        if install is not None:
            shrunk = with_app_removed(install, app.id)
            save_state(self._settings.config_dir, shrunk)
            if shrunk.storage_root is not None:
                root = PurePosixPath(shrunk.storage_root)
                write_marker(self._settings, root, shrunk.app_ids, shrunk.puid, shrunk.pgid)
                write_compose(self._settings, build_stack_plan(shrunk))

        self._emit(self._replace_finale(adding=None))
        return True

    def reconnect(self, app_id: str) -> AddStart:
        """Re-run only one already-installed app's wiring steps.

        The same "press it again" idea a failed full deploy already offers,
        narrowed to one app: nothing about the app itself is touched, only
        the connections its wiring steps make - so an app that is already
        fully wired is never at risk of losing anything by being
        reconnected again.
        """
        install = load_state(self._settings.config_dir)
        snapshot = self.snapshot()
        if install is None or snapshot.phase != "finale":
            return "not_ready"
        if self._is_running() or snapshot.adding is not None:
            return "busy"
        if not any(progress.app_id == app_id for progress in snapshot.apps):
            return "unknown_app"
        app = get_app(app_id)

        started_at = _now_iso()
        adding = AppAdd(
            app_id=app_id,
            purpose="reconnect",
            state="wiring",
            line=hub_line_connecting(app.name),
            note=None,
            failure=None,
            wiring=(),
            compose_ran=True,
            started_at=started_at,
        )
        self._emit(replace(snapshot, adding=adding))
        self._task = asyncio.create_task(self._run_reconnect(app, install))
        return "started"

    def _current_adding(self) -> AppAdd | None:
        return self._snapshot.adding

    def _replace_finale(
        self,
        *,
        adding: AppAdd | None,
        apps: tuple[AppProgress, ...] | None = None,
        wiring_gaps: tuple[WiringGap, ...] | None = None,
    ) -> DeploySnapshot:
        """The current snapshot with only `adding` (and optionally `apps` /
        `wiring_gaps`) swapped - `phase`, `headline`, `detail`, `failure`,
        `wiring`, `started_at` and `finished_at` all stay exactly what the
        finale deploy already set them to. This is what keeps every
        snapshot emitted during an add still reading `phase == "finale"` -
        the Hub never bounces to /deploy while one is running.
        """
        current = self._snapshot
        return replace(
            current,
            adding=adding,
            apps=current.apps if apps is None else apps,
            wiring_gaps=current.wiring_gaps if wiring_gaps is None else wiring_gaps,
        )

    def _gaps_with(self, app_id: str, failed_lines: tuple[str, ...]) -> tuple[WiringGap, ...]:
        """`wiring_gaps` with `app_id`'s own entry replaced, added, or
        removed - a clean reconnect drops a gap it just closed instead of
        leaving a stale one behind.
        """
        remaining = tuple(gap for gap in self._snapshot.wiring_gaps if gap.app_id != app_id)
        if not failed_lines:
            return remaining
        return (*remaining, WiringGap(app_id=app_id, failed_lines=failed_lines))

    # --- Subscribing -------------------------------------------------------

    async def subscribe(self) -> AsyncIterator[DeploySnapshot]:
        """Yield the current snapshot immediately, then one per change.

        Delivery is best-effort: a subscriber that stops reading fills its
        own one-slot queue and is dropped rather than allowed to make
        `_emit` block the run - the deploy has to keep going independent of
        any browser connection, closed tab or otherwise.
        """
        queue: asyncio.Queue[DeploySnapshot] = asyncio.Queue(maxsize=1)
        queue.put_nowait(self.snapshot())
        self._subscribers.append(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    # --- Persistence and fan-out --------------------------------------------

    def _emit(self, snapshot: DeploySnapshot) -> None:
        self._snapshot = snapshot
        write_json_atomic(self._deploy_file, dataclasses.asdict(snapshot))
        still_listening = []
        for queue in self._subscribers:
            try:
                queue.put_nowait(snapshot)
                still_listening.append(queue)
            except asyncio.QueueFull:
                pass  # a slow subscriber is dropped, never allowed to block the run
        self._subscribers = still_listening

    async def _publish(self, snapshot: DeploySnapshot) -> None:
        """Emit, then yield once - so a subscriber gets a real chance to see it
        before the run races on to the next state.
        """
        self._emit(snapshot)
        await asyncio.sleep(0)

    def _load_persisted_snapshot(self) -> DeploySnapshot | None:
        try:
            raw = self._deploy_file.read_text()
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        try:
            return _snapshot_from_payload(payload)
        except (KeyError, TypeError, ValueError):
            return None

    # --- The run itself ------------------------------------------------------

    async def _run(self, install: InstallState) -> None:
        run_id = uuid.uuid4().hex
        started_at = _now_iso()
        catalog_apps = apps_in_order(install.app_ids)
        progresses = [_waiting_progress(app) for app in catalog_apps]

        try:
            await self._run_steps(install, run_id, started_at, catalog_apps, progresses)
        except Exception as error:  # the background task must never die silently
            logger.exception("deploy engine crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed("Marrquee"))
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
            )

    async def _run_steps(
        self,
        install: InstallState,
        run_id: str,
        started_at: str,
        catalog_apps: tuple[CatalogApp, ...],
        progresses: list[AppProgress],
    ) -> None:
        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="running",
                apps=tuple(progresses),
                headline=PHASE_HEADLINE_RUNNING,
                detail=None,
                failure=None,
                started_at=started_at,
                finished_at=None,
                wiring=(),
            )
        )

        docker_status = await self._engine.status()
        if not docker_status.connected:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _docker_unreachable_failure(docker_status.detail or "Docker did not answer"),
            )
            return

        if install.storage_root is None:
            headline, what_to_do = _split_failure_text(
                refusal_path_missing("(no storage folder chosen)")
            )
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                Failure(
                    code="storage_refused",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical="no storage_root was ever saved",
                ),
            )
            return

        storage_check = check_storage_root(self._settings, install.storage_root)
        if not storage_check.ok:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _storage_failure(storage_check, install.storage_root),
            )
            return

        root = PurePosixPath(install.storage_root)
        freshness = check_fresh_start(self._settings, root, install.app_ids)
        if not freshness.ok:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _freshness_failure(freshness, install.storage_root),
            )
            return

        clash = await self._find_name_clash(install, root, catalog_apps)
        if clash is not None:
            await self._fail(run_id, started_at, progresses, install, clash)
            return

        build_folders(self._settings, root, install.app_ids, install.puid, install.pgid)
        write_marker(self._settings, root, install.app_ids, install.puid, install.pgid)
        plan = build_stack_plan(install)
        compose_path = write_compose(self._settings, plan)

        # Marrquee has no label on the stack's own compose project, so this
        # fallback chain is the only way to identify its own container -
        # checked once, up front, since a missing id means nothing could
        # ever join the network later either, regardless of anything else.
        self_id = await self._engine.self_container_id()
        if self_id is None:
            await self._fail(
                run_id,
                started_at,
                progresses,
                install,
                _docker_unreachable_failure("could not identify Marrquee's own container"),
            )
            return

        for index, app in enumerate(catalog_apps):
            report = self._full_deploy_reporter(progresses, index, app, run_id, started_at)
            failure = await self._bring_up_app(
                app, install, compose_path, plan.network, self_id, report
            )
            if failure is not None:
                await self._fail(run_id, started_at, progresses, install, failure)
                return

        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="wiring",
                apps=tuple(progresses),
                headline=PHASE_HEADLINE_WIRING,
                detail=None,
                failure=None,
                started_at=started_at,
                finished_at=None,
                wiring=(),
            )
        )

        # Keyed by `index` rather than appended, so a step that re-emits
        # `running` (a reassurance note) or moves from `running` to a
        # terminal state replaces its own row instead of leaving a stale one
        # behind - the screen (and the finale) only ever sees the latest
        # frame for each step.
        wiring_rows: dict[int, WiringStep] = {}

        def collect_wiring_step(step: WiringStep) -> None:
            if step.technical:
                self._append_diagnostics(_redact_secrets(step.technical, install.api_keys))
            wiring_rows[step.index] = replace(step, technical=None)
            self._emit(
                DeploySnapshot(
                    run_id=run_id,
                    phase="wiring",
                    apps=tuple(progresses),
                    headline=PHASE_HEADLINE_WIRING,
                    detail=None,
                    failure=None,
                    started_at=started_at,
                    finished_at=None,
                    wiring=tuple(wiring_rows[index] for index in sorted(wiring_rows)),
                )
            )

        try:
            await self._wiring.run(install, collect_wiring_step)
        except Exception:
            # A wiring problem is not a deploy failure - the apps are
            # already up and done, so this never turns a successful deploy
            # into one that looks failed.
            logger.exception("wiring runner raised; continuing to finale anyway")

        wiring_steps = tuple(wiring_rows[index] for index in sorted(wiring_rows))
        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="finale",
                apps=tuple(progresses),
                headline=PHASE_HEADLINE_FINALE,
                detail=_wiring_finale_detail(wiring_steps),
                failure=None,
                started_at=started_at,
                finished_at=_now_iso(),
                wiring=wiring_steps,
            )
        )

    async def _find_name_clash(
        self, install: InstallState, root: PurePosixPath, catalog_apps: tuple[CatalogApp, ...]
    ) -> Failure | None:
        marker = read_marker(self._settings, root)
        owned_ids = frozenset(marker.app_ids) if marker is not None else frozenset()
        for app in catalog_apps:
            if app.id in owned_ids:
                continue  # a container we created on an earlier attempt at this same root
            existing = await self._engine.inspect(app.id)
            if existing.exists:
                headline, what_to_do = _split_failure_text(refusal_name_clash(app.name))
                return Failure(
                    code="name_clash",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"a container named {app.id!r} already exists and isn't ours",
                )
        return None

    def _full_deploy_reporter(
        self,
        progresses: list[AppProgress],
        index: int,
        app: CatalogApp,
        run_id: str,
        started_at: str,
    ) -> Callable[[AppState, str, str | None], Awaitable[None]]:
        """The full deploy's own `report` callback for `_bring_up_app`.

        Reproduces exactly the snapshot the inline code used to publish at
        each of its three call sites - the existing `test_deploy` suite is
        what proves this refactor changed no observable behaviour.
        """
        chip_for_state = {"starting": STATUS_CHIP_STARTING, "done": STATUS_CHIP_DONE}
        headline_for_state = {
            "starting": app_headline_starting(app.name),
            "done": app_headline_done(app.name),
        }

        async def report(state: AppState, line: str, note: str | None) -> None:
            progresses[index] = replace(
                progresses[index], state=state, chip=chip_for_state[state], line=line, note=note
            )
            await self._publish(
                _running_snapshot(run_id, started_at, progresses, headline_for_state[state])
            )

        return report

    async def _bring_up_app(
        self,
        app: CatalogApp,
        install: InstallState,
        compose_path: Path,
        network: str,
        self_id: str,
        report: Callable[[AppState, str, str | None], Awaitable[None]],
    ) -> Failure | None:
        api_key = install.api_keys.get(app.id)
        if api_key is None:
            headline, what_to_do = _split_failure_text(
                refusal_path_missing(install.storage_root or "")
            )
            return Failure(
                code="storage_refused",
                headline=headline,
                what_to_do=what_to_do,
                technical=f"no API key was recorded for {app.id!r}",
            )

        downloading = not await self._engine.image_present(app.image)
        line = app_line_downloading(app.name) if downloading else app_line_starting(app.name)
        note: str | None = None
        await report("starting", line, note)

        result = await self._engine.compose_up(self._settings.stack_project, compose_path, app.id)
        if not result.ok:
            return _compose_failure(app, downloading, result)

        # Compose creates the stack's network as a side effect of its own
        # first successful `up` - on a fresh host, nothing exists before
        # that, so this can never run any earlier. Idempotent (an
        # already-connected container counts as success) and repeated for
        # every app rather than once, so a Marrquee container recreated
        # mid-deploy (a restart) rejoins the network here instead of
        # silently staying off it.
        connect_result = await self._engine.connect_network(network, self_id)
        if not connect_result.ok:
            return _docker_unreachable_failure(
                connect_result.detail
                or f"could not join the {network!r} network (self_id={self_id!r})"
            )

        start = self._clock()
        reassured = False
        while True:
            elapsed = self._clock() - start
            if elapsed >= self.NEVER_READY_AFTER_SECONDS:
                logs = await self._engine.logs(app.id, tail=50)
                headline, what_to_do = _split_failure_text(failure_never_became_ready(app.name))
                return Failure(
                    code="never_became_ready",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=logs,
                )

            container = await self._engine.inspect(app.id)
            if container.state == "running":
                if await self._probe.check(app.id, app.port, app.api_base, api_key):
                    await report("done", app_line_done(app.name), None)
                    return None
                candidate_line = app_line_warming_up(app.name)
            else:
                candidate_line = line

            candidate_note = note
            if not reassured and elapsed >= self.REASSURANCE_AFTER_SECONDS:
                reassured = True
                candidate_note = app_note_slow_start(app.name)

            if candidate_line != line or candidate_note != note:
                line, note = candidate_line, candidate_note
                await report("starting", line, note)

            await self._sleep(self.POLL_INTERVAL_SECONDS)

    # --- Running an add or a retry -----------------------------------------

    def _diagnostics_recorder(self) -> Callable[[str], None]:
        """A `record(text)` closure for one add/reconnect run: the FIRST
        call replaces the diagnostics file, every later call appends -
        "Last problem" is replaced only by a newer problem, and an add with
        nothing to say about it never touches an older run's evidence.
        """
        wrote = False

        def record(text: str) -> None:
            nonlocal wrote
            if not wrote:
                self._diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
                self._diagnostics_file.write_text(text if text.endswith("\n") else f"{text}\n")
                wrote = True
            else:
                self._append_diagnostics(text)

        return record

    async def _run_add(self, app: CatalogApp, install: InstallState) -> None:
        """Run a whole add (or a retry of one) to completion.

        `install` is the ORIGINAL, on-disk state - not yet grown - the same
        object whether this is a fresh `add_app`, a `retry_add`, or a
        resumed one; `_run_add_steps` is the one place that decides whether
        `app` already belongs to it.
        """
        record_diagnostics = self._diagnostics_recorder()
        try:
            await self._run_add_steps(app, install, record_diagnostics=record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("add-app run crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            await self._fail_add(
                app,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
                record_diagnostics,
            )

    async def _run_add_steps(
        self, app: CatalogApp, install: InstallState, *, record_diagnostics: Callable[[str], None]
    ) -> None:
        # Idempotent: an already-grown state (a resumed add) just re-keeps
        # its existing ids and key - this is what lets every caller
        # (add_app, retry_add, a resume) pass the ORIGINAL, on-disk install
        # and let this one call decide whether it's already grown.
        grown = with_app_added(install, app.id)
        save_state(self._settings.config_dir, grown)

        docker_status = await self._engine.status()
        if not docker_status.connected:
            await self._fail_add(
                app,
                grown,
                _docker_unreachable_failure(docker_status.detail or "Docker did not answer"),
                record_diagnostics,
            )
            return

        if grown.storage_root is None:
            headline, what_to_do = _split_failure_text(
                refusal_path_missing("(no storage folder chosen)")
            )
            await self._fail_add(
                app,
                grown,
                Failure(
                    code="storage_refused",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical="no storage_root was ever saved",
                ),
                record_diagnostics,
            )
            return

        storage_check = check_storage_root(self._settings, grown.storage_root)
        if not storage_check.ok:
            await self._fail_add(
                app,
                grown,
                _storage_failure(storage_check, grown.storage_root),
                record_diagnostics,
            )
            return

        root = PurePosixPath(grown.storage_root)
        freshness = check_fresh_start(self._settings, root, grown.app_ids)
        if not freshness.ok:
            await self._fail_add(
                app, grown, _freshness_failure(freshness, grown.storage_root), record_diagnostics
            )
            return

        clash = await self._find_name_clash(grown, root, (app,))
        if clash is not None:
            await self._fail_add(app, grown, clash, record_diagnostics)
            return

        build_folders(self._settings, root, grown.app_ids, grown.puid, grown.pgid)
        write_marker(self._settings, root, grown.app_ids, grown.puid, grown.pgid)
        plan = build_stack_plan(grown)
        compose_path = write_compose(self._settings, plan)

        self_id = await self._engine.self_container_id()
        if self_id is None:
            await self._fail_add(
                app,
                grown,
                _docker_unreachable_failure("could not identify Marrquee's own container"),
                record_diagnostics,
            )
            return

        current = self._current_adding()
        if current is not None:
            self._emit(self._replace_finale(adding=replace(current, compose_ran=True)))

        report = self._add_reporter()
        failure = await self._bring_up_app(app, grown, compose_path, plan.network, self_id, report)
        if failure is not None:
            await self._fail_add(app, grown, failure, record_diagnostics)
            return

        current = self._current_adding()
        if current is not None:
            self._emit(
                self._replace_finale(
                    adding=replace(current, state="wiring", line=hub_line_connecting(app.name))
                )
            )

        await self._run_wiring_for_add(app, grown, record_diagnostics)

    def _add_reporter(self) -> Callable[[AppState, str, str | None], Awaitable[None]]:
        """The add path's own `report` callback for `_bring_up_app`.

        Only `line`/`note` change here - `adding.state` stays `"starting"`
        throughout the whole call (the explicit transition to `"wiring"`
        happens once, right after `_bring_up_app` returns), so this never
        needs to know which of the two `AppState`s it was just told about.
        """

        async def report(state: AppState, line: str, note: str | None) -> None:
            current = self._current_adding()
            if current is None:
                return
            await self._publish(self._replace_finale(adding=replace(current, line=line, note=note)))

        return report

    async def _run_wiring_for_add(
        self, app: CatalogApp, install: InstallState, record_diagnostics: Callable[[str], None]
    ) -> None:
        """Run only the wiring steps about `app`, then fold it into `apps`.

        Shared by a fresh add and a resumed one - the wiring runner is
        idempotent (look-before-write), so re-running it for an app whose
        wiring already fully succeeded on an earlier, interrupted attempt
        is a repeat, not a risk.
        """
        wiring_rows: dict[int, WiringStep] = {}

        def collect_wiring_step(step: WiringStep) -> None:
            if step.technical:
                record_diagnostics(_redact_secrets(step.technical, install.api_keys))
            wiring_rows[step.index] = replace(step, technical=None)
            current = self._current_adding()
            if current is not None:
                ordered_steps = tuple(wiring_rows[index] for index in sorted(wiring_rows))
                self._emit(self._replace_finale(adding=replace(current, wiring=ordered_steps)))

        try:
            await self._wiring.run(install, collect_wiring_step, only_app=app.id)
        except Exception:
            # A wiring problem is not an add failure - the app is already
            # up and done, so this never turns a successful add into one
            # that looks failed.
            logger.exception("wiring runner raised during an add; continuing anyway")

        wiring_steps = tuple(wiring_rows[index] for index in sorted(wiring_rows))
        failed_lines = tuple(step.line for step in wiring_steps if step.state == "error")

        progresses_by_id = {progress.app_id: progress for progress in self._snapshot.apps}
        progresses_by_id[app.id] = AppProgress(
            app_id=app.id,
            name=app.name,
            state="done",
            chip=STATUS_CHIP_DONE,
            line=app_line_done(app.name),
            note=None,
            port=app.port,
        )
        ordered_ids = tuple(catalog_app.id for catalog_app in apps_in_order(progresses_by_id))
        new_apps = tuple(progresses_by_id[app_id] for app_id in ordered_ids)

        await self._publish(
            self._replace_finale(
                adding=None, apps=new_apps, wiring_gaps=self._gaps_with(app.id, failed_lines)
            )
        )

    async def _run_reconnect(self, app: CatalogApp, install: InstallState) -> None:
        record_diagnostics = self._diagnostics_recorder()
        try:
            await self._run_wiring_for_add(app, install, record_diagnostics)
        except Exception as error:  # the background task must never die silently
            logger.exception("reconnect crashed unexpectedly")
            headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
            await self._fail_add(
                app,
                install,
                Failure(
                    code="compose_failed",
                    headline=headline,
                    what_to_do=what_to_do,
                    technical=f"{type(error).__name__}: {error}",
                ),
                record_diagnostics,
            )

    async def _fail_add(
        self,
        app: CatalogApp,
        install: InstallState,
        failure: Failure,
        record_diagnostics: Callable[[str], None],
    ) -> None:
        redacted = replace(failure, technical=_redact_secrets(failure.technical, install.api_keys))
        record_diagnostics(f"{redacted.code}\n{redacted.technical}")
        logger.error("add failed (%s): %s", redacted.code, redacted.technical)
        current = self._current_adding()
        if current is None:
            return
        updated = replace(current, state="error", line=app_line_error(app.name), failure=redacted)
        await self._publish(self._replace_finale(adding=updated))

    async def _fail(
        self,
        run_id: str,
        started_at: str,
        progresses: list[AppProgress],
        install: InstallState,
        failure: Failure,
    ) -> None:
        redacted = replace(failure, technical=_redact_secrets(failure.technical, install.api_keys))
        self._diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
        self._diagnostics_file.write_text(f"{redacted.code}\n{redacted.technical}\n")
        logger.error("deploy failed (%s): %s", redacted.code, redacted.technical)
        await self._publish(
            DeploySnapshot(
                run_id=run_id,
                phase="error",
                apps=tuple(_stopped_progress(app) for app in progresses),
                headline=redacted.headline,
                detail=redacted.what_to_do,
                failure=redacted,
                started_at=started_at,
                finished_at=_now_iso(),
                wiring=(),
            )
        )

    def _append_diagnostics(self, text: str) -> None:
        self._diagnostics_file.parent.mkdir(parents=True, exist_ok=True)
        with self._diagnostics_file.open("a", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else f"{text}\n")


def read_last_failure(config_dir: Path) -> str | None:
    """The diagnostics file's own text, for the Diagnostics page's "Last
    problem" section - or `None` when there is nothing worth showing.

    `None` covers a missing file, one that can't be read, and one that's
    empty or whitespace-only - the page has exactly one wording for
    "nothing has gone wrong", and this is the single place that decides
    which of the two frames it's in. Reads with `errors="replace"` so a
    stray non-UTF-8 byte in captured output can never turn "show the
    problem" into a 500.
    """
    path = config_dir / _DIAGNOSTICS_FILE_NAME
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    return text if text.strip() else None


def _running_snapshot(
    run_id: str, started_at: str, progresses: list[AppProgress], headline: str
) -> DeploySnapshot:
    return DeploySnapshot(
        run_id=run_id,
        phase="running",
        apps=tuple(progresses),
        headline=headline,
        detail=None,
        failure=None,
        started_at=started_at,
        finished_at=None,
        wiring=(),
    )


def _resting_snapshot() -> DeploySnapshot:
    return DeploySnapshot(
        run_id="",
        phase="ready",
        apps=(),
        headline=PHASE_HEADLINE_READY,
        detail=None,
        failure=None,
        started_at=None,
        finished_at=None,
        wiring=(),
    )


def _waiting_progress(app: CatalogApp) -> AppProgress:
    return AppProgress(
        app_id=app.id,
        name=app.name,
        state="waiting",
        chip=STATUS_CHIP_WAITING,
        line=STATUS_CHIP_WAITING,
        note=None,
        port=app.port,
    )


def _stopped_progress(app: AppProgress) -> AppProgress:
    """The app that was `starting` when the deploy failed, told the truth.

    `waiting` and `done` entries are untouched - only the one app the run
    was actually waiting on when it gave up ever spent the failure sitting
    there mid-boot, gold and spinning, above a panel that just said it
    failed.
    """
    if app.state != "starting":
        return app
    return replace(
        app, state="error", chip=STATUS_CHIP_ERROR, line=app_line_error(app.name), note=None
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _split_failure_text(message: str) -> tuple[str, str]:
    """Split one of words.py's "what happened. what to do next." messages.

    Every failure message in words.py reads as exactly that shape - one
    sentence naming the problem, then one or more naming the fix. Splitting
    on the first sentence boundary keeps `Failure.headline` and
    `Failure.what_to_do` genuinely distinct without inventing wording beyond
    what is already approved.
    """
    headline, separator, what_to_do = message.partition(". ")
    if not separator:
        return message, message
    return f"{headline}.", what_to_do


def _docker_unreachable_failure(detail: str) -> Failure:
    headline, what_to_do = _split_failure_text(FAILURE_DOCKER_UNREACHABLE)
    return Failure(
        code="docker_unreachable", headline=headline, what_to_do=what_to_do, technical=detail
    )


_STORAGE_REFUSAL_WORDS: dict[str, Callable[[str], str]] = {
    "empty": refusal_path_missing,
    "not_absolute": refusal_path_missing,
    "system_path": refusal_system_path,
    "missing": refusal_path_missing,
    "not_a_folder": refusal_not_a_folder,
    "not_writable": refusal_not_writable,
    "not_shared": refusal_not_shared,
}


def _storage_failure(check: StorageCheck, path: str) -> Failure:
    wording = _STORAGE_REFUSAL_WORDS.get(check.reason or "missing", refusal_path_missing)
    headline, what_to_do = _split_failure_text(wording(path))
    return Failure(
        code="storage_refused",
        headline=headline,
        what_to_do=what_to_do,
        technical=f"reason={check.reason}, detail={check.detail}",
    )


def _freshness_failure(freshness: FreshnessCheck, path: str) -> Failure:
    headline, what_to_do = _split_failure_text(refusal_populated_target(path))
    return Failure(
        code="storage_refused",
        headline=headline,
        what_to_do=what_to_do,
        technical=f"occupied={freshness.occupied}",
    )


def _looks_like_port_conflict(output: str) -> bool:
    lowered = output.lower()
    return "already allocated" in lowered or "address already in use" in lowered


def _compose_failure(app: CatalogApp, downloading: bool, result: ComposeResult) -> Failure:
    if downloading:
        headline, what_to_do = _split_failure_text(failure_download_failed(app.name))
        return Failure(
            code="image_download_failed",
            headline=headline,
            what_to_do=what_to_do,
            technical=result.output,
        )
    if _looks_like_port_conflict(result.output):
        headline, what_to_do = _split_failure_text(failure_port_in_use(app.name, app.port))
        return Failure(
            code="port_in_use", headline=headline, what_to_do=what_to_do, technical=result.output
        )
    headline, what_to_do = _split_failure_text(failure_compose_failed(app.name))
    return Failure(
        code="compose_failed", headline=headline, what_to_do=what_to_do, technical=result.output
    )


def _redact_secrets(text: str, api_keys: Mapping[str, str]) -> str:
    """Replace every known API key with a placeholder before it reaches a log
    line, a diagnostics file or a snapshot's failure detail.

    Docker or an app's own error output could echo back an environment
    value we set ourselves - redacting by value here is what keeps "API keys
    never appear in a log or a diagnostics file" true even if a future log
    line surprises us.
    """
    redacted = text
    for key in api_keys.values():
        if key:
            redacted = redacted.replace(key, _REDACTED_PLACEHOLDER)
    return redacted


def _wiring_finale_detail(steps: tuple[WiringStep, ...]) -> str | None:
    """`None` on a clean run; otherwise every failed step's own line, in
    step order - the finale stays green either way (a wiring problem is
    never a deploy failure), but a failed connection still gets named.
    """
    failed_lines = tuple(step.line for step in steps if step.state == "error")
    return wiring_finale_note(failed_lines) if failed_lines else None


# --- deploy.json round-tripping: reload-on-construction, resume, persistence -


def _snapshot_from_payload(payload: object) -> DeploySnapshot:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")

    apps = tuple(_app_progress_from_payload(item) for item in _require_list(payload.get("apps")))
    wiring = tuple(
        _wiring_step_from_payload(item) for item in _require_list(payload.get("wiring", []))
    )
    failure_payload = payload.get("failure")
    failure = _failure_from_payload(failure_payload) if isinstance(failure_payload, dict) else None
    adding_payload = payload.get("adding")
    adding = _app_add_from_payload(adding_payload) if isinstance(adding_payload, dict) else None
    wiring_gaps = tuple(
        _wiring_gap_from_payload(item) for item in _require_list(payload.get("wiring_gaps", []))
    )

    return DeploySnapshot(
        run_id=_require_str(payload.get("run_id")),
        phase=cast(
            DeployPhase,
            _require_choice(
                payload.get("phase"), ("ready", "running", "wiring", "finale", "error")
            ),
        ),
        apps=apps,
        headline=_require_str(payload.get("headline")),
        detail=_require_optional_str(payload.get("detail")),
        failure=failure,
        started_at=_require_optional_str(payload.get("started_at")),
        finished_at=_require_optional_str(payload.get("finished_at")),
        wiring=wiring,
        adding=adding,
        wiring_gaps=wiring_gaps,
    )


def _app_progress_from_payload(payload: object) -> AppProgress:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")
    return AppProgress(
        app_id=_require_str(payload.get("app_id")),
        name=_require_str(payload.get("name")),
        state=cast(
            AppState,
            _require_choice(payload.get("state"), ("waiting", "starting", "done", "error")),
        ),
        chip=_require_str(payload.get("chip")),
        line=_require_str(payload.get("line")),
        note=_require_optional_str(payload.get("note")),
        port=_require_int(payload.get("port")),
    )


def _failure_from_payload(payload: dict[str, object]) -> Failure:
    return Failure(
        code=cast(
            FailureCode,
            _require_choice(
                payload.get("code"),
                (
                    "docker_unreachable",
                    "storage_refused",
                    "name_clash",
                    "image_download_failed",
                    "compose_failed",
                    "never_became_ready",
                    "port_in_use",
                ),
            ),
        ),
        headline=_require_str(payload.get("headline")),
        what_to_do=_require_str(payload.get("what_to_do")),
        technical=_require_str(payload.get("technical")),
    )


def _wiring_step_from_payload(payload: object) -> WiringStep:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")
    involved = payload.get("involved", [])
    if not isinstance(involved, list) or not all(isinstance(item, str) for item in involved):
        raise TypeError(f"expected a list of strings, got {involved!r}")
    return WiringStep(
        index=_require_int(payload.get("index")),
        total=_require_int(payload.get("total")),
        key=_require_str(payload.get("key")),
        line=_require_str(payload.get("line")),
        state=cast(
            WiringStepState,
            _require_choice(payload.get("state"), ("running", "done", "skipped", "error")),
        ),
        chip=_require_str(payload.get("chip")),
        note=_require_optional_str(payload.get("note")),
        technical=_require_optional_str(payload.get("technical")),
        involved=tuple(involved),
    )


def _app_add_from_payload(payload: dict[str, object]) -> AppAdd:
    wiring = tuple(
        _wiring_step_from_payload(item) for item in _require_list(payload.get("wiring", []))
    )
    failure_payload = payload.get("failure")
    failure = _failure_from_payload(failure_payload) if isinstance(failure_payload, dict) else None
    return AppAdd(
        app_id=_require_str(payload.get("app_id")),
        purpose=cast(AddPurpose, _require_choice(payload.get("purpose"), ("add", "reconnect"))),
        state=cast(
            AddState, _require_choice(payload.get("state"), ("starting", "wiring", "error"))
        ),
        line=_require_str(payload.get("line")),
        note=_require_optional_str(payload.get("note")),
        failure=failure,
        wiring=wiring,
        compose_ran=_require_bool(payload.get("compose_ran")),
        started_at=_require_str(payload.get("started_at")),
    )


def _wiring_gap_from_payload(payload: object) -> WiringGap:
    if not isinstance(payload, dict):
        raise TypeError(f"expected a JSON object, got {payload!r}")
    failed_lines = payload.get("failed_lines", [])
    if not isinstance(failed_lines, list) or not all(
        isinstance(item, str) for item in failed_lines
    ):
        raise TypeError(f"expected a list of strings, got {failed_lines!r}")
    return WiringGap(app_id=_require_str(payload.get("app_id")), failed_lines=tuple(failed_lines))


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected a bool, got {value!r}")
    return value


def _require_optional_str(value: object) -> str | None:
    return None if value is None else _require_str(value)


def _require_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected a whole number, got {value!r}")
    return value


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list, got {value!r}")
    return value


def _require_choice(value: object, allowed: tuple[str, ...]) -> str:
    """A string that is one of `allowed`, or a ValueError.

    Callers `cast()` the result to whichever `Literal` type `allowed`
    represents - the runtime membership check just above the cast is what
    makes that cast honest rather than a bare assertion.
    """
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"expected one of {allowed}, got {value!r}")
    return value
