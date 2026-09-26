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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal

from marrquee.addresses import app_url
from marrquee.catalog import (
    CATALOG,
    AppKind,
    CatalogApp,
    apps_in_order,
    companions_for,
    description_for,
    get_app,
    unavailable_reason,
)
from marrquee.deploy import AddState, AppAdd, WiringGap
from marrquee.docker_client import ContainerHealth
from marrquee.health import AppHealth, HubState, LinkHealth, LinkState
from marrquee.links import LinkCard, link_address, link_glyph
from marrquee.login import LoginRecord, LoginStatus, login_status, pending_app_ids, reset_reminder
from marrquee.questions import QuestionStep, question_steps_for
from marrquee.vpn import VPN_APP_ID, TunnelPlace, tunnel_place_line
from marrquee.words import (
    HUB_ALL_UP,
    HUB_CHIP_ADD_FAILED,
    HUB_CHIP_ADDING,
    HUB_CHIP_DOWN,
    HUB_CHIP_PAUSED,
    HUB_CHIP_STARTING,
    HUB_CHIP_UNKNOWN,
    HUB_CHIP_UP,
    HUB_CHIP_VPN_CHANGE_FAILED,
    HUB_CHIP_VPN_CHANGING,
    HUB_INSTALL_BUSY_LOGIN,
    HUB_INSTALL_LOGIN_FIRST,
    HUB_LINE_PAUSED_FOR_VPN,
    HUB_LINE_RESTARTING_WITHOUT_VPN,
    HUB_LINK_LINE_DOWN,
    HUB_LINKS_ALL_UP,
    HUB_NOTHING_SET_UP,
    VPN_LINE_CONNECTING,
    VPN_LINE_NOT_SURE,
    VPN_LINE_TUNNEL_DOWN,
    WIRING_CHIP_RUNNING,
    app_line_error,
    hub_announce_add_failed,
    hub_announce_adding,
    hub_install_busy,
    hub_install_resolve_first,
    hub_line_connecting,
    hub_line_down,
    hub_line_down_last_seen,
    hub_line_gone,
    hub_line_no_address,
    hub_line_starting,
    hub_line_unknown,
    hub_links_some_down,
    hub_open_app_aria,
    hub_some_up,
    hub_wiring_gap_note,
    relative_time,
)

# The page renders this into `data-poll-ms`, so `hub.js` never has to carry
# the number itself - the 15-second cadence the owner approved lives in
# exactly one place.
HUB_POLL_MS: Final = 15000

# While an add or reconnect is running, `hub.js` polls this fast instead -
# the spotlight-to-green moment is worth watching live, not on a 15-second
# lag. Read from `data-add-poll-ms`, the same way `HUB_POLL_MS` is read from
# `data-poll-ms`.
HUB_ADD_POLL_MS: Final = 2000

# Where the Change pane's "Forgot your password?" link and the reset
# reminder both point - the README's own GUI-only steps for the NAS-side
# `MARRQUEE_RESET_LOGIN` line. Pinned as the repo's real remote so the
# anchor (GitHub's own slug for "## Forgot your apps' password?") stays
# correct without a second, hand-typed copy of it anywhere else.
LOGIN_HELP_URL: Final = "https://github.com/PurpleFox-07/marrquee#forgot-your-apps-password"

_CHIP_BY_STATE: dict[HubState, str] = {
    "up": HUB_CHIP_UP,
    "starting": HUB_CHIP_STARTING,
    "down": HUB_CHIP_DOWN,
    "unknown": HUB_CHIP_UNKNOWN,
}

_LINK_CHIP_BY_STATE: dict[LinkState, str] = {
    "up": HUB_CHIP_UP,
    "down": HUB_CHIP_DOWN,
}

# States whose poster still gets a link: "up" obviously, and "unknown"
# because Docker not answering says nothing about whether the app itself
# would open fine - dropping its link would be worse than an honest guess.
_LINKED_STATES: frozenset[HubState] = frozenset({"up", "unknown"})

# Gluetun's own signal, once its container is running: whether the tunnel
# itself is proven, still connecting, or has dropped - distinct from
# `HubState`, since a VPN's container can be "up" while its tunnel is
# anything but (Docker's own health check tells the two apart).
TunnelState = Literal["up", "connecting", "down", "unknown"]


