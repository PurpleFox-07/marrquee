"""Tests for the wiring engine: the plan, the patient wait, the report.

`plan_wiring` is pure and tested with no client at all. Everything that
touches an app goes through `FakeArrClient` - no network, no Docker, no arr
app - and every wait is driven by an injected fake clock, so a 180-second
readiness budget or a 30-second reassurance threshold costs nothing real.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from marrquee import words
from marrquee.state import STATE_VERSION, InstallState
from marrquee.wiring import WiringRunner, WiringStep
from marrquee.wiring.arr_client import ArrFailure, ArrResponse, FakeArrClient, HttpArrClient
from marrquee.wiring.engine import (
    AppSyncTask,
    ExplainTask,
    RootFolderTask,
    WiringEngine,
    plan_wiring,
)

_ROOT = "/volume1/media"

# Prowlarr's `applications/schema` shape: a `disabled` template per
# implementation, the same fixture shape used in test_wiring_steps.py.
_SCHEMA: list[object] = [
    {
        "id": 0,
        "implementation": "Sonarr",
        "syncLevel": "disabled",
        "fields": [
            {"name": "prowlarrUrl", "value": ""},
            {"name": "baseUrl", "value": ""},
            {"name": "apiKey", "value": ""},
        ],
    },
    {
        "id": 0,
        "implementation": "Radarr",
        "syncLevel": "disabled",
        "fields": [
            {"name": "prowlarrUrl", "value": ""},
            {"name": "baseUrl", "value": ""},
            {"name": "apiKey", "value": ""},
        ],
    },
]


class _FakeClock:
    """A clock and a sleep function that agree with each other and never
    actually wait - mirrors `test_deploy.py`'s own fixture, so a 180-second
    readiness budget or a 30-second reassurance threshold costs nothing real.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _install_state(
    app_ids: tuple[str, ...],
    *,
    api_keys: Mapping[str, str] | None = None,
    storage_root: str = _ROOT,
) -> InstallState:
    keys = api_keys if api_keys is not None else {app_id: (app_id[:1] * 32) for app_id in app_ids}
    return InstallState(
        version=STATE_VERSION,
        storage_root=storage_root,
        app_ids=app_ids,
        api_keys=keys,
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="UTC",
        created="2026-09-23T00:00:00+00:00",
    )


def _ok(payload: object = None, status: int = 200) -> ArrResponse:
    return ArrResponse(ok=True, status=status, payload=payload, failures=(), detail=None)


def _created(payload: object = None) -> ArrResponse:
    return ArrResponse(ok=True, status=201, payload=payload, failures=(), detail=None)


def _failed(
    status: int, *, failures: tuple[ArrFailure, ...] = (), detail: str | None = None
) -> ArrResponse:
    return ArrResponse(ok=False, status=status, payload=None, failures=failures, detail=detail)


def _frames_by_key(steps: list[WiringStep]) -> dict[str, list[WiringStep]]:
    by_key: dict[str, list[WiringStep]] = {}
    for step in steps:
        by_key.setdefault(step.key, []).append(step)
    return by_key


# --- plan_wiring: pure, no client -------------------------------------------


