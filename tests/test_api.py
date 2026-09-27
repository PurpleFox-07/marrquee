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
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
from test_deploy import _run_to_terminal, _StatefulEngine

import marrquee.questions as questions_module
from marrquee.catalog import get_app, require_port
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
from marrquee.graphics_chip import GRAPHICS_DEVICE_NODE
from marrquee.hardlinks import HardlinkMonitor, HardlinkResult, save_hardlink_result
from marrquee.login import load_login, save_login
from marrquee.main import create_app
from marrquee.plex import (
    ExistingPlex,
    FakePlexServer,
    FakePlexTv,
    PlexIdentity,
    PlexServer,
    save_existing_plex,
)
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep, load_answers
from marrquee.recyclarr import RecyclarrControl, SyncRecord, SyncStatus
from marrquee.routes.api import _event_stream
from marrquee.state import InstallState, load_state, save_state, write_json_atomic
from marrquee.storage import write_marker
from marrquee.vpn import TunnelPlace
from marrquee.vpn_control import FakeGluetunControl
from marrquee.wiring import NoWiringYet, WiringStep
from marrquee.wiring.engine import WiringEngine
from marrquee.wiring.qbit_client import FakeQbitClient
from marrquee.words import (
    HUB_DRIVE_NOTE_COPIES,
    HUB_INSTALL_LOGIN_FIRST,
    HUB_INSTALL_UNKNOWN,
    HUB_SETUP_DONE_REFUSAL,
    LOGIN_PROBLEM_PASSWORD_SHORT,
    PHASE_HEADLINE_READY,
    REFUSAL_NO_LOGIN,
    REFUSAL_NOTHING_CHOSEN,
    STORAGE_CHECK_OK_MESSAGE,
    hub_install_busy,
    recyclarr_line_app_down,
)

# --- Shared fixtures and small builders --------------------------------------


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")
    _seed_fresh_drive_check(settings)
    return settings


def _seed_fresh_drive_check(settings: Settings) -> None:
    """A drive-check result saved just now, so `create_app`'s own default
    `HardlinkMonitor` never starts a real filesystem probe in the
    background for a test that has no opinion about the drive check -
    `refresh_if_due` only starts one for a missing or day-old result. A
    test that DOES care injects its own `hardlinks=` with a saved result
    and clock it controls, which simply overwrites this one.
    """
    save_hardlink_result(
        settings.config_dir,
        HardlinkResult(
            outcome="works",
            reason=None,
            folder=None,
            technical=None,
            checked_at=datetime.now(UTC).isoformat(),
        ),
    )


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


def _client(
    settings: Settings,
    manager: DeployManager,
    *,
    engine: FakeDockerEngine | None = None,
    vpn_control: FakeGluetunControl | None = None,
    hardlinks: HardlinkMonitor | None = None,
    recyclarr: RecyclarrControl | None = None,
    plex_server: PlexServer | None = None,
) -> TestClient:
    app = create_app(
        settings=settings,
        engine=engine if engine is not None else FakeDockerEngine(DockerStatus(connected=True)),
        manager=manager,
        vpn_control=vpn_control,
        hardlinks=hardlinks,
        recyclarr=recyclarr,
        plex_server=plex_server,
    )
    return TestClient(app)


class _RecordingRecyclarr:
    """Stands in for `RecyclarrMonitor` on a route test: a fixed status to
    hand back, and a count of how many times a sync was ever requested.
    """

    def __init__(self, status: SyncStatus) -> None:
        self._status = status
        self.sync_calls = 0

    def request_sync(self) -> object:
        self.sync_calls += 1
        return None

    async def status(self) -> SyncStatus:
        return self._status


def _idle_manager(settings: Settings) -> DeployManager:
    return DeployManager(settings, FakeDockerEngine(DockerStatus(connected=True)))


# The one login every `/api/install` body below carries - a fixed, valid
# credential, since none of these tests are about the login rules
# themselves (those live in `tests/test_login.py`).
_LOGIN_BODY = {"username": "install-owner", "password": "s3cret-password-1"}


# --- The catalog route --------------------------------------------------------


