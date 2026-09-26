"""The single source of truth for which apps Marrquee can deploy.

Every other module - folder planning, the compose file, the deploy engine,
the wizard's app list - reads `CATALOG` instead of writing an app's port,
image or env prefix down a second time. Adding a new app later is one new
`CatalogApp` entry here and nothing else.

This module is a leaf on purpose: it imports nothing from `state`,
`storage`, `compose` or `deploy`, so nothing about how an app is deployed
can ever leak back into what an app *is*.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from marrquee.words import (
    PROWLARR_DESCRIPTION,
    QBITTORRENT_DESCRIPTION,
    RADARR_DESCRIPTION,
    SONARR_DESCRIPTION,
    VPN_DESCRIPTION,
)


@dataclass(frozen=True)
class AppRule:
    """One condition that must hold before an app can be added.

    `needs_any` fails when none of `app_ids` is present yet (Radarr needing
    a search source before it has anything to sync with); `excludes_any`
    fails when any of them is already present (a second media server, once
    one is already installed). `reason` is plain text the app defining the
    rule supplies directly - never a words.py lookup, so this module never
    has to import words.py just to hold a rule.
    """

    kind: Literal["needs_any", "excludes_any"]
    app_ids: tuple[str, ...]
    reason: str


# What kind of login an app takes, so login.py knows which apps to put the
# saved username and password on. "none" is the default and the fail-safe
# value - an app that has it wrongly just never gets a login pushed to it,
# it never ends up locked out of one it doesn't have. "qbittorrent" is its
# own kind rather than reusing "arr": it takes the login through its API
# key (`app/setPreferences`), never Sonarr/Radarr's cookie login, and it is
# never recreated by the login run's arr-only second phase (its login is
# already live once the first phase's POST succeeds).
LoginKind = Literal["arr", "none", "qbittorrent"]

# What kind of app this is, so the compose builder, the deploy engine and
# the Hub's poster all know which branch to take instead of an "arr" app
# faking fields it doesn't have. "arr" is the default - every media app
# that looks like Prowlarr, Sonarr or Radarr; "vpn" is Gluetun, the one app
# with no web page, no API key and its own compose shape; "downloader" is
# qBittorrent - it rides another app's network (`network_via`) instead of
# getting its own, and it is never reached with arr's `X-Api-Key` header.
AppKind = Literal["arr", "vpn", "downloader"]


@dataclass(frozen=True)
class CatalogApp:
    """Everything Marrquee needs to know about one app before it exists.

    `kind="vpn"` (Gluetun) selects a separate compose branch and readiness
    check instead of faking the arr fields (`api_base`, `login_kind`) it
    has no use for. `kind="downloader"` (qBittorrent) does the same for an
    app that rides another app's network instead of getting its own.
    `offered=False` keeps an app like that out of every screen that lets an
    owner choose it directly (the wizard's grid, the Hub's "+" panel, `GET
    /api/catalog`) while it stays a normal entry everywhere else (catalog
    order, its own question step, its own bring-up) - whatever app needs it
    adds it as a companion instead.

    `network_via`, when set, names the id of the app this one shares a
    Docker network with instead of joining `marrquee` on its own -
    `app_host`, `companions_for`, `riders_of` and `build_stack_plan`'s
    "never outside the VPN" refusal are its only readers. `api_key_style`
    picks the shape of the key `install.api_key_for` generates: "hex32" is
    the arr apps' own 32 lowercase-hex-character key; "qbt" is
    qBittorrent's own `qbt_`-prefixed, 28-character format.
    """

    id: str
    name: str
    description: str
    image: str
    port: int
    env_prefix: str
    api_base: str
    media_folders: tuple[str, ...]
    needs_data_mount: bool
    glyph: str
    order: int
    default_ticked: bool = True
    web_page: bool = True
    rules: tuple[AppRule, ...] = ()
    login_kind: LoginKind = "none"
    kind: AppKind = "arr"
    offered: bool = True
    api_key_style: Literal["hex32", "qbt"] = "hex32"
    network_via: str | None = None


# Prowlarr, Sonarr, Radarr, Gluetun (the VPN tunnel) and qBittorrent (the
# downloader). Gluetun is `offered=False`: it never appears as its own "+"
# choice or wizard tick - whatever needs it (qBittorrent, today) adds it as
# a companion instead. qBittorrent's `network_via="gluetun"` is what
# enforces "the downloader must never run outside the tunnel": it has no
# network or API key of its own, and `build_stack_plan` refuses any install
# that has it without Gluetun.
CATALOG: tuple[CatalogApp, ...] = (
    CatalogApp(
        id="prowlarr",
        name="Prowlarr",
        description=PROWLARR_DESCRIPTION,
        image="lscr.io/linuxserver/prowlarr:latest",
        port=9696,
        env_prefix="PROWLARR",
        api_base="api/v1",
        media_folders=(),
        needs_data_mount=False,
        glyph="PR",
        order=0,
        login_kind="arr",
    ),
    CatalogApp(
        id="sonarr",
        name="Sonarr",
        description=SONARR_DESCRIPTION,
        image="lscr.io/linuxserver/sonarr:latest",
        port=8989,
        env_prefix="SONARR",
        api_base="api/v3",
        media_folders=("tv",),
        needs_data_mount=True,
        glyph="SN",
        order=1,
        login_kind="arr",
    ),
    CatalogApp(
        id="radarr",
        name="Radarr",
        description=RADARR_DESCRIPTION,
        image="lscr.io/linuxserver/radarr:latest",
        port=7878,
        env_prefix="RADARR",
        api_base="api/v3",
        media_folders=("movies",),
        needs_data_mount=True,
        glyph="RD",
        order=2,
        login_kind="arr",
    ),
    CatalogApp(
        id="gluetun",
        name="VPN",
        description=VPN_DESCRIPTION,
        image="qmcgaw/gluetun:v3",
        port=8000,
        env_prefix="GLUETUN",
        api_base="v1",
        media_folders=(),
        needs_data_mount=False,
        glyph="VPN",
        order=3,
        default_ticked=False,
        web_page=False,
        kind="vpn",
        offered=False,
        login_kind="none",
    ),
    CatalogApp(
        id="qbittorrent",
        name="qBittorrent",
        description=QBITTORRENT_DESCRIPTION,
        image="lscr.io/linuxserver/qbittorrent:5.2.3",
        port=8080,
        env_prefix="QBITTORRENT",
        api_base="api/v2",
        media_folders=(),
        needs_data_mount=True,
        glyph="QB",
        order=4,
        default_ticked=False,
        web_page=True,
        kind="downloader",
        offered=True,
        login_kind="qbittorrent",
        network_via="gluetun",
        api_key_style="qbt",
    ),
)


def get_app(app_id: str) -> CatalogApp:
    """Look up one catalog entry by id.

    Raises KeyError for an unknown id rather than returning None - a typo'd
    app id is a bug to surface immediately, not a silent no-op.
    """
    for app in CATALOG:
        if app.id == app_id:
            return app
    raise KeyError(app_id)


def apps_in_order(app_ids: Iterable[str]) -> tuple[CatalogApp, ...]:
    """The given app ids as CatalogApp entries, always in catalog (deploy) order.

    Input order never matters here - this is what keeps folder creation,
    compose services and deploy order in lockstep regardless of how a caller
    collected the ids (a set, a wizard's click order, anything).
    """
    chosen = set(app_ids)
    return tuple(app for app in CATALOG if app.id in chosen)


def unavailable_reason(app: CatalogApp, present: Iterable[str]) -> str | None:
    """Why `app` can't be added right now, or `None` when every rule passes.

    `present` is whatever "already chosen" set matters to the caller - the
    Hub's installed app ids, or a wizard's other ticked ids - this function
    only ever compares against the ids it's given. Rules are checked in the
    order the catalog declares them, and the first failing rule wins; later
    rules never override an earlier verdict.
    """
    present_ids = set(present)
    for rule in app.rules:
        if rule.kind == "needs_any" and present_ids.isdisjoint(rule.app_ids):
            return rule.reason
        if rule.kind == "excludes_any" and not present_ids.isdisjoint(rule.app_ids):
            return rule.reason
    return None


def app_host(app: CatalogApp) -> str:
    """The hostname another container reaches `app` at, on the `marrquee`
    network.

    An app with no `network_via` has its own DNS name, `app.id`. An app
    that rides another one's network (qBittorrent, today) has none of its
    own - it answers only at that other app's hostname, since Docker's
    `network_mode: service:<x>` shares `<x>`'s whole network namespace.
    """
    return app.network_via or app.id


def companions_for(app_id: str, present: Iterable[str]) -> tuple[str, ...]:
    """The extra app(s) adding `app_id` must bring along with it.

    Today this is at most one id: the app `app_id` rides the network of,
    when it isn't already present. Returns `()` for an app with no
    `network_via`, or when its companion is already installed - adding
    qBittorrent a second time (after Gluetun already exists) brings nothing
    else with it.
    """
    app = get_app(app_id)
    if app.network_via is None:
        return ()
    if app.network_via in set(present):
        return ()
    return (app.network_via,)


def riders_of(app_id: str, present: Iterable[str]) -> tuple[CatalogApp, ...]:
    """Every present app that rides `app_id`'s network, in catalog order.

    Reads `present` rather than the whole catalog so a caller (the compose
    builder publishing a rider's port on `app_id`) only ever sees riders
    that are actually part of this install.
    """
    return tuple(app for app in apps_in_order(present) if app.network_via == app_id)
