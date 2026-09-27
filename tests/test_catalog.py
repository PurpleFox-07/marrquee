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
    EXISTING_PLEX_APP_ID,
    JELLYFIN_APP_ID,
    MEDIA_SERVER_APP_IDS,
    PLEX_APP_ID,
    RECYCLARR_APP_ID,
    SEERR_APP_ID,
    AppRule,
    CatalogApp,
    app_host,
    apps_in_order,
    companions_for,
    description_for,
    get_app,
    media_server_of,
    require_port,
    riders_of,
    unavailable_reason,
)
from marrquee.words import (
    EXCLUDED_BY_EXISTING_PLEX,
    EXISTING_PLEX_DESCRIPTION,
    EXISTING_PLEX_EXCLUDES_PLEX,
    JELLYFIN_DESCRIPTION,
    JELLYFIN_EXCLUDES_PLEX,
    PLEX_DESCRIPTION,
    PLEX_EXCLUDES_JELLYFIN,
    PROWLARR_DESCRIPTION,
    QBITTORRENT_DESCRIPTION,
    QBITTORRENT_DESCRIPTION_NO_VPN,
    RADARR_DESCRIPTION,
    RECYCLARR_DESCRIPTION,
    RECYCLARR_NEEDS_ARR,
    SEERR_DESCRIPTION,
    SEERR_NEEDS_ARR,
    SEERR_NEEDS_PLEX_OR_JELLYFIN,
    SONARR_DESCRIPTION,
)


def test_catalog_holds_the_three_arr_apps_gluetun_and_qbittorrent_in_deploy_order() -> None:
    ids = [app.id for app in CATALOG]

    assert ids == [
        "prowlarr",
        "sonarr",
        "radarr",
        "gluetun",
        "qbittorrent",
        "recyclarr",
        "plex",
        "jellyfin",
        "existing-plex",
        "seerr",
    ]
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
        "plex": "none",
        "jellyfin": "jellyfin",
        "existing-plex": "none",
        "seerr": "none",
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
    assert descriptions_by_id["plex"] == PLEX_DESCRIPTION
    assert descriptions_by_id["jellyfin"] == JELLYFIN_DESCRIPTION
    assert descriptions_by_id["existing-plex"] == EXISTING_PLEX_DESCRIPTION
    assert descriptions_by_id["seerr"] == SEERR_DESCRIPTION
    assert PROWLARR_DESCRIPTION == "Your search sources, managed in one place."
    assert SONARR_DESCRIPTION == "Finds and organizes your TV shows."
    assert RADARR_DESCRIPTION == "Finds and organizes your movies."


def test_unknown_app_id_raises_key_error_not_a_silent_empty_result() -> None:
    with pytest.raises(KeyError):
        get_app("not-a-real-app")


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


def test_recyclarr_sits_right_after_qbittorrent() -> None:
    ids = [app.id for app in CATALOG]

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


# --- Plex: a media-server app with no key of its own, its own library ------
# folders, and its own web path -----------------------------------------------


def test_plex_sits_last_right_after_recyclarr() -> None:
    ids = [app.id for app in CATALOG]

    assert ids.index("plex") == ids.index("recyclarr") + 1


def test_plex_facts_match_the_verified_image_docs() -> None:
    plex = get_app(PLEX_APP_ID)

    assert plex.id == "plex"
    assert plex.name == "Plex"
    assert plex.description == PLEX_DESCRIPTION
    assert plex.image == "lscr.io/linuxserver/plex:latest"
    assert plex.port == 32400
    assert plex.env_prefix == "PLEX"
    assert plex.api_base == ""
    assert plex.media_folders == ()
    assert plex.needs_data_mount is False
    assert plex.glyph == "PX"
    assert plex.order == 6
    assert plex.default_ticked is False
    assert plex.web_page is True
    assert plex.login_kind == "none"
    assert plex.kind == "media_server"
    assert plex.offered is True
    # No `api_key_source` field exists - Plex mints an ordinary, unused
    # Marrquee key the same way Recyclarr does.
    assert plex.api_key_style == "hex32"
    assert plex.web_path == "/web"
    assert plex.library_folders == ("movies", "tv")