@dataclass(frozen=True)
class HubTile:
    """One poster, exactly as the page (and the live check) should draw it.

    `add_state` is set only while this app is being added or reconnected -
    it drives the spotlight/linking CSS on the Hub without needing a second
    "is this the one being added" flag. `note` carries a wiring gap's own
    amber sentence, and `actions` says which extra form (if any) the poster
    needs beside its usual link: `retry` for a failed add, `reconnect` for
    an app whose wiring only partly finished. `kind` is copied straight
    from the catalog so the template (and `HubTileOut`) can style the one
    VPN poster differently without importing the catalog itself. `paused`
    is true only for a downloader whose own container is up but the tunnel
    it rides is down or connecting - a truthful "down" with its own wording,
    never an error. `can_change_seeding` offers the Hub's "Change seeding"
    link on an already-installed downloader's own tile. `try_again` is a
    failed run whose only recovery is pressing it again (a failed Change
    VPN, or a failed "restore" mover) - never paired with a Cancel button,
    unlike `retry`, which still offers one. `retry_or_restore` is a failed
    "Add your VPN" that moved an already-installed app: Cancel there means
    "Keep running without VPN", never "remove it" the way a plain `retry`
    tile's Cancel does. `can_change_vpn` offers the "Change VPN" link -
    Gluetun's own tile once it's installed and no VPN run is touching it,
    or a fresh "Add your VPN" that's already moved a rider and failed (the
    one place the owner can still edit the VPN answers before trying again).
    """

    app_id: str
    glyph: str
    name: str
    description: str
    state: HubState
    chip: str
    line: str
    url: str | None
    aria: str | None
    add_state: AddState | None = None
    note: str = ""
    actions: Literal["none", "retry", "reconnect", "try_again", "retry_or_restore"] = "none"
    kind: AppKind = "arr"
    paused: bool = False
    can_change_seeding: bool = False
    can_change_vpn: bool = False


@dataclass(frozen=True)
class LinkTile:
    """One link card, exactly as the page (and the live check) should draw it.

    `url` and `aria` are never `None` - unlike an app poster, a link card
    stays clickable even when it reads Down, because Marrquee checks from
    inside its own container and "down" here is only a hint.
    """

    link_id: str
    glyph: str
    label: str
    address: str
    url: str
    state: LinkState
    chip: str
    line: str
    aria: str


@dataclass(frozen=True)
class InstallRow:
    """One row in the "+" panel's install pane - one not-yet-installed app,
    whether it can be added right now, and whatever questions it asks
    before it can be.

    `without_vpn` is true only for qBittorrent's own row, once the owner
    has confirmed the break-glass phrase - the row then shows the
    "no VPN" description and an undo, instead of Gluetun's own question
    step (already dropped from `steps` by the same confirmation).
    """

    app: CatalogApp
    unavailable: str | None
    steps: tuple[QuestionStep, ...]
    without_vpn: bool = False


@dataclass(frozen=True)
class HubView:
    """Everything the Hub page draws, from one deploy's worth of apps.

    `vpn_tunnel` is `None` when no VPN is installed - the one signal Story 5
    needs before it can ever let qBittorrent run, read straight off the
    same tile the poster itself draws so the two can never disagree.

    `running_without_vpn` is what lights the amber badge - true only while
    some installed app's own `network_via` companion is genuinely missing
    (qBittorrent with no Gluetun), and never while a move is already under
    way (the badge would lie mid-move, when qBittorrent isn't running at
    all). `vpn_pane` is which VPN form the "vpn" panel pane should draw -
    `None` closes that pane no matter what `?panel=` a request carries.
    `without_vpn_offer` is whether the escape hatch is still reachable at
    all: only before qBittorrent (or the VPN) exists, and only once, since
    a saved confirmation already answered the question the escape hatch
    asks.
    """

    tiles: tuple[HubTile, ...]
    links: tuple[LinkTile, ...]
    installable: tuple[CatalogApp, ...]
    install_rows: tuple[InstallRow, ...]
    install_block: str | None
    busy: bool
    announce: str
    any_down: bool
    docker_unreachable: bool
    proxied: bool
    empty: bool
    login: LoginView | None = None
    vpn_tunnel: TunnelState | None = None
    running_without_vpn: bool = False
    vpn_pane: Literal["add", "change"] | None = None
    without_vpn_offer: bool = False


