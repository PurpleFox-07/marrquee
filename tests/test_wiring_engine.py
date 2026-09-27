"""Tests for the wiring engine: the plan, the patient wait, the report.

`plan_wiring` is pure and tested with no client at all. Everything that
touches an app goes through `FakeArrClient` - no network, no Docker, no arr
app - and every wait is driven by an injected fake clock, so a 180-second
readiness budget or a 30-second reassurance threshold costs nothing real.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest

from marrquee import questions, words
from marrquee.config import Settings
from marrquee.jellyfin import FakeJellyfinServer, JellyfinResponse, save_jellyfin
from marrquee.plex import (
    ExistingPlex,
    FakePlexServer,
    PlexIdentity,
    PlexResponse,
    browse_path,
    load_existing_plex,
    save_existing_plex,
    save_plex_sign_in,
)
from marrquee.questions import save_step_answers
from marrquee.state import STATE_VERSION, InstallState
from marrquee.storage import container_media_path, host_media_path
from marrquee.vpn_control import FakeGluetunControl
from marrquee.wiring import WiringRunner, WiringStep
from marrquee.wiring.arr_client import ArrFailure, ArrResponse, FakeArrClient, HttpArrClient
from marrquee.wiring.engine import (
    AppSyncTask,
    DownloadClientTask,
    ExistingPlexLibrariesTask,
    ExplainTask,
    JellyfinGraphicsTask,
    JellyfinLibrariesTask,
    PlexDirectPlayTask,
    PlexLibrariesTask,
    QbitSettingsTask,
    RootFolderTask,
    WiringEngine,
    plan_wiring,
)
from marrquee.wiring.qbit_client import FakeQbitClient, QbitResponse

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


def _qbit_ok(payload: object = None, status: int = 200) -> QbitResponse:
    return QbitResponse(ok=True, status=status, payload=payload, detail=None)


def _posted_json(call: tuple[str, str, str, Mapping[str, str] | None]) -> object:
    form = call[3]
    assert form is not None
    return json.loads(form["json"])


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


# --- about / only_app -------------------------------------------------------

# The unfiltered plan for every 1-, 2- and 3-app subset - pinned by hand so
# a mutation in the only_app filter (applied even when only_app is None)
# has something honest to fail against, not just "equals itself".
_EXPECTED_KEYS: dict[tuple[str, ...], tuple[str, ...]] = {
    (): (),
    ("prowlarr",): ("prowlarr-alone",),
    ("sonarr",): ("no-prowlarr:sonarr", "root-folder:sonarr"),
    ("radarr",): ("no-prowlarr:radarr", "root-folder:radarr"),
    ("prowlarr", "sonarr"): ("app-sync:sonarr", "root-folder:sonarr"),
    ("prowlarr", "radarr"): ("app-sync:radarr", "root-folder:radarr"),
    ("sonarr", "radarr"): (
        "no-prowlarr:sonarr",
        "no-prowlarr:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    ),
    ("prowlarr", "sonarr", "radarr"): (
        "app-sync:sonarr",
        "app-sync:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    ),
}


def test_app_sync_and_root_folder_tasks_about_equals_involved() -> None:
    tasks = {task.key: task for task in plan_wiring(_install_state(("prowlarr", "sonarr")))}

    assert tasks["app-sync:sonarr"].about == tasks["app-sync:sonarr"].involved
    assert tasks["root-folder:sonarr"].about == tasks["root-folder:sonarr"].involved


def test_explain_task_about_names_the_app_even_though_nothing_is_involved() -> None:
    """`involved` stays empty (there is nothing to prove ready, nothing gets
    written) - but `about` still names the app, or a filtered plan would
    silently drop the honest "nothing to sync yet" explanation.
    """
    no_prowlarr = {task.key: task for task in plan_wiring(_install_state(("sonarr",)))}
    explain = no_prowlarr["no-prowlarr:sonarr"]
    assert explain.involved == ()
    assert explain.about == ("sonarr",)

    prowlarr_alone = {task.key: task for task in plan_wiring(_install_state(("prowlarr",)))}
    alone = prowlarr_alone["prowlarr-alone"]
    assert alone.involved == ()
    assert alone.about == ("prowlarr",)


def test_only_app_keeps_the_steps_about_that_app() -> None:
    three_apps = _install_state(("prowlarr", "sonarr", "radarr"))
    assert [task.key for task in plan_wiring(three_apps)] == [
        "app-sync:sonarr",
        "app-sync:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    ]

    assert [task.key for task in plan_wiring(three_apps, only_app="radarr")] == [
        "app-sync:radarr",
        "root-folder:radarr",
    ]

    two_apps = _install_state(("sonarr", "radarr"))
    assert [task.key for task in plan_wiring(two_apps, only_app="sonarr")] == [
        "no-prowlarr:sonarr",
        "root-folder:sonarr",
    ]


@pytest.mark.parametrize("app_ids", list(_EXPECTED_KEYS))
def test_only_app_none_is_byte_identical_to_todays_plan(app_ids: tuple[str, ...]) -> None:
    state = _install_state(app_ids)

    tasks = plan_wiring(state, only_app=None)

    assert tuple(task.key for task in tasks) == _EXPECTED_KEYS[app_ids]
    assert tuple(task.line for task in plan_wiring(state)) == tuple(task.line for task in tasks)


async def test_an_empty_filtered_plan_emits_nothing_to_connect() -> None:
    engine = WiringEngine(client=FakeArrClient({}))
    steps: list[WiringStep] = []
    state = _install_state(("prowlarr", "sonarr"))

    await engine.run(state, steps.append, only_app="radarr")

    assert len(steps) == 1
    assert steps[0].key == "nothing-to-connect"
    assert steps[0].state == "skipped"
    assert steps[0].total == 1


async def test_only_app_step_numbering_counts_the_filtered_list() -> None:
    """ "Step N of M" has to describe what the owner is watching happen right
    now - the filtered add, not the whole stack's plan - or a two-step add
    would misleadingly claim to be "step 1 of 4".
    """
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/system/status"): [_ok(None)],
            ("GET", "http://radarr:7878", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([])],
            ("GET", "http://prowlarr:9696", "api/v1/applications/schema"): [_ok(_SCHEMA)],
            ("POST", "http://prowlarr:9696", "api/v1/applications"): [_created({"id": 5})],
            ("GET", "http://radarr:7878", "api/v3/rootfolder"): [
                _ok([{"id": 2, "path": "/data/media/movies"}])
            ],
        }
    )
    engine = WiringEngine(client=fake)
    steps: list[WiringStep] = []
    state = _install_state(("prowlarr", "sonarr", "radarr"))

    await engine.run(state, steps.append, only_app="radarr")

    frames = _frames_by_key(steps)
    assert set(frames) == {"app-sync:radarr", "root-folder:radarr"}
    assert {frame.total for step_frames in frames.values() for frame in step_frames} == {2}


# --- plan_wiring: qBittorrent's own settings and download-client tasks ------


def test_downloader_tasks_land_after_app_sync_and_before_root_folders() -> None:
    state = _install_state(("prowlarr", "sonarr", "radarr", "gluetun", "qbittorrent"))

    keys = [task.key for task in plan_wiring(state)]

    assert keys == [
        "app-sync:sonarr",
        "app-sync:radarr",
        "downloader-settings:qbittorrent",
        "download-client:sonarr",
        "download-client:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    ]


def test_no_qbittorrent_leaves_the_plan_exactly_as_before() -> None:
    state = _install_state(("prowlarr", "sonarr", "radarr"))

    keys = [task.key for task in plan_wiring(state)]

    assert keys == [
        "app-sync:sonarr",
        "app-sync:radarr",
        "root-folder:sonarr",
        "root-folder:radarr",
    ]


def test_only_app_sonarr_keeps_its_download_client_task() -> None:
    state = _install_state(("prowlarr", "sonarr", "radarr", "gluetun", "qbittorrent"))

    keys = [task.key for task in plan_wiring(state, only_app="sonarr")]

    assert keys == ["app-sync:sonarr", "download-client:sonarr", "root-folder:sonarr"]


def test_downloader_tasks_about_equals_involved() -> None:
    state = _install_state(("sonarr", "gluetun", "qbittorrent"))
    tasks = {task.key: task for task in plan_wiring(state)}

    settings_task = tasks["downloader-settings:qbittorrent"]
    assert isinstance(settings_task, QbitSettingsTask)
    assert settings_task.involved == ("qbittorrent",)
    assert settings_task.about == settings_task.involved

    client_task = tasks["download-client:sonarr"]
    assert isinstance(client_task, DownloadClientTask)
    assert client_task.involved == ("sonarr", "qbittorrent")
    assert client_task.about == client_task.involved


def test_radarr_download_client_task_uses_movies() -> None:
    state = _install_state(("radarr", "gluetun", "qbittorrent"))
    tasks = {task.key: task for task in plan_wiring(state)}

    client_task = tasks["download-client:radarr"]
    assert isinstance(client_task, DownloadClientTask)
    assert client_task.category_field == "movieCategory"
    assert client_task.media_folder == "movies"


# --- WiringEngine.run: qBittorrent's own settings task ----------------------


async def test_qbittorrent_is_ready_without_polling_system_status() -> None:
    fake_arr = FakeArrClient({})
    fake_qbit = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [_qbit_ok({})],
            ("POST", "http://gluetun:8080", "api/v2/app/setPreferences"): [_qbit_ok()],
        }
    )
    engine = WiringEngine(client=fake_arr, qbit=fake_qbit)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("gluetun", "qbittorrent")), steps.append)

    frames = _frames_by_key(steps)["downloader-settings:qbittorrent"]
    assert frames[-1].state == "done"
    assert not any(call[2].endswith("system/status") for call in fake_arr.calls)


async def test_equal_seeding_preferences_post_nothing() -> None:
    from marrquee.qbittorrent import QBIT_BASE_PREFERENCES
    from marrquee.seeding import seeding_preferences

    current = {**QBIT_BASE_PREFERENCES, **seeding_preferences(None)}
    fake_qbit = FakeQbitClient(
        {("GET", "http://gluetun:8080", "api/v2/app/preferences"): [_qbit_ok(current)]}
    )
    engine = WiringEngine(client=FakeArrClient({}), qbit=fake_qbit)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("gluetun", "qbittorrent")), steps.append)

    frames = _frames_by_key(steps)["downloader-settings:qbittorrent"]
    assert frames[-1].state == "done"
    assert frames[-1].note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert not any(call[0] == "POST" for call in fake_qbit.calls)


async def test_a_changed_seeding_answer_posts_the_new_limits(tmp_path: Path) -> None:
    questions.save_step_answers(tmp_path, "qbittorrent", {"seeding": "private"})
    fake_qbit = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [_qbit_ok({})],
            ("POST", "http://gluetun:8080", "api/v2/app/setPreferences"): [_qbit_ok()],
        }
    )
    engine = WiringEngine(client=FakeArrClient({}), qbit=fake_qbit, config_dir=tmp_path)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("gluetun", "qbittorrent")), steps.append)

    post_calls = [call for call in fake_qbit.calls if call[0] == "POST"]
    assert len(post_calls) == 1
    body = _posted_json(post_calls[0])
    assert isinstance(body, dict)
    assert body["max_seeding_time"] == 43200


async def test_a_forwarded_port_is_included_a_missing_one_is_not() -> None:
    fake_qbit = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [
                _qbit_ok({}),
                _qbit_ok({}),
            ],
            ("POST", "http://gluetun:8080", "api/v2/app/setPreferences"): [
                _qbit_ok(),
                _qbit_ok(),
            ],
        }
    )
    state = _install_state(("gluetun", "qbittorrent"))

    engine_with_port = WiringEngine(
        client=FakeArrClient({}), qbit=fake_qbit, vpn=FakeGluetunControl(port=51413)
    )
    steps_with_port: list[WiringStep] = []
    await engine_with_port.run(state, steps_with_port.append)
    body_with_port = _posted_json(fake_qbit.calls[1])
    assert isinstance(body_with_port, dict)
    assert body_with_port["listen_port"] == 51413

    engine_without_port = WiringEngine(
        client=FakeArrClient({}), qbit=fake_qbit, vpn=FakeGluetunControl(port=None)
    )
    steps_without_port: list[WiringStep] = []
    await engine_without_port.run(state, steps_without_port.append)
    body_without_port = _posted_json(fake_qbit.calls[3])
    assert isinstance(body_without_port, dict)
    assert "listen_port" not in body_without_port


async def test_no_listen_port_and_no_control_call_without_a_vpn() -> None:
    """A stale `api_keys["gluetun"]` left over from an earlier install
    (`with_app_removed` keeps a departed app's own key) must never be read
    once gluetun has actually left `app_ids` - that would call a Gluetun
    that no longer exists. Gluetun is deliberately ABSENT from `app_ids`
    here while its key lingers in `api_keys`, so a guard keyed on the key
    alone would pass this test vacuously.
    """
    fake_qbit = FakeQbitClient(
        {
            ("GET", "http://qbittorrent:8080", "api/v2/app/preferences"): [_qbit_ok({})],
            ("POST", "http://qbittorrent:8080", "api/v2/app/setPreferences"): [_qbit_ok()],
        }
    )
    fake_vpn = FakeGluetunControl(port=51413)
    state = _install_state(
        ("sonarr", "qbittorrent"),
        api_keys={"sonarr": "s" * 32, "qbittorrent": "qbt_" + "q" * 28, "gluetun": "g" * 32},
    )
    engine = WiringEngine(client=FakeArrClient({}), qbit=fake_qbit, vpn=fake_vpn)
    steps: list[WiringStep] = []

    await engine.run(state, steps.append)

    post_calls = [call for call in fake_qbit.calls if call[0] == "POST"]
    assert len(post_calls) == 1
    body = _posted_json(post_calls[0])
    assert isinstance(body, dict)
    assert "listen_port" not in body
    assert fake_vpn.calls == []


# --- WiringEngine.run: Sonarr/Radarr's download client ----------------------


async def test_download_client_task_creates_category_then_the_client() -> None:
    fake_arr = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/downloadclient"): [_ok([])],
            ("GET", "http://sonarr:8989", "api/v3/downloadclient/schema"): [
                _ok(
                    [
                        {
                            "id": 0,
                            "name": "qBittorrent",
                            "implementation": "QBittorrent",
                            "fields": [
                                {"name": "host", "value": ""},
                                {"name": "port", "value": 8080},
                                {"name": "tvCategory", "value": ""},
                            ],
                        }
                    ]
                )
            ],
            ("POST", "http://sonarr:8989", "api/v3/downloadclient"): [_created({"id": 4})],
        }
    )
    fake_qbit = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [_qbit_ok({})],
            ("POST", "http://gluetun:8080", "api/v2/app/setPreferences"): [_qbit_ok()],
            ("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [_qbit_ok({})],
            ("POST", "http://gluetun:8080", "api/v2/torrents/createCategory"): [_qbit_ok()],
        }
    )
    engine = WiringEngine(client=fake_arr, qbit=fake_qbit)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr", "gluetun", "qbittorrent")), steps.append)

    frames = _frames_by_key(steps)["download-client:sonarr"]
    assert frames[-1].state == "done"
    assert any(call[2] == "api/v2/torrents/createCategory" for call in fake_qbit.calls)
    assert any(call[0] == "POST" and call[2] == "api/v3/downloadclient" for call in fake_arr.calls)


async def test_a_category_failure_stops_before_the_client_is_ever_written() -> None:
    """The category write happens first: if qBittorrent refuses it, Sonarr's
    own download-client endpoint is never even called.
    """
    fake_arr = FakeArrClient({("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)]})
    fake_qbit = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [_qbit_ok({})],
            ("POST", "http://gluetun:8080", "api/v2/app/setPreferences"): [_qbit_ok()],
            ("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [
                QbitResponse(ok=False, status=403, payload=None, detail="refused")
            ],
        }
    )
    engine = WiringEngine(client=fake_arr, qbit=fake_qbit)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr", "gluetun", "qbittorrent")), steps.append)

    frames = _frames_by_key(steps)["download-client:sonarr"]
    assert frames[-1].state == "error"
    assert not any(call[2].startswith("api/v3/downloadclient") for call in fake_arr.calls)


# --- plan_wiring: Plex's libraries and direct-play tasks --------------------


def _plex_ok(payload: object = None, status: int = 200) -> PlexResponse:
    return PlexResponse(ok=True, status=status, payload=payload, detail=None)


def _plex_prefs_on() -> PlexResponse:
    return _plex_ok(
        {"MediaContainer": {"Setting": [{"id": "TranscoderCanOnlyRemuxVideo", "value": True}]}}
    )


def _plex_no_libraries() -> PlexResponse:
    return _plex_ok({"MediaContainer": {"Directory": []}})


async def _no_plex_address() -> str | None:
    return None


async def _fake_plex_address() -> str | None:
    return "192.168.1.5"


def test_plan_wiring_appends_the_plex_tasks_last_only_when_plex_is_installed() -> None:
    state = _install_state(("prowlarr", "sonarr", "plex"))

    keys = [task.key for task in plan_wiring(state)]

    assert keys == [
        "app-sync:sonarr",
        "root-folder:sonarr",
        "libraries:plex",
        "direct-play:plex",
    ]

    without_plex_keys = [task.key for task in plan_wiring(_install_state(("prowlarr", "sonarr")))]
    assert "libraries:plex" not in without_plex_keys
    assert "direct-play:plex" not in without_plex_keys


def test_plex_tasks_about_equals_involved() -> None:
    tasks = {task.key: task for task in plan_wiring(_install_state(("plex",)))}

    libraries = tasks["libraries:plex"]
    assert isinstance(libraries, PlexLibrariesTask)
    assert libraries.involved == ("plex",)
    assert libraries.about == libraries.involved

    direct_play = tasks["direct-play:plex"]
    assert isinstance(direct_play, PlexDirectPlayTask)
    assert direct_play.involved == ("plex",)
    assert direct_play.about == direct_play.involved


def test_plex_task_lines_match_content_direction() -> None:
    tasks = {task.key: task for task in plan_wiring(_install_state(("plex",)))}

    assert tasks["libraries:plex"].line == words.wiring_line_libraries("Plex")
    assert tasks["direct-play:plex"].line == words.WIRING_LINE_PLEX_DIRECT_PLAY


# --- WiringEngine.run: resolving Plex's address and saved token ------------


async def test_missing_plex_address_reports_unreachable_for_both_tasks() -> None:
    engine = WiringEngine(
        client=FakeArrClient({}),
        plex=FakePlexServer(),
        plex_address=_no_plex_address,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("plex",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:plex"][-1].state == "error"
    assert frames["libraries:plex"][-1].note == words.wiring_failure_unreachable("Plex")
    assert frames["direct-play:plex"][-1].state == "error"
    assert frames["direct-play:plex"][-1].note == words.wiring_failure_unreachable("Plex")


async def test_missing_plex_token_reports_sign_in_needed(tmp_path: Path) -> None:
    engine = WiringEngine(
        client=FakeArrClient({}),
        plex=FakePlexServer(),
        plex_address=_fake_plex_address,
        config_dir=tmp_path,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("plex",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:plex"][-1].state == "error"
    assert frames["libraries:plex"][-1].note == words.WIRING_PLEX_SIGN_IN_NEEDED
    assert frames["direct-play:plex"][-1].state == "error"
    assert frames["direct-play:plex"][-1].note == words.WIRING_PLEX_SIGN_IN_NEEDED


async def test_plex_tasks_use_the_resolved_address_and_saved_token(tmp_path: Path) -> None:
    save_plex_sign_in(tmp_path, "the-plex-token", "owner")
    server = FakePlexServer(
        script={
            ("GET", "/library/sections"): [_plex_no_libraries()],
            ("POST", "/library/sections"): [_plex_ok(), _plex_ok()],
            ("GET", "/:/prefs"): [_plex_prefs_on()],
        }
    )
    engine = WiringEngine(
        client=FakeArrClient({}),
        plex=server,
        plex_address=_fake_plex_address,
        config_dir=tmp_path,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("plex",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:plex"][-1].state == "done"
    assert frames["direct-play:plex"][-1].state == "done"


async def test_a_401_from_plex_is_retried_then_errors(tmp_path: Path) -> None:
    save_plex_sign_in(tmp_path, "the-plex-token", "owner")
    server = FakePlexServer(
        script={
            ("GET", "/library/sections"): [
                PlexResponse(ok=False, status=401, payload=None, detail=None) for _ in range(4)
            ],
            ("GET", "/:/prefs"): [_plex_prefs_on()],
        }
    )
    clock = _FakeClock()
    engine = WiringEngine(
        client=FakeArrClient({}),
        plex=server,
        plex_address=_fake_plex_address,
        config_dir=tmp_path,
        clock=clock.time,
        sleep=clock.sleep,
        attempts=4,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("plex",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:plex"][-1].state == "error"
    gets = [call for call in server.calls if call[0] == "GET" and call[1] == "/library/sections"]
    assert len(gets) == 4


async def test_plex_free_run_never_resolves_the_plex_address_or_reads_plex_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plex-free install must cost nothing extra: `plex_address()` and the
    plex.json loader must never even be CALLED, not merely "answer nothing" -
    the cost of the Plex feature must be zero for every install that never
    added Plex.
    """
    import marrquee.wiring.engine as engine_module

    address_calls: list[None] = []

    async def _recording_address() -> str | None:
        address_calls.append(None)
        return None

    load_calls: list[Path] = []

    def _recording_load(config_dir: Path) -> None:
        load_calls.append(config_dir)
        return None

    monkeypatch.setattr(engine_module, "load_plex_account", _recording_load)

    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _ok([{"id": 1, "path": "/data/media/tv"}])
            ],
        }
    )
    engine = WiringEngine(client=fake, config_dir=tmp_path, plex_address=_recording_address)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    assert address_calls == []
    assert load_calls == []
    assert _frames_by_key(steps)["root-folder:sonarr"][-1].state == "done"


