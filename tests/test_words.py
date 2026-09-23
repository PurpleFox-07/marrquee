"""Tests for the copy inventory: every sentence Marrquee shows the owner.

`WORDS_INVENTORY` is the review surface the story's Content Direction table
promises - a copy edit is a diff to a name in this list, not a silent change
buried in a template. These tests pin that list and prove no other module
in the package writes its own user-facing sentence instead of reading one
of these.
"""

from __future__ import annotations

import ast
from pathlib import Path

from marrquee import catalog, deploy, words

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "marrquee"

# Mirrors the story's Content Direction table, row for row. Adding, removing
# or renaming any of these is a copy change and must fail here until the
# owner has reviewed it.
_EXPECTED_INVENTORY = (
    "STATUS_CHIP_WAITING",
    "STATUS_CHIP_STARTING",
    "STATUS_CHIP_DONE",
    "app_line_downloading",
    "app_line_starting",
    "app_line_warming_up",
    "app_note_slow_start",
    "app_line_done",
    "app_line_error",
    "PHASE_HEADLINE_READY",
    "PHASE_HEADLINE_RUNNING",
    "PHASE_HEADLINE_WIRING",
    "PHASE_HEADLINE_FINALE",
    "app_headline_starting",
    "app_headline_done",
    "refusal_populated_target",
    "refusal_path_missing",
    "refusal_not_a_folder",
    "refusal_not_writable",
    "refusal_system_path",
    "refusal_name_clash",
    "REFUSAL_UNKNOWN_APP",
    "REFUSAL_PATH_EMPTY",
    "STORAGE_CHECK_OK_MESSAGE",
    "storage_check_message",
    "STATUS_CHIP_ERROR",
    "FAILURE_DOCKER_UNREACHABLE",
    "failure_download_failed",
    "failure_never_became_ready",
    "failure_port_in_use",
    "failure_compose_failed",
    "REFUSAL_NOTHING_CHOSEN",
    "PROWLARR_DESCRIPTION",
    "SONARR_DESCRIPTION",
    "RADARR_DESCRIPTION",
    "COMPOSE_FILE_HEADER_COMMENT",
    "HOST_MOUNT_COMMENT",
    "DATA_MOUNT_COMMENT",
    "MARKER_WHAT_IS_THIS",
    "refusal_not_shared",
    "WIZARD_TITLE_APPS",
    "WIZARD_TITLE_DRIVE",
    "WIZARD_EYEBROW",
    "WIZARD_STEP_APPS",
    "WIZARD_STEP_DRIVE",
    "WIZARD_STEP_DEPLOY",
    "WIZARD_BACK",
    "WIZARD_APPS_HEADLINE",
    "WIZARD_APPS_LEDE",
    "WIZARD_CONTINUE",
    "WIZARD_PICK_AT_LEAST_ONE",
    "WIZARD_PLATFORM_WARNING",
    "WIZARD_DRIVE_HEADLINE",
    "WIZARD_DRIVE_LEDE",
    "WIZARD_PATH_LABEL",
    "WIZARD_PATH_PLACEHOLDER",
    "wizard_path_hint",
    "WIZARD_FRESH_START_NOTE",
    "wizard_folder_found",
    "WIZARD_FOUND_ROOM_UNKNOWN",
    "WIZARD_NOT_WHOLE_PATH",
    "wizard_did_you_mean",
    "wizard_use_suggestion",
    "WIZARD_MISSING_NO_SUGGESTION",
    "wizard_other_drive_hint",
    "WIZARD_CHECKING",
    "WIZARD_CONTINUE_TO_DEPLOY",
    "free_space_terabytes",
    "free_space_gigabytes",
    "FREE_SPACE_UNDER_ONE_GB",
    "DIAGNOSTICS_LEDE",
    "WIZARD_TIMEZONE_LABEL",
    "WIZARD_TIMEZONE_HINT",
    "WIZARD_TIMEZONE_UTC",
    "TIMEZONE_REGION_LABELS",
    "wiring_line_app_sync",
    "wiring_line_root_folder",
    "MEDIA_FOLDER_LABEL",
    "WIRING_CHIP_RUNNING",
    "WIRING_CHIP_DONE",
    "WIRING_CHIP_SKIPPED",
    "WIRING_CHIP_ERROR",
    "WIRING_NOTE_ALREADY_CONNECTED",
    "WIRING_NOTHING_TO_CONNECT",
    "wiring_skip_no_prowlarr",
    "WIRING_SKIP_PROWLARR_ALONE",
    "wiring_note_still_waking",
    "wiring_failure_unreachable",
    "wiring_failure_refused",
    "wiring_failure_folder",
    "wiring_failure_prowlarr_too_old",
    "wiring_finale_note",
    "DEPLOY_TITLE",
    "DEPLOY_EYEBROW",
    "DEPLOY_HEADLINE",
    "DEPLOY_LEDE",
    "bill_line",
    "bill_one_app",
    "DOWNLOADS_LABEL",
    "DEPLOY_BUTTON",
    "DEPLOY_MICROCOPY",
    "RUN_SUB",
    "WIRING_STEP_TEMPLATE",
    "wiring_step_label",
    "FINALE_BADGE",
    "FINALE_HEADLINE",
    "FINALE_SUB",
    "FINALE_CTA",
    "SHOWTIMES_TITLE",
    "open_app_label",
    "DEPLOY_TRY_AGAIN",
    "DEPLOY_AGAIN",
    "SEE_TECHNICAL_DETAILS",
    "NOSCRIPT_REFRESH_NOTE",
    "LAST_PROBLEM_TITLE",
    "LAST_PROBLEM_INTRO",
    "LAST_PROBLEM_EMPTY",
    "COPY_BUTTON",
    "COPY_DONE",
    "COPY_BLOCKED",
    "HUB_TITLE",
    "HUB_EYEBROW",
    "HUB_HEADLINE",
    "HUB_LEDE",
    "HUB_STATUS_LABEL",
    "HUB_CHIP_UP",
    "HUB_CHIP_STARTING",
    "HUB_CHIP_DOWN",
    "HUB_CHIP_UNKNOWN",
    "hub_line_starting",
    "hub_line_down_last_seen",
    "hub_line_down",
    "hub_line_gone",
    "hub_line_unknown",
    "hub_line_no_address",
    "hub_open_app_aria",
    "HUB_DOWN_NOTE",
    "HUB_DOCKER_BANNER",
    "HUB_PROXY_BANNER",
    "HUB_STALE_NOTE",
    "HUB_NOSCRIPT_NOTE",
    "HUB_ALL_UP",
    "hub_some_up",
    "HUB_NOTHING_SET_UP",
    "HUB_DIAGNOSTICS_LINK",
    "relative_time",
)

