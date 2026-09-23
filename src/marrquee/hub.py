"""The Hub page's view model: one pure function, `hub_view`.

The Hub reads from three places that never overlap - which apps were
actually deployed (`snapshot.apps`, at the call site), what Docker just
said about each one (`AppHealth`, from `health.read_health`), and the
address the browser used to reach Marrquee (`authority`, since a health
check is built with no request attached and can never know it). This
module is the one seam where those three become a single value the
template can draw, and the same value `GET /api/hub/status` (Chunk 5)
returns as JSON - so the page and the live check can never word a poster
differently.

Pure and FastAPI-free on purpose: every poster this story promises (up,
starting, down with a time, down without one, gone, not sure, no address)
is a value this function can return from plain inputs, which is what lets
the whole page be proven with no HTML and no Docker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from marrquee.addresses import app_url
from marrquee.catalog import CatalogApp, apps_in_order
from marrquee.health import AppHealth, HubState
from marrquee.words import (
    HUB_ALL_UP,
    HUB_CHIP_DOWN,
    HUB_CHIP_STARTING,
    HUB_CHIP_UNKNOWN,
    HUB_CHIP_UP,
    HUB_NOTHING_SET_UP,
    hub_line_down,
    hub_line_down_last_seen,
    hub_line_gone,
    hub_line_no_address,
    hub_line_starting,
    hub_line_unknown,
    hub_open_app_aria,
    hub_some_up,
    relative_time,
)

# The page renders this into `data-poll-ms`, so `hub.js` never has to carry
# the number itself - the 15-second cadence the owner approved lives in
# exactly one place.
HUB_POLL_MS: Final = 15000

_CHIP_BY_STATE: dict[HubState, str] = {
    "up": HUB_CHIP_UP,
    "starting": HUB_CHIP_STARTING,
    "down": HUB_CHIP_DOWN,
    "unknown": HUB_CHIP_UNKNOWN,
}

# States whose poster still gets a link: "up" obviously, and "unknown"
# because Docker not answering says nothing about whether the app itself
# would open fine - dropping its link would be worse than an honest guess.
_LINKED_STATES: frozenset[HubState] = frozenset({"up", "unknown"})


@dataclass(frozen=True)
class HubTile:
    """One poster, exactly as the page (and the live check) should draw it."""

    app_id: str
    glyph: str
    name: str
    description: str
    state: HubState
    chip: str
    line: str
    url: str | None
    aria: str | None


@dataclass(frozen=True)
class HubView:
    """Everything the Hub page draws, from one deploy's worth of apps."""

    tiles: tuple[HubTile, ...]
    announce: str
    any_down: bool
    docker_unreachable: bool
    proxied: bool
    empty: bool


def hub_view(
    app_ids: Sequence[str],
    healths: Sequence[AppHealth],
    *,
    authority: str | None,
    proxied: bool,
    now: datetime,
) -> HubView:
    healths_by_id = {health.app_id: health for health in healths}
    tiles = tuple(
        _tile(app, healths_by_id.get(app.id), authority=authority, now=now)
        for app in apps_in_order(app_ids)
    )
    return HubView(
        tiles=tiles,
        announce=_announce(tiles),
        any_down=any(tile.state == "down" for tile in tiles),
        docker_unreachable=bool(tiles) and all(tile.state == "unknown" for tile in tiles),
        proxied=proxied,
        empty=not tiles,
    )


def _tile(
    app: CatalogApp, health: AppHealth | None, *, authority: str | None, now: datetime
) -> HubTile:
    # No matching health reading (a deployed app Docker was never asked
    # about, or whose id it didn't recognise) is honestly "unknown", never
    # a silent "down".
    state: HubState = health.state if health is not None else "unknown"
    chip = _CHIP_BY_STATE[state]
    url = app_url(authority, app.port) if state in _LINKED_STATES else None
    aria = hub_open_app_aria(app.name, chip) if url is not None else None
    return HubTile(
        app_id=app.id,
        glyph=app.glyph,
        name=app.name,
        description=app.description,
        state=state,
        chip=chip,
        line=_line(app, state, url, health, now),
        url=url,
        aria=aria,
    )


def _line(
    app: CatalogApp, state: HubState, url: str | None, health: AppHealth | None, now: datetime
) -> str:
    if state == "up":
        # An Up poster with a working link says nothing more - the owner's
        # wireframe shows no extra line here (Content Direction row 10).
        return "" if url is not None else hub_line_no_address(app.name, app.port)
    if state == "starting":
        return hub_line_starting(app.name)
    if state == "unknown":
        return (
            hub_line_unknown(app.name)
            if url is not None
            else hub_line_no_address(app.name, app.port)
        )

    # state == "down"
    if health is None or not health.exists:
        return hub_line_gone(app.name)
    when = _last_seen(health.finished_at, now)
    if when is None:
        return hub_line_down(app.name)
    return hub_line_down_last_seen(app.name, when)


def _last_seen(finished_at: str | None, now: datetime) -> str | None:
    """`relative_time` of `finished_at`, or `None` when it can't be trusted.

    A missing time zone would raise `TypeError` the moment it's compared
    with `now`, and a future time (a NAS with a wrong clock) would read as
    "in 3 hours ago" - both count as "we don't actually know", the same
    honesty `health._state_of` already applies to Docker's own answer.
    """
    if finished_at is None:
        return None
    try:
        parsed = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    if parsed.tzinfo is None or not parsed < now:
        return None
    return relative_time((now - parsed).total_seconds())


def _announce(tiles: tuple[HubTile, ...]) -> str:
    if not tiles:
        return HUB_NOTHING_SET_UP
    up_count = sum(1 for tile in tiles if tile.state == "up")
    if up_count == len(tiles):
        return HUB_ALL_UP
    return hub_some_up(up_count, len(tiles))
