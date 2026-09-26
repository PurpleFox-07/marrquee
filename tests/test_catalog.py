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
    RECYCLARR_APP_ID,
    AppRule,
    CatalogApp,
    app_host,
    apps_in_order,
    companions_for,
    description_for,
    get_app,
    require_port,
    riders_of,
    unavailable_reason,
)
from marrquee.words import (
    PROWLARR_DESCRIPTION,
    QBITTORRENT_DESCRIPTION,
    QBITTORRENT_DESCRIPTION_NO_VPN,
    RADARR_DESCRIPTION,
    RECYCLARR_DESCRIPTION,
    RECYCLARR_NEEDS_ARR,
    SONARR_DESCRIPTION,
)


def test_catalog_holds_the_three_arr_apps_gluetun_and_qbittorrent_in_deploy_order() -> None:
    ids = [app.id for app in CATALOG]

    assert ids == ["prowlarr", "sonarr", "radarr", "gluetun", "qbittorrent", "recyclarr"]
    orders = [app.order for app in CATALOG]
    assert orders == sorted(orders)


def test_the_three_arr_apps_are_unchanged_in_value() -> None:
    """Every arr catalog entry keeps its old values - the new fields only
    ever change behaviour when a caller sets them explicitly.
    """
    arr_apps = [app for app in CATALOG if app.kind == "arr"]
    assert len(arr_apps) == 3
    for app in arr_apps:
        assert app.default_ticked is True
        assert app.web_page is True
        assert app.rules == ()
        assert app.kind == "arr"
        assert app.offered is True
        assert app.api_key_style == "hex32"
        assert app.network_via is None


def test_gluetun_is_a_vpn_app_never_offered_and_not_ticked_by_default() -> None:
    gluetun = get_app("gluetun")

    assert gluetun.kind == "vpn"
    assert gluetun.offered is False
    assert gluetun.default_ticked is False
    assert gluetun.web_page is False
    assert gluetun.login_kind == "none"
    assert gluetun.image == "qmcgaw/gluetun:v3"
    assert gluetun.port == 8000
    assert gluetun.env_prefix == "GLUETUN"
    assert gluetun.media_folders == ()
    assert gluetun.needs_data_mount is False


def test_the_three_arr_apps_take_the_login_gluetun_takes_none_qbittorrent_its_own() -> None:
    kinds = {app.id: app.login_kind for app in CATALOG}

    assert kinds == {
        "prowlarr": "arr",
        "sonarr": "arr",
        "radarr": "arr",
        "gluetun": "none",
        "qbittorrent": "qbittorrent",
        "recyclarr": "none",
    }


def test_login_kind_default_is_none_the_fail_safe_value() -> None:
    """An app that forgets to set `login_kind` just never gets a login
    pushed to it - it never ends up locked out of one it doesn't have.
    """
    app = CatalogApp(
        id="stub",
        name="Stub",
        description="a stub app for this test only",
        image="example/stub:latest",
        port=1234,
        env_prefix="STUB",
        api_base="api/v1",
        media_folders=(),
        needs_data_mount=False,
        glyph="ST",
        order=99,
    )

    assert app.login_kind == "none"
    assert app.kind == "arr"
    assert app.offered is True


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
    assert descriptions_by_id["recyclarr"] == RECYCLARR_DESCRIPTION
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


# --- qBittorrent: the catalog entry, and its network-sharing helpers --------


def test_qbittorrent_sits_after_gluetun_and_rides_its_network() -> None:
    qbittorrent = get_app("qbittorrent")

    ids = [app.id for app in CATALOG]
    assert ids.index("qbittorrent") == ids.index("gluetun") + 1
    assert qbittorrent.network_via == "gluetun"
    assert qbittorrent.kind == "downloader"
    assert qbittorrent.image == "lscr.io/linuxserver/qbittorrent:5.2.3"
    assert qbittorrent.port == 8080
    assert qbittorrent.api_base == "api/v2"
    assert qbittorrent.media_folders == ()
    assert qbittorrent.needs_data_mount is True
    assert qbittorrent.default_ticked is False
    assert qbittorrent.web_page is True
    assert qbittorrent.offered is True
    assert qbittorrent.login_kind == "qbittorrent"
    assert qbittorrent.api_key_style == "qbt"
    assert qbittorrent.rules == ()
    assert qbittorrent.description == QBITTORRENT_DESCRIPTION


