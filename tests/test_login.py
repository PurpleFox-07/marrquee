"""Tests for the saved login: its file, its rules, and the QuestionSteps
that ask for it.

Nothing here touches Docker, HTTP or a route - that's `login_apply.py` and
the DeployManager (later chunks). This module only ever answers "what is
saved" and "is this one acceptable", so these tests exercise exactly that:
`load_login` never raising, `save_login`/`record_applied`'s generation
bookkeeping, and the three QuestionSteps' rules.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from marrquee import login, words
from marrquee.login import (
    CHANGE_STEP,
    LOGIN_RESET_STEP,
    LOGIN_STEP,
    LoginRecord,
    SavedLogin,
    load_login,
    login_status,
    password_matches,
    pending_app_ids,
    record_applied,
    reset_reminder,
    save_login,
)
from marrquee.questions import check_step

_EMPTY = LoginRecord(login=None, applied={}, reset_honored=None)


@pytest.mark.parametrize(
    "make_file",
    [
        pytest.param(lambda path: None, id="missing"),
        pytest.param(lambda path: path.write_text(""), id="empty"),
        pytest.param(lambda path: path.write_text("{not json"), id="corrupt"),
        pytest.param(
            lambda path: path.write_text(json.dumps({"version": 999, "username": "owner"})),
            id="future-version",
        ),
        pytest.param(
            lambda path: path.write_text(json.dumps({"version": 1, "username": "owner"})),
            id="missing-fields",
        ),
        pytest.param(
            lambda path: path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "username": "owner",
                        "password": "secret123",
                        "generation": True,  # bool, not an int
                        "applied": {},
                        "reset_honored": None,
                    }
                )
            ),
            id="bool-as-generation",
        ),
    ],
)
def test_a_missing_blank_corrupt_wrong_version_or_wrong_shape_login_reads_as_no_login(
    tmp_path: Path, make_file: Callable[[Path], object]
) -> None:
    make_file(tmp_path / "login.json")

    record = load_login(tmp_path)

    assert record == _EMPTY


def test_login_round_trips_through_save_and_load(tmp_path: Path) -> None:
    saved = save_login(tmp_path, "owner", "first-password", honor_reset=None)

    record = load_login(tmp_path)

    assert record.login == saved
    assert saved.username == "owner"
    assert saved.generation == 1
    assert record.applied == {}
    assert record.reset_honored is None


def test_login_file_is_written_0600(tmp_path: Path) -> None:
    save_login(tmp_path, "owner", "first-password", honor_reset=None)

    written_files = list(tmp_path.iterdir())
    assert len(written_files) == 1
    login_file = written_files[0]
    assert not login_file.name.endswith(".tmp")
    mode = login_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_save_login_bumps_generation_and_keeps_applied_so_every_app_becomes_pending(
    tmp_path: Path,
) -> None:
    save_login(tmp_path, "owner", "first-password", honor_reset=None)
    record_applied(tmp_path, "prowlarr", 1)
    record_applied(tmp_path, "sonarr", 1)

    saved = save_login(tmp_path, "owner", "second-password", honor_reset=None)

    assert saved.generation == 2
    record = load_login(tmp_path)
    assert record.applied == {"prowlarr": 1, "sonarr": 1}
    assert pending_app_ids(record, ("prowlarr", "sonarr", "radarr")) == (
        "prowlarr",
        "sonarr",
        "radarr",
    )


def test_pending_app_ids_is_every_login_taking_app_when_nothing_is_saved() -> None:
    assert pending_app_ids(_EMPTY, ("radarr", "prowlarr", "sonarr")) == (
        "prowlarr",
        "sonarr",
        "radarr",
    )


def test_pending_app_ids_ignores_apps_whose_login_kind_is_none() -> None:
    record = LoginRecord(
        login=SavedLogin(username="owner", generation=1, password="first-password"),
        applied={"prowlarr": 1, "sonarr": 1, "radarr": 1},
        reset_honored=None,
    )

    assert pending_app_ids(record, ("prowlarr", "sonarr", "radarr", "unknown-app")) == ()


def test_record_applied_ignores_a_stale_generation(tmp_path: Path) -> None:
    save_login(tmp_path, "owner", "first-password", honor_reset=None)
    save_login(tmp_path, "owner", "second-password", honor_reset=None)

    record_applied(tmp_path, "prowlarr", 1)

    record = load_login(tmp_path)
    assert record.applied == {}
    assert record.login is not None
    assert record.login.generation == 2


def test_record_applied_writes_the_current_generation(tmp_path: Path) -> None:
    save_login(tmp_path, "owner", "first-password", honor_reset=None)

    record_applied(tmp_path, "prowlarr", 1)

    record = load_login(tmp_path)
    assert record.applied == {"prowlarr": 1}


def test_record_applied_on_a_missing_login_never_raises(tmp_path: Path) -> None:
    record_applied(tmp_path, "prowlarr", 1)

    assert load_login(tmp_path) == _EMPTY


def test_repr_of_a_saved_login_never_contains_the_password() -> None:
    saved_login = SavedLogin(username="owner", generation=1, password="super-secret-pw")

    assert "super-secret-pw" not in repr(saved_login)
    assert "owner" in repr(saved_login)


def test_login_status_is_reset_only_while_differing_from_the_honoured_value(
    tmp_path: Path,
) -> None:
    assert login_status(_EMPTY, reset_value="forgot-2026") == "none"

    save_login(tmp_path, "owner", "first-password", honor_reset=None)
    record = load_login(tmp_path)
    assert login_status(record, reset_value=None) == "set"
    assert login_status(record, reset_value="") == "set"
    assert login_status(record, reset_value="forgot-2026") == "reset"
    assert reset_reminder(record, "forgot-2026") is False

    save_login(tmp_path, "owner", "second-password", honor_reset="forgot-2026")
    record_after = load_login(tmp_path)
    assert login_status(record_after, reset_value="forgot-2026") == "set"
    assert reset_reminder(record_after, "forgot-2026") is True
    assert reset_reminder(record_after, None) is False
    assert reset_reminder(record_after, "") is False


def test_password_matches_uses_constant_time_comparison() -> None:
    saved_login = SavedLogin(username="owner", generation=1, password="right-password-1")

    assert password_matches(saved_login, "right-password-1") is True
    assert password_matches(saved_login, "wrong-password-1") is False
    assert password_matches(saved_login, "") is False


def test_login_rules_lower_uppercase_and_refuse_a_short_username() -> None:
    result = check_step(
        LOGIN_STEP,
        {"username": "Owner", "password": "good-password-1", "password_again": "good-password-1"},
        {},
    )

    assert result.ok is True
    assert result.answers["username"] == "owner"

    refused = check_step(
        LOGIN_STEP,
        {"username": "ab", "password": "good-password-1", "password_again": "good-password-1"},
        {},
    )

    assert refused.ok is False
    assert refused.field == "username"
    assert refused.problem == words.LOGIN_PROBLEM_USERNAME


def test_login_rules_refuse_a_short_password() -> None:
    refused = check_step(
        LOGIN_STEP, {"username": "owner", "password": "short12", "password_again": "short12"}, {}
    )

    assert refused.ok is False
    assert refused.field == "password"
    assert refused.problem == words.LOGIN_PROBLEM_PASSWORD_SHORT


def test_login_rules_refuse_an_overly_long_password() -> None:
    long_password = "a1" * 65  # 130 characters

    refused = check_step(
        LOGIN_STEP,
        {"username": "owner", "password": long_password, "password_again": long_password},
        {},
    )

    assert refused.ok is False
    assert refused.field == "password"
    assert refused.problem == words.LOGIN_PROBLEM_PASSWORD_LONG


def test_login_rules_refuse_edge_whitespace_on_the_password() -> None:
    refused = check_step(
        LOGIN_STEP,
        {
            "username": "owner",
            "password": "good-password-1 ",
            "password_again": "good-password-1 ",
        },
        {},
    )

    assert refused.ok is False
    assert refused.field == "password"
    assert refused.problem == words.LOGIN_PROBLEM_PASSWORD_SPACES


def test_login_rules_refuse_a_mismatched_confirmation() -> None:
    refused = check_step(
        LOGIN_STEP,
        {
            "username": "owner",
            "password": "good-password-1",
            "password_again": "good-password-2",
        },
        {},
    )

    assert refused.ok is False
    assert refused.field == "password_again"
    assert refused.problem == words.LOGIN_PROBLEM_MISMATCH


def test_login_reset_step_shares_the_login_steps_rules() -> None:
    result = check_step(
        LOGIN_RESET_STEP,
        {"username": "owner", "password": "good-password-1", "password_again": "good-password-1"},
        {},
    )

    assert result.ok is True
    assert LOGIN_RESET_STEP.title != LOGIN_STEP.title


def test_change_blank_new_password_means_keep_and_blank_current_is_refused() -> None:
    blank_current = check_step(
        CHANGE_STEP,
        {"current_password": "", "username": "owner", "password": "", "password_again": ""},
        {},
    )

    assert blank_current.ok is False
    assert blank_current.field == "current_password"
    assert blank_current.problem == words.CHANGE_PROBLEM_CURRENT_BLANK

    keep = check_step(
        CHANGE_STEP,
        {
            "current_password": "right-password-1",
            "username": "owner",
            "password": "",
            "password_again": "",
        },
        {},
    )

    assert keep.ok is True
    assert keep.answers["password"] == ""
    assert keep.answers["password_again"] == ""
    assert keep.answers["current_password"] == "right-password-1"
    assert keep.answers["username"] == "owner"


def test_change_a_new_password_is_checked_by_the_same_rules() -> None:
    changed = check_step(
        CHANGE_STEP,
        {
            "current_password": "right-password-1",
            "username": "Owner",
            "password": "new-password-1",
            "password_again": "new-password-1",
        },
        {},
    )

    assert changed.ok is True
    assert changed.answers["username"] == "owner"
    assert changed.answers["password"] == "new-password-1"

    refused = check_step(
        CHANGE_STEP,
        {
            "current_password": "right-password-1",
            "username": "owner",
            "password": "short12",
            "password_again": "short12",
        },
        {},
    )

    assert refused.ok is False
    assert refused.field == "password"
    assert refused.problem == words.LOGIN_PROBLEM_PASSWORD_SHORT


def test_login_hint_words_mention_the_same_limits_the_rules_enforce() -> None:
    assert str(login._USERNAME_MAX) in words.LOGIN_USERNAME_HINT
    assert str(login._PASSWORD_MIN) in words.LOGIN_PASSWORD_HINT