def test_catalog_route_lists_apps_in_deploy_order_with_port_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.get("/api/catalog")

    assert response.status_code == 200
    apps = response.json()["apps"]
    assert [app["id"] for app in apps] == [
        "prowlarr",
        "sonarr",
        "radarr",
        "qbittorrent",
        "recyclarr",
        "plex",
        "jellyfin",
        "seerr",
    ]
    assert apps[0].keys() == {"id", "name", "description", "port"}
    assert apps[0]["port"] == 9696
    apps_by_id = {app["id"]: app for app in apps}
    assert apps_by_id["recyclarr"]["port"] is None
    assert apps_by_id["plex"]["port"] == 32400
    assert apps_by_id["jellyfin"]["port"] == 8096
    assert apps_by_id["seerr"]["port"] == 5055
    # existing-plex is `offered=True` but `managed=False`: it is connected
    # from the Hub only, never listed as a catalog choice.
    assert "existing-plex" not in apps_by_id


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


def test_deploy_without_a_saved_login_answers_409_and_starts_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",), PurePosixPath("/volume1/media")))
    manager = _idle_manager(settings)
    client = _client(settings, manager)
    assert load_login(settings.config_dir).login is None

    response = client.post("/api/deploy")

    assert response.status_code == 409
    assert response.json()["detail"] == REFUSAL_NO_LOGIN
    assert manager.snapshot().phase == "ready"


# --- Installing --------------------------------------------------------------


