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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
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
    require_port,
    unavailable_reason,
)
from marrquee.deploy import AddState, AppAdd, WiringGap
from marrquee.docker_client import ContainerHealth
from marrquee.hardlinks import HardlinkResult
from marrquee.health import AppHealth, HubState, LinkHealth, LinkState
from marrquee.links import LinkCard, link_address, link_glyph
from marrquee.login import LoginRecord, LoginStatus, login_status, pending_app_ids, reset_reminder
from marrquee.plex import (
    ExistingPlex,
    FolderState,
    PlexServers,
    existing_plex_web_url,
    link_matches_plex,
)
from marrquee.questions import QuestionStep, question_steps_for
from marrquee.recyclarr import SyncRecord, SyncState, SyncStatus
from marrquee.storage import host_media_path
from marrquee.vpn import VPN_APP_ID, TunnelPlace, tunnel_place_line
from marrquee.words import (
    EXISTING_PLEX_LINE_DOWN,
    EXISTING_PLEX_LIST_FAILED,
    EXISTING_PLEX_NO_SERVERS,
    EXISTING_PLEX_NOTE_CANT_SEE,
    EXISTING_PLEX_SIGN_IN_AGAIN,
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
    HUB_DRIVE_NOTE_COPIES,
    HUB_DRIVE_NOTE_UNCHECKED,
    HUB_INSTALL_BUSY_LOGIN,
    HUB_INSTALL_LOGIN_FIRST,
    HUB_LINE_PAUSED_FOR_VPN,
    HUB_LINE_RESTARTING_WITHOUT_VPN,
    HUB_LINK_LINE_DOWN,
    HUB_LINKS_ALL_UP,
    HUB_NOTHING_SET_UP,
    RECYCLARR_CHIP_NEEDS_LOOK,
    RECYCLARR_LINE_COULDNT_START,
    RECYCLARR_LINE_NEVER,
    RECYCLARR_LINE_SYNCING,
    VPN_LINE_CONNECTING,
    VPN_LINE_NOT_SURE,
    VPN_LINE_TUNNEL_DOWN,
    WIRING_CHIP_RUNNING,
    app_line_error,
    existing_plex_description,
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
    recyclarr_line_app_down,
    recyclarr_line_failed,
    recyclarr_line_last_synced,
    recyclarr_line_late,
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

# How stale a good sync is allowed to look before the poster stops trusting
# it - the owner's own 48-hour call, not Recyclarr's cron interval itself.
# An injectable default rather than a bare literal in `hub_view`, so a test
# can shrink it instead of forging multi-day timestamps.
SYNC_LATE_AFTER: Final = timedelta(hours=48)

# `HubTile.actions`, shared with `HubTileOut` (routes/api.py) so the two
# never drift apart on which extra form a poster may show beside its link.
TileAction = Literal["none", "retry", "reconnect", "try_again", "retry_or_restore", "sync"]

# The only two apps a sync tile's "couldn't reach it" reason ever names -
# Recyclarr configures nothing else, so no other app's health is worth
# reading here.
_RECYCLARR_PARTNER_APP_IDS: Final = ("sonarr", "radarr")

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
    `sync_state` is Recyclarr's own poster state (`None` for every other
    app) - `actions="sync"` stays set through every one of its up states,
    syncing included, so the grid's own shape never flips; the CSS alone
    hides the button while a sync is running. `managed` is `False` only for
    the owner's own, already-running Plex (`kind="media_server"`,
    `CatalogApp.managed=False`): the one poster Marrquee never deployed and
    never asks Docker about, which is what tells the template to draw
    `data-managed="false"` and what keeps it out of `any_down` and
    `docker_unreachable` - a Down reading there is Plex's own `/identity`
    disagreeing, not Docker's "start it again from your NAS" story.
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
    actions: TileAction = "none"
    kind: AppKind = "arr"
    paused: bool = False
    can_change_seeding: bool = False
    can_change_vpn: bool = False
    sync_state: SyncState | None = None
    managed: bool = True


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

    `needs_sign_in` is true only while `steps` carries a `sign_in` field
    still unanswered - Plex's own row, before the owner has pressed Sign in
    with Plex. `sign_in_answers` holds ONLY that field's saved value(s),
    never any other step's answer (a VPN password among them): the row's
    own hidden multi-step form re-renders through the same shared partial
    every other row uses, and that partial must never be handed a secret it
    could echo into the page.
    """

    app: CatalogApp
    unavailable: str | None
    steps: tuple[QuestionStep, ...]
    without_vpn: bool = False
    needs_sign_in: bool = False
    sign_in_answers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExistingPlexView:
    """Everything the Manage pane needs about the owner's own Plex - never
    the token, which stays inside `plex.py`'s own `ExistingPlex` record and
    never reaches this view model (or `HubStatusOut`'s JSON) at all.
    """

    name: str
    folders: Mapping[str, FolderState]
    host_media_root: str


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

    `drive_note` is the Hub's own amber sentence about the drive check
    (`None` while it's quiet) - the one place that sentence is computed,
    read alike by the page and by `GET /api/hub/status`, so the two can
    never disagree about it.

    `existing_plex_view` is `None` unless the owner's own Plex is installed
    - the Manage pane's own data, built once here rather than re-read by
    the route, and deliberately never the `ExistingPlex` record itself
    (which carries the server's token).
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
    drive_note: str | None = None
    existing_plex_view: ExistingPlexView | None = None


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
    "closed",
    "choose",
    "install",
    "link",
    "edit",
    "login",
    "seeding",
    "vpn",
    "without-vpn",
    "plex-servers",
    "plex",
]


@dataclass(frozen=True)
class PlexServerOffer:
    """One of the owner's own Plex servers, exactly as the servers pane
    should draw it - the address and the token both stay inside `plex.py`'s
    own `PlexServerChoice`, never reaching this view model at all.

    `replace_link` is the one saved link card (if any) that already points
    at this same server, ticked by default so accepting the offer removes
    the stale card the moment the connection succeeds.
    """

    machine_id: str
    name: str
    online: bool
    replace_link: LinkCard | None


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
    refusal (a new card has no id yet). `plex_servers`/`plex_problem` are
    the `plex-servers` pane's own data - a plain sentence when there's
    nothing to offer (signed out, plex.tv unreachable, or an empty
    account), the list otherwise. The `plex` (Manage) pane reuses `error`
    for a busy or refused Disconnect, the same way the `link`/`edit` panes
    already do.
    """

    mode: PanelMode
    edit: LinkCard | None
    label: str
    url: str
    error: str | None
    plex_servers: tuple[PlexServerOffer, ...] = ()
    plex_problem: str | None = None


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


def _sign_in_status(
    steps: Sequence[QuestionStep], answers: Mapping[str, Mapping[str, str]]
) -> tuple[bool, dict[str, str]]:
    """Whether `steps` still has an unanswered `sign_in` field, and the map
    of just those fields' saved values - never any other saved answer, so a
    VPN password can never reach a row's re-rendered markup through this
    door.
    """
    needs_sign_in = False
    sign_in_answers: dict[str, str] = {}
    for step in steps:
        saved = answers.get(step.app_id, {})
        for question_field in step.fields:
            if question_field.kind != "sign_in":
                continue
            if question_field.name in saved:
                sign_in_answers[question_field.name] = saved[question_field.name]
            else:
                needs_sign_in = True
    return needs_sign_in, sign_in_answers


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
    drive: HardlinkResult | None = None,
    recyclarr: SyncStatus | None = None,
    sync_late_after: timedelta = SYNC_LATE_AFTER,
    answers: Mapping[str, Mapping[str, str]] = {},
    graphics_chip: bool = False,
    existing_plex: ExistingPlex | None = None,
    storage_root: str | None = None,
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
            healths_by_id=healths_by_id,
            sync=recyclarr,
            sync_late_after=sync_late_after,
            existing_plex=existing_plex,
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

    def _install_row(app: CatalogApp) -> InstallRow:
        steps = question_steps_for(
            (*companions_for(app.id, deployed_ids, without_vpn=without_vpn), app.id),
            present=deployed_ids,
            graphics_chip=graphics_chip,
        )
        needs_sign_in, sign_in_answers = _sign_in_status(steps, answers)
        return InstallRow(
            app=app,
            unavailable=unavailable_reason(app, deployed_ids),
            steps=steps,
            without_vpn=without_vpn and app.network_via is not None,
            needs_sign_in=needs_sign_in,
            sign_in_answers=sign_in_answers,
        )

    install_rows = tuple(_install_row(app) for app in installable if app.id != excluded_id)

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

    # Docker's own silence never speaks for an app Docker was never asked
    # about - the owner's own Plex reads Down or Up from `/identity` alone,
    # so it's excluded here the same way `_counts_toward_any_down` excludes
    # it from `any_down`.
    docker_asked_tiles = tuple(tile for tile in regular_tiles if tile.managed)

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
        docker_unreachable=bool(docker_asked_tiles)
        and all(tile.state == "unknown" for tile in docker_asked_tiles),
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
        drive_note=_drive_note(drive),
        existing_plex_view=_existing_plex_view(existing_plex, storage_root),
    )