_DEPLOY_ENGINE_WORD_COUNT = 40
_WIZARD_WORD_COUNT = 35
_WIRING_WORD_COUNT = 17
_DEPLOY_SCREEN_WORD_COUNT = 28


def test_words_inventory_is_pinned() -> None:
    assert words.WORDS_INVENTORY == _EXPECTED_INVENTORY


def test_every_public_name_in_words_module_is_listed_in_the_inventory() -> None:
    """Nothing in words.py can be added without also landing in the inventory.

    A new constant that forgets to join WORDS_INVENTORY would otherwise be
    real, working copy that never gets the owner's review pass.
    """
    non_copy_names = {"WORDS_INVENTORY", "annotations"}
    public_names = {
        name for name in vars(words) if not name.startswith("_") and name not in non_copy_names
    }

    assert public_names == set(words.WORDS_INVENTORY)


def test_status_chips_match_the_mockups_exact_vocabulary() -> None:
    # The deploy screen sets its data-state attribute straight from these
    # values, so a synonym here would be a stylesheet selector that
    # silently stops matching.
    assert words.STATUS_CHIP_WAITING == "Waiting"
    assert words.STATUS_CHIP_STARTING == "Starting…"
    assert words.STATUS_CHIP_DONE == "Ready"


def test_app_line_functions_fill_in_the_apps_own_name() -> None:
    assert words.app_line_downloading("Sonarr") == (
        "Downloading Sonarr - this only happens the first time"
    )
    assert words.app_line_starting("Sonarr") == "Starting Sonarr"
    assert words.app_line_warming_up("Sonarr") == "Sonarr is waking up"
    assert words.app_line_done("Sonarr") == "Sonarr is ready"