def test_install_saves_state_generates_one_key_per_app_and_keeps_keys_on_repost(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    first = client.post(
        "/api/install",
        json={"path": str(root), "app_ids": ["sonarr", "radarr"], "login": _LOGIN_BODY},
    )
    assert first.status_code == 200
    assert first.json() == {"saved": True}

    first_state = load_state(settings.config_dir)
    assert first_state is not None
    assert set(first_state.api_keys) == {"sonarr", "radarr"}
    first_keys = dict(first_state.api_keys)

    second = client.post(
        "/api/install",
        json={"path": str(root), "app_ids": ["sonarr", "radarr"], "login": _LOGIN_BODY},
    )
    assert second.status_code == 200

    second_state = load_state(settings.config_dir)
    assert second_state is not None
    assert dict(second_state.api_keys) == first_keys


def test_install_saves_the_posted_login_lowered_and_generation_one(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install",
        json={
            "path": str(root),
            "app_ids": ["sonarr"],
            "login": {"username": "Install-Owner", "password": "s3cret-password-1"},
        },
    )

    assert response.status_code == 200
    record = load_login(settings.config_dir)
    assert record.login is not None
    assert record.login.username == "install-owner"
    assert record.login.generation == 1


def test_install_refuses_a_bad_login_with_400_and_never_leaks_the_password(
    tmp_path: Path,
) -> None:
    """`LOGIN_STEP.check` owns the 8-128 rule, not pydantic - a refusal is
    always the plain sentence that rule already owns, and the password
    itself never reaches the response body, the state file or login.json.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install",
        json={
            "path": str(root),
            "app_ids": ["sonarr"],
            "login": {"username": "owner", "password": "abc12"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == LOGIN_PROBLEM_PASSWORD_SHORT
    assert "abc12" not in response.text
    assert load_state(settings.config_dir) is None
    assert load_login(settings.config_dir).login is None


def test_install_refuses_with_a_valid_login_but_a_bad_install_saves_neither(
    tmp_path: Path,
) -> None:
    """The login is checked before `install_apps` runs, but only SAVED once
    that install actually succeeds - a refused install (here, a populated
    target) must never leave a login saved with no storage choice to go
    with it.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    (settings.host_mount / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    (
        settings.host_mount / "volume1" / "media" / "data" / "media" / "tv" / "Old Show.mkv"
    ).write_text("")
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install", json={"path": str(root), "app_ids": ["sonarr"], "login": _LOGIN_BODY}
    )

    assert response.status_code == 409
    assert load_state(settings.config_dir) is None
    assert load_login(settings.config_dir).login is None


def test_install_refuses_a_populated_target_with_409_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    (settings.host_mount / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    (
        settings.host_mount / "volume1" / "media" / "data" / "media" / "tv" / "Old Show.mkv"
    ).write_text("")
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install", json={"path": str(root), "app_ids": ["sonarr"], "login": _LOGIN_BODY}
    )

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

    response = client.post(
        "/api/install", json={"path": str(root), "app_ids": ["sonarr"], "login": _LOGIN_BODY}
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    assert load_state(settings.config_dir) is None


def test_install_refuses_an_empty_app_list_with_400(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install", json={"path": str(root), "app_ids": [], "login": _LOGIN_BODY}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == REFUSAL_NOTHING_CHOSEN


def test_install_refuses_an_unknown_app_id_with_400(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install",
        json={"path": str(root), "app_ids": ["not-a-real-app"], "login": _LOGIN_BODY},
    )

    assert response.status_code == 400


def test_install_refuses_gluetun_in_the_app_list_with_400(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    client = _client(settings, _idle_manager(settings))

    response = client.post(
        "/api/install",
        json={"path": str(root), "app_ids": ["radarr", "gluetun"], "login": _LOGIN_BODY},
    )

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

    response = client.post(
        "/api/install", json={"path": str(root), "app_ids": ["sonarr"], "login": _LOGIN_BODY}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == HUB_SETUP_DONE_REFUSAL
    assert load_state(settings.config_dir) is None


# --- /api/hub/status: kind per tile, and the VPN's own tunnel signal ----------


def test_hub_status_carries_kind_per_tile_and_the_vpn_tunnel_signal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("sonarr", "gluetun"), root))
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="sonarr",
                name="Sonarr",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8989,
            ),
            AppProgress(
                app_id="gluetun",
                name="VPN",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8000,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={
            "sonarr": _running_container("sonarr"),
            "gluetun": ContainerSnapshot(
                name="gluetun",
                exists=True,
                state="running",
                exit_code=None,
                image=get_app("gluetun").image,
                detail=None,
                health="healthy",
            ),
        },
    )
    place = TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )
    vpn_control = FakeGluetunControl(place=place)
    client = _client(settings, _idle_manager(settings), engine=engine, vpn_control=vpn_control)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    payload = response.json()
    kinds = {app["app_id"]: app["kind"] for app in payload["apps"]}
    assert kinds == {"sonarr": "arr", "gluetun": "vpn"}
    assert payload["vpn_tunnel"] == "up"


def test_hub_status_carries_managed_false_only_for_the_existing_plex_tile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("sonarr", "existing-plex"), root))
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="sonarr",
                name="Sonarr",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8989,
            ),
            AppProgress(
                app_id="existing-plex",
                name="Plex",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=None,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))
    record = ExistingPlex(
        machine_id="m1",
        name="Den",
        base_url="http://192.168.1.20:32400",
        port=32400,
        on_this_nas=False,
        token="tok-super-secret-999",
        folders={"movies": "added", "tv": "added"},
        sections={"movies": "5", "tv": "6"},
        replaces_link=None,
    )
    save_existing_plex(settings.config_dir, record)
    engine = FakeDockerEngine(
        DockerStatus(connected=True), containers={"sonarr": _running_container("sonarr")}
    )
    plex_server = FakePlexServer(identities_by_url={record.base_url: PlexIdentity(True, "m1")})
    manager = DeployManager(settings, engine, plex_server=plex_server)
    client = _client(settings, manager, engine=engine, plex_server=plex_server)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    by_id = {app["app_id"]: app for app in response.json()["apps"]}
    assert by_id["sonarr"]["managed"] is True
    assert by_id["existing-plex"]["managed"] is False
    assert by_id["existing-plex"]["state"] == "up"
    assert ("inspect", ("existing-plex",)) not in engine.calls


def test_hub_status_vpn_tunnel_is_none_with_no_vpn_installed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    assert response.json()["vpn_tunnel"] is None


