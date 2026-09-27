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
from typing import Final, Literal

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
# already live once the first phase's POST succeeds). "jellyfin" is its own
# kind too: the login becomes Jellyfin's admin through its own API key
# (Marrquee's first-time setup door), never Sonarr/Radarr's cookie login,
# and - like qBittorrent - it is never recreated by the arr-only second
# phase either.
LoginKind = Literal["arr", "none", "qbittorrent", "jellyfin"]

# What kind of app this is, so the compose builder, the deploy engine and
# the Hub's poster all know which branch to take instead of an "arr" app
# faking fields it doesn't have. "arr" is the default - every media app
# that looks like Prowlarr, Sonarr or Radarr; "vpn" is Gluetun, the one app
# with no web page, no API key and its own compose shape; "downloader" is
# qBittorrent - it rides another app's network (`network_via`) instead of
# getting its own, and it is never reached with arr's `X-Api-Key` header.
# "sync" is Recyclarr: it has no web page and no port of its own either -
# it reuses "downloader"'s no-web-page poster pattern rather than a second
# poster mechanism, but is "ready" once its container is running, never
# through an HTTP probe. "media_server" is Plex (and, later, Jellyfin): the
# one app the owner actually watches through - it has its own compose
# shape and bring-up rules (no pre-generated API key, its own claim/sign-in
# door), and `library_folders` names the shared library it opens onto
# instead of a per-app media folder of its own. "requests" is Seerr: the
# owner's "ask for a movie or show" door - it takes an ordinary Marrquee
# key (handed over as `API_KEY`, never read back) and needs its own
# bring-up entirely; its config folder is chowned to its image's own user
# rather than the drive's owner (`config_owner`, below), because it runs as
# a fixed uid its own image picked, not whatever PUID the owner's drive uses.
AppKind = Literal["arr", "vpn", "downloader", "sync", "media_server", "requests"]


@dataclass(frozen=True)
class CatalogApp:
    """Everything Marrquee needs to know about one app before it exists.

    `kind="vpn"` (Gluetun) selects a separate compose branch and readiness
    check instead of faking the arr fields (`api_base`, `login_kind`) it
    has no use for. `kind="downloader"` (qBittorrent) does the same for an
    app that rides another app's network instead of getting its own.
    `kind="sync"` (Recyclarr) has no web page and no port at all - `port`
    is `None` for it, and every reader of `port` either narrows through
    `require_port` (a real bug if one is ever reached for this app) or is
    written to handle `None` honestly.
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

    `description_without_vpn`, when non-empty, is what `description_for`
    returns instead of `description` while `network_via` isn't part of the
    install - an app whose normal description promises "only ever through
    your VPN" needs a second, honest sentence for the one deliberate
    exception that lets it run without one.

    `web_path` is appended to `app_url`'s host and port - every arr app
    keeps the default `"/"`, but Plex's own web UI lives at `/web`, and a
    bare `:32400/` answers an unauthenticated 401 instead of the player.
    `library_folders` names the shared `data/media/<m>` folders this app's
    library opens onto (`plan_folders` plans them even when nothing else in
    the install has a `media_folders` entry of its own) - unlike
    `media_folders`, it never makes this app a Prowlarr sync partner.

    `managed=False` (the owner's own existing Plex, connected but never
    deployed) is what every reader of "is this app actually mine to run"
    has to check before it does anything Docker-shaped: compose's service
    loop, the per-app config folder `plan_folders` plans, the name-clash
    check, and the deploy engine's bring-up and cancel each skip an
    unmanaged app instead of trying to build, start or stop a container
    that was never Marrquee's to make. It still gets an ordinary catalog
    id, rules and a Hub poster - only the "Marrquee runs this" half is
    switched off.

    `config_owner`, when set (Seerr's `(1000, 1000)`), is the fixed uid:gid
    `build_folders` chowns this app's created config folder to instead of
    the drive's own puid:pgid - for an app whose image always runs as one
    particular user regardless of what PUID the rest of the stack uses.
    `None` (every other app) keeps the ordinary puid:pgid behaviour.
    """

    id: str
    name: str
    description: str
    image: str
    port: int | None
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
    description_without_vpn: str = ""
    web_path: str = "/"
    library_folders: tuple[str, ...] = ()
    managed: bool = True
    config_owner: tuple[int, int] | None = None


RECYCLARR_APP_ID: Final = "recyclarr"
PLEX_APP_ID: Final = "plex"
JELLYFIN_APP_ID: Final = "jellyfin"
EXISTING_PLEX_APP_ID: Final = "existing-plex"
SEERR_APP_ID: Final = "seerr"

