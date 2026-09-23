"""Tests for the install-save: the plain function `POST /api/install` wraps.

Story 4 will call this exact same function from a no-JavaScript install
page, so its behaviour has to be provable without FastAPI in the picture at
all - every test here builds a `Settings` and calls `install_apps` directly.
`tests/test_api.py` covers the same behaviour again, but through the route.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from marrquee.config import Settings
from marrquee.install import install_apps
from marrquee.state import load_state
from marrquee.words import REFUSAL_NOTHING_CHOSEN, REFUSAL_UNKNOWN_APP


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _fresh_root(settings: Settings) -> PurePosixPath:
    """Create an empty, never-deployed-to target and return its host path."""
    (settings.host_mount / "volume1" / "media").mkdir(parents=True)
    return PurePosixPath("/volume1/media")


def test_install_apps_saves_state_with_one_generated_key_per_app(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), ["sonarr", "radarr"])

    assert result.ok
    assert result.kind == "ok"
    assert result.state is not None
    assert set(result.state.api_keys) == {"sonarr", "radarr"}
    assert result.state.api_keys["sonarr"] != result.state.api_keys["radarr"]
    assert all(len(key) == 32 for key in result.state.api_keys.values())

    saved = load_state(settings.config_dir)
    assert saved == result.state


def test_install_apps_keeps_an_existing_key_on_a_repost(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    first = install_apps(settings, str(root), ["sonarr"])
    assert first.state is not None
    original_key = first.state.api_keys["sonarr"]

    second = install_apps(settings, str(root), ["sonarr"])

    assert second.ok
    assert second.state is not None
    assert second.state.api_keys["sonarr"] == original_key


def test_install_apps_generates_a_fresh_key_for_a_newly_added_app(tmp_path: Path) -> None:
    """Adding a second app on a re-post must not disturb the first app's key."""
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    first = install_apps(settings, str(root), ["sonarr"])
    assert first.state is not None
    sonarr_key = first.state.api_keys["sonarr"]

    second = install_apps(settings, str(root), ["sonarr", "radarr"])

    assert second.state is not None
    assert second.state.api_keys["sonarr"] == sonarr_key
    assert "radarr" in second.state.api_keys


def test_install_apps_orders_app_ids_by_catalog_order_regardless_of_input(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), ["radarr", "prowlarr", "sonarr"])

    assert result.state is not None
    assert result.state.app_ids == ("prowlarr", "sonarr", "radarr")


def test_install_apps_attaches_derived_ids_from_the_chosen_root(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), ["sonarr"])

    assert result.state is not None
    assert result.state.umask == "002"
    assert result.state.timezone
    assert result.state.puid >= 0
    assert result.state.pgid >= 0


def test_install_apps_refuses_an_empty_app_list_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), [])

    assert not result.ok
    assert result.kind == "invalid_input"
    assert result.message == REFUSAL_NOTHING_CHOSEN
    assert result.state is None
    assert load_state(settings.config_dir) is None


def test_install_apps_refuses_an_unknown_app_id_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), ["plex"])

    assert not result.ok
    assert result.kind == "invalid_input"
    assert result.message == REFUSAL_UNKNOWN_APP
    assert load_state(settings.config_dir) is None


def test_install_apps_refuses_a_missing_path_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = install_apps(settings, "/volume1/does-not-exist", ["sonarr"])

    assert not result.ok
    assert result.kind == "refused"
    assert result.message
    assert load_state(settings.config_dir) is None


def test_install_apps_refuses_a_populated_target_and_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    (settings.host_mount / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    (
        settings.host_mount / "volume1" / "media" / "data" / "media" / "tv" / "Old Show.mkv"
    ).write_text("")

    result = install_apps(settings, str(root), ["sonarr"])

    assert not result.ok
    assert result.kind == "refused"
    assert "files" in result.message
    assert load_state(settings.config_dir) is None


def test_install_apps_saves_an_explicit_timezone_when_given_one(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), ["sonarr"], timezone="Europe/London")

    assert result.ok
    assert result.state is not None
    assert result.state.timezone == "Europe/London"


def test_install_apps_keeps_todays_behaviour_when_no_timezone_is_given(tmp_path: Path) -> None:
    """`timezone=None` (today's only caller, `/api/install`) must derive the
    zone exactly as it did before this keyword existed.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    result = install_apps(settings, str(root), ["sonarr"])

    assert result.ok
    assert result.state is not None
    assert result.state.timezone == "Etc/UTC"


def test_install_apps_can_be_reposted_against_the_same_still_fresh_root(tmp_path: Path) -> None:
    """`install_apps` itself never builds folders or writes the marker - that
    is the deploy engine's job - so a repeat call before any deploy has run
    sees the same still-empty root both times and must not be refused.
    """
    settings = _settings(tmp_path)
    root = _fresh_root(settings)

    first = install_apps(settings, str(root), ["sonarr"])
    assert first.ok

    second = install_apps(settings, str(root), ["sonarr"])

    assert second.ok
