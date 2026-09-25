"""Tests for the Deploy screen's server-rendered shell: `GET /deploy` and
`POST /deploy`, plus the ready and running frames and the poster grid.

Every fixture writes the real files `DeployManager` and `load_state` read
back (`install.json`, `deploy.json`), through the same `write_json_atomic`
helper the engine itself uses - the same mechanism a NAS restart mid-deploy
relies on - so nothing here needs a stub manager. HTML structure (which
poster carries which state) is read with the stdlib `html.parser`, the same
approach `test_wizard_apps.py` uses; plain wording and ordering checks are
substring/index checks instead.
"""

from __future__ import annotations

import dataclasses
import re
import time
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

import marrquee.questions as questions_module
from marrquee import words
from marrquee.catalog import CATALOG, get_app
from marrquee.config import Settings
from marrquee.deploy import (
    AppProgress,
    AppState,
    DeployManager,
    DeploySnapshot,
    Failure,
    FakeReadinessProbe,
)
from marrquee.docker_client import ComposeResult, DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.questions import QuestionCheck, QuestionStep
from marrquee.state import STATE_VERSION, InstallState, save_state, write_json_atomic
from marrquee.wiring import WiringStep
from marrquee.words import (
    STATUS_CHIP_DONE,
    STATUS_CHIP_ERROR,
    STATUS_CHIP_STARTING,
    STATUS_CHIP_WAITING,
    app_line_error,
)

# --- Shared fixtures and small builders --------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _fresh_root(settings: Settings) -> PurePosixPath:
    """Create an empty, never-deployed-to target and return its host path."""
    (settings.host_mount / "volume1" / "media").mkdir(parents=True)
    return PurePosixPath("/volume1/media")


def _install_state(
    app_ids: tuple[str, ...], *, storage_root: str | None = "/volume1/media"
) -> InstallState:
    return InstallState(
        version=STATE_VERSION,
        storage_root=storage_root,
        app_ids=app_ids,
        api_keys={app_id: f"fake-{app_id}-api-key" for app_id in app_ids},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-23T00:00:00+00:00",
    )


def _client(settings: Settings, status: DockerStatus | None = None) -> TestClient:
    if status is None:
        status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _progress(
    app_id: str,
    name: str,
    state: AppState,
    *,
    chip: str,
    line: str,
    note: str | None = None,
    port: int,
) -> AppProgress:
    return AppProgress(
        app_id=app_id, name=name, state=state, chip=chip, line=line, note=note, port=port
    )


def _write_snapshot(settings: Settings, snapshot: DeploySnapshot) -> None:
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


class _PosterCollector(HTMLParser):
    """Collects every poster `<li data-app="...">`'s own attributes, keyed
    by app id - enough to check which state and which chip each tile
    carries without depending on where in the page it happens to sit.
    """

    def __init__(self) -> None:
        super().__init__()
        self.posters: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        app_id = attrs_dict.get("data-app")
        if tag == "li" and app_id is not None:
            self.posters[app_id] = attrs_dict


def _posters(page_html: str) -> dict[str, dict[str, str | None]]:
    collector = _PosterCollector()
    collector.feed(page_html)
    return collector.posters


