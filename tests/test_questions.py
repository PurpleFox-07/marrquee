"""Tests for the per-app question registry: fields, checking and the
answers store.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from marrquee import questions, vpn, words
from marrquee.login import LOGIN_STEP
from marrquee.questions import (
    MOVIE_QUALITY_STEP,
    QUESTION_STEPS,
    SEEDING_STEP,
    TV_QUALITY_STEP,
    VPN_STEP,
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


def test_question_steps_holds_the_registered_vpn_and_seeding_steps() -> None:
    assert QUESTION_STEPS == (VPN_STEP, SEEDING_STEP, TV_QUALITY_STEP, MOVIE_QUALITY_STEP)


def test_question_option_and_field_gain_their_new_optional_attributes() -> None:
    option = QuestionOption(value="a", label="A")
    assert option.url == ""
    assert option.disabled is False

    field = QuestionField(name="n", label="L", kind="text")
    assert field.guide_label == ""
    assert field.guide_url == ""


def test_check_step_treats_list_kind_like_choice() -> None:
    step = QuestionStep(
        app_id="radarr",
        step_id="fixture",
        title="t",
        lede="l",
        fields=(
            QuestionField(
                name="pick",
                label="Pick",
                kind="list",
                options=(QuestionOption(value="a", label="A"),),
            ),
        ),
        check=_ok,
    )

    refused = check_step(step, {"pick": "b"}, {})
    assert refused.ok is False
    assert refused.problem == QUESTION_PICK_ONE
    assert refused.field == "pick"

    accepted = check_step(step, {"pick": " a "}, {})
    assert accepted.ok is True
    assert accepted.answers["pick"] == "a"


def test_login_step_behaves_unchanged_after_list_kind_support() -> None:
    """LOGIN_STEP has no `list` field, so adding `list` support to
    `check_step` must not change a single one of its existing outcomes.
    """
    accepted = check_step(
        LOGIN_STEP,
        {"username": "owner", "password": "good-password-1", "password_again": "good-password-1"},
        {},
    )
    assert accepted.ok is True
    assert accepted.answers["username"] == "owner"

    refused = check_step(
        LOGIN_STEP,
        {"username": "ab", "password": "good-password-1", "password_again": "good-password-1"},
        {},
    )
    assert refused.ok is False
    assert refused.field == "username"


def test_vpn_step_fields_are_in_contract_order() -> None:
    names = [field.name for field in VPN_STEP.fields]

    assert names == [
        "provider",
        "vpn_type",
        "openvpn_user",
        "openvpn_password",
        "wireguard_private_key",
        "wireguard_addresses",
        "wireguard_preshared_key",
        "server_countries",
    ]
    assert VPN_STEP.app_id == "gluetun"
    assert VPN_STEP.step_id == "vpn"

    provider_field = VPN_STEP.fields[0]
    assert provider_field.kind == "list"
    assert provider_field.options[0] == QuestionOption(
        value="", label=words.VPN_PROVIDER_PLACEHOLDER, disabled=True
    )
    assert len(provider_field.options) == len(vpn.VPN_PROVIDERS) + 1
    assert provider_field.guide_label == words.VPN_GUIDE_LINK
    assert provider_field.guide_url == words.VPN_GUIDE_INDEX_URL

    cyberghost_option = next(
        option for option in provider_field.options if option.value == "cyberghost"
    )
    assert cyberghost_option.disabled is True
    assert words.VPN_PROVIDER_NEEDS_FILES in cyberghost_option.label

    vpn_type_field = VPN_STEP.fields[1]
    assert vpn_type_field.default == "openvpn"


def test_the_vpn_steps_empty_option_fails_its_own_check_never_question_pick_one() -> None:
    # `provider`'s empty option is itself an allowed choice value (it's a
    # real `QuestionOption`, just disabled) - the generic per-field check in
    # `check_step` lets it through, so the refusal comes only from
    # `check_vpn_answers` inside the step's own `check`. Every other field
    # posts a value that survives the generic check, so the provider
    # refusal isn't masked by an unrelated one.
    result = check_step(VPN_STEP, {"provider": "", "vpn_type": "openvpn"}, {})

    assert result.ok is False
    assert result.field == "provider"
    assert result.problem == vpn.VPN_PROBLEM_PICK_PROVIDER
    assert result.problem != QUESTION_PICK_ONE


def test_seeding_step_fields_are_in_contract_order() -> None:
    names = [field.name for field in SEEDING_STEP.fields]

    assert names == ["seeding", "seed_ratio", "seed_days"]
    assert SEEDING_STEP.app_id == "qbittorrent"
    assert SEEDING_STEP.step_id == "seeding"

    seeding_field = SEEDING_STEP.fields[0]
    assert seeding_field.kind == "choice"
    assert seeding_field.default == "good_neighbor"
    assert [option.value for option in seeding_field.options] == [
        "good_neighbor",
        "save_space",
        "private",
        "own",
    ]

    ratio_field = SEEDING_STEP.fields[1]
    days_field = SEEDING_STEP.fields[2]
    assert ratio_field.kind == "text"
    assert days_field.kind == "text"


def test_seeding_step_check_routes_through_check_seeding() -> None:
    accepted = check_step(SEEDING_STEP, {"seeding": "save_space"}, {})
    assert accepted.ok is True
    assert accepted.answers == {"seeding": "save_space", "seed_ratio": "", "seed_days": ""}

    refused = check_step(SEEDING_STEP, {"seeding": "own", "seed_ratio": "", "seed_days": ""}, {})
    assert refused.ok is False
    assert refused.field == "seed_ratio"


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


def test_quality_steps_fields_are_owned_by_their_arr_app() -> None:
    assert TV_QUALITY_STEP.app_id == "sonarr"
    assert TV_QUALITY_STEP.asked_with == "recyclarr"
    assert [field.name for field in TV_QUALITY_STEP.fields] == ["tv_quality"]
    assert [option.value for option in TV_QUALITY_STEP.fields[0].options] == ["1080p", "4k"]
    assert TV_QUALITY_STEP.fields[0].default == "1080p"

    assert MOVIE_QUALITY_STEP.app_id == "radarr"
    assert MOVIE_QUALITY_STEP.asked_with == "recyclarr"
    assert [field.name for field in MOVIE_QUALITY_STEP.fields] == ["movie_quality"]


def test_quality_step_check_accepts_whatever_check_step_already_let_through() -> None:
    accepted = check_step(TV_QUALITY_STEP, {"tv_quality": "4k"}, {})
    assert accepted.ok is True
    assert accepted.answers == {"tv_quality": "4k"}

    refused = check_step(TV_QUALITY_STEP, {"tv_quality": "8k"}, {})
    assert refused.ok is False
    assert refused.problem == QUESTION_PICK_ONE


def test_recyclarr_add_asks_installed_arr_steps_only() -> None:
    steps = question_steps_for(("recyclarr",), present=("prowlarr", "sonarr"))

    assert steps == (TV_QUALITY_STEP,)


def test_sonarr_add_with_recyclarr_present_asks_tv_quality() -> None:
    steps = question_steps_for(("sonarr",), present=("prowlarr", "recyclarr"))

    assert steps == (TV_QUALITY_STEP,)


def test_sonarr_add_without_recyclarr_asks_nothing() -> None:
    assert question_steps_for(("sonarr",)) == ()


def test_wizard_with_sonarr_and_recyclarr_ticked_includes_the_tv_step() -> None:
    """The wizard never passes `present=` - both ids arrive together in
    `app_ids` instead, since everything ticked is "adding" from its point
    of view.
    """
    steps = question_steps_for(("sonarr", "recyclarr"))

    assert TV_QUALITY_STEP in steps
    assert MOVIE_QUALITY_STEP not in steps


def test_present_unset_keeps_every_pre_existing_step_list_unchanged() -> None:
    assert question_steps_for(("gluetun", "qbittorrent")) == (VPN_STEP, SEEDING_STEP)
    assert question_steps_for(("qbittorrent",)) == (SEEDING_STEP,)
    assert question_steps_for(("gluetun",)) == (VPN_STEP,)


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