LoginBanner = Literal["none", "choose", "reset", "applying", "pending"]


@dataclass(frozen=True)
class LoginView:
    """What the Hub should draw about the one saved login: which banner (if
    any), the summary's username, which installed apps are still waiting
    for the current generation, the login run's own progress line, and
    whether the reset-line reminder belongs on the page.
    """

    status: LoginStatus
    banner: LoginBanner
    username: str | None
    pending_names: tuple[str, ...]
    line: str | None
    reset_reminder: bool


def login_view(
    record: LoginRecord,
    *,
    reset_value: str | None,
    installed: Sequence[str],
    running_line: str | None,
) -> LoginView:
    """Pure: everything the Hub needs to draw about the login, built from
    the saved record, `MARRQUEE_RESET_LOGIN`'s current value, which apps
    are installed, and the login run's own in-memory progress line.

    Banner precedence puts a running line first - while a login run is
    going, the page always says so, even mid-run when the record itself
    would otherwise still read as `pending` or `reset`. Then "nothing saved
    yet", then a still-outstanding reset, then any app still waiting for
    the current generation, and only once every one of those is settled
    does the banner disappear.
    """
    status = login_status(record, reset_value)
    pending_names = tuple(get_app(app_id).name for app_id in pending_app_ids(record, installed))

    banner: LoginBanner
    if running_line is not None:
        banner = "applying"
    elif status == "none":
        banner = "choose"
    elif status == "reset":
        banner = "reset"
    elif pending_names:
        banner = "pending"
    else:
        banner = "none"

    return LoginView(
        status=status,
        banner=banner,
        username=record.login.username if record.login is not None else None,
        pending_names=pending_names,
        line=running_line if banner == "applying" else None,
        reset_reminder=reset_reminder(record, reset_value),
    )


PanelMode = Literal[
    "closed", "choose", "install", "link", "edit", "login", "seeding", "vpn", "without-vpn"
]


@dataclass(frozen=True)
class HubPanel:
    """What the "+" tile's panel should draw - closed by default, so a
    plain `GET /` renders the same dialog markup either way, just without
    its `open` attribute.

    `label`/`url` are the boxes' current values: empty for `choose` and
    `install`, the stored card's own values for a fresh `edit`, and
    whatever the owner just typed (valid or not) after a refusal. `edit` is
    the card being edited - its `id` is what the edit and remove forms'
    `action` targets - and stays `None` everywhere else, including a `link`
    refusal (a new card has no id yet).
    """

    mode: PanelMode
    edit: LinkCard | None
    label: str
    url: str
    error: str | None


@dataclass(frozen=True)
class LoginFormState:
    """What the choose-login banner's form (or the panel's Change pane)
    should show back after a post - never the boxes it started from.

    `answers` holds ONLY `username` - a password is never worth echoing
    back, right or wrong, so there is no key for one here at all. A pass
    redirects instead of reaching this type; every `LoginFormState` a
    template ever sees is a refusal.
    """

    answers: Mapping[str, str]
    problem: str | None
    problem_field: str | None


@dataclass(frozen=True)
class SeedingFormState:
    """What the seeding pane's form should show - the saved answer on a
    plain open, or whatever was just posted (valid or not) after a refusal.

    Unlike `LoginFormState`, every field here is safe to echo back verbatim:
    a seeding answer never carries a secret.
    """

    answers: Mapping[str, str]
    problem: str | None
    problem_field: str | None


# The only VPN answer keys ever safe to show back on the Hub's own LAN page
# - never the username, a password or a key, even when re-rendering a
# refusal (the Hub has no login of its own, so anyone on the LAN who can
# reach it could otherwise read a saved VPN username straight off the
# page).
_VPN_SAFE_ANSWER_KEYS: Final = frozenset(
    {"provider", "vpn_type", "server_countries", "wireguard_addresses"}
)


def vpn_prefill(answers: Mapping[str, str]) -> dict[str, str]:
    """`answers`, kept to only the keys `_VPN_SAFE_ANSWER_KEYS` allows."""
    return {key: value for key, value in answers.items() if key in _VPN_SAFE_ANSWER_KEYS}


@dataclass(frozen=True)
class VpnFormState:
    """What the "Add your VPN"/"Change VPN" pane's form should show back -
    never the username, a password or a key, whether this is a plain open
    or a refusal's own re-render.
    """

    answers: Mapping[str, str]
    problem: str | None
    problem_field: str | None


