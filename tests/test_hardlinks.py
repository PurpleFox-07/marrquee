"""Tests for the drive check: a real hard link, proven at the apps' own folders.

Every test that runs the probe for real works inside `tmp_path` - a real
kernel filesystem (APFS locally, ext4 on CI) - because the one thing that
matters here is whether an actual `os.link` on an actual folder tree
behaves the way the plain-language reasons say it does. Fault paths inject
a fake `link` (or `chown`) instead of trying to provoke a real EACCES or a
real full disk.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from marrquee import hardlinks, storage
from marrquee.config import Settings
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.state import InstallState, save_state

_NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path, config_dir=tmp_path / "config")


def _install(
    tmp_path: Path,
    *,
    storage_root: str | None = "/volume1/media",
    app_ids: tuple[str, ...] = ("sonarr", "qbittorrent"),
    puid: int = 1000,
    pgid: int = 1000,
) -> InstallState:
    del tmp_path  # kept for symmetry with the other fixtures; not needed here
    return InstallState(
        version=1,
        storage_root=storage_root,
        app_ids=app_ids,
        api_keys={},
        puid=puid,
        pgid=pgid,
        umask="002",
        timezone="Etc/UTC",
        created="2026-01-01T00:00:00+00:00",
    )


def _no_op_chown(path: Path, uid: int, gid: int) -> None:
    del path, uid, gid  # the test runner isn't root; a real chown would fail here


def _build_real_folders(
    tmp_path: Path, install: InstallState, root: str = "/volume1/media"
) -> None:
    settings = _settings(tmp_path)
    container_root = tmp_path / PurePosixPath(root).relative_to("/")
    container_root.mkdir(parents=True, exist_ok=True)
    storage.build_folders(
        settings,
        PurePosixPath(root),
        install.app_ids,
        install.puid,
        install.pgid,
        chown=_no_op_chown,
    )


def _never_link(source: Path, dest: Path) -> None:
    raise AssertionError(f"link should never be called (source={source}, dest={dest})")


def _torrents_test_file(tmp_path: Path, media_type: str, root: str = "volume1/media") -> Path:
    return tmp_path / root / "data" / "torrents" / media_type / hardlinks.LINK_TEST_FILE_NAME


def _media_test_file(tmp_path: Path, media_type: str, root: str = "volume1/media") -> Path:
    return tmp_path / root / "data" / "media" / media_type / hardlinks.LINK_TEST_FILE_NAME


# --- the real walk: works, and leaves nothing behind -------------------------


def test_a_real_drive_in_tmp_path_hard_links_and_leaves_nothing_behind(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW)

    assert result.outcome == "works"
    assert result.reason is None
    assert not _torrents_test_file(tmp_path, "tv").exists()
    assert not _media_test_file(tmp_path, "tv").exists()


def test_every_media_type_is_tested_and_the_first_failure_wins(tmp_path: Path) -> None:
    install = _install(tmp_path, app_ids=("sonarr", "radarr", "qbittorrent"))
    _build_real_folders(tmp_path, install)

    def fake_link(source: Path, dest: Path) -> None:
        if "movies" in str(dest):
            raise OSError(errno.EXDEV, "cross-device link")
        os.link(source, dest)

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=fake_link)

    assert result.outcome == "copies"
    assert result.reason == "different_drives"
    assert result.folder == "/volume1/media/data/media/movies"
    # the tv pair (tested first, and it worked) is cleaned up too
    assert not _torrents_test_file(tmp_path, "tv").exists()
    assert not _media_test_file(tmp_path, "tv").exists()
    assert not _torrents_test_file(tmp_path, "movies").exists()


# --- errno truth: each Linux link(2) failure maps to one plain reason --------


@pytest.mark.parametrize(
    ("raised_errno", "expected_outcome", "expected_reason"),
    [
        pytest.param(errno.EXDEV, "copies", "different_drives", id="EXDEV-different-drives"),
        pytest.param(errno.EPERM, "copies", "no_hard_links", id="EPERM-no-hard-links"),
        pytest.param(errno.EOPNOTSUPP, "copies", "no_hard_links", id="EOPNOTSUPP-no-hard-links"),
        pytest.param(errno.EMLINK, "copies", "no_hard_links", id="EMLINK-no-hard-links"),
        pytest.param(errno.EACCES, "couldnt_check", "not_allowed", id="EACCES-not-allowed"),
        pytest.param(errno.EROFS, "couldnt_check", "not_allowed", id="EROFS-not-allowed"),
        pytest.param(errno.ENOSPC, "couldnt_check", "drive_full", id="ENOSPC-drive-full"),
        pytest.param(errno.EDQUOT, "couldnt_check", "drive_full", id="EDQUOT-drive-full"),
    ],
)
def test_link_errno_is_mapped_to_a_plain_reason(
    tmp_path: Path, raised_errno: int, expected_outcome: str, expected_reason: str
) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    def failing_link(source: Path, dest: Path) -> None:
        raise OSError(raised_errno, os.strerror(raised_errno))

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=failing_link)

    assert result.outcome == expected_outcome
    assert result.reason == expected_reason
    assert not _torrents_test_file(tmp_path, "tv").exists()
    assert not _media_test_file(tmp_path, "tv").exists()


def test_a_link_that_isnt_really_a_link_is_caught(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    def copying_link(source: Path, dest: Path) -> None:
        dest.write_bytes(source.read_bytes())

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=copying_link)

    assert result.outcome == "copies"
    assert result.reason == "no_hard_links"
    assert not _torrents_test_file(tmp_path, "tv").exists()
    assert not _media_test_file(tmp_path, "tv").exists()


def test_an_unmapped_link_errno_falls_back_to_unexpected(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    def failing_link(source: Path, dest: Path) -> None:
        raise OSError(errno.EIO, "input/output error")

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=failing_link)

    assert result.outcome == "couldnt_check"
    assert result.reason == "unexpected"


def test_a_disappearing_link_is_reported_not_raised(tmp_path: Path) -> None:
    """A real link that vanishes before the follow-up `stat` (a race with
    something else on the drive) is still just a plain reason, not a crash.
    """
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    def vanishing_link(source: Path, dest: Path) -> None:
        os.link(source, dest)
        os.unlink(dest)

    result = hardlinks.run_hardlink_check(
        _settings(tmp_path), install, now=_NOW, link=vanishing_link
    )

    assert result.outcome == "couldnt_check"
    assert result.reason == "folder_missing"
    assert not _torrents_test_file(tmp_path, "tv").exists()


def test_a_read_only_torrents_folder_is_reported_as_not_allowed(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)
    torrents_dir = tmp_path / "volume1" / "media" / "data" / "torrents" / "tv"
    torrents_dir.chmod(0o555)

    try:
        result = hardlinks.run_hardlink_check(
            _settings(tmp_path), install, now=_NOW, link=_never_link
        )
    finally:
        torrents_dir.chmod(0o755)

    assert result.outcome == "couldnt_check"
    assert result.reason == "not_allowed"


def test_a_missing_media_folder_is_reported_nothing_created(tmp_path: Path) -> None:
    install = _install(tmp_path)
    torrents_dir = tmp_path / "volume1" / "media" / "data" / "torrents" / "tv"
    torrents_dir.mkdir(parents=True)

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=_never_link)

    assert result.outcome == "couldnt_check"
    assert result.reason == "folder_missing"
    assert list(torrents_dir.iterdir()) == []


def test_a_symlinked_media_folder_is_refused(tmp_path: Path) -> None:
    install = _install(tmp_path)
    torrents_dir = tmp_path / "volume1" / "media" / "data" / "torrents" / "tv"
    torrents_dir.mkdir(parents=True)
    media_dir = tmp_path / "volume1" / "media" / "data" / "media" / "tv"
    media_dir.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    media_dir.symlink_to(elsewhere)

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=_never_link)

    assert result.outcome == "couldnt_check"
    assert result.reason == "folder_elsewhere"


# --- not_needed never touches the drive --------------------------------------


def test_nothing_to_test_without_a_downloader(tmp_path: Path) -> None:
    install = _install(tmp_path, app_ids=("sonarr",))

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=_never_link)

    assert result.outcome == "not_needed"
    assert result.reason is None


def test_nothing_to_test_without_a_media_type(tmp_path: Path) -> None:
    install = _install(tmp_path, app_ids=("qbittorrent",))

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=_never_link)

    assert result.outcome == "not_needed"


def test_nothing_to_test_without_a_drive(tmp_path: Path) -> None:
    install = _install(tmp_path, storage_root=None)

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=_never_link)

    assert result.outcome == "not_needed"


def test_nothing_to_test_without_an_install(tmp_path: Path) -> None:
    result = hardlinks.run_hardlink_check(_settings(tmp_path), None, now=_NOW, link=_never_link)

    assert result.outcome == "not_needed"


# --- litter ------------------------------------------------------------------


def test_a_stale_test_file_from_a_crash_is_removed_first(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)
    stale = _torrents_test_file(tmp_path, "tv")
    stale.write_text("leftover from a crash")

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW)

    assert result.outcome == "works"
    assert not stale.exists()


def test_a_folder_with_the_test_files_name_blocks_the_check_honestly(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)
    blocker = _torrents_test_file(tmp_path, "tv")
    blocker.mkdir()

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=_never_link)

    assert result.outcome == "couldnt_check"
    assert result.reason == "unexpected"
    assert blocker.is_dir()


# --- ownership -----------------------------------------------------------


def test_the_test_file_is_chowned_to_the_installs_ids(tmp_path: Path) -> None:
    install = _install(tmp_path, puid=4242, pgid=4343)
    _build_real_folders(tmp_path, install)
    calls: list[tuple[Path, int, int]] = []

    def recording_chown(path: Path, uid: int, gid: int) -> None:
        calls.append((path, uid, gid))

    result = hardlinks.run_hardlink_check(
        _settings(tmp_path), install, now=_NOW, chown=recording_chown
    )

    assert result.outcome == "works"
    assert calls == [(_torrents_test_file(tmp_path, "tv"), 4242, 4343)]


def test_a_chown_failure_is_ignored(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    def failing_chown(path: Path, uid: int, gid: int) -> None:
        raise OSError(errno.EPERM, "not the owner")

    result = hardlinks.run_hardlink_check(
        _settings(tmp_path), install, now=_NOW, chown=failing_chown
    )

    assert result.outcome == "works"


# --- the folder the owner sees is always their own host path -----------------


def test_the_result_never_carries_a_container_path(tmp_path: Path) -> None:
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)

    def failing_link(source: Path, dest: Path) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    result = hardlinks.run_hardlink_check(_settings(tmp_path), install, now=_NOW, link=failing_link)

    assert result.folder is not None
    assert result.folder == "/volume1/media/data/media/tv"
    assert str(tmp_path) not in result.folder
    assert "/host" not in result.folder


# --- the deletion fence: hardlinks.py gets its own copy of storage.py's proof --


_BANNED_CALL_ATTRIBUTES = {"remove", "unlink", "rmdir", "rmtree", "move", "rename", "replace"}


def _banned_calls_by_function(tree: ast.AST) -> dict[str, set[str]]:
    """Every banned name called by each top-level function, keyed by its name."""
    banned_by_function: dict[str, set[str]] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        names: set[str] = set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            target = call.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
        banned_by_function[node.name] = names & _BANNED_CALL_ATTRIBUTES
    return banned_by_function


def test_deletion_calls_live_only_in_remove_own_test_file() -> None:
    source = Path(hardlinks.__file__).read_text()
    tree = ast.parse(source)

    banned_by_function = _banned_calls_by_function(tree)

    for name, banned in banned_by_function.items():
        if name == "_remove_own_test_file":
            continue
        assert banned == set(), (
            f"{name} must never delete, move or rename anything - found {banned}"
        )

    assert banned_by_function["_remove_own_test_file"] == {"unlink"}


def test_hardlinks_never_calls_replace_anywhere() -> None:
    """The deletion fence flags bare-name AND attribute calls, so a
    `.replace(...)` used only for string or path formatting would still
    trip it. Nothing in this module may call `replace` at all.
    """
    source = Path(hardlinks.__file__).read_text()
    tree = ast.parse(source)

    all_calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            all_calls.add(target.attr)
        elif isinstance(target, ast.Name):
            all_calls.add(target.id)

    assert "replace" not in all_calls


def test_storage_py_deletion_fence_still_holds() -> None:
    source = Path(storage.__file__).read_text()
    tree = ast.parse(source)

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)

    assert names & _BANNED_CALL_ATTRIBUTES == set()


def test_remove_own_test_file_refuses_any_other_name(tmp_path: Path) -> None:
    victim = tmp_path / "keep.mkv"
    victim.write_text("do not touch")

    removed = hardlinks._remove_own_test_file(victim)

    assert removed is False
    assert victim.exists()


def test_remove_own_test_file_refuses_a_directory(tmp_path: Path) -> None:
    victim = tmp_path / hardlinks.LINK_TEST_FILE_NAME
    victim.mkdir()

    removed = hardlinks._remove_own_test_file(victim)

    assert removed is False
    assert victim.is_dir()


def test_remove_own_test_file_treats_missing_as_already_gone(tmp_path: Path) -> None:
    missing = tmp_path / hardlinks.LINK_TEST_FILE_NAME

    assert hardlinks._remove_own_test_file(missing) is True


# --- save / load / is_due -----------------------------------------------------


def test_the_saved_file_round_trips(tmp_path: Path) -> None:
    result = hardlinks.HardlinkResult(
        outcome="copies",
        reason="different_drives",
        folder="/volume1/media/data/media/tv",
        technical="Invalid cross-device link (errno 18)",
        checked_at="2026-09-26T12:00:00+00:00",
    )

    hardlinks.save_hardlink_result(tmp_path, result)
    loaded = hardlinks.load_hardlink_result(tmp_path)

    assert loaded == result


def test_the_saved_file_is_root_only(tmp_path: Path) -> None:
    result = hardlinks.HardlinkResult(
        outcome="works",
        reason=None,
        folder=None,
        technical=None,
        checked_at="2026-09-26T12:00:00+00:00",
    )

    hardlinks.save_hardlink_result(tmp_path, result)

    mode = (tmp_path / hardlinks.HARDLINK_FILE_NAME).stat().st_mode
    assert mode & 0o777 == 0o600


def test_a_missing_file_loads_as_none(tmp_path: Path) -> None:
    assert hardlinks.load_hardlink_result(tmp_path) is None


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("", id="empty"),
        pytest.param(json.dumps({"version": 2, "outcome": "works"}), id="wrong-version"),
        pytest.param(
            json.dumps(
                {
                    "version": 1,
                    "outcome": "sideways",
                    "reason": None,
                    "folder": None,
                    "technical": None,
                    "checked_at": "x",
                }
            ),
            id="unknown-outcome",
        ),
        pytest.param(
            json.dumps(
                {
                    "version": 1,
                    "outcome": "works",
                    "reason": "different_drives",
                    "folder": None,
                    "technical": None,
                    "checked_at": "x",
                }
            ),
            id="broken-invariant-works-with-a-reason",
        ),
        pytest.param(
            json.dumps(
                {
                    "version": 1,
                    "outcome": "copies",
                    "reason": None,
                    "folder": None,
                    "technical": None,
                    "checked_at": "x",
                }
            ),
            id="broken-invariant-copies-with-no-reason",
        ),
    ],
)
def test_a_broken_file_loads_as_none(tmp_path: Path, broken: str) -> None:
    (tmp_path / hardlinks.HARDLINK_FILE_NAME).write_text(broken)

    assert hardlinks.load_hardlink_result(tmp_path) is None


def test_is_due_at_23h59_is_false_at_24h_is_true_and_none_is_always_true() -> None:
    checked_at = datetime(2026, 9, 26, 0, 0, 0, tzinfo=UTC)
    saved = hardlinks.HardlinkResult(
        outcome="works", reason=None, folder=None, technical=None, checked_at=checked_at.isoformat()
    )

    assert hardlinks.is_due(saved, checked_at + timedelta(hours=23, minutes=59)) is False
    assert hardlinks.is_due(saved, checked_at + timedelta(hours=24)) is True
    assert hardlinks.is_due(None, checked_at) is True


def test_is_due_is_true_when_checked_at_does_not_parse() -> None:
    saved = hardlinks.HardlinkResult(
        outcome="works", reason=None, folder=None, technical=None, checked_at="not-a-timestamp"
    )

    assert hardlinks.is_due(saved, _NOW) is True


# --- HardlinkMonitor: one owner of "check now", "after every deploy/add" and --
# --- "once a day" --------------------------------------------------------------


def _saved_result(
    checked_at: datetime, outcome: hardlinks.HardlinkOutcome = "works"
) -> hardlinks.HardlinkResult:
    return hardlinks.HardlinkResult(
        outcome=outcome, reason=None, folder=None, technical=None, checked_at=checked_at.isoformat()
    )


def _blocking_link(release: threading.Event, calls: list[tuple[Path, Path]]) -> hardlinks.LinkFn:
    """A `link` fake that records every call, then blocks the CALLING
    THREAD (never the event loop - it only ever runs inside
    `asyncio.to_thread`) until `release` is set, then performs a real link.
    """

    def link(source: Path, dest: Path) -> None:
        calls.append((source, dest))
        if not release.wait(timeout=5):
            raise AssertionError("release was never set - the test itself is broken")
        os.link(source, dest)

    return link


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Yield to the event loop until `predicate()` is true - the task
    a blocking `link` fake runs in only reaches its first recorded call
    once the loop has actually handed control to the executor thread.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(0.001)


async def test_refresh_if_due_starts_one_check_for_a_day_old_result_and_none_while_running(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)
    save_state(settings.config_dir, install)
    hardlinks.save_hardlink_result(settings.config_dir, _saved_result(_NOW - timedelta(hours=25)))

    release = threading.Event()
    calls: list[tuple[Path, Path]] = []
    monitor = hardlinks.HardlinkMonitor(
        settings, link=_blocking_link(release, calls), clock=lambda: _NOW
    )

    monitor.refresh_if_due()
    task = monitor._task  # type: ignore[attr-defined]
    assert task is not None
    await _wait_until(lambda: len(calls) == 1)

    # A second call while the first check is still running starts none -
    # the very same task is still the one in flight.
    monitor.refresh_if_due()
    assert monitor._task is task  # type: ignore[attr-defined]
    assert len(calls) == 1

    release.set()
    result = await task
    assert result.outcome == "works"


async def test_refresh_if_due_starts_none_for_a_fresh_result(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    install = _install(tmp_path)
    save_state(settings.config_dir, install)
    hardlinks.save_hardlink_result(settings.config_dir, _saved_result(_NOW - timedelta(hours=1)))

    monitor = hardlinks.HardlinkMonitor(settings, link=_never_link, clock=lambda: _NOW)

    monitor.refresh_if_due()

    assert monitor._task is None  # type: ignore[attr-defined]


async def test_refresh_if_due_treats_a_missing_result_as_due(tmp_path: Path) -> None:
    """The upgrade path: a process that has never saved a result before runs
    one on the very first look, exactly like `is_due(None, ...)`.
    """
    settings = _settings(tmp_path)
    install = _install(tmp_path, app_ids=("sonarr",))  # not_needed - fast, no drive touched
    save_state(settings.config_dir, install)

    monitor = hardlinks.HardlinkMonitor(settings, link=_never_link, clock=lambda: _NOW)

    monitor.refresh_if_due()
    task = monitor._task  # type: ignore[attr-defined]
    assert task is not None
    result = await task
    assert result.outcome == "not_needed"


async def test_two_request_check_calls_while_one_runs_share_one_task(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)
    save_state(settings.config_dir, install)

    release = threading.Event()
    calls: list[tuple[Path, Path]] = []
    monitor = hardlinks.HardlinkMonitor(
        settings, link=_blocking_link(release, calls), clock=lambda: _NOW
    )

    first = monitor.request_check()
    second = monitor.request_check()
    assert first is second
    await _wait_until(lambda: len(calls) == 1)
    assert len(calls) == 1  # the "tv" pair - the only pair this install plans

    release.set()
    result = await first
    assert result.outcome == "works"


async def test_check_now_returns_none_past_its_wait_and_the_result_is_still_saved_afterwards(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    install = _install(tmp_path)
    _build_real_folders(tmp_path, install)
    save_state(settings.config_dir, install)

    release = threading.Event()
    calls: list[tuple[Path, Path]] = []
    monitor = hardlinks.HardlinkMonitor(
        settings, link=_blocking_link(release, calls), clock=lambda: _NOW
    )

    result = await monitor.check_now(wait_seconds=0.01)

    assert result is None
    assert hardlinks.load_hardlink_result(settings.config_dir) is None  # not saved yet

    release.set()
    task = monitor._task  # type: ignore[attr-defined]
    assert task is not None
    await task

    saved = hardlinks.load_hardlink_result(settings.config_dir)
    assert saved is not None
    assert saved.outcome == "works"


async def test_a_save_failure_is_logged_and_the_result_is_still_returned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_blocker = tmp_path / "config-is-a-file"
    config_blocker.write_text("not a directory - a save here must fail")
    settings = Settings(host_mount=tmp_path / "host", config_dir=config_blocker)
    # No install can ever be loaded from a config_dir that is a plain file,
    # so this is `not_needed` - fast, and never touches a drive - the point
    # of this test is the SAVE failure, not the probe itself.
    monitor = hardlinks.HardlinkMonitor(settings, link=_never_link, clock=lambda: _NOW)

    with caplog.at_level(logging.WARNING):
        result = await monitor.request_check()

    assert result.outcome == "not_needed"
    assert any("save" in record.message.lower() for record in caplog.records)


def test_create_app_puts_one_monitor_on_app_state_and_hands_it_to_the_manager_it_builds(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")

    app = create_app(settings=settings, engine=FakeDockerEngine(DockerStatus(connected=True)))

    assert isinstance(app.state.hardlinks, hardlinks.HardlinkMonitor)
    assert app.state.deploy._hardlinks is app.state.hardlinks  # type: ignore[attr-defined]


async def _call_request_check(
    monitor: hardlinks.HardlinkMonitor,
) -> asyncio.Task[hardlinks.HardlinkResult]:
    return monitor.request_check()


def test_a_task_from_a_dead_loop_is_never_joined_as_in_flight(tmp_path: Path) -> None:
    """A bare `TestClient` hands each request its own event loop
    (.claude/rules/testclient-context-manager.md) - a task left running on
    an earlier, now-abandoned loop must never be "joined" by a later
    request, or it would wait forever for a loop nothing is iterating any
    more.
    """
    settings = _settings(tmp_path)
    # not_needed - the task finishes fast once its loop actually runs it.
    install = _install(tmp_path, app_ids=("sonarr",))
    save_state(settings.config_dir, install)
    monitor = hardlinks.HardlinkMonitor(settings, link=_never_link, clock=lambda: _NOW)

    dead_loop = asyncio.new_event_loop()
    orphaned_task = dead_loop.run_until_complete(_call_request_check(monitor))
    # `dead_loop` is abandoned right here, exactly like a bare `TestClient`'s
    # own per-request loop - `orphaned_task` never gets another chance to run.
    assert monitor._task is orphaned_task  # type: ignore[attr-defined]
    assert not orphaned_task.done()

    fresh_loop = asyncio.new_event_loop()
    try:
        new_task = fresh_loop.run_until_complete(_call_request_check(monitor))
        assert new_task is not orphaned_task

        result = fresh_loop.run_until_complete(new_task)
        assert result.outcome == "not_needed"
    finally:
        fresh_loop.close()
        dead_loop.close()
