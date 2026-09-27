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

import asyncio
import dataclasses
import html
import re
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from test_deploy_login import _already_finale_manager

from marrquee import hub as hub_module
from marrquee import questions as questions_module
from marrquee import words
from marrquee.catalog import CATALOG, apps_in_order, get_app
from marrquee.config import Settings
from marrquee.deploy import (
    AppAdd,
    AppProgress,
    DeployManager,
    DeploySnapshot,
    Failure,
    FakeReadinessProbe,
    WiringGap,
)
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.graphics_chip import GRAPHICS_DEVICE_NODE
from marrquee.hardlinks import HardlinkMonitor, HardlinkResult, save_hardlink_result
from marrquee.health import FakeLinkProbe
from marrquee.hub import LOGIN_HELP_URL
from marrquee.links import LINK_COUNT_MAX, LinkCard, load_links, save_links
from marrquee.login import LOGIN_STEP, load_login, save_login
from marrquee.login_apply import FakeLoginApplier
from marrquee.main import create_app
from marrquee.questions import (
    PLEX_STEP,
    SEEDING_STEP,
    VPN_STEP,
    QuestionCheck,
    QuestionField,
    QuestionStep,
    load_answers,
    save_step_answers,
)
from marrquee.recyclarr import RecyclarrControl, SyncRecord, SyncStatus
from marrquee.routes.wizard import router as wizard_router
from marrquee.state import STATE_VERSION, InstallState, load_state, save_state, write_json_atomic
from marrquee.vpn import VPN_PROVIDERS, TunnelPlace, provider_wiki_url
from marrquee.vpn_control import FakeGluetunControl
from marrquee.without_vpn import save_without_vpn, without_vpn_confirmed
from marrquee.words import STATUS_CHIP_DONE, app_line_done

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates"

# --- Shared fixtures and small builders --------------------------------------


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")
    _seed_fresh_drive_check(settings)
    return settings


def _seed_fresh_drive_check(settings: Settings) -> None:
    """A drive-check result saved just now, so `create_app`'s own default
    `HardlinkMonitor` (built whenever a test's own `create_app(...)` call
    passes no `hardlinks=`) never starts a real filesystem probe in the
    background - `refresh_if_due` only starts one for a missing or day-old
    result, and most of this file's tests have no opinion about the drive
    check at all. A test that DOES care injects its own `hardlinks=` with a
    saved result and clock it controls, which simply overwrites this one.
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


def _progress(app_id: str, name: str, port: int | None) -> AppProgress:
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
    moves: tuple[str, ...] = (),
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
        moves=moves,
    )


def _client(
    settings: Settings,
    engine: FakeDockerEngine | None = None,
    *,
    link_probe: FakeLinkProbe | None = None,
    vpn_control: FakeGluetunControl | None = None,
    hardlinks: HardlinkMonitor | None = None,
    recyclarr: RecyclarrControl | None = None,
) -> TestClient:
    if engine is None:
        engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    if link_probe is None:
        link_probe = FakeLinkProbe()
    app = create_app(
        settings=settings,
        engine=engine,
        link_probe=link_probe,
        vpn_control=vpn_control,
        hardlinks=hardlinks,
        recyclarr=recyclarr,
    )
    return TestClient(app)


class _RecordingRecyclarr:
    """Stands in for `RecyclarrMonitor` on a Hub route test: a fixed status
    to hand back, and a count of how many times a sync was ever requested -
    the same recording-fake shape `_RecordingSyncTrigger` gives the deploy
    engine's own tests.
    """

    def __init__(self, status: SyncStatus) -> None:
        self._status = status
        self.sync_calls = 0

    def request_sync(self) -> object:
        self.sync_calls += 1
        return None

    async def status(self) -> SyncStatus:
        return self._status


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


class _ButtonCollector(HTMLParser):
    """Collects every `<button data-role="...">`'s own attributes, keyed by
    that role - the Sync now button's own class (`btn-primary`/`btn-ghost`)
    lives on the tag itself, not any wrapping element.
    """

    def __init__(self) -> None:
        super().__init__()
        self.buttons: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        role = attrs_dict.get("data-role")
        if tag == "button" and role is not None:
            self.buttons[role] = attrs_dict


def _buttons(page_html: str) -> dict[str, dict[str, str | None]]:
    collector = _ButtonCollector()
    collector.feed(page_html)
    return collector.buttons


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


def _seeding_pane_html(page_html: str) -> str:
    """The seeding pane's own markup, sliced out of the whole page - it's
    the dialog's last pane, so its own close tag is always the very next
    `</dialog>`.
    """
    match = re.search(r'<div data-panel-pane="seeding">.*?</dialog>', page_html, re.DOTALL)
    assert match is not None, "no seeding pane found on the page"
    return match.group(0)


def _pane_html(page_html: str, name: str) -> str:
    """One named `data-panel-pane`'s own markup, sliced out of the whole
    page up to whichever comes first - the next pane, or the dialog's own
    close tag - so this works regardless of where the pane sits among its
    siblings.
    """
    match = re.search(
        rf'<div data-panel-pane="{name}">.*?(?=<div data-panel-pane="|</dialog>)',
        page_html,
        re.DOTALL,
    )
    assert match is not None, f"no {name} pane found on the page"
    return match.group(0)


def _assert_unique_ids(page_html: str) -> None:
    """Every `id="..."` on a rendered page has to be unique - two elements
    sharing one breaks every `label[for]`/`getElementById` hook that names
    it, silently, for whichever one a browser happens to pick.
    """
    ids = re.findall(r'\bid="([^"]+)"', page_html)
    dupes = sorted({id_ for id_ in ids if ids.count(id_) > 1})
    assert not dupes, f"duplicate element ids: {dupes!r}"


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


# --- read_hub_view and the VPN control server ---------------------------------


def test_read_hub_view_never_asks_the_control_server_with_no_vpn_installed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    vpn_control = FakeGluetunControl()
    client = _client(settings, engine, vpn_control=vpn_control)

    client.get("/api/hub/status")

    assert vpn_control.calls == []


def test_read_hub_view_asks_the_control_server_with_gluetuns_own_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "gluetun")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "gluetun")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            **_running_containers(("sonarr",)),
            "gluetun": ContainerSnapshot(
                name="gluetun",
                exists=True,
                state="running",
                exit_code=None,
                image=None,
                detail=None,
                health="healthy",
            ),
        },
    )
    place = TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )
    vpn_control = FakeGluetunControl(place=place)
    client = _client(settings, engine, vpn_control=vpn_control)

    response = client.get("/api/hub/status")

    assert vpn_control.calls == [("public_ip", "fake-gluetun-api-key")]
    payload = response.json()
    gluetun_tile = next(app for app in payload["apps"] if app["app_id"] == "gluetun")
    assert (
        gluetun_tile["line"]
        == "Protected - your downloads appear to come from Amsterdam, Netherlands"
    )


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
    all_offered = ("prowlarr", "sonarr", "radarr", "qbittorrent", "recyclarr", "plex", "jellyfin")
    save_state(settings.config_dir, _install_state(all_offered))
    _write_snapshot(settings, _finale_snapshot(all_offered))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    all_done_client = _client(settings)

    all_done = all_done_client.get("/?panel=install")

    assert words.HUB_INSTALL_ALL_DONE in all_done.text

    partial_settings = _settings(tmp_path / "partial")
    save_state(partial_settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(partial_settings, _finale_snapshot(("sonarr",)))
    save_login(partial_settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    partial_client = _client(partial_settings)

    partial = partial_client.get("/?panel=install")

    assert words.HUB_INSTALL_ALL_DONE not in partial.text
    assert "Prowlarr" in partial.text
    assert "Radarr" in partial.text


def test_jellyfins_install_row_gets_the_graphics_step_only_with_a_chip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)

    no_chip_engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
    )
    no_chip_page = _client(settings, no_chip_engine).get("/?panel=install").text
    assert 'data-question-step="jellyfin:graphics"' not in no_chip_page

    with_chip_engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr",)),
        host_paths={GRAPHICS_DEVICE_NODE},
    )
    with_chip_page = _client(settings, with_chip_engine).get("/?panel=install").text
    assert 'data-question-step="jellyfin:graphics"' in with_chip_page


def test_the_hub_never_probes_for_a_chip_once_jellyfin_is_already_installed(
    tmp_path: Path,
) -> None:
    """The chip could no longer matter for an already-installed Jellyfin -
    the predicate that guards the probe must skip it before it ever asks
    Docker, not merely discard whatever answer comes back.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "jellyfin")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "jellyfin")))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("sonarr", "jellyfin")),
        host_paths={GRAPHICS_DEVICE_NODE},
    )

    _client(settings, engine).get("/?panel=install")

    assert "probe_host_path" not in [name for name, _args in engine.calls]


