"""Tests for Recyclarr's own config: the guide-backed quality profiles,
rendering `recyclarr.yml` from an install plus saved answers, writing or
removing Marrquee's own copy of it, reading its own verdict back out of its
per-run logs, and the monitor that runs a sync at most once at a time.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
import yaml

from marrquee.config import Settings
from marrquee.docker_client import ContainerSnapshot, DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.recyclarr import (
    GUIDE_PROFILES,
    RecyclarrMonitor,
    chosen_quality,
    quality_profile_name,
    read_last_sync,
    recyclarr_config_host_path,
    recyclarr_log_dir,
    remove_recyclarr_config,
    render_recyclarr_config,
    sync_wanted_after,
    write_recyclarr_config,
)
from marrquee.state import STATE_VERSION, InstallState, save_state
from marrquee.storage import PathEscapesRoot

_SONARR_KEY = "2" * 32
_RADARR_KEY = "3" * 32


def _fixture_state(app_ids: tuple[str, ...] = ("sonarr", "radarr")) -> InstallState:
    all_keys = {"sonarr": _SONARR_KEY, "radarr": _RADARR_KEY}
    return InstallState(
        version=STATE_VERSION,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys={app_id: all_keys.get(app_id, f"fake-{app_id}-key") for app_id in app_ids},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-19T00:00:00+00:00",
    )


def _running_recyclarr() -> ContainerSnapshot:
    return ContainerSnapshot(
        name="recyclarr",
        exists=True,
        state="running",
        exit_code=None,
        image="ghcr.io/recyclarr/recyclarr:8.7.2",
        detail=None,
    )


# --- Rendering ----------------------------------------------------------------


def test_render_uses_the_saved_answer_and_a_missing_answer_renders_1080p() -> None:
    state = _fixture_state(("sonarr", "radarr"))
    answers = {"sonarr": {"tv_quality": "4k"}}  # radarr's answer is missing entirely

    doc = yaml.safe_load(render_recyclarr_config(state, answers))

    sonarr_profile = doc["sonarr"]["sonarr"]["quality_profiles"][0]
    assert sonarr_profile["trash_id"] == GUIDE_PROFILES[("sonarr", "4k")].trash_id
    assert sonarr_profile["name"] == "WEB-2160p"

    radarr_profile = doc["radarr"]["radarr"]["quality_profiles"][0]
    assert radarr_profile["trash_id"] == GUIDE_PROFILES[("radarr", "1080p")].trash_id
    assert radarr_profile["name"] == "HD Bluray + WEB"


def test_render_configures_only_installed_arr_apps() -> None:
    state = _fixture_state(("radarr",))

    doc = yaml.safe_load(render_recyclarr_config(state, {}))

    assert "sonarr" not in doc
    assert doc["radarr"]["radarr"]["base_url"] == "http://radarr:7878"
    assert doc["radarr"]["radarr"]["api_key"] == _RADARR_KEY


def test_render_raises_with_no_arr_app() -> None:
    state = _fixture_state(())

    with pytest.raises(ValueError):
        render_recyclarr_config(state, {})


def test_rendered_yaml_parses_and_every_string_scalar_is_quoted() -> None:
    state = _fixture_state(("sonarr", "radarr"))

    text = render_recyclarr_config(state, {})
    doc = yaml.safe_load(text)

    assert doc["sonarr"]["sonarr"]["api_key"] == _SONARR_KEY
    assert doc["sonarr"]["sonarr"]["custom_format_groups"]["add"] == [
        {"trash_id": group} for group in GUIDE_PROFILES[("sonarr", "1080p")].cf_groups
    ]
    assert doc["sonarr"]["sonarr"]["quality_profiles"][0]["reset_unmatched_scores"] == {
        "enabled": True
    }

    quoted_prefixes = ("base_url:", "api_key:", "type:", "name:", "trash_id:", "- trash_id:")
    for line in text.splitlines():
        stripped = line.strip()
        if not any(stripped.startswith(prefix) for prefix in quoted_prefixes):
            continue
        value = stripped.split(":", 1)[1].strip()
        assert value.startswith('"') and value.endswith('"'), stripped


def test_quality_profile_name_maps_4k_sonarr_and_returns_none_for_prowlarr() -> None:
    answers = {"sonarr": {"tv_quality": "4k"}}

    assert quality_profile_name("sonarr", answers) == "WEB-2160p"
    assert quality_profile_name("prowlarr", answers) is None


def test_chosen_quality_falls_back_to_default_for_an_unknown_app_or_value() -> None:
    assert chosen_quality("prowlarr", {}) == "1080p"
    assert chosen_quality("sonarr", {"sonarr": {"tv_quality": "8k"}}) == "1080p"
    assert chosen_quality("radarr", {"radarr": {"movie_quality": "4k"}}) == "4k"


# --- Paths ----------------------------------------------------------------


def test_recyclarr_config_host_path_is_under_the_apps_folder() -> None:
    assert recyclarr_config_host_path(PurePosixPath("/volume1/media")) == PurePosixPath(
        "/volume1/media/marrquee/apps/recyclarr/recyclarr.yml"
    )


# --- Writing ----------------------------------------------------------------


def test_write_recyclarr_config_writes_0600_and_chowns_to_puid_pgid(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    state = _fixture_state(("sonarr",))

    calls: list[tuple[Path, int, int]] = []
    write_recyclarr_config(
        settings, state, {}, chown=lambda path, uid, gid: calls.append((path, uid, gid))
    )

    written = tmp_path / "volume1" / "media" / "marrquee" / "apps" / "recyclarr" / "recyclarr.yml"
    assert written.is_file()
    assert written.stat().st_mode & 0o777 == 0o600
    assert calls == [(written, 1000, 1000)]


def test_write_recyclarr_config_survives_a_chown_oserror(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    state = _fixture_state(("sonarr",))

    def _boom(path: Path, uid: int, gid: int) -> None:
        raise OSError("no chown here")

    write_recyclarr_config(settings, state, {}, chown=_boom)

    written = tmp_path / "volume1" / "media" / "marrquee" / "apps" / "recyclarr" / "recyclarr.yml"
    assert written.is_file()


def test_write_refuses_a_symlinked_apps_folder(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    state = _fixture_state(("sonarr",))
    apps_root = tmp_path / "volume1" / "media" / "marrquee" / "apps"
    apps_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (apps_root / "recyclarr").symlink_to(outside)

    with pytest.raises(PathEscapesRoot):
        write_recyclarr_config(settings, state, {}, chown=lambda *_: None)


def test_write_recyclarr_config_raises_without_a_storage_root(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    state = dataclasses.replace(_fixture_state(("sonarr",)), storage_root=None)

    with pytest.raises(ValueError):
        write_recyclarr_config(settings, state, {}, chown=lambda *_: None)


# --- Removing ----------------------------------------------------------------


def test_remove_deletes_only_recyclarr_yml_and_never_raises(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    apps_dir = tmp_path / "volume1" / "media" / "marrquee" / "apps" / "recyclarr"
    apps_dir.mkdir(parents=True)
    (apps_dir / "recyclarr.yml").write_text("keep-me: no")
    (apps_dir / "logs").mkdir()

    remove_recyclarr_config(settings, root)

    assert not (apps_dir / "recyclarr.yml").exists()
    assert (apps_dir / "logs").is_dir()


def test_remove_leaves_a_non_regular_file_untouched(tmp_path: Path) -> None:
    """Only a genuine regular file at `recyclarr.yml`'s place is ever
    unlinked. A FIFO reaches this exact case: `_safe_join`'s `.resolve()`
    only ever follows a SYMLINK component (a FIFO isn't one, so it passes
    through untouched), and `unlink()` itself would happily remove a FIFO
    the same way it removes a regular file - so only the dataclass's own
    `stat.S_ISREG` check stands between "something happens to sit at this
    name" and Marrquee deleting it.
    """
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    apps_dir = tmp_path / "volume1" / "media" / "marrquee" / "apps" / "recyclarr"
    apps_dir.mkdir(parents=True)
    fifo_path = apps_dir / "recyclarr.yml"
    os.mkfifo(fifo_path)

    remove_recyclarr_config(settings, root)

    assert stat.S_ISFIFO(fifo_path.lstat().st_mode)


def test_remove_never_raises_when_nothing_was_ever_written(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")

    remove_recyclarr_config(settings, root)  # must not raise


def test_remove_never_raises_on_a_symlinked_apps_folder(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    apps_root = tmp_path / "volume1" / "media" / "marrquee" / "apps"
    apps_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (apps_root / "recyclarr").symlink_to(outside)

    remove_recyclarr_config(settings, root)  # must not raise


# --- Reading Recyclarr's own verdict back out of its logs ---------------------


def _log_dir(tmp_path: Path) -> Path:
    return tmp_path / "volume1" / "media" / "marrquee" / "apps" / "recyclarr" / "logs" / "cli"


def test_read_last_sync_picks_the_newest_log_and_flags_err(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)

    older = log_dir / "recyclarr_2026-09-23_00-00-01.debug.log"
    older.write_text("[00:00:02 INF] started\n[00:00:03 DBG] done\n")
    older_time = datetime(2026, 9, 23, 0, 0, 3, tzinfo=UTC).timestamp()
    os.utime(older, (older_time, older_time))

    newer = log_dir / "recyclarr_2026-09-24_00-00-01.debug.log"
    newer.write_text("[00:00:02 INF] started\n[00:00:03 ERR] sonarr sync failed\n")
    newer_time = datetime(2026, 9, 24, 0, 0, 3, tzinfo=UTC).timestamp()
    os.utime(newer, (newer_time, newer_time))

    record = read_last_sync(settings, "/volume1/media")

    assert record is not None
    assert record.ok is False
    assert record.finished_at == datetime.fromtimestamp(newer_time, tz=UTC)


def test_read_last_sync_removing_the_err_line_makes_it_ok(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    log_path = log_dir / "recyclarr_2026-09-24_00-00-01.debug.log"
    log_path.write_text("[00:00:02 INF] started\n[00:00:03 DBG] done\n")

    record = read_last_sync(settings, "/volume1/media")

    assert record is not None
    assert record.ok is True


def test_read_last_sync_missing_folder_is_none(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)

    assert read_last_sync(settings, "/volume1/media") is None


def test_read_last_sync_ignores_files_with_the_wrong_name(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "notes.txt").write_text("[00:00:03 ERR] not a recyclarr log")
    (log_dir / "recyclarr_2026-09-24_00-00-01.debug.log.bak").write_text("[00:00:03 ERR] backup")

    assert read_last_sync(settings, "/volume1/media") is None


def test_read_last_sync_survives_an_unreadable_file(tmp_path: Path) -> None:
    """A directory sitting where the newest-named log should be can't be
    opened as a file - the whole read must come back None, never raise.
    """
    settings = Settings(host_mount=tmp_path)
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "recyclarr_2026-09-24_00-00-01.debug.log").mkdir()

    assert read_last_sync(settings, "/volume1/media") is None


def test_recyclarr_log_dir_is_under_the_apps_folder() -> None:
    assert recyclarr_log_dir(PurePosixPath("/volume1/media")) == PurePosixPath(
        "/volume1/media/marrquee/apps/recyclarr/logs/cli"
    )


# --- Deciding when a sync is worth asking for ----------------------------------


def test_sync_wanted_after_a_full_deploy_when_recyclarr_is_installed() -> None:
    assert sync_wanted_after(("prowlarr", "sonarr", "recyclarr"), None) is True


def test_sync_wanted_after_a_full_deploy_without_recyclarr() -> None:
    assert sync_wanted_after(("prowlarr", "sonarr"), None) is False


def test_sync_wanted_after_adding_sonarr_or_radarr_with_recyclarr_installed() -> None:
    assert sync_wanted_after(("recyclarr", "sonarr"), "sonarr") is True
    assert sync_wanted_after(("recyclarr", "radarr"), "radarr") is True
    assert sync_wanted_after(("recyclarr",), "recyclarr") is True


def test_sync_wanted_after_adding_an_unrelated_app_does_not() -> None:
    assert sync_wanted_after(("recyclarr", "prowlarr"), "prowlarr") is False


def test_sync_wanted_after_without_recyclarr_installed_never_wants_one() -> None:
    assert sync_wanted_after(("sonarr",), "sonarr") is False


# --- RecyclarrMonitor -----------------------------------------------------------


def _monitor_settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path, config_dir=tmp_path / "config")


async def test_a_run_with_recyclarr_not_installed_makes_no_docker_call(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "radarr")))
    engine = FakeDockerEngine(DockerStatus(connected=True))
    monitor = RecyclarrMonitor(settings, engine)

    result = await monitor.request_sync()

    assert result is False
    assert engine.calls == []


async def test_a_run_with_no_install_makes_no_docker_call(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    engine = FakeDockerEngine(DockerStatus(connected=True))
    monitor = RecyclarrMonitor(settings, engine)

    result = await monitor.request_sync()

    assert result is False
    assert engine.calls == []


async def test_exec_on_a_stopped_recyclarr_sets_start_failed_until_a_newer_log_appears(
    tmp_path: Path,
) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(DockerStatus(connected=True))  # recyclarr never started
    now = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
    monitor = RecyclarrMonitor(settings, engine, clock=lambda: now)

    result = await monitor.request_sync()
    assert result is False
    assert ("exec_start", ("recyclarr", ("recyclarr", "sync"))) in engine.calls

    status = await monitor.status()
    assert status.start_failed is True
    assert status.run_failed is True

    # A newer, ok log landing afterwards (the container's own daily cron
    # run, say) is what finally clears both.
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    later = now + timedelta(hours=1)
    log_path = log_dir / f"recyclarr_{later:%Y-%m-%d_%H-%M-%S}.debug.log"
    log_path.write_text("[00:00:03 INF] ok\n")
    later_mtime = later.timestamp()
    os.utime(log_path, (later_mtime, later_mtime))

    status_after = await monitor.status()
    assert status_after.start_failed is False
    assert status_after.run_failed is False


async def test_a_config_write_failure_sets_start_failed_and_makes_no_docker_call(
    tmp_path: Path,
) -> None:
    settings = _monitor_settings(tmp_path)
    state = _fixture_state(("sonarr", "recyclarr"))
    save_state(settings.config_dir, state)
    # Plant a symlinked apps folder so `write_recyclarr_config` raises
    # `PathEscapesRoot`, the same "config write failed" this monitor must
    # survive before it ever touches Docker.
    apps_root = tmp_path / "volume1" / "media" / "marrquee" / "apps"
    apps_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (apps_root / "recyclarr").symlink_to(outside)
    engine = FakeDockerEngine(DockerStatus(connected=True))
    monitor = RecyclarrMonitor(settings, engine)

    result = await monitor.request_sync()

    assert result is False
    assert engine.calls == []
    status = await monitor.status()
    assert status.start_failed is True
    assert status.run_failed is True


async def test_exit_zero_with_an_err_in_the_log_still_fails_the_run(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "recyclarr_2026-09-24_00-00-01.debug.log").write_text(
        "[00:00:03 ERR] sonarr sync failed\n"
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 0},
    )
    monitor = RecyclarrMonitor(settings, engine)

    result = await monitor.request_sync()

    assert result is False
    status = await monitor.status()
    assert status.run_failed is True
    assert status.start_failed is False


async def test_exit_zero_with_no_err_in_the_log_succeeds(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "recyclarr_2026-09-24_00-00-01.debug.log").write_text("[00:00:03 INF] ok\n")
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 0},
    )
    monitor = RecyclarrMonitor(settings, engine)

    result = await monitor.request_sync()

    assert result is True
    status = await monitor.status()
    assert status.run_failed is False
    assert status.start_failed is False


async def test_a_non_zero_exit_fails_the_run_even_with_an_ok_log(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "recyclarr_2026-09-24_00-00-01.debug.log").write_text("[00:00:03 INF] ok\n")
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 1},
    )
    monitor = RecyclarrMonitor(settings, engine)

    result = await monitor.request_sync()

    assert result is False
    status = await monitor.status()
    assert status.run_failed is True


async def test_a_clean_run_clears_run_failed_even_with_no_log_to_confirm_it(
    tmp_path: Path,
) -> None:
    """A failed run sets `run_failed_at`. The NEXT clean run (exit 0) has to
    clear it even when there is no log at all to read back (Recyclarr wrote
    nowhere Marrquee looks, say) - `last is None` must never be treated as
    "still failed forever". A log that happens to already be ok masks a
    monitor that forgot to clear the flag on success; this test never lets
    one exist.
    """
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 1},
    )
    monitor = RecyclarrMonitor(settings, engine)

    first_result = await monitor.request_sync()
    assert first_result is False
    mid_status = await monitor.status()
    assert mid_status.run_failed is True
    assert mid_status.last is None  # no log folder ever existed

    engine._exec_exit_codes["recyclarr"] = 0  # type: ignore[attr-defined]
    second_result = await monitor.request_sync()

    assert second_result is True
    final_status = await monitor.status()
    assert final_status.last is None  # still no log to read - unrelated to the fix
    assert final_status.run_failed is False


# --- after_sync: Seerr's profile refresh hook ----------------------------------


class _SyncSpy:
    """Stands in for `main.py`'s own `_refresh_seerr` closure: records
    whether the hook actually ran, and can be told to raise so a test can
    prove that never changes the run's own verdict.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.calls = 0
        self._raises = raises

    async def __call__(self) -> None:
        self.calls += 1
        if self._raises:
            raise RuntimeError("seerr refresh boom")


