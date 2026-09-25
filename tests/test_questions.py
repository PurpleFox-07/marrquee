"""Tests for the per-app question registry: fields, checking and the
answers store.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from marrquee import questions
from marrquee.questions import (
    QUESTION_STEPS,
    QuestionCheck,
    QuestionField,
    QuestionOption,
    QuestionStep,
    check_step,
    find_step,
    load_answers,
    missing_step,
    question_steps_for,
    save_step_answers,
)
from marrquee.words import QUESTION_PICK_ONE


def _ok(answers: Mapping[str, str]) -> QuestionCheck:
    return QuestionCheck(ok=True, answers=answers, problem=None, field=None)


def _text_step(app_id: str, step_id: str = "fixture") -> QuestionStep:
    return QuestionStep(
        app_id=app_id,
        step_id=step_id,
        title="Fixture step",
        lede="A fixture question for tests.",
        fields=(QuestionField(name="name", label="Name", kind="text"),),
        check=_ok,
    )


def test_question_steps_ships_empty() -> None:
    assert QUESTION_STEPS == ()


def test_question_steps_for_orders_by_catalog_then_declared_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    radarr_step = _text_step("radarr")
    sonarr_step = _text_step("sonarr")
    monkeypatch.setattr(questions, "QUESTION_STEPS", (radarr_step, sonarr_step))

    steps = question_steps_for(("radarr", "sonarr"))

    assert steps == (sonarr_step, radarr_step)


def test_question_steps_for_only_returns_steps_for_the_given_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    radarr_step = _text_step("radarr")
    sonarr_step = _text_step("sonarr")
    monkeypatch.setattr(questions, "QUESTION_STEPS", (radarr_step, sonarr_step))

    assert question_steps_for(("sonarr",)) == (sonarr_step,)


def test_find_step_matches_both_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    step = _text_step("radarr")
    monkeypatch.setattr(questions, "QUESTION_STEPS", (step,))

    assert find_step("radarr", "fixture") is step
    assert find_step("radarr", "other") is None
    assert find_step("sonarr", "fixture") is None


def test_check_step_drops_unknown_fields_and_strips_a_text_value() -> None:
    captured: dict[str, str] = {}

    def check(answers: Mapping[str, str]) -> QuestionCheck:
        captured.update(answers)
        return _ok(answers)

    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="t",
        lede="l",
        fields=(QuestionField(name="name", label="Name", kind="text"),),
        check=check,
    )

    result = check_step(step, {"name": " Bob ", "unexpected": "sneaky"}, {})

    assert result.ok is True
    assert captured == {"name": "Bob"}


def test_check_step_keeps_a_saved_password_on_a_blank_post() -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="t",
        lede="l",
        fields=(QuestionField(name="token", label="Token", kind="password"),),
        check=_ok,
    )

    result = check_step(step, {"token": ""}, {"token": "already-saved"})

    assert result.answers["token"] == "already-saved"


def test_check_step_never_strips_a_password_value() -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="t",
        lede="l",
        fields=(QuestionField(name="token", label="Token", kind="password"),),
        check=_ok,
    )

    result = check_step(step, {"token": "  spaced  "}, {})

    assert result.answers["token"] == "  spaced  "


def test_check_step_refuses_an_unknown_choice() -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="t",
        lede="l",
        fields=(
            QuestionField(
                name="mode",
                label="Mode",
                kind="choice",
                options=(
                    QuestionOption(value="a", label="A"),
                    QuestionOption(value="b", label="B"),
                ),
            ),
        ),
        check=_ok,
    )

    result = check_step(step, {"mode": "c"}, {})

    assert result.ok is False
    assert result.problem == QUESTION_PICK_ONE
    assert result.field == "mode"


def test_check_step_accepts_a_known_choice() -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="t",
        lede="l",
        fields=(
            QuestionField(
                name="mode",
                label="Mode",
                kind="choice",
                options=(QuestionOption(value="a", label="A"),),
            ),
        ),
        check=_ok,
    )

    result = check_step(step, {"mode": "a"}, {})

    assert result.ok is True
    assert result.answers["mode"] == "a"


def test_missing_step_names_the_first_unanswered_step(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _text_step("prowlarr", "first")
    second = _text_step("radarr", "second")
    monkeypatch.setattr(questions, "QUESTION_STEPS", (first, second))

    assert missing_step(("prowlarr", "radarr"), {}) is first
    assert missing_step(("prowlarr", "radarr"), {"prowlarr": {"name": "x"}}) is second
    assert (
        missing_step(("prowlarr", "radarr"), {"prowlarr": {"name": "x"}, "radarr": {"name": "y"}})
        is None
    )


def test_question_step_rejects_a_bad_step_id() -> None:
    with pytest.raises(ValueError):
        QuestionStep(
            app_id="radarr",
            step_id="Not-Valid!",
            title="t",
            lede="l",
            fields=(),
            check=_ok,
        )


def test_question_step_rejects_duplicate_field_names() -> None:
    with pytest.raises(ValueError):
        QuestionStep(
            app_id="radarr",
            step_id="fixture",
            title="t",
            lede="l",
            fields=(
                QuestionField(name="a", label="A", kind="text"),
                QuestionField(name="a", label="A again", kind="text"),
            ),
            check=_ok,
        )


def test_load_answers_reads_any_bad_shape_as_nothing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    assert load_answers(config_dir) == {}

    config_dir.mkdir(parents=True)
    answers_path = config_dir / "answers.json"

    answers_path.write_text("")
    assert load_answers(config_dir) == {}

    answers_path.write_text("[]")
    assert load_answers(config_dir) == {}

    answers_path.write_text('{"version": 2, "apps": {"radarr": {"token": "abc"}}}')
    assert load_answers(config_dir) == {}

    answers_path.write_text('{"version": 1, "apps": {"radarr": {"token": 5}}}')
    assert load_answers(config_dir) == {}


def test_save_step_answers_merges_into_the_apps_existing_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    save_step_answers(config_dir, "radarr", {"root": "/data/media/movies"})
    save_step_answers(config_dir, "radarr", {"token": "abc123"})

    answers = load_answers(config_dir)

    assert answers["radarr"] == {"root": "/data/media/movies", "token": "abc123"}


def test_save_step_answers_does_not_disturb_a_different_apps_map(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"

    save_step_answers(config_dir, "prowlarr", {"a": "1"})
    save_step_answers(config_dir, "radarr", {"b": "2"})

    answers = load_answers(config_dir)

    assert answers == {"prowlarr": {"a": "1"}, "radarr": {"b": "2"}}
