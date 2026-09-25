"""Tests for screen two - where's your big drive, and your time zone.

Most of this file exercises the no-JavaScript path: every POST here is a
real form submission through `TestClient`, the same as a browser with
scripting switched off would send. `wizard.assess` and the time zone
helpers are tested directly first (no FastAPI, no disk beyond `tmp_path`),
then again through the route, because the route's whole job is calling
them and rendering what they return. The bottom two sections cover what
Chunk 5 added on top of that working base: the live JSON check the
in-browser script calls, and `wizard.js` itself, read as plain text since
there is no browser runner in this suite.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import marrquee.questions as questions_module
from marrquee import wizard, words
from marrquee.config import Settings
from marrquee.deploy import AppProgress, DeployPhase, DeploySnapshot
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep
from marrquee.state import STATE_VERSION, InstallState, load_state, save_state, write_json_atomic

_WIZARD_JS_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "js" / "wizard.js"
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(config_dir=tmp_path / "config", host_mount=tmp_path / "host")


def _client(settings: Settings) -> TestClient:
    status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    return TestClient(app)


def _mount_volume1(settings: Settings, *names: str) -> None:
    for name in names:
        (settings.host_mount / "volume1" / name).mkdir(parents=True)


def _install_state(
    *, app_ids: tuple[str, ...] = ("radarr",), timezone: str = "Etc/UTC"
) -> InstallState:
    return InstallState(
        version=STATE_VERSION,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys={},
        puid=1000,
        pgid=1000,
        umask="022",
        timezone=timezone,
        created="2026-09-22T00:00:00+00:00",
    )


def _write_deploy(settings: Settings, *, phase: DeployPhase) -> None:
    """Persist a `deploy.json` at `phase` - the shape every hub-exists-guard
    test needs, and the "still error, still reachable" test's counterpart.
    """
    snapshot = DeploySnapshot(
        run_id="run-1",
        phase=phase,
        apps=(),
        headline="Now showing" if phase == "finale" else "Something went wrong",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(snapshot))


def _fixture_step(app_id: str = "radarr") -> QuestionStep:
    return QuestionStep(
        app_id=app_id,
        step_id="fixture",
        title="Fixture questions",
        lede="A fixture step for the test.",
        fields=(QuestionField(name="token", label="Token", kind="text"),),
        check=lambda answers: QuestionCheck(ok=True, answers=answers, problem=None, field=None),
    )


# --- assess: the one function the form post and the live check share --------


def test_assess_a_good_empty_folder_is_usable_with_an_ok_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")

    usable, message = wizard.assess(settings, "/volume1/fresh", ("radarr",))

    assert usable
    assert message.tone == "ok"


def test_assess_a_populated_target_is_not_usable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")
    movies = settings.host_mount / "volume1" / "media" / "data" / "media" / "movies"
    movies.mkdir(parents=True)
    (movies / "old.mkv").write_text("")

    usable, message = wizard.assess(settings, "/volume1/media", ("radarr",))

    assert not usable
    assert message.tone == "problem"
    assert "files" in message.text


def test_assess_a_folder_outside_volume1_is_not_shared_not_a_typo(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")

    usable, message = wizard.assess(settings, "/mnt/storage", ("radarr",))

    assert not usable
    assert "/volume1" in message.text
    assert "Check the spelling" not in message.text


def test_assess_never_raises_on_a_hostile_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    for hostile in ("", "relative", "/", "/etc", "/a\nb", "/" + "x" * 4000):
        usable, message = wizard.assess(settings, hostile, ("radarr",))
        assert not usable
        assert message.text


def test_assess_never_puts_storage_check_detail_in_the_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "locked")

    import marrquee.storage as storage_module

    def _raise_permission_error(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated: [Errno 13] secret detail")

    monkeypatch.setattr(storage_module.tempfile, "NamedTemporaryFile", _raise_permission_error)

    _, message = wizard.assess(settings, "/volume1/locked", ("radarr",))

    assert "secret detail" not in message.text


# --- time zone: the fixed list, aliases hidden, and the choice functions ----


def test_timezone_keys_holds_canonical_names_only_aliases_are_hidden() -> None:
    assert "Asia/Kolkata" in wizard.TIMEZONE_KEYS
    assert "Asia/Calcutta" not in wizard.TIMEZONE_KEYS
    assert "Etc/UTC" in wizard.TIMEZONE_KEYS
    assert wizard.TIMEZONE_ALIASES["Asia/Calcutta"] == "Asia/Kolkata"


def test_timezone_choice_returns_the_default_for_none_or_unknown() -> None:
    assert wizard.timezone_choice(None, "Etc/UTC") == "Etc/UTC"
    assert wizard.timezone_choice("Not/AZone", "Etc/UTC") == "Etc/UTC"
    assert wizard.timezone_choice("", "Etc/UTC") == "Etc/UTC"


def test_timezone_choice_accepts_a_known_zone() -> None:
    assert wizard.timezone_choice("America/Chicago", "Etc/UTC") == "America/Chicago"


def test_timezone_choice_translates_an_alias_to_its_canonical_name() -> None:
    assert wizard.timezone_choice("Asia/Calcutta", "Etc/UTC") == "Asia/Kolkata"


def test_timezone_groups_cover_every_key_exactly_once() -> None:
    groups = wizard.timezone_groups()
    seen = [value for group in groups for value, _label in group.options]

    assert set(seen) == wizard.TIMEZONE_KEYS
    assert len(seen) == len(set(seen))


def test_timezone_groups_labels_read_as_place_names_region_dropped() -> None:
    groups = {group.label: dict(group.options) for group in wizard.timezone_groups()}

    assert groups["Americas"]["America/Argentina/Buenos_Aires"] == "Argentina - Buenos Aires"
    assert groups["Other"]["Etc/UTC"] == words.WIZARD_TIMEZONE_UTC


def test_default_timezone_prefers_the_saved_zone(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state = _install_state(timezone="Europe/London")

    assert wizard.default_timezone(settings, state) == ("Europe/London", "saved")


def test_default_timezone_falls_back_to_the_host_zone_when_nothing_is_saved(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    (settings.host_mount / "etc").mkdir(parents=True)
    (settings.host_mount / "etc" / "timezone").write_text("America/Chicago\n")

    assert wizard.default_timezone(settings, None) == ("America/Chicago", "host")


def test_default_timezone_falls_back_to_utc_when_nothing_is_usable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert wizard.default_timezone(settings, None) == ("Etc/UTC", "host")


# --- GET /setup/drive: needs usable apps, first paint is honest -------------


def test_no_apps_chosen_redirects_to_the_apps_screen(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"


def test_drive_with_an_unanswered_step_redirects_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_fixture_step(),))
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", params={"apps": "radarr"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/questions/radarr/fixture?apps=radarr"


def test_drive_moves_on_once_the_step_is_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_fixture_step(),))
    settings = _settings(tmp_path)
    client = _client(settings)
    client.post(
        "/setup/questions/radarr/fixture",
        data={"apps": "radarr", "token": "a-value"},
        follow_redirects=False,
    )

    response = client.get("/setup/drive", params={"apps": "radarr"}, follow_redirects=False)

    assert response.status_code == 200


def test_setup_drive_redirects_home_once_the_hub_exists(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")
    save_state(settings.config_dir, _install_state(app_ids=("prowlarr",)))
    install_bytes_before = (settings.config_dir / "install.json").read_bytes()
    _write_deploy(settings, phase="finale")
    client = _client(settings)

    get_response = client.get("/setup/drive", params={"apps": "radarr"}, follow_redirects=False)
    post_response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/media"},
        follow_redirects=False,
    )

    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/"
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/"
    assert (settings.config_dir / "install.json").read_bytes() == install_bytes_before


def test_a_failed_first_deploy_can_still_reach_setup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_deploy(settings, phase="error")
    client = _client(settings)

    response = client.get("/setup/drive", params={"apps": "radarr"}, follow_redirects=False)

    assert response.status_code == 200


def test_first_paint_is_empty_with_placeholder_hint_and_freshstart_note(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.host_mount / "volume1").mkdir(parents=True)
    client = _client(settings)

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert response.status_code == 200
    assert 'value=""' in response.text
    assert f'placeholder="{words.WIZARD_PATH_PLACEHOLDER}"' in response.text
    assert "/volume1" in response.text
    assert words.WIZARD_FRESH_START_NOTE in response.text

    match = re.search(r'id="drive-result"[^>]*>(.*?)</div>', response.text, re.DOTALL)
    assert match is not None
    assert match.group(1).strip() == ""


def test_back_carries_the_chosen_apps(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", params={"apps": "radarr,prowlarr"})

    assert 'href="/setup/apps?apps=prowlarr,radarr"' in response.text


def test_the_timezone_list_is_grouped_by_region_with_place_name_labels(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert '<optgroup label="Americas">' in response.text
    assert '<optgroup label="Indian Ocean">' in response.text
    assert '<optgroup label="Other">' in response.text
    assert "Argentina - Buenos Aires" in response.text
    assert words.WIZARD_TIMEZONE_UTC in response.text


def test_timezone_defaults_to_etc_utc_with_no_host_file(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert 'data-timezone-source="host"' in response.text
    assert re.search(r'<option value="Etc/UTC"[^>]*selected', response.text)


def test_timezone_defaults_to_the_host_files_zone_when_present(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    (settings.host_mount / "etc").mkdir(parents=True)
    (settings.host_mount / "etc" / "timezone").write_text("America/Chicago\n")
    client = _client(settings)

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert 'data-timezone-source="host"' in response.text
    assert re.search(r'<option value="America/Chicago"[^>]*selected', response.text)


def test_timezone_defaults_to_the_saved_zone_for_a_returning_owner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    save_state(settings.config_dir, _install_state(timezone="Europe/London"))
    client = _client(settings)

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert 'data-timezone-source="saved"' in response.text
    assert re.search(r'<option value="Europe/London"[^>]*selected', response.text)


# --- POST /setup/drive: assess first, then install_apps ---------------------


def test_a_good_path_saves_through_install_apps_and_redirects_to_deploy(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "prowlarr,radarr", "path": "/volume1/fresh", "timezone": "America/Chicago"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/deploy"
    saved = load_state(settings.config_dir)
    assert saved is not None
    assert saved.app_ids == ("prowlarr", "radarr")
    assert set(saved.api_keys) == {"prowlarr", "radarr"}
    assert saved.timezone == "America/Chicago"


def test_posting_drive_with_an_unanswered_step_redirects_to_it_and_saves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(questions_module, "QUESTION_STEPS", (_fixture_step(),))
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/fresh"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/questions/radarr/fixture?apps=radarr"
    assert load_state(settings.config_dir) is None


def test_continuing_to_setup_once_the_hub_exists_closes_the_wizard_instead(
    tmp_path: Path,
) -> None:
    """Once a deploy has reached its finale, the Hub is home - a stale
    wizard tab (re-choosing apps on a NAS that's already set up) must never
    resurrect the wizard or rewrite install.json out from under a running
    Hub. The owner's own way back in is the Hub's own "+" panel, not this
    screen.
    """
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    finished = DeploySnapshot(
        run_id="a-finished-run-from-before-these-new-choices",
        phase="finale",
        apps=(
            AppProgress(
                app_id="prowlarr",
                name="Prowlarr",
                state="done",
                chip="Ready",
                line="Prowlarr is ready",
                note=None,
                port=9696,
            ),
        ),
        headline="Now showing: your media server",
        detail=None,
        failure=None,
        started_at="2026-09-19T00:00:00+00:00",
        finished_at="2026-09-19T00:05:00+00:00",
        wiring=(),
    )
    write_json_atomic(settings.config_dir / "deploy.json", dataclasses.asdict(finished))
    status = DockerStatus(connected=True, version="27.3.1")
    app = create_app(settings=settings, engine=FakeDockerEngine(status))
    client = TestClient(app)
    assert app.state.deploy.snapshot().phase == "finale"

    response = client.post(
        "/setup/drive",
        data={"apps": "prowlarr", "path": "/volume1/fresh", "timezone": "America/Chicago"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert load_state(settings.config_dir) is None
    assert app.state.deploy.snapshot().phase == "finale"


def test_a_populated_folder_is_refused_at_200_with_storys_wording_and_nothing_saved(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")
    movies = settings.host_mount / "volume1" / "media" / "data" / "media" / "movies"
    movies.mkdir(parents=True)
    (movies / "old.mkv").write_text("")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/media"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert words.refusal_populated_target("/volume1/media") in response.text
    assert load_state(settings.config_dir) is None


def test_a_folder_outside_volume1_is_refused_with_not_shared_wording(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/mnt/storage"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "/volume1" in response.text
    assert "Check the spelling" not in response.text
    assert load_state(settings.config_dir) is None


def test_use_suggestion_overrides_the_typed_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "movies")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/movis", "use_suggestion": "/volume1/movies"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = load_state(settings.config_dir)
    assert saved is not None
    assert saved.storage_root == "/volume1/movies"


def test_a_refused_post_echoes_the_typed_path_back_into_the_field(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/movis"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert 'value="/volume1/movis"' in response.text


def test_a_posted_timezone_is_saved(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/fresh", "timezone": "America/Chicago"},
        follow_redirects=False,
    )

    saved = load_state(settings.config_dir)
    assert saved is not None
    assert saved.timezone == "America/Chicago"


def test_an_unknown_timezone_saves_the_default_and_never_500s(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/fresh", "timezone": "Not/AZone"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = load_state(settings.config_dir)
    assert saved is not None
    assert saved.timezone == "Etc/UTC"


def test_a_missing_timezone_field_saves_the_default_and_never_500s(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/fresh"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = load_state(settings.config_dir)
    assert saved is not None
    assert saved.timezone == "Etc/UTC"


def test_a_refused_post_keeps_the_chosen_timezone_selected_marked_posted(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/does-not-exist", "timezone": "America/Chicago"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert 'data-timezone-source="posted"' in response.text
    assert re.search(r'<option value="America/Chicago"[^>]*selected', response.text)


def test_a_pathologically_long_path_is_refused_at_200_not_500(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/" + "x" * 4000},
        follow_redirects=False,
    )

    assert response.status_code == 200


# --- Accessibility / boundary -------------------------------------------------


def test_no_response_contains_storage_check_detail_nothing_disabled_glyph_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "locked")
    client = _client(settings)

    import marrquee.storage as storage_module

    def _raise_permission_error(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated: [Errno 13] secret detail")

    monkeypatch.setattr(storage_module.tempfile, "NamedTemporaryFile", _raise_permission_error)

    response = client.post(
        "/setup/drive",
        data={"apps": "radarr", "path": "/volume1/locked"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "secret detail" not in response.text
    assert "disabled" not in response.text
    assert 'aria-hidden="true"' in response.text


# --- POST /setup/drive/check: the live, as-you-type verdict -----------------


def test_check_endpoint_reports_a_real_folder_ok_with_free_space(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    response = client.post(
        "/setup/drive/check", json={"path": "/volume1/fresh", "apps": ["radarr"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tone"] == "ok"
    assert re.search(r"about \d+(\.\d)? (GB|TB) free|less than 1 GB free", body["text"])
    assert body["suggestion"] is None


def test_check_endpoint_offers_one_suggestion_for_a_near_miss(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "movies")
    client = _client(settings)

    response = client.post(
        "/setup/drive/check", json={"path": "/volume1/movis", "apps": ["radarr"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tone"] == "problem"
    assert body["suggestion"] == "/volume1/movies"


def test_check_endpoint_names_volume1_for_a_folder_outside_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")
    client = _client(settings)

    response = client.post("/setup/drive/check", json={"path": "/mnt/storage", "apps": ["radarr"]})

    assert response.status_code == 200
    body = response.json()
    assert body["tone"] == "problem"
    assert body["suggestion"] is None
    assert "/volume1" in body["text"]


def test_check_endpoint_did_you_mean_plus_other_drive_guidance_for_a_second_pool(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "media")
    client = _client(settings)

    response = client.post(
        "/setup/drive/check", json={"path": "/volume2/media", "apps": ["radarr"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["suggestion"] == "/volume1/media"
    assert "/volume1/media" in body["text"]
    assert body["guidance"] is not None
    assert "/volume2" in body["guidance"]


def test_check_endpoint_never_5xx_over_a_hostile_table(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    for hostile in ("", "relative", "/", "/etc", "/a\nb", "/" + "x" * 4000):
        response = client.post("/setup/drive/check", json={"path": hostile, "apps": ["radarr"]})
        assert response.status_code < 500


def test_check_endpoint_rejects_an_oversized_body_with_422_not_500(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/drive/check", json={"path": "/" + "x" * 5000, "apps": ["radarr"]}
    )

    assert response.status_code == 422


def test_check_endpoint_rejects_an_unknown_field(tmp_path: Path) -> None:
    client = _client(_settings(tmp_path))

    response = client.post(
        "/setup/drive/check",
        json={"path": "/volume1/media", "apps": ["radarr"], "extra": "nope"},
    )

    assert response.status_code == 422


def test_check_endpoint_never_returns_storage_check_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "locked")
    client = _client(settings)

    import marrquee.storage as storage_module

    def _raise_permission_error(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated: [Errno 13] secret detail")

    monkeypatch.setattr(storage_module.tempfile, "NamedTemporaryFile", _raise_permission_error)

    response = client.post(
        "/setup/drive/check", json={"path": "/volume1/locked", "apps": ["radarr"]}
    )

    assert response.status_code == 200
    assert "secret detail" not in response.text
    assert "detail" not in response.json()


def test_check_endpoint_saves_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _mount_volume1(settings, "fresh")
    client = _client(settings)

    client.post("/setup/drive/check", json={"path": "/volume1/fresh", "apps": ["radarr"]})

    assert load_state(settings.config_dir) is None


# --- wizard.js: read as plain text, since there is no browser runner here ---


def test_wizard_drive_html_loads_the_script_with_defer_and_keeps_form_action(
    tmp_path: Path,
) -> None:
    client = _client(_settings(tmp_path))

    response = client.get("/setup/drive", params={"apps": "radarr"})

    assert "<script defer" in response.text
    assert "static/js/wizard.js" in response.text
    assert 'method="post" action="/setup/drive" data-drive-form' in response.text


def test_wizard_js_contains_no_words_py_sentence() -> None:
    script = _WIZARD_JS_PATH.read_text()

    for name in words.WORDS_INVENTORY:
        value = getattr(words, name)
        if isinstance(value, str) and len(value) >= 12:
            assert value not in script, f"{name} ({value!r}) leaked into wizard.js"


def test_wizard_js_does_nothing_without_a_drive_form() -> None:
    script = _WIZARD_JS_PATH.read_text()

    assert 'querySelector("[data-drive-form]")' in script


def test_wizard_js_debounces_and_aborts_the_previous_request() -> None:
    script = _WIZARD_JS_PATH.read_text()

    assert "400" in script
    assert "AbortController" in script
    assert ".abort()" in script
    assert 'addEventListener("input"' in script


def test_wizard_js_shows_the_checking_text_with_aria_busy_while_in_flight() -> None:
    script = _WIZARD_JS_PATH.read_text()

    assert "checkingText" in script
    assert 'setAttribute("aria-busy", "true")' in script


def test_wizard_js_only_applies_browser_timezone_when_source_is_host_and_option_exists() -> None:
    script = _WIZARD_JS_PATH.read_text()

    assert 'dataset.timezoneSource !== "host"' in script
    assert "resolvedOptions().timeZone" in script
    assert 'option[value="' in script
    assert "timezoneAliases" in script


def test_wizard_js_intercepts_a_suggestion_click_instead_of_letting_it_submit() -> None:
    """A single delegated listener, so this covers both a suggestion button
    this script built from a live check and the real `<button
    type="submit">` the server renders on first paint after a refused post.
    """
    script = _WIZARD_JS_PATH.read_text()

    assert 'closest(".suggestion-button")' in script
    assert "event.preventDefault()" in script
