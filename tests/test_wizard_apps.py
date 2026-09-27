"""Tests for screen one - pick your apps.

Every test builds the app through `create_app`, the same seam `test_alive.py`
uses, so nothing here touches a real Docker socket or filesystem path beyond
`tmp_path`. HTML is read with the stdlib `html.parser` rather than a regex
wherever a tag's attributes matter (which checkboxes exist, which are
checked) - a regex is fine for plain substring checks (words, css ordering).
"""

from __future__ import annotations

import dataclasses
import html
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marrquee import words
from marrquee.catalog import CATALOG, AppRule
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.state import STATE_VERSION, InstallState, save_state, write_json_atomic
from marrquee.without_vpn import save_without_vpn, without_vpn_confirmed

_WIZARD_CSS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "css" / "wizard.css"
)

# Gluetun is a real catalog entry but `offered=False` - the wizard's own
# grid never draws a checkbox for it, so every assertion about "one
# checkbox per app" means one per *offered* app.
_OFFERED = tuple(app for app in CATALOG if app.offered)


class _CheckboxCollector(HTMLParser):
    """Collects every `<input type="checkbox">` tag's attributes, in DOM order."""

    def __init__(self) -> None:
        super().__init__()
        self.checkboxes: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._collect(tag, attrs)

    def _collect(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "input" and attrs_dict.get("type") == "checkbox":
            self.checkboxes.append(attrs_dict)


def _checkbox_inputs(page_html: str) -> list[dict[str, str | None]]:
    collector = _CheckboxCollector()
    collector.feed(page_html)
    return collector.checkboxes


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings, status: DockerStatus | None = None) -> TestClient:
    if status is None:
        status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _write_finale_deploy(settings: Settings) -> None:
    """Persist a `deploy.json` already at `finale` - the shape every
    `/setup/...` guard test needs to prove the Hub is home.
    """
    snapshot = DeploySnapshot(
        run_id="run-1",
        phase="finale",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="done",
                chip="Ready",
                line="Prowlarr is ready",
                note=None,
                port=9696,
            ),
        ),
        headline="Now showing: your media server",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


def _install_state(*, app_ids: tuple[str, ...]) -> InstallState:
    return InstallState(
        version=STATE_VERSION,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys={},
        puid=1000,
        pgid=1000,
        umask="022",
        timezone="Etc/UTC",
        created="2026-09-22T00:00:00+00:00",
    )


# --- Chrome: base.html, css order, the alive page untouched -----------------