async def test_after_sync_runs_once_after_a_successful_sync(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 0},
    )
    spy = _SyncSpy()
    monitor = RecyclarrMonitor(settings, engine, after_sync=spy)

    result = await monitor.request_sync()

    assert result is True
    assert spy.calls == 1


async def test_after_sync_does_not_run_on_a_failed_exit_code(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 1},
    )
    spy = _SyncSpy()
    monitor = RecyclarrMonitor(settings, engine, after_sync=spy)

    result = await monitor.request_sync()

    assert result is False
    assert spy.calls == 0


async def test_after_sync_does_not_run_when_the_log_reads_as_failed(tmp_path: Path) -> None:
    """The exec can exit 0 and still not count as a real success - the
    freshly written log showing an ERR line is what `_run_once` trusts
    instead, and the hook must never run ahead of that check.
    """
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    log_dir = _log_dir(tmp_path)
    log_dir.mkdir(parents=True)
    (log_dir / "recyclarr_2026-09-24_00-00-01.debug.log").write_text(
        "[00:00:03 ERR] sonarr sync failed\n"
    )
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 0},
    )
    spy = _SyncSpy()
    monitor = RecyclarrMonitor(settings, engine, after_sync=spy)

    result = await monitor.request_sync()

    assert result is False
    assert spy.calls == 0