def test_plex_is_unavailable_beside_jellyfin() -> None:
    plex = get_app(PLEX_APP_ID)

    assert unavailable_reason(plex, ("jellyfin",)) == PLEX_EXCLUDES_JELLYFIN
    assert unavailable_reason(plex, ()) is None
    assert unavailable_reason(plex, ("sonarr",)) is None


# --- Jellyfin: a media-server app that excludes, and is excluded by, Plex ---


def test_jellyfin_sits_right_after_plex() -> None:
    ids = [app.id for app in CATALOG]

    assert ids.index("jellyfin") == ids.index("plex") + 1


def test_jellyfin_facts_match_the_verified_image_docs() -> None:
    jellyfin = get_app(JELLYFIN_APP_ID)

    assert jellyfin.id == "jellyfin"
    assert jellyfin.name == "Jellyfin"
    assert jellyfin.description == JELLYFIN_DESCRIPTION
    assert jellyfin.image == "lscr.io/linuxserver/jellyfin:latest"
    assert jellyfin.port == 8096
    assert jellyfin.env_prefix == "JELLYFIN"
    assert jellyfin.api_base == ""
    assert jellyfin.media_folders == ()
    assert jellyfin.needs_data_mount is False
    assert jellyfin.glyph == "JF"
    assert jellyfin.order == 7
    assert jellyfin.default_ticked is False
    assert jellyfin.web_page is True
    assert jellyfin.login_kind == "jellyfin"
    assert jellyfin.kind == "media_server"
    assert jellyfin.offered is True
    assert jellyfin.api_key_style == "hex32"
    assert jellyfin.web_path == "/"
    assert jellyfin.library_folders == ("movies", "tv")


def test_jellyfin_and_plex_exclude_each_other_in_both_directions() -> None:
    plex = get_app(PLEX_APP_ID)
    jellyfin = get_app(JELLYFIN_APP_ID)

    assert unavailable_reason(jellyfin, ("plex",)) == JELLYFIN_EXCLUDES_PLEX
    assert unavailable_reason(jellyfin, ()) is None
    assert unavailable_reason(plex, ("jellyfin",)) == PLEX_EXCLUDES_JELLYFIN
    assert unavailable_reason(plex, ()) is None


def test_media_server_of_picks_plex() -> None:
    found = media_server_of(("sonarr", "plex", "radarr"))
    assert found is not None
    assert found.id == "plex"


def test_media_server_of_returns_none_without_one() -> None:
    assert media_server_of(("sonarr", "radarr")) is None
    assert media_server_of(()) is None


def test_media_server_app_ids_names_plex_jellyfin_and_existing_plex() -> None:
    assert MEDIA_SERVER_APP_IDS == ("plex", "jellyfin", "existing-plex")


# --- existing-plex: a media-server app Marrquee connects to but never ------
# deploys ----------------------------------------------------------------------


def test_existing_plex_sits_right_after_jellyfin() -> None:
    """Seerr now follows it - see `test_seerr_sits_last_right_after_existing_plex`."""
    ids = [app.id for app in CATALOG]

    assert ids.index("existing-plex") == ids.index("jellyfin") + 1


def test_existing_plex_is_a_media_server_marrquee_never_deploys() -> None:
    existing_plex = get_app(EXISTING_PLEX_APP_ID)

    assert existing_plex.id == "existing-plex"
    assert existing_plex.name == "Plex"
    assert existing_plex.description == EXISTING_PLEX_DESCRIPTION
    assert existing_plex.image == ""
    assert existing_plex.port is None
    assert existing_plex.env_prefix == "EXISTING_PLEX"
    assert existing_plex.api_base == ""
    assert existing_plex.media_folders == ()
    assert existing_plex.needs_data_mount is False
    assert existing_plex.glyph == "PX"
    assert existing_plex.default_ticked is False
    assert existing_plex.web_page is True
    assert existing_plex.login_kind == "none"
    assert existing_plex.kind == "media_server"
    assert existing_plex.offered is True
    assert existing_plex.web_path == "/web"
    assert existing_plex.library_folders == ("movies", "tv")
    assert existing_plex.managed is False