# --- plan_wiring: Jellyfin's libraries and graphics tasks -------------------


def _jellyfin_ok(payload: object = None, status: int = 200) -> JellyfinResponse:
    return JellyfinResponse(ok=True, status=status, payload=payload, detail=None)


def _jellyfin_no_libraries() -> JellyfinResponse:
    return _jellyfin_ok([])


def _jellyfin_vaapi_already_set() -> JellyfinResponse:
    return _jellyfin_ok({"HardwareAccelerationType": "vaapi", "VaapiDevice": "/dev/dri/renderD128"})


def test_plan_wiring_appends_the_jellyfin_tasks_last_after_plex() -> None:
    state = _install_state(("prowlarr", "sonarr", "plex", "jellyfin"))

    keys = [task.key for task in plan_wiring(state, answers={"jellyfin": {"graphics_chip": "yes"}})]

    assert keys == [
        "app-sync:sonarr",
        "root-folder:sonarr",
        "libraries:plex",
        "direct-play:plex",
        "libraries:jellyfin",
        "graphics:jellyfin",
    ]


@pytest.mark.parametrize(
    "app_ids",
    [
        (),
        ("prowlarr",),
        ("prowlarr", "sonarr", "radarr"),
        ("prowlarr", "sonarr", "qbittorrent"),
        ("plex",),
        ("prowlarr", "sonarr", "plex"),
    ],
)
def test_no_jellyfin_leaves_the_plan_byte_identical(app_ids: tuple[str, ...]) -> None:
    """A jellyfin-free install must cost nothing: adding the `answers`
    parameter, and Jellyfin's own tasks, must never change one key of an
    existing plan - even when `answers` claims the graphics chip is wanted.
    """
    state = _install_state(app_ids)

    before = [task.key for task in plan_wiring(state)]
    after = [
        task.key for task in plan_wiring(state, answers={"jellyfin": {"graphics_chip": "yes"}})
    ]

    assert before == after
    assert "libraries:jellyfin" not in after
    assert "graphics:jellyfin" not in after