def test_the_slow_start_note_is_the_mockups_reassurance_sentence() -> None:
    assert words.app_note_slow_start("Sonarr") == (
        "Sonarr is taking a little longer than usual - this is normal on first start."
    )


def test_refusal_functions_carry_the_typed_path_or_name() -> None:
    assert "/volume1/media" in words.refusal_populated_target("/volume1/media")
    assert "/vol1/mdia" in words.refusal_path_missing("/vol1/mdia")
    assert "/volume1/media.txt" in words.refusal_not_a_folder("/volume1/media.txt")
    assert "/volume1/locked" in words.refusal_not_writable("/volume1/locked")
    assert "/etc" in words.refusal_system_path("/etc")
    assert "sonarr" in words.refusal_name_clash("sonarr")


def test_failure_functions_carry_the_apps_own_name_and_port() -> None:
    assert "Sonarr" in words.failure_download_failed("Sonarr")
    assert "Sonarr" in words.failure_never_became_ready("Sonarr")
    assert "Sonarr" in words.failure_compose_failed("Sonarr")
    port_message = words.failure_port_in_use("Sonarr", 8989)
    assert "Sonarr" in port_message
    assert "8989" in port_message


def test_error_state_has_its_own_chip_and_line() -> None:
    # The mockup has no hook for a failed app - this is the deploy engine's
    # own fourth state, additive to the three the mockup already styles.
    assert words.STATUS_CHIP_ERROR
    assert words.STATUS_CHIP_ERROR not in (
        words.STATUS_CHIP_WAITING,
        words.STATUS_CHIP_STARTING,
        words.STATUS_CHIP_DONE,
    )
    assert "Sonarr" in words.app_line_error("Sonarr")


def test_per_app_headline_functions_name_the_app() -> None:
    assert "Sonarr" in words.app_headline_starting("Sonarr")
    assert "Sonarr" in words.app_headline_done("Sonarr")


def test_storage_check_message_picks_the_right_sentence_per_reason() -> None:
    assert words.storage_check_message(None, "/volume1/media") == words.STORAGE_CHECK_OK_MESSAGE
    assert words.storage_check_message("empty", "") == words.REFUSAL_PATH_EMPTY
    assert "/vol1/mdia" in words.storage_check_message("missing", "/vol1/mdia")
    assert "/etc" in words.storage_check_message("system_path", "/etc")


def test_refusal_not_shared_names_the_visible_roots() -> None:
    message = words.refusal_not_shared("/mnt/storage", ["/volume1"])

    assert "/mnt/storage" in message
    assert "/volume1" in message


def test_refusal_not_shared_falls_back_when_there_are_no_visible_roots() -> None:
    message = words.refusal_not_shared("/mnt/storage", [])

    assert "/mnt/storage" in message
    assert "Pick a folder in there" not in message


def test_refusal_not_shared_falls_back_when_there_are_more_than_three_visible_roots() -> None:
    message = words.refusal_not_shared(
        "/mnt/storage", ["/volume1", "/volume2", "/volume3", "/volume4"]
    )

    assert "/mnt/storage" in message
    assert "Pick a folder in there" not in message
    assert "/volume4" not in message


def test_refusal_not_shared_first_sentence_ends_at_the_first_full_stop() -> None:
    message = words.refusal_not_shared("/mnt/storage", ["/volume1"])

    headline, separator, rest = message.partition(". ")
    assert separator == ". "
    assert headline
    assert rest


