"""Tests for the Diagnostics page's "Last problem" section and its Copy
button.

`diagnostics.js` is read as plain text, the same way `test_wizard_drive.py`
checks `wizard.js` - there is no browser runner in this suite, so the
unverifiable condition (does the fallback copy really work on the owner's
plain-http NAS?) is pinned here as "the script has all three steps", and
proven for real on the NAS in Chunk 7's owner walk.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from marrquee import words
from marrquee.config import Settings
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app

_DIAGNOSTICS_JS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "js" / "diagnostics.js"
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings) -> TestClient:
    app = create_app(
        settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    )
    return TestClient(app)


# --- diagnostics.js: read as text, the same shape as wizard.js's own tests --


def test_the_script_tries_the_clipboard_then_the_fallback_copy_then_selects_and_explains() -> None:
    """FIRST TEST - the unverifiable condition: all three steps must be present."""
    script = _DIAGNOSTICS_JS_PATH.read_text()

    assert "isSecureContext" in script
    assert "navigator.clipboard" in script
    assert 'execCommand("copy")' in script
    assert ".select()" in script


def test_the_script_does_nothing_without_both_the_source_and_the_button() -> None:
    script = _DIAGNOSTICS_JS_PATH.read_text()

    assert 'querySelector("[data-copy-source]")' in script
    assert 'querySelector("[data-copy-button]")' in script


def test_the_script_never_uses_innerhtml() -> None:
    script = _DIAGNOSTICS_JS_PATH.read_text()

    assert "innerHTML" not in script


def test_diagnostics_js_contains_no_words_py_sentence() -> None:
    script = _DIAGNOSTICS_JS_PATH.read_text()

    for name in words.WORDS_INVENTORY:
        value = getattr(words, name)
        if isinstance(value, str) and len(value) >= 12:
            assert value not in script, f"{name} ({value!r}) leaked into diagnostics.js"


# --- the page: with a problem on file ----------------------------------------


def test_the_last_problem_shows_the_saved_text_and_a_copy_button(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "last-failure.txt").write_text("Sonarr couldn't start.\n")
    client = _client(settings)

    response = client.get("/diagnostics")

    assert response.status_code == 200
    assert "Sonarr couldn&#39;t start." in response.text
    assert "data-copy-source" in response.text
    assert "data-copy-button" in response.text
    assert words.COPY_BUTTON in response.text


def test_the_last_problem_text_is_escaped_not_run_as_html(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "last-failure.txt").write_text("<script>alert(1)</script>")
    client = _client(settings)

    response = client.get("/diagnostics")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_the_copy_button_starts_hidden_so_it_never_shows_without_javascript(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "last-failure.txt").write_text("Sonarr couldn't start.")
    client = _client(settings)

    response = client.get("/diagnostics")

    assert "data-copy-button" in response.text
    button_start = response.text.index("data-copy-button")
    button_tag_end = response.text.index(">", button_start)
    assert "hidden" in response.text[button_start:button_tag_end]


def test_the_section_carries_the_last_problem_anchor(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.get("/diagnostics")

    assert 'id="last-problem"' in response.text
    assert 'aria-labelledby="last-problem-title"' in response.text
    assert 'id="last-problem-title"' in response.text


# --- the page: with nothing on file -------------------------------------------


def test_with_nothing_saved_the_section_shows_the_empty_sentence_and_no_copy_button(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/diagnostics")

    assert words.LAST_PROBLEM_EMPTY in response.text
    assert "data-copy-button" not in response.text
    assert "data-copy-source" not in response.text


def test_a_whitespace_only_problem_file_counts_as_nothing_saved(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.config_dir.mkdir(parents=True)
    (settings.config_dir / "last-failure.txt").write_text("   \n\n  ")
    client = _client(settings)

    response = client.get("/diagnostics")

    assert words.LAST_PROBLEM_EMPTY in response.text
    assert "data-copy-button" not in response.text


def test_a_missing_config_dir_shows_the_empty_state_not_a_500(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.get("/diagnostics")

    assert response.status_code == 200
    assert words.LAST_PROBLEM_EMPTY in response.text


# --- the moved .btn-ghost rule -------------------------------------------------


def test_the_wizards_back_button_still_renders_with_btn_ghost_after_the_move(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert 'class="btn-ghost"' in response.text


def test_btn_ghost_no_longer_lives_in_wizard_css() -> None:
    wizard_css_path = (
        Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "css" / "wizard.css"
    )
    app_css_path = (
        Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "css" / "app.css"
    )

    assert ".btn-ghost" not in wizard_css_path.read_text()
    assert ".btn-ghost {" in app_css_path.read_text()
