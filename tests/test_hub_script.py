"""Tests for `hub.js`, the Hub page's live layer.

The script is read as plain text, the same way `tests/test_deploy_script.py`
pins `deploy.js` - there is no browser automation in this project, and this
chunk does not add any. The FIRST test compares the script's own field
lists against the real API output models (`routes/api.py`), not the pure
view layer's own dataclasses, because those are what actually cross the
wire in a `GET /api/hub/status` frame.
"""

from __future__ import annotations

import re
from pathlib import Path

from marrquee import words
from marrquee.routes.api import HubStatusOut, HubTileOut

_HUB_JS_PATH = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "js" / "hub.js"
_HUB_HTML_PATH = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates" / "hub.html"
_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates"

_FIELD_ARRAY_RE = re.compile(r"var\s+(\w+_FIELDS)\s*=\s*\[([^\]]*)\];")


def _field_arrays(script: str) -> dict[str, set[str]]:
    arrays: dict[str, set[str]] = {}
    for name, body in _FIELD_ARRAY_RE.findall(script):
        arrays[name] = {item.strip().strip("\"'") for item in body.split(",") if item.strip()}
    return arrays


# --- FIRST TEST: the script's field lists match the API's output shape ------


def test_the_script_names_only_fields_on_the_hub_output_models() -> None:
    script = _HUB_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert arrays.keys() == {"STATUS_FIELDS", "APP_FIELDS"}
    # Every array is non-empty, so a regex that silently matched nothing
    # can't pass this test by accident.
    assert all(arrays.values())

    assert arrays["STATUS_FIELDS"] <= set(HubStatusOut.model_fields)
    assert arrays["APP_FIELDS"] <= set(HubTileOut.model_fields)


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
