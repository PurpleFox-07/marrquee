"""A Docker-less demo server for watching the Deploy screen without a NAS.

Developer tool only. Nothing in `src/` imports this file, there is no
environment variable or setting anywhere that turns on a fake engine in the
shipped app, and `.dockerignore` excludes the whole `tools/` folder from
the image that ships - the only way to reach this code is to run it by
hand from a checkout.

It builds a real `DeployManager` against the real templates and scripts
Chunks 2-6 wrote. Only three things are scripted: the Docker engine (no
image is ever pulled and no container is ever started for real), the
readiness probe (no HTTP request ever leaves this process) and the wiring
runner (no arr app is ever called). Everything else - folder building, the
compose file, the view model, the page, the live event feed - is the real
code, running against a fresh temporary directory that stands in for the
NAS's `/config` and `/host` mounts.

Run one of the three demo scenes:

    uv run python tools/dev_fake_server.py --scene happy
    uv run python tools/dev_fake_server.py --scene wiring-problem
    uv run python tools/dev_fake_server.py --scene failure

Then open http://127.0.0.1:7788/deploy and press Deploy.

Every wait here goes through an injected `sleep`, the same shape
`DeployManager` and `WiringEngine` already use for their own tests - which
is what lets `tests/test_dev_fake_server.py` play every scene to its
ending in well under a second of real time, and what makes the delays
below purely cosmetic pacing for a human watching the screen.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.deploy import DeployManager, FakeReadinessProbe
from marrquee.docker_client import (
    ComposeResult,
    ContainerSnapshot,
    DockerStatus,
    FakeDockerEngine,
)
from marrquee.main import create_app
from marrquee.state import InstallState, save_state
from marrquee.wiring import WiringStep
from marrquee.wiring.engine import plan_wiring
from marrquee.words import (
    WIRING_CHIP_DONE,
    WIRING_CHIP_ERROR,
    WIRING_CHIP_RUNNING,
    wiring_failure_unreachable,
)

SCENES = ("happy", "wiring-problem", "failure")

_APP_IDS: tuple[str, ...] = ("prowlarr", "sonarr", "radarr")
_STORAGE_ROOT = "/volume1/media"
_HOST = "127.0.0.1"
_PORT = 7788

# Demo-sized so a developer isn't staring at a real 45-second reassurance
# wait or a real 5-minute timeout - see `DeployManager`'s own ClassVar
# comments for why the production values are that generous.
_POLL_INTERVAL_SECONDS = 0.5
_REASSURANCE_AFTER_SECONDS = 3.0
_NEVER_READY_AFTER_SECONDS = 10.0

_WIRING_STEP_SECONDS = 0.8
_WIRING_PROBLEM_TECHNICAL = (
    "demo wiring problem (tools/dev_fake_server.py --scene wiring-problem): "
    "the target app answered HTTP 500 when Prowlarr tried to add it. Nothing "
    "was really called - this text only exists to show what Last problem "
    "looks like with something on file."
)


class _DemoDeployManager(DeployManager):
    """`DeployManager` with demo-sized waits - see the module docstring."""

    POLL_INTERVAL_SECONDS = _POLL_INTERVAL_SECONDS
    REASSURANCE_AFTER_SECONDS = _REASSURANCE_AFTER_SECONDS
    NEVER_READY_AFTER_SECONDS = _NEVER_READY_AFTER_SECONDS


class _DemoDockerEngine(FakeDockerEngine):
    """Reports a service's container as `running` the moment its own
    `compose_up` succeeds.

    The exported `FakeDockerEngine` leaves that to whoever scripts it - a
    fixed lookup table is right for a unit test, but this demo needs the
    readiness probe to see a container that only just started, the same
    way a real one does, so a slow-starting scene has something honest to
    wait on.
    """

    async def compose_up(self, project: str, compose_file: Path, service: str) -> ComposeResult:
        result = await super().compose_up(project, compose_file, service)
        if result.ok:
            self._containers[service] = ContainerSnapshot(
                name=service,
                exists=True,
                state="running",
                exit_code=None,
                image=None,
                detail=None,
            )
        return result


class _DemoWiringRunner:
    """Emits real-shaped `WiringStep`s for the owner's chosen apps.

    `plan_wiring` is the same pure function the real `WiringEngine` uses to
    decide what steps a set of chosen apps justifies, so the demo's "Step N
    of M", step lines and `involved` app ids are exactly what a real run
    would show - only the outcome of each step (and how long it takes) is
    scripted, through the same injected `sleep` every other demo dependency
    uses.
    """

    def __init__(self, *, scene: str, sleep: Callable[[float], Awaitable[None]]) -> None:
        self._scene = scene
        self._sleep = sleep

    async def run(self, state: InstallState, emit: Callable[[WiringStep], None]) -> None:
        tasks = plan_wiring(state)
        total = len(tasks)
        for position, task in enumerate(tasks, start=1):
            emit(
                WiringStep(
                    index=position,
                    total=total,
                    key=task.key,
                    line=task.line,
                    state="running",
                    chip=WIRING_CHIP_RUNNING,
                    note=None,
                    technical=None,
                    involved=task.involved,
                )
            )
            await self._sleep(_WIRING_STEP_SECONDS)

            if self._scene == "wiring-problem" and position == total:
                target_name = get_app(task.involved[-1]).name if task.involved else "it"
                emit(
                    WiringStep(
                        index=position,
                        total=total,
                        key=task.key,
                        line=task.line,
                        state="error",
                        chip=WIRING_CHIP_ERROR,
                        note=wiring_failure_unreachable(target_name),
                        technical=_WIRING_PROBLEM_TECHNICAL,
                        involved=task.involved,
                    )
                )
                continue

            emit(
                WiringStep(
                    index=position,
                    total=total,
                    key=task.key,
                    line=task.line,
                    state="done",
                    chip=WIRING_CHIP_DONE,
                    note=None,
                    technical=None,
                    involved=task.involved,
                )
            )


def _probe_for_scene(scene: str) -> FakeReadinessProbe:
    radarr = get_app("radarr")
    if scene == "happy":
        # A handful of "not yet" answers earns the reassurance note
        # honestly, then Radarr comes up like everything else.
        return FakeReadinessProbe(responses={(radarr.id, radarr.port): [False] * 8}, default=True)
    if scene == "failure":
        # Prowlarr and Sonarr answer ready first try; Radarr never does, so
        # the deploy times out on it, exactly like a real stuck app.
        prowlarr, sonarr = get_app("prowlarr"), get_app("sonarr")
        return FakeReadinessProbe(
            responses={(prowlarr.id, prowlarr.port): [True], (sonarr.id, sonarr.port): [True]},
            default=False,
        )
    return FakeReadinessProbe(default=True)  # "wiring-problem": every app comes up cleanly


def _seed_install(settings: Settings) -> None:
    """Create the demo's storage folder and save the choices a deploy reads."""
    (settings.host_mount / "volume1" / "media").mkdir(parents=True, exist_ok=True)
    save_state(
        settings.config_dir,
        InstallState(
            version=1,
            storage_root=_STORAGE_ROOT,
            app_ids=_APP_IDS,
            api_keys={app_id: f"demo-{app_id}-api-key" for app_id in _APP_IDS},
            # `build_folders` chowns every folder it creates for real.
            # Chowning a path you already own to your own id needs no
            # privileges - the same reasoning `tests/test_deploy.py` uses
            # for the same call.
            puid=os.getuid(),
            pgid=os.getgid(),
            umask="002",
            timezone="Etc/UTC",
            created="2026-09-23T00:00:00+00:00",
        ),
    )