@dataclass(frozen=True)
class WithoutVpnState:
    """Which break-glass step the "without-vpn" pane should draw, and that
    step's own refusal (stage 3's mismatched phrase), if any.
    """

    stage: Literal[1, 2, 3]
    problem: str | None


def hub_view(
    app_ids: Sequence[str],
    healths: Sequence[AppHealth],
    *,
    authority: str | None,
    proxied: bool,
    now: datetime,
    links: Sequence[LinkCard] = (),
    link_healths: Sequence[LinkHealth] = (),
    adding: AppAdd | None = None,
    wiring_gaps: Sequence[WiringGap] = (),
    busy: bool = False,
    login: LoginView | None = None,
    vpn_place: TunnelPlace | None = None,
    without_vpn: bool = False,
) -> HubView:
    healths_by_id = {health.app_id: health for health in healths}
    gaps_by_id = {gap.app_id: gap for gap in wiring_gaps}
    deployed_ids = set(app_ids)

    # A brand-new app being added has no place in `apps` yet, so it has no
    # health reading either - it's inserted into the grid by id, in the
    # same catalog order everything else already respects, and dropped
    # again from every count below that would otherwise call it "down" or
    # "unknown" for the honest reason that it doesn't exist yet.
    adding_new = adding is not None and adding.app_id not in deployed_ids
    tile_ids = (*app_ids, adding.app_id) if adding_new and adding is not None else tuple(app_ids)

    def _build_tile(app: CatalogApp) -> tuple[HubTile, TunnelState | None]:
        if adding_new and adding is not None and app.id == adding.app_id:
            return _adding_tile(app, adding, deployed_ids), None
        tile, tunnel = _tile(
            app,
            healths_by_id.get(app.id),
            authority=authority,
            now=now,
            vpn_place=vpn_place,
            present=deployed_ids,
        )
        return _apply_add_overlay(tile, app, adding, gaps_by_id.get(app.id)), tunnel

    built_tiles = tuple(_build_tile(app) for app in apps_in_order(tile_ids))
    tiles = tuple(tile for tile, _ in built_tiles)
    # At most one app is ever `kind="vpn"` today - the first (only) tunnel
    # reading found wins, so this stays correct without assuming that.
    vpn_tunnel = next((tunnel for _, tunnel in built_tiles if tunnel is not None), None)
    # A downloader's own container can be "up" while the tunnel it rides is
    # anything but - this can only be decided once every tile (the VPN's
    # included) has been built, so it's a second pass over the already-built
    # tiles rather than something `_tile` could ever know on its own.
    if vpn_tunnel in ("down", "connecting"):
        tiles = tuple(
            _paused_for_vpn(tile) if tile.kind == "downloader" and tile.state == "up" else tile
            for tile in tiles
        )
    regular_tiles = tuple(
        tile
        for tile in tiles
        if not (adding_new and adding is not None and tile.app_id == adding.app_id)
    )

    link_healths_by_id = {health.link_id: health for health in link_healths}
    link_tiles = tuple(_link_tile(card, link_healths_by_id.get(card.id)) for card in links)

    announce = _announce(regular_tiles, link_tiles)
    if adding_new and adding is not None:
        name = get_app(adding.app_id).name
        add_sentence = (
            hub_announce_add_failed(name) if adding.state == "error" else hub_announce_adding(name)
        )
        announce = f"{announce} {add_sentence}"

    installable = tuple(app for app in CATALOG if app.offered and app.id not in deployed_ids)
    excluded_id = adding.app_id if adding is not None else None
    install_rows = tuple(
        InstallRow(
            app=app,
            unavailable=unavailable_reason(app, deployed_ids),
            steps=question_steps_for(
                (*companions_for(app.id, deployed_ids, without_vpn=without_vpn), app.id)
            ),
            without_vpn=without_vpn and app.network_via is not None,
        )
        for app in installable
        if app.id != excluded_id
    )

    install_block: str | None = None
    if login is not None and login.status == "none":
        install_block = HUB_INSTALL_LOGIN_FIRST
    elif login is not None and busy and adding is None:
        install_block = HUB_INSTALL_BUSY_LOGIN
    elif adding is not None:
        name = get_app(adding.app_id).name
        if busy:
            install_block = hub_install_busy(name)
        elif adding.state == "error":
            install_block = hub_install_resolve_first(name)

    # A run's own mover(s) (paused, or restarting on their own network) and,
    # while a Change VPN is going, Gluetun's own tile never belong in
    # "something needs your attention" - the down-note's "start it again
    # from your NAS" advice is actively wrong for all three; the owner's own
    # Try again/Cancel on the tile itself is the only advice that fits.
    exempt_ids: set[str] = set()
    if adding is not None:
        if adding.purpose in ("add", "change_vpn", "restore"):
            exempt_ids.update(adding.moves)
        if adding.purpose == "change_vpn":
            exempt_ids.add(adding.app_id)

    running_without_vpn_ = running_without_vpn(app_ids, adding)
    has_qbittorrent_row = any(row.app.id == "qbittorrent" for row in install_rows)

    return HubView(
        tiles=tiles,
        links=link_tiles,
        installable=installable,
        install_rows=install_rows,
        install_block=install_block,
        busy=busy,
        announce=announce,
        any_down=any(
            _counts_toward_any_down(tile, healths_by_id.get(tile.app_id))
            for tile in regular_tiles
            if tile.app_id not in exempt_ids
        ),
        docker_unreachable=bool(regular_tiles)
        and all(tile.state == "unknown" for tile in regular_tiles),
        proxied=proxied,
        empty=not regular_tiles,
        login=login,
        vpn_tunnel=vpn_tunnel,
        running_without_vpn=running_without_vpn_,
        vpn_pane=_vpn_pane(deployed_ids, adding, running_without_vpn_),
        without_vpn_offer=(
            has_qbittorrent_row
            and "gluetun" not in deployed_ids
            and "qbittorrent" not in deployed_ids
            and adding is None
            and not without_vpn
        ),
    )


