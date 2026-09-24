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

from fastapi.testclient import TestClient

from marrquee import words
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.health import FakeLinkProbe
from marrquee.links import LinkCard, save_links
from marrquee.main import create_app
from marrquee.routes.api import HubStatusOut, HubTileOut, LinkTileOut
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

    assert arrays.keys() == {"STATUS_FIELDS", "APP_FIELDS", "LINK_FIELDS"}
    # Every array is non-empty, so a regex that silently matched nothing
    # can't pass this test by accident.
    assert all(arrays.values())

    assert arrays["STATUS_FIELDS"] <= set(HubStatusOut.model_fields)
    assert arrays["APP_FIELDS"] <= set(HubTileOut.model_fields)
    assert arrays["LINK_FIELDS"] <= set(LinkTileOut.model_fields)


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


def _client(settings: Settings) -> TestClient:
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine, link_probe=FakeLinkProbe())
    return TestClient(app)


def test_every_hook_the_script_queries_exists_on_the_rendered_hub(tmp_path: Path) -> None:
    script = _HUB_JS_PATH.read_text()
    hooks = _data_hooks(script)
    assert hooks, "expected the script to read at least one data-* hook"

    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(settings, _finale_snapshot(("sonarr",)))
    card = LinkCard(id="0" * 16, label="Router", url="http://192.168.1.1")
    save_links(settings.config_dir, [card])
    client = _client(settings)

    page = client.get(f"/?panel=edit&link={card.id}")

    for hook in hooks:
        assert hook in page.text, f"{hook!r} is read by hub.js but never rendered on the page"


def test_the_script_composes_no_hub_links_url() -> None:
    script = _HUB_JS_PATH.read_text()

    assert "/hub/links" not in script


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