def build_app(
    *,
    scene: str,
    root: Path,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> FastAPI:
    """Build a Marrquee app whose deploy engine is real but whose Docker,
    readiness and wiring are scripted - see the module docstring.

    `root` stands in for the NAS's mounts: `root/config` is `/config` and
    `root/host` is `/host`. Nothing here ever reads or writes outside it.

    Resolved up front: a raw `tempfile.TemporaryDirectory()` path can carry
    an unresolved symlink segment (on a Mac, `/var` is itself a symlink to
    `/private/var`), and `storage.check_storage_root` resolves the typed
    folder before deciding it's inside `root` - an unresolved `root` here
    would make that check see two different paths and refuse a perfectly
    real folder as unshared.
    """
    if scene not in SCENES:
        raise ValueError(f"unknown scene {scene!r} - choose one of {SCENES}")

    root = root.resolve()
    settings = Settings(config_dir=root / "config", host_mount=root / "host")
    _seed_install(settings)

    engine = _DemoDockerEngine(DockerStatus(connected=True))
    manager = _DemoDeployManager(
        settings,
        engine,
        probe=_probe_for_scene(scene),
        clock=clock,
        sleep=sleep,
        wiring=_DemoWiringRunner(scene=scene, sleep=sleep),
    )
    return create_app(settings, engine, manager=manager)


def main(argv: Sequence[str] | None = None) -> None:
    """Run the demo server at http://127.0.0.1:7788, in a fresh temporary
    directory that is deleted again when the process stops.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene", choices=SCENES, default="happy")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="marrquee-dev-fake-server-") as tmp:
        app = build_app(scene=args.scene, root=Path(tmp), clock=time.monotonic, sleep=asyncio.sleep)
        uvicorn.run(app, host=_HOST, port=_PORT)


if __name__ == "__main__":
    main()