def test_app_host_follows_the_installed_ids() -> None:
    qbittorrent = get_app("qbittorrent")

    assert app_host(qbittorrent, ("gluetun", "qbittorrent")) == "gluetun"
    assert app_host(qbittorrent, ("sonarr", "qbittorrent")) == "qbittorrent"
    assert app_host(get_app("sonarr"), ()) == "sonarr"
    assert app_host(get_app("sonarr"), ("gluetun",)) == "sonarr"
    assert app_host(get_app("gluetun"), ("gluetun",)) == "gluetun"


def test_companions_for_adds_gluetun_only_when_it_is_missing() -> None:
    assert companions_for("qbittorrent", (), without_vpn=False) == ("gluetun",)
    assert companions_for("qbittorrent", ("sonarr",), without_vpn=False) == ("gluetun",)
    assert companions_for("qbittorrent", ("gluetun", "sonarr"), without_vpn=False) == ()
    assert companions_for("sonarr", (), without_vpn=False) == ()


def test_companions_for_drops_the_vpn_only_when_confirmed() -> None:
    assert companions_for("qbittorrent", (), without_vpn=True) == ()
    assert companions_for("qbittorrent", ("sonarr",), without_vpn=True) == ()
    # Gluetun already installed: nothing to drop either way.
    assert companions_for("qbittorrent", ("gluetun", "sonarr"), without_vpn=True) == ()
    # An app with no VPN companion at all is unaffected by the flag.
    assert companions_for("sonarr", (), without_vpn=True) == ()


def test_description_for_says_no_vpn_only_when_gluetun_is_missing() -> None:
    qbittorrent = get_app("qbittorrent")

    assert description_for(qbittorrent, ("sonarr", "qbittorrent")) == QBITTORRENT_DESCRIPTION_NO_VPN
    assert description_for(qbittorrent, ("gluetun", "qbittorrent")) == QBITTORRENT_DESCRIPTION
    assert description_for(get_app("sonarr"), ()) == SONARR_DESCRIPTION


def test_description_without_vpn_defaults_to_empty() -> None:
    assert get_app("sonarr").description_without_vpn == ""
    assert get_app("qbittorrent").description_without_vpn == QBITTORRENT_DESCRIPTION_NO_VPN


def test_riders_of_returns_present_apps_that_ride_the_given_network_in_catalog_order() -> None:
    assert riders_of("gluetun", ("qbittorrent", "sonarr", "gluetun")) == (get_app("qbittorrent"),)
    assert riders_of("gluetun", ("sonarr", "radarr")) == ()
    assert riders_of("qbittorrent", ("qbittorrent", "sonarr")) == ()


# --- Recyclarr: a "sync" app with no port -----------------------------------


def test_recyclarr_is_offered_unticked_and_has_no_port() -> None:
    recyclarr = get_app(RECYCLARR_APP_ID)

    assert recyclarr.id == "recyclarr"
    assert recyclarr.name == "Recyclarr"
    assert recyclarr.description == RECYCLARR_DESCRIPTION
    assert recyclarr.image == "ghcr.io/recyclarr/recyclarr:8.7.2"
    assert recyclarr.port is None
    assert recyclarr.env_prefix == "RECYCLARR"
    assert recyclarr.api_base == ""
    assert recyclarr.media_folders == ()
    assert recyclarr.needs_data_mount is False
    assert recyclarr.glyph == "RC"
    assert recyclarr.order == 5
    assert recyclarr.default_ticked is False
    assert recyclarr.web_page is False
    assert recyclarr.login_kind == "none"
    assert recyclarr.kind == "sync"
    assert recyclarr.offered is True
    assert recyclarr.api_key_style == "hex32"
    assert recyclarr.network_via is None
    assert recyclarr.description_without_vpn == ""
    assert recyclarr.rules == (
        AppRule(kind="needs_any", app_ids=("sonarr", "radarr"), reason=RECYCLARR_NEEDS_ARR),
    )


def test_recyclarr_sits_last_right_after_qbittorrent() -> None:
    ids = [app.id for app in CATALOG]

    assert ids[-1] == "recyclarr"
    assert ids.index("recyclarr") == ids.index("qbittorrent") + 1


def test_unavailable_reason_for_recyclarr_without_sonarr_or_radarr_is_recyclarr_needs_arr() -> None:
    recyclarr = get_app("recyclarr")

    assert unavailable_reason(recyclarr, ()) == RECYCLARR_NEEDS_ARR
    assert unavailable_reason(recyclarr, ("sonarr",)) is None
    assert unavailable_reason(recyclarr, ("radarr",)) is None


def test_require_port_raises_for_recyclarr_and_returns_8989_for_sonarr() -> None:
    assert require_port(get_app("sonarr")) == 8989

    with pytest.raises(ValueError, match="recyclarr has no port"):
        require_port(get_app("recyclarr"))
