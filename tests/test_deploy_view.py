"""Tests for `deploy_screen.deploy_view` - the one pure function that turns
the owner's saved choices, the engine's current snapshot and the browser's
own address into everything the Deploy page draws.

No FastAPI, no clock, no Docker: every value here is built by hand, so a
test failure always points at the view model itself, never at a route, a
template or a background task.
"""

from __future__ import annotations

import dataclasses

from marrquee.deploy import AppProgress, DeployPhase, DeploySnapshot, Failure
from marrquee.deploy_screen import (
    DeployView,
    FailureView,
    LinkView,
    SummaryRow,
    TileView,
    WiringView,
    deploy_view,
)
from marrquee.state import InstallState
from marrquee.wiring import WiringStep, WiringStepState
from marrquee.words import (
    DOWNLOADS_LABEL,
    REFUSAL_NOTHING_CHOSEN,
    STATUS_CHIP_ERROR,
    app_line_error,
    bill_line,
    bill_one_app,
    open_app_label,
)

# --- Small builders - every test constructs its own snapshot by hand ---------


def _install_state(app_ids: tuple[str, ...], storage_root: str | None) -> InstallState:
    return InstallState(
        version=1,
        storage_root=storage_root,
        app_ids=app_ids,
        api_keys={app_id: f"fake-{app_id}-api-key" for app_id in app_ids},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-19T00:00:00+00:00",
    )


def _progress(
    app_id: str,
    name: str,
    state: str,
    *,
    chip: str = "chip",
    line: str = "line",
    note: str | None = None,
    port: int = 1234,
) -> AppProgress:
    return AppProgress(
        app_id=app_id,
        name=name,
        state=state,  # type: ignore[arg-type]
        chip=chip,
        line=line,
        note=note,
        port=port,
    )


def _snapshot(
    *,
    phase: DeployPhase = "running",
    apps: tuple[AppProgress, ...] = (),
    headline: str = "headline",
    detail: str | None = None,
    failure: Failure | None = None,
    wiring: tuple[WiringStep, ...] = (),
) -> DeploySnapshot:
    return DeploySnapshot(
        run_id="run-1",
        phase=phase,
        apps=apps,
        headline=headline,
        detail=detail,
        failure=failure,
        started_at="2026-09-23T00:00:00+00:00",
        finished_at=None,
        wiring=wiring,
    )


def _wiring_step(
    index: int,
    total: int,
    *,
    state: WiringStepState = "done",
    involved: tuple[str, ...] = (),
    line: str = "wiring line",
    chip: str = "wiring chip",
    note: str | None = None,
) -> WiringStep:
    return WiringStep(
        index=index,
        total=total,
        key=f"step-{index}",
        line=line,
        state=state,
        chip=chip,
        note=note,
        technical=None,
        involved=involved,
    )


# --- Tiles ---------------------------------------------------------------------


def test_ready_view_lists_apps_in_snapshot_order_with_catalog_glyphs() -> None:
    state = _install_state(("prowlarr", "sonarr", "radarr"), "/volume1/media")
    snapshot = _snapshot(
        phase="ready",
        apps=(
            _progress("sonarr", "Sonarr", "waiting"),
            _progress("prowlarr", "Prowlarr", "waiting"),
        ),
    )

    view = deploy_view(state, snapshot, authority=None)

    assert [tile.app_id for tile in view.tiles] == ["sonarr", "prowlarr"]
    assert [tile.order for tile in view.tiles] == [1, 2]
    assert view.tiles[0].glyph == "SN"
    assert view.tiles[1].glyph == "PR"
    assert view.tiles[0].description
    assert view.back_href == "/setup/drive?apps=prowlarr,sonarr,radarr"


def test_unknown_app_id_falls_back_to_a_derived_glyph_and_empty_description() -> None:
    state = _install_state(("mystery",), "/volume1/media")
    snapshot = _snapshot(phase="running", apps=(_progress("mystery", "Mystery App", "waiting"),))

    view = deploy_view(state, snapshot, authority=None)

    tile = view.tiles[0]
    assert tile.glyph == "MY"
    assert tile.description == ""


