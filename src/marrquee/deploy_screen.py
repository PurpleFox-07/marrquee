"""The Deploy page's view model: one pure function, `deploy_view`.

`GET /deploy` has exactly one job: draw the true current state of the
world. That state lives in three places - the owner's saved choices
(`InstallState`), the engine's live progress (`DeploySnapshot`), and the
address the browser used to reach Marrquee (a plain string, since a
snapshot is built with no request attached and can never know it). This
module is the one seam where all three become a single value the template
can draw without ever asking "which phase is this" itself - it only ever
asks "is this field set".

Pure, total and FastAPI-free on purpose: every one of the six frames this
screen can show (ready, running, wiring, finale, finale with a wiring
note, error) is a value this function can return from plain inputs, which
is what lets the whole screen be proven before a single line of HTML
exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from marrquee.addresses import app_url
from marrquee.catalog import CATALOG, apps_in_order
from marrquee.deploy import AppProgress, AppState, DeployPhase, DeploySnapshot, Failure
from marrquee.state import InstallState
from marrquee.storage import host_media_path, plan_folders
from marrquee.wiring import WiringStepState
from marrquee.words import (
    DOWNLOADS_LABEL,
    MEDIA_FOLDER_LABEL,
    REFUSAL_NOTHING_CHOSEN,
    bill_line,
    bill_one_app,
    open_app_label,
    wiring_step_label,
)

# Looked up once, at import time - the catalog is a fixed tuple, so this
# never goes stale for the life of the process.
_CATALOG_BY_ID = {app.id: app for app in CATALOG}


@dataclass(frozen=True)
class TileView:
    """One poster, exactly as the page should draw it."""

    app_id: str
    order: int
    glyph: str
    name: str
    description: str
    state: AppState
    chip: str
    line: str
    note: str | None
    linking: bool


@dataclass(frozen=True)
class SummaryRow:
    """One row of the ready ticket's folder list."""

    label: str
    path: str


@dataclass(frozen=True)
class LinkView:
    """One "Open {name}" link on the finale, already built for this device."""

    label: str
    url: str


@dataclass(frozen=True)
class WiringView:
    """The wiring block: the latest step, or the phase's own headline when
    nothing has been reported yet.
    """

    count_label: str
    line: str
    state: WiringStepState | None
    chip: str
    note: str | None


@dataclass(frozen=True)
class FailureView:
    """Only what the owner sees - never `Failure.technical`."""

    headline: str
    what_to_do: str


@dataclass(frozen=True)
class DeployView:
    """Everything the Deploy page draws, for whichever frame is current."""

    phase: DeployPhase
    tiles: tuple[TileView, ...]
    bill: str
    summary: tuple[SummaryRow, ...]
    back_href: str
    run_title: str
    announce: str
    wiring: WiringView | None
    finale_note: str | None
    links: tuple[LinkView, ...]
    failure: FailureView | None
    is_live: bool


def deploy_view(
    state: InstallState, snapshot: DeploySnapshot, *, authority: str | None
) -> DeployView:
    return DeployView(
        phase=snapshot.phase,
        tiles=_build_tiles(snapshot),
        bill=_bill(state.app_ids),
        summary=_summary(state.storage_root, state.app_ids),
        back_href=f"/setup/drive?apps={','.join(state.app_ids)}",
        run_title=snapshot.headline,
        announce=snapshot.headline,
        wiring=_wiring_view(snapshot),
        finale_note=snapshot.detail if snapshot.phase == "finale" else None,
        links=_links(snapshot.apps, authority),
        failure=_failure_view(snapshot.failure),
        is_live=snapshot.phase in ("running", "wiring"),
    )


def _build_tiles(snapshot: DeploySnapshot) -> tuple[TileView, ...]:
    linking_ids = _linking_app_ids(snapshot)
    tiles = []
    for order, app in enumerate(snapshot.apps, start=1):
        catalog_app = _CATALOG_BY_ID.get(app.app_id)
        # An app id missing from the catalog can't happen through the
        # wizard, but a total function still has to answer something
        # sensible rather than raise - two letters from the name and no
        # description read as "unknown" without ever crashing the page.
        glyph = catalog_app.glyph if catalog_app is not None else app.name[:2].upper()
        description = catalog_app.description if catalog_app is not None else ""
        tiles.append(
            TileView(
                app_id=app.app_id,
                order=order,
                glyph=glyph,
                name=app.name,
                description=description,
                state=app.state,
                chip=app.chip,
                line=app.line,
                note=app.note,
                linking=app.app_id in linking_ids,
            )
        )
    return tuple(tiles)


def _linking_app_ids(snapshot: DeploySnapshot) -> frozenset[str]:
    if snapshot.phase != "wiring" or not snapshot.wiring:
        return frozenset()
    return frozenset(snapshot.wiring[-1].involved)


def _bill(app_ids: tuple[str, ...]) -> str:
    apps = apps_in_order(app_ids)
    if not apps:
        return REFUSAL_NOTHING_CHOSEN
    if len(apps) == 1:
        return bill_one_app(apps[0].name)
    return bill_line(len(apps), apps[0].name)


def _summary(storage_root: str | None, app_ids: tuple[str, ...]) -> tuple[SummaryRow, ...]:
    if storage_root is None:
        return ()

    root = PurePosixPath(storage_root)
    folders = plan_folders(app_ids)
    rows: list[SummaryRow] = []

    # Every app that downloads shares one torrents folder, so only the
    # first data/torrents/... entry is needed to name it - a second entry
    # for a second app would point at the very same parent folder.
    torrents_entry = next(
        (entry for entry in folders if entry.parts[:2] == ("data", "torrents")), None
    )
    if torrents_entry is not None:
        rows.append(SummaryRow(label=DOWNLOADS_LABEL, path=str(root / torrents_entry.parent)))

    for entry in folders:
        if entry.parts[:2] != ("data", "media"):
            continue
        media = entry.parts[2]
        rows.append(
            SummaryRow(
                label=MEDIA_FOLDER_LABEL.get(media, media),
                path=str(host_media_path(storage_root, media)),
            )
        )

    return tuple(rows)


def _wiring_view(snapshot: DeploySnapshot) -> WiringView | None:
    if snapshot.phase != "wiring":
        return None
    if not snapshot.wiring:
        return WiringView(count_label="", line=snapshot.headline, state=None, chip="", note=None)
    step = snapshot.wiring[-1]
    return WiringView(
        count_label=wiring_step_label(step.index, step.total),
        line=step.line,
        state=step.state,
        chip=step.chip,
        note=step.note,
    )


def _failure_view(failure: Failure | None) -> FailureView | None:
    if failure is None:
        return None
    return FailureView(headline=failure.headline, what_to_do=failure.what_to_do)


def _links(apps: tuple[AppProgress, ...], authority: str | None) -> tuple[LinkView, ...]:
    """Built in every phase, from each app's own port - the template
    reveals these only at the finale, but drawing them once here means the
    script never has to build a link (or an innerHTML) itself.
    """
    links = []
    for app in apps:
        url = app_url(authority, app.port)
        if url is not None:
            links.append(LinkView(label=open_app_label(app.name), url=url))
    return tuple(links)