def running_without_vpn(app_ids: Iterable[str], adding: AppAdd | None) -> bool:
    """Whether some already-installed app's own `network_via` companion is
    genuinely missing - qBittorrent with no Gluetun, the one shape today's
    catalog can produce.

    Never true for an app currently being moved (`adding.moves`, while an
    "add" or "change_vpn" run is going) - the badge would otherwise lie the
    moment that app is stopped for the move, before it's back up behind (or,
    on a failed move, still without) the VPN.
    """
    ids = set(app_ids)
    moved = adding.moves if adding is not None and adding.purpose in ("add", "change_vpn") else ()
    return any(
        app.network_via is not None and app.network_via not in ids and app.id not in moved
        for app in apps_in_order(ids)
    )


def _vpn_pane(
    app_ids: Iterable[str], adding: AppAdd | None, running_without_vpn_: bool
) -> Literal["add", "change"] | None:
    ids = set(app_ids)
    if VPN_APP_ID in ids:
        return "change"
    add_with_moves_in_error = (
        adding is not None
        and adding.purpose == "add"
        and bool(adding.moves)
        and adding.state == "error"
    )
    if running_without_vpn_ or add_with_moves_in_error:
        return "add"
    return None


def _paused_for_vpn(tile: HubTile) -> HubTile:
    """The downloader's own tile while its container is up but the tunnel it
    rides is down or still connecting - never linked (Gluetun's own page is
    the only door, and it's not proven yet), and never an ordinary Down
    poster's "start it again from your NAS" advice, since the container
    itself never stopped.
    """
    return replace(
        tile,
        state="down",
        chip=HUB_CHIP_PAUSED,
        line=HUB_LINE_PAUSED_FOR_VPN,
        url=None,
        aria=None,
        paused=True,
    )


def _counts_toward_any_down(tile: HubTile, health: AppHealth | None) -> bool:
    """Whether a Down poster belongs in the "something needs attention"
    count `any_down` drives the Hub's own down-note from.

    A VPN tile reads Down the moment its tunnel drops, even while Gluetun's
    container is still running and already retrying on its own - the
    down-note's "start it again from your NAS" would be actively wrong
    advice there, so only a stopped VPN container (the one case that advice
    fits) counts. A paused downloader is the same idea one hop over: its
    container never stopped either, so it never belongs in this count.
    """
    if tile.paused:
        return False
    if tile.state != "down":
        return False
    if tile.kind == "vpn" and health is not None and health.state == "up":
        return False
    return True


