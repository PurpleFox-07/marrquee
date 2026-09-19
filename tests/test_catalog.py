"""Tests for the app catalog - the single source of app identity.

Every other module (folder planning, the compose file, the deploy engine,
the wizard's app list) is supposed to read `CATALOG` instead of writing an
app's port, image or env prefix down a second time, so these tests pin the
catalog's shape rather than any one module's use of it.
"""

from __future__ import annotations

import pytest

from marrquee.catalog import CATALOG, apps_in_order, get_app
from marrquee.words import PROWLARR_DESCRIPTION, RADARR_DESCRIPTION, SONARR_DESCRIPTION


def test_catalog_holds_exactly_the_three_cycle_1_apps_in_deploy_order() -> None:
    ids = [app.id for app in CATALOG]

    assert ids == ["prowlarr", "sonarr", "radarr"]
    orders = [app.order for app in CATALOG]
    assert orders == sorted(orders)
    assert "qbittorrent" not in ids
    assert "gluetun" not in ids


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
