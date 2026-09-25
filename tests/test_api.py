"""Tests for the JSON/SSE surface Stories 4, 5 and 6 build their screens on.

Every test builds the app through `create_app`, injecting a `Settings` that
points at a throwaway directory, a `FakeDockerEngine`, and (where a test
needs to control timing precisely) a `DeployManager` built the same way
`tests/test_deploy.py` builds one. Nothing here touches a real Docker socket
or waits a real second.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
from test_deploy import _run_to_terminal, _StatefulEngine

import marrquee.questions as questions_module
from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.deploy import (
    AppProgress,
    DeployManager,
    DeploySnapshot,
    FakeReadinessProbe,
)
from marrquee.docker_client import (
    ComposeResult,
    ContainerSnapshot,
    DockerStatus,
    FakeDockerEngine,
)
from marrquee.main import create_app
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep, load_answers
from marrquee.routes.api import _event_stream
from marrquee.state import InstallState, load_state, save_state, write_json_atomic
from marrquee.storage import write_marker
from marrquee.wiring import NoWiringYet, WiringStep
from marrquee.wiring.engine import WiringEngine
from marrquee.words import (
    HUB_INSTALL_UNKNOWN,
    HUB_SETUP_DONE_REFUSAL,
    PHASE_HEADLINE_READY,
    REFUSAL_NOTHING_CHOSEN,
    STORAGE_CHECK_OK_MESSAGE,
    hub_install_busy,
)

# --- Shared fixtures and small builders --------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _fresh_root(settings: Settings) -> PurePosixPath:
    """Create an empty, never-deployed-to target and return its host path."""
    (settings.host_mount / "volume1" / "media").mkdir(parents=True)
    return PurePosixPath("/volume1/media")


def _install_state(app_ids: tuple[str, ...], root: PurePosixPath) -> InstallState:
    return InstallState(
        version=1,
        storage_root=str(root),
        app_ids=app_ids,
        api_keys={app_id: f"fake-{app_id}-api-key" for app_id in app_ids},
        puid=os.getuid(),
        pgid=os.getgid(),
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-19T00:00:00+00:00",
    )


def _running_container(app_id: str) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=app_id,
        exists=True,
        state="running",
        exit_code=None,
        image=get_app(app_id).image,
        detail=None,
    )


def _client(settings: Settings, manager: DeployManager) -> TestClient:
    app = create_app(
        settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True)), manager=manager
    )
    return TestClient(app)


def _idle_manager(settings: Settings) -> DeployManager:
    return DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))


# --- The catalog route --------------------------------------------------------


def test_catalog_route_lists_apps_in_deploy_order_with_port_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.get("/api/catalog")

    assert response.status_code == 200
    apps = response.json()["apps"]
    assert [app["id"] for app in apps] == ["prowlarr", "sonarr", "radarr"]
    assert apps[0].keys() == {"id", "name", "description", "port"}
    assert apps[0]["port"] == 9696


# --- The resting state: honest before any deploy has run ---------------------


def test_resting_state_is_honest_and_refuses_politely(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    get_response = client.get("/api/deploy")
    assert get_response.status_code == 200
    body = get_response.json()
    assert body["phase"] == "ready"
    assert body["apps"] == []
    assert body["headline"] == PHASE_HEADLINE_READY
    assert "failure" in body
    assert body["failure"] is None

    post_response = client.post("/api/deploy")
    assert post_response.status_code == 409
    assert post_response.json()["detail"] == REFUSAL_NOTHING_CHOSEN


# --- Installing --------------------------------------------------------------


def test_install_saves_state_generates_one_key_per_app_and_keeps_keys_on_repost(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    first = client.post("/api/install", json={"path": str(root), "app_ids": ["sonarr", "radarr"]})
    assert first.status_code == 200
    assert first.json() == {"saved": True}

    first_state = load_state(settings.config_dir)
    assert first_state is not None
    assert set(first_state.api_keys) == {"sonarr", "radarr"}
    first_keys = dict(first_state.api_keys)

    second = client.post("/api/install", json={"path": str(root), "app_ids": ["sonarr", "radarr"]})
    assert second.status_code == 200

    second_state = load_state(settings.config_dir)
    assert second_state is not None
    assert dict(second_state.api_keys) == first_keys


def test_install_refuses_a_populated_target_with_409_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    (settings.host_mount / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    (
        settings.host_mount / "volume1" / "media" / "data" / "media" / "tv" / "Old Show.mkv"
    ).write_text("")
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/install", json={"path": str(root), "app_ids": ["sonarr"]})

    assert response.status_code == 409
    assert response.json()["detail"]
    assert load_state(settings.config_dir) is None


def test_install_refuses_a_target_whose_media_folder_links_outside_with_409_not_500(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    media = settings.host_mount / "volume1" / "media" / "data" / "media"
    media.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (media / "tv").symlink_to(outside)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/install", json={"path": str(root), "app_ids": ["sonarr"]})

    assert response.status_code == 409
    assert response.json()["detail"]
    assert load_state(settings.config_dir) is None


def test_install_refuses_an_empty_app_list_with_400(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/install", json={"path": str(root), "app_ids": []})

    assert response.status_code == 400
    assert response.json()["detail"] == REFUSAL_NOTHING_CHOSEN


def test_install_refuses_an_unknown_app_id_with_400(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/install", json={"path": str(root), "app_ids": ["plex"]})

    assert response.status_code == 400


def test_install_refuses_once_the_hub_exists_and_changes_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/install", json={"path": str(root), "app_ids": ["sonarr"]})

    assert response.status_code == 409
    assert response.json()["detail"] == HUB_SETUP_DONE_REFUSAL
    assert load_state(settings.config_dir) is None


# --- Installing an app from the Hub's "+" panel --------------------------------


async def test_hub_install_endpoint_starts_an_add_and_refuses_a_second(tmp_path: Path) -> None:
    """FIRST TEST (Pre-Flight walk) - a real add, driven through the live
    app, refuses a second POST for the same app while the first is still
    going. Radarr's own readiness never answers, so the add stays parked
    mid-flight (or, once it eventually times out, still `adding`) instead
    of racing a fast fake add to "already_installed" before the second
    request lands.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr"), root))

    engine = _StatefulEngine(
        ("prowlarr", "sonarr"),
        images={get_app(app_id).image for app_id in ("prowlarr", "sonarr", "radarr")},
    )
    radarr = get_app("radarr")
    probe = FakeReadinessProbe(responses={(radarr.id, radarr.port): [False] * 1000}, default=True)
    manager = DeployManager(settings, engine, probe=probe)
    manager.start()
    await _run_to_terminal(manager)
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        first = client.post("/api/hub/apps/radarr/install", json={"answers": {}})
        assert first.status_code == 202
        assert first.json() == {"ok": True, "message": None, "step_id": None, "field": None}

        second = client.post("/api/hub/apps/radarr/install", json={"answers": {}})

    assert second.status_code == 409
    assert second.json()["message"] == hub_install_busy("Radarr")


