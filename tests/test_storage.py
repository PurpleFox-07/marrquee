"""Tests for the storage module: the only code allowed to touch the owner's drive.

The owner gave Marrquee a read-write view of their NAS's shared-folder
root(s) (for example `/volume1` mounted at `/host/volume1`), so this
module's safety properties are the only thing standing between a bug
here and the owner's data. These tests lean hard on that: every refusal
path is proven to write nothing, folder creation is proven to touch only
what it created, and the module's own source is scanned to prove it cannot
delete, move or rename anything.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path, PurePosixPath

import pytest

from marrquee import storage, words
from marrquee.config import Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_PATH = _REPO_ROOT / "compose.install.yaml"
_README_PATH = _REPO_ROOT / "README.md"


# --- to_host_view / from_host_view: the one place path translation happens --


def test_to_host_view_translates_an_absolute_host_path_under_the_mount() -> None:
    settings = Settings(host_mount=Path("/host"))

    assert storage.to_host_view(settings, "/volume1/media") == Path("/host/volume1/media")


def test_to_host_view_collapses_a_trailing_slash() -> None:
    settings = Settings(host_mount=Path("/host"))

    assert storage.to_host_view(settings, "/volume1/media/") == Path("/host/volume1/media")


def test_to_host_view_of_bare_root_is_the_mount_itself() -> None:
    settings = Settings(host_mount=Path("/host"))

    assert storage.to_host_view(settings, "/") == Path("/host")


def test_to_host_view_rejects_relative_input() -> None:
    settings = Settings(host_mount=Path("/host"))

    with pytest.raises(ValueError, match="volume1/media"):
        storage.to_host_view(settings, "volume1/media")


def test_to_host_view_rejects_empty_input() -> None:
    settings = Settings(host_mount=Path("/host"))

    with pytest.raises(ValueError):
        storage.to_host_view(settings, "")


def test_to_host_view_defeats_a_path_that_tries_to_escape_the_mount() -> None:
    settings = Settings(host_mount=Path("/host"))

    escaping = storage.to_host_view(settings, "/../../etc/passwd")

    # Naively joining "../../etc/passwd" onto the mount would let the OS walk
    # back out of /host entirely. The leading ".." has to be collapsed before
    # the join, not after.
    assert escaping == Path("/host/etc/passwd")


def test_from_host_view_is_the_inverse_of_to_host_view() -> None:
    settings = Settings(host_mount=Path("/host"))
    container_path = storage.to_host_view(settings, "/volume1/media")

    assert storage.from_host_view(settings, container_path) == PurePosixPath("/volume1/media")


def test_from_host_view_of_the_mount_itself_is_the_host_root() -> None:
    settings = Settings(host_mount=Path("/host"))

    assert storage.from_host_view(settings, Path("/host")) == PurePosixPath("/")


# --- check_storage_root: the wizard's live, never-raising path check --------


@pytest.mark.parametrize(
    ("typed", "expected_reason"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("volume1/media", "not_absolute"),
        ("/etc", "system_path"),
    ],
)
def test_an_empty_relative_or_system_path_is_refused_with_its_own_reason(
    tmp_path: Path, typed: str, expected_reason: str
) -> None:
    settings = Settings(host_mount=tmp_path)

    result = storage.check_storage_root(settings, typed)

    assert result.ok is False
    assert result.reason == expected_reason
    assert list(tmp_path.iterdir()) == []


def test_bare_root_is_refused_as_a_system_path() -> None:
    settings = Settings(host_mount=Path("/host"))

    result = storage.check_storage_root(settings, "/")

    assert result.ok is False
    assert result.reason == "system_path"


def test_a_missing_path_suggests_the_closest_siblings(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "vol1" / "media").mkdir(parents=True)
    (tmp_path / "vol1" / "music").mkdir()

    result = storage.check_storage_root(settings, "/vol1/mdia")

    assert result.ok is False
    assert result.reason == "missing"
    assert "media" in result.suggestions


def test_a_pathologically_long_segment_is_refused_not_raised(tmp_path: Path) -> None:
    """A single path segment long enough that the OS itself refuses to stat
    it (`OSError: File name too long`) must come back as an ordinary
    refusal - the checker's whole contract is that it never raises, and a
    typed path is exactly the kind of input the wizard cannot pre-validate
    the length of before asking the filesystem.
    """
    settings = Settings(host_mount=tmp_path)

    result = storage.check_storage_root(settings, "/" + "a" * 4000)

    assert result.ok is False
    assert result.reason in ("missing", "not_shared")
    assert result.suggestions == ()
    assert result.suggested_path is None


# --- not_shared: a folder Marrquee simply cannot see, not a typo ------------


def test_a_path_whose_first_folder_isnt_mounted_is_not_shared_not_missing(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1").mkdir()

    result = storage.check_storage_root(settings, "/mnt/storage")

    assert result.ok is False
    assert result.reason == "not_shared"


def test_a_second_pool_that_isnt_mounted_is_not_shared_and_suggests_the_existing_twin(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    result = storage.check_storage_root(settings, "/volume2/media")

    assert result.ok is False
    assert result.reason == "not_shared"
    assert result.suggested_path == PurePosixPath("/volume1/media")


def test_a_typo_inside_a_mounted_folder_stays_missing_and_suggests_one_full_path(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "movies").mkdir(parents=True)

    result = storage.check_storage_root(settings, "/volume1/movis")

    assert result.ok is False
    assert result.reason == "missing"
    assert result.suggested_path == PurePosixPath("/volume1/movies")


def test_suggested_path_falls_back_to_the_matching_folder_when_the_swap_isnt_a_folder(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "movies").mkdir(parents=True)

    result = storage.check_storage_root(settings, "/volume1/movis/subfolder")

    assert result.ok is False
    assert result.reason == "missing"
    # /volume1/movies/subfolder doesn't exist, so the suggestion falls back
    # to the existing folder the name was matched against.
    assert result.suggested_path == PurePosixPath("/volume1/movies")


def test_suggested_path_is_none_when_nothing_is_close(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1").mkdir()

    result = storage.check_storage_root(settings, "/mnt/storage")

    assert result.suggestions == ()
    assert result.suggested_path is None


def test_a_typo_in_the_existing_test_stays_missing(tmp_path: Path) -> None:
    """`/vol1/mdia` has `vol1` mounted, so it stays a spelling problem, not a visibility one."""
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "vol1" / "media").mkdir(parents=True)

    result = storage.check_storage_root(settings, "/vol1/mdia")

    assert result.reason == "missing"


# --- shared_roots: what the field hint tells the owner Marrquee can see ----


def test_shared_roots_lists_mounted_top_level_folders_and_skips_system_names(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1").mkdir()
    (tmp_path / "etc").mkdir()

    assert storage.shared_roots(settings) == (PurePosixPath("/volume1"),)


def test_shared_roots_is_empty_not_an_error_when_the_mount_is_missing(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path / "does-not-exist")

    assert storage.shared_roots(settings) == ()


def test_shared_roots_skips_files_and_sorts_the_result(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume2").mkdir()
    (tmp_path / "volume1").mkdir()
    (tmp_path / "notes.txt").write_text("not a folder")

    assert storage.shared_roots(settings) == (
        PurePosixPath("/volume1"),
        PurePosixPath("/volume2"),
    )


def test_a_file_typed_as_the_root_is_refused_as_not_a_folder(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1").mkdir()
    (tmp_path / "volume1" / "media.txt").write_text("hi")

    result = storage.check_storage_root(settings, "/volume1/media.txt")

    assert result.ok is False
    assert result.reason == "not_a_folder"


def test_writability_is_probed_not_assumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    def _raise_permission_error(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated: running as an over-privileged user")

    monkeypatch.setattr(storage.tempfile, "NamedTemporaryFile", _raise_permission_error)

    result = storage.check_storage_root(settings, "/volume1/media")

    assert result.ok is False
    assert result.reason == "not_writable"
    assert result.writable is False


def test_free_space_comes_from_the_filesystem_never_computed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    class _FakeStatvfs:
        f_bavail = 12345
        f_frsize = 4096
        f_blocks = 999999

    monkeypatch.setattr(storage.os, "statvfs", lambda path: _FakeStatvfs())

    result = storage.check_storage_root(settings, "/volume1/media")

    assert result.ok is True
    assert result.free_bytes == 12345 * 4096
    assert result.total_bytes == 999999 * 4096


def test_a_valid_writable_folder_is_accepted(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    result = storage.check_storage_root(settings, "/volume1/media")

    assert result.ok is True
    assert result.reason is None
    assert result.host_path == PurePosixPath("/volume1/media")
    assert result.exists is True
    assert result.is_dir is True
    assert result.writable is True
    assert result.free_bytes is not None
    assert result.total_bytes is not None
    assert result.detail is None


def test_check_storage_root_refuses_a_root_that_is_a_symlink_to_a_system_path(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "volume1").symlink_to(tmp_path / "etc")

    result = storage.check_storage_root(settings, "/volume1")

    assert result.ok is False
    assert result.reason == "system_path"


# --- plan_folders: the pure, TRaSH-shaped folder tree -----------------------


def test_the_planned_tree_matches_trashs_shape_for_two_chosen_apps() -> None:
    assert storage.plan_folders(("sonarr", "radarr")) == (
        PurePosixPath("data/torrents/tv"),
        PurePosixPath("data/torrents/movies"),
        PurePosixPath("data/media/tv"),
        PurePosixPath("data/media/movies"),
        PurePosixPath("marrquee"),
        PurePosixPath("marrquee/apps/sonarr"),
        PurePosixPath("marrquee/apps/radarr"),
    )


def test_the_planned_tree_matches_trashs_shape_for_one_chosen_app() -> None:
    assert storage.plan_folders(("radarr",)) == (
        PurePosixPath("data/torrents/movies"),
        PurePosixPath("data/media/movies"),
        PurePosixPath("marrquee"),
        PurePosixPath("marrquee/apps/radarr"),
    )


def test_prowlarr_alone_plans_no_data_folders() -> None:
    assert storage.plan_folders(("prowlarr",)) == (
        PurePosixPath("marrquee"),
        PurePosixPath("marrquee/apps/prowlarr"),
    )


# --- check_fresh_start: the owner's hardest rule ----------------------------


def test_a_populated_library_is_refused_and_nothing_is_created(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    movies_dir = container_root / "data" / "media" / "movies"
    movies_dir.mkdir(parents=True)
    (movies_dir / "Old Film.mkv").write_text("")

    check = storage.check_fresh_start(settings, root, ("radarr",))

    assert check.ok is False
    assert check.reason == "already_has_files"
    assert check.occupied == ("data/media/movies",)
    assert not (container_root / "marrquee").exists()


def test_an_empty_target_with_no_marker_is_a_fresh_start(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    check = storage.check_fresh_start(settings, root, ("radarr",))

    assert check.ok is True
    assert check.occupied == ()


def test_our_own_marker_makes_a_rerun_acceptable_even_with_files_present(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    movies_dir = container_root / "data" / "media" / "movies"
    movies_dir.mkdir(parents=True)
    (movies_dir / "Old Film.mkv").write_text("")
    (container_root / "marrquee").mkdir()
    storage.write_marker(settings, root, ("radarr",), 1000, 1000, chown=lambda *_: None)

    check = storage.check_fresh_start(settings, root, ("radarr",))

    assert check.ok is True


def test_torrents_folders_do_not_count_toward_occupancy(tmp_path: Path) -> None:
    """Only the media half of the tree is the owner's real library.

    data/torrents is built ahead of the downloader that will use it, so it
    starting non-empty (e.g. leftover partial downloads from a previous
    tool) must never block a fresh start.
    """
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    torrents_dir = container_root / "data" / "torrents" / "movies"
    torrents_dir.mkdir(parents=True)
    (torrents_dir / "leftover.part").write_text("")

    check = storage.check_fresh_start(settings, root, ("radarr",))

    assert check.ok is True


def test_check_fresh_start_refuses_when_a_planned_media_folder_escapes_the_root(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media" / "data" / "media"
    container_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (container_root / "movies").symlink_to(outside)

    check = storage.check_fresh_start(settings, root, ("radarr",))

    assert check.ok is False
    assert check.reason == "already_has_files"
    assert check.occupied == ("data/media/movies",)


def test_check_fresh_start_refuses_when_its_own_folder_escapes_the_root(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    container_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (container_root / "marrquee").symlink_to(outside)

    check = storage.check_fresh_start(settings, root, ("radarr",))

    assert check.ok is False
    assert check.reason == "already_has_files"


# --- write_marker ------------------------------------------------------------


def test_write_marker_persists_created_version_app_ids_and_what_is_this(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)

    marker_path = storage.write_marker(
        settings, root, ("radarr", "sonarr"), 1000, 1000, chown=lambda *_: None
    )

    payload = json.loads(marker_path.read_text())
    assert payload["app_ids"] == ["sonarr", "radarr"]  # catalog order, not input order
    assert payload["what_is_this"] == words.MARKER_WHAT_IS_THIS
    assert payload["version"] == 1
    assert "created" in payload


def test_write_marker_chowns_the_file_it_wrote_to_puid_and_pgid(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)

    calls: list[tuple[Path, int, int]] = []
    marker_path = storage.write_marker(
        settings,
        root,
        ("radarr",),
        4242,
        4343,
        chown=lambda path, uid, gid: calls.append((path, uid, gid)),
    )

    assert calls == [(marker_path, 4242, 4343)]


def test_write_marker_swallows_a_chown_failure_instead_of_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)

    def _raising_chown(path: Path, uid: int, gid: int) -> None:
        raise PermissionError("this NAS share does not support chown")

    with caplog.at_level(logging.WARNING):
        marker_path = storage.write_marker(
            settings, root, ("radarr",), 1000, 1000, chown=_raising_chown
        )

    assert marker_path.is_file()  # the marker is still written and valid
    assert "chown" in caplog.text.lower()


# --- read_marker: telling "we made this container" from "someone else did" --


def test_read_marker_returns_none_when_no_marker_has_ever_been_written(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    assert storage.read_marker(settings, root) is None


def test_read_marker_returns_the_app_ids_write_marker_recorded(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)
    storage.write_marker(settings, root, ("radarr", "sonarr"), 1000, 1000, chown=lambda *_: None)

    marker = storage.read_marker(settings, root)

    assert marker is not None
    assert marker.app_ids == ("sonarr", "radarr")  # catalog order, not input order
    assert marker.created  # a non-empty ISO timestamp string


def test_read_marker_returns_none_for_a_corrupt_marker_file(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    marker_dir = tmp_path / "volume1" / "media" / "marrquee"
    marker_dir.mkdir(parents=True)
    (marker_dir / storage.MARKER_NAME).write_text("not json")

    assert storage.read_marker(settings, root) is None


# --- build_folders: create-and-chown-only, nothing pre-existing touched ----


def test_only_folders_we_created_are_chowned(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    container_root.mkdir(parents=True)
    (container_root / "data").mkdir()

    recorded_chown: list[tuple[Path, int, int]] = []
    recorded_chmod: list[tuple[Path, int]] = []

    report = storage.build_folders(
        settings,
        root,
        ("radarr",),
        puid=1000,
        pgid=1000,
        chown=lambda path, uid, gid: recorded_chown.append((path, uid, gid)),
        chmod=lambda path, mode: recorded_chmod.append((path, mode)),
    )

    chowned_paths = {path for path, _, _ in recorded_chown}
    assert chowned_paths == set(report.created)
    assert container_root not in chowned_paths
    assert (container_root / "data") not in chowned_paths
    assert all(uid == 1000 and gid == 1000 for _, uid, gid in recorded_chown)
    assert {path for path, _ in recorded_chmod} == chowned_paths
    assert all(mode == 0o775 for _, mode in recorded_chmod)


def _no_op_chown(path: Path, uid: int, gid: int) -> None:
    """A stand-in for os.chown: tests don't run as root, so a real chown to
    an arbitrary uid/gid would fail with PermissionError regardless of
    whether build_folders picked the right targets.
    """


def _no_op_chmod(path: Path, mode: int) -> None:
    pass


def test_build_folders_creates_exactly_the_planned_tree(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    container_root.mkdir(parents=True)

    storage.build_folders(
        settings,
        root,
        ("radarr",),
        puid=1000,
        pgid=1000,
        chown=_no_op_chown,
        chmod=_no_op_chmod,
    )

    for relative in storage.plan_folders(("radarr",)):
        assert (container_root / relative).is_dir()


def test_build_folders_is_safe_to_run_twice(tmp_path: Path) -> None:
    """A re-run (marker already present) must not choke on folders it made last time."""
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    container_root.mkdir(parents=True)

    first_report = storage.build_folders(
        settings, root, ("radarr",), puid=1000, pgid=1000, chown=_no_op_chown, chmod=_no_op_chmod
    )
    second_report = storage.build_folders(
        settings, root, ("radarr",), puid=1000, pgid=1000, chown=_no_op_chown, chmod=_no_op_chmod
    )

    assert first_report.created
    assert second_report.created == ()


def test_build_folders_refuses_when_a_planned_folder_is_a_symlink_escaping_the_root(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    container_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (container_root / "data").symlink_to(outside)

    with pytest.raises(storage.PathEscapesRoot):
        storage.build_folders(settings, root, ("radarr",), puid=1000, pgid=1000)


# --- derive_ids ---------------------------------------------------------------


def test_derive_ids_reads_the_roots_actual_owner_when_not_root(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    container_root = tmp_path / "volume1" / "media"
    container_root.mkdir(parents=True)

    ids = storage.derive_ids(settings, root)

    stat_result = container_root.stat()
    assert ids.puid == stat_result.st_uid
    assert ids.pgid == stat_result.st_gid
    assert ids.umask == "002"


def test_derive_ids_falls_back_to_1000_when_the_root_is_owned_by_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    class _RootOwnedStat:
        st_uid = 0
        st_gid = 0

    monkeypatch.setattr(storage.os, "stat", lambda path: _RootOwnedStat())

    ids = storage.derive_ids(settings, root)

    assert ids.puid == 1000
    assert ids.pgid == 1000


def test_derive_ids_reads_the_hosts_timezone_file_when_present(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media").mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "timezone").write_text("Europe/London\n")

    ids = storage.derive_ids(settings, root)

    assert ids.timezone == "Europe/London"


def test_derive_ids_falls_back_to_utc_when_no_timezone_file_exists(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media").mkdir(parents=True)

    ids = storage.derive_ids(settings, root)

    assert ids.timezone == "Etc/UTC"


# --- host_timezone --------------------------------------------------------------


def test_host_timezone_reads_and_trims_the_hosts_timezone_file(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "timezone").write_text("America/Chicago\n")

    assert storage.host_timezone(settings) == "America/Chicago"


def test_host_timezone_falls_back_to_utc_when_the_file_is_missing(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)

    assert storage.host_timezone(settings) == "Etc/UTC"


def test_host_timezone_falls_back_to_utc_when_the_file_is_empty(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "timezone").write_text("   \n")

    assert storage.host_timezone(settings) == "Etc/UTC"


def test_derive_ids_returns_exactly_what_host_timezone_returns(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media").mkdir(parents=True)
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "timezone").write_text("Europe/London\n")

    ids = storage.derive_ids(settings, root)

    assert ids.timezone == storage.host_timezone(settings)


# --- storage.py cannot delete anything ---------------------------------------

_BANNED_CALL_ATTRIBUTES = {"remove", "unlink", "rmdir", "rmtree", "move", "rename", "replace"}


def _called_names(tree: ast.AST) -> set[str]:
    """Every function/method name this module's source actually calls."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def test_storage_py_has_no_deletion_calls() -> None:
    source = Path(storage.__file__).read_text()
    tree = ast.parse(source)

    offenders = _called_names(tree) & _BANNED_CALL_ATTRIBUTES

    assert offenders == set(), (
        f"storage.py must never delete, move or rename anything - found {offenders}"
    )


# --- install artefacts carry the mount this module depends on ---------------


def test_the_install_file_and_readme_both_carry_the_host_mount() -> None:
    assert "- /volume1:/host/volume1" in _COMPOSE_PATH.read_text()
    assert "-v /volume1:/host/volume1" in _README_PATH.read_text()