def _existing_plex_view(
    existing_plex: ExistingPlex | None, storage_root: str | None
) -> ExistingPlexView | None:
    if existing_plex is None:
        return None
    host_media_root = (
        str(host_media_path(storage_root, "movies").parent) if storage_root is not None else ""
    )
    return ExistingPlexView(
        name=existing_plex.name, folders=existing_plex.folders, host_media_root=host_media_root
    )


def _drive_note(drive: HardlinkResult | None) -> str | None:
    """The Hub's own amber sentence for the latest saved drive check, or
    `None` while it should stay quiet - a drive that works, isn't needed
    yet, or has never been checked all say nothing on the Hub (Diagnostics
    always runs fresh, so a missing result is never the Hub's job to flag).
    """
    if drive is None:
        return None
    if drive.outcome == "copies":
        return HUB_DRIVE_NOTE_COPIES
    if drive.outcome == "couldnt_check":
        return HUB_DRIVE_NOTE_UNCHECKED
    return None


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

    An unmanaged tile (the owner's own Plex) never counts either: Marrquee
    never started it, so "start it again from your NAS" is never the right
    advice for it - its own Manage pane is where a Down reading gets
    explained.
    """
    if not tile.managed:
        return False
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
    healths_by_id: Mapping[str, AppHealth] | None = None,
    sync: SyncStatus | None = None,
    sync_late_after: timedelta = SYNC_LATE_AFTER,
    existing_plex: ExistingPlex | None = None,
) -> tuple[HubTile, TunnelState | None]:
    # Goes before every other dispatch: an unmanaged app (only the owner's
    # own Plex today) has no container for `_line`'s generic "up but no
    # link" branch to reason about - `require_port` would raise the moment
    # it tried, since `existing-plex` is `port=None` by design.
    if not app.managed:
        return _existing_plex_tile(app, health, existing_plex, authority=authority), None
    if app.kind == "vpn":
        return _vpn_tile(app, health, vpn_place, now, present)
    if app.kind == "sync":
        return (
            _sync_tile(
                app,
                health,
                sync,
                now,
                present,
                healths_by_id=healths_by_id or {},
                sync_late_after=sync_late_after,
            ),
            None,
        )

    # No matching health reading (a deployed app Docker was never asked
    # about, or whose id it didn't recognise) is honestly "unknown", never
    # a silent "down".
    state: HubState = health.state if health is not None else "unknown"
    chip = _CHIP_BY_STATE[state]
    # An app with no web page of its own never gets a link, whatever Docker
    # says about it.
    url = (
        app_url(authority, app.port, path=app.web_path)
        if state in _LINKED_STATES and app.web_page
        else None
    )
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


def _existing_plex_tile(
    app: CatalogApp,
    health: AppHealth | None,
    record: ExistingPlex | None,
    *,
    authority: str | None,
) -> HubTile:
    """The owner's own, already-running Plex.

    `health` comes from `health.read_existing_plex_health` (`/identity`),
    never Docker, so a missing reading here is honestly "down" rather than
    "unknown" - unlike every other app, there's no "Docker didn't answer"
    case for this poster at all. Its link is built from the record (never
    `app.port`, which is `None`) and stays live in every state, since a NAS
    that can't reach it right now says nothing about whether the owner's
    own browser still can.
    """
    state: HubState = health.state if health is not None else "down"
    chip = _CHIP_BY_STATE[state]
    url = existing_plex_web_url(record, authority) if record is not None else None
    aria = hub_open_app_aria(app.name, chip) if url is not None else None
    description = existing_plex_description(record.name) if record is not None else app.description
    cant_see = record is not None and any(
        folder_state == "not_seen" for folder_state in record.folders.values()
    )
    return HubTile(
        app_id=app.id,
        glyph=app.glyph,
        name=app.name,
        description=description,
        state=state,
        chip=chip,
        line="" if state == "up" else EXISTING_PLEX_LINE_DOWN,
        url=url,
        aria=aria,
        kind=app.kind,
        note=EXISTING_PLEX_NOTE_CANT_SEE if cant_see else "",
        actions="reconnect" if cant_see else "none",
        managed=False,
    )


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


def _sync_tile(
    app: CatalogApp,
    health: AppHealth | None,
    sync: SyncStatus | None,
    now: datetime,
    present: Iterable[str],
    *,
    healths_by_id: Mapping[str, AppHealth],
    sync_late_after: timedelta,
) -> HubTile:
    """Recyclarr's own poster: never linked (it has no web page of its own),
    and while its container is up, coloured by Marrquee's OWN sync attempts
    and Recyclarr's own per-run logs - never by anything Recyclarr's log
    itself says in English. A stopped container is an ordinary Down, the
    same arr-style mapping and lines every other app gets.
    """
    state: HubState = health.state if health is not None else "unknown"
    if state != "up":
        return HubTile(
            app_id=app.id,
            glyph=app.glyph,
            name=app.name,
            description=description_for(app, present),
            state=state,
            chip=_CHIP_BY_STATE[state],
            line=_line(app, state, None, health, now),
            url=None,
            aria=None,
            kind=app.kind,
        )

    sync_state, line, chip = _sync_up_details(sync, now, present, healths_by_id, sync_late_after)
    return HubTile(
        app_id=app.id,
        glyph=app.glyph,
        name=app.name,
        description=description_for(app, present),
        state="up",
        chip=chip,
        line=line,
        url=None,
        aria=None,
        kind=app.kind,
        # Every up state offers Sync now, syncing included - the CSS alone
        # hides the button while `sync_state == "syncing"`, so the grid's
        # own shape (and `hub.js`'s reload signature) never has to flip.
        actions="sync",
        sync_state=sync_state,
    )


def _sync_up_details(
    sync: SyncStatus | None,
    now: datetime,
    present: Iterable[str],
    healths_by_id: Mapping[str, AppHealth],
    sync_late_after: timedelta,
) -> tuple[SyncState, str, str]:
    if sync is not None and sync.syncing:
        return "syncing", RECYCLARR_LINE_SYNCING, HUB_CHIP_UP

    last = sync.last if sync is not None else None
    start_failed = sync is not None and sync.start_failed
    # `run_failed` is the broader signal (a bad exit code, a timeout, or a
    # clean exit whose own newest log still shows an error) - it is true
    # whenever `start_failed` is, so checking it alone still catches every
    # "Marrquee itself couldn't finish this sync" case, plus the ones a
    # real Recyclarr release's exit code doesn't prove on its own.
    run_failed = sync is not None and sync.run_failed

    if start_failed:
        return "failed", RECYCLARR_LINE_COULDNT_START, RECYCLARR_CHIP_NEEDS_LOOK
    if run_failed and (last is None or last.ok):
        # Either nothing has ever landed in the log Marrquee reads, or the
        # newest one there predates this failure and still says "fine" - in
        # both cases the freshest truth is "the attempt itself failed", not
        # whatever a stale or absent log would otherwise suggest.
        return "failed", RECYCLARR_LINE_COULDNT_START, RECYCLARR_CHIP_NEEDS_LOOK

    if last is None:
        return "never", RECYCLARR_LINE_NEVER, HUB_CHIP_UP

    age = _sync_age_seconds(last, now)
    if last.ok:
        if age < sync_late_after.total_seconds():
            return "ok", recyclarr_line_last_synced(relative_time(age)), HUB_CHIP_UP
        return "late", recyclarr_line_late(relative_time(age)), RECYCLARR_CHIP_NEEDS_LOOK

    return (
        "failed",
        _recyclarr_failure_reason(last, age, present, healths_by_id),
        RECYCLARR_CHIP_NEEDS_LOOK,
    )


def _sync_age_seconds(last: SyncRecord, now: datetime) -> float:
    # A future `finished_at` (a NAS with a bad clock) reads as "just now"
    # rather than a nonsensical negative age - the same honesty
    # `_last_seen` already applies to a down app's own last-seen time.
    return max(0.0, (now - last.finished_at).total_seconds())


def _recyclarr_failure_reason(
    last: SyncRecord, age: float, present: Iterable[str], healths_by_id: Mapping[str, AppHealth]
) -> str:
    ids = set(present)
    partners = (app for app in apps_in_order(ids) if app.id in _RECYCLARR_PARTNER_APP_IDS)
    for partner in partners:
        partner_health = healths_by_id.get(partner.id)
        if partner_health is not None and partner_health.state == "down":
            return recyclarr_line_app_down(partner.name)
    return recyclarr_line_failed(relative_time(age))


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
        # Every app with a web page also has a port - `require_port` turns
        # a future contract violation into a loud bug instead of a poster
        # silently naming a port that doesn't exist.
        return "" if url is not None else hub_line_no_address(app.name, require_port(app))
    if state == "starting":
        return hub_line_starting(app.name)
    if state == "unknown":
        # A `kind="sync"` app (Recyclarr) has no port to fall back to - an
        # unknown container state reads as plainly unknown for it, never
        # as "no address" naming a port that doesn't exist.
        return (
            hub_line_unknown(app.name)
            if url is not None or app.port is None
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
    if panel == "plex-servers":
        # The server list itself comes from plex.tv, which this function
        # never calls - `get_hub` fills `plex_servers`/`plex_problem` in
        # once it has fetched them, through `plex_servers_panel` below.
        return HubPanel(mode="plex-servers", edit=None, label="", url="", error=None)
    if panel == "plex":
        # Whether the owner's own Plex is actually connected
        # (`HubView.existing_plex_view`) is, likewise, a question this
        # function never sees the answer to - `get_hub` closes it back
        # down when there's no connected Plex to manage.
        return HubPanel(mode="plex", edit=None, label="", url="", error=None)
    if panel == "edit":
        card = next((link for link in links if link.id == link_id), None)
        if card is not None:
            return HubPanel(mode="edit", edit=card, label=card.label, url=card.url, error=None)
    return _CLOSED_PANEL


def plex_servers_panel(
    servers: PlexServers, links: Sequence[LinkCard], *, problem: str | None = None
) -> HubPanel:
    """The `plex-servers` pane's own panel, built from a `PlexServers`
    answer already in hand and the saved link cards - never touches
    plex.tv itself, so both a plain `GET /?panel=plex-servers` and a
    refused `POST /plex/connect` can build this from whatever they already
    fetched, instead of asking plex.tv a second time.

    `problem` overrides whatever `servers.state` would otherwise say - a
    posted `machine_id` plex.tv no longer recognises, or an address that
    didn't answer, are both refusals a route only discovers after this
    same list was already fetched.
    """
    if problem is not None:
        plex_problem = problem
    elif servers.state == "signed_out":
        plex_problem = EXISTING_PLEX_SIGN_IN_AGAIN
    elif servers.state == "unreachable":
        plex_problem = EXISTING_PLEX_LIST_FAILED
    elif not servers.servers:
        plex_problem = EXISTING_PLEX_NO_SERVERS
    else:
        plex_problem = None

    offers = tuple(
        PlexServerOffer(
            machine_id=choice.machine_id,
            name=choice.name,
            online=choice.online,
            replace_link=next((link for link in links if link_matches_plex(link, choice)), None),
        )
        for choice in servers.servers
    )
    return HubPanel(
        mode="plex-servers",
        edit=None,
        label="",
        url="",
        error=None,
        plex_servers=offers,
        plex_problem=plex_problem,
    )