def test_hub_install_endpoint_refuses_an_unknown_app_with_409(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/hub/apps/not-a-real-app/install", json={"answers": {}})

    assert response.status_code == 409
    assert response.json()["message"] == HUB_INSTALL_UNKNOWN


async def test_hub_install_fixture_step_refuses_with_400_and_saves_only_after_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="Fixture",
        lede="A fixture step.",
        fields=(QuestionField(name="name", label="Name", kind="text"),),
        check=lambda answers: QuestionCheck(
            ok=bool(answers.get("name")),
            answers=answers,
            problem=None if answers.get("name") else "Name is required.",
            field=None if answers.get("name") else "name",
        ),
    )
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (fixture_step,))

    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr"), root))
    engine = _StatefulEngine(
        ("prowlarr", "sonarr"),
        images={get_app(app_id).image for app_id in ("prowlarr", "sonarr", "radarr")},
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    manager.start()
    await _run_to_terminal(manager)
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        refused = client.post("/api/hub/apps/radarr/install", json={"answers": {"name": ""}})
        assert refused.status_code == 400
        body = refused.json()
        assert body["ok"] is False
        assert body["step_id"] == "fixture"
        assert body["field"] == "name"
        assert load_answers(settings.config_dir) == {}

        accepted = client.post(
            "/api/hub/apps/radarr/install", json={"answers": {"name": "Interesting"}}
        )
        assert accepted.status_code == 202

    assert load_answers(settings.config_dir)["radarr"]["name"] == "Interesting"


# --- The storage check --------------------------------------------------------


def test_storage_check_reports_ok_with_a_plain_language_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/storage/check", json={"path": str(root)})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == STORAGE_CHECK_OK_MESSAGE
    assert "detail" not in body


def test_storage_check_never_leaks_the_raw_os_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/storage/check", json={"path": "/definitely/missing"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "detail" not in body
    assert "Errno" not in json.dumps(body)


# --- Starting a deploy ---------------------------------------------------------


def test_deploy_start_returns_202_once_and_200_on_a_second_call_while_running(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("prowlarr").image},
        compose_results={"prowlarr": ComposeResult(ok=True, exit_code=0, output="")},
        self_container_id="marrquee",
    )
    # Never answers - keeps the run parked in the readiness loop so the
    # second POST below reliably lands on "already going".
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=False))
    client = _client(settings, manager)

    first = client.post("/api/deploy")
    assert first.status_code == 202

    # Give the manager's background task real wall-clock time to publish its
    # first "running" snapshot before the second request checks it.
    time.sleep(0.1)

    second = client.post("/api/deploy")
    assert second.status_code == 200
    assert second.json()["phase"] == "running"


