"""Tests for the wizard's own question pages - one per registered
`QuestionStep`, inserted between "Your apps" and "Your drive".

No catalog app has a question yet, so every test here monkeypatches
`marrquee.questions.QUESTION_STEPS` with a fixture step, the same pattern
`tests/test_questions.py` and `tests/test_api.py` already use.
"""

from __future__ import annotations

import dataclasses
import html
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import marrquee.questions as questions_module
from marrquee import words
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.questions import (
    QuestionCheck,
    QuestionField,
    QuestionStep,
    load_answers,
    save_step_answers,
)
from marrquee.state import write_json_atomic


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings) -> TestClient:
    status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _write_finale_deploy(settings: Settings) -> None:
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
        headline="Now showing",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


def _refusing_step(app_id: str = "radarr") -> QuestionStep:
    return QuestionStep(
        app_id=app_id,
        step_id="fixture",
        title="Fixture questions",
        lede="A fixture step for the test.",
        fields=(QuestionField(name="name", label="Name", kind="text"),),
        check=lambda answers: QuestionCheck(
            ok=bool(answers.get("name")),
            answers=answers,
            problem=None if answers.get("name") else "Name is required.",
            field=None if answers.get("name") else "name",
        ),
    )


# --- GET: an unknown app/step bounces to the apps screen ---------------------