def test_no_graphics_task_without_a_yes_answer() -> None:
    state = _install_state(("jellyfin",))

    assert [task.key for task in plan_wiring(state)] == ["libraries:jellyfin"]
    assert [task.key for task in plan_wiring(state, answers={})] == ["libraries:jellyfin"]
    assert [
        task.key for task in plan_wiring(state, answers={"jellyfin": {"graphics_chip": "no"}})
    ] == ["libraries:jellyfin"]
    assert [
        task.key for task in plan_wiring(state, answers={"jellyfin": {"graphics_chip": "yes"}})
    ] == ["libraries:jellyfin", "graphics:jellyfin"]


def test_jellyfin_tasks_about_equals_involved() -> None:
    state = _install_state(("jellyfin",))
    tasks = {
        task.key: task
        for task in plan_wiring(state, answers={"jellyfin": {"graphics_chip": "yes"}})
    }

    libraries = tasks["libraries:jellyfin"]
    assert isinstance(libraries, JellyfinLibrariesTask)
    assert libraries.involved == ("jellyfin",)
    assert libraries.about == libraries.involved

    graphics = tasks["graphics:jellyfin"]
    assert isinstance(graphics, JellyfinGraphicsTask)
    assert graphics.involved == ("jellyfin",)
    assert graphics.about == graphics.involved