def test_deploy_start_refuses_with_409_when_nothing_is_chosen_yet(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/deploy")

    assert response.status_code == 409
    assert response.json()["detail"] == REFUSAL_NOTHING_CHOSEN


# --- The event stream ----------------------------------------------------------


async def test_event_stream_sends_the_current_snapshot_as_its_first_event(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manager = _idle_manager(settings)

    stream = _event_stream(manager)
    try:
        first_chunk = await stream.__anext__()
    finally:
        await stream.aclose()

    assert first_chunk.startswith(b"data: ")
    payload = json.loads(first_chunk.removeprefix(b"data: ").decode())
    assert payload["phase"] == "ready"


async def test_event_stream_sends_a_heartbeat_comment_when_nothing_changes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    manager = _idle_manager(settings)

    stream = _event_stream(manager, heartbeat_seconds=0.01)
    try:
        await stream.__anext__()  # the current snapshot
        second = await stream.__anext__()  # nothing changed - a heartbeat instead
    finally:
        await stream.aclose()

    assert second == b": keep-alive\n\n"


async def test_event_stream_unsubscribes_once_the_reader_stops(tmp_path: Path) -> None:
    """A client that disconnects must be unsubscribed, or the manager keeps
    trying to feed a queue nobody is ever going to read again.

    `DeployManager` has no public way to ask "how many subscribers do you
    have right now" - reaching into `._subscribers` here is a deliberate,
    narrow exception to prove this one structural promise.
    """
    settings = _settings(tmp_path)
    manager = _idle_manager(settings)

    stream = _event_stream(manager)
    await stream.__anext__()
    assert len(manager._subscribers) == 1

    await stream.aclose()

    assert len(manager._subscribers) == 0


# --- Diagnostics ----------------------------------------------------------------


def test_diagnostics_returns_204_when_nothing_has_failed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.get("/api/deploy/diagnostics")

    assert response.status_code == 204
    assert response.content == b""


def test_diagnostics_returns_the_failure_text_as_plain_text(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "last-failure.txt").write_text(
        "never_became_ready\nsonarr never answered\n"
    )
    client = _client(settings, _idle_manager(settings))

    response = client.get("/api/deploy/diagnostics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "never_became_ready" in response.text


# --- No technical string reaches anything but diagnostics ----------------------

_SUSPICIOUS_MARKERS = ("Traceback", "sha256:", "exit code", '.py", line')


async def test_no_response_body_contains_technical_markers_except_diagnostics(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("sonarr",), root)
    save_state(settings.config_dir, install)

    technical_output = (
        "Traceback (most recent call last):\n"
        'File "compose.py", line 42\n'
        "sha256:deadbeef exit code 137"
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("sonarr").image},
        compose_results={"sonarr": ComposeResult(ok=False, exit_code=137, output=technical_output)},
        self_container_id="marrquee",
    )
    manager = DeployManager(settings, engine)
    client = _client(settings, manager)

    manager.start()
    for _ in range(10_000):
        if manager.snapshot().phase == "error":
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("deploy never reached error")

    deploy_body = client.get("/api/deploy").text
    for marker in _SUSPICIOUS_MARKERS:
        assert marker not in deploy_body, f"{marker!r} leaked into GET /api/deploy"

    stream = _event_stream(manager)
    try:
        event = await stream.__anext__()
    finally:
        await stream.aclose()
    for marker in _SUSPICIOUS_MARKERS:
        assert marker.encode() not in event, f"{marker!r} leaked into the event stream"

    diagnostics_text = client.get("/api/deploy/diagnostics").text
    assert "Traceback" in diagnostics_text  # captured, just not leaked elsewhere


# --- Input validation: never a 500, never a bare traceback ----------------------


def test_state_changing_routes_are_post_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    assert client.get("/api/install").status_code == 405
    assert client.get("/api/storage/check").status_code == 405


def test_oversized_path_returns_a_4xx_not_a_500(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    huge_path = "/" + ("a" * 5000)
    response = client.post("/api/storage/check", json={"path": huge_path})

    assert 400 <= response.status_code < 500


def test_non_string_path_returns_a_4xx_not_a_500(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/storage/check", json={"path": 12345})

    assert 400 <= response.status_code < 500


def test_malformed_body_returns_a_4xx_not_a_500(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/storage/check", content=b"not json at all", headers={"Content-Type": "text/plain"}
    )

    assert 400 <= response.status_code < 500


def test_too_many_app_ids_returns_a_4xx_not_a_500(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/install", json={"path": str(root), "app_ids": ["sonarr"] * 51})

    assert 400 <= response.status_code < 500


# --- No CORS: same-origin only, per the owner's LAN-only decision ---------------


def test_no_cors_headers_are_ever_added(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.get("/api/deploy", headers={"Origin": "http://example.com"})

    assert "access-control-allow-origin" not in response.headers


# --- Resuming an interrupted deploy on startup -----------------------------------


def test_the_app_resumes_an_interrupted_deploy_on_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr",), root)
    save_state(settings.config_dir, install)

    stale = DeploySnapshot(
        run_id="stale-run-from-before-a-restart",
        phase="running",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="starting",
                chip="Starting…",
                line="Starting Prowlarr",
                note=None,
                port=9696,
            ),
        ),
        headline="Starting your apps, one at a time.",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at=None,
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(stale))
    # Our own marker, as if a first attempt got far enough to write it before
    # the process stopped - without it, the resumed run's name-clash check
    # would (correctly) refuse to adopt a "prowlarr" it doesn't recognise.
    write_marker(settings, root, ("prowlarr",), install.puid, install.pgid)

    # A daemon that already has this container running - the resumed run's
    # compose_up (--no-recreate) and readiness check both see it as done.
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("prowlarr").image},
        containers={"prowlarr": _running_container("prowlarr")},
        compose_results={"prowlarr": ComposeResult(ok=True, exit_code=0, output="")},
        self_container_id="marrquee",
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    app = create_app(settings=settings, engine=engine, manager=manager)

    with TestClient(app) as client:
        # `with` triggers the lifespan startup hook, which awaits
        # `resume_if_interrupted()` fully before this block's body runs.
        final_phase = None
        for _ in range(200):
            final_phase = client.get("/api/deploy").json()["phase"]
            if final_phase == "finale":
                break
            time.sleep(0.01)

        assert final_phase == "finale"


def test_create_app_keeps_working_with_only_settings_and_engine_supplied(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    app = create_app(settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True)))
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/deploy").status_code == 200


# --- The live app wires for real; a bare DeployManager does not ---------------


def test_create_app_wires_for_real_but_a_bare_deploy_manager_does_not(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    app = create_app(settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True)))

    # No deploy is ever started here, so this never touches the network -
    # it only proves which runner `create_app` wired in.
    assert isinstance(app.state.deploy._wiring, WiringEngine)
    assert isinstance(_idle_manager(settings)._wiring, NoWiringYet)


class _PausingWiringRunner:
    """Emits one running frame, waits briefly (real time, so a client
    polling `GET /api/deploy` over the wire can observe it), then finishes.
    """

    async def run(self, state: InstallState, emit: object, *, only_app: str | None = None) -> None:
        step = WiringStep(
            index=1,
            total=1,
            key="app-sync:sonarr",
            line="Introducing Prowlarr to Sonarr",
            state="running",
            chip="Connecting…",
            note=None,
            technical=None,
        )
        emit(step)  # type: ignore[operator]
        await asyncio.sleep(0.2)
        emit(  # type: ignore[operator]
            dataclasses.replace(
                step,
                state="done",
                chip="Connected",
                note="Already connected - nothing to change.",
            )
        )


def test_get_deploy_mid_wiring_shows_the_rows_so_far(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    install = _install_state(("prowlarr", "sonarr"), root)
    save_state(settings.config_dir, install)
    # Marks both apps as already ours, so the name-clash check (which would
    # otherwise see the pre-seeded "already running" containers below as
    # somebody else's) skips straight past them.
    write_marker(settings, root, ("prowlarr", "sonarr"), install.puid, install.pgid)

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={
            "prowlarr": _running_container("prowlarr"),
            "sonarr": _running_container("sonarr"),
        },
        images={get_app("prowlarr").image, get_app("sonarr").image},
        network_exists=True,
        self_container_id="marrquee",
    )
    manager = DeployManager(
        settings, engine, probe=FakeReadinessProbe(default=True), wiring=_PausingWiringRunner()
    )
    app = create_app(
        settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True)), manager=manager
    )

    # `with` keeps one portal (one event loop) alive across every request in
    # this block - a plain `TestClient(app)` spins up a fresh one per call,
    # which would orphan the manager's background task between polls instead
    # of letting it keep progressing on real wall-clock time.
    with TestClient(app) as client:
        client.post("/api/deploy")

        seen_running_row = False
        final_phase = None
        for _ in range(500):
            body = client.get("/api/deploy").json()
            final_phase = body["phase"]
            if final_phase == "wiring" and body["wiring"]:
                seen_running_row = True
                assert body["wiring"][0]["state"] == "running"
            if final_phase == "finale":
                break
            time.sleep(0.01)

    assert seen_running_row
    assert final_phase == "finale"
    assert final_phase == "finale"
