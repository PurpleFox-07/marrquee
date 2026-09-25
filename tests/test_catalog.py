"""Tests for the app catalog - the single source of app identity.

Every other module (folder planning, the compose file, the deploy engine,
the wizard's app list) is supposed to read `CATALOG` instead of writing an
app's port, image or env prefix down a second time, so these tests pin the
catalog's shape rather than any one module's use of it.
"""

from __future__ import annotations

import dataclasses

import pytest

from marrquee.catalog import (
    CATALOG,
    AppRule,
    CatalogApp,
    apps_in_order,
    get_app,
    unavailable_reason,
)
from marrquee.words import PROWLARR_DESCRIPTION, RADARR_DESCRIPTION, SONARR_DESCRIPTION


def test_catalog_holds_exactly_the_three_cycle_1_apps_in_deploy_order() -> None:
    ids = [app.id for app in CATALOG]

    assert ids == ["prowlarr", "sonarr", "radarr"]
    orders = [app.order for app in CATALOG]
    assert orders == sorted(orders)
    assert "qbittorrent" not in ids
    assert "gluetun" not in ids


def test_the_catalog_is_unchanged_in_value() -> None:
    """Every catalog entry keeps its old values - the new fields only ever
    change behaviour when a caller sets them explicitly.
    """
    assert len(CATALOG) == 3
    for app in CATALOG:
        assert app.default_ticked is True
        assert app.web_page is True
        assert app.rules == ()


def _with_rules(app_id: str, rules: tuple[AppRule, ...]) -> CatalogApp:
    return dataclasses.replace(get_app(app_id), rules=rules)


def test_needs_any_refuses_until_one_partner_is_present() -> None:
    app = _with_rules(
        "radarr",
        (
            AppRule(
                kind="needs_any", app_ids=("prowlarr", "sonarr"), reason="needs a search source"
            ),
        ),
    )

    assert unavailable_reason(app, ()) == "needs a search source"
    assert unavailable_reason(app, ("sonarr",)) is None
    assert unavailable_reason(app, ("prowlarr",)) is None


def test_excludes_any_refuses_once_one_partner_is_present() -> None:
    app = _with_rules(
        "radarr",
        (
            AppRule(
                kind="excludes_any", app_ids=("plex",), reason="you already have a media server"
            ),
        ),
    )

    assert unavailable_reason(app, ()) is None
    assert unavailable_reason(app, ("plex",)) == "you already have a media server"


def test_the_first_failing_rule_wins_even_when_a_later_rule_would_also_fail() -> None:
    app = _with_rules(
        "radarr",
        (
            AppRule(kind="needs_any", app_ids=("prowlarr",), reason="first reason"),
            AppRule(kind="excludes_any", app_ids=("plex",), reason="second reason"),
        ),
    )

    # prowlarr is absent (first rule fails) AND plex is present (second rule
    # fails too) - the declared order must decide, not which rule is checked.
    assert unavailable_reason(app, ("plex",)) == "first reason"

    # Only the second rule fails here, so it - and only it - can win.
    assert unavailable_reason(app, ("prowlarr", "plex")) == "second reason"


def test_unavailable_reason_is_none_with_no_rules() -> None:
    app = get_app("radarr")

    assert unavailable_reason(app, ()) is None
    assert unavailable_reason(app, ("prowlarr", "sonarr")) is None


def test_every_catalog_description_is_the_mockups_own_wording() -> None:
    descriptions_by_id = {app.id: app.description for app in CATALOG}

    assert descriptions_by_id["prowlarr"] == PROWLARR_DESCRIPTION
    assert descriptions_by_id["sonarr"] == SONARR_DESCRIPTION
    assert descriptions_by_id["radarr"] == RADARR_DESCRIPTION
    assert PROWLARR_DESCRIPTION == "Your search sources, managed in one place."
    assert SONARR_DESCRIPTION == "Finds and organizes your TV shows."
    assert RADARR_DESCRIPTION == "Finds and organizes your movies."


def test_unknown_app_id_raises_key_error_not_a_silent_empty_result() -> None:
    with pytest.raises(KeyError):
        get_app("plex")


def test_get_app_returns_the_matching_catalog_entry() -> None:
    app = get_app("sonarr")

    assert app.id == "sonarr"
    assert app.name == "Sonarr"


def test_prowlarr_facts_match_the_verified_image_docs() -> None:
    prowlarr = get_app("prowlarr")

    assert prowlarr.image == "lscr.io/linuxserver/prowlarr:latest"
    assert prowlarr.port == 9696
    assert prowlarr.env_prefix == "PROWLARR"
    assert prowlarr.api_base == "api/v1"
    assert prowlarr.media_folders == ()
    assert prowlarr.needs_data_mount is False
    assert prowlarr.glyph == "PR"


def test_sonarr_and_radarr_both_need_the_shared_data_mount() -> None:
    sonarr = get_app("sonarr")
    radarr = get_app("radarr")

    assert sonarr.image == "lscr.io/linuxserver/sonarr:latest"
    assert sonarr.port == 8989
    assert sonarr.env_prefix == "SONARR"
    assert sonarr.api_base == "api/v3"
    assert sonarr.media_folders == ("tv",)
    assert sonarr.needs_data_mount is True
    assert sonarr.glyph == "SN"

    assert radarr.image == "lscr.io/linuxserver/radarr:latest"
    assert radarr.port == 7878
    assert radarr.env_prefix == "RADARR"
    assert radarr.api_base == "api/v3"
    assert radarr.media_folders == ("movies",)
    assert radarr.needs_data_mount is True
    assert radarr.glyph == "RD"


def test_apps_in_order_returns_catalog_order_regardless_of_input_order() -> None:
    result = apps_in_order(["radarr", "prowlarr", "sonarr"])

    assert [app.id for app in result] == ["prowlarr", "sonarr", "radarr"]


def test_apps_in_order_only_returns_the_requested_ids() -> None:
    result = apps_in_order(["radarr"])

    assert [app.id for app in result] == ["radarr"]
