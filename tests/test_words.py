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

from marrquee import words

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
)


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
    offenders_by_file: dict[str, list[str]] = {}
    for path in sorted(_SRC_DIR.glob("*.py")):
        if path.name == "words.py":
            continue
        found = _sentence_shaped_literals(path.read_text(), filename=str(path))
        if found:
            offenders_by_file[path.name] = found

    assert offenders_by_file == {}