async def test_after_sync_raising_is_logged_and_never_changes_the_result(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_exit_codes={"recyclarr": 0},
    )
    spy = _SyncSpy(raises=True)
    monitor = RecyclarrMonitor(settings, engine, after_sync=spy)

    result = await monitor.request_sync()

    assert result is True
    assert spy.calls == 1


def test_create_app_wires_the_default_monitor_with_a_seerr_refresh_hook(tmp_path: Path) -> None:
    """`create_app`'s own default `RecyclarrMonitor` (no `recyclarr=`
    passed in) must carry a real `after_sync` hook - otherwise a real,
    Marrquee-started sync would never move Seerr's Sonarr/Radarr profiles
    at all. An injected `recyclarr=` keeps whichever hook it already has.
    """
    settings = _monitor_settings(tmp_path)
    app = create_app(settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True)))

    assert app.state.recyclarr._after_sync is not None  # type: ignore[attr-defined]


async def test_status_reports_syncing_while_the_exec_polls_running(tmp_path: Path) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_running_polls=1,
    )
    seen: list[bool] = []

    async def fake_sleep(seconds: float) -> None:
        status = await monitor.status()
        seen.append(status.syncing)

    monitor = RecyclarrMonitor(settings, engine, sleep=fake_sleep)
    result = await monitor.request_sync()

    assert result is True
    assert seen == [True]