def _install_row_html(page_html: str, app_id: str) -> str:
    match = re.search(
        rf'<li\s+class="install-row"\s+data-install-row="{app_id}".*?</li>',
        page_html,
        re.DOTALL,
    )
    assert match is not None, f"no install row rendered for {app_id!r}"
    return match.group(0)


def test_the_needs_sign_in_row_renders_a_no_js_form_a_signed_in_row_renders_add(
    tmp_path: Path,
) -> None:
    """Before a sign-in, Plex's own row is a plain, un-hidden form post - it
    has to work with JavaScript off. Once `plex_account` is saved, the row
    switches to the ordinary (JS-only) Add button every other app gets.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    client = _client(settings)

    unsigned_row = _install_row_html(client.get("/?panel=install").text, "plex")
    assert 'action="/plex/sign-in"' in unsigned_row
    assert 'data-sign-in="plex"' in unsigned_row
    # A bare `hidden` attribute (not `type="hidden"`, the pin-forwarding
    # field) would make the control a dead one until `hub.js` un-hides it -
    # this row has to work with no script at all.
    assert re.search(r"\shidden[\s>]", unsigned_row) is None
    assert "data-install-app" not in unsigned_row
    assert words.PLEX_SIGN_IN_BUTTON in html.unescape(unsigned_row)

    save_step_answers(settings.config_dir, "plex", {"plex_account": "ryan"})

    signed_in_row = _install_row_html(client.get("/?panel=install").text, "plex")
    assert 'data-install-app="plex"' in signed_in_row
    assert 'action="/plex/sign-in"' not in signed_in_row


def test_the_sign_in_refusal_banner_shows_only_when_the_query_says_failed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    client = _client(settings)

    quiet = client.get("/?panel=install")
    assert 'data-role="sign-in-refusal"' not in quiet.text

    failed = client.get("/?panel=install&sign_in=failed")
    assert 'data-role="sign-in-refusal"' in failed.text
    assert words.PLEX_SIGN_IN_DIDNT_FINISH in html.unescape(failed.text)


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


# --- Recyclarr's own poster: last synced, amber with a reason, Sync now -----


def test_page_and_poll_agree_on_the_app_down_reason(tmp_path: Path) -> None:
    """FIRST TEST (has-data-pipeline): a failed sync's own reason names
    Radarr because Marrquee's own health read says it's down - never
    because of anything Recyclarr's log itself says - and `GET /` and
    `GET /api/hub/status` must never disagree about it.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr", "recyclarr")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "radarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            **_running_containers(("sonarr", "recyclarr")),
            "radarr": ContainerSnapshot(
                name="radarr", exists=True, state="exited", exit_code=1, image=None, detail=None
            ),
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
    client = _client(settings, engine, recyclarr=recyclarr)

    page = client.get("/")
    poll = client.get("/api/hub/status")

    expected_line = words.recyclarr_line_app_down("Radarr")
    assert expected_line in html.unescape(page.text)
    poll_apps = {app["app_id"]: app for app in poll.json()["apps"]}
    assert poll_apps["recyclarr"]["line"] == expected_line
    assert poll_apps["recyclarr"]["sync_state"] == "failed"
    assert _posters(page.text)["recyclarr"]["data-sync-state"] == "failed"


def test_sync_now_posts_once_and_redirects(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "recyclarr")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "recyclarr")))
    recyclarr = _RecordingRecyclarr(
        SyncStatus(syncing=False, last=None, start_failed=False, run_failed=False)
    )
    client = _client(settings, recyclarr=recyclarr)

    response = client.post("/hub/apps/recyclarr/sync", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert recyclarr.sync_calls == 1


def test_a_sync_post_for_a_non_sync_app_changes_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "recyclarr")))
    _write_snapshot(settings, _finale_snapshot(("sonarr", "recyclarr")))
    recyclarr = _RecordingRecyclarr(
        SyncStatus(syncing=False, last=None, start_failed=False, run_failed=False)
    )
    client = _client(settings, recyclarr=recyclarr)

    response = client.post("/hub/apps/sonarr/sync", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert recyclarr.sync_calls == 0


def test_a_stopped_recyclarr_is_the_ordinary_down_poster_with_no_sync_now(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("recyclarr",)))
    _write_snapshot(settings, _finale_snapshot(("recyclarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            "recyclarr": ContainerSnapshot(
                name="recyclarr", exists=True, state="exited", exit_code=1, image=None, detail=None
            )
        },
    )
    client = _client(settings, engine)

    response = client.get("/")

    poster = _posters(response.text)["recyclarr"]
    assert poster["data-state"] == "down"
    assert "data-sync-state" not in poster
    assert 'action="/hub/apps/recyclarr/sync"' not in response.text


def test_sync_now_button_is_primary_when_failed_and_ghost_when_ok(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("recyclarr",)))
    _write_snapshot(settings, _finale_snapshot(("recyclarr",)))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(("recyclarr",)),
    )
    failed = _RecordingRecyclarr(
        SyncStatus(
            syncing=False,
            last=SyncRecord(finished_at=datetime.now(UTC), ok=False),
            start_failed=False,
            run_failed=False,
        )
    )
    ok = _RecordingRecyclarr(
        SyncStatus(
            syncing=False,
            last=SyncRecord(finished_at=datetime.now(UTC), ok=True),
            start_failed=False,
            run_failed=False,
        )
    )

    failed_page = _client(settings, engine, recyclarr=failed).get("/").text
    ok_page = _client(settings, engine, recyclarr=ok).get("/").text

    assert _buttons(failed_page)["sync-now"]["class"] == "btn-primary"
    assert _buttons(ok_page)["sync-now"]["class"] == "btn-ghost"


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
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
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
        step=step,
        answers=answers or {},
        problem=problem,
        problem_field=problem_field,
        words=words,
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