def test_only_chosen_apps_are_wired() -> None:
    def keys_for(app_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(task.key for task in plan_wiring(_install_state(app_ids)))

    assert keys_for(("prowlarr", "sonarr", "radarr")) == (
        "app-sync:sonarr",
        "app-sync:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    )
    assert keys_for(("sonarr",)) == ("no-prowlarr:sonarr", "root-folder:sonarr")
    assert keys_for(("prowlarr",)) == ("prowlarr-alone",)


def test_prowlarr_alone_says_so_once() -> None:
    tasks = plan_wiring(_install_state(("prowlarr",)))

    assert len(tasks) == 1
    assert isinstance(tasks[0], ExplainTask)
    assert tasks[0].key == "prowlarr-alone"
    assert tasks[0].note == words.WIRING_SKIP_PROWLARR_ALONE


def test_a_partner_without_prowlarr_still_gets_its_library_folder_and_a_plain_explanation() -> None:
    tasks = plan_wiring(_install_state(("sonarr",)))

    assert [task.key for task in tasks] == ["no-prowlarr:sonarr", "root-folder:sonarr"]
    explain = tasks[0]
    assert isinstance(explain, ExplainTask)
    assert explain.note == words.wiring_skip_no_prowlarr("Sonarr")
    folder = tasks[1]
    assert isinstance(folder, RootFolderTask)
    assert folder.involved == ("sonarr",)


def test_an_empty_plan_has_no_tasks() -> None:
    assert plan_wiring(_install_state(())) == ()


def test_involved_names_the_apps_each_step_touches() -> None:
    state = _install_state(("prowlarr", "sonarr", "radarr"))
    tasks = {task.key: task for task in plan_wiring(state)}

    assert tasks["app-sync:sonarr"].involved == ("prowlarr", "sonarr")
    assert tasks["app-sync:radarr"].involved == ("prowlarr", "radarr")
    assert tasks["root-folder:sonarr"].involved == ("sonarr",)
    assert tasks["root-folder:radarr"].involved == ("radarr",)


@pytest.mark.parametrize(
    "app_ids",
    [
        (),
        ("prowlarr",),
        ("sonarr",),
        ("radarr",),
        ("prowlarr", "sonarr"),
        ("prowlarr", "radarr"),
        ("sonarr", "radarr"),
        ("prowlarr", "sonarr", "radarr"),
    ],
)
def test_step_numbering_is_honest_for_every_subset(app_ids: tuple[str, ...]) -> None:
    tasks = plan_wiring(_install_state(app_ids))

    # `total` always equals the number of steps the owner will see; an empty
    # plan is the engine's own job to turn into exactly one step (see
    # test_an_empty_plan_emits_one_skipped_step) rather than plan_wiring's.
    assert len(tasks) == len({task.key for task in tasks})


# --- WiringEngine.run: the empty and graceful-skip plans --------------------


async def test_an_empty_plan_emits_one_skipped_step() -> None:
    engine = WiringEngine(client=FakeArrClient({}))
    steps: list[WiringStep] = []

    await engine.run(_install_state(()), steps.append)

    assert len(steps) == 1
    step = steps[0]
    assert step.index == 1
    assert step.total == 1
    assert step.key == "nothing-to-connect"
    assert step.state == "skipped"
    assert step.chip == words.WIRING_CHIP_SKIPPED
    assert step.line == words.WIRING_NOTHING_TO_CONNECT


# --- WiringEngine.run: readiness, reassurance and timeout -------------------


async def test_a_slow_app_is_waited_for_never_failed() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [
                *([_failed(503)] * 20),
                _ok(None),
            ],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _ok([{"id": 1, "path": "/data/media/tv"}])
            ],
        }
    )
    clock = _FakeClock()
    engine = WiringEngine(
        client=fake, clock=clock.time, sleep=clock.sleep, ready_interval=2.0, reassure_after=30.0
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    folder_frames = _frames_by_key(steps)["root-folder:sonarr"]
    reassurance_frames = [
        frame
        for frame in folder_frames
        if frame.state == "running" and frame.note == words.wiring_note_still_waking("Sonarr")
    ]
    assert reassurance_frames, "expected at least one reassurance frame"
    assert folder_frames[-1].state == "done"


async def test_an_app_that_never_answers_ends_as_error_with_what_to_do_words() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_failed(503)] * 10,
        }
    )
    clock = _FakeClock()
    engine = WiringEngine(
        client=fake, clock=clock.time, sleep=clock.sleep, ready_interval=2.0, ready_timeout=5.0
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    folder_frames = _frames_by_key(steps)["root-folder:sonarr"]
    assert folder_frames[-1].state == "error"
    assert folder_frames[-1].note == words.wiring_failure_unreachable("Sonarr")
    assert folder_frames[-1].technical is not None
    assert "root-folder:sonarr" in folder_frames[-1].technical


async def test_readiness_is_checked_once_per_app_per_run() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([])],
            ("GET", "http://prowlarr:9696", "api/v1/applications/schema"): [_ok(_SCHEMA)],
            ("POST", "http://prowlarr:9696", "api/v1/applications"): [_created({"id": 5})],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _ok([{"id": 1, "path": "/data/media/tv"}])
            ],
        }
    )
    engine = WiringEngine(client=fake)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("prowlarr", "sonarr")), steps.append)

    frames = _frames_by_key(steps)
    assert frames["app-sync:sonarr"][-1].state == "done"
    assert frames["root-folder:sonarr"][-1].state == "done"
    status_calls = [call for call in fake.calls if call[2].endswith("system/status")]
    assert len(status_calls) == 2


