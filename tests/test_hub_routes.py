"""Tests for the Hub's server-rendered shell: `GET /` once a deploy has
reached `finale`, its 303 redirects otherwise, and the two front-door tests
that used to live in `test_wizard_drive.py`.

Every fixture writes the real files `DeployManager` and `load_state` read
back (`install.json`, `deploy.json`), through the same `write_json_atomic`
helper `test_deploy_page.py` uses - the same mechanism a NAS restart relies
on - so a fresh `create_app` over a `tmp_path` config folder is enough; no
deploy ever runs inside these tests. HTML structure (which poster carries
which state, the root's own attributes) is read with the stdlib
`html.parser`, the same approach `test_deploy_page.py` uses.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from marrquee import words
from marrquee.catalog import apps_in_order
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot, Failure
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.routes.wizard import router as wizard_router
from marrquee.state import STATE_VERSION, InstallState, save_state, write_json_atomic
from marrquee.words import STATUS_CHIP_DONE

# --- Shared fixtures and small builders --------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _install_state(app_ids: tuple[str, ...]) -> InstallState:
    return InstallState(
        version=STATE_VERSION,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys={app_id: f"fake-{app_id}-api-key" for app_id in app_ids},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-23T00:00:00+00:00",
    )


def _progress(app_id: str, name: str, port: int) -> AppProgress:
    return AppProgress(
        app_id=app_id,
        name=name,
        state="done",
        chip=STATUS_CHIP_DONE,
        line="Ready",
        note=None,
        port=port,
    )


def _finale_snapshot(app_ids: tuple[str, ...]) -> DeploySnapshot:
    apps = tuple(_progress(app.id, app.name, app.port) for app in apps_in_order(app_ids))
    return DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=apps,
        headline="Now showing: your media server",
        detail=None,
        failure=None,
        started_at="2026-09-23T00:00:00+00:00",
        finished_at="2026-09-23T00:05:00+00:00",
        wiring=(),
    )


def _write_snapshot(settings: Settings, snapshot: DeploySnapshot) -> None:
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


def _client(settings: Settings, engine: FakeDockerEngine | None = None) -> TestClient:
    if engine is None:
        engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine)
    return TestClient(app)


def _running_containers(app_ids: tuple[str, ...]) -> dict[str, ContainerSnapshot]:
    return {
        app_id: ContainerSnapshot(
            name=app_id, exists=True, state="running", exit_code=None, image=None, detail=None
        )
        for app_id in app_ids
    }


class _PosterCollector(HTMLParser):
    """Collects every poster `<a data-app="...">`'s own attributes, keyed by
    app id - a Hub tile's `data-app` lives on the link itself, not on the
    `<li>` around it, unlike the Deploy screen's tiles.
    """

    def __init__(self) -> None:
        super().__init__()
        self.posters: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        app_id = attrs_dict.get("data-app")
        if tag == "a" and app_id is not None:
            self.posters[app_id] = attrs_dict


def _posters(page_html: str) -> dict[str, dict[str, str | None]]:
    collector = _PosterCollector()
    collector.feed(page_html)
    return collector.posters


class _RootCollector(HTMLParser):
    """Collects the page's own root `<main data-hub ...>` attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main" and not self.attrs:
            self.attrs = dict(attrs)


def _root(page_html: str) -> dict[str, str | None]:
    collector = _RootCollector()
    collector.feed(page_html)
    return collector.attrs


# --- GET /: the front door ----------------------------------------------------