def test_storage_check_message_and_the_deploy_refusal_map_both_handle_not_shared() -> None:
    assert "/mnt/storage" in words.storage_check_message("not_shared", "/mnt/storage")
    assert "/mnt/storage" in deploy._STORAGE_REFUSAL_WORDS["not_shared"]("/mnt/storage")


def test_the_words_inventory_is_deploy_names_then_wizard_names_then_wiring_names() -> None:
    wizard_end = _DEPLOY_ENGINE_WORD_COUNT + _WIZARD_WORD_COUNT
    wiring_end = wizard_end + _WIRING_WORD_COUNT
    deploy_screen_end = wiring_end + _DEPLOY_SCREEN_WORD_COUNT

    deploy_names = words.WORDS_INVENTORY[:_DEPLOY_ENGINE_WORD_COUNT]
    wizard_names = words.WORDS_INVENTORY[_DEPLOY_ENGINE_WORD_COUNT:wizard_end]
    wiring_names = words.WORDS_INVENTORY[wizard_end:wiring_end]
    deploy_screen_names = words.WORDS_INVENTORY[wiring_end:deploy_screen_end]

    assert deploy_names == _EXPECTED_INVENTORY[:_DEPLOY_ENGINE_WORD_COUNT]
    assert wizard_names == _EXPECTED_INVENTORY[_DEPLOY_ENGINE_WORD_COUNT:wizard_end]
    assert wiring_names == _EXPECTED_INVENTORY[wizard_end:wiring_end]
    assert deploy_screen_names == _EXPECTED_INVENTORY[wiring_end:deploy_screen_end]
    assert "WIZARD_TITLE_APPS" not in deploy_names
    assert "refusal_not_shared" in deploy_names
    assert "wiring_line_app_sync" not in deploy_names
    assert "wiring_line_app_sync" not in wizard_names
    assert "DEPLOY_TITLE" not in wiring_names
    assert "wiring_finale_note" not in deploy_screen_names
    assert "HUB_TITLE" not in deploy_screen_names


def test_the_hub_names_are_the_last_section() -> None:
    """The Hub section follows the Deploy screen section, and its own slice
    is open-ended - it's the newest section, so nothing else follows it yet.
    """
    deploy_screen_end = (
        _DEPLOY_ENGINE_WORD_COUNT
        + _WIZARD_WORD_COUNT
        + _WIRING_WORD_COUNT
        + _DEPLOY_SCREEN_WORD_COUNT
    )

    hub_names = words.WORDS_INVENTORY[deploy_screen_end:]

    assert hub_names == _EXPECTED_INVENTORY[deploy_screen_end:]
    assert hub_names[0] == "HUB_TITLE"
    assert hub_names[-1] == "relative_time"
    assert "COPY_BLOCKED" not in hub_names


def test_wizard_headline_tuples_carry_the_gradient_word_in_the_middle() -> None:
    lead, accent, tail = words.WIZARD_APPS_HEADLINE
    assert accent == "media server"
    assert lead.endswith(" ")
    assert tail.startswith("?")

    lead, accent, tail = words.WIZARD_DRIVE_HEADLINE
    assert accent == "big drive"


def test_wizard_path_hint_names_up_to_three_visible_roots() -> None:
    hint = words.wizard_path_hint(["/volume1"])

    assert "/volume1" in hint
    assert "/volume1/media" in hint


def test_wizard_path_hint_falls_back_when_no_roots_are_visible() -> None:
    hint = words.wizard_path_hint([])

    assert hint == "On most NAS boxes this starts with /volume1."


def test_wizard_path_hint_falls_back_when_more_than_three_roots_are_visible() -> None:
    hint = words.wizard_path_hint(["/volume1", "/volume2", "/volume3", "/volume4"])

    assert hint == "On most NAS boxes this starts with /volume1."


def test_wizard_folder_found_names_the_free_space_words() -> None:
    assert words.wizard_folder_found("about 1.2 TB") == "Folder found - about 1.2 TB free"