# The id every media-server door registers under - read by `media_server_of`
# and Stories 9/10's connect/Seerr flows, so a media-server-shaped app never
# has to be spelled out as a literal a second time.
MEDIA_SERVER_APP_IDS: Final = ("plex", "jellyfin", "existing-plex")

# Prowlarr, Sonarr, Radarr, Gluetun (the VPN tunnel), qBittorrent (the
# downloader), Recyclarr (quality settings from the TRaSH guides), Plex
# (a media server, linked to the owner's own Plex account), Jellyfin (a
# media server, signed in with the owner's own Marrquee login), the
# owner's own existing Plex (a media server Marrquee connects to but never
# deploys - `managed=False`) and Seerr (the owner's "ask for a movie or
# show" door, last in deploy order so a media server and an arr app are
# always already installed before it needs either one).
# Gluetun is `offered=False`: it never appears as its own "+" choice or
# wizard tick - whatever needs it (qBittorrent, today) adds it as a
# companion instead. qBittorrent's `network_via="gluetun"` is what enforces
# "the downloader must never run outside the tunnel": it has no network or
# API key of its own, and `build_stack_plan` refuses any install that has
# it without Gluetun. Plex, Jellyfin and the owner's existing Plex all keep
# an ordinary, unused Marrquee API key (like Recyclarr) - Plex authenticates
# only with the owner's own Plex account token, Jellyfin keeps its own admin
# API key in its own settings file (never Marrquee's key), and the existing
# Plex's key is never read at all. Each of the three excludes the other two
# (`AppRule("excludes_any", ...)`): only one media server per install. The
# existing Plex is also `offered=True` but `managed=False`: it is connected
# from the Hub's "+" panel, never ticked on the wizard's own app grid.
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
        description_without_vpn=QBITTORRENT_DESCRIPTION_NO_VPN,
    ),
    CatalogApp(
        id=RECYCLARR_APP_ID,
        name="Recyclarr",
        description=RECYCLARR_DESCRIPTION,
        image="ghcr.io/recyclarr/recyclarr:8.7.2",
        port=None,
        env_prefix="RECYCLARR",
        api_base="",
        media_folders=(),
        needs_data_mount=False,
        glyph="RC",
        order=5,
        default_ticked=False,
        web_page=False,
        rules=(
            AppRule(kind="needs_any", app_ids=("sonarr", "radarr"), reason=RECYCLARR_NEEDS_ARR),
        ),
        kind="sync",
        offered=True,
        login_kind="none",
    ),
    CatalogApp(
        id=PLEX_APP_ID,
        name="Plex",
        description=PLEX_DESCRIPTION,
        image="lscr.io/linuxserver/plex:latest",
        port=32400,
        env_prefix="PLEX",
        api_base="",
        media_folders=(),
        needs_data_mount=False,
        glyph="PX",
        order=6,
        default_ticked=False,
        web_page=True,
        rules=(
            AppRule(kind="excludes_any", app_ids=("jellyfin",), reason=PLEX_EXCLUDES_JELLYFIN),
            AppRule(
                kind="excludes_any",
                app_ids=("existing-plex",),
                reason=EXCLUDED_BY_EXISTING_PLEX,
            ),
        ),
        login_kind="none",
        kind="media_server",
        offered=True,
        web_path="/web",
        library_folders=("movies", "tv"),
    ),
    CatalogApp(
        id=JELLYFIN_APP_ID,
        name="Jellyfin",
        description=JELLYFIN_DESCRIPTION,
        image="lscr.io/linuxserver/jellyfin:latest",
        port=8096,
        env_prefix="JELLYFIN",
        api_base="",
        media_folders=(),
        needs_data_mount=False,
        glyph="JF",
        order=7,
        default_ticked=False,
        web_page=True,
        rules=(
            AppRule(kind="excludes_any", app_ids=("plex",), reason=JELLYFIN_EXCLUDES_PLEX),
            AppRule(
                kind="excludes_any",
                app_ids=("existing-plex",),
                reason=EXCLUDED_BY_EXISTING_PLEX,
            ),
        ),
        login_kind="jellyfin",
        kind="media_server",
        offered=True,
        library_folders=("movies", "tv"),
    ),
    CatalogApp(
        id=EXISTING_PLEX_APP_ID,
        name="Plex",
        description=EXISTING_PLEX_DESCRIPTION,
        image="",
        port=None,
        env_prefix="EXISTING_PLEX",
        api_base="",
        media_folders=(),
        needs_data_mount=False,
        glyph="PX",
        order=8,
        default_ticked=False,
        web_page=True,
        rules=(
            AppRule(kind="excludes_any", app_ids=("plex",), reason=EXISTING_PLEX_EXCLUDES_PLEX),
            AppRule(kind="excludes_any", app_ids=("jellyfin",), reason=PLEX_EXCLUDES_JELLYFIN),
        ),
        login_kind="none",
        kind="media_server",
        offered=True,
        web_path="/web",
        library_folders=("movies", "tv"),
        managed=False,
    ),
    CatalogApp(
        id=SEERR_APP_ID,
        name="Seerr",
        description=SEERR_DESCRIPTION,
        image="ghcr.io/seerr-team/seerr:v3.4.1",
        port=5055,
        env_prefix="SEERR",
        api_base="api/v1",
        media_folders=(),
        needs_data_mount=False,
        glyph="SR",
        order=9,
        default_ticked=False,
        web_page=True,
        rules=(
            AppRule(
                kind="needs_any",
                app_ids=MEDIA_SERVER_APP_IDS,
                reason=SEERR_NEEDS_PLEX_OR_JELLYFIN,
            ),
            AppRule(kind="needs_any", app_ids=("sonarr", "radarr"), reason=SEERR_NEEDS_ARR),
        ),
        login_kind="none",
        kind="requests",
        offered=True,
        web_path="/",
        library_folders=(),
        managed=True,
        config_owner=(1000, 1000),
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


def require_port(app: CatalogApp) -> int:
    """`app.port`, for every reader that only ever makes sense for a
    ported app (the compose builder's arr/vpn/downloader branches, the
    readiness probe, a wiring partner's base URL).

    Raises rather than silently faking a port (`0`, say) - a `kind="sync"`
    app like Recyclarr reaching one of these callers is a real bug, not
    something to paper over with a number that would leak into a poster,
    a deploy link or a "no address" line.
    """
    if app.port is None:
        raise ValueError(f"{app.id} has no port")
    return app.port


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


def app_host(app: CatalogApp, present: Iterable[str]) -> str:
    """The hostname another container reaches `app` at, on the `marrquee`
    network.

    An app with no `network_via`, or whose `network_via` isn't part of
    this install, has its own DNS name, `app.id`. An app that rides
    another one's network AND that other app is actually present
    (qBittorrent behind Gluetun) has none of its own - it answers only at
    that other app's hostname, since Docker's `network_mode: service:<x>`
    shares `<x>`'s whole network namespace. `present` is required, never
    defaulted - every caller must say which ids are actually installed, so
    a stale one-argument assumption ("the VPN is always there") can never
    creep back in unnoticed.
    """
    present_ids = set(present)
    if app.network_via is not None and app.network_via in present_ids:
        return app.network_via
    return app.id


def companions_for(app_id: str, present: Iterable[str], *, without_vpn: bool) -> tuple[str, ...]:
    """The extra app(s) adding `app_id` must bring along with it.

    Today this is at most one id: the app `app_id` rides the network of,
    when it isn't already present. Returns `()` for an app with no
    `network_via`, when its companion is already installed - adding
    qBittorrent a second time (after Gluetun already exists) brings nothing
    else with it - or, when `without_vpn` is True, when that companion is
    the VPN itself: the owner's break-glass confirmation is the one thing
    that turns "needs a VPN" into "runs on its own network instead".
    """
    app = get_app(app_id)
    if app.network_via is None:
        return ()
    if app.network_via in set(present):
        return ()
    if without_vpn and get_app(app.network_via).kind == "vpn":
        return ()
    return (app.network_via,)


def description_for(app: CatalogApp, present: Iterable[str]) -> str:
    """The one description every screen that names `app` shows.

    `app.description` almost always promises "only ever through your VPN"
    for a downloader - once the owner has broken the glass, that becomes a
    lie, so this reads `description_without_vpn` instead whenever the app's
    `network_via` isn't part of `present`. An app with no
    `description_without_vpn` set (everything but qBittorrent, today) is
    unaffected either way.
    """
    if (
        app.description_without_vpn
        and app.network_via is not None
        and app.network_via not in set(present)
    ):
        return app.description_without_vpn
    return app.description


def riders_of(app_id: str, present: Iterable[str]) -> tuple[CatalogApp, ...]:
    """Every present app that rides `app_id`'s network, in catalog order.

    Reads `present` rather than the whole catalog so a caller (the compose
    builder publishing a rider's port on `app_id`) only ever sees riders
    that are actually part of this install.
    """
    return tuple(app for app in apps_in_order(present) if app.network_via == app_id)


def media_server_of(app_ids: Iterable[str]) -> CatalogApp | None:
    """The one `kind="media_server"` app among `app_ids`, in catalog order,
    or `None` when it holds neither Plex nor Jellyfin.

    Callers (Stories 8b/9/10) never have to know there can be at most one -
    the "one media server per install" rule lives in the wizard/Hub refusal
    (`AppRule("excludes_any", ...)` on each media-server entry), not here.
    """
    for app in apps_in_order(app_ids):
        if app.kind == "media_server":
            return app
    return None