def test_the_front_door_renders_the_hub_after_a_restart(tmp_path: Path) -> None:
    """FIRST TEST - a fresh `create_app` over a config folder holding a
    `finale` `deploy.json`, with no deploy ever run in this process. The
    `DeployManager` constructor loads the file, so this is what proves the
    Hub survives a Marrquee restart.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    client = _client(settings, engine)

    response = client.get("/")

    assert response.status_code == 200
    assert "data-hub" in response.text
    assert words.HUB_TITLE in response.text


def test_no_saved_choices_goes_to_setup_apps(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


def test_saved_choices_with_no_finished_deploy_go_to_deploy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    client = _client(settings)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/deploy"


def test_a_deploy_that_ended_in_error_goes_to_deploy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="error",
            apps=(),
            headline="Something went wrong",
            detail=None,
            failure=Failure(
                code="docker_unreachable",
                headline="Docker didn't answer",
                what_to_do="Check your NAS's Docker app and try again",
                technical="connection refused",
            ),
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/deploy"


def test_the_finales_go_to_your_hub_link_now_lands_on_the_hub(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    deploy_response = client.get("/deploy")
    assert 'href="/"' in deploy_response.text

    hub_response = client.get("/")
    assert hub_response.status_code == 200


def test_the_wizard_router_no_longer_registers_the_front_door() -> None:
    paths = {route.path for route in wizard_router.routes if isinstance(route, APIRoute)}
    assert "/" not in paths


# --- Posters: addresses, states, wording --------------------------------------


def test_a_poster_links_to_the_host_the_browser_used(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    client = _client(settings, engine)

    response = client.get("/", headers={"host": "192.168.1.50:7788"})

    assert 'href="http://192.168.1.50:8989/"' in response.text
    assert 'aria-label="Open Sonarr. Status: Up. Opens in a new tab."' in response.text


def test_an_ipv6_host_gives_a_bracketed_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    client = _client(settings, engine)

    response = client.get("/", headers={"host": "[2001:db8::1]:7788"})

    assert 'href="http://[2001:db8::1]:8989/"' in response.text


def test_a_down_app_says_when_it_was_last_seen_has_no_href_and_no_aria_label(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("radarr",)))
    _write_snapshot(settings, _finale_snapshot(("radarr",)))
    finished_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            "radarr": ContainerSnapshot(
                name="radarr",
                exists=True,
                state="exited",
                exit_code=0,
                image=None,
                detail=None,
                finished_at=finished_at,
            ),
        },
    )
    client = _client(settings, engine)

    response = client.get("/")

    assert "Radarr stopped - last seen 2 hours ago." in response.text
    poster = _posters(response.text)["radarr"]
    assert poster.get("href") is None
    assert poster.get("aria-label") is None


def test_the_down_note_shows_when_any_app_is_down(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "radarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            **_running_containers(("sonarr",)),
            "radarr": ContainerSnapshot(
                name="radarr", exists=True, state="exited", exit_code=1, image=None, detail=None
            ),
        },
    )
    client = _client(settings, engine)

    response = client.get("/")

    assert _root(response.text)["data-any-down"] == "true"
    # Jinja autoescapes `'` as `&#39;`, and HUB_DOWN_NOTE carries one.
    assert words.HUB_DOWN_NOTE.replace("'", "&#39;") in response.text


def test_docker_unreachable_shows_not_sure_for_every_poster_and_still_200(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(settings, _finale_snapshot(("prowlarr", "sonarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            "prowlarr": ContainerSnapshot(
                name="prowlarr",
                exists=False,
                state=None,
                exit_code=None,
                image=None,
                detail="connection refused",
            ),
            "sonarr": ContainerSnapshot(
                name="sonarr",
                exists=False,
                state=None,
                exit_code=None,
                image=None,
                detail="connection refused",
            ),
        },
    )
    client = _client(settings, engine)

    response = client.get("/")

    assert response.status_code == 200
    assert _root(response.text)["data-docker-unreachable"] == "true"
    assert response.text.count(f">{words.HUB_CHIP_UNKNOWN}<") == 2


def test_a_proxied_request_shows_the_proxy_banner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    response = client.get("/", headers={"x-forwarded-host": "example.com"})

    # Jinja autoescapes `'` as `&#39;`, and HUB_PROXY_BANNER carries one.
    assert words.HUB_PROXY_BANNER.replace("'", "&#39;") in response.text


def test_posters_come_from_the_deploy_snapshot_not_the_saved_choices(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr")))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))  # radarr was never deployed
    client = _client(settings)

    response = client.get("/")

    assert set(_posters(response.text)) == {"sonarr"}


def test_every_poster_has_one_dot_the_status_label_and_a_chip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    client = _client(settings, engine)

    response = client.get("/")

    assert response.text.count('class="hub-dot"') == 1
    assert words.HUB_STATUS_LABEL in response.text
    assert f">{words.HUB_CHIP_UP}<" in response.text


def test_the_page_carries_no_meta_refresh(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    response = client.get("/")

    assert "http-equiv" not in response.text


# --- GET /api/hub/status: the same words, as JSON --------------------------


def test_the_status_endpoint_answers_200_with_no_saved_choices(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["apps"] == []
    assert payload["announce"] == words.HUB_NOTHING_SET_UP


def test_the_status_endpoint_answers_200_with_docker_unreachable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            "sonarr": ContainerSnapshot(
                name="sonarr",
                exists=False,
                state=None,
                exit_code=None,
                image=None,
                detail="connection refused",
            ),
        },
    )
    client = _client(settings, engine)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["docker_unreachable"] is True
    assert payload["apps"][0]["state"] == "unknown"


def test_the_status_endpoint_and_the_page_agree_on_every_posters_chip_and_line(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "radarr")))
    finished_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            **_running_containers(("sonarr",)),
            "radarr": ContainerSnapshot(
                name="radarr",
                exists=True,
                state="exited",
                exit_code=0,
                image=None,
                detail=None,
                finished_at=finished_at,
            ),
        },
    )
    client = _client(settings, engine)

    page = client.get("/")
    status = client.get("/api/hub/status")

    assert status.status_code == 200
    payload = status.json()
    assert len(payload["apps"]) == 2
    for app in payload["apps"]:
        assert app["chip"] in page.text
        if app["line"]:
            assert app["line"] in page.text


def test_the_status_endpoints_json_has_no_detail_field_anywhere(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            "sonarr": ContainerSnapshot(
                name="sonarr",
                exists=False,
                state=None,
                exit_code=None,
                image=None,
                detail="connection refused: dial unix /var/run/docker.sock",
            ),
        },
    )
    client = _client(settings, engine)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    assert "detail" not in response.text
    assert "connection refused" not in response.text
