"""Tests for the wizard's pure presentation logic - no FastAPI, no disk.

Every decision the two screens make (which apps are ticked, which platform
warning to show, how much room to say a folder has, which sentence answers
which storage check) lives in `wizard.py` as a plain function, tested here
before either screen's HTML exists.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import get_args

from marrquee import catalog, wizard, words
from marrquee.storage import FreshnessCheck, StorageCheck, StorageCheckReason

_STORAGE_CHECK_REASONS = get_args(StorageCheckReason)
_DEFAULT_HOST_PATH = PurePosixPath("/volume1/media")


def _check(
    reason: str | None,
    *,
    host_path: PurePosixPath | None = _DEFAULT_HOST_PATH,
    suggested_path: PurePosixPath | None = None,
    suggestions: tuple[str, ...] = (),
    free_bytes: int | None = None,
    total_bytes: int | None = None,
    detail: str | None = None,
) -> StorageCheck:
    ok = reason is None
    return StorageCheck(
        ok=ok,
        host_path=host_path,
        exists=ok,
        is_dir=ok,
        writable=ok,
        free_bytes=free_bytes,
        total_bytes=total_bytes,
        reason=reason,  # type: ignore[arg-type]
        suggestions=suggestions,
        detail=detail,
        suggested_path=suggested_path,
    )


# --- parse_app_ids ----------------------------------------------------------


def test_parse_app_ids_returns_catalog_order_and_drops_unknown_ids_and_duplicates() -> None:
    assert wizard.parse_app_ids("radarr,prowlarr,unknown,radarr") == ("prowlarr", "radarr")


def test_parse_app_ids_accepts_a_sequence_as_well_as_a_csv_string() -> None:
    assert wizard.parse_app_ids(["radarr", "bogus", "sonarr"]) == ("sonarr", "radarr")


def test_parse_app_ids_never_raises_on_junk() -> None:
    assert wizard.parse_app_ids("") == ()
    assert wizard.parse_app_ids([]) == ()
    assert wizard.parse_app_ids(",,,") == ()
    assert wizard.parse_app_ids("not-an-app-at-all") == ()


def test_default_app_ids_is_every_catalog_id_in_order() -> None:
    assert wizard.DEFAULT_APP_IDS == tuple(app.id for app in catalog.CATALOG)


# --- platform_warning / WIZARD_STEPS ----------------------------------------


def test_platform_warning_only_fires_for_docker_desktop() -> None:
    assert wizard.platform_warning("docker_desktop") == words.WIZARD_PLATFORM_WARNING
    assert wizard.platform_warning("nas_or_linux") is None
    assert wizard.platform_warning("unknown") is None


def test_wizard_steps_are_numbered_one_through_three_in_order() -> None:
    assert [step.number for step in wizard.WIZARD_STEPS] == [1, 2, 3]
    assert [step.label for step in wizard.WIZARD_STEPS] == [
        words.WIZARD_STEP_APPS,
        words.WIZARD_STEP_DRIVE,
        words.WIZARD_STEP_DEPLOY,
    ]


# --- free_space_words --------------------------------------------------------


def test_free_space_words_uses_one_decimal_tb_whole_gb_and_under_one_gb() -> None:
    assert wizard.free_space_words(1_200_000_000_000) == "about 1.2 TB"
    assert wizard.free_space_words(340_000_000_000) == "about 340 GB"
    assert wizard.free_space_words(500_000_000) == "less than 1 GB"


def test_free_space_words_boundaries_use_the_bigger_unit_at_the_threshold() -> None:
    assert wizard.free_space_words(10**12) == "about 1.0 TB"
    assert wizard.free_space_words(10**9) == "about 1 GB"
    assert wizard.free_space_words(10**9 - 1) == "less than 1 GB"


# --- drive_message ------------------------------------------------------------


def test_drive_message_maps_every_storage_check_reason_to_a_problem_tone_and_a_sentence() -> None:
    for reason in _STORAGE_CHECK_REASONS:
        message = wizard.drive_message(_check(reason))
        assert message.tone == "problem"
        assert message.text


def test_drive_message_maps_a_clean_check_to_ok() -> None:
    check = _check(None, free_bytes=1_200_000_000_000, total_bytes=2_000_000_000_000)

    message = wizard.drive_message(check)

    assert message.tone == "ok"
    assert message.text == "Folder found - about 1.2 TB free"
    assert message.suggestion is None
    assert message.guidance is None


def test_drive_message_reports_unknown_room_when_free_bytes_is_none() -> None:
    """Defensive: the real checker always measures free space when ok=True,
    but the mapper stays total even if that ever stops being true.
    """
    check = StorageCheck(
        ok=True,
        host_path=PurePosixPath("/volume1/media"),
        exists=True,
        is_dir=True,
        writable=True,
        free_bytes=None,
        total_bytes=None,
        reason=None,
        suggestions=(),
        detail=None,
    )

    message = wizard.drive_message(check)

    assert message.tone == "unknown"
    assert message.text == words.WIZARD_FOUND_ROOM_UNKNOWN


def test_drive_message_offers_at_most_one_suggestion_as_the_full_existing_path() -> None:
    check = _check(
        "missing",
        host_path=PurePosixPath("/volume1/movis"),
        suggested_path=PurePosixPath("/volume1/movies"),
        suggestions=("movies",),
    )

    message = wizard.drive_message(check)

    assert message.suggestion == "/volume1/movies"
    assert "/volume1/movies" in message.text


def test_drive_message_missing_without_a_suggestion_names_no_folder() -> None:
    check = _check("missing", host_path=PurePosixPath("/volume1/zzz"))

    message = wizard.drive_message(check)

    assert message.suggestion is None
    assert message.text == words.WIZARD_MISSING_NO_SUGGESTION


def test_a_not_shared_did_you_mean_carries_the_other_drive_guidance_naming_the_first_folder() -> (
    None
):
    check = _check(
        "not_shared",
        host_path=PurePosixPath("/volume2/media"),
        suggested_path=PurePosixPath("/volume1/media"),
        suggestions=("volume1",),
    )

    message = wizard.drive_message(check)

    assert message.suggestion == "/volume1/media"
    assert message.guidance is not None
    assert "/volume2" in message.guidance


def test_not_shared_without_a_suggestion_names_the_visible_roots() -> None:
    check = _check("not_shared", host_path=PurePosixPath("/mnt/storage"))

    message = wizard.drive_message(check, shared=(PurePosixPath("/volume1"),))

    assert message.suggestion is None
    assert "/volume1" in message.text


def test_a_fresh_start_refusal_outranks_an_otherwise_fine_check() -> None:
    check = _check(None, free_bytes=1_200_000_000_000)
    freshness = FreshnessCheck(
        ok=False, reason="already_has_files", occupied=("data/media/movies",)
    )

    message = wizard.drive_message(check, freshness)

    assert message.tone == "problem"
    assert message.text == words.refusal_populated_target("/volume1/media")


def test_drive_message_never_puts_storage_check_detail_into_any_field() -> None:
    check = _check("not_writable", detail="[Errno 13] secret")

    message = wizard.drive_message(check)

    assert "[Errno 13] secret" not in message.text
    assert message.guidance is None or "[Errno 13] secret" not in message.guidance
    assert message.suggestion is None or "[Errno 13] secret" not in message.suggestion
