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

from marrquee.words import PROWLARR_DESCRIPTION, RADARR_DESCRIPTION, SONARR_DESCRIPTION


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


@dataclass(frozen=True)
class CatalogApp:
    """Everything Marrquee needs to know about one arr app before it exists."""

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


# Prowlarr, Sonarr and Radarr only - no qBittorrent, because the downloader
# must never run outside a VPN, and Marrquee has no VPN container yet.
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
