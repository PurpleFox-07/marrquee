"""Tests for InstallState persistence: save_state, load_state, new_api_key.

`load_state` must never raise - a settings folder the owner deleted, a
half-written file, or a future version this build has never heard of are
all real states, and each one has to render as "nothing chosen yet" rather
than crash the first page a real user sees.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from marrquee.state import STATE_VERSION, InstallState, load_state, new_api_key, save_state

_A_STATE = InstallState(
    version=STATE_VERSION,
    storage_root="/volume1/media",
    app_ids=("prowlarr", "sonarr", "radarr"),
    api_keys={
        "prowlarr": "a" * 32,
        "sonarr": "b" * 32,
        "radarr": "c" * 32,
    },
    puid=1000,
    pgid=1000,
    umask="002",
    timezone="Etc/UTC",
    created="2026-09-19T00:00:00+00:00",
)


def test_state_round_trips_through_save_and_load(tmp_path: Path) -> None:
    save_state(tmp_path, _A_STATE)

    loaded = load_state(tmp_path)

    assert loaded == _A_STATE
    assert loaded is not None
    assert loaded.app_ids == ("prowlarr", "sonarr", "radarr")
    assert dict(loaded.api_keys) == dict(_A_STATE.api_keys)


def test_state_file_is_written_0600_and_atomically(tmp_path: Path) -> None:
    save_state(tmp_path, _A_STATE)

    written_files = list(tmp_path.iterdir())
    assert len(written_files) == 1
    state_file = written_files[0]
    assert not state_file.name.endswith(".tmp")
    mode = state_file.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.parametrize(
    "make_file",
    [
        pytest.param(lambda path: None, id="missing"),
        pytest.param(lambda path: path.write_text(""), id="empty"),
        pytest.param(lambda path: path.write_text("{not json"), id="corrupt"),
        pytest.param(
            lambda path: path.write_text('{"version": 999}'),
            id="future-version",
        ),
    ],
)
def test_a_missing_empty_corrupt_or_future_version_state_file_loads_as_none(
    tmp_path: Path, make_file: Callable[[Path], object]
) -> None:
    state_path = tmp_path / "install.json"
    make_file(state_path)

    assert load_state(tmp_path) is None


def test_new_api_key_returns_32_lowercase_hex_characters_and_two_calls_differ() -> None:
    first = new_api_key()
    second = new_api_key()

    assert len(first) == 32
    assert first == first.lower()
    assert all(character in "0123456789abcdef" for character in first)
    assert first != second