def test_wizard_did_you_mean_and_use_suggestion_carry_the_suggested_path() -> None:
    assert "/volume1/movies" in words.wizard_did_you_mean("/volume1/movies")
    assert words.wizard_use_suggestion("/volume1/movies") == "Use /volume1/movies"


def test_wizard_other_drive_hint_names_the_separate_drive() -> None:
    hint = words.wizard_other_drive_hint("/volume2")

    assert "/volume2" in hint


def test_free_space_functions_match_the_content_direction_examples() -> None:
    assert words.free_space_terabytes(1.2) == "about 1.2 TB"
    assert words.free_space_gigabytes(340) == "about 340 GB"
    assert words.FREE_SPACE_UNDER_ONE_GB == "less than 1 GB"


def test_timezone_region_labels_cover_every_region_and_the_other_group() -> None:
    assert words.TIMEZONE_REGION_LABELS["America"] == "Americas"
    assert words.TIMEZONE_REGION_LABELS["Indian"] == "Indian Ocean"
    assert words.TIMEZONE_REGION_LABELS["Other"] == "Other"
    for region in (
        "Africa",
        "America",
        "Antarctica",
        "Asia",
        "Atlantic",
        "Australia",
        "Europe",
        "Indian",
        "Pacific",
        "Other",
    ):
        assert region in words.TIMEZONE_REGION_LABELS


def test_wizard_timezone_utc_names_utc_as_the_same_everywhere() -> None:
    assert words.WIZARD_TIMEZONE_UTC == "UTC (the same everywhere)"


_LOGGING_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Every string-literal node that is a module/class/function docstring."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_ids.add(id(first.value))
    return docstring_ids


def _logging_argument_ids(tree: ast.AST) -> set[int]:
    """The first argument of any `something.debug(...)`/`.info(...)`/etc. call."""
    logging_ids: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOGGING_METHODS
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            logging_ids.add(id(node.args[0]))
    return logging_ids


def _sentence_shaped_literals(source: str, filename: str) -> list[str]:
    """Long, sentence-shaped string literals in `source`, minus the allowed ones.

    "Sentence-shaped" mirrors the chunk's own definition: over 30 characters,
    containing both a space and a full stop. Docstrings and the message
    argument of a logging call are not copy a user ever sees, so they're
    excluded the same way.
    """
    tree = ast.parse(source, filename=filename)
    excluded = _docstring_ids(tree) | _logging_argument_ids(tree)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in excluded:
            continue
        text = node.value
        if len(text) > 30 and " " in text and "." in text:
            offenders.append(text)
    return offenders


def test_no_user_facing_sentence_lives_outside_words_py() -> None:
    # Top-level modules plus `wiring/*.py` and `routes/*.py` - not a
    # recursive glob. `routes/alive.py` is excluded by name: it's a
    # pre-existing, separately-flagged gap this test does not own.
    scanned_paths = [
        *sorted(_SRC_DIR.glob("*.py")),
        *sorted((_SRC_DIR / "wiring").glob("*.py")),
        *sorted((_SRC_DIR / "routes").glob("*.py")),
    ]

    offenders_by_file: dict[str, list[str]] = {}
    for path in scanned_paths:
        if path.name in ("words.py", "alive.py"):
            continue
        found = _sentence_shaped_literals(path.read_text(), filename=str(path))
        if found:
            offenders_by_file[path.name] = found

    assert offenders_by_file == {}


def test_wiring_line_functions_match_content_direction() -> None:
    assert words.wiring_line_app_sync("Prowlarr", "Sonarr") == "Introducing Prowlarr to Sonarr"
    assert (
        words.wiring_line_root_folder("Sonarr", "TV shows")
        == "Telling Sonarr where your TV shows live"
    )
    assert (
        words.wiring_line_root_folder("Radarr", "movies") == "Telling Radarr where your movies live"
    )


def test_wiring_note_already_connected_matches_content_direction() -> None:
    assert words.WIRING_NOTE_ALREADY_CONNECTED == "Already connected - nothing to change."