def test_a_mid_run_snapshot_renders_the_true_frame() -> None:
    state = _install_state(("prowlarr", "sonarr", "radarr"), "/volume1/media")
    snapshot = _snapshot(
        phase="running",
        headline="Starting Sonarr...",
        apps=(
            _progress("prowlarr", "Prowlarr", "done", chip="Ready", line="Prowlarr is ready"),
            _progress("sonarr", "Sonarr", "starting", chip="Starting…", line="Starting Sonarr"),
            _progress("radarr", "Radarr", "waiting", chip="Waiting", line="Waiting"),
        ),
    )

    view = deploy_view(state, snapshot, authority=None)

    assert [(tile.app_id, tile.state, tile.chip, tile.line) for tile in view.tiles] == [
        ("prowlarr", "done", "Ready", "Prowlarr is ready"),
        ("sonarr", "starting", "Starting…", "Starting Sonarr"),
        ("radarr", "waiting", "Waiting", "Waiting"),
    ]
    assert view.phase == "running"
    assert view.run_title == snapshot.headline
    assert view.announce == snapshot.headline


def test_a_failed_runs_stuck_tile_carries_error_chip_and_line() -> None:
    state = _install_state(("sonarr",), "/volume1/media")
    snapshot = _snapshot(
        phase="error",
        headline="Sonarr couldn't start",
        apps=(
            _progress(
                "sonarr", "Sonarr", "error", chip=STATUS_CHIP_ERROR, line=app_line_error("Sonarr")
            ),
        ),
        failure=Failure(
            code="never_became_ready",
            headline="Sonarr couldn't start",
            what_to_do="Press Try again.",
            technical="secret log output",
        ),
    )

    view = deploy_view(state, snapshot, authority=None)

    tile = view.tiles[0]
    assert tile.state == "error"
    assert tile.chip == STATUS_CHIP_ERROR
    assert tile.line == app_line_error("Sonarr")
    assert view.failure == FailureView(
        headline="Sonarr couldn't start", what_to_do="Press Try again."
    )


# --- Wiring, the gold outline and its count -------------------------------------


def test_posters_in_latest_steps_involved_are_linking_only_in_the_wiring_phase() -> None:
    state = _install_state(("prowlarr", "sonarr"), "/volume1/media")
    apps = (
        _progress("prowlarr", "Prowlarr", "done"),
        _progress("sonarr", "Sonarr", "done"),
    )
    wiring = (
        _wiring_step(1, 2, involved=("prowlarr",)),
        _wiring_step(2, 2, involved=("prowlarr", "sonarr")),
    )

    wiring_view = deploy_view(
        state, _snapshot(phase="wiring", apps=apps, wiring=wiring), authority=None
    )
    assert [tile.linking for tile in wiring_view.tiles] == [True, True]

    finale_view = deploy_view(
        state, _snapshot(phase="finale", apps=apps, wiring=wiring), authority=None
    )
    assert [tile.linking for tile in finale_view.tiles] == [False, False]


def test_empty_wiring_tuple_shows_the_phase_headline_and_no_count() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")
    snapshot = _snapshot(
        phase="wiring",
        headline="Connecting everything together.",
        apps=(_progress("prowlarr", "Prowlarr", "done"),),
        wiring=(),
    )

    view = deploy_view(state, snapshot, authority=None)

    assert view.wiring == WiringView(
        count_label="", line=snapshot.headline, state=None, chip="", note=None
    )


def test_wiring_view_is_none_outside_the_wiring_phase() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")

    assert deploy_view(state, _snapshot(phase="running"), authority=None).wiring is None
    assert deploy_view(state, _snapshot(phase="finale"), authority=None).wiring is None


# --- Summary rows: the ready screen's folder list -------------------------------


def test_a_prowlarr_only_install_shows_no_downloads_row() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")

    view = deploy_view(state, _snapshot(phase="ready"), authority=None)

    assert view.summary == ()


def test_summary_rows_are_host_paths_under_the_storage_root_never_data() -> None:
    state = _install_state(("sonarr", "radarr"), "/volume1/media")

    view = deploy_view(state, _snapshot(phase="ready"), authority=None)

    labels = [row.label for row in view.summary]
    assert labels[0] == DOWNLOADS_LABEL
    assert "TV shows" in labels
    assert "movies" in labels
    for row in view.summary:
        assert row.path.startswith("/volume1/media")
        assert not row.path.startswith("/data")
    downloads_row = view.summary[0]
    assert downloads_row.path == "/volume1/media/data/torrents"