def test_hub_status_carries_sync_state_and_the_app_down_reason(tmp_path: Path) -> None:
    """Recyclarr's own `sync_state` and `actions` reach the JSON poll the
    same way `hub_view` computes them - the app-down reason named because
    Radarr's own health, not Recyclarr's log wording, says it's down.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr", "recyclarr"), root))
    write_json_atomic(
        settings.config_dir / "deploy.json",
        dataclasses.asdict(
            DeploySnapshot(
                run_id="run-1",
                phase="finale",
                apps=(
                    AppProgress(
                        app_id="sonarr",
                        name="Sonarr",
                        state="done",
                        chip="chip",
                        line="Ready",
                        note=None,
                        port=8989,
                    ),
                    AppProgress(
                        app_id="radarr",
                        name="Radarr",
                        state="done",
                        chip="chip",
                        line="Ready",
                        note=None,
                        port=7878,
                    ),
                    AppProgress(
                        app_id="recyclarr",
                        name="Recyclarr",
                        state="done",
                        chip="chip",
                        line="Ready",
                        note=None,
                        port=None,
                    ),
                ),
                headline="Now showing",
                detail=None,
                failure=None,
                started_at="2026-09-19T00:00:00+00:00",
                finished_at="2026-09-19T00:05:00+00:00",
                wiring=(),
            )
        ),
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={
            "sonarr": _running_container("sonarr"),
            "radarr": ContainerSnapshot(
                name="radarr",
                exists=True,
                state="exited",
                exit_code=1,
                image=get_app("radarr").image,
                detail=None,
            ),
            "recyclarr": _running_container("recyclarr"),
        },
    )
    recyclarr = _RecordingRecyclarr(
        SyncStatus(
            syncing=False,
            last=SyncRecord(finished_at=datetime.now(UTC), ok=False),
            start_failed=False,
            run_failed=False,
        )
    )
    client = _client(settings, _idle_manager(settings), engine=engine, recyclarr=recyclarr)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    by_id = {app["app_id"]: app for app in response.json()["apps"]}
    assert by_id["recyclarr"]["sync_state"] == "failed"
    assert by_id["recyclarr"]["actions"] == "sync"
    assert by_id["recyclarr"]["line"] == recyclarr_line_app_down("Radarr")


def test_hub_status_carries_the_drive_note(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_hardlink_result(
        settings.config_dir,
        HardlinkResult(
            outcome="copies",
            reason="different_drives",
            folder="/volume1/media/data/media/tv",
            technical=None,
            checked_at=datetime.now(UTC).isoformat(),
        ),
    )
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, _idle_manager(settings), hardlinks=monitor)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    assert response.json()["drive_note"] == HUB_DRIVE_NOTE_COPIES


def test_hub_status_carries_paused_and_can_change_seeding_for_the_downloader(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent"), root))
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="gluetun",
                name="VPN",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8000,
            ),
            AppProgress(
                app_id="qbittorrent",
                name="qBittorrent",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8080,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={
            "qbittorrent": _running_container("qbittorrent"),
            "gluetun": ContainerSnapshot(
                name="gluetun",
                exists=True,
                state="running",
                exit_code=None,
                image=get_app("gluetun").image,
                detail=None,
                health="unhealthy",
            ),
        },
    )
    client = _client(settings, _idle_manager(settings), engine=engine)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    by_id = {app["app_id"]: app for app in response.json()["apps"]}
    assert by_id["qbittorrent"]["paused"] is True
    assert by_id["qbittorrent"]["can_change_seeding"] is True
    assert by_id["gluetun"]["paused"] is False


def test_hub_status_carries_running_without_vpn_and_can_change_vpn(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent"), root))
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="gluetun",
                name="VPN",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8000,
            ),
            AppProgress(
                app_id="qbittorrent",
                name="qBittorrent",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8080,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={
            "qbittorrent": _running_container("qbittorrent"),
            "gluetun": ContainerSnapshot(
                name="gluetun",
                exists=True,
                state="running",
                exit_code=None,
                image=get_app("gluetun").image,
                detail=None,
                health="healthy",
            ),
        },
    )
    client = _client(settings, _idle_manager(settings), engine=engine)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    payload = response.json()
    by_id = {app["app_id"]: app for app in payload["apps"]}
    assert payload["running_without_vpn"] is False
    assert by_id["gluetun"]["can_change_vpn"] is True
    assert by_id["gluetun"]["actions"] in (
        "none",
        "retry",
        "reconnect",
        "try_again",
        "retry_or_restore",
    )


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
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)

    engine = _StatefulEngine(
        ("prowlarr", "sonarr"),
        images={get_app(app_id).image for app_id in ("prowlarr", "sonarr", "radarr")},
    )
    radarr = get_app("radarr")
    probe = FakeReadinessProbe(
        responses={(radarr.id, require_port(radarr)): [False] * 1000}, default=True
    )
    manager = DeployManager(settings, engine, probe=probe)
    manager.start()
    await _run_to_terminal(manager)
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        first = client.post("/api/hub/apps/radarr/install", json={"answers": {}})
        assert first.status_code == 202
        assert first.json() == {
            "ok": True,
            "message": None,
            "step_id": None,
            "step_app_id": None,
            "field": None,
        }

        second = client.post("/api/hub/apps/radarr/install", json={"answers": {}})

    assert second.status_code == 409
    assert second.json()["message"] == hub_install_busy("Radarr")


def test_hub_install_endpoint_refuses_an_unknown_app_with_409(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings, _idle_manager(settings))

    response = client.post("/api/hub/apps/not-a-real-app/install", json={"answers": {}})

    assert response.status_code == 409
    assert response.json()["message"] == HUB_INSTALL_UNKNOWN


async def test_hub_install_endpoint_refuses_with_409_when_no_login_is_saved(
    tmp_path: Path,
) -> None:
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
        response = client.post("/api/hub/apps/radarr/install", json={"answers": {}})

    assert response.status_code == 409
    assert response.json()["message"] == HUB_INSTALL_LOGIN_FIRST


async def test_hub_install_jellyfin_refuses_a_missing_graphics_answer_when_a_chip_exists(
    tmp_path: Path,
) -> None:
    """The install endpoint applies the exact same graphics-chip predicate
    the Hub page itself renders from - a POST can never skip a question the
    GET would have shown.

    The finale snapshot is written directly (as
    `test_hub_install_sonarr_with_recyclarr_installed_asks_tv_quality` does)
    rather than run through a real bring-up - this test has no opinion
    about Prowlarr or Sonarr's own readiness, only about Jellyfin's step.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr"), root))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=9696,
            ),
            AppProgress(
                app_id="sonarr",
                name="Sonarr",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=8989,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))

    engine = FakeDockerEngine(DockerStatus(connected=True), host_paths={GRAPHICS_DEVICE_NODE})
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        refused = client.post("/api/hub/apps/jellyfin/install", json={"answers": {}})

    assert refused.status_code == 400
    body = refused.json()
    assert body["ok"] is False
    assert body["step_id"] == "graphics"
    assert body["step_app_id"] == "jellyfin"
    assert body["field"] == "graphics_chip"


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
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
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


