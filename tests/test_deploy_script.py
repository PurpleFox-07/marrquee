"""Tests for `deploy.js`, the Deploy page's live layer.

The script is read as plain text, the same way `tests/test_wizard_drive.py`
pins `wizard.js` - there is no browser automation in this project, and this
chunk does not add any. The FIRST test compares the script's own field
lists against the real API output models (`routes/api.py`), not the
engine's internal dataclasses, because those are what actually cross the
wire in an SSE frame.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from marrquee import words
from marrquee.config import Settings
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.routes.api import AppProgressOut, DeploySnapshotOut, FailureOut, WiringStepOut
from marrquee.state import STATE_VERSION, InstallState, save_state

_DEPLOY_JS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "js" / "deploy.js"
)
_DEPLOY_HTML_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates" / "deploy.html"
)

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates"

_FIELD_ARRAY_RE = re.compile(r"var\s+(\w+_FIELDS)\s*=\s*\[([^\]]*)\];")
_DATA_ROLE_RE = re.compile(r'\[data-role="([\w-]+)"\]')


def _field_arrays(script: str) -> dict[str, set[str]]:
    arrays: dict[str, set[str]] = {}
    for name, body in _FIELD_ARRAY_RE.findall(script):
        arrays[name] = {item.strip().strip("\"'") for item in body.split(",") if item.strip()}
    return arrays


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _client(settings: Settings) -> TestClient:
    status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _install_state(app_ids: tuple[str, ...] = ("prowlarr", "sonarr")) -> InstallState:
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


# --- FIRST TEST: the script's field lists match the API's output shape ------


def test_the_script_names_only_fields_that_exist_on_the_apis_output_models() -> None:
    script = _DEPLOY_JS_PATH.read_text()
    arrays = _field_arrays(script)

    assert arrays.keys() == {
        "SNAPSHOT_FIELDS",
        "APP_FIELDS",
        "WIRING_FIELDS",
        "FAILURE_FIELDS",
    }
    # Every array is non-empty, so a regex that silently matched nothing
    # can't pass this test by accident.
    assert all(arrays.values())

    assert arrays["SNAPSHOT_FIELDS"] <= set(DeploySnapshotOut.model_fields)
    assert arrays["APP_FIELDS"] <= set(AppProgressOut.model_fields)
    assert arrays["WIRING_FIELDS"] <= set(WiringStepOut.model_fields)
    assert arrays["FAILURE_FIELDS"] <= set(FailureOut.model_fields)


# --- No English lives in the script ------------------------------------------


def test_the_script_contains_no_user_facing_sentence_of_twelve_characters_or_more() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    for name in words.WORDS_INVENTORY:
        value = getattr(words, name)
        if isinstance(value, str) and len(value) >= 12:
            assert value not in script, f"{name} ({value!r}) leaked into deploy.js"


def test_the_script_never_uses_innerhtml() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    assert "innerHTML" not in script


# --- The page carries every data-role the script addresses ------------------


def test_the_page_exposes_every_data_role_the_script_addresses(tmp_path: Path) -> None:
    script = _DEPLOY_JS_PATH.read_text()
    roles = set(_DATA_ROLE_RE.findall(script))
    assert roles

    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state())
    client = _client(settings)

    response = client.get("/deploy")

    for role in roles:
        assert f'data-role="{role}"' in response.text


# --- The live connection ------------------------------------------------------


def test_the_script_opens_exactly_one_eventsource_at_the_events_path_only_when_live() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    assert script.count("new EventSource(") == 1
    assert 'new EventSource("/api/deploy/events")' in script
    assert 'dataset.live === "true"' in script


def test_the_script_closes_the_stream_at_finale_and_at_error() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    assert ".close()" in script
    assert '"finale"' in script
    assert '"error"' in script


def test_the_announce_region_is_only_written_when_the_headline_changes() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    assert script.count("lastAnnounced") >= 2


# --- The wiring beat -----------------------------------------------------------


def test_the_linking_rule_reads_involved_from_the_last_wiring_row() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    assert ".involved" in script
    assert "wiring.length - 1" in script
    assert '"wiring"' in script


def test_the_wiring_count_template_reaches_the_script_as_a_data_attribute() -> None:
    script = _DEPLOY_JS_PATH.read_text()

    assert "countTemplate" in script
    assert '.replace("{index}"' in script
    assert '.replace("{total}"' in script


# --- Loading ------------------------------------------------------------------


def test_the_script_is_loaded_with_defer_and_only_from_deploy_html() -> None:
    deploy_html = _DEPLOY_HTML_PATH.read_text()

    assert "<script defer" in deploy_html
    assert "js/deploy.js" in deploy_html

    for template_path in _TEMPLATES_DIR.rglob("*.html"):
        if template_path == _DEPLOY_HTML_PATH:
            continue
        assert "js/deploy.js" not in template_path.read_text()
