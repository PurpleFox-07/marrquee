"""Tests for `hub.js`, the Hub page's live layer.

The script is read as plain text, the same way `tests/test_deploy_script.py`
pins `deploy.js` - there is no browser automation in this project, and this
chunk does not add any. The FIRST test compares the script's own field
lists against the real API output models (`routes/api.py`), not the pure
view layer's own dataclasses, because those are what actually cross the
wire in a `GET /api/hub/status` frame. The second FIRST test (for the
panel) renders a real page and checks every `data-*` hook the script reads
by querySelector/getAttribute genuinely exists on it - the click-through
itself can't run here (no browser), so this is the closest proof available
that the wiring lines up.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marrquee import questions as questions_module
from marrquee import words
from marrquee.config import Settings
from marrquee.deploy import AppAdd, AppProgress, DeploySnapshot
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.health import FakeLinkProbe
from marrquee.links import LinkCard, save_links
from marrquee.login import save_login
from marrquee.main import create_app
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep
from marrquee.recyclarr import RecyclarrControl, SyncStatus
from marrquee.routes.api import HubInstallOut, HubStatusOut, HubTileOut, LinkTileOut
from marrquee.state import STATE_VERSION, InstallState, save_state, write_json_atomic
from marrquee.words import STATUS_CHIP_DONE

_HUB_JS_PATH = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "js" / "hub.js"
_HUB_HTML_PATH = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates" / "hub.html"
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates"

_FIELD_ARRAY_RE = re.compile(r"var\s+(\w+_FIELDS)\s*=\s*\[([^\]]*)\];")

# Every quoted JS string literal in the script - comments use `//`/`/* */`,
# never quotes, so this can never pick up prose, only real string values
# the script itself builds selectors, keys or messages from.
_STRING_LITERAL_RE = re.compile(r"(['\"])((?:\\.|(?!\1).)*)\1")

# A `data-...` token embedded in a quoted literal, with its `="value"` kept
# only when the literal spells the value out too (a selector built by
# concatenation, like `'[data-app="' + id + '"]'`, only ever contributes
# the bare attribute name - which is still a fair thing to check for on
# the page, since the concatenated value is a real id at render time).
_DATA_HOOK_RE = re.compile(r'data-[a-z-]+(?:="[^"]*")?')


def _field_arrays(script: str) -> dict[str, set[str]]:
    arrays: dict[str, set[str]] = {}
    for name, body in _FIELD_ARRAY_RE.findall(script):
        arrays[name] = {item.strip().strip("\"'") for item in body.split(",") if item.strip()}
    return arrays


def _data_hooks(script: str) -> set[str]:
    hooks: set[str] = set()
    for _quote, content in _STRING_LITERAL_RE.findall(script):
        if "data-" not in content:
            continue
        hooks.update(_DATA_HOOK_RE.findall(content))
    return hooks


# --- FIRST TEST: the script's field lists match the API's output shape ------


def test_the_script_names_only_fields_on_the_hub_output_models() -> None:
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert arrays.keys() == {"STATUS_FIELDS", "APP_FIELDS", "LINK_FIELDS", "INSTALL_FIELDS"}
    # Every array is non-empty, so a regex that silently matched nothing
    # can't pass this test by accident.
    assert all(arrays.values())

    assert arrays["STATUS_FIELDS"] <= set(HubStatusOut.model_fields)
    assert arrays["APP_FIELDS"] <= set(HubTileOut.model_fields)
    assert arrays["LINK_FIELDS"] <= set(LinkTileOut.model_fields)
    assert arrays["INSTALL_FIELDS"] <= set(HubInstallOut.model_fields)


def test_status_fields_include_login_banner() -> None:
    """The reload guard needs `login_banner` in every polled frame - without
    it in `STATUS_FIELDS`, a future field-name drift on `HubStatusOut`
    could silently stop being checked here.
    """
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert "login_banner" in arrays["STATUS_FIELDS"]


def test_status_fields_include_running_without_vpn() -> None:
    """The badge appearing or disappearing is its own shape change - without
    `running_without_vpn` in `STATUS_FIELDS`, the reload guard could never
    see a move start or finish.
    """
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert "running_without_vpn" in arrays["STATUS_FIELDS"]


def test_status_fields_include_drive_note() -> None:
    """The drive note has to repaint on every poll - without `drive_note`
    in `STATUS_FIELDS`, `paint` would never see the field it sets
    `data-drive-note` and the note's own text from.
    """
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert "drive_note" in arrays["STATUS_FIELDS"]


def test_paint_toggles_the_drive_note_attribute_and_its_text() -> None:
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function paint\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a paint function in hub.js"
    body = match.group(0)

    assert "root.dataset.driveNote = payload.drive_note" in body
    assert 'setText("drive-note-text", payload.drive_note' in body


def test_drive_note_never_reaches_the_reload_guard_signature() -> None:
    """The note toggles live, in place - it never reloads the page, so
    `structureSignature` and the reload guard built from it must never
    fold `drive_note` in.
    """
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function structureSignature\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a structureSignature function in hub.js"
    assert "drive" not in match.group(0).lower()

    guard_match = re.search(
        r"function reloadIfStructureChanged\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL
    )
    assert guard_match is not None, "expected a reloadIfStructureChanged function in hub.js"
    assert "drive_note" not in guard_match.group(0)


def test_app_fields_include_paused() -> None:
    """A paused downloader tile has to repaint on every poll - without
    `paused` in `APP_FIELDS`, `paintTile` would never see the field it sets
    `data-paused` from.
    """
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert "paused" in arrays["APP_FIELDS"]


def test_app_fields_include_sync_state() -> None:
    """Recyclarr's own poster has to repaint on every poll - without
    `sync_state` in `APP_FIELDS`, `paintTile` would never see the field it
    sets `data-sync-state` and the Sync now button's own class from.
    """
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert "sync_state" in arrays["APP_FIELDS"]


def test_sync_state_never_reaches_the_reload_guard_signature() -> None:
    """`actions` stays `"sync"` through every up state, syncing included -
    `sync_state` itself must never fold into `structureSignature`, or a
    sync starting/finishing would reload the page for no reason.
    """
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function structureSignature\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a structureSignature function in hub.js"

    assert "sync_state" not in match.group(0)
    assert "syncState" not in match.group(0)


def test_the_reload_guard_signature_folds_in_the_login_banner() -> None:
    """`structureSignature` has to actually combine both inputs - a version
    that quietly went back to `return actions;` would still poll and paint
    fine, and every other test here would stay green, but a login state
    that flips the page's whole banner section (choose -> set, say) would
    never trigger the one-time reload that repaints it.
    """
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function structureSignature\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a structureSignature function in hub.js"
    body = match.group(0)

    assert re.search(r"return\s+actions\b.*loginBanner", body, re.DOTALL), (
        "structureSignature must combine both actions and loginBanner in its return value"
    )
    # A third argument folds in the badge's own shape (running without a
    # VPN) - a version that dropped back to the two-argument form would
    # still combine actions and loginBanner fine, but a move starting or
    # finishing would never trigger the reload that repaints the badge.
    assert re.search(r"return\s+actions\b.*loginBanner.*runningWithoutVpn", body, re.DOTALL), (
        "structureSignature must also combine runningWithoutVpn in its return value"
    )

    # And the reload guard must actually call it with the *incoming* login
    # banner (the payload's) on one side and the *page's own* current one
    # (`data-login-banner`, via `root.dataset.loginBanner`) on the other -
    # comparing two signatures built from the same side would never detect
    # a change at all. Same for the badge's own field.
    guard_match = re.search(
        r"function reloadIfStructureChanged\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL
    )
    assert guard_match is not None, "expected a reloadIfStructureChanged function in hub.js"
    guard_body = guard_match.group(0)
    assert "payload.login_banner" in guard_body
    assert "root.dataset.loginBanner" in guard_body
    assert "payload.running_without_vpn" in guard_body
    assert "root.dataset.runningWithoutVpn" in guard_body


# --- FIRST TEST: every hook the panel script queries exists on the page ----


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


def _finale_snapshot(app_ids: tuple[str, ...]) -> DeploySnapshot:
    apps = tuple(
        AppProgress(
            app_id=app_id,
            name=app_id,
            state="done",
            chip=STATUS_CHIP_DONE,
            line="Ready",
            note=None,
            port=8080,
        )
        for app_id in app_ids
    )
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


def _client(
    settings: Settings,
    *,
    engine: FakeDockerEngine | None = None,
    recyclarr: RecyclarrControl | None = None,
) -> TestClient:
    if engine is None:
        engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(
        settings=settings, engine=engine, link_probe=FakeLinkProbe(), recyclarr=recyclarr
    )
    return TestClient(app)


class _FakeRecyclarr:
    """A fixed `SyncStatus`, so the sync-fixture render below always draws
    the same tile - the sync request count is never read by this file.
    """

    def __init__(self, status: SyncStatus) -> None:
        self._status = status

    def request_sync(self) -> object:
        return None

    async def status(self) -> SyncStatus:
        return self._status


def test_every_hook_the_script_queries_exists_on_the_rendered_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRST TEST - every `data-*` selector `hub.js` reads by querySelector
    or getAttribute must exist on at least one real render: the edit pane
    (with a saved link card), the install pane (with a fixture question
    step registered, so the form/step/back/next/submit/refusal hooks the
    install flow needs exist too) and a page with an app mid-add (for
    `data-add-state`, which only ever appears on an adding tile).
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

    script = _HUB_JS_PATH.read_text()
    hooks = _data_hooks(script)
    assert hooks, "expected the script to read at least one data-* hook"

    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(settings, _finale_snapshot(("prowlarr", "sonarr")))
    # A saved login, so the install pane renders its fixture question step
    # (and every hook the install flow needs) instead of the "choose your
    # login first" sentence this story adds ahead of it.
    save_login(settings.config_dir, "owner", "s3cret-password-1", honor_reset=None)
    card = LinkCard(id="0" * 16, label="Router", url="http://192.168.1.1")
    save_links(settings.config_dir, [card])
    client = _client(settings)

    edit_page = client.get(f"/?panel=edit&link={card.id}").text
    install_page = client.get("/?panel=install").text

    adding_settings = _settings(tmp_path / "adding")
    save_state(adding_settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(
        adding_settings,
        dataclasses.replace(
            _finale_snapshot(("prowlarr", "sonarr")),
            adding=AppAdd(
                app_id="radarr",
                purpose="add",
                state="starting",
                line="Starting Radarr",
                note=None,
                failure=None,
                wiring=(),
                compose_ran=False,
                started_at="2026-09-24T00:00:00+00:00",
            ),
        ),
    )
    adding_page = _client(adding_settings).get("/").text

    # A running Recyclarr, for `data-sync-state` and the Sync now button's
    # own `data-role="sync-now"` - neither ever appears on any other tile.
    sync_settings = _settings(tmp_path / "sync")
    save_state(sync_settings.config_dir, _install_state(("recyclarr",)))
    _write_snapshot(sync_settings, _finale_snapshot(("recyclarr",)))
    sync_engine = FakeDockerEngine(
        DockerStatus(connected=True, version="27.3.1"),
        containers={
            "recyclarr": ContainerSnapshot(
                name="recyclarr",
                exists=True,
                state="running",
                exit_code=None,
                image=None,
                detail=None,
            )
        },
    )
    sync_recyclarr = _FakeRecyclarr(
        SyncStatus(syncing=False, last=None, start_failed=False, run_failed=False)
    )
    sync_page = _client(sync_settings, engine=sync_engine, recyclarr=sync_recyclarr).get("/").text

    rendered = (edit_page, install_page, adding_page, sync_page)
    for hook in hooks:
        assert any(hook in page for page in rendered), (
            f"{hook!r} is read by hub.js but never rendered on any page"
        )


def test_the_script_composes_no_hub_links_url() -> None:
    script = _HUB_JS_PATH.read_text()

    assert "/hub/links" not in script


def test_the_install_click_listener_never_reaches_for_data_sign_in() -> None:
    """The Plex sign-in button posts natively through its own
    `formaction`/`formmethod` - it carries none of `bindInstall`'s own
    trigger attributes, so its click must never be intercepted the way
    every other install control's is.
    """
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function bindInstall\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a bindInstall function in hub.js"
    body = match.group(0)

    assert "data-sign-in" not in body
    for selector in (
        "[data-install-app]",
        "[data-install-next]",
        "[data-install-back]",
        "[data-install-submit]",
    ):
        assert selector in body


def test_the_install_collector_reads_select_values_too() -> None:
    """A `list` field (the VPN provider dropdown) is a `<select>`, not an
    `<input>` - `collectAnswers` has to query both, or a provider choice
    would silently never reach `POST /api/hub/apps/{id}/install`.
    """
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function collectAnswers\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a collectAnswers function in hub.js"

    assert "select[name]" in match.group(0)


def test_the_script_never_removes_href_from_a_link_card() -> None:
    script = _HUB_JS_PATH.read_text()
    match = re.search(r"function paintLink\([^)]*\)\s*\{.*?\n  \}", script, re.DOTALL)
    assert match is not None, "expected a paintLink function in hub.js"

    assert "removeAttribute" not in match.group(0)


# --- No English lives in the script ------------------------------------------


def test_the_script_contains_no_user_facing_sentence_of_twelve_characters_or_more() -> None:
    script = _HUB_JS_PATH.read_text()

    for name in words.WORDS_INVENTORY:
        value = getattr(words, name)
        if isinstance(value, str) and len(value) >= 12:
            assert value not in script, f"{name} ({value!r}) leaked into hub.js"


def test_the_script_never_uses_innerhtml() -> None:
    script = _HUB_JS_PATH.read_text()

    assert "innerHTML" not in script


# --- Polling: no EventSource, driven by data-poll-ms, paused when hidden ----


def test_the_script_reads_its_interval_from_data_poll_ms() -> None:
    script = _HUB_JS_PATH.read_text()

    assert "dataset.pollMs" in script


def test_the_script_uses_no_eventsource() -> None:
    script = _HUB_JS_PATH.read_text()

    assert "EventSource" not in script


def test_the_script_listens_for_visibilitychange() -> None:
    script = _HUB_JS_PATH.read_text()

    assert "visibilitychange" in script


def test_the_script_fetches_the_status_endpoint_with_no_store_caching() -> None:
    script = _HUB_JS_PATH.read_text()

    assert 'fetch("/api/hub/status"' in script
    assert "no-store" in script


# --- Loading ------------------------------------------------------------------


def test_the_script_is_loaded_with_defer_and_only_from_hub_html() -> None:
    hub_html = _HUB_HTML_PATH.read_text()

    assert "<script defer" in hub_html
    assert "js/hub.js" in hub_html

    for template_path in _TEMPLATES_DIR.rglob("*.html"):
        if template_path == _HUB_HTML_PATH:
            continue
        assert "js/hub.js" not in template_path.read_text()