def test_get_unknown_step_redirects_to_apps(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get(
        "/setup/questions/radarr/no-such-step", params={"apps": "radarr"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


def test_get_a_hand_typed_asked_with_step_without_its_partner_redirects_to_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`find_step` matches on ids alone, so a step that is only ever
    registered ALONGSIDE another app (Recyclarr's quality question, owned
    by Sonarr) must still be refused when that other app was never
    ticked - a hand-typed URL must not render a step nobody actually
    registered for this run.
    """
    step = dataclasses.replace(_refusing_step(app_id="sonarr"), asked_with="recyclarr")
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (step,))
    client = _client(_settings(tmp_path))

    response = client.get(
        "/setup/questions/sonarr/fixture", params={"apps": "sonarr"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


def test_get_a_step_for_an_app_not_ticked_redirects_to_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_refusing_step(),))
    client = _client(_settings(tmp_path))

    response = client.get(
        "/setup/questions/radarr/fixture", params={"apps": "prowlarr"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


def test_get_renders_the_pill_row_with_the_question_as_pill_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_refusing_step(),))
    client = _client(_settings(tmp_path))

    response = client.get("/setup/questions/radarr/fixture", params={"apps": "radarr"})

    assert response.status_code == 200
    assert 'data-question-step="radarr:fixture"' in response.text
    assert "Fixture questions" in response.text
    # Pill 2 is this step, current; pill 3 is "Your drive".
    assert response.text.index("Fixture questions") < response.text.index(words.WIZARD_STEP_DRIVE)
    assert 'href="/setup/apps?apps=radarr"' in response.text


# --- POST: refuse and re-render, or save and move on -------------------------


def test_posting_a_bad_answer_rerenders_the_step_with_its_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_refusing_step(),))
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.post(
        "/setup/questions/radarr/fixture",
        data={"apps": "radarr", "name": ""},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "Name is required." in response.text
    assert load_answers(settings.config_dir) == {}


def test_posting_a_good_answer_saves_and_moves_on_to_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_refusing_step(),))
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.post(
        "/setup/questions/radarr/fixture",
        data={"apps": "radarr", "name": "a name"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/drive?apps=radarr"
    assert load_answers(settings.config_dir) == {"radarr": {"name": "a name"}}


def test_posting_a_good_answer_moves_to_the_next_registered_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _refusing_step(app_id="prowlarr")
    second = _refusing_step(app_id="radarr")
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (first, second))
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/questions/prowlarr/fixture",
        data={"apps": "prowlarr,radarr", "name": "a name"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/questions/radarr/fixture?apps=prowlarr,radarr"


def test_qbittorrent_ticked_asks_gluetuns_vpn_step_before_its_own_seeding(
    tmp_path: Path,
) -> None:
    """The real (never monkeypatched) registry: with `qbittorrent` ticked
    (the wizard already unioned Gluetun in), Gluetun's own VPN question is
    pill two - never qBittorrent's seeding question - and a valid VPN
    answer moves on to qBittorrent's own seeding step next.
    """
    client = _client(_settings(tmp_path))

    response = client.get("/setup/questions/gluetun/vpn", params={"apps": "gluetun,qbittorrent"})

    assert response.status_code == 200
    assert 'data-question-step="gluetun:vpn"' in response.text
    assert "qbittorrent:seeding" not in response.text

    posted = client.post(
        "/setup/questions/gluetun/vpn",
        data={
            "apps": "gluetun,qbittorrent",
            "provider": "mullvad",
            "vpn_type": "openvpn",
            "openvpn_user": "ci-user-7f3a",
            "openvpn_password": "pw-9d1c",
        },
        follow_redirects=False,
    )

    assert posted.status_code == 303
    assert posted.headers["location"] == (
        "/setup/questions/qbittorrent/seeding?apps=gluetun,qbittorrent"
    )


def test_the_escape_link_is_on_the_vpn_question_only_when_qbittorrent_is_chosen(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    with_qbittorrent = client.get(
        "/setup/questions/gluetun/vpn", params={"apps": "gluetun,qbittorrent"}
    )
    assert 'data-role="without-vpn-link"' in with_qbittorrent.text
    assert 'href="/setup/without-vpn?apps=gluetun,qbittorrent"' in with_qbittorrent.text
    assert "data-panel-choice" not in with_qbittorrent.text

    without_qbittorrent = client.get("/setup/questions/gluetun/vpn", params={"apps": "gluetun"})
    assert 'data-role="without-vpn-link"' not in without_qbittorrent.text


def test_a_password_field_never_carries_a_value_even_with_a_saved_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="Fixture questions",
        lede="",
        fields=(QuestionField(name="token", label="Token", kind="password"),),
        check=lambda answers: QuestionCheck(ok=True, answers=answers, problem=None, field=None),
    )
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (step,))
    settings = _settings(tmp_path)
    client = _client(settings)
    client.post(
        "/setup/questions/radarr/fixture",
        data={"apps": "radarr", "token": "super-secret"},
        follow_redirects=False,
    )

    response = client.get("/setup/questions/radarr/fixture", params={"apps": "radarr"})

    assert "super-secret" not in response.text
    assert 'type="password"' in response.text


# --- Setup closes once the Hub exists ---------------------------------------


def test_setup_questions_redirects_home_once_the_hub_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_refusing_step(),))
    settings = _settings(tmp_path)
    _write_finale_deploy(settings)
    client = _client(settings)

    get_response = client.get(
        "/setup/questions/radarr/fixture", params={"apps": "radarr"}, follow_redirects=False
    )
    post_response = client.post(
        "/setup/questions/radarr/fixture",
        data={"apps": "radarr", "name": "a name"},
        follow_redirects=False,
    )

    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/"
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/"


# --- questions.js keeps a `list` field's guide link in step with it ---------


def test_the_question_page_loads_questions_js_with_defer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_refusing_step(),))
    client = _client(_settings(tmp_path))

    response = client.get("/setup/questions/radarr/fixture", params={"apps": "radarr"})

    assert response.status_code == 200
    assert "<script defer" in response.text
    assert "js/questions.js" in response.text


# --- Plex's own real step: a sign-in field, not a text box -------------------
#
# Uses the real, registered `PLEX_STEP` rather than a fixture - the whole
# point here is `check_step`'s own "a posted value is never read" rule for a
# `sign_in` field, and the actual refusal/confirmation wording.


def test_wizard_next_without_signing_in_is_refused_on_plex_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    response = client.post(
        "/setup/questions/plex/sign-in",
        data={"apps": "plex", "plex_account": "typed-by-a-browser"},
    )

    assert response.status_code == 200
    assert words.PLEX_PROBLEM_SIGN_IN_FIRST in html.unescape(response.text)
    assert load_answers(settings.config_dir).get("plex", {}) == {}


def test_a_saved_sign_in_lets_the_wizard_step_move_on(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_step_answers(settings.config_dir, "plex", {"plex_account": "ryan"})
    client = _client(settings)

    response = client.post(
        "/setup/questions/plex/sign-in", data={"apps": "plex"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/drive?apps=plex"


def test_the_plex_step_shows_signed_in_as_once_a_sign_in_is_saved(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_step_answers(settings.config_dir, "plex", {"plex_account": "ryan"})
    client = _client(settings)

    response = client.get("/setup/questions/plex/sign-in", params={"apps": "plex"})

    assert response.status_code == 200
    assert words.plex_signed_in_as("ryan") in html.unescape(response.text)
    assert "<input" not in _fieldset(response.text, "plex:sign-in")


def test_an_abandoned_sign_in_shows_the_wizard_refusal(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get(
        "/setup/questions/plex/sign-in", params={"apps": "plex", "sign_in": "failed"}
    )

    assert response.status_code == 200
    assert words.PLEX_SIGN_IN_DIDNT_FINISH in html.unescape(response.text)


def _fieldset(page_html: str, step_key: str) -> str:
    match = re.search(
        rf'<fieldset[^>]*data-question-step="{re.escape(step_key)}".*?</fieldset>',
        page_html,
        re.DOTALL,
    )
    assert match is not None, f"no fieldset found for {step_key!r}"
    return match.group(0)