def test_the_sign_in_partial_never_renders_the_account_in_an_input() -> None:
    """`plex_account` never becomes a text box a browser could resubmit -
    the only way it's ever written is through `/plex/signed-in`."""
    unsigned = _render_question_step(PLEX_STEP)
    assert "<input" not in unsigned
    assert words.PLEX_SIGN_IN_BUTTON in html.unescape(unsigned)

    signed_in = _render_question_step(PLEX_STEP, answers={"plex_account": "ryan"})
    assert "<input" not in signed_in
    assert 'data-role="signed-in"' in signed_in
    assert words.plex_signed_in_as("ryan") in html.unescape(signed_in)
    assert words.PLEX_SIGN_IN_OTHER in html.unescape(signed_in)
    # The account name is only ever text content, never an attribute value
    # a script could read back as though it were a form field.
    assert 'value="ryan"' not in signed_in


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


# --- The one saved login: choose, change, retry, and the reset line ---------


def _wait_until_idle(manager: DeployManager, *, budget: float = 2.0) -> None:
    """Poll for a background run (an add, a reconnect, or a login run) to
    finish - the login run's own version of the plain `time.sleep(0.05)`
    every other background-task test in this file already uses, made into
    a loop because a login run's phase 2 can genuinely take a few ticks
    (a docker status read, a compose write, one recreate per app).
    """
    deadline = time.monotonic() + budget
    while manager.is_busy():
        if time.monotonic() > deadline:
            raise AssertionError("background run did not finish in time")
        time.sleep(0.01)