async def test_request_sync_twice_while_running_starts_one_exec_then_exactly_one_more(
    tmp_path: Path,
) -> None:
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True),
        containers={"recyclarr": _running_recyclarr()},
        exec_running_polls=1,
    )
    requested_again = False

    async def fake_sleep(seconds: float) -> None:
        nonlocal requested_again
        if not requested_again:
            requested_again = True
            # A second request, arriving while the first sync is still
            # polling, must not be dropped - it earns exactly one more run.
            second_task = monitor.request_sync()
            assert second_task is first_task

    monitor = RecyclarrMonitor(settings, engine, sleep=fake_sleep)
    first_task = monitor.request_sync()
    result = await first_task

    assert result is True
    exec_starts = [call for call in engine.calls if call[0] == "exec_start"]
    assert len(exec_starts) == 2
    assert all(call[1] == ("recyclarr", ("recyclarr", "sync")) for call in exec_starts)


async def test_a_second_request_after_the_first_finishes_starts_its_own_new_exec(
    tmp_path: Path,
) -> None:
    """A request that arrives once the previous run is already done gets a
    brand-new task, never the finished one handed back again.
    """
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True), containers={"recyclarr": _running_recyclarr()}
    )
    monitor = RecyclarrMonitor(settings, engine)

    first_result = await monitor.request_sync()
    second_task = monitor.request_sync()
    second_result = await second_task

    assert first_result is True
    assert second_result is True
    exec_starts = [call for call in engine.calls if call[0] == "exec_start"]
    assert len(exec_starts) == 2


