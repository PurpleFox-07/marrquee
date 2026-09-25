"""Tests for the wizard's own question pages - one per registered
`QuestionStep`, inserted between "Your apps" and "Your drive".

No catalog app has a question yet, so every test here monkeypatches
`marrquee.questions.QUESTION_STEPS` with a fixture step, the same pattern
`tests/test_questions.py` and `tests/test_api.py` already use.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import marrquee.questions as questions_module
from marrquee import words
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep, load_answers
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
