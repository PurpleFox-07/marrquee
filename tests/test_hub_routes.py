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
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from marrquee import questions as questions_module
from marrquee import words
from marrquee.catalog import apps_in_order
from marrquee.config import Settings
from marrquee.deploy import AppAdd, AppProgress, DeployManager, DeploySnapshot, Failure, WiringGap
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.health import FakeLinkProbe
from marrquee.links import LINK_COUNT_MAX, LinkCard, load_links, save_links
from marrquee.main import create_app
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep
from marrquee.routes.wizard import router as wizard_router
from marrquee.state import STATE_VERSION, InstallState, load_state, save_state, write_json_atomic
from marrquee.words import STATUS_CHIP_DONE, app_line_done

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates"

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


def _finale_with_add(
    app_ids: tuple[str, ...],
    *,
    adding: AppAdd | None = None,
    wiring_gaps: tuple[WiringGap, ...] = (),
) -> DeploySnapshot:
    return dataclasses.replace(_finale_snapshot(app_ids), adding=adding, wiring_gaps=wiring_gaps)


def _adding(
    app_id: str = "radarr",
    *,
    purpose: str = "add",
    state: str = "starting",
    line: str = "Starting Radarr",
    note: str | None = None,
    failure: Failure | None = None,
    compose_ran: bool = False,
) -> AppAdd:
    return AppAdd(
        app_id=app_id,
        purpose=purpose,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        line=line,
        note=note,
        failure=failure,
        wiring=(),
        compose_ran=compose_ran,
        started_at="2026-09-24T00:00:00+00:00",
    )


def _client(
    settings: Settings,
    engine: FakeDockerEngine | None = None,
    *,
    link_probe: FakeLinkProbe | None = None,
) -> TestClient:
    if engine is None:
        engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    if link_probe is None:
        link_probe = FakeLinkProbe()
    app = create_app(settings=settings, engine=engine, link_probe=link_probe)
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


def _link(link_id: str, label: str, url: str) -> LinkCard:
    return LinkCard(id=link_id, label=label, url=url)


class _LinkCollector(HTMLParser):
    """Collects every link card `<a data-link="...">`'s own attributes,
    keyed by link id - mirrors `_PosterCollector` for app posters.
    """

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        link_id = attrs_dict.get("data-link")
        if tag == "a" and link_id is not None:
            self.links[link_id] = attrs_dict


def _links(page_html: str) -> dict[str, dict[str, str | None]]:
    collector = _LinkCollector()
    collector.feed(page_html)
    return collector.links


class _GridOrderCollector(HTMLParser):
    """Collects `data-app`/`data-link` values (and `"+"` for the "+" tile)
    in the order they appear in the poster grid, proving apps are drawn
    before link cards, which are drawn before the "+" tile.
    """

    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attrs_dict = dict(attrs)
        value = attrs_dict.get("data-app") or attrs_dict.get("data-link")
        if value is not None:
            self.order.append(value)
        elif "hub-plus" in (attrs_dict.get("class") or "").split():
            self.order.append("+")


def _grid_order(page_html: str) -> list[str]:
    collector = _GridOrderCollector()
    collector.feed(page_html)
    return collector.order


class _DialogCollector(HTMLParser):
    """Collects the page's own `<dialog data-role="hub-panel">` attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "dialog" and not self.attrs:
            self.attrs = dict(attrs)


def _dialog(page_html: str) -> dict[str, str | None]:
    collector = _DialogCollector()
    collector.feed(page_html)
    return collector.attrs


class _AnchorNestingCollector(HTMLParser):
    """Walks every start/end tag, and fails the moment an `<a class="…
    hub-link-edit …">` opens while another `<a>` is still open around it -
    the one shape HTML forbids (nested interactive content) and the one
    the Edit pill's markup must never take.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open_anchors = 0
        self.nested_edit_pill = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        if "hub-link-edit" in classes and self.open_anchors > 0:
            self.nested_edit_pill = True
        self.open_anchors += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.open_anchors > 0:
            self.open_anchors -= 1