def test_jellyfin_task_lines_match_content_direction() -> None:
    state = _install_state(("jellyfin",))
    tasks = {
        task.key: task
        for task in plan_wiring(state, answers={"jellyfin": {"graphics_chip": "yes"}})
    }

    assert tasks["libraries:jellyfin"].line == words.wiring_line_libraries("Jellyfin")
    assert tasks["graphics:jellyfin"].line == words.WIRING_LINE_JELLYFIN_GRAPHICS


def test_only_app_jellyfin_keeps_both_jellyfin_tasks() -> None:
    state = _install_state(("prowlarr", "sonarr", "jellyfin"))

    keys = [
        task.key
        for task in plan_wiring(
            state, only_app="jellyfin", answers={"jellyfin": {"graphics_chip": "yes"}}
        )
    ]

    assert keys == ["libraries:jellyfin", "graphics:jellyfin"]


# --- WiringEngine.run: resolving Jellyfin's address and saved key -----------


async def _no_jellyfin_address() -> str | None:
    return None


async def _fake_jellyfin_address() -> str | None:
    return "192.168.1.6"


async def test_missing_jellyfin_address_reports_unreachable_for_both_tasks(
    tmp_path: Path,
) -> None:
    save_step_answers(tmp_path, "jellyfin", {"graphics_chip": "yes"})
    engine = WiringEngine(
        client=FakeArrClient({}),
        jellyfin=FakeJellyfinServer(script={}),
        jellyfin_address=_no_jellyfin_address,
        config_dir=tmp_path,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("jellyfin",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:jellyfin"][-1].state == "error"
    assert frames["libraries:jellyfin"][-1].note == words.wiring_failure_unreachable("Jellyfin")
    assert frames["graphics:jellyfin"][-1].state == "error"
    assert frames["graphics:jellyfin"][-1].note == words.wiring_failure_unreachable("Jellyfin")


async def test_missing_jellyfin_key_reports_not_set_up(tmp_path: Path) -> None:
    save_step_answers(tmp_path, "jellyfin", {"graphics_chip": "yes"})
    engine = WiringEngine(
        client=FakeArrClient({}),
        jellyfin=FakeJellyfinServer(script={}),
        jellyfin_address=_fake_jellyfin_address,
        config_dir=tmp_path,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("jellyfin",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:jellyfin"][-1].state == "error"
    assert frames["libraries:jellyfin"][-1].note == words.WIRING_JELLYFIN_NOT_SET_UP
    assert frames["graphics:jellyfin"][-1].state == "error"
    assert frames["graphics:jellyfin"][-1].note == words.WIRING_JELLYFIN_NOT_SET_UP


async def test_jellyfin_tasks_use_the_resolved_address_and_saved_key(tmp_path: Path) -> None:
    save_jellyfin(tmp_path, "the-jellyfin-key", "admin-id")
    save_step_answers(tmp_path, "jellyfin", {"graphics_chip": "yes"})
    server = FakeJellyfinServer(
        script={
            ("GET", "/Library/VirtualFolders"): [_jellyfin_no_libraries()],
            ("POST", "/Library/VirtualFolders"): [_jellyfin_ok(), _jellyfin_ok()],
            ("GET", "/System/Configuration/encoding"): [_jellyfin_vaapi_already_set()],
        }
    )
    engine = WiringEngine(
        client=FakeArrClient({}),
        jellyfin=server,
        jellyfin_address=_fake_jellyfin_address,
        config_dir=tmp_path,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("jellyfin",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:jellyfin"][-1].state == "done"
    assert frames["graphics:jellyfin"][-1].state == "done"
    assert all(call[3] for call in server.calls)  # every call carried the saved key


async def test_a_401_from_jellyfin_libraries_is_not_retried_and_errors(tmp_path: Path) -> None:
    save_jellyfin(tmp_path, "the-jellyfin-key", "admin-id")
    unauthorized = JellyfinResponse(ok=False, status=401, payload=None, detail=None)
    server = FakeJellyfinServer(
        script={("GET", "/Library/VirtualFolders"): [unauthorized for _ in range(4)]}
    )
    engine = WiringEngine(
        client=FakeArrClient({}),
        jellyfin=server,
        jellyfin_address=_fake_jellyfin_address,
        config_dir=tmp_path,
        attempts=4,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("jellyfin",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:jellyfin"][-1].state == "error"
    assert frames["libraries:jellyfin"][-1].note == words.wiring_failure_refused("Jellyfin")
    gets = [
        call for call in server.calls if call[0] == "GET" and call[1] == "/Library/VirtualFolders"
    ]
    assert len(gets) == 1  # a 401 is a considered refusal, never retried


async def test_jellyfin_free_run_never_resolves_the_jellyfin_address_or_reads_jellyfin_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A jellyfin-free install must cost nothing extra: `jellyfin_address()`
    and the jellyfin.json loader must never even be CALLED, not merely
    "answer nothing" - the cost of the Jellyfin feature must be zero for
    every install that never added Jellyfin.
    """
    import marrquee.wiring.engine as engine_module

    address_calls: list[None] = []

    async def _recording_address() -> str | None:
        address_calls.append(None)
        return None

    load_calls: list[Path] = []

    def _recording_load(config_dir: Path) -> None:
        load_calls.append(config_dir)
        return None

    monkeypatch.setattr(engine_module, "load_jellyfin", _recording_load)

    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _ok([{"id": 1, "path": "/data/media/tv"}])
            ],
        }
    )
    engine = WiringEngine(client=fake, config_dir=tmp_path, jellyfin_address=_recording_address)
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    assert address_calls == []
    assert load_calls == []
    assert _frames_by_key(steps)["root-folder:sonarr"][-1].state == "done"


# --- plan_wiring: the existing-Plex libraries task --------------------------


def test_plan_wiring_appends_the_existing_plex_task_last_and_never_without_it() -> None:
    state = _install_state(("prowlarr", "sonarr", "plex", "jellyfin", "existing-plex"))

    keys = [task.key for task in plan_wiring(state)]

    assert keys == [
        "app-sync:sonarr",
        "root-folder:sonarr",
        "libraries:plex",
        "direct-play:plex",
        "libraries:jellyfin",
        "libraries:existing-plex",
    ]

    without = [task.key for task in plan_wiring(_install_state(("prowlarr", "sonarr")))]
    assert "libraries:existing-plex" not in without


def test_existing_plex_task_about_equals_involved() -> None:
    tasks = {task.key: task for task in plan_wiring(_install_state(("existing-plex",)))}

    task = tasks["libraries:existing-plex"]
    assert isinstance(task, ExistingPlexLibrariesTask)
    assert task.involved == ("existing-plex",)
    assert task.about == task.involved


def test_existing_plex_task_line_matches_content_direction() -> None:
    tasks = {task.key: task for task in plan_wiring(_install_state(("existing-plex",)))}

    assert tasks["libraries:existing-plex"].line == words.wiring_line_libraries("Plex")


# --- WiringEngine.run: resolving the owner's own Plex's saved record -------


def _existing_plex_settings(tmp_path: Path) -> Settings:
    (tmp_path / "volume1" / "media" / "data" / "media" / "movies").mkdir(parents=True)
    (tmp_path / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    return Settings(host_mount=tmp_path)


_EXISTING_BASE_URL = "http://192.168.1.20:32400"


def _existing_plex_record(**overrides: object) -> ExistingPlex:
    fields: dict[str, object] = {
        "machine_id": "m1",
        "name": "Den",
        "base_url": _EXISTING_BASE_URL,
        "port": 32400,
        "on_this_nas": False,
        "token": "owner-plex-token",
        "folders": {"movies": "unchecked", "tv": "unchecked"},
        "sections": {},
        "replaces_link": None,
    }
    fields.update(overrides)
    return ExistingPlex(**fields)  # type: ignore[arg-type]


def _existing_plex_no_libraries() -> PlexResponse:
    return PlexResponse(
        ok=True, status=200, payload={"MediaContainer": {"Directory": []}}, detail=None
    )


def _existing_plex_not_seen() -> PlexResponse:
    return PlexResponse(ok=True, status=200, payload={"MediaContainer": {"Path": []}}, detail=None)


async def test_missing_settings_reports_unreachable_for_existing_plex(tmp_path: Path) -> None:
    save_existing_plex(tmp_path, _existing_plex_record())
    engine = WiringEngine(
        client=FakeArrClient({}),
        plex=FakePlexServer(identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")}),
        config_dir=tmp_path,
        settings=None,
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("existing-plex",)), steps.append)

    frames = _frames_by_key(steps)
    last = frames["libraries:existing-plex"][-1]
    assert last.state == "error"
    assert last.note == words.wiring_failure_unreachable("Plex")
    # Pins the precheck's OWN technical text - not merely the same note an
    # uncaught exception's generic handler would also have produced.
    assert last.technical == "libraries:existing-plex: no plex server or settings"


async def test_missing_existing_plex_record_reports_it_lost(tmp_path: Path) -> None:
    engine = WiringEngine(
        client=FakeArrClient({}),
        plex=FakePlexServer(identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")}),
        config_dir=tmp_path,
        settings=_existing_plex_settings(tmp_path),
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("existing-plex",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:existing-plex"][-1].state == "error"
    assert frames["libraries:existing-plex"][-1].note == words.WIRING_EXISTING_PLEX_MISSING


async def test_existing_plex_task_wires_both_libraries_from_the_saved_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _existing_plex_settings(tmp_path)
    save_existing_plex(tmp_path, _existing_plex_record())
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    marker_name = "marrquee-plex-check-deadbeef"
    root = PurePosixPath(_ROOT)
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))

    def _seen(candidate: str) -> PlexResponse:
        return PlexResponse(
            ok=True,
            status=200,
            payload={
                "MediaContainer": {
                    "Path": [{"path": f"{candidate}/{marker_name}", "title": marker_name}]
                }
            },
            detail=None,
        )

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", "/library/sections"): [
                _existing_plex_no_libraries(),
                PlexResponse(
                    ok=True,
                    status=200,
                    payload={
                        "MediaContainer": {
                            "Directory": [
                                {
                                    "key": "1",
                                    "type": "movie",
                                    "title": words.EXISTING_PLEX_LIBRARY_MOVIES,
                                    "Location": [{"path": container_movies}],
                                },
                                {
                                    "key": "2",
                                    "type": "show",
                                    "title": words.EXISTING_PLEX_LIBRARY_TV,
                                    "Location": [{"path": container_tv}],
                                },
                            ]
                        }
                    },
                    detail=None,
                ),
            ],
            ("POST", "/library/sections"): [
                PlexResponse(ok=True, status=200, payload=None, detail=None),
                PlexResponse(ok=True, status=200, payload=None, detail=None),
            ],
            ("GET", browse_path(host_movies)): [_existing_plex_not_seen()],
            ("GET", browse_path(container_movies)): [_seen(container_movies)],
            ("GET", browse_path(host_tv)): [_existing_plex_not_seen()],
            ("GET", browse_path(container_tv)): [_seen(container_tv)],
        },
    )
    engine = WiringEngine(
        client=FakeArrClient({}), plex=server, config_dir=tmp_path, settings=settings
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("existing-plex",)), steps.append)

    frames = _frames_by_key(steps)
    assert frames["libraries:existing-plex"][-1].state == "done"
    assert frames["libraries:existing-plex"][-1].note == words.EXISTING_PLEX_NOTE_ADDED
    record = load_existing_plex(tmp_path)
    assert record is not None
    assert record.folders == {"movies": "added", "tv": "added"}
    assert record.sections == {"movies": "1", "tv": "2"}


async def test_existing_plex_free_run_never_reads_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cost of the existing-Plex feature must be zero for every install
    that never connected one - `load_existing_plex` must never even be
    CALLED, not merely "answer nothing".
    """
    import marrquee.wiring.engine as engine_module

    load_calls: list[Path] = []

    def _recording_load(config_dir: Path) -> ExistingPlex | None:
        load_calls.append(config_dir)
        return None

    monkeypatch.setattr(engine_module, "load_existing_plex", _recording_load)

    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/system/status"): [_ok(None)],
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _ok([{"id": 1, "path": "/data/media/tv"}])
            ],
        }
    )
    engine = WiringEngine(
        client=fake, config_dir=tmp_path, settings=_existing_plex_settings(tmp_path)
    )
    steps: list[WiringStep] = []

    await engine.run(_install_state(("sonarr",)), steps.append)

    assert load_calls == []
    assert _frames_by_key(steps)["root-folder:sonarr"][-1].state == "done"