async def test_hub_install_qbittorrent_refuses_the_vpn_step_and_names_gluetun(
    tmp_path: Path,
) -> None:
    """qBittorrent's own install form carries Gluetun's VPN step too - a
    refusal on that step must name GLUETUN as the owning app
    (`step_app_id`), never qBittorrent, so the browser's own
    `data-question-step="gluetun:vpn"` fieldset is the one that lights up.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr"), root))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = _StatefulEngine(
        ("prowlarr", "sonarr"),
        images={
            get_app(app_id).image
            for app_id in ("prowlarr", "sonarr", "radarr", "gluetun", "qbittorrent")
        },
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    manager.start()
    await _run_to_terminal(manager)
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        refused = client.post("/api/hub/apps/qbittorrent/install", json={"answers": {}})

    assert refused.status_code == 400
    body = refused.json()
    assert body["ok"] is False
    assert body["step_id"] == "vpn"
    assert body["step_app_id"] == "gluetun"
    assert load_answers(settings.config_dir) == {}


async def test_hub_install_recyclarr_saves_tv_quality_under_sonarr(tmp_path: Path) -> None:
    """Recyclarr's own quality question is owned by Sonarr
    (`step.app_id == "sonarr"`), so the endpoint's existing
    `save_step_answers(..., step.app_id, ...)` lands the answer under
    `sonarr`, never under `recyclarr` - the app whose add actually
    triggered the question.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr"), root))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = _StatefulEngine(
        ("prowlarr", "sonarr"),
        images={get_app(app_id).image for app_id in ("prowlarr", "sonarr", "recyclarr")},
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    manager.start()
    await _run_to_terminal(manager)
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        response = client.post(
            "/api/hub/apps/recyclarr/install", json={"answers": {"tv_quality": "4k"}}
        )

    assert response.status_code == 202
    saved = load_answers(settings.config_dir)
    assert saved["sonarr"]["tv_quality"] == "4k"
    assert "recyclarr" not in saved