class _StaleTask:
    """Stands in for a task left over from a dead event loop: not done, and
    bound to a loop that no longer runs - the exact two things
    `_in_flight_task` checks, with no real coroutine to ever leave
    unawaited.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def done(self) -> bool:
        return False

    def get_loop(self) -> asyncio.AbstractEventLoop:
        return self._loop


async def test_request_sync_ignores_a_task_from_a_dead_loop(tmp_path: Path) -> None:
    """A bare `TestClient(app)` hands each request a fresh event loop - a
    task left over from an earlier one (never finished, never going to be)
    must never be joined. `request_sync` has to start a genuinely new run
    on the CURRENT loop instead of waiting forever for one nothing iterates
    any more.
    """
    settings = _monitor_settings(tmp_path)
    save_state(settings.config_dir, _fixture_state(("sonarr", "recyclarr")))
    engine = FakeDockerEngine(
        DockerStatus(connected=True), containers={"recyclarr": _running_recyclarr()}
    )
    monitor = RecyclarrMonitor(settings, engine)

    dead_loop = asyncio.new_event_loop()
    dead_loop.close()
    stale_task = _StaleTask(dead_loop)
    monitor._task = stale_task  # type: ignore[assignment]

    assert monitor._in_flight_task() is None  # type: ignore[attr-defined]

    result = await monitor.request_sync()

    assert result is True
    assert monitor._task is not stale_task  # type: ignore[comparison-overlap]
    assert any(call[0] == "exec_start" for call in engine.calls)