def _every_edit_pill_is_a_sibling(page_html: str) -> bool:
    collector = _AnchorNestingCollector()
    collector.feed(page_html)
    return not collector.nested_edit_pill


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


# --- Link cards: saved, checked, and drawn the same way on the page and the poll -


def test_a_saved_link_renders_as_a_card_after_the_apps(tmp_path: Path) -> None:
    """FIRST TEST - a link saved to links.json shows up as its own card,
    checked by the injected `FakeLinkProbe` rather than the network, and
    drawn after the app posters.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_links(settings.config_dir, [_link("a" * 16, "Router", "http://192.168.1.1")])
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    client = _client(settings, engine)

    response = client.get("/")

    assert response.status_code == 200
    assert _grid_order(response.text) == ["sonarr", "a" * 16, "+"]
    link = _links(response.text)["a" * 16]
    assert link["href"] == "http://192.168.1.1"
    assert link["data-state"] == "up"


def test_a_down_link_card_keeps_its_href_and_says_the_hedged_line(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_links(settings.config_dir, [_link("b" * 16, "Router", "http://192.168.1.1")])
    client = _client(settings, link_probe=FakeLinkProbe(default=False))

    response = client.get("/")

    link = _links(response.text)["b" * 16]
    assert link["href"] == "http://192.168.1.1"
    assert link["data-state"] == "down"
    # Jinja autoescapes `'`, and HUB_LINK_LINE_DOWN carries one.
    assert words.HUB_LINK_LINE_DOWN.replace("'", "&#39;") in response.text


def test_page_and_status_agree_on_link_cards(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_links(settings.config_dir, [_link("c" * 16, "Router", "http://192.168.1.1")])
    client = _client(settings, link_probe=FakeLinkProbe(default=False))

    page = client.get("/")
    status = client.get("/api/hub/status")

    assert status.status_code == 200
    payload = status.json()
    assert len(payload["links"]) == 1
    link_out = payload["links"][0]
    assert link_out["chip"] in page.text
    # Jinja autoescapes `'`, and the hedged line carries one.
    assert link_out["line"].replace("'", "&#39;") in page.text


def test_a_broken_links_json_still_renders_the_hub_with_200_and_no_link_cards(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    (settings.config_dir / "links.json").write_text("not json")
    client = _client(settings)

    response = client.get("/")

    assert response.status_code == 200
    assert _links(response.text) == {}


def test_the_status_endpoint_carries_links_empty_with_no_links_json(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    assert response.json()["links"] == []


def test_no_link_probe_runs_when_there_are_no_links(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    probe = FakeLinkProbe()
    client = _client(settings, link_probe=probe)

    client.get("/")

    assert probe.calls == []


# --- Writing links: "+" and Edit are real form posts, no JavaScript needed --


def _link_id(n: int) -> str:
    return f"{n:016x}"


def test_posts_before_finale_save_nothing(tmp_path: Path) -> None:
    """FIRST TEST - install.json exists but no deploy has ever reached
    finale (no deploy.json at all), so every write route must refuse and
    change nothing, the same guard `GET /` already applies.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    client = _client(settings)

    response = client.post(
        "/hub/links", data={"label": "Router", "url": "192.168.1.1"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert not (settings.config_dir / "links.json").exists()


def test_added_link_lands_between_the_apps_and_plus(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    response = client.post(
        "/hub/links", data={"label": "Router", "url": "192.168.1.1"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    saved = load_links(settings.config_dir)
    assert len(saved) == 1
    assert saved[0].url == "http://192.168.1.1"

    page = client.get("/")
    assert _grid_order(page.text) == ["sonarr", saved[0].id, "+"]


def test_a_refusal_re_renders_with_the_typed_values_and_the_panel_open_on_link(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    response = client.post("/hub/links", data={"label": "Test", "url": "ftp://x"})

    assert response.status_code == 200
    assert _dialog(response.text).get("data-panel-mode") == "link"
    assert words.link_problem_message("url_not_web") in response.text
    assert 'value="ftp://x"' in response.text
    assert not (settings.config_dir / "links.json").exists()


def test_the_51st_link_is_refused_with_too_many(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    existing = [_link(_link_id(n), f"Link {n}", f"http://host{n}") for n in range(LINK_COUNT_MAX)]
    save_links(settings.config_dir, existing)
    client = _client(settings)

    response = client.post("/hub/links", data={"label": "One too many", "url": "http://host50"})

    assert response.status_code == 200
    assert words.LINK_PROBLEM_TOO_MANY in response.text
    assert len(load_links(settings.config_dir)) == LINK_COUNT_MAX


def test_edit_keeps_the_cards_place_and_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    card_a = _link(_link_id(1), "Router", "http://192.168.1.1")
    card_b = _link(_link_id(2), "NAS", "http://192.168.1.2")
    save_links(settings.config_dir, [card_a, card_b])
    client = _client(settings)

    response = client.post(
        f"/hub/links/{card_a.id}",
        data={"label": "New Name", "url": "192.168.1.99"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"

    saved = load_links(settings.config_dir)
    assert [card.id for card in saved] == [card_a.id, card_b.id]
    assert saved[0].label == "New Name"
    assert saved[0].url == "http://192.168.1.99"

    page = client.get("/")
    assert _grid_order(page.text) == ["sonarr", card_a.id, card_b.id, "+"]


def test_edit_of_an_unknown_id_changes_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    card = _link(_link_id(1), "Router", "http://192.168.1.1")
    save_links(settings.config_dir, [card])
    client = _client(settings)
    before = load_links(settings.config_dir)

    well_formed_but_missing = client.post(
        f"/hub/links/{_link_id(9)}",
        data={"label": "New", "url": "http://x"},
        follow_redirects=False,
    )
    malformed = client.post(
        "/hub/links/not-a-real-id", data={"label": "New", "url": "http://x"}, follow_redirects=False
    )

    assert well_formed_but_missing.status_code == 303
    assert well_formed_but_missing.headers["location"] == "/"
    assert malformed.status_code == 303
    assert malformed.headers["location"] == "/"
    assert load_links(settings.config_dir) == before


def test_remove_drops_only_that_card_and_leaves_install_json_byte_identical(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    card_a = _link(_link_id(1), "Router", "http://192.168.1.1")
    card_b = _link(_link_id(2), "NAS", "http://192.168.1.2")
    save_links(settings.config_dir, [card_a, card_b])
    install_before = (settings.config_dir / "install.json").read_bytes()
    client = _client(settings)

    response = client.post(f"/hub/links/{card_a.id}/remove", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_links(settings.config_dir) == (card_b,)
    assert (settings.config_dir / "install.json").read_bytes() == install_before


def test_panel_choose_draws_the_dialog_open_with_two_choices_install_first(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    response = client.get("/?panel=choose")

    dialog = _dialog(response.text)
    assert dialog.get("data-panel-mode") == "choose"
    assert "open" in dialog
    assert response.text.index('data-panel-choice="install"') < response.text.index(
        'data-panel-choice="link"'
    )


def test_panel_nonsense_and_edit_unknown_link_draw_it_closed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    nonsense = client.get("/?panel=nonsense")
    unknown_edit = client.get(f"/?panel=edit&link={_link_id(9)}")

    for response in (nonsense, unknown_edit):
        dialog = _dialog(response.text)
        assert dialog.get("data-panel-mode") == "closed"
        assert "open" not in dialog


def test_install_pane_is_truthful(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr", "radarr")))
    _write_snapshot(settings, _finale_snapshot(("prowlarr", "sonarr", "radarr")))
    all_done_client = _client(settings)

    all_done = all_done_client.get("/?panel=install")

    assert words.HUB_INSTALL_ALL_DONE in all_done.text

    partial_settings = _settings(tmp_path / "partial")
    save_state(partial_settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(partial_settings, _finale_snapshot(("sonarr",)))
    partial_client = _client(partial_settings)

    partial = partial_client.get("/?panel=install")

    assert words.HUB_INSTALL_ALL_DONE not in partial.text
    assert "Prowlarr" in partial.text
    assert "Radarr" in partial.text


def test_plus_is_the_last_li_with_zero_links(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    response = client.get("/")

    assert _grid_order(response.text) == ["sonarr", "+"]


def test_every_edit_pill_is_a_sibling_of_its_card_link_never_inside_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_links(settings.config_dir, [_link(_link_id(1), "Router", "http://192.168.1.1")])
    client = _client(settings)

    response = client.get("/")

    assert 'class="hub-link-edit"' in response.text
    assert _every_edit_pill_is_a_sibling(response.text)


def test_the_edit_pane_form_actions_target_the_cards_own_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    card = _link(_link_id(1), "Router", "http://192.168.1.1")
    save_links(settings.config_dir, [card])
    client = _client(settings)

    response = client.get(f"/?panel=edit&link={card.id}")

    assert _dialog(response.text).get("data-panel-mode") == "edit"
    assert f'action="/hub/links/{card.id}"' in response.text
    assert f'action="/hub/links/{card.id}/remove"' in response.text
    assert 'data-role="edit-form"' in response.text
    assert 'data-role="remove-form"' in response.text
    assert 'data-role="edit-label"' in response.text
    assert 'data-role="edit-url"' in response.text
    assert f'value="{card.label}"' in response.text
    assert f'value="{card.url}"' in response.text


# --- Adding an app: the Hub keeps showing while one is in flight -------------


def test_an_add_in_flight_keeps_the_hub_and_spotlights_one_tile(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(
        settings,
        _finale_with_add(("prowlarr", "sonarr"), adding=_adding(line="Starting Radarr")),
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("prowlarr", "sonarr")),
    )
    client = _client(settings, engine)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 200
    assert _grid_order(response.text) == ["prowlarr", "sonarr", "radarr", "+"]
    radarr = _posters(response.text)["radarr"]
    assert radarr["data-state"] == "starting"
    assert radarr.get("href") is None
    assert f">{words.HUB_CHIP_ADDING}<" in response.text
    assert "Starting Radarr" in response.text

    status_payload = client.get("/api/hub/status").json()
    radarr_status = next(app for app in status_payload["apps"] if app["app_id"] == "radarr")
    assert radarr_status["add_state"] == "starting"
    assert radarr_status["url"] is None


def test_a_failed_add_tile_has_no_link_says_the_headline_and_advice_and_offers_retry(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    failure = Failure(
        code="port_in_use",
        headline="Radarr couldn't be added.",
        what_to_do="Check your NAS's Docker app and try again.",
        technical="boom",
    )
    _write_snapshot(
        settings,
        _finale_with_add(
            ("sonarr",),
            adding=_adding(state="error", line=words.app_line_error("Radarr"), failure=failure),
        ),
    )
    client = _client(settings)

    response = client.get("/")

    assert response.status_code == 200
    radarr = _posters(response.text)["radarr"]
    assert radarr.get("href") is None
    # Jinja autoescapes `'`, and both sentences carry one.
    expected_line = "Radarr couldn't be added. Check your NAS's Docker app and try again."
    assert expected_line.replace("'", "&#39;") in response.text

    status_payload = client.get("/api/hub/status").json()
    radarr_status = next(app for app in status_payload["apps"] if app["app_id"] == "radarr")
    assert radarr_status["actions"] == "retry"


def test_a_gap_tile_is_up_with_the_amber_note_and_connect_again(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(
        settings,
        _finale_with_add(
            ("sonarr",),
            wiring_gaps=(
                WiringGap(app_id="sonarr", failed_lines=("Prowlarr wasn't told about Sonarr",)),
            ),
        ),
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), containers=_running_containers(("sonarr",))
    )
    client = _client(settings, engine)

    response = client.get("/")
    status_payload = client.get("/api/hub/status").json()

    assert response.status_code == 200
    poster = _posters(response.text)["sonarr"]
    assert poster["data-state"] == "up"
    sonarr_status = next(app for app in status_payload["apps"] if app["app_id"] == "sonarr")
    assert sonarr_status["actions"] == "reconnect"
    assert sonarr_status["note"] == words.hub_wiring_gap_note(
        "Sonarr", ("Prowlarr wasn't told about Sonarr",)
    )


def test_page_and_status_agree_on_the_adding_tiles_chip_line_and_add_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_with_add(("sonarr",), adding=_adding(state="wiring")))
    client = _client(settings)

    page = client.get("/")
    status = client.get("/api/hub/status").json()

    radarr_status = next(app for app in status["apps"] if app["app_id"] == "radarr")
    assert radarr_status["chip"] in page.text
    assert radarr_status["line"] in page.text
    assert radarr_status["add_state"] == "wiring"


# --- Retry, cancel and reconnect: form posts that always 303 to / -----------


def test_retry_cancel_and_reconnect_for_the_wrong_app_change_nothing_and_303(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    failure = Failure(code="port_in_use", headline="x", what_to_do="y", technical="z")
    _write_snapshot(
        settings,
        _finale_with_add(
            ("sonarr",), adding=_adding(app_id="radarr", state="error", failure=failure)
        ),
    )
    client = _client(settings)
    before = (settings.config_dir / "deploy.json").read_bytes()

    retry_wrong = client.post("/hub/apps/sonarr/retry", follow_redirects=False)
    cancel_wrong = client.post("/hub/apps/sonarr/cancel", follow_redirects=False)
    reconnect_unknown = client.post("/hub/apps/not-a-real-app/reconnect", follow_redirects=False)

    for response in (retry_wrong, cancel_wrong, reconnect_unknown):
        assert response.status_code == 303
        assert response.headers["location"] == "/"
    assert (settings.config_dir / "deploy.json").read_bytes() == before


def test_retry_re_runs_a_failed_add_from_scratch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    failure = Failure(code="port_in_use", headline="x", what_to_do="y", technical="z")
    _write_snapshot(
        settings,
        _finale_with_add(
            ("sonarr",), adding=_adding(app_id="radarr", state="error", failure=failure)
        ),
    )
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post("/hub/apps/radarr/retry", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # `retry_add` runs in the background - whatever it settles on, its own
    # `started_at` (always "now") proves a genuinely new run replaced the
    # stale one, rather than the wrong-app guard silently leaving it alone.
    time.sleep(0.05)
    adding = manager.snapshot().adding
    assert adding is not None
    assert adding.started_at != "2026-09-24T00:00:00+00:00"


def test_cancel_removes_a_created_container_and_returns_the_app_to_the_install_list(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr")))
    failure = Failure(code="port_in_use", headline="x", what_to_do="y", technical="z")
    _write_snapshot(
        settings,
        _finale_with_add(
            ("sonarr",),
            adding=_adding(app_id="radarr", state="error", failure=failure, compose_ran=True),
        ),
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("radarr",)),
    )
    manager = DeployManager(settings, engine)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post("/hub/apps/radarr/cancel", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert manager.snapshot().adding is None
    assert ("remove_container", ("radarr",)) in engine.calls
    grown = load_state(settings.config_dir)
    assert grown is not None
    assert grown.app_ids == ("sonarr",)


class _FormInsideAnchorCollector(HTMLParser):
    """Fails the moment a `<form>` opens while an `<a>` is still open around
    it - the one shape HTML forbids, and the one an adding/reconnecting
    tile's Try again / Cancel / Connect again forms must never take.
    """

    def __init__(self) -> None:
        super().__init__()
        self.open_anchors = 0
        self.nested_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.open_anchors += 1
        elif tag == "form" and self.open_anchors > 0:
            self.nested_form = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.open_anchors > 0:
            self.open_anchors -= 1


def test_action_forms_are_siblings_of_the_poster_link_never_inside_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    failure = Failure(code="port_in_use", headline="x", what_to_do="y", technical="z")
    _write_snapshot(
        settings, _finale_with_add(("sonarr",), adding=_adding(state="error", failure=failure))
    )
    client = _client(settings)

    response = client.get("/")

    assert 'class="hub-tile-actions"' in response.text
    collector = _FormInsideAnchorCollector()
    collector.feed(response.text)
    assert not collector.nested_form


class _InstallButtonCollector(HTMLParser):
    """Collects every `<button data-install-app="...">`'s own attributes,
    keyed by app id.
    """

    def __init__(self) -> None:
        super().__init__()
        self.buttons: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        app_id = attrs_dict.get("data-install-app")
        if tag == "button" and app_id is not None:
            self.buttons[app_id] = attrs_dict


class _InstallFormCollector(HTMLParser):
    """Collects every `<form data-install-form="...">`'s own attributes,
    keyed by app id.
    """

    def __init__(self) -> None:
        super().__init__()
        self.forms: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        app_id = attrs_dict.get("data-install-form")
        if tag == "form" and app_id is not None:
            self.forms[app_id] = attrs_dict


def test_no_js_install_pane_hides_every_control_and_shows_the_noscript_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no JavaScript, nothing in the install pane is a dead control -
    the "Add" button and its question form both render `hidden` (only
    `hub.js` ever removes that), and the pane's own `<noscript>` says so
    plainly instead.
    """
    fixture_step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="Fixture step",
        lede="A fixture question for tests.",
        fields=(QuestionField(name="name", label="Name", kind="text"),),
        check=lambda answers: QuestionCheck(ok=True, answers=answers, problem=None, field=None),
    )
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (fixture_step,))
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(settings, _finale_snapshot(("prowlarr", "sonarr")))
    client = _client(settings)

    response = client.get("/?panel=install")

    buttons = _InstallButtonCollector()
    buttons.feed(response.text)
    assert "hidden" in buttons.buttons["radarr"]

    forms = _InstallFormCollector()
    forms.feed(response.text)
    assert "hidden" in forms.forms["radarr"]

    assert "<noscript>" in response.text
    assert words.HUB_INSTALL_NEEDS_JS.replace("'", "&#39;") in response.text


def _render_question_step(
    step: QuestionStep,
    *,
    answers: dict[str, str] | None = None,
    problem: str | None = None,
    problem_field: str | None = None,
) -> str:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("partials/app_questions.html")
    return template.render(
        step=step, answers=answers or {}, problem=problem, problem_field=problem_field
    )


def test_a_password_field_never_carries_a_value_attribute_even_with_a_saved_answer() -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="Fixture step",
        lede="l",
        fields=(QuestionField(name="token", label="Token", kind="password"),),
        check=lambda answers: QuestionCheck(ok=True, answers=answers, problem=None, field=None),
    )

    html = _render_question_step(step, answers={"token": "super-secret-value"})

    assert "value=" not in html
    assert "super-secret-value" not in html
    assert 'type="password"' in html


def test_reconnect_re_runs_wiring_for_an_installed_app(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post("/hub/apps/sonarr/reconnect", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # `NoWiringYet` (the default runner) finishes with no real wait, so the
    # reconnect may already be done by the time this checks - either way,
    # sonarr's own line is rebuilt from scratch only by a reconnect actually
    # completing, never by the wrong-app guard leaving it untouched.
    time.sleep(0.05)
    final = manager.snapshot()
    assert final.adding is None
    assert final.apps[0].line == app_line_done("Sonarr")
