"""Tests for `hub.hub_view`: the Hub's pure view model.

No HTML and no Docker here - `AppHealth` values are built by hand, the same
way `tests/test_health.py` builds `ContainerSnapshot` values. The template
and the live JSON endpoint (Chunks 4-5) both read whatever this module
returns, so every poster word this story promises is proven here first.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

import marrquee.catalog as catalog_module
import marrquee.hub as hub_module
import marrquee.questions as questions_module
from marrquee import words
from marrquee.catalog import CATALOG, AppRule
from marrquee.deploy import AppAdd, Failure, WiringGap
from marrquee.health import AppHealth, HubState, LinkHealth, LinkState
from marrquee.hub import HUB_POLL_MS, HubPanel, HubTile, InstallRow, LinkTile, hub_panel, hub_view
from marrquee.links import LinkCard
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep

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


def _link(link_id: str, label: str, url: str) -> LinkCard:
    return LinkCard(id=link_id, label=label, url=url)


def _link_health(link_id: str, state: LinkState = "up") -> LinkHealth:
    return LinkHealth(link_id=link_id, state=state)


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


# --- Link tiles: hub_view's other kind of card -------------------------------


def test_a_down_link_keeps_its_url_and_says_the_hedged_line() -> None:
    card = _link("0123456789abcdef", "Router", "http://192.168.1.1")

    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        links=[card],
        link_healths=[_link_health(card.id, "down")],
    )

    tile = view.links[0]
    assert isinstance(tile, LinkTile)
    assert tile.link_id == card.id
    assert tile.url == card.url
    assert tile.state == "down"
    assert tile.line == words.HUB_LINK_LINE_DOWN
    assert tile.aria == "Open Router. Status: Down. Opens in a new tab."


def test_an_up_link_has_an_empty_line() -> None:
    card = _link("0123456789abcdef", "Router", "http://192.168.1.1")

    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        links=[card],
        link_healths=[_link_health(card.id, "up")],
    )

    tile = view.links[0]
    assert tile.state == "up"
    assert tile.line == ""
    assert tile.aria == "Open Router. Status: Up. Opens in a new tab."


def test_a_link_with_no_matching_health_reading_is_down() -> None:
    card = _link("0123456789abcdef", "Router", "http://192.168.1.1")

    view = hub_view([], [], authority=_AUTHORITY, proxied=False, now=_NOW, links=[card])

    assert view.links[0].state == "down"
    assert view.links[0].line == words.HUB_LINK_LINE_DOWN
    assert view.links[0].url == card.url


def test_link_tile_carries_its_glyph_label_and_address() -> None:
    card = _link("0123456789abcdef", "Home Assistant", "http://192.168.1.20:8123/lovelace")

    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        links=[card],
        link_healths=[_link_health(card.id, "up")],
    )

    tile = view.links[0]
    assert tile.glyph == "HA"
    assert tile.label == "Home Assistant"
    assert tile.address == "192.168.1.20:8123"


def test_announce_counts_links_separately_from_apps() -> None:
    apps = [_health("sonarr", state="up"), _health("radarr", state="up")]
    up_link = _link("0000000000000001", "Router", "http://a")
    down_link = _link("0000000000000002", "NAS", "http://b")

    no_links = hub_view(["sonarr", "radarr"], apps, authority=_AUTHORITY, proxied=False, now=_NOW)
    all_links_up = hub_view(
        ["sonarr", "radarr"],
        apps,
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        links=[up_link],
        link_healths=[_link_health(up_link.id, "up")],
    )
    one_link_down = hub_view(
        ["sonarr", "radarr"],
        apps,
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        links=[up_link, down_link],
        link_healths=[_link_health(up_link.id, "up"), _link_health(down_link.id, "down")],
    )

    assert no_links.announce == "All your apps are up."
    assert all_links_up.announce == "All your apps are up. All your links are up."
    assert one_link_down.announce == "All your apps are up. 1 of 2 links is down."


def test_a_down_link_never_raises_any_down_or_docker_unreachable() -> None:
    down_link = _link("0000000000000002", "NAS", "http://b")

    view = hub_view(
        ["sonarr"],
        [_health("sonarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        links=[down_link],
        link_healths=[_link_health(down_link.id, "down")],
    )

    assert view.any_down is False
    assert view.docker_unreachable is False


def test_installable_is_the_catalog_minus_the_deploy_in_catalog_order() -> None:
    view = hub_view(["radarr"], [_health("radarr")], authority=_AUTHORITY, proxied=False, now=_NOW)

    assert [app.id for app in view.installable] == ["prowlarr", "sonarr"]

    every_id = [app.id for app in CATALOG]
    full_view = hub_view(
        every_id,
        [_health(app_id) for app_id in every_id],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
    )

    assert full_view.installable == ()


# --- hub_panel: what the "+" tile's panel draws, from the query string -------


def test_a_missing_panel_query_is_closed() -> None:
    panel = hub_panel(None, None, ())

    assert panel == HubPanel(mode="closed", edit=None, label="", url="", error=None)


def test_an_unrecognised_panel_value_is_closed() -> None:
    panel = hub_panel("nonsense", None, ())

    assert panel.mode == "closed"


def test_choose_install_and_link_open_their_own_pane_with_no_edit_target() -> None:
    for mode in ("choose", "install", "link"):
        panel = hub_panel(mode, None, ())
        assert panel.mode == mode
        assert panel.edit is None
        assert panel.label == ""
        assert panel.url == ""
        assert panel.error is None


def test_edit_with_an_unknown_id_is_closed() -> None:
    card = _link("0123456789abcdef", "Router", "http://192.168.1.1")

    panel = hub_panel("edit", "ffffffffffffffff", [card])

    assert panel.mode == "closed"


def test_edit_with_no_id_at_all_is_closed() -> None:
    card = _link("0123456789abcdef", "Router", "http://192.168.1.1")

    panel = hub_panel("edit", None, [card])

    assert panel.mode == "closed"


# --- Adding, retrying, gaps and install rows --------------------------------


def _adding(
    app_id: str = "radarr",
    *,
    purpose: str = "add",
    state: str = "starting",
    line: str = "Starting Radarr",
    note: str | None = None,
    failure: Failure | None = None,
) -> AppAdd:
    return AppAdd(
        app_id=app_id,
        purpose=purpose,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        line=line,
        note=note,
        failure=failure,
        wiring=(),
        compose_ran=False,
        started_at="2026-09-24T00:00:00+00:00",
    )


def _failure(
    headline: str = "Radarr couldn't be added.", what_to_do: str = "Try again."
) -> Failure:
    return Failure(
        code="compose_failed", headline=headline, what_to_do=what_to_do, technical="boom"
    )


def test_an_app_with_no_web_page_never_gets_a_url_even_when_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = tuple(
        dataclasses.replace(app, web_page=False) if app.id == "sonarr" else app
        for app in catalog_module.CATALOG
    )
    # `apps_in_order`/`get_app` read `catalog.CATALOG` at call time (they
    # live in catalog.py); `hub.installable` reads its own imported copy -
    # both have to see the same patched entry for this test to be honest.
    monkeypatch.setattr(catalog_module, "CATALOG", patched)
    monkeypatch.setattr(hub_module, "CATALOG", patched)

    view = hub_view(
        ["sonarr"], [_health("sonarr", state="up")], authority=_AUTHORITY, proxied=False, now=_NOW
    )

    tile = view.tiles[0]
    assert tile.url is None
    assert tile.aria is None
    assert tile.line == ""


def test_hub_tile_defaults_carry_no_add_state_no_note_and_no_actions() -> None:
    view = hub_view(["sonarr"], [_health("sonarr")], authority=_AUTHORITY, proxied=False, now=_NOW)

    tile = view.tiles[0]
    assert tile.add_state is None
    assert tile.note == ""
    assert tile.actions == "none"


def test_install_rows_exclude_the_app_being_added_and_grey_an_unavailable_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    needs_prowlarr = AppRule(kind="needs_any", app_ids=("prowlarr",), reason="needs Prowlarr first")
    patched = tuple(
        dataclasses.replace(app, rules=(needs_prowlarr,)) if app.id == "radarr" else app
        for app in catalog_module.CATALOG
    )
    # `hub.py` does `from marrquee.catalog import CATALOG`, which copies the
    # reference at import time - patching `catalog_module.CATALOG` alone
    # would never be seen here, so the name is patched where it's actually
    # read from.
    monkeypatch.setattr(hub_module, "CATALOG", patched)

    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="sonarr"),
    )

    ids = [row.app.id for row in view.install_rows]
    assert "sonarr" not in ids
    assert ids == ["prowlarr", "radarr"]
    by_id = {row.app.id: row for row in view.install_rows}
    assert isinstance(by_id["radarr"], InstallRow)
    assert by_id["prowlarr"].unavailable is None
    assert by_id["radarr"].unavailable == "needs Prowlarr first"


def test_install_rows_carry_each_apps_registered_question_steps() -> None:
    fixture_step = QuestionStep(
        app_id="prowlarr",
        step_id="fixture",
        title="Fixture",
        lede="A fixture step.",
        fields=(QuestionField(name="name", label="Name", kind="text"),),
        check=lambda answers: QuestionCheck(ok=True, answers=answers, problem=None, field=None),
    )
    original = questions_module.QUESTION_STEPS
    questions_module.QUESTION_STEPS = (fixture_step,)
    try:
        view = hub_view([], [], authority=_AUTHORITY, proxied=False, now=_NOW)
    finally:
        questions_module.QUESTION_STEPS = original

    by_id = {row.app.id: row for row in view.install_rows}
    assert by_id["prowlarr"].steps == (fixture_step,)
    assert by_id["sonarr"].steps == ()


def test_a_starting_add_gets_a_spotlit_tile_with_no_url() -> None:
    view = hub_view(
        ["sonarr"],
        [_health("sonarr")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="radarr", state="starting", line="Starting Radarr", note=None),
    )

    assert [tile.app_id for tile in view.tiles] == ["sonarr", "radarr"]
    tile = view.tiles[1]
    assert tile.state == "starting"
    assert tile.chip == words.HUB_CHIP_ADDING
    assert tile.line == "Starting Radarr"
    assert tile.url is None
    assert tile.aria is None
    assert tile.add_state == "starting"


def test_a_starting_add_prefers_its_note_over_its_line() -> None:
    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(line="Starting Radarr", note="Downloading Radarr - this only happens once"),
    )

    tile = view.tiles[0]
    assert tile.line == "Downloading Radarr - this only happens once"


def test_a_wiring_add_shows_the_connecting_chip_and_line() -> None:
    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(state="wiring"),
    )

    tile = view.tiles[0]
    assert tile.chip == words.WIRING_CHIP_RUNNING
    assert tile.line == words.hub_line_connecting("Radarr")
    assert tile.add_state == "wiring"
    assert tile.url is None


def test_a_failed_add_shows_the_failure_headline_and_offers_retry() -> None:
    failure = _failure("Radarr couldn't be added.", "Check your NAS's Docker app and try again.")
    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(state="error", line=words.app_line_error("Radarr"), failure=failure),
    )

    tile = view.tiles[0]
    assert tile.state == "down"
    assert tile.chip == words.HUB_CHIP_ADD_FAILED
    assert tile.line == "Radarr couldn't be added. Check your NAS's Docker app and try again."
    assert tile.actions == "retry"
    assert tile.url is None


def test_a_failed_cancel_keeps_its_own_line_instead_of_the_failure_text() -> None:
    failure = _failure()
    view = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(state="error", line=words.hub_cancel_failed("Radarr"), failure=failure),
    )

    tile = view.tiles[0]
    assert tile.line == words.hub_cancel_failed("Radarr")


def test_the_adding_tile_is_excluded_from_any_down_docker_unreachable_empty_and_the_up_count() -> (
    None
):
    view = hub_view(
        ["sonarr"],
        [_health("sonarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="radarr"),
    )

    assert view.any_down is False
    assert view.docker_unreachable is False
    assert view.empty is False
    assert view.announce.startswith(words.HUB_ALL_UP)


def test_announce_names_the_app_being_added_or_that_it_failed() -> None:
    starting = hub_view(
        [], [], authority=_AUTHORITY, proxied=False, now=_NOW, adding=_adding(app_id="radarr")
    )
    failed = hub_view(
        [],
        [],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="radarr", state="error", failure=_failure()),
    )

    assert starting.announce == f"{words.HUB_NOTHING_SET_UP} {words.hub_announce_adding('Radarr')}"
    assert failed.announce == (
        f"{words.HUB_NOTHING_SET_UP} {words.hub_announce_add_failed('Radarr')}"
    )


def test_a_reconnecting_installed_app_keeps_its_health_tile_but_shows_connecting() -> None:
    view = hub_view(
        ["sonarr"],
        [_health("sonarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="sonarr", purpose="reconnect", state="wiring"),
    )

    tile = view.tiles[0]
    assert tile.state == "up"
    assert tile.chip == words.HUB_CHIP_UP
    assert tile.line == words.hub_line_connecting("Sonarr")
    assert tile.add_state == "wiring"
    assert tile.url is not None


def test_a_failed_reconnect_offers_connect_again_never_cancel() -> None:
    failure = _failure("Sonarr couldn't be reconnected.", "Try again in a moment.")
    view = hub_view(
        ["sonarr"],
        [_health("sonarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(
            app_id="sonarr",
            purpose="reconnect",
            state="error",
            line=words.app_line_error("Sonarr"),
            failure=failure,
        ),
    )

    tile = view.tiles[0]
    assert tile.state == "up"
    assert tile.add_state == "error"
    assert tile.line == "Sonarr couldn't be reconnected. Try again in a moment."
    # "reconnect" offers only Connect again - never Cancel, which would
    # remove an already-installed, working app's own container.
    assert tile.actions == "reconnect"


def test_a_wiring_gap_tile_is_up_with_the_amber_note_and_reconnect_action() -> None:
    view = hub_view(
        ["sonarr"],
        [_health("sonarr", state="up")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        wiring_gaps=[
            WiringGap(app_id="sonarr", failed_lines=("Prowlarr wasn't told about Sonarr",))
        ],
    )

    tile = view.tiles[0]
    assert tile.state == "up"
    assert tile.note == words.hub_wiring_gap_note("Sonarr", ("Prowlarr wasn't told about Sonarr",))
    assert tile.actions == "reconnect"


def test_a_clean_reconnect_removes_the_gap_and_its_note() -> None:
    view = hub_view(
        ["sonarr"], [_health("sonarr", state="up")], authority=_AUTHORITY, proxied=False, now=_NOW
    )

    tile = view.tiles[0]
    assert tile.note == ""
    assert tile.actions == "none"


def test_install_block_reports_busy_or_a_waiting_failed_add_or_nothing() -> None:
    idle = hub_view(["sonarr"], [_health("sonarr")], authority=_AUTHORITY, proxied=False, now=_NOW)
    busy = hub_view(
        ["sonarr"],
        [_health("sonarr")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="radarr"),
        busy=True,
    )
    waiting_failed = hub_view(
        ["sonarr"],
        [_health("sonarr")],
        authority=_AUTHORITY,
        proxied=False,
        now=_NOW,
        adding=_adding(app_id="radarr", state="error", failure=_failure()),
        busy=False,
    )

    assert idle.install_block is None
    assert busy.install_block == words.hub_install_busy("Radarr")
    assert waiting_failed.install_block == words.hub_install_resolve_first("Radarr")
    assert idle.busy is False
    assert busy.busy is True


def test_hub_panel_prefills_the_stored_url_not_a_re_derived_one() -> None:
    card = _link("0123456789abcdef", "Home Assistant", "http://192.168.1.20:8123/lovelace")

    panel = hub_panel("edit", card.id, [card])

    assert panel.mode == "edit"
    assert panel.edit == card
    assert panel.label == "Home Assistant"
    # The stored URL, not `link_address(card.url)` - a re-derived value would
    # silently drop the path the owner actually saved.
    assert panel.url == "http://192.168.1.20:8123/lovelace"
    assert panel.error is None