def _adding_tile(app: CatalogApp, adding: AppAdd, present: Iterable[str]) -> HubTile:
    """The one tile for a brand-new app while it's being added - never a
    health-driven tile, since Docker has no opinion about it yet.
    """
    if adding.state == "starting":
        return HubTile(
            app_id=app.id,
            glyph=app.glyph,
            name=app.name,
            description=description_for(app, present),
            state="starting",
            chip=HUB_CHIP_ADDING,
            line=adding.note or adding.line,
            url=None,
            aria=None,
            add_state="starting",
            kind=app.kind,
        )
    if adding.state == "wiring":
        return HubTile(
            app_id=app.id,
            glyph=app.glyph,
            name=app.name,
            description=description_for(app, present),
            state="starting",
            chip=WIRING_CHIP_RUNNING,
            line=hub_line_connecting(app.name),
            url=None,
            aria=None,
            add_state="wiring",
            kind=app.kind,
        )

    # adding.state == "error": a failed add, waiting for "Try again" or
    # "Cancel". `adding.moves` (a failed "Add your VPN" that already
    # stopped a rider) widens both what the tile offers and what it lets
    # the owner reach: "retry_or_restore" pairs Try again with "Keep
    # running without VPN" instead of a plain remove, and `can_change_vpn`
    # reopens the VPN pane so the owner can fix a wrong answer before
    # trying again - the one place that's true for a brand-new VPN add,
    # since an already-installed Gluetun's own failed Change VPN never
    # offers it (Try again alone; see `_apply_add_overlay`).
    return HubTile(
        app_id=app.id,
        glyph=app.glyph,
        name=app.name,
        description=description_for(app, present),
        state="down",
        chip=HUB_CHIP_ADD_FAILED,
        line=_add_failure_line(app, adding),
        url=None,
        aria=None,
        add_state="error",
        actions="retry_or_restore" if adding.moves else "retry",
        kind=app.kind,
        can_change_vpn=bool(adding.moves),
    )


def _add_failure_line(app: CatalogApp, adding: AppAdd) -> str:
    """The line for an `AppAdd` in `state="error"`.

    `adding.line` is the generic `app_line_error` sentence unless Cancel
    itself just failed, in which case it already carries that failure's
    own, more specific sentence - the failure headline underneath it is
    stale in that case, so it's never shown twice.
    """
    failure = adding.failure
    if failure is not None and adding.line == app_line_error(app.name):
        return f"{failure.headline} {failure.what_to_do}"
    return adding.line


def _apply_add_overlay(
    tile: HubTile, app: CatalogApp, adding: AppAdd | None, gap: WiringGap | None
) -> HubTile:
    """Layer a reconnect (in flight or failed), a Change VPN, a mover being
    moved or restored, and/or a lingering wiring gap onto an
    already-installed app's normal, health-driven tile.
    """
    if gap is not None:
        tile = replace(
            tile, note=hub_wiring_gap_note(app.name, gap.failed_lines), actions="reconnect"
        )

    if adding is not None and app.id in adding.moves:
        if adding.purpose in ("add", "change_vpn"):
            # Paused whatever Docker says about it - a mover is being
            # stopped, recreated and rewired behind (or, on a failure, back
            # off) the VPN, so its own container state is never the truth
            # to draw right now.
            tile = _paused_for_vpn(tile)
        elif adding.purpose == "restore":
            tile = _restore_mover_tile(tile, app, adding)

    if tile.kind == "vpn":
        # "No gluetun run in progress" - reachable to the owner exactly
        # when nothing about the VPN is already in flight or already
        # failed (a failed Change VPN offers only Try again; see below).
        gluetun_run_active = adding is not None and adding.app_id == app.id
        tile = replace(tile, can_change_vpn=not gluetun_run_active)

    if adding is None or adding.app_id != app.id:
        return tile

    if adding.purpose == "reconnect":
        if adding.state == "error":
            # Never "retry" here: that action pairs with a Cancel button,
            # and Cancel must never remove an already-installed, working
            # app's own container - "Connect again" is this tile's only
            # way back.
            return replace(
                tile,
                add_state="error",
                line=_add_failure_line(app, adding),
                actions="reconnect",
            )
        return replace(tile, add_state=adding.state, line=hub_line_connecting(app.name))

    if adding.purpose == "change_vpn":
        if adding.state == "error":
            return replace(
                tile,
                state="down",
                chip=HUB_CHIP_VPN_CHANGE_FAILED,
                line=_add_failure_line(app, adding),
                url=None,
                aria=None,
                actions="try_again",
            )
        return replace(
            tile,
            state="starting",
            chip=HUB_CHIP_VPN_CHANGING,
            line=adding.note or adding.line,
            add_state="starting",
        )

    return tile