def test_summary_is_empty_when_no_storage_root_was_ever_saved() -> None:
    state = _install_state(("sonarr",), None)

    view = deploy_view(state, _snapshot(phase="ready"), authority=None)

    assert view.summary == ()


# --- The bill line ---------------------------------------------------------------


def test_the_bill_line_reads_naturally_for_one_two_and_zero_apps() -> None:
    empty_state = _install_state((), "/volume1/media")
    zero = deploy_view(empty_state, _snapshot(phase="ready"), authority=None)
    assert zero.bill == REFUSAL_NOTHING_CHOSEN

    one = deploy_view(
        _install_state(("sonarr",), "/volume1/media"), _snapshot(phase="ready"), authority=None
    )
    assert one.bill == bill_one_app("Sonarr")

    two = deploy_view(
        _install_state(("prowlarr", "sonarr"), "/volume1/media"),
        _snapshot(phase="ready"),
        authority=None,
    )
    assert two.bill == bill_line(2, "Prowlarr")


# --- Finale links, built per device ------------------------------------------------


def test_links_are_built_from_the_browsers_address_with_each_apps_own_port() -> None:
    state = _install_state(("prowlarr", "sonarr"), "/volume1/media")
    snapshot = _snapshot(
        phase="finale",
        apps=(
            _progress("prowlarr", "Prowlarr", "done", port=9696),
            _progress("sonarr", "Sonarr", "done", port=8989),
        ),
    )

    view = deploy_view(state, snapshot, authority="192.168.1.50:7788")

    assert view.links == (
        LinkView(label=open_app_label("Prowlarr"), url="http://192.168.1.50:9696/"),
        LinkView(label=open_app_label("Sonarr"), url="http://192.168.1.50:8989/"),
    )


def test_an_unusable_address_produces_no_links_never_a_guessed_one() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")
    snapshot = _snapshot(
        phase="finale", apps=(_progress("prowlarr", "Prowlarr", "done", port=9696),)
    )

    assert deploy_view(state, snapshot, authority=None).links == ()
    # Unbracketed IPv6 - urlsplit alone would misparse this into a fake host.
    assert deploy_view(state, snapshot, authority="2001:db8::1").links == ()


def test_links_are_built_in_every_phase_not_only_the_finale() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")
    snapshot = _snapshot(
        phase="running", apps=(_progress("prowlarr", "Prowlarr", "waiting", port=9696),)
    )

    view = deploy_view(state, snapshot, authority="192.168.1.50:7788")

    assert view.links == (
        LinkView(label=open_app_label("Prowlarr"), url="http://192.168.1.50:9696/"),
    )


# --- No technical string can ever reach a view -------------------------------------


def test_no_view_dataclass_has_a_field_that_can_hold_a_technical_string() -> None:
    for cls in (TileView, SummaryRow, LinkView, WiringView, FailureView, DeployView):
        field_names = {field.name for field in dataclasses.fields(cls)}
        assert "technical" not in field_names


# --- The finale that carries a wiring note -----------------------------------------


def test_a_finale_with_a_wiring_problem_keeps_phase_finale_and_carries_the_note() -> None:
    state = _install_state(("prowlarr", "sonarr"), "/volume1/media")
    note = (
        "Your apps are all running. One connection didn't finish: Introducing Prowlarr to Sonarr."
    )
    snapshot = _snapshot(
        phase="finale",
        detail=note,
        apps=(_progress("prowlarr", "Prowlarr", "done"), _progress("sonarr", "Sonarr", "done")),
    )

    view = deploy_view(state, snapshot, authority=None)

    assert view.phase == "finale"
    assert view.finale_note == note


def test_finale_note_is_none_outside_the_finale_phase() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")
    snapshot = _snapshot(phase="running", detail="a detail that should never surface here")

    view = deploy_view(state, snapshot, authority=None)

    assert view.finale_note is None


# --- is_live ------------------------------------------------------------------------


def test_is_live_is_true_only_while_running_or_wiring() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")
    live_phases = {"running", "wiring"}

    for phase in ("ready", "running", "wiring", "finale", "error"):
        view = deploy_view(state, _snapshot(phase=phase), authority=None)
        assert view.is_live == (phase in live_phases)


def test_failure_is_none_when_the_snapshot_has_no_failure() -> None:
    state = _install_state(("prowlarr",), "/volume1/media")

    view = deploy_view(state, _snapshot(phase="ready", failure=None), authority=None)

    assert view.failure is None