def test_nas_with_no_login_shows_the_hub_not_the_wizard(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    page = client.get("/", follow_redirects=False)
    assert page.status_code == 200

    status = client.get("/api/hub/status")
    assert status.json()["login_banner"] == "choose"


def test_choose_saves_the_login_starts_the_run_and_redirects(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post(
        "/hub/login",
        data={"username": "Owner", "password": "s3cret-pass-1", "password_again": "s3cret-pass-1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    record = load_login(config_dir)
    assert record.login is not None
    # `check_step`'s own rule lowercases the username - what's saved is
    # what every app's own API will see, not the raw keystrokes.
    assert record.login.username == "owner"

    # A fake applier and a fake Docker engine never actually await anything,
    # so the background run may already be done by the time this checks -
    # `_wait_until_idle` is a no-op in that case, and the real proof either
    # way is `applied` landing for the one installed app.
    _wait_until_idle(manager)
    assert load_login(config_dir).applied.get("prowlarr") == record.login.generation


def test_a_refused_choose_writes_nothing_and_keeps_the_username(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post(
        "/hub/login",
        data={"username": "Owner", "password": "short", "password_again": "short"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert load_login(config_dir).login is None
    assert manager.is_busy() is False
    # The username is kept in the box (lowercased, the same rule a saved
    # login is normalised by), the refusal names the field, and the
    # password boxes are never re-filled with what was typed.
    assert 'value="owner"' in response.text
    assert words.LOGIN_PROBLEM_PASSWORD_SHORT in response.text
    assert 'value="short"' not in response.text


def test_change_with_a_wrong_current_password_changes_nothing(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    save_login(config_dir, "owner", "right-password-1", honor_reset=None)
    before = (config_dir / "login.json").read_bytes()
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post(
        "/hub/login/change",
        data={
            "current_password": "nope-nope-1",
            "username": "owner",
            "password": "new-password-1",
            "password_again": "new-password-1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert (config_dir / "login.json").read_bytes() == before
    assert manager.is_busy() is False


def test_a_blank_new_password_keeps_it_and_changes_only_the_username(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    original = save_login(config_dir, "owner", "first-password-1", honor_reset=None)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post(
        "/hub/login/change",
        data={
            "current_password": "first-password-1",
            "username": "new-owner",
            "password": "",
            "password_again": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    record = load_login(config_dir)
    assert record.login is not None
    assert record.login.username == "new-owner"
    assert record.login.password == "first-password-1"
    assert record.login.generation == original.generation + 1


def test_reset_is_one_shot_and_the_reminder_shows_while_the_line_stays(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    reset_value = "forgot-my-password-2026"
    settings = Settings(
        host_mount=tmp_path / "host", config_dir=config_dir, reset_login=reset_value
    )
    save_login(config_dir, "owner", "old-password-1", honor_reset=None)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    before_reset = client.get("/api/hub/status")
    assert before_reset.json()["login_banner"] == "reset"

    # No current password required while a reset is outstanding.
    response = client.post(
        "/hub/login",
        data={
            "username": "owner",
            "password": "second-password-1",
            "password_again": "second-password-1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    record = load_login(config_dir)
    assert record.reset_honored == reset_value

    after_reset = client.get("/api/hub/status")
    # `record_applied` hasn't landed yet (a background run just started),
    # so the honest banner right after saving is "applying", not "none" -
    # what matters here is that it is never "reset" again for this value.
    assert after_reset.json()["login_banner"] != "reset"

    _wait_until_idle(manager)

    # Restarting the app (a new `create_app`, the same `reset_login` value
    # already honoured) must never reset a second time.
    second_app = create_app(settings=settings, engine=engine, manager=manager)
    second_client = TestClient(second_app)
    still_set = second_client.get("/api/hub/status")
    assert still_set.json()["login_banner"] == "none"


def test_retry_starts_a_run_only_when_something_is_pending(tmp_path: Path) -> None:
    applier = FakeLoginApplier(results={"sonarr": False})
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr", "sonarr"), login_applier=applier
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    first = client.post("/hub/login/retry", follow_redirects=False)
    assert first.status_code == 303
    _wait_until_idle(manager)

    # The applier was actually called for both pending apps - proof the
    # retry started a real run, not a silent no-op.
    assert {call[0] for call in applier.calls} == {"prowlarr", "sonarr"}
    record = load_login(config_dir)
    assert record.login is not None
    assert record.applied.get("prowlarr") == record.login.generation
    assert record.applied.get("sonarr") != record.login.generation

    # A second retry while sonarr is still pending calls the applier again.
    second = client.post("/hub/login/retry", follow_redirects=False)
    assert second.status_code == 303
    _wait_until_idle(manager)
    assert len(applier.calls) > 2


def test_status_json_carries_login_banner_and_never_a_password(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.get("/api/hub/status")

    assert response.status_code == 200
    assert "login_banner" in response.json()
    assert "s3cret-password-1" not in response.text


# --- The one saved login: the Hub's own markup, for every state -------------


def test_every_login_hook_exists_for_its_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRST TEST - every login hook the page promises exists on a real
    render of the state that should draw it: `data-login-banner` always
    carries the current banner, `data-role="login-banner"` wraps the
    choose/reset form and the applying line, `data-role="login-pending"`
    names the apps still waiting, `data-role="login-summary"` and
    `data-panel-open="login"` appear once a login is set, and
    `data-panel-pane="login"` (the Change pane's own div) is on every
    render, whatever the state.
    """
    # choose: nothing saved yet.
    choose_root = tmp_path / "choose"
    manager, engine, config_dir = _already_finale_manager(
        choose_root, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=choose_root / "host", config_dir=config_dir)
    choose_page = (
        TestClient(create_app(settings=settings, engine=engine, manager=manager)).get("/").text
    )
    assert 'data-login-banner="choose"' in choose_page
    assert 'data-role="login-banner"' in choose_page
    assert 'action="/hub/login"' in choose_page
    assert 'data-panel-pane="login"' in choose_page
    assert 'data-role="login-summary"' not in choose_page

    # reset: saved, but the current MARRQUEE_RESET_LOGIN value is new.
    reset_root = tmp_path / "reset"
    manager, engine, config_dir = _already_finale_manager(
        reset_root, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    save_login(config_dir, "owner", "old-password-1", honor_reset=None)
    settings = Settings(
        host_mount=reset_root / "host", config_dir=config_dir, reset_login="forgot-2026"
    )
    reset_page = (
        TestClient(create_app(settings=settings, engine=engine, manager=manager)).get("/").text
    )
    assert 'data-login-banner="reset"' in reset_page
    assert 'data-role="login-banner"' in reset_page
    assert 'action="/hub/login"' in reset_page

    # applying: a login run is going right now.
    applying_root = tmp_path / "applying"
    manager, engine, config_dir = _already_finale_manager(
        applying_root, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    save_login(config_dir, "owner", "s3cret-password-1", honor_reset=None)
    monkeypatch.setattr(manager, "login_progress", lambda: "Putting your login on Prowlarr…")
    settings = Settings(host_mount=applying_root / "host", config_dir=config_dir)
    applying_page = (
        TestClient(create_app(settings=settings, engine=engine, manager=manager)).get("/").text
    )
    assert 'data-login-banner="applying"' in applying_page
    assert 'data-role="login-banner"' in applying_page
    assert 'data-role="login-line"' in applying_page
    assert "Putting your login on Prowlarr…" in applying_page

    # pending: saved, but sonarr never received it.
    pending_root = tmp_path / "pending"
    manager, engine, config_dir = _already_finale_manager(
        pending_root,
        ("prowlarr", "sonarr"),
        login_applier=FakeLoginApplier(results={"sonarr": False}),
    )
    settings = Settings(host_mount=pending_root / "host", config_dir=config_dir)
    client = TestClient(create_app(settings=settings, engine=engine, manager=manager))
    client.post(
        "/hub/login",
        data={
            "username": "owner",
            "password": "s3cret-password-1",
            "password_again": "s3cret-password-1",
        },
    )
    _wait_until_idle(manager)
    pending_page = client.get("/").text
    assert 'data-login-banner="pending"' in pending_page
    assert 'data-role="login-pending"' in pending_page
    assert 'action="/hub/login/retry"' in pending_page

    # set: every installed app has it - the summary and its Change/Forgot
    # links replace every banner, and the pane behind them is on the page.
    set_root = tmp_path / "set"
    manager, engine, config_dir = _already_finale_manager(
        set_root, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=set_root / "host", config_dir=config_dir)
    client = TestClient(create_app(settings=settings, engine=engine, manager=manager))
    client.post(
        "/hub/login",
        data={
            "username": "owner",
            "password": "s3cret-password-1",
            "password_again": "s3cret-password-1",
        },
    )
    _wait_until_idle(manager)
    set_page = client.get("/").text
    assert 'data-login-banner="none"' in set_page
    assert 'data-role="login-summary"' in set_page
    assert 'data-panel-open="login"' in set_page
    assert 'data-panel-pane="login"' in set_page
    assert 'data-role="login-banner"' not in set_page
    assert 'data-role="login-pending"' not in set_page


def test_choose_banner_shows_rules_hints_and_the_plex_note_with_no_password_value(
    tmp_path: Path,
) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    client = TestClient(create_app(settings=settings, engine=engine, manager=manager))

    page = client.get("/").text
    start = page.index('data-role="login-banner"')
    banner = page[start : page.index("</section>", start)]

    assert words.LOGIN_USERNAME_HINT in banner
    assert words.LOGIN_PASSWORD_HINT in banner
    assert words.LOGIN_PLEX_NOTE in banner
    password_inputs = re.findall(r"<input[^>]*type=\"password\"[^>]*>", banner)
    assert len(password_inputs) == 2
    assert all("value=" not in field for field in password_inputs)


def test_pending_banner_names_sonarr_and_offers_try_again(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path,
        ("prowlarr", "sonarr"),
        login_applier=FakeLoginApplier(results={"sonarr": False}),
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    client = TestClient(create_app(settings=settings, engine=engine, manager=manager))
    client.post(
        "/hub/login",
        data={
            "username": "owner",
            "password": "s3cret-password-1",
            "password_again": "s3cret-password-1",
        },
    )
    _wait_until_idle(manager)

    page = client.get("/").text

    assert 'data-role="login-pending"' in page
    start = page.index('data-role="login-pending"')
    pending = page[start : page.index("</div>", start)]

    assert words.hub_login_pending(("Sonarr",)) in pending
    assert words.LOGIN_TRY_AGAIN in pending
    assert 'action="/hub/login/retry"' in pending


def test_reset_reminder_links_to_the_readme_section(tmp_path: Path) -> None:
    reset_value = "forgot-my-password-2026"
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(
        host_mount=tmp_path / "host", config_dir=config_dir, reset_login=reset_value
    )
    client = TestClient(create_app(settings=settings, engine=engine, manager=manager))
    client.post(
        "/hub/login",
        data={
            "username": "owner",
            "password": "s3cret-password-1",
            "password_again": "s3cret-password-1",
        },
    )
    _wait_until_idle(manager)

    page = client.get("/").text

    # Scoped to the reminder's own element - `LOGIN_HELP_URL` also appears
    # in the footer summary once a login is set, so a bare "is it on the
    # page anywhere" check would pass even if the reminder itself linked
    # nowhere.
    start = page.index('data-role="login-reset-reminder"')
    reminder = page[start : page.index("</div>", start)]

    assert words.HUB_LOGIN_RESET_REMINDER.replace("'", "&#39;") in reminder
    assert f'href="{LOGIN_HELP_URL}"' in reminder


def test_a_refused_change_reopens_the_login_pane_with_focus_on_the_field(tmp_path: Path) -> None:
    manager, engine, config_dir = _already_finale_manager(
        tmp_path, ("prowlarr",), login_applier=FakeLoginApplier()
    )
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_dir)
    save_login(config_dir, "owner", "right-password-1", honor_reset=None)
    client = TestClient(create_app(settings=settings, engine=engine, manager=manager))

    response = client.post(
        "/hub/login/change",
        data={
            "current_password": "nope-nope-1",
            "username": "owner",
            "password": "new-password-1",
            "password_again": "new-password-1",
        },
    )

    assert response.status_code == 200
    assert 'data-panel-mode="login"' in response.text
    assert words.CHANGE_PROBLEM_WRONG_CURRENT.replace("'", "&#39;") in response.text
    assert "new-password-1" not in response.text
    assert "nope-nope-1" not in response.text
    match = re.search(r'id="q-marrquee-current_password"[^>]*autofocus', response.text)
    assert match is not None, "expected autofocus on the current-password field"


# --- The VPN questions on screen, the guide link and the red poster ---------
#
# `_render_question_step` (defined above, beside the password-field test)
# is reused here rather than redefined.


def test_the_choose_login_banner_and_the_gluetun_install_row_render_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRST TEST - every selector `questions.js` reads (`[data-guide-select]`,
    `[data-role="question-guide"]`, `data-guide-url`) has to exist on a real
    render, and the two places that share `partials/app_questions.html` on
    the very same page - the choose-login banner (`marrquee:login`) and an
    install row's own question form (`gluetun:vpn`) - have to keep every
    field id unique between them.

    Gluetun is never actually offered on the Hub (`offered=False` keeps it
    out of `install_rows`), so this borrows a temporarily-offered catalog
    entry to put both on one render at once. A saved login that's
    outstanding against `MARRQUEE_RESET_LOGIN` (banner "reset") draws the
    same choose-login section as "no login yet" (banner "choose") without
    blocking the install pane the way "no login yet" does.
    """
    settings = Settings(
        host_mount=tmp_path / "host",
        config_dir=tmp_path / "config",
        reset_login="forgot-my-password-2026",
    )
    deployed = ("prowlarr",)
    save_state(settings.config_dir, _install_state(deployed))
    _write_snapshot(settings, _finale_snapshot(deployed))
    save_login(settings.config_dir, "owner", "old-password-1", honor_reset=None)
    # qBittorrent's own row would ALSO carry Gluetun's VPN step as a
    # companion - excluded here (`offered=False`) so exactly one gluetun
    # row is on the page for this test's own id-uniqueness assertion to be
    # about, and so `running_without_vpn` (which would open the Hub's OWN
    # separate VPN pane, carrying the very same field ids a second time)
    # never becomes true just because this test needed Gluetun's row.
    offered_catalog = tuple(
        dataclasses.replace(app, offered=True)
        if app.id == "gluetun"
        else dataclasses.replace(app, offered=False)
        if app.id == "qbittorrent"
        else app
        for app in CATALOG
    )
    monkeypatch.setattr(hub_module, "CATALOG", offered_catalog)
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers=_running_containers(deployed),
    )
    client = _client(settings, engine)

    page = client.get("/?panel=install").text

    assert 'id="choose-login"' in page
    assert 'data-install-form="gluetun"' in page
    assert 'data-question-step="gluetun:vpn"' in page
    assert "data-guide-select" in page
    assert 'data-role="question-guide"' in page
    assert "data-guide-url=" in page

    _assert_unique_ids(page)


def test_the_vpn_step_renders_a_select_with_every_provider() -> None:
    html = _render_question_step(VPN_STEP)

    assert html.count("<option") == len(VPN_PROVIDERS) + 1
    # The placeholder option is both disabled and the one selected, since
    # nothing has been answered yet.
    placeholder_match = re.search(r'<option\s+value=""[^>]*>', html)
    assert placeholder_match is not None
    assert "disabled" in placeholder_match.group(0)
    assert "selected" in placeholder_match.group(0)

    cyberghost_match = re.search(r'<option\s+value="cyberghost"[^>]*>([^<]*)</option>', html)
    assert cyberghost_match is not None
    assert "disabled" in cyberghost_match.group(0)
    reason = cyberghost_match.group(1).replace("&#39;", "'")
    assert words.VPN_PROVIDER_NEEDS_FILES in reason


def test_a_saved_provider_is_selected_and_the_guide_link_points_at_its_wiki_page() -> None:
    mullvad = next(provider for provider in VPN_PROVIDERS if provider.value == "mullvad")

    html = _render_question_step(VPN_STEP, answers={"provider": "mullvad"})

    mullvad_match = re.search(r'<option\s+value="mullvad"[^>]*>', html)
    assert mullvad_match is not None
    assert "selected" in mullvad_match.group(0)

    guide_match = re.search(r'<a\s+class="question-guide"[^>]*href="([^"]+)"', html, re.DOTALL)
    assert guide_match is not None
    assert guide_match.group(1) == provider_wiki_url(mullvad)


def test_the_vpn_guide_link_falls_back_to_the_provider_index_with_nothing_chosen() -> None:
    html = _render_question_step(VPN_STEP)

    guide_match = re.search(r'<a\s+class="question-guide"[^>]*href="([^"]+)"', html, re.DOTALL)
    assert guide_match is not None
    assert guide_match.group(1) == words.VPN_GUIDE_INDEX_URL


def test_vpn_password_fields_never_carry_a_value_even_with_saved_answers() -> None:
    html = _render_question_step(
        VPN_STEP,
        answers={
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "openvpn_password": "super-secret-password",
            "wireguard_private_key": "super-secret-key",
            "wireguard_preshared_key": "super-secret-psk",
        },
    )

    assert "super-secret-password" not in html
    assert "super-secret-key" not in html
    assert "super-secret-psk" not in html


def test_login_step_still_renders_byte_identically_through_the_shared_partial() -> None:
    """Regression: the new `list` branch in `partials/app_questions.html`
    sits ahead of the `else` (text/password) branch without changing a
    single byte of what the shared partial already draws for a step with
    no `list` field, like the Marrquee-wide login.
    """
    html = _render_question_step(LOGIN_STEP)

    assert html == (
        "\n\n"
        '<fieldset class="question-step" data-question-step="marrquee:login">\n'
        "  <legend>Choose one login for your apps</legend>\n"
        '  <p class="question-step__lede">Every app Marrquee installs asks for this '
        "username and password. Your browser can remember it, so you only type it "
        "once per device.</p>\n\n  \n\n  \n  \n  \n  \n"
        '  <label class="field-label" for="q-marrquee-username">Username</label>\n'
        "  <input\n"
        '    class="path-input"\n'
        '    type="text"\n'
        '    id="q-marrquee-username"\n'
        '    name="username"\n'
        '    value=""\n'
        "    \n"
        '    aria-describedby="q-marrquee-username-hint"\n'
        "  >\n"
        '  <p class="field-hint" id="q-marrquee-username-hint">3 to 32 characters: '
        "letters, numbers, dots, dashes or underscores. Capitals become small "
        "letters.</p>\n  \n  \n  \n  \n  \n"
        '  <label class="field-label" for="q-marrquee-password">Password</label>\n'
        "  <input\n"
        '    class="path-input"\n'
        '    type="password"\n'
        '    id="q-marrquee-password"\n'
        '    name="password"\n'
        "    \n    \n"
        '    aria-describedby="q-marrquee-password-hint"\n'
        "  >\n"
        '  <p class="field-hint" id="q-marrquee-password-hint">At least 8 characters, '
        "with no space at the start or end.</p>\n  \n  \n  \n  \n  \n"
        '  <label class="field-label" for="q-marrquee-password_again">Type it '
        "again</label>\n"
        "  <input\n"
        '    class="path-input"\n'
        '    type="password"\n'
        '    id="q-marrquee-password_again"\n'
        '    name="password_again"\n'
        "    \n    \n    \n"
        "  >\n  \n  \n  \n  \n"
        "</fieldset>"
    )


def test_the_vpn_tile_carries_data_kind_vpn_and_an_arr_tile_carries_data_kind_arr(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "gluetun")))
    _write_snapshot(settings, _finale_snapshot(("prowlarr", "gluetun")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            **_running_containers(("prowlarr",)),
            "gluetun": ContainerSnapshot(
                name="gluetun",
                exists=True,
                state="running",
                exit_code=None,
                image=None,
                detail=None,
                health="unhealthy",
            ),
        },
    )
    client = _client(settings, engine)

    posters = _posters(client.get("/").text)

    assert posters["gluetun"]["data-kind"] == "vpn"
    assert posters["gluetun"]["data-state"] == "down"
    assert posters["prowlarr"]["data-kind"] == "arr"


# --- qBittorrent's own paused poster and "Change seeding" -------------------


def _qbittorrent_containers() -> dict[str, ContainerSnapshot]:
    return {
        "qbittorrent": ContainerSnapshot(
            name="qbittorrent",
            exists=True,
            state="running",
            exit_code=None,
            image=None,
            detail=None,
        ),
        "gluetun": ContainerSnapshot(
            name="gluetun",
            exists=True,
            state="running",
            exit_code=None,
            image=None,
            detail=None,
            health="unhealthy",
        ),
    }


def test_seeding_panel_hooks_exist_when_qbittorrent_is_installed(tmp_path: Path) -> None:
    """FIRST TEST - every hook the page promises for the seeding pane
    genuinely exists once qBittorrent is installed: the poster's own
    `data-paused`, the "Change seeding" link's `data-panel-open="seeding"`,
    the pane's `data-panel-pane="seeding"`, its form's
    `action="/hub/seeding"`, and the own-numbers fields' `data-field`
    wrappers.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), containers=_qbittorrent_containers()
    )
    client = _client(settings, engine)

    page = client.get("/?panel=seeding")

    posters = _posters(page.text)
    assert posters["qbittorrent"]["data-paused"] == "true"
    assert re.search(r'<a[^>]*data-panel-open="seeding"[^>]*>', page.text) is not None
    assert _dialog(page.text).get("data-panel-mode") == "seeding"

    pane = _seeding_pane_html(page.text)
    assert 'action="/hub/seeding"' in pane
    assert 'data-field="seed_ratio"' in pane
    assert 'data-field="seed_days"' in pane


def test_the_seeding_steps_own_number_fields_carry_a_data_field_wrapper() -> None:
    html = _render_question_step(SEEDING_STEP)

    assert 'data-field="seed_ratio"' in html
    assert 'data-field="seed_days"' in html
    # The choice field itself never gets one - only a field with its own
    # `shown_when` does.
    assert 'data-field="seeding"' not in html


def test_a_non_qbittorrent_hub_never_offers_the_seeding_panel(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    client = _client(settings)

    page = client.get("/?panel=seeding")

    assert _dialog(page.text).get("data-panel-mode") == "closed"


def test_the_seeding_panes_own_fields_never_duplicate_the_install_rows(tmp_path: Path) -> None:
    """qBittorrent's own "+" row carries the same `SEEDING_STEP` (app_id
    "qbittorrent") the seeding pane's form does - while qBittorrent isn't
    installed yet, the pane's copy of those fields must not render at all,
    or every `id="q-qbittorrent-seed*"` on the page would exist twice.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    client = _client(settings)

    page = client.get("/?panel=install").text

    assert 'data-install-form="qbittorrent"' in page
    assert 'data-question-step="qbittorrent:seeding"' in page
    _assert_unique_ids(page)


def test_change_seeding_saves_the_new_answer_and_reconnects(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine)
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post(
        "/hub/seeding",
        data={"seeding": "private", "seed_ratio": "", "seed_days": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    time.sleep(0.05)
    assert load_answers(settings.config_dir)["qbittorrent"]["seeding"] == "private"
    assert manager.snapshot().adding is None


async def _never_returns(_seconds: float) -> None:
    """A `sleep` that never wakes up - the deterministic way to hold a
    `DeployManager` run "busy" forever, with no race against how fast a
    fake add would otherwise finish.
    """
    await asyncio.Event().wait()


def test_seeding_refuses_and_saves_nothing_while_the_manager_is_busy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), images={get_app("radarr").image}
    )
    manager = DeployManager(
        settings, engine, probe=FakeReadinessProbe(default=False), sleep=_never_returns
    )
    app = create_app(settings=settings, engine=engine, manager=manager)

    with TestClient(app) as client:
        started = client.post("/api/hub/apps/radarr/install", json={"answers": {}})
        assert started.status_code == 202

        response = client.post(
            "/hub/seeding",
            data={"seeding": "private", "seed_ratio": "", "seed_days": ""},
        )

        assert response.status_code == 200
        assert _dialog(response.text).get("data-panel-mode") == "seeding"
        assert words.HUB_SEEDING_BUSY in _seeding_pane_html(response.text)
        assert "qbittorrent" not in load_answers(settings.config_dir)


def test_own_with_blank_numbers_refuses_on_seed_ratio_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    client = _client(settings)

    response = client.post(
        "/hub/seeding", data={"seeding": "own", "seed_ratio": "", "seed_days": ""}
    )

    assert response.status_code == 200
    pane = _seeding_pane_html(response.text)
    assert words.SEEDING_PROBLEM_OWN_EMPTY in pane
    assert "qbittorrent" not in load_answers(settings.config_dir)


# --- The badge, Change VPN and the three-step escape hatch ------------------


def _vpn_answers(
    provider: str = "mullvad", *, user: str = "ci-user", password: str = "ci-pass"
) -> dict[str, str]:
    return {
        "provider": provider,
        "vpn_type": "openvpn",
        "openvpn_user": user,
        "openvpn_password": password,
        "wireguard_private_key": "",
        "wireguard_addresses": "",
        "wireguard_preshared_key": "",
        "server_countries": "",
    }


def test_every_vpn_change_hook_exists_in_the_rendered_hub(tmp_path: Path) -> None:
    """FIRST TEST - every hook the badge, the Change VPN link, both new
    panel panes and the escape link promise genuinely exists once each is
    reachable.
    """
    # Running without a VPN: the badge, and the "add" VPN pane's own form.
    running = _settings(tmp_path / "running")
    save_state(running.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(running, _finale_snapshot(("qbittorrent",)))
    running_page = _client(running).get("/").text

    badge = re.search(r'<a[^>]*class="no-vpn-badge"[^>]*>', running_page)
    assert badge is not None
    assert 'href="/?panel=vpn#hub-panel"' in badge.group(0)
    assert 'data-panel-open="vpn"' in badge.group(0)
    running_vpn_pane = _pane_html(running_page, "vpn")
    assert 'action="/hub/vpn"' in running_vpn_pane
    _assert_unique_ids(running_page)

    # Gluetun installed: its own "Change VPN" link, and the "change" pane.
    changeable = _settings(tmp_path / "changeable")
    save_state(changeable.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(changeable, _finale_snapshot(("gluetun", "qbittorrent")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), containers=_qbittorrent_containers()
    )
    changeable_page = _client(changeable, engine).get("/").text

    assert (
        re.search(
            r'<a class="hub-tile-link" href="/\?panel=vpn#hub-panel" data-panel-open="vpn">',
            changeable_page,
        )
        is not None
    )
    changeable_vpn_pane = _pane_html(changeable_page, "vpn")
    assert 'action="/hub/vpn"' in changeable_vpn_pane
    _assert_unique_ids(changeable_page)

    # Not installed yet: the without-vpn escape link on qBittorrent's own
    # VPN companion step, and the undo form once confirmed.
    fresh = _settings(tmp_path / "fresh")
    save_state(fresh.config_dir, _install_state(()))
    _write_snapshot(fresh, _finale_snapshot(()))
    save_login(fresh.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    fresh_page = _client(fresh).get("/?panel=install").text

    assert 'data-role="without-vpn-link"' in fresh_page
    assert 'data-panel-choice="without-vpn"' in fresh_page
    without_vpn_pane = _pane_html(fresh_page, "without-vpn")
    assert 'action="/hub/without-vpn"' in without_vpn_pane
    assert 'name="stage"' in without_vpn_pane
    _assert_unique_ids(fresh_page)

    confirmed = _settings(tmp_path / "confirmed")
    save_state(confirmed.config_dir, _install_state(()))
    _write_snapshot(confirmed, _finale_snapshot(()))
    save_login(confirmed.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    save_without_vpn(confirmed.config_dir, now=datetime(2026, 9, 26, tzinfo=UTC))
    confirmed_page = _client(confirmed).get("/?panel=install").text

    assert 'action="/hub/without-vpn/undo"' in confirmed_page
    _assert_unique_ids(confirmed_page)


def test_the_status_endpoint_carries_running_without_vpn_and_the_widened_actions(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(settings, _finale_snapshot(("qbittorrent",)))
    client = _client(settings)

    status = client.get("/api/hub/status").json()

    assert status["running_without_vpn"] is True
    assert status["apps"][0]["actions"] == "none"
    assert status["apps"][0]["can_change_vpn"] is False


def test_the_escape_link_is_absent_from_the_change_vpn_pane(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    client = _client(settings)

    pane = _pane_html(client.get("/?panel=vpn").text, "vpn")

    assert "without-vpn-link" not in pane
    assert words.QUESTION_NO_VPN_LINK not in pane


def test_the_vpn_form_never_echoes_the_username_or_a_password(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    save_step_answers(settings.config_dir, "gluetun", _vpn_answers(user="do-not-leak-me"))
    client = _client(settings)

    pane = _pane_html(client.get("/?panel=vpn").text, "vpn")

    assert "do-not-leak-me" not in pane
    assert "ci-pass" not in pane
    assert 'value="ci-user"' not in pane


def test_post_hub_vpn_starts_change_vpn_then_saves(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), containers=_qbittorrent_containers()
    )
    manager = DeployManager(settings, engine, vpn=FakeGluetunControl())
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post(
        "/hub/vpn", data=_vpn_answers(provider="protonvpn"), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    adding = manager.snapshot().adding
    assert adding is not None
    assert adding.app_id == "gluetun"
    assert adding.purpose == "change_vpn"
    assert load_answers(settings.config_dir)["gluetun"]["provider"] == "protonvpn"


def test_post_hub_vpn_busy_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(settings, _finale_snapshot(("qbittorrent",)))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), images={get_app("radarr").image}
    )
    manager = DeployManager(
        settings, engine, probe=FakeReadinessProbe(default=False), sleep=_never_returns
    )
    app = create_app(settings=settings, engine=engine, manager=manager)

    with TestClient(app) as client:
        started = client.post("/api/hub/apps/radarr/install", json={"answers": {}})
        assert started.status_code == 202

        response = client.post("/hub/vpn", data=_vpn_answers())

        assert response.status_code == 200
        assert _dialog(response.text).get("data-panel-mode") == "vpn"
        assert words.HUB_VPN_BUSY in _pane_html(response.text, "vpn")
        assert "gluetun" not in load_answers(settings.config_dir)


def test_post_hub_vpn_refusal_keeps_the_pane_open_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    client = _client(settings)

    response = client.post("/hub/vpn", data={**_vpn_answers(), "provider": ""})

    assert response.status_code == 200
    assert _dialog(response.text).get("data-panel-mode") == "vpn"
    assert "gluetun" not in load_answers(settings.config_dir)


def test_post_hub_vpn_in_no_vpn_mode_adds_gluetun(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(settings, _finale_snapshot(("qbittorrent",)))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"), containers=_qbittorrent_containers()
    )
    manager = DeployManager(settings, engine, vpn=FakeGluetunControl())
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post("/hub/vpn", data=_vpn_answers(), follow_redirects=False)

    assert response.status_code == 303
    adding = manager.snapshot().adding
    assert adding is not None
    assert adding.app_id == "gluetun"
    assert adding.purpose == "add"
    assert adding.moves == ("qbittorrent",)


def test_post_hub_vpn_in_error_it_retries(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    failure = Failure(code="vpn_refused", headline="x", what_to_do="y", technical="z")
    _write_snapshot(
        settings,
        _finale_with_add(
            (),
            adding=_adding(
                app_id="gluetun",
                purpose="add",
                state="error",
                line=app_line_done("VPN"),
                failure=failure,
                moves=("qbittorrent",),
            ),
        ),
    )
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    manager = DeployManager(settings, engine, vpn=FakeGluetunControl())
    app = create_app(settings=settings, engine=engine, manager=manager)
    client = TestClient(app)

    response = client.post("/hub/vpn", data=_vpn_answers(), follow_redirects=False)

    assert response.status_code == 303
    adding = manager.snapshot().adding
    assert adding is not None
    # A genuinely new run replaced the stale one - its own `started_at`
    # (always "now") is never the persisted snapshot's placeholder value.
    assert adding.started_at != "2026-09-24T00:00:00+00:00"


def test_three_posts_confirm_a_wrong_sentence_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    client = _client(settings)

    stage1 = client.post("/hub/without-vpn", data={"stage": "1", "typed": ""})
    assert stage1.status_code == 200
    assert _pane_html(stage1.text, "without-vpn").count('data-stage="2"') == 1
    assert not (settings.config_dir / "without_vpn.json").exists()

    stage2 = client.post("/hub/without-vpn", data={"stage": "2", "typed": ""})
    assert stage2.status_code == 200
    assert _pane_html(stage2.text, "without-vpn").count('data-stage="3"') == 1
    assert not (settings.config_dir / "without_vpn.json").exists()

    wrong = client.post("/hub/without-vpn", data={"stage": "3", "typed": "wrong"})
    assert wrong.status_code == 200
    pane = _pane_html(wrong.text, "without-vpn")
    # HTML-escaped (the sentence's own apostrophe becomes `&#39;`) - checked
    # by a substring with no punctuation of its own.
    assert "exactly as shown" in pane
    assert not without_vpn_confirmed(settings.config_dir)

    right = client.post(
        "/hub/without-vpn",
        data={"stage": "3", "typed": words.WITHOUT_VPN_PHRASE},
        follow_redirects=False,
    )
    assert right.status_code == 303
    assert right.headers["location"] == "/?panel=install#hub-panel"
    assert without_vpn_confirmed(settings.config_dir)


def test_without_vpn_post_is_refused_once_qbittorrent_is_installed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(settings, _finale_snapshot(("qbittorrent",)))
    client = _client(settings)

    response = client.post(
        "/hub/without-vpn",
        data={"stage": "3", "typed": words.WITHOUT_VPN_PHRASE},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert not without_vpn_confirmed(settings.config_dir)


def test_undo_clears_the_confirmation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_without_vpn(settings.config_dir, now=datetime(2026, 9, 26, tzinfo=UTC))
    client = _client(settings)

    response = client.post("/hub/without-vpn/undo", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/?panel=install#hub-panel"
    assert not without_vpn_confirmed(settings.config_dir)


def test_undo_is_refused_once_qbittorrent_is_installed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(settings, _finale_snapshot(("qbittorrent",)))
    save_without_vpn(settings.config_dir, now=datetime(2026, 9, 26, tzinfo=UTC))
    client = _client(settings)

    client.post("/hub/without-vpn/undo")

    assert without_vpn_confirmed(settings.config_dir)


def test_the_qbittorrent_row_without_the_vpn_shows_the_note_the_undo_and_only_seeding(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(()))
    _write_snapshot(settings, _finale_snapshot(()))
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    save_without_vpn(settings.config_dir, now=datetime(2026, 9, 26, tzinfo=UTC))
    client = _client(settings)

    page = client.get("/?panel=install").text

    assert words.WITHOUT_VPN_ROW_NOTE in page
    assert words.QBITTORRENT_DESCRIPTION_NO_VPN in page
    assert 'action="/hub/without-vpn/undo"' in page
    assert 'data-question-step="gluetun:vpn"' not in page
    assert 'data-question-step="qbittorrent:seeding"' in page


def test_the_seeding_pane_preselects_the_saved_preset(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("gluetun", "qbittorrent")))
    _write_snapshot(settings, _finale_snapshot(("gluetun", "qbittorrent")))
    save_step_answers(
        settings.config_dir,
        "qbittorrent",
        {"seeding": "private", "seed_ratio": "", "seed_days": ""},
    )
    client = _client(settings)

    pane = _seeding_pane_html(client.get("/?panel=seeding").text)

    assert re.search(r'value="private"\s+checked', pane) is not None


# --- The drive check's own amber note: one builder, read alike by the page
# and the poll ----------------------------------------------------------------


class _DriveNoteCollector(HTMLParser):
    """Collects the drive note's own text - `None` if the tag is never
    reached at all (it's ALWAYS in the markup per the contract, so a
    missing tag would itself be worth surfacing rather than reading as "").
    """

    def __init__(self) -> None:
        super().__init__()
        self.text: str | None = None
        self._capturing = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "span" and dict(attrs).get("data-role") == "drive-note-text":
            self._capturing = True
            self.text = ""

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self.text = (self.text or "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._capturing = False


def _drive_note_text(page_html: str) -> str | None:
    collector = _DriveNoteCollector()
    collector.feed(page_html)
    return collector.text


def _drive_result(
    outcome: str, *, reason: str | None = None, checked_at: datetime | None = None
) -> HardlinkResult:
    return HardlinkResult(
        outcome=outcome,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        folder="/volume1/media/data/media/tv" if reason is not None else None,
        technical=None,
        checked_at=(checked_at or datetime.now(UTC)).isoformat(),
    )


def test_page_note_and_poll_note_are_the_same_words_from_one_builder(tmp_path: Path) -> None:
    """FIRST TEST (has-data-pipeline) - `GET /` and `GET /api/hub/status`
    both draw the amber note through `read_hub_view`, so the two can never
    word it differently.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_hardlink_result(settings.config_dir, _drive_result("copies", reason="different_drives"))
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, hardlinks=monitor)

    page = client.get("/")
    status = client.get("/api/hub/status")

    page_note = _drive_note_text(page.text)
    assert page_note == status.json()["drive_note"] == words.HUB_DRIVE_NOTE_COPIES
    assert _root(page.text)["data-drive-note"] == "true"


def test_a_works_result_shows_no_note_on_the_page_or_the_poll(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_hardlink_result(settings.config_dir, _drive_result("works"))
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, hardlinks=monitor)

    page = client.get("/")
    status = client.get("/api/hub/status")

    assert _drive_note_text(page.text) == ""
    assert _root(page.text)["data-drive-note"] == "false"
    assert status.json()["drive_note"] is None


def test_couldnt_check_shows_its_own_sentence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_hardlink_result(
        settings.config_dir, _drive_result("couldnt_check", reason="folder_missing")
    )
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, hardlinks=monitor)

    page = client.get("/")

    assert _drive_note_text(page.text) == words.HUB_DRIVE_NOTE_UNCHECKED
    assert _root(page.text)["data-drive-note"] == "true"


def test_not_needed_and_no_saved_file_show_no_note(tmp_path: Path) -> None:
    # A raw Settings, never seeded with a saved result - the same "nothing
    # usable yet" shape an upgrading owner's config folder starts in.
    settings = Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, hardlinks=monitor)

    page = client.get("/")

    assert _drive_note_text(page.text) == ""
    assert _root(page.text)["data-drive-note"] == "false"


def test_the_note_links_to_diagnostics_your_drive_section(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    save_hardlink_result(settings.config_dir, _drive_result("copies", reason="different_drives"))
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, hardlinks=monitor)

    page = client.get("/").text

    note = re.search(r'<a[^>]*data-role="drive-note"[^>]*>', page)
    assert note is not None
    assert 'href="/diagnostics#your-drive"' in note.group(0)
    _assert_unique_ids(page)


def test_the_note_sits_after_the_no_vpn_badge_when_both_apply(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("qbittorrent",)))
    _write_snapshot(settings, _finale_snapshot(("qbittorrent",)))
    save_hardlink_result(settings.config_dir, _drive_result("copies", reason="different_drives"))
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    client = _client(settings, hardlinks=monitor)

    page = client.get("/").text

    badge_index = page.index('data-role="no-vpn-badge"')
    note_index = page.index('data-role="drive-note"')
    assert badge_index < note_index


def test_read_hub_view_asks_the_monitor_to_refresh_exactly_once_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_hub_view` has several callers (the page, the poll, every write
    route's own re-render), so its own job is asking the monitor exactly
    once each time it's called - the monitor's own dedupe is what then
    keeps that to at most one check actually in flight, however many
    callers ask on the same tick.
    """
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    monitor = HardlinkMonitor(settings, clock=lambda: datetime.now(UTC))
    calls: list[None] = []
    original_refresh = monitor.refresh_if_due

    def counting_refresh() -> None:
        calls.append(None)
        original_refresh()

    monkeypatch.setattr(monitor, "refresh_if_due", counting_refresh)
    client = _client(settings, hardlinks=monitor)

    client.get("/")
    assert len(calls) == 1

    client.get("/api/hub/status")
    assert len(calls) == 2


def test_the_poll_starts_the_daily_check_when_the_saved_result_is_a_day_old(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    stale_at = datetime(2026, 9, 24, 0, 0, 0, tzinfo=UTC)
    save_hardlink_result(settings.config_dir, _drive_result("works", checked_at=stale_at))
    monitor = HardlinkMonitor(settings, clock=lambda: stale_at + timedelta(hours=25))
    client = _client(settings, hardlinks=monitor)

    client.get("/api/hub/status")

    assert monitor._task is not None  # type: ignore[attr-defined]


def test_a_fresh_saved_result_or_one_still_in_flight_starts_no_check(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    fresh_at = datetime(2026, 9, 24, 0, 0, 0, tzinfo=UTC)
    save_hardlink_result(settings.config_dir, _drive_result("works", checked_at=fresh_at))
    monitor = HardlinkMonitor(settings, clock=lambda: fresh_at + timedelta(hours=1))
    client = _client(settings, hardlinks=monitor)

    client.get("/api/hub/status")

    assert monitor._task is None  # type: ignore[attr-defined]
