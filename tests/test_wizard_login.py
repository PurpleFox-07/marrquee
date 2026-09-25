"""Tests for the wizard's login screen - the one username and password
every app Marrquee installs will use, asked right after "Your apps" (pill
2) and before any per-app question or the drive screen.

No catalog app has a question of its own in this story, so `apps=sonarr`
below walks straight from the login step to `/setup/drive` - the same
"no question steps registered" shape every other wizard test in this
codebase already assumes.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from marrquee import words
from marrquee.config import Settings
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.login import load_login
from marrquee.main import create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings) -> TestClient:
    status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _login_form(page_html: str) -> str:
    start = page_html.index('action="/setup/login"')
    return page_html[start : page_html.index("</form>", start)]


# --- The guard: nothing reaches drive without a saved login ------------------


def test_drive_waits_for_a_login(tmp_path: Path) -> None:
    """FIRST TEST (Pre-Flight walk) - no `login.json` yet, so `/setup/drive`
    sends the owner to the login step instead. A good POST there saves the
    login at generation 1 and continues straight on to drive, since no
    question step is registered for Sonarr.
    """
    settings = _settings(tmp_path)
    client = _client(settings)

    drive_response = client.get("/setup/drive", params={"apps": "sonarr"}, follow_redirects=False)
    assert drive_response.status_code == 303
    assert drive_response.headers["location"] == "/setup/login?apps=sonarr"

    login_response = client.post(
        "/setup/login",
        data={
            "apps": "sonarr",
            "username": "Owner",
            "password": "s3cret-password-1",
            "password_again": "s3cret-password-1",
        },
        follow_redirects=False,
    )

    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/setup/drive?apps=sonarr"
    record = load_login(settings.config_dir)
    assert record.login is not None
    assert record.login.username == "owner"  # lowered, per the login rules
    assert record.login.generation == 1


def test_a_refused_login_rerenders_at_200_with_the_username_kept_and_saves_nothing(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.post(
        "/setup/login",
        data={
            "apps": "sonarr",
            "username": "Owner",
            "password": "short",
            "password_again": "short",
        },
    )

    assert response.status_code == 200
    assert words.LOGIN_PROBLEM_PASSWORD_SHORT in response.text
    form = _login_form(response.text)
    assert 'value="owner"' in form
    password_inputs = re.findall(r'<input[^>]*type="password"[^>]*>', form)
    assert len(password_inputs) == 2
    assert all("value=" not in field for field in password_inputs)
    assert load_login(settings.config_dir).login is None


def test_returning_to_the_login_step_with_a_login_saved_keeps_it_when_both_boxes_are_blank(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)
    client.post(
        "/setup/login",
        data={
            "apps": "sonarr",
            "username": "owner",
            "password": "s3cret-password-1",
            "password_again": "s3cret-password-1",
        },
        follow_redirects=False,
    )

    get_response = client.get("/setup/login", params={"apps": "sonarr"})
    assert 'value="owner"' in _login_form(get_response.text)

    post_response = client.post(
        "/setup/login",
        data={"apps": "sonarr", "username": "owner", "password": "", "password_again": ""},
        follow_redirects=False,
    )

    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/setup/drive?apps=sonarr"
    record = load_login(settings.config_dir)
    assert record.login is not None
    assert record.login.generation == 2
    assert record.login.password == "s3cret-password-1"


# --- The progress row: four pills, "Your login" second and current ----------


def test_login_screen_pills_read_apps_your_login_drive_deploy(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/login", params={"apps": "sonarr"})

    start = response.text.index('class="step-pills"')
    nav = response.text[start : response.text.index("</nav>", start)]
    assert nav.index(words.WIZARD_STEP_APPS) < nav.index(words.WIZARD_STEP_LOGIN)
    assert nav.index(words.WIZARD_STEP_LOGIN) < nav.index(words.WIZARD_STEP_DRIVE)
    assert nav.index(words.WIZARD_STEP_DRIVE) < nav.index(words.WIZARD_STEP_DEPLOY)
    assert f'aria-current="step">2 &middot; {words.WIZARD_STEP_LOGIN}' in nav


def test_login_screen_names_the_plex_exception_and_has_a_back_link(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/login", params={"apps": "sonarr,prowlarr"})

    assert words.LOGIN_PLEX_NOTE in response.text
    assert 'href="/setup/apps?apps=prowlarr,sonarr"' in response.text