def test_apps_screen_renders_through_base_html_with_lang_viewport_and_css_order(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/apps")

    assert response.status_code == 200
    page = response.text
    assert '<html lang="en">' in page
    assert 'name="viewport"' in page

    tokens_index = page.index("css/tokens.css")
    app_index = page.index("css/app.css")
    wizard_index = page.index("css/wizard.css")
    assert tokens_index < app_index < wizard_index


def test_alive_page_renders_identically_after_the_head_block_is_added(tmp_path: Path) -> None:
    # The alive page now answers at "/diagnostics" rather than "/" - this
    # test still proves its original claim: the empty `head` block on
    # base.html leaves this page's own render untouched.
    client = _client(_settings(tmp_path), DockerStatus(connected=True, version="27.3.1"))

    response = client.get("/diagnostics")

    assert response.status_code == 200
    assert "Talking to Docker" in response.text
    assert 'href="/diagnostics">Check again</a>' in response.text
    # The new block is empty on this page - nothing from it should render.
    assert "wizard.css" not in response.text


# --- GET /setup/apps: one checkbox per app, defaults vs saved state ---------


def test_one_checked_checkbox_per_catalog_app_in_catalog_order_with_description(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/apps")

    checkboxes = _checkbox_inputs(response.text)
    assert [box["value"] for box in checkboxes] == [app.id for app in _OFFERED]
    checked_by_id = {box["value"]: "checked" in box for box in checkboxes}
    assert checked_by_id == {app.id: app.default_ticked for app in _OFFERED}
    # Jinja2 escapes an apostrophe (Recyclarr's description has one) as an
    # entity - unescape before comparing against the plain-text constant.
    rendered = html.unescape(response.text)
    for app in _OFFERED:
        assert app.name in response.text
        assert app.description in rendered


def test_gluetun_never_gets_a_checkbox_of_its_own(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/apps")

    checkboxes = _checkbox_inputs(response.text)
    assert "gluetun" not in [box["value"] for box in checkboxes]


def test_a_saved_install_ticks_exactly_the_saved_apps(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(app_ids=("radarr",)))
    client = _client(settings)

    response = client.get("/setup/apps")

    checked = {box["value"]: "checked" in box for box in _checkbox_inputs(response.text)}
    assert checked == {
        "prowlarr": False,
        "sonarr": False,
        "radarr": True,
        "qbittorrent": False,
        "recyclarr": False,
        "plex": False,
        "jellyfin": False,
    }


def test_an_unknown_id_in_the_query_string_is_dropped_not_a_500(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/apps", params={"apps": "bogus,radarr"})

    assert response.status_code == 200
    checked = {box["value"]: "checked" in box for box in _checkbox_inputs(response.text)}
    assert checked == {
        "prowlarr": False,
        "sonarr": False,
        "radarr": True,
        "qbittorrent": False,
        "recyclarr": False,
        "plex": False,
        "jellyfin": False,
    }


# --- POST /setup/apps: catalog-ordered csv, or a 200 refusal ----------------


def test_posting_a_subset_redirects_to_the_login_step_with_a_catalog_ordered_csv(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/apps",
        data={"apps": ["radarr", "prowlarr"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/login?apps=prowlarr,radarr"


def test_posting_gluetun_alone_is_treated_the_same_as_posting_nothing(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/setup/apps", data={"apps": ["gluetun"]}, follow_redirects=False)

    assert response.status_code == 200
    assert words.WIZARD_PICK_AT_LEAST_ONE in response.text


def test_posting_gluetun_alongside_real_apps_drops_it_from_the_redirect(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/apps", data={"apps": ["gluetun", "radarr"]}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/login?apps=radarr"


def test_ticking_qbittorrent_alone_carries_gluetun_along_in_catalog_order(
    tmp_path: Path,
) -> None:
    """Gluetun rides in silently the moment its own rider is chosen - the
    owner never ticks it directly, but qBittorrent must never run outside
    the tunnel.
    """
    client = _client(_settings(tmp_path))

    response = client.post("/setup/apps", data={"apps": ["qbittorrent"]}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/login?apps=gluetun,qbittorrent"


def test_ticking_qbittorrent_with_gluetun_already_posted_never_duplicates_it(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/apps",
        data={"apps": ["gluetun", "qbittorrent", "sonarr"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/login?apps=sonarr,gluetun,qbittorrent"


def test_posting_the_app_list_clears_a_saved_confirmation_and_brings_the_vpn_step_back(
    tmp_path: Path,
) -> None:
    """A confirmation from an earlier try must never outlive the choice of
    apps it was given for - posting the app list again clears it, even
    when qBittorrent is ticked right back in, so "Add your VPN"'s own
    question is asked fresh.
    """
    settings = _settings(tmp_path)
    save_without_vpn(settings.config_dir, now=datetime(2026, 9, 26, tzinfo=UTC))
    client = _client(settings)

    response = client.post("/setup/apps", data={"apps": ["qbittorrent"]}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/login?apps=gluetun,qbittorrent"
    assert without_vpn_confirmed(settings.config_dir) is False


def test_posting_nothing_rerenders_at_200_with_refusal_and_every_checkbox(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/setup/apps", data={}, follow_redirects=False)

    assert response.status_code == 200
    assert words.WIZARD_PICK_AT_LEAST_ONE in response.text
    checkboxes = _checkbox_inputs(response.text)
    assert [box["value"] for box in checkboxes] == [app.id for app in _OFFERED]
    assert all("checked" not in box for box in checkboxes)


# --- The Docker Desktop platform warning ------------------------------------


@pytest.mark.parametrize(
    ("status", "expect_warning"),
    [
        (
            DockerStatus(
                connected=True,
                platform_name="Docker Desktop 4.34.0 (165256)",
                kernel_version="6.10.11-linuxkit",
            ),
            True,
        ),
        (DockerStatus(connected=True, os_type="linux"), False),
        (DockerStatus(connected=False), False),
    ],
)
def test_platform_warning_appears_only_for_docker_desktop(
    tmp_path: Path, status: DockerStatus, expect_warning: bool
) -> None:
    client = _client(_settings(tmp_path), status)

    response = client.get("/setup/apps")
    # Jinja2 escapes the apostrophe in "it's" as an entity - unescape before
    # comparing against the plain-text constant from words.py.
    rendered = html.unescape(response.text)

    if expect_warning:
        assert words.WIZARD_PLATFORM_WARNING in rendered
        assert 'aria-hidden="true"' in response.text
    else:
        assert words.WIZARD_PLATFORM_WARNING not in rendered


# --- Accessibility floor ------------------------------------------------------


def test_nothing_on_the_screen_is_disabled_or_role_checkbox(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/apps")

    assert "disabled" not in response.text
    assert 'role="checkbox"' not in response.text


def test_each_checkbox_is_immediately_followed_by_the_card_face_span(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/apps")

    matches = re.findall(
        r'<input[^>]*type="checkbox"[^>]*>\s*<span class="app-card__face">', response.text
    )
    assert len(matches) == len(_OFFERED)


# --- wizard.css structure ----------------------------------------------------


def test_wizard_css_collapses_app_grid_to_one_column_at_640px() -> None:
    css = _WIZARD_CSS_PATH.read_text()

    assert "@media (max-width: 640px)" in css
    assert "grid-template-columns: 1fr" in css


def test_wizard_css_has_no_min_width_above_343px() -> None:
    css = _WIZARD_CSS_PATH.read_text()

    widths = [int(match) for match in re.findall(r"min-width:\s*(\d+)px", css)]
    assert all(width <= 343 for width in widths)


# --- An unavailable combination is refused, not silently accepted -----------


def test_posting_an_unavailable_combination_is_refused_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reason = "needs a search source first"
    rule = AppRule(kind="needs_any", app_ids=("a-search-source-nothing-ticks",), reason=reason)
    radarr = next(app for app in CATALOG if app.id == "radarr")
    patched_radarr = dataclasses.replace(radarr, rules=(rule,))
    patched_catalog = tuple(patched_radarr if app.id == "radarr" else app for app in CATALOG)
    monkeypatch.setattr("marrquee.catalog.CATALOG", patched_catalog)
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/apps", data={"apps": ["prowlarr", "radarr"]}, follow_redirects=False
    )

    assert response.status_code == 200
    assert words.wizard_app_unavailable("Radarr", reason) in html.unescape(response.text)


def test_posting_plex_and_jellyfin_together_is_refused_with_story_2s_wording(
    tmp_path: Path,
) -> None:
    """Ticking Plex and Jellyfin together is refused through the same
    `unavailable_reason` mechanism as any other `excludes_any` rule - no
    special-cased "one media server" check exists anywhere else.
    """
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/apps", data={"apps": ["plex", "jellyfin"]}, follow_redirects=False
    )

    assert response.status_code == 200
    assert words.wizard_app_unavailable("Plex", words.PLEX_EXCLUDES_JELLYFIN) in html.unescape(
        response.text
    )


def test_an_available_combination_is_never_refused(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/apps", data={"apps": ["prowlarr", "radarr"]}, follow_redirects=False
    )

    assert response.status_code == 303


# --- Setup closes once the Hub exists ---------------------------------------


def test_setup_apps_redirects_home_once_the_hub_exists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_finale_deploy(settings)
    client = _client(settings)

    get_response = client.get("/setup/apps", follow_redirects=False)
    post_response = client.post("/setup/apps", data={"apps": ["radarr"]}, follow_redirects=False)

    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/"
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/"