def test_wiring_chips_are_chosen_purely_from_state() -> None:
    assert words.WIRING_CHIP_RUNNING == "Connecting…"
    assert words.WIRING_CHIP_DONE == "Connected"
    assert words.WIRING_CHIP_SKIPPED == "Nothing to do"
    assert words.WIRING_CHIP_ERROR == "Couldn't connect"
    chips = {
        words.WIRING_CHIP_RUNNING,
        words.WIRING_CHIP_DONE,
        words.WIRING_CHIP_SKIPPED,
        words.WIRING_CHIP_ERROR,
    }
    assert len(chips) == 4


def test_wiring_plan_level_skip_words_match_content_direction() -> None:
    assert words.WIRING_NOTHING_TO_CONNECT == "Nothing to connect this time."
    assert "Sonarr" in words.wiring_skip_no_prowlarr("Sonarr")
    assert "Prowlarr" in words.wiring_skip_no_prowlarr("Sonarr")
    assert "Sonarr or Radarr" in words.WIRING_SKIP_PROWLARR_ALONE


def test_wiring_note_still_waking_names_the_app() -> None:
    assert words.wiring_note_still_waking("Sonarr") == (
        "Sonarr is still waking up - Marrquee is waiting for it."
    )


def test_wiring_failure_functions_carry_the_apps_own_name() -> None:
    assert "Sonarr" in words.wiring_failure_unreachable("Sonarr")
    assert "Sonarr" in words.wiring_failure_refused("Sonarr")
    assert "Diagnostics" in words.wiring_failure_refused("Sonarr")
    assert "last-failure.txt" not in words.wiring_failure_refused("Sonarr")
    assert "Sonarr" in words.wiring_failure_prowlarr_too_old("Sonarr")


def test_wiring_failure_folder_names_the_owners_host_path_only() -> None:
    message = words.wiring_failure_folder("Sonarr", "/volume1/media/data/media/tv")

    assert "/volume1/media/data/media/tv" in message
    assert "Sonarr" in message


def test_wiring_finale_note_names_every_failed_step_and_never_a_count() -> None:
    one = words.wiring_finale_note(("Introducing Prowlarr to Sonarr",))
    assert one == (
        "Your apps are all running. One connection didn't finish: "
        "Introducing Prowlarr to Sonarr. Press Deploy again to retry - it's safe to repeat."
    )
    assert not any(char.isdigit() for char in one)

    two = words.wiring_finale_note(
        ("Introducing Prowlarr to Sonarr", "Introducing Prowlarr to Radarr")
    )
    assert two == (
        "Your apps are all running. These connections didn't finish: "
        "Introducing Prowlarr to Sonarr, Introducing Prowlarr to Radarr. "
        "Press Deploy again to retry - it's safe to repeat."
    )
    assert not any(char.isdigit() for char in two)
    assert "Introducing Prowlarr to Sonarr" in two
    assert "Introducing Prowlarr to Radarr" in two


def test_every_media_folder_has_a_label() -> None:
    used = {media for app in catalog.CATALOG for media in app.media_folders}

    assert used
    assert used <= set(words.MEDIA_FOLDER_LABEL.keys())


def test_deploy_headline_puts_the_gradient_line_second() -> None:
    lead, gradient = words.DEPLOY_HEADLINE

    assert lead == "Tonight's feature:"
    assert gradient == "your media server"


def test_bill_line_functions_match_content_direction() -> None:
    assert words.bill_line(3, "Prowlarr") == "3 apps on the bill tonight, starting with Prowlarr."
    assert words.bill_one_app("Sonarr") == "One app on the bill tonight: Sonarr."


def test_wiring_step_label_formats_the_shared_template() -> None:
    assert words.WIRING_STEP_TEMPLATE == "Step {index} of {total}"
    assert words.wiring_step_label(2, 4) == "Step 2 of 4"


def test_open_app_label_names_the_app() -> None:
    assert words.open_app_label("Sonarr") == "Open Sonarr"