class _RootCollector(HTMLParser):
    """Collects the page's own root `<main data-deploy ...>` attributes -
    used instead of a whole-page substring search, because a poster tile
    carries its own `data-has-note` too and a bare substring check can't
    tell the two apart.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attrs: dict[str, str | None] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "main" and "data-deploy" in attrs_dict and not self.attrs:
            self.attrs = attrs_dict


def _root(page_html: str) -> dict[str, str | None]:
    collector = _RootCollector()
    collector.feed(page_html)
    return collector.attrs


# --- GET /deploy: nothing saved redirects to the wizard ----------------------


def test_opening_deploy_with_nothing_saved_sends_the_owner_to_the_app_picker(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/deploy", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


# --- GET /deploy: the ready frame --------------------------------------------


def test_the_ready_page_names_every_chosen_app_and_every_folder_that_will_be_built(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr", "radarr")))
    client = _client(settings)

    response = client.get("/deploy")

    assert response.status_code == 200
    assert "Sonarr" in response.text
    assert "Radarr" in response.text
    assert words.DOWNLOADS_LABEL in response.text
    assert "TV shows" in response.text
    assert "movies" in response.text
    assert "/volume1/media/data/torrents" in response.text
    assert "/volume1/media/data/media/tv" in response.text
    assert "/volume1/media/data/media/movies" in response.text


def test_the_ready_page_shows_the_step_pills_and_a_back_link_the_running_page_shows_neither(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))

    ready = _client(settings).get("/deploy")
    assert 'class="step-pills"' in ready.text
    assert 'href="/setup/drive?apps=prowlarr"' in ready.text
    assert words.WIZARD_BACK in ready.text

    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="running",
            apps=(
                _progress(
                    "prowlarr",
                    "Prowlarr",
                    "starting",
                    chip=STATUS_CHIP_STARTING,
                    line="Starting Prowlarr",
                    port=9696,
                ),
            ),
            headline="Starting Prowlarr...",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(),
        ),
    )
    # `DeployManager` reads `deploy.json` once, at construction - a second
    # client built after the write above is what makes it see this new
    # snapshot, the same way a fresh Marrquee process would after a restart.
    running = _client(settings).get("/deploy")
    assert 'class="step-pills"' not in running.text
    assert 'href="/setup/drive?apps=prowlarr"' not in running.text


def test_a_ticked_apps_registered_question_step_grows_the_ready_pages_pill_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_step = QuestionStep(
        app_id="prowlarr",
        step_id="fixture",
        title="Fixture questions",
        lede="",
        fields=(),
        check=lambda answers: QuestionCheck(ok=True, answers=answers, problem=None, field=None),
    )
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (fixture_step,))
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))

    response = _client(settings).get("/deploy")

    assert response.status_code == 200
    nav_match = re.search(r'<nav class="step-pills".*?</nav>', response.text, re.DOTALL)
    assert nav_match is not None
    nav = nav_match.group()
    fixture_index = nav.index("Fixture questions")
    drive_index = nav.index(words.WIZARD_STEP_DRIVE)
    deploy_index = nav.rindex(words.WIZARD_STEP_DEPLOY)
    assert fixture_index < drive_index < deploy_index


# --- GET /deploy: the running and error frames, through the poster grid ------


def test_a_running_snapshot_renders_one_lit_tile_and_the_rest_waiting(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr", "radarr")))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="running",
            apps=(
                _progress(
                    "prowlarr",
                    "Prowlarr",
                    "done",
                    chip=STATUS_CHIP_DONE,
                    line="Prowlarr is ready",
                    port=9696,
                ),
                _progress(
                    "sonarr",
                    "Sonarr",
                    "starting",
                    chip=STATUS_CHIP_STARTING,
                    line="Starting Sonarr",
                    port=8989,
                ),
                _progress(
                    "radarr",
                    "Radarr",
                    "waiting",
                    chip=STATUS_CHIP_WAITING,
                    line="Waiting",
                    port=7878,
                ),
            ),
            headline="Starting Sonarr...",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    posters = _posters(response.text)
    assert posters["prowlarr"]["data-state"] == "done"
    assert posters["sonarr"]["data-state"] == "starting"
    assert posters["radarr"]["data-state"] == "waiting"


def test_a_failed_runs_stuck_tile_renders_data_state_error_with_its_chip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="error",
            apps=(
                _progress(
                    "prowlarr",
                    "Prowlarr",
                    "done",
                    chip=STATUS_CHIP_DONE,
                    line="Prowlarr is ready",
                    port=9696,
                ),
                _progress(
                    "sonarr",
                    "Sonarr",
                    "error",
                    chip=STATUS_CHIP_ERROR,
                    line=app_line_error("Sonarr"),
                    port=8989,
                ),
            ),
            headline="Sonarr couldn't start",
            detail="Give it another try.",
            failure=Failure(
                code="never_became_ready",
                headline="Sonarr couldn't start",
                what_to_do="Give it another try.",
                technical="raw docker output nobody should see",
            ),
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    posters = _posters(response.text)
    assert posters["sonarr"]["data-state"] == "error"
    assert STATUS_CHIP_ERROR in response.text
    assert "raw docker output nobody should see" not in response.text


def test_every_tile_carries_all_four_status_icons_and_a_visible_chip(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(tuple(app.id for app in CATALOG)))
    client = _client(settings)

    response = client.get("/deploy")

    tile_count = len(CATALOG)
    # Scoped to `poster-icon` - the wiring block carries its own four
    # icons too, sharing two of these same `data-icon` values ("done" and
    # "error"), so an unscoped count would over-count once wiring exists.
    for icon in ("waiting", "starting", "done", "error"):
        assert response.text.count(f'class="poster-icon" data-icon="{icon}"') == tile_count
    assert response.text.count(f'data-role="chip">{STATUS_CHIP_WAITING}<') == tile_count


def test_the_finale_page_renders_200_and_does_not_redirect(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "prowlarr",
                    "Prowlarr",
                    "done",
                    chip=STATUS_CHIP_DONE,
                    line="Prowlarr is ready",
                    port=9696,
                ),
            ),
            headline="Now showing: your media server",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy", follow_redirects=False)

    assert response.status_code == 200


# --- POST /deploy -------------------------------------------------------------


def test_posting_deploy_without_a_saved_install_starts_nothing_and_redirects_to_the_wizard(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.post("/deploy", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


def test_posting_deploy_starts_a_run_and_comes_back_to_the_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    root = _fresh_root(settings)
    save_state(settings.config_dir, _install_state(("prowlarr",), storage_root=str(root)))

    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        images={get_app("prowlarr").image},
        compose_results={"prowlarr": ComposeResult(ok=True, exit_code=0, output="")},
        self_container_id="marrquee",
    )
    manager = DeployManager(settings, engine, probe=FakeReadinessProbe(default=True))
    app = create_app(settings=settings, engine=engine, manager=manager)

    # The manager's background task must keep progressing between the POST
    # and the polling GET below - a bare `TestClient(app)` never runs
    # startup and cannot be trusted to keep a background task alive across
    # requests (see tests/test_api.py's own note on the same fixture).
    with TestClient(app) as client:
        response = client.post("/deploy", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/deploy"

        final_phase = None
        for _ in range(200):
            final_phase = app.state.deploy.snapshot().phase
            if final_phase != "ready":
                break
            time.sleep(0.01)

        assert final_phase != "ready"


def test_the_deploy_form_carries_its_action_and_method_in_the_html(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    client = _client(settings)

    response = client.get("/deploy")

    assert '<form method="post" action="/deploy"' in response.text


# --- The wiring frame and its gold "linking" outline -------------------------


def test_the_wiring_frame_shows_the_step_count_the_steps_words_its_note_and_its_chip(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    step = WiringStep(
        index=2,
        total=4,
        key="prowlarr-sonarr",
        line="Connecting Prowlarr to Sonarr",
        state="done",
        chip=words.WIRING_CHIP_DONE,
        note="This can take a moment.",
        technical=None,
        involved=("prowlarr", "sonarr"),
    )
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="wiring",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
                _progress(
                    "sonarr", "Sonarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=8989
                ),
            ),
            headline="Connecting everything together.",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(step,),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert words.wiring_step_label(2, 4) in response.text
    assert "Connecting Prowlarr to Sonarr" in response.text
    assert "This can take a moment." in response.text
    assert words.WIRING_CHIP_DONE in response.text


def test_posters_in_the_current_steps_involved_carry_data_linking_others_do_not(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr", "radarr")))
    step = WiringStep(
        index=1,
        total=1,
        key="prowlarr-sonarr",
        line="Connecting Prowlarr to Sonarr",
        state="running",
        chip=words.WIRING_CHIP_RUNNING,
        note=None,
        technical=None,
        involved=("prowlarr", "sonarr"),
    )
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="wiring",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
                _progress(
                    "sonarr", "Sonarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=8989
                ),
                _progress(
                    "radarr", "Radarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=7878
                ),
            ),
            headline="Connecting everything together.",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(step,),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    posters = _posters(response.text)
    assert posters["prowlarr"]["data-linking"] == "true"
    assert posters["sonarr"]["data-linking"] == "true"
    assert posters["radarr"]["data-linking"] == "false"


def test_an_empty_wiring_tuple_still_renders_the_block_with_the_phase_headline(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="wiring",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
            ),
            headline="Connecting everything together.",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert response.status_code == 200
    assert 'data-phase="wiring"' in response.text
    assert 'data-role="wiring-text">Connecting everything together.<' in response.text


# --- The finale: the badge, the links, and the calm wiring-problem note ------


def test_the_finale_renders_the_badge_the_headline_and_the_hub_call_to_action(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
            ),
            headline="Now showing: your media server",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert words.FINALE_BADGE in response.text
    assert words.FINALE_HEADLINE in response.text
    assert words.FINALE_SUB in response.text
    assert f">{words.FINALE_CTA}<" in response.text
    assert 'href="/"' in response.text


def test_finale_links_use_the_address_the_browser_used(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "sonarr", "Sonarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=8989
                ),
            ),
            headline="Now showing: your media server",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy", headers={"host": "192.168.1.50:7788"})

    assert 'href="http://192.168.1.50:8989/"' in response.text
    assert words.open_app_label("Sonarr") in response.text


def test_the_finale_with_an_unusable_address_hides_the_links_grid_and_its_title(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
            ),
            headline="Now showing: your media server",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    # An unbracketed IPv6-shaped Host header is exactly the case `addresses.py`
    # refuses to guess at - `host_only` returns `None` for it.
    response = client.get("/deploy", headers={"host": "2001:db8::1"})

    assert words.SHOWTIMES_TITLE not in response.text
    assert words.open_app_label("Prowlarr") not in response.text


def test_a_finale_with_a_wiring_problem_stays_a_finale_shows_the_note_a_deploy_again_form_and_a_link_to_last_problem(  # noqa: E501
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    note = words.wiring_finale_note(("Connecting Prowlarr to Sonarr",))
    step = WiringStep(
        index=1,
        total=1,
        key="prowlarr-sonarr",
        line="Connecting Prowlarr to Sonarr",
        state="error",
        chip=words.WIRING_CHIP_ERROR,
        note=None,
        technical="raw docker output nobody should see",
        involved=("prowlarr", "sonarr"),
    )
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
                _progress(
                    "sonarr", "Sonarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=8989
                ),
            ),
            headline="Now showing: your media server",
            detail=note,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(step,),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert response.status_code == 200
    assert 'data-phase="finale"' in response.text
    assert _root(response.text)["data-has-note"] == "true"
    # Jinja autoescapes `'` as `&#39;`, and the engine's own note carries one.
    assert note.replace("'", "&#39;") in response.text
    assert f">{words.DEPLOY_AGAIN}<" in response.text
    assert 'href="/diagnostics#last-problem"' in response.text
    assert "raw docker output nobody should see" not in response.text


def test_a_clean_finale_has_no_deploy_again_button(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
            ),
            headline="Now showing: your media server",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert _root(response.text)["data-has-note"] == "false"


# --- The failure frame: it explains itself and offers a way out --------------


def test_the_failure_frame_shows_the_headline_the_advice_try_again_and_a_link_to_last_problem(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr", "sonarr")))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="error",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
                _progress(
                    "sonarr",
                    "Sonarr",
                    "error",
                    chip=STATUS_CHIP_ERROR,
                    line=app_line_error("Sonarr"),
                    port=8989,
                ),
            ),
            headline="Sonarr couldn't start",
            detail="Give it another try.",
            failure=Failure(
                code="never_became_ready",
                headline="Sonarr couldn't start",
                what_to_do="Give it another try.",
                technical="raw docker output nobody should see",
            ),
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    # Jinja autoescapes `'` as `&#39;`, and `app_line_error` carries one.
    assert "Sonarr couldn&#39;t start" in response.text
    assert "Give it another try." in response.text
    assert f">{words.DEPLOY_TRY_AGAIN}<" in response.text
    assert 'href="/diagnostics#last-problem"' in response.text


def test_no_page_links_to_the_raw_diagnostics_endpoint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("sonarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="error",
            apps=(
                _progress(
                    "sonarr",
                    "Sonarr",
                    "error",
                    chip=STATUS_CHIP_ERROR,
                    line=app_line_error("Sonarr"),
                    port=8989,
                ),
            ),
            headline="Sonarr couldn't start",
            detail=None,
            failure=Failure(
                code="never_became_ready",
                headline="Sonarr couldn't start",
                what_to_do="Give it another try.",
                technical="raw docker output nobody should see",
            ),
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert "/api/deploy/diagnostics" not in response.text


def test_the_failure_frame_contains_no_technical_string(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("radarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="error",
            apps=(
                _progress(
                    "radarr",
                    "Radarr",
                    "error",
                    chip=STATUS_CHIP_ERROR,
                    line=app_line_error("Radarr"),
                    port=7878,
                ),
            ),
            headline="Radarr couldn't start",
            detail=None,
            failure=Failure(
                code="never_became_ready",
                headline="Radarr couldn't start",
                what_to_do="Give it another try.",
                technical="Error response from daemon",
            ),
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    assert "Error response from daemon" not in response.text


def test_the_slow_start_note_renders_without_changing_the_tiles_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("radarr",)))
    note = words.app_note_slow_start("Radarr")
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="running",
            apps=(
                _progress(
                    "radarr",
                    "Radarr",
                    "starting",
                    chip=STATUS_CHIP_STARTING,
                    line="Starting Radarr",
                    note=note,
                    port=7878,
                ),
            ),
            headline="Starting Radarr...",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(),
        ),
    )
    client = _client(settings)

    response = client.get("/deploy")

    posters = _posters(response.text)
    assert posters["radarr"]["data-state"] == "starting"
    assert posters["radarr"]["data-has-note"] == "true"
    assert note in response.text
    assert 'class="poster-note-icon"' in response.text


def test_the_noscript_refresh_is_present_while_live_and_absent_at_finale_and_error(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="running",
            apps=(
                _progress(
                    "prowlarr",
                    "Prowlarr",
                    "starting",
                    chip=STATUS_CHIP_STARTING,
                    line="Starting Prowlarr",
                    port=9696,
                ),
            ),
            headline="Starting Prowlarr...",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at=None,
            wiring=(),
        ),
    )
    running = _client(settings).get("/deploy")
    assert '<meta http-equiv="refresh" content="5">' in running.text
    assert words.NOSCRIPT_REFRESH_NOTE in running.text

    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="finale",
            apps=(
                _progress(
                    "prowlarr", "Prowlarr", "done", chip=STATUS_CHIP_DONE, line="Ready", port=9696
                ),
            ),
            headline="Now showing: your media server",
            detail=None,
            failure=None,
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    finale = _client(settings).get("/deploy")
    assert "<noscript>" not in finale.text

    _write_snapshot(
        settings,
        DeploySnapshot(
            run_id="run-1",
            phase="error",
            apps=(
                _progress(
                    "prowlarr",
                    "Prowlarr",
                    "error",
                    chip=STATUS_CHIP_ERROR,
                    line=app_line_error("Prowlarr"),
                    port=9696,
                ),
            ),
            headline="Prowlarr couldn't start",
            detail=None,
            failure=Failure(
                code="never_became_ready",
                headline="Prowlarr couldn't start",
                what_to_do="Give it another try.",
                technical="raw",
            ),
            started_at="2026-09-23T00:00:00+00:00",
            finished_at="2026-09-23T00:05:00+00:00",
            wiring=(),
        ),
    )
    error = _client(settings).get("/deploy")
    assert "<noscript>" not in error.text


# --- Chrome: stylesheet order --------------------------------------------------


def test_tokens_app_wizard_poster_and_deploy_stylesheets_load_in_that_order(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(("prowlarr",)))
    client = _client(settings)

    response = client.get("/deploy")

    page = response.text
    tokens_index = page.index("css/tokens.css")
    app_index = page.index("css/app.css")
    wizard_index = page.index("css/wizard.css")
    poster_index = page.index("css/poster.css")
    deploy_index = page.index("css/deploy.css")
    assert tokens_index < app_index < wizard_index < poster_index < deploy_index