def _restore_mover_tile(tile: HubTile, app: CatalogApp, adding: AppAdd) -> HubTile:
    """A mover being brought back onto its own network after "Keep running
    without VPN" - never linked while it's still starting (wiring isn't
    done yet, even once the container itself answers), and a plain failed
    line with only Try again on a failure (Cancel has nothing left to undo:
    the VPN is already gone).
    """
    if adding.state == "error":
        return replace(
            tile,
            state="down",
            chip=HUB_CHIP_ADD_FAILED,
            line=_add_failure_line(app, adding),
            url=None,
            aria=None,
            actions="try_again",
        )
    return replace(
        tile,
        state="starting",
        chip=HUB_CHIP_STARTING,
        line=HUB_LINE_RESTARTING_WITHOUT_VPN,
        url=None,
        aria=None,
    )


def _link_tile(card: LinkCard, health: LinkHealth | None) -> LinkTile:
    # A link Marrquee hasn't checked yet (no matching health reading) is
    # honestly "down", never a silent "up" - the first render always probes
    # before drawing a card, so this only ever fires when it genuinely
    # hasn't been checked.
    state: LinkState = health.state if health is not None else "down"
    chip = _LINK_CHIP_BY_STATE[state]
    return LinkTile(
        link_id=card.id,
        glyph=link_glyph(card.label),
        label=card.label,
        address=link_address(card.url),
        url=card.url,
        state=state,
        chip=chip,
        line="" if state == "up" else HUB_LINK_LINE_DOWN,
        aria=hub_open_app_aria(card.label, chip),
    )


def _tile(
    app: CatalogApp,
    health: AppHealth | None,
    *,
    authority: str | None,
    now: datetime,
    vpn_place: TunnelPlace | None = None,
    present: Iterable[str] = (),
) -> tuple[HubTile, TunnelState | None]:
    if app.kind == "vpn":
        return _vpn_tile(app, health, vpn_place, now, present)

    # No matching health reading (a deployed app Docker was never asked
    # about, or whose id it didn't recognise) is honestly "unknown", never
    # a silent "down".
    state: HubState = health.state if health is not None else "unknown"
    chip = _CHIP_BY_STATE[state]
    # An app with no web page of its own never gets a link, whatever Docker
    # says about it.
    url = app_url(authority, app.port) if state in _LINKED_STATES and app.web_page else None
    aria = hub_open_app_aria(app.name, chip) if url is not None else None
    tile = HubTile(
        app_id=app.id,
        glyph=app.glyph,
        name=app.name,
        description=description_for(app, present),
        state=state,
        chip=chip,
        line=_line(app, state, url, health, now),
        url=url,
        aria=aria,
        kind=app.kind,
        # Every already-installed downloader gets the "Change seeding" link -
        # a brand-new one being added never reaches this function at all (it
        # draws from `_adding_tile` instead, which never sets this), so
        # nothing else has to name that exception here.
        can_change_seeding=app.kind == "downloader",
    )
    return tile, None


def _vpn_tile(
    app: CatalogApp,
    health: AppHealth | None,
    vpn_place: TunnelPlace | None,
    now: datetime,
    present: Iterable[str] = (),
) -> tuple[HubTile, TunnelState]:
    """The VPN poster: never linked (Gluetun has no web page of its own),
    and coloured by Gluetun's OWN health check once its container is
    running - Docker's "running" alone says nothing about whether the
    tunnel itself is up, only its own `State.Health.Status` does.

    While the container isn't running at all, the ordinary arr-style
    mapping and lines apply unchanged (a stopped VPN is an ordinary Down,
    same as any other app) - only a running-but-unproven container gets the
    tunnel-specific wording below.
    """
    state: HubState = health.state if health is not None else "unknown"
    if health is not None and state == "up":
        hub_state, line, tunnel = _vpn_running_tile(health.health, vpn_place)
    else:
        hub_state = state
        line = _line(app, state, None, health, now)
        tunnel = "unknown" if state == "unknown" else "down"

    tile = HubTile(
        app_id=app.id,
        glyph=app.glyph,
        name=app.name,
        description=description_for(app, present),
        state=hub_state,
        chip=_CHIP_BY_STATE[hub_state],
        line=line,
        url=None,
        aria=None,
        kind=app.kind,
    )
    return tile, tunnel