def test_last_problem_and_copy_words_match_content_direction() -> None:
    assert words.LAST_PROBLEM_TITLE == "Last problem"
    assert "secret keys" in words.LAST_PROBLEM_INTRO
    assert "Nothing has gone wrong" in words.LAST_PROBLEM_EMPTY
    assert words.COPY_BUTTON == "Copy"
    assert words.COPY_DONE == "Copied"
    assert "Ctrl+C" in words.COPY_BLOCKED
    assert "Cmd+C" in words.COPY_BLOCKED


def test_hub_chrome_matches_content_direction() -> None:
    assert words.HUB_TITLE == "Your media server · Marrquee"
    assert words.HUB_EYEBROW == "Now playing"
    lead, accent = words.HUB_HEADLINE
    assert lead == "Your"
    assert accent == "media server"
    assert words.HUB_LEDE == "Everything you set up, one click away."
    assert words.HUB_DIAGNOSTICS_LINK == "Check Marrquee's own health"


def test_hub_status_chips_are_the_owners_up_down_wording() -> None:
    assert words.HUB_STATUS_LABEL == "Status:"
    assert words.HUB_CHIP_UP == "Up"
    assert words.HUB_CHIP_STARTING == "Starting"
    assert words.HUB_CHIP_DOWN == "Down"
    assert words.HUB_CHIP_UNKNOWN == "Not sure"
    chips = {
        words.HUB_CHIP_UP,
        words.HUB_CHIP_STARTING,
        words.HUB_CHIP_DOWN,
        words.HUB_CHIP_UNKNOWN,
    }
    assert len(chips) == 4


def test_hub_line_functions_name_the_app_and_match_content_direction() -> None:
    assert words.hub_line_starting("Sonarr") == "Sonarr is starting up."
    assert (
        words.hub_line_down_last_seen("Radarr", "2 hours ago")
        == "Radarr stopped - last seen 2 hours ago."
    )
    assert words.hub_line_down("Radarr") == "Radarr isn't running right now."
    assert words.hub_line_gone("Radarr") == "Radarr isn't on this machine any more."
    assert words.hub_line_unknown("Sonarr") == "Marrquee couldn't check Sonarr just now."


def test_hub_line_no_address_names_the_app_and_its_port() -> None:
    message = words.hub_line_no_address("Sonarr", 8989)

    assert "Sonarr" in message
    assert "8989" in message


def test_hub_open_app_aria_follows_the_owner_approved_shape() -> None:
    assert words.hub_open_app_aria("Sonarr", "Up") == "Open Sonarr. Status: Up. Opens in a new tab."


def test_hub_notes_and_banners_match_content_direction() -> None:
    assert "NAS's own Docker app" in words.HUB_DOWN_NOTE
    assert "can't reach Docker" in words.HUB_DOCKER_BANNER
    assert "isn't your NAS's own" in words.HUB_PROXY_BANNER
    assert "Reload this page" in words.HUB_STALE_NOTE
    assert "JavaScript switched off" in words.HUB_NOSCRIPT_NOTE


def test_hub_announce_words_use_the_owners_up_down_wording() -> None:
    assert words.HUB_ALL_UP == "All your apps are up."
    assert words.hub_some_up(2, 3) == "2 of 3 apps are up."
    assert words.HUB_NOTHING_SET_UP == "No apps are set up yet."


def test_relative_time_boundaries() -> None:
    assert words.relative_time(0) == "just now"
    assert words.relative_time(59) == "just now"
    assert words.relative_time(60) == "a minute ago"
    assert words.relative_time(119) == "a minute ago"
    assert words.relative_time(120) == "2 minutes ago"
    assert words.relative_time(3599) == "59 minutes ago"
    assert words.relative_time(3600) == "an hour ago"
    assert words.relative_time(7199) == "an hour ago"
    assert words.relative_time(7200) == "2 hours ago"
    assert words.relative_time(86399) == "23 hours ago"
    assert words.relative_time(86400) == "yesterday"
    assert words.relative_time(172799) == "yesterday"
    assert words.relative_time(172800) == "2 days ago"