# --- WiringEngine.run: retries -----------------------------------------------


async def test_a_transient_failure_is_retried_and_then_succeeds() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _failed(500, detail="boom"),
                _failed(500, detail="boom"),
                _ok([{"id": 1, "path": "/data/media/tv"}]),
            ],
        }
    )
    clock = _FakeClock()
    engine = WiringEngine(
        client=fake, clock=clock.time, sleep=clock.sleep, attempts=3, retry_delay=3.0
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    frames = _frames_by_key(steps)["root-folder:sonarr"]
    assert frames[-1].state == "done"
    rootfolder_calls = [call for call in fake.calls if call[2] == "api/v3/rootfolder"]
    assert len(rootfolder_calls) == 3
    assert clock.now == 6.0  # two retries, 3 seconds apart


async def test_a_400_is_never_retried() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [
                _failed(400, failures=(ArrFailure("Path", "not writable", False),))
            ],
        }
    )
    clock = _FakeClock()
    engine = WiringEngine(client=fake, clock=clock.time, sleep=clock.sleep, attempts=4)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    frames = _frames_by_key(steps)["root-folder:sonarr"]
    assert frames[-1].state == "error"
    post_calls = [call for call in fake.calls if call[0] == "POST"]
    assert len(post_calls) == 1
    assert clock.now == 0.0  # no retry delay was ever slept


# --- WiringEngine.run: reporting shape ---------------------------------------


async def test_every_step_emits_a_running_frame_and_then_exactly_one_terminal_frame() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://radarr:7878", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([]), _ok([])],
            ("GET", "http://prowlarr:9696", "api/v1/applications/schema"): [
                _ok(_SCHEMA),
                _ok(_SCHEMA),
            ],
            ("POST", "http://prowlarr:9696", "api/v1/applications"): [
                _created({"id": 5}),
                _created({"id": 6}),
            ],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [
                _created({"id": 1, "path": "/data/media/tv"})
            ],
            ("GET", "http://radarr:7878", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://radarr:7878", "api/v3/rootfolder"): [
                _created({"id": 2, "path": "/data/media/movies"})
            ],
        }
    )
    engine = WiringEngine(client=fake)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("prowlarr", "sonarr", "radarr")), steps.append)

    frames = _frames_by_key(steps)
    assert set(frames) == {
        "app-sync:sonarr",
        "app-sync:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    }
    terminal_states = {"done", "skipped", "error"}
    for key, key_frames in frames.items():
        assert key_frames[0].state == "running", key
        terminal = [frame for frame in key_frames if frame.state in terminal_states]
        assert len(terminal) == 1, key
        assert key_frames[-1] is terminal[0]
        assert key_frames[-1].state == "done"


# --- WiringEngine.run: never raises ------------------------------------------


async def test_the_engine_never_raises_even_when_a_task_throws() -> None:
    # An entirely unscripted FakeArrClient raises KeyError on its first
    # call - the engine's own "a task threw" path, with no network involved.
    engine = WiringEngine(client=FakeArrClient({}))
    steps: list[WiringStep] = []

    await engine.run(_install_state(("prowlarr", "sonarr", "radarr")), steps.append)

    frames = _frames_by_key(steps)
    for key_frames in frames.values():
        assert key_frames[-1].state == "error"
        assert key_frames[-1].technical is not None
        assert "KeyError" in key_frames[-1].technical


# --- Construction and protocol conformance -----------------------------------


def test_wiring_engine_satisfies_the_wiring_runner_protocol() -> None:
    runner: WiringRunner = WiringEngine(client=FakeArrClient({}))
    assert callable(runner.run)


def test_client_none_builds_the_real_http_arr_client() -> None:
    engine = WiringEngine()
    assert isinstance(engine._client, HttpArrClient)


def test_app_sync_task_involved_order_is_subject_then_object() -> None:
    tasks = {task.key: task for task in plan_wiring(_install_state(("prowlarr", "sonarr")))}
    task = tasks["app-sync:sonarr"]
    assert isinstance(task, AppSyncTask)
    assert task.line == words.wiring_line_app_sync("Prowlarr", "Sonarr")
