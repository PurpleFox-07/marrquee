"""Tests for the break-glass "run qBittorrent without a VPN" rule: the
typed phrase, the three confirmation stages, and the one file that records
the owner's choice.

`without_vpn_confirmed` is the one function `build_stack_plan`'s refusal is
allowed to bend for - every failure mode here must read as False, the safe
side, never True.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from marrquee.without_vpn import (
    StageOutcome,
    clear_without_vpn,
    next_stage,
    phrase_matches,
    save_without_vpn,
    without_vpn_confirmed,
)

_NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


# --- without_vpn_confirmed: never raises, fails safe -------------------------


def test_missing_file_is_not_confirmed(tmp_path: Path) -> None:
    assert without_vpn_confirmed(tmp_path) is False


def test_blank_file_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").write_text("   ")

    assert without_vpn_confirmed(tmp_path) is False


def test_garbage_json_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").write_text("{not json")

    assert without_vpn_confirmed(tmp_path) is False


def test_wrong_version_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").write_text(
        json.dumps({"version": 2, "confirmed_at": "2026-09-26T00:00:00+00:00"})
    )

    assert without_vpn_confirmed(tmp_path) is False


def test_blank_confirmed_at_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").write_text(json.dumps({"version": 1, "confirmed_at": ""}))

    assert without_vpn_confirmed(tmp_path) is False


def test_missing_confirmed_at_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").write_text(json.dumps({"version": 1}))

    assert without_vpn_confirmed(tmp_path) is False


def test_a_json_list_instead_of_an_object_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").write_text(json.dumps([1, 2, 3]))

    assert without_vpn_confirmed(tmp_path) is False


def test_a_directory_where_the_file_should_be_is_not_confirmed(tmp_path: Path) -> None:
    (tmp_path / "without_vpn.json").mkdir()

    assert without_vpn_confirmed(tmp_path) is False


@pytest.mark.skipif(os.name == "nt", reason="permission bits are POSIX-only")
def test_an_unreadable_file_is_not_confirmed(tmp_path: Path) -> None:
    path = tmp_path / "without_vpn.json"
    path.write_text(json.dumps({"version": 1, "confirmed_at": "2026-09-26T00:00:00+00:00"}))
    os.chmod(path, 0o000)

    try:
        assert without_vpn_confirmed(tmp_path) is False
    finally:
        os.chmod(path, 0o600)  # so pytest can clean up tmp_path afterward


# --- save / confirmed / clear round trip -------------------------------------


def test_save_then_confirmed(tmp_path: Path) -> None:
    save_without_vpn(tmp_path, now=_NOW)

    assert without_vpn_confirmed(tmp_path) is True


def test_clear_then_not_confirmed(tmp_path: Path) -> None:
    save_without_vpn(tmp_path, now=_NOW)

    clear_without_vpn(tmp_path)

    assert without_vpn_confirmed(tmp_path) is False


def test_clear_of_a_missing_file_is_quiet(tmp_path: Path) -> None:
    clear_without_vpn(tmp_path)  # must not raise

    assert without_vpn_confirmed(tmp_path) is False


def test_clear_of_an_unremovable_path_is_quiet_and_logged(tmp_path: Path) -> None:
    """A directory sitting where the file should be raises `IsADirectoryError`
    (an `OSError`, never `FileNotFoundError`) - still swallowed and logged,
    never raised.
    """
    (tmp_path / "without_vpn.json").mkdir()

    clear_without_vpn(tmp_path)  # must not raise


def test_the_saved_file_is_0600(tmp_path: Path) -> None:
    save_without_vpn(tmp_path, now=_NOW)

    mode = (tmp_path / "without_vpn.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_the_saved_payload_holds_version_and_an_iso_timestamp(tmp_path: Path) -> None:
    save_without_vpn(tmp_path, now=_NOW)

    payload = json.loads((tmp_path / "without_vpn.json").read_text())
    assert payload == {"version": 1, "confirmed_at": _NOW.isoformat()}


# --- phrase_matches: case, spacing and a final full stop, nothing looser ----


def test_the_exact_phrase_matches() -> None:
    assert phrase_matches("I understand my real address will be visible") is True


@pytest.mark.parametrize(
    "typed",
    [
        "i understand my real address will be visible",
        "I UNDERSTAND MY REAL ADDRESS WILL BE VISIBLE",
        "  I understand   my real address will be visible  ",
        "I understand my real address will be visible.",
        "  i UNDERSTAND my real  address will be visible. ",
    ],
)
def test_case_spacing_and_a_final_full_stop_are_ignored(typed: str) -> None:
    assert phrase_matches(typed) is True


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "I understand",
        "I understand my real address will definitely be visible",
        "I understand my real address will be visible!",
    ],
)
def test_a_prefix_an_insertion_or_a_missing_word_never_matches(typed: str) -> None:
    assert phrase_matches(typed) is False


# --- next_stage: 1 -> 2 -> 3, confirm only a matching stage 3 ----------------


def test_stage_1_moves_to_stage_2_and_saves_nothing() -> None:
    outcome = next_stage("1", "")

    assert outcome == StageOutcome(confirmed=False, stage=2, problem=None)


def test_stage_2_moves_to_stage_3_and_saves_nothing() -> None:
    outcome = next_stage("2", "")

    assert outcome == StageOutcome(confirmed=False, stage=3, problem=None)


def test_stage_3_with_the_matching_phrase_confirms() -> None:
    outcome = next_stage("3", "i understand my real address will be visible")

    assert outcome == StageOutcome(confirmed=True, stage=3, problem=None)


def test_stage_3_with_a_mismatch_re_renders_stage_3_with_a_problem() -> None:
    from marrquee.words import WITHOUT_VPN_PROBLEM_MISMATCH

    outcome = next_stage("3", "nope")

    assert outcome == StageOutcome(confirmed=False, stage=3, problem=WITHOUT_VPN_PROBLEM_MISMATCH)


@pytest.mark.parametrize("posted_stage", ["", "0", "4", "garbage"])
def test_anything_unknown_resets_to_stage_1(posted_stage: str) -> None:
    outcome = next_stage(posted_stage, "whatever")

    assert outcome == StageOutcome(confirmed=False, stage=1, problem=None)