def _vpn_running_tile(
    docker_health: ContainerHealth | None, vpn_place: TunnelPlace | None
) -> tuple[HubState, str, TunnelState]:
    if docker_health == "healthy":
        return "up", tunnel_place_line(vpn_place), "up"
    if docker_health == "starting":
        return "starting", VPN_LINE_CONNECTING, "connecting"
    if docker_health == "unhealthy":
        return "down", VPN_LINE_TUNNEL_DOWN, "down"
    # No HEALTHCHECK data at all (a changed image) is honestly "unknown",
    # never a silent "up" the container's own running state can't back up.
    return "unknown", VPN_LINE_NOT_SURE, "unknown"


def _line(
    app: CatalogApp, state: HubState, url: str | None, health: AppHealth | None, now: datetime
) -> str:
    if state == "up":
        # An Up poster with a working link says nothing more - the owner's
        # wireframe shows no extra line here (Content Direction row 10).
        # An app with no web page at all is equally silent when it's up -
        # there being no link is normal for it, not a missing address.
        if not app.web_page:
            return ""
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


def _announce(tiles: tuple[HubTile, ...], link_tiles: tuple[LinkTile, ...]) -> str:
    if not tiles:
        apps_sentence = HUB_NOTHING_SET_UP
    else:
        up_count = sum(1 for tile in tiles if tile.state == "up")
        apps_sentence = HUB_ALL_UP if up_count == len(tiles) else hub_some_up(up_count, len(tiles))

    if not link_tiles:
        return apps_sentence

    down_count = sum(1 for tile in link_tiles if tile.state == "down")
    links_sentence = (
        HUB_LINKS_ALL_UP if down_count == 0 else hub_links_some_down(down_count, len(link_tiles))
    )
    return f"{apps_sentence} {links_sentence}"


_CLOSED_PANEL: Final = HubPanel(mode="closed", edit=None, label="", url="", error=None)


def hub_panel(panel: str | None, link_id: str | None, links: Sequence[LinkCard]) -> HubPanel:
    """The panel `GET /?panel=...&link=...` should draw.

    Anything this doesn't recognise - a missing `panel`, a typo'd value, an
    `edit` whose `link` id matches no saved card - is honestly closed,
    never a guess. `edit` is the one mode that reads `links`: it prefills
    the *stored* label and URL, so what the owner sees, what gets saved on
    a plain re-submit and what the card's own `href` uses are one value.
    """
    if panel == "choose":
        return HubPanel(mode="choose", edit=None, label="", url="", error=None)
    if panel == "install":
        return HubPanel(mode="install", edit=None, label="", url="", error=None)
    if panel == "link":
        return HubPanel(mode="link", edit=None, label="", url="", error=None)
    if panel == "login":
        return HubPanel(mode="login", edit=None, label="", url="", error=None)
    if panel == "seeding":
        # Whether qBittorrent is actually installed is a question about the
        # deploy snapshot, which this function never sees (its whole job is
        # reading the query string against the saved links) - the caller
        # (`get_hub`) closes this back down when it isn't.
        return HubPanel(mode="seeding", edit=None, label="", url="", error=None)
    if panel == "vpn":
        # Whether "Add your VPN"/"Change VPN" is actually reachable right
        # now (`HubView.vpn_pane`) is, likewise, a question this function
        # never sees the answer to - `get_hub` closes it back down when
        # `vpn_pane is None`.
        return HubPanel(mode="vpn", edit=None, label="", url="", error=None)
    if panel == "without-vpn":
        return HubPanel(mode="without-vpn", edit=None, label="", url="", error=None)
    if panel == "edit":
        card = next((link for link in links if link.id == link_id), None)
        if card is not None:
            return HubPanel(mode="edit", edit=card, label=card.label, url=card.url, error=None)
    return _CLOSED_PANEL
