"""The wiring engine: turns the owner's chosen apps into an honest list of
steps, waits patiently for anything slow to answer, runs each step once
with bounded retries, and reports every step in plain words.

`plan_wiring` is pure - it never touches the network - so the full shape of
a run (which steps, in what order, "Step N of M") is provable with nothing
but an `InstallState`. `WiringEngine.run` is the only part that talks to
the apps, and it never raises: a wiring problem is not a deploy failure, so
every problem becomes an emitted step with `state="error"` instead of an
exception reaching the deploy engine that owns this seam.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from marrquee.catalog import CATALOG, CatalogApp, apps_in_order
from marrquee.state import InstallState
from marrquee.storage import container_media_path, host_media_path
from marrquee.wiring import WiringStep, WiringStepState
from marrquee.wiring.arr_client import ArrClient, HttpArrClient
from marrquee.wiring.steps import (
    StepOutcome,
    app_base_url,
    ensure_application,
    ensure_root_folder,
)
from marrquee.words import (
    MEDIA_FOLDER_LABEL,
    WIRING_CHIP_DONE,
    WIRING_CHIP_ERROR,
    WIRING_CHIP_RUNNING,
    WIRING_CHIP_SKIPPED,
    WIRING_NOTHING_TO_CONNECT,
    WIRING_SKIP_PROWLARR_ALONE,
    wiring_failure_unreachable,
    wiring_line_app_sync,
    wiring_line_root_folder,
    wiring_note_still_waking,
    wiring_skip_no_prowlarr,
)

_CHIP_FOR_STATE: dict[WiringStepState, str] = {
    "running": WIRING_CHIP_RUNNING,
    "done": WIRING_CHIP_DONE,
    "skipped": WIRING_CHIP_SKIPPED,
    "error": WIRING_CHIP_ERROR,
}

_SYSTEM_STATUS_PATH = "system/status"


@dataclass(frozen=True)
class WiringContext:
    """Everything a task needs to actually run - built once per `run()` call."""

    client: ArrClient
    state: InstallState
    apps: Mapping[str, CatalogApp]


class WiringTask(Protocol):
    """One connection this run might make - or explain why it can't yet.

    Knows nothing about ordering, waiting, retrying or reporting - the
    engine owns all of that. `involved` doubles as the list of apps the
    engine proves ready before calling `apply`, in the order the mockup
    expects the posters to light up in (subject, then object). `about` is
    a separate, wider list: every app this step concerns, whether or not
    anything gets proven ready or written for it. Filtering on `involved`
    would silently drop a graceful "nothing to sync yet" explanation, whose
    `involved` is always empty.
    """

    # Declared as read-only properties, not plain attributes - every real
    # task is a frozen dataclass, and mypy treats a frozen field as
    # read-only, which only satisfies a Protocol that asks for the same.
    @property
    def key(self) -> str: ...

    @property
    def line(self) -> str: ...

    @property
    def involved(self) -> tuple[str, ...]: ...

    @property
    def about(self) -> tuple[str, ...]: ...

    async def apply(self, ctx: WiringContext) -> StepOutcome: ...


@dataclass(frozen=True)
class AppSyncTask:
    """Prowlarr gains, or repairs, a full-sync application entry for one partner."""

    key: str
    line: str
    involved: tuple[str, ...]
    prowlarr_id: str
    target_id: str

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        prowlarr = ctx.apps[self.prowlarr_id]
        target = ctx.apps[self.target_id]
        return await ensure_application(
            ctx.client,
            prowlarr,
            ctx.state.api_keys[self.prowlarr_id],
            target,
            ctx.state.api_keys[self.target_id],
        )


@dataclass(frozen=True)
class RootFolderTask:
    """One app gains, or already has, one library folder."""

    key: str
    line: str
    involved: tuple[str, ...]
    app_id: str
    media_folder: str
    host_path: PurePosixPath

    @property
    def about(self) -> tuple[str, ...]:
        return self.involved

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        app = ctx.apps[self.app_id]
        return await ensure_root_folder(
            ctx.client,
            app,
            ctx.state.api_keys[self.app_id],
            container_path=container_media_path(self.media_folder),
            host_path=self.host_path,
        )


@dataclass(frozen=True)
class ExplainTask:
    """A step whose outcome is always `skipped`, with a fixed plain-language note.

    Covers the two graceful "nothing to sync yet" cases: a partner chosen
    without Prowlarr, and Prowlarr chosen with no partner. `involved` is
    empty - there is no app to prove ready and nothing gets written - but
    `about` still names the app this explanation concerns, so filtering an
    add to `only_app` keeps it instead of silently dropping it.
    """

    key: str
    line: str
    involved: tuple[str, ...]
    about: tuple[str, ...]
    note: str

    async def apply(self, ctx: WiringContext) -> StepOutcome:
        return StepOutcome(
            state="skipped", note=self.note, technical=None, changed=False, transient=False
        )


def plan_wiring(state: InstallState, *, only_app: str | None = None) -> tuple[WiringTask, ...]:
    """The honest list of steps the owner's chosen apps justify. Pure - no client.

    Application-sync steps (or their graceful explanations) come first, in
    catalog order; then one root-folder step per media folder of each
    chosen app, also in catalog order. Input order never matters, the same
    way `apps_in_order` already guarantees for folder creation and compose.

    `only_app` narrows the result to the steps *about* that one app (used
    by an add or a reconnect), keeping the same relative order. `None`
    (the default) returns every step, unchanged from before this parameter
    existed.
    """
    apps = apps_in_order(state.app_ids)
    prowlarr = next((app for app in apps if app.id == "prowlarr"), None)
    partners = [app for app in apps if app.id != "prowlarr" and app.media_folders]

    tasks: list[WiringTask] = []

    if prowlarr is not None:
        if partners:
            for partner in partners:
                tasks.append(
                    AppSyncTask(
                        key=f"app-sync:{partner.id}",
                        line=wiring_line_app_sync(prowlarr.name, partner.name),
                        involved=(prowlarr.id, partner.id),
                        prowlarr_id=prowlarr.id,
                        target_id=partner.id,
                    )
                )
        else:
            tasks.append(
                ExplainTask(
                    key="prowlarr-alone",
                    line=WIRING_SKIP_PROWLARR_ALONE,
                    involved=(),
                    about=("prowlarr",),
                    note=WIRING_SKIP_PROWLARR_ALONE,
                )
            )
    else:
        for partner in partners:
            note = wiring_skip_no_prowlarr(partner.name)
            tasks.append(
                ExplainTask(
                    key=f"no-prowlarr:{partner.id}",
                    line=note,
                    involved=(),
                    about=(partner.id,),
                    note=note,
                )
            )

    for app in apps:
        for media_folder in app.media_folders:
            tasks.append(
                RootFolderTask(
                    key=f"root-folder:{app.id}",
                    line=wiring_line_root_folder(app.name, MEDIA_FOLDER_LABEL[media_folder]),
                    involved=(app.id,),
                    app_id=app.id,
                    media_folder=media_folder,
                    host_path=host_media_path(state.storage_root or "", media_folder),
                )
            )

    if only_app is None:
        return tuple(tasks)
    return tuple(task for task in tasks if only_app in task.about)


class WiringEngine:
    """Plans, waits for, runs and reports every wiring step for one run.

    Every non-deterministic dependency - the HTTP client, the clock, the
    sleep function - is injected, the same shape as `DeployManager`, so a
    test drives a slow app, a retry or a timeout with no network and no
    real waiting.
    """

    def __init__(
        self,
        client: ArrClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        ready_timeout: float = 180.0,
        ready_interval: float = 2.0,
        reassure_after: float = 30.0,
        attempts: int = 4,
        retry_delay: float = 3.0,
    ) -> None:
        self._client = client if client is not None else HttpArrClient()
        self._sleep = sleep
        self._clock = clock
        self._ready_timeout = ready_timeout
        self._ready_interval = ready_interval
        self._reassure_after = reassure_after
        self._attempts = attempts
        self._retry_delay = retry_delay

    async def run(
        self,
        state: InstallState,
        emit: Callable[[WiringStep], None],
        *,
        only_app: str | None = None,
    ) -> None:
        tasks = plan_wiring(state, only_app=only_app)
        if not tasks:
            emit(
                WiringStep(
                    index=1,
                    total=1,
                    key="nothing-to-connect",
                    line=WIRING_NOTHING_TO_CONNECT,
                    state="skipped",
                    chip=WIRING_CHIP_SKIPPED,
                    note=None,
                    technical=None,
                    involved=(),
                )
            )
            return

        ctx = WiringContext(client=self._client, state=state, apps={app.id: app for app in CATALOG})
        ready_apps: set[str] = set()
        total = len(tasks)
        for index, task in enumerate(tasks, start=1):
            await self._process_task(task, index, total, ctx, ready_apps, emit)

    async def _process_task(
        self,
        task: WiringTask,
        index: int,
        total: int,
        ctx: WiringContext,
        ready_apps: set[str],
        emit: Callable[[WiringStep], None],
    ) -> None:
        emit(self._frame(task, index, total, state="running", note=None))
        try:
            for app_id in task.involved:
                if app_id in ready_apps:
                    continue
                became_ready = await self._wait_until_ready(task, index, total, ctx, app_id, emit)
                if not became_ready:
                    name = ctx.apps[app_id].name
                    emit(
                        self._frame(
                            task,
                            index,
                            total,
                            state="error",
                            note=wiring_failure_unreachable(name),
                            technical=f"{task.key}: {app_id} never answered {_SYSTEM_STATUS_PATH}",
                        )
                    )
                    return
                ready_apps.add(app_id)

            outcome = await self._apply_with_retry(task, ctx)
        except Exception as error:  # a task must never take the whole run down with it
            name = ctx.apps[task.involved[0]].name if task.involved else task.key
            emit(
                self._frame(
                    task,
                    index,
                    total,
                    state="error",
                    note=wiring_failure_unreachable(name),
                    technical=f"{task.key}: {type(error).__name__}: {error}",
                )
            )
            return

        emit(
            self._frame(
                task,
                index,
                total,
                state=outcome.state,
                note=outcome.note,
                technical=f"{task.key}: {outcome.technical}" if outcome.technical else None,
            )
        )

    async def _wait_until_ready(
        self,
        task: WiringTask,
        index: int,
        total: int,
        ctx: WiringContext,
        app_id: str,
        emit: Callable[[WiringStep], None],
    ) -> bool:
        """Poll `system/status` until it answers 2xx, or the budget runs out.

        Every app is proved ready at most once per run - the caller only
        reaches this for an `app_id` not already in `ready_apps`.
        """
        app = ctx.apps[app_id]
        api_key = ctx.state.api_keys.get(app_id, "")
        base_url = app_base_url(app)
        path = f"{app.api_base}/{_SYSTEM_STATUS_PATH}"

        start = self._clock()
        reassured = False
        while True:
            response = await ctx.client.request("GET", base_url, path, api_key)
            if response.ok:
                return True

            elapsed = self._clock() - start
            if elapsed >= self._ready_timeout:
                return False

            if not reassured and elapsed >= self._reassure_after:
                reassured = True
                emit(
                    self._frame(
                        task,
                        index,
                        total,
                        state="running",
                        note=wiring_note_still_waking(app.name),
                    )
                )

            await self._sleep(self._ready_interval)

    async def _apply_with_retry(self, task: WiringTask, ctx: WiringContext) -> StepOutcome:
        """Call `task.apply` up to `self._attempts` times, but only while it
        keeps coming back transient - a considered "no" is never retried.
        """
        outcome = await task.apply(ctx)
        attempt = 1
        while outcome.transient and attempt < self._attempts:
            await self._sleep(self._retry_delay)
            outcome = await task.apply(ctx)
            attempt += 1
        return outcome

    @staticmethod
    def _frame(
        task: WiringTask,
        index: int,
        total: int,
        *,
        state: WiringStepState,
        note: str | None,
        technical: str | None = None,
    ) -> WiringStep:
        return WiringStep(
            index=index,
            total=total,
            key=task.key,
            line=task.line,
            state=state,
            chip=_CHIP_FOR_STATE[state],
            note=note,
            technical=technical,
            involved=task.involved,
        )
