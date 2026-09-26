"""Tests for qBittorrent's seeding rules: a plain saved answer, checked and
turned into qBittorrent's own preference keys.

`seeding.py` is a leaf like `vpn.py` - these tests call `check_seeding` and
`seeding_preferences` directly, never through `questions.check_step`.
"""

from __future__ import annotations

from collections.abc import Mapping

from marrquee.seeding import SeedingCheck, check_seeding, seeding_preferences
from marrquee.words import (
    SEEDING_PROBLEM_DAYS,
    SEEDING_PROBLEM_OWN_EMPTY,
    SEEDING_PROBLEM_RATIO,
)


def _answers(**fields: str) -> dict[str, str]:
    base = {"seeding": "", "seed_ratio": "", "seed_days": ""}
    base.update(fields)
    return base


# --- check_seeding: presets always carry all three fields --------------------


def test_a_preset_answer_is_accepted_and_blanks_the_own_numbers() -> None:
    result = check_seeding(_answers(seeding="good_neighbor", seed_ratio="9.9", seed_days="30"))

    assert result.ok is True
    assert result.answers == {"seeding": "good_neighbor", "seed_ratio": "", "seed_days": ""}
    assert result.problem is None
    assert result.field is None


def test_every_preset_is_accepted_and_drops_stale_own_numbers() -> None:
    for preset in ("good_neighbor", "save_space", "private"):
        result = check_seeding(_answers(seeding=preset, seed_ratio="2.0", seed_days="10"))
        assert result.ok is True
        assert result.answers["seeding"] == preset
        assert result.answers["seed_ratio"] == ""
        assert result.answers["seed_days"] == ""


def test_an_unrecognised_preset_is_refused_and_echoes_every_field() -> None:
    result = check_seeding(_answers(seeding="bogus", seed_ratio="1.0", seed_days="5"))

    assert result.ok is False
    assert result.field == "seeding"
    assert result.answers == {"seeding": "bogus", "seed_ratio": "1.0", "seed_days": "5"}


# --- check_seeding: "own" numbers ---------------------------------------------


def test_own_numbers_refuses_when_both_are_blank() -> None:
    result = check_seeding(_answers(seeding="own"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_OWN_EMPTY
    assert result.field == "seed_ratio"


def test_own_numbers_refuses_a_ratio_of_zero() -> None:
    result = check_seeding(_answers(seeding="own", seed_ratio="0"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_RATIO
    assert result.field == "seed_ratio"


def test_own_numbers_refuses_a_comma_decimal() -> None:
    result = check_seeding(_answers(seeding="own", seed_ratio="1,5"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_RATIO
    assert result.field == "seed_ratio"


def test_own_numbers_refuses_scientific_notation() -> None:
    result = check_seeding(_answers(seeding="own", seed_ratio="1e3"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_RATIO
    assert result.field == "seed_ratio"


def test_own_numbers_refuses_more_than_two_decimal_places() -> None:
    result = check_seeding(_answers(seeding="own", seed_ratio="1.234"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_RATIO
    assert result.field == "seed_ratio"


def test_own_numbers_refuses_a_ratio_over_one_hundred() -> None:
    result = check_seeding(_answers(seeding="own", seed_ratio="100.01"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_RATIO


def test_own_numbers_refuses_days_over_the_year_bound() -> None:
    result = check_seeding(_answers(seeding="own", seed_days="400"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_DAYS
    assert result.field == "seed_days"


def test_own_numbers_refuses_a_non_digit_day_count() -> None:
    result = check_seeding(_answers(seeding="own", seed_days="-1"))

    assert result.ok is False
    assert result.problem == SEEDING_PROBLEM_DAYS
    assert result.field == "seed_days"


def test_own_numbers_keeps_one_of_the_two() -> None:
    ratio_only = check_seeding(_answers(seeding="own", seed_ratio="2.5"))
    assert ratio_only.ok is True
    assert ratio_only.answers == {"seeding": "own", "seed_ratio": "2.5", "seed_days": ""}

    days_only = check_seeding(_answers(seeding="own", seed_days="14"))
    assert days_only.ok is True
    assert days_only.answers == {"seeding": "own", "seed_ratio": "", "seed_days": "14"}


def test_own_numbers_accepts_both_at_their_boundaries() -> None:
    result = check_seeding(_answers(seeding="own", seed_ratio="0.1", seed_days="365"))

    assert result.ok is True
    assert result.answers == {"seeding": "own", "seed_ratio": "0.1", "seed_days": "365"}


def test_check_seeding_never_raises_on_a_completely_empty_mapping() -> None:
    result: SeedingCheck = check_seeding({})

    assert result.ok is False
    assert result.answers == {"seeding": "", "seed_ratio": "", "seed_days": ""}


# --- seeding_preferences: each preset's exact prefs ---------------------------


def _prefs_for(answers: Mapping[str, str] | None) -> Mapping[str, object]:
    return seeding_preferences(answers)


def test_good_neighbor_stops_by_time_only() -> None:
    prefs = _prefs_for({"seeding": "good_neighbor", "seed_ratio": "", "seed_days": ""})

    assert prefs == {
        "max_ratio_enabled": False,
        "max_seeding_time_enabled": True,
        "max_seeding_time": 10080,
        "max_inactive_seeding_time_enabled": False,
        "max_ratio_act": 0,
    }


def test_save_space_stops_at_a_1_to_1_ratio_or_seven_days() -> None:
    prefs = _prefs_for({"seeding": "save_space", "seed_ratio": "", "seed_days": ""})

    assert prefs == {
        "max_ratio_enabled": True,
        "max_ratio": 1.0,
        "max_seeding_time_enabled": True,
        "max_seeding_time": 10080,
        "max_inactive_seeding_time_enabled": False,
        "max_ratio_act": 0,
    }


def test_private_shares_for_thirty_days() -> None:
    prefs = _prefs_for({"seeding": "private", "seed_ratio": "", "seed_days": ""})

    assert prefs == {
        "max_ratio_enabled": False,
        "max_seeding_time_enabled": True,
        "max_seeding_time": 43200,
        "max_inactive_seeding_time_enabled": False,
        "max_ratio_act": 0,
    }


def test_own_numbers_enable_only_the_given_values() -> None:
    ratio_only = _prefs_for({"seeding": "own", "seed_ratio": "2.5", "seed_days": ""})
    assert ratio_only == {
        "max_ratio_enabled": True,
        "max_ratio": 2.5,
        "max_seeding_time_enabled": False,
        "max_inactive_seeding_time_enabled": False,
        "max_ratio_act": 0,
    }

    days_only = _prefs_for({"seeding": "own", "seed_ratio": "", "seed_days": "14"})
    assert days_only == {
        "max_ratio_enabled": False,
        "max_seeding_time_enabled": True,
        "max_seeding_time": 20160,
        "max_inactive_seeding_time_enabled": False,
        "max_ratio_act": 0,
    }


def test_none_answers_fall_back_to_good_neighbor() -> None:
    assert _prefs_for(None) == _prefs_for({"seeding": "good_neighbor"})


def test_a_refused_answer_falls_back_to_good_neighbor() -> None:
    assert _prefs_for({"seeding": "own"}) == _prefs_for({"seeding": "good_neighbor"})


def test_every_preset_sets_the_stop_action_never_delete() -> None:
    for answers in (
        {"seeding": "good_neighbor"},
        {"seeding": "save_space"},
        {"seeding": "private"},
        {"seeding": "own", "seed_ratio": "1.0"},
    ):
        assert _prefs_for(answers)["max_ratio_act"] == 0