def test_every_other_catalog_app_defaults_to_managed() -> None:
    for app in CATALOG:
        if app.id == EXISTING_PLEX_APP_ID:
            continue
        assert app.managed is True


def test_plex_jellyfin_and_existing_plex_exclude_each_other() -> None:
    plex = get_app(PLEX_APP_ID)
    jellyfin = get_app(JELLYFIN_APP_ID)
    existing_plex = get_app(EXISTING_PLEX_APP_ID)

    assert unavailable_reason(plex, ("jellyfin",)) == PLEX_EXCLUDES_JELLYFIN
    assert unavailable_reason(plex, ("existing-plex",)) == EXCLUDED_BY_EXISTING_PLEX
    assert unavailable_reason(plex, ()) is None

    assert unavailable_reason(jellyfin, ("plex",)) == JELLYFIN_EXCLUDES_PLEX
    assert unavailable_reason(jellyfin, ("existing-plex",)) == EXCLUDED_BY_EXISTING_PLEX
    assert unavailable_reason(jellyfin, ()) is None

    assert unavailable_reason(existing_plex, ("plex",)) == EXISTING_PLEX_EXCLUDES_PLEX
    assert unavailable_reason(existing_plex, ("jellyfin",)) == PLEX_EXCLUDES_JELLYFIN
    assert unavailable_reason(existing_plex, ()) is None


def test_media_server_of_finds_the_connected_plex() -> None:
    found = media_server_of(("sonarr", "existing-plex"))
    assert found is not None
    assert found.id == "existing-plex"


# --- seerr: the owner's "ask for a movie or show" door ----------------------


def test_seerr_sits_last_right_after_existing_plex() -> None:
    ids = [app.id for app in CATALOG]

    assert ids[-1] == "seerr"
    assert ids.index("seerr") == ids.index("existing-plex") + 1


def test_seerr_facts_match_the_verified_image_docs() -> None:
    seerr = get_app(SEERR_APP_ID)

    assert seerr.id == "seerr"
    assert seerr.name == "Seerr"
    assert seerr.description == SEERR_DESCRIPTION
    assert seerr.image == "ghcr.io/seerr-team/seerr:v3.4.1"
    assert seerr.port == 5055
    assert seerr.env_prefix == "SEERR"
    assert seerr.api_base == "api/v1"
    assert seerr.media_folders == ()
    assert seerr.needs_data_mount is False
    assert seerr.glyph == "SR"
    assert seerr.order == 9
    assert seerr.default_ticked is False
    assert seerr.web_page is True
    assert seerr.login_kind == "none"
    assert seerr.kind == "requests"
    assert seerr.offered is True
    # No `api_key_source` field exists - Seerr mints its key the same way
    # every other app does, and hands it over as `API_KEY`.
    assert seerr.api_key_style == "hex32"
    assert seerr.web_path == "/"
    assert seerr.library_folders == ()
    assert seerr.managed is True
    assert seerr.config_owner == (1000, 1000)


def test_seerr_rules_need_a_media_server_first_then_sonarr_or_radarr() -> None:
    """The first failing rule wins: with neither a media server nor an arr
    app present, the media-server reason is the one the row shows.
    """
    seerr = get_app(SEERR_APP_ID)

    assert unavailable_reason(seerr, ()) == SEERR_NEEDS_PLEX_OR_JELLYFIN
    assert unavailable_reason(seerr, ("jellyfin",)) == SEERR_NEEDS_ARR
    assert unavailable_reason(seerr, ("plex",)) == SEERR_NEEDS_ARR
    assert unavailable_reason(seerr, ("existing-plex",)) == SEERR_NEEDS_ARR
    assert unavailable_reason(seerr, ("jellyfin", "sonarr")) is None
    assert unavailable_reason(seerr, ("existing-plex", "radarr")) is None


def test_every_other_catalog_app_has_no_config_owner() -> None:
    for app in CATALOG:
        if app.id == SEERR_APP_ID:
            continue
        assert app.config_owner is None
