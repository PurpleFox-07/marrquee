"""Tests for `hub.hub_view`: the Hub's pure view model.

No HTML and no Docker here - `AppHealth` values are built by hand, the same
way `tests/test_health.py` builds `ContainerSnapshot` values. The template
and the live JSON endpoint (Chunks 4-5) both read whatever this module
returns, so every poster word this story promises is proven here first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from marrquee import words
from marrquee.health import AppHealth, HubState
from marrquee.hub import HUB_POLL_MS, HubTile, hub_view

_NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
_AUTHORITY = "192.168.1.50:7788"


def _health(
    app_id: str,
    *,
    state: HubState = "up",
    exists: bool = True,
    finished_at: str | None = None,
) -> AppHealth:
    return AppHealth(app_id=app_id, state=state, exists=exists, finished_at=finished_at)


def test_a_down_app_with_a_real_finished_at_says_when_it_was_last_seen() -> None:
    """FIRST TEST - the one sentence the story's Acceptance quotes verbatim."""
    finished_at = (_NOW - timedelta(hours=2)).isoformat()
    health = _health("radarr", state="down", finished_at=finished_at)

    view = hub_view(["radarr"], [health], authority=_AUTHORITY, proxied=False, now=_NOW)

    tile = view.tiles[0]
    assert tile.line == "Radarr stopped - last seen 2 hours ago."
    assert tile.url is None
    assert tile.aria is None


def test_a_future_or_naive_finished_at_has_no_last_seen() -> None:
    future = _health("radarr", state="down", finished_at="2999-01-01T00:00:00Z")
    naive = _health("radarr", state="down", finished_at="2026-09-23T10:00:00")

    future_view = hub_view(["radarr"], [future], authority=_AUTHORITY, proxied=False, now=_NOW)
    naive_view = hub_view(["radarr"], [naive], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert future_view.tiles[0].line == words.hub_line_down("Radarr")
    assert naive_view.tiles[0].line == words.hub_line_down("Radarr")


def test_a_gone_container_says_it_isnt_on_this_machine_any_more() -> None:
    health = _health("radarr", state="down", exists=False)

    view = hub_view(["radarr"], [health], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert view.tiles[0].line == "Radarr isn't on this machine any more."


def test_an_up_tile_with_an_address_has_a_url_an_aria_label_and_an_empty_line() -> None:
    health = _health("sonarr", state="up")

    view = hub_view(["sonarr"], [health], authority=_AUTHORITY, proxied=False, now=_NOW)

    tile = view.tiles[0]
    assert tile.url == "http://192.168.1.50:8989/"
    assert tile.aria == "Open Sonarr. Status: Up. Opens in a new tab."
    assert tile.line == ""


def test_a_down_or_starting_tile_has_no_url_and_no_aria_label() -> None:
    down = _health("sonarr", state="down")
    starting = _health("sonarr", state="starting")

    down_view = hub_view(["sonarr"], [down], authority=_AUTHORITY, proxied=False, now=_NOW)
    starting_view = hub_view(["sonarr"], [starting], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert down_view.tiles[0].url is None
    assert down_view.tiles[0].aria is None
    assert starting_view.tiles[0].url is None
    assert starting_view.tiles[0].aria is None
    assert starting_view.tiles[0].line == "Sonarr is starting up."


def test_a_not_sure_tile_keeps_its_url() -> None:
    health = _health("sonarr", state="unknown")

    view = hub_view(["sonarr"], [health], authority=_AUTHORITY, proxied=False, now=_NOW)

    tile = view.tiles[0]
    assert tile.url == "http://192.168.1.50:8989/"
    assert tile.aria == "Open Sonarr. Status: Not sure. Opens in a new tab."
    assert tile.line == "Marrquee couldn't check Sonarr just now."


def test_no_trustworthy_address_gives_the_no_address_line_and_no_url() -> None:
    up = _health("sonarr", state="up")
    unknown = _health("prowlarr", state="unknown")

    view = hub_view(["sonarr", "prowlarr"], [up, unknown], authority=None, proxied=False, now=_NOW)

    for tile in view.tiles:
        assert tile.url is None
        assert tile.aria is None
        assert "couldn't work out this machine's address" in tile.line


def test_tiles_come_back_in_catalog_order_and_unknown_ids_are_dropped() -> None:
    healths = [_health("radarr"), _health("sonarr"), _health("prowlarr")]

    view = hub_view(
        ["radarr", "sonarr", "prowlarr", "not-a-real-app"],
        healths,
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
    )

    assert [tile.app_id for tile in view.tiles] == ["prowlarr", "sonarr", "radarr"]


def test_an_app_with_no_matching_health_is_unknown() -> None:
    view = hub_view(["sonarr"], [], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert view.tiles[0].state == "unknown"


def test_docker_unreachable_only_when_every_tile_is_unknown() -> None:
    all_unknown = hub_view(
        ["sonarr", "radarr"],
        [_health("sonarr", state="unknown"), _health("radarr", state="unknown")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
    )
    mixed = hub_view(
        ["sonarr", "radarr"],
        [_health("sonarr", state="unknown"), _health("radarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
    )
    no_apps = hub_view([], [], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert all_unknown.docker_unreachable is True
    assert mixed.docker_unreachable is False
    assert no_apps.docker_unreachable is False
    assert no_apps.empty is True


def test_announce_says_all_up_some_up_or_nothing_set_up() -> None:
    all_up = hub_view(
        ["sonarr", "radarr"],
        [_health("sonarr", state="up"), _health("radarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
    )
    some_up = hub_view(
        ["sonarr", "radarr"],
        [_health("sonarr", state="up"), _health("radarr", state="down")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
    )
    nothing = hub_view([], [], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert all_up.announce == "All your apps are up."
    assert some_up.announce == "1 of 2 apps are up."
    assert nothing.announce == "No apps are set up yet."


def test_any_down_and_proxied_flags() -> None:
    view = hub_view(
        ["sonarr", "radarr"],
        [_health("sonarr", state="up"), _health("radarr", state="down")],
        authority=_AUTHORITY,
        proxied=True,
        now=_NOW,
    )

    assert view.any_down is True
    assert view.proxied is True
    assert view.empty is False


def test_hub_tile_carries_the_catalog_apps_glyph_name_and_description() -> None:
    view = hub_view(["sonarr"], [_health("sonarr")], authority=_AUTHORITY, proxied=False, now=_NOW)

    tile = view.tiles[0]
    assert isinstance(tile, HubTile)
    assert tile.glyph == "SN"
    assert tile.name == "Sonarr"
    assert tile.description == words.SONARR_DESCRIPTION


def test_hub_poll_ms_matches_the_owner_approved_polling_interval() -> None:
    assert HUB_POLL_MS == 15000