def test_hub_install_sonarr_with_recyclarr_installed_asks_tv_quality(tmp_path: Path) -> None:
    """Adding Sonarr after Recyclarr already exists asks the same question
    in the other direction, and still saves it under Sonarr.

    The finale snapshot is written directly (as
    `test_hub_status_carries_kind_per_tile_and_the_vpn_tunnel_signal` does)
    rather than run through a real bring-up - Recyclarr's own compose
    branch and readiness rule don't exist until a later chunk, and this
    test has no opinion about either.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr", "recyclarr"), root))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    finished = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=9696,
            ),
            AppProgress(
                app_id="recyclarr",
                name="Recyclarr",
                state="done",
                chip="chip",
                line="Ready",
                note=None,
                port=None,
            ),
        ),
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))

    engine = _StatefulEngine(
        ("prowlarr", "recyclarr"),
        images={get_app(app_id).image for app_id in ("prowlarr", "sonarr", "recyclarr")},
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    assert manager.snapshot().phase == "finale"

    app = create_app(settings=settings, engine=engine, manager=manager)
    with TestClient(app) as client:
        refused = client.post("/api/hub/apps/sonarr/install", json={"answers": {}})
        assert refused.status_code == 400
        body = refused.json()
        assert body["step_id"] == "quality"
        assert body["step_app_id"] == "sonarr"

        accepted = client.post(
            "/api/hub/apps/sonarr/install", json={"answers": {"tv_quality": "1080p"}}
        )
        assert accepted.status_code == 202

    saved = load_answers(settings.config_dir)
    assert saved["sonarr"]["tv_quality"] == "1080p"


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
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)

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

    response = client.post(
        "/api/install",
        json={"path": str(root), "app_ids": ["sonarr"] * 51, "login": _LOGIN_BODY},
    )

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


def test_create_app_builds_one_qbit_client_and_shares_it(tmp_path: Path) -> None:
    """`create_app` builds exactly ONE qBittorrent door and hands the SAME
    instance to both the deploy manager and its login applier - two
    separate clients would still work, but a caller-supplied fake (a test,
    or a future dev tool) would only ever reach one of them.
    """
    settings = _settings(tmp_path)
    fake = FakeQbitClient({})

    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True)),
        qbit_client=fake,
    )

    assert app.state.deploy._qbit is fake
    assert app.state.deploy._login._qbit is fake
    assert app.state.qbit_client is fake


def test_create_app_builds_one_plex_door_pair_and_shares_it(tmp_path: Path) -> None:
    """`create_app` builds exactly one plex.tv door and one local-server
    door, and hands the SAME pair to the deploy manager AND `app.state` -
    the sign-in routes (a later chunk) and the manager must never be able
    to see two different fakes for the same running process.
    """
    settings = _settings(tmp_path)
    fake_tv = FakePlexTv()
    fake_server = FakePlexServer()

    app = create_app(
        settings=settings,
        engine=FakeDockerEngine(DockerStatus(connected=True)),
        plex_tv=fake_tv,
        plex_server=fake_server,
    )

    assert app.state.deploy._plex_tv is fake_tv
    assert app.state.deploy._plex_server is fake_server
    assert app.state.plex_tv is fake_tv
    assert app.state.plex_server is fake_server


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
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
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
