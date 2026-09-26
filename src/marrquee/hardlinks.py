"""Proves the owner's drive can hard-link a finished download into place.

Sonarr and Radarr move a finished download into the library with a hard
link when the filesystem allows it: two directory entries pointing at the
same data, no extra space used, no wait. This module runs that very same
operation for real, at the apps' own folders, so a "not enough space"
surprise a month from now can instead be a plain sentence today.

This stays a separate module from `storage.py` on purpose: `storage.py`'s
own promise ("only ever `mkdir`s and `chown`s, never deletes") is proven by
scanning storage.py's own source for a delete call, and this check is the
one legitimate reason Marrquee ever creates *and removes* a file.
`_remove_own_test_file` is the only function here allowed to delete
anything, and it refuses any name but its own test file - the same fence,
with the same proof, in its own file.

Every probe here catches `OSError`: a sleeping NAS disk, a permission
refusal, a full drive or a missing folder are all things a real deploy can
hit, and none of them may ever turn into a stack trace on the page that is
supposed to explain them in plain words.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat as stat_module
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final, Literal, Protocol, cast

from marrquee import storage, words
from marrquee.catalog import apps_in_order
from marrquee.config import Settings
from marrquee.state import InstallState, load_state, write_json_atomic

logger = logging.getLogger(__name__)

LINK_TEST_FILE_NAME: Final = ".marrquee-link-test"
HARDLINK_FILE_NAME: Final = "hardlink-check.json"

_RESULT_VERSION = 1

HardlinkOutcome = Literal["works", "copies", "couldnt_check", "not_needed"]
HardlinkReason = Literal[
    "different_drives",
    "no_hard_links",
    "not_allowed",
    "drive_full",
    "folder_missing",
    "folder_elsewhere",
    "unexpected",
]

_OUTCOMES: frozenset[str] = frozenset({"works", "copies", "couldnt_check", "not_needed"})
_REASONS: frozenset[str] = frozenset(
    {
        "different_drives",
        "no_hard_links",
        "not_allowed",
        "drive_full",
        "folder_missing",
        "folder_elsewhere",
        "unexpected",
    }
)
_NO_REASON_OUTCOMES: frozenset[str] = frozenset({"works", "not_needed"})


@dataclass(frozen=True)
class HardlinkResult:
    """One drive-check's verdict.

    `folder` is always the owner's own HOST path for the media folder
    involved (mirrors `storage.host_media_path`'s shape), never Marrquee's
    own `/host/...` view - the owner has never seen that path and never
    should. `reason` is set exactly when `outcome` is neither `works` nor
    `not_needed`.
    """

    outcome: HardlinkOutcome
    reason: HardlinkReason | None
    folder: str | None
    technical: str | None
    checked_at: str


LinkFn = Callable[[Path, Path], None]


# --- errno truth (Linux `link(2)`/`open(2)`): the code IS the decision -------

_CREATE_ERRNO_REASONS: dict[int | None, HardlinkReason] = {
    errno.EACCES: "not_allowed",
    errno.EROFS: "not_allowed",
    errno.ENOSPC: "drive_full",
    errno.EDQUOT: "drive_full",
    errno.ENOENT: "folder_missing",
    errno.ENOTDIR: "folder_missing",
}

_LINK_ERRNO_OUTCOMES: dict[int | None, tuple[HardlinkOutcome, HardlinkReason]] = {
    errno.EXDEV: ("copies", "different_drives"),
    errno.EPERM: ("copies", "no_hard_links"),
    errno.EOPNOTSUPP: ("copies", "no_hard_links"),
    errno.ENOTSUP: ("copies", "no_hard_links"),
    errno.ENOSYS: ("copies", "no_hard_links"),
    errno.EMLINK: ("copies", "no_hard_links"),
}


def _map_create_error(error: OSError) -> tuple[HardlinkOutcome, HardlinkReason]:
    """What a failure to create, write or stat the test file means."""
    return "couldnt_check", _CREATE_ERRNO_REASONS.get(error.errno, "unexpected")


def _map_link_error(error: OSError) -> tuple[HardlinkOutcome, HardlinkReason]:
    """What a failed `link(2)` means - only EXDEV and "no hard links" are copying."""
    mapped = _LINK_ERRNO_OUTCOMES.get(error.errno)
    return mapped if mapped is not None else _map_create_error(error)


def _technical(error: OSError | None) -> str | None:
    if error is None:
        return None
    return f"{error.strerror} (errno {error.errno})"


def _failure(
    outcome: HardlinkOutcome,
    reason: HardlinkReason,
    folder: str | None,
    checked_at: str,
    error: OSError | None = None,
) -> HardlinkResult:
    return HardlinkResult(
        outcome=outcome,
        reason=reason,
        folder=folder,
        technical=_technical(error),
        checked_at=checked_at,
    )


def _not_needed(checked_at: str) -> HardlinkResult:
    return HardlinkResult(
        outcome="not_needed", reason=None, folder=None, technical=None, checked_at=checked_at
    )


# --- which folders even need testing - derived from plan_folders, never guessed --


def _media_types_to_check(app_ids: Iterable[str]) -> tuple[str, ...]:
    """Every media type `plan_folders` builds BOTH a torrents and a media
    folder for, in the order `plan_folders` builds them.

    Reusing `plan_folders` instead of re-deriving media types from the
    catalog means this check can never test a folder Marrquee doesn't
    build. A downloader with no media app yet plans a bare `data/torrents`
    with no matching `data/media` folder - that is never a pair, on
    purpose, so it stays `not_needed` rather than being tested against a
    media folder that was never planned.
    """
    planned = storage.plan_folders(app_ids)
    media_folder_types = {
        relative.parts[2]
        for relative in planned
        if len(relative.parts) == 3 and relative.parts[:2] == ("data", "media")
    }
    return tuple(
        relative.parts[2]
        for relative in planned
        if len(relative.parts) == 3
        and relative.parts[:2] == ("data", "torrents")
        and relative.parts[2] in media_folder_types
    )


def _host_media_folder(storage_root: str, media_type: str) -> str:
    """The owner's own HOST path for one media type - mirrors
    `storage.host_media_path`'s shape without importing it (this module's
    only allowed dependency on storage.py is its path-translation helpers).
    """
    return str(PurePosixPath(storage_root) / "data" / "media" / media_type)


# --- the probe itself ---------------------------------------------------------


def run_hardlink_check(
    settings: Settings,
    install: InstallState | None,
    *,
    now: datetime,
    link: LinkFn = os.link,
    chown: storage.ChownFn = os.chown,
) -> HardlinkResult:
    """Prove, at the apps' own folders, that a finished download can become
    a library file with no extra space used - never raises.

    Nothing here touches the filesystem until every "is there even
    something to test" question is answered: no install, no storage root,
    no downloader, or no media type with both a torrents and a media
    folder planned, all come back `not_needed` without a single filesystem
    call.
    """
    checked_at = now.isoformat()

    if install is None or install.storage_root is None:
        return _not_needed(checked_at)

    apps = apps_in_order(install.app_ids)
    if not any(app.kind == "downloader" for app in apps):
        return _not_needed(checked_at)

    media_types = _media_types_to_check(install.app_ids)
    if not media_types:
        return _not_needed(checked_at)

    container_root = storage.to_host_view(settings, install.storage_root)

    for media_type in media_types:
        result = _check_pair(
            container_root,
            media_type,
            _host_media_folder(install.storage_root, media_type),
            checked_at,
            link=link,
            chown=chown,
            puid=install.puid,
            pgid=install.pgid,
        )
        if result.outcome != "works":
            return result

    return HardlinkResult(
        outcome="works", reason=None, folder=None, technical=None, checked_at=checked_at
    )


def _check_pair(
    container_root: Path,
    media_type: str,
    host_media_folder: str,
    checked_at: str,
    *,
    link: LinkFn,
    chown: storage.ChownFn,
    puid: int,
    pgid: int,
) -> HardlinkResult:
    """Prove (or disprove) a hard link for one torrents/media pair.

    Always removes its own test file before returning, on every path out -
    a crash between create and link must never leave litter for the next
    run, or the owner, to trip over.
    """
    try:
        torrents_dir = storage._safe_join(
            container_root, PurePosixPath("data", "torrents", media_type)
        )
        media_dir = storage._safe_join(container_root, PurePosixPath("data", "media", media_type))
    except storage.PathEscapesRoot:
        return _failure("couldnt_check", "folder_elsewhere", host_media_folder, checked_at)

    if not _is_dir(torrents_dir) or not _is_dir(media_dir):
        return _failure("couldnt_check", "folder_missing", host_media_folder, checked_at)

    source = torrents_dir / LINK_TEST_FILE_NAME
    dest = media_dir / LINK_TEST_FILE_NAME

    if not _remove_own_test_file(source) or not _remove_own_test_file(dest):
        return _failure("couldnt_check", "unexpected", host_media_folder, checked_at)

    try:
        return _probe_link(
            source,
            dest,
            host_media_folder,
            checked_at,
            link=link,
            chown=chown,
            puid=puid,
            pgid=pgid,
        )
    finally:
        _remove_own_test_file(source)
        _remove_own_test_file(dest)


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _probe_link(
    source: Path,
    dest: Path,
    host_media_folder: str,
    checked_at: str,
    *,
    link: LinkFn,
    chown: storage.ChownFn,
    puid: int,
    pgid: int,
) -> HardlinkResult:
    try:
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError as error:
        return _failure(*_map_create_error(error), host_media_folder, checked_at, error)

    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(words.LINK_TEST_FILE_TEXT)
    except OSError as error:
        return _failure(*_map_create_error(error), host_media_folder, checked_at, error)

    try:
        chown(source, puid, pgid)
    except OSError:
        logger.warning("could not chown the drive-check test file at %s", source, exc_info=True)

    try:
        link(source, dest)
    except OSError as error:
        return _failure(*_map_link_error(error), host_media_folder, checked_at, error)

    try:
        source_stat = os.stat(source)
        dest_stat = os.stat(dest)
    except OSError as error:
        return _failure(*_map_create_error(error), host_media_folder, checked_at, error)

    same_file = (source_stat.st_dev, source_stat.st_ino) == (dest_stat.st_dev, dest_stat.st_ino)
    if same_file and dest_stat.st_nlink >= 2:
        return HardlinkResult(
            outcome="works", reason=None, folder=None, technical=None, checked_at=checked_at
        )
    return _failure("copies", "no_hard_links", host_media_folder, checked_at)


def _remove_own_test_file(path: Path) -> bool:
    """Remove exactly the drive-check's own file - the ONLY function in
    this module allowed to delete anything (storage.py's own no-delete
    proof gets a twin here, scoped to this one function).

    Refuses any other name outright, without touching the filesystem at
    all, so this can never become a general-purpose delete by accident.
    Missing is success (nothing left to remove); a directory, or any other
    OSError, is a refusal - never a deletion of something this check did
    not create.
    """
    if path.name != LINK_TEST_FILE_NAME:
        logger.error("refusing to remove %s: not the drive-check's own file", path)
        return False

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning(
            "could not check the drive-check test file before removing it", exc_info=True
        )
        return False

    if stat_module.S_ISDIR(info.st_mode):
        logger.error("refusing to remove %s: it is a directory, not the drive-check's file", path)
        return False

    try:
        os.unlink(path)
    except OSError:
        logger.warning("could not remove the drive-check test file at %s", path, exc_info=True)
        return False
    return True


# --- saving and loading the latest result -------------------------------------


def save_hardlink_result(config_dir: Path, result: HardlinkResult) -> None:
    """Write the latest result to `<config_dir>/hardlink-check.json`.

    Root-only (0600, via `write_json_atomic`): only Marrquee reads this
    file, unlike the folders it just tested, which the owner's own account
    can always see and clear. OSError propagates - a caller decides what a
    failed save means for the result it already has in hand.
    """
    write_json_atomic(
        config_dir / HARDLINK_FILE_NAME,
        {
            "version": _RESULT_VERSION,
            "outcome": result.outcome,
            "reason": result.reason,
            "folder": result.folder,
            "technical": result.technical,
            "checked_at": result.checked_at,
        },
    )


def load_hardlink_result(config_dir: Path) -> HardlinkResult | None:
    """Read the latest saved result, or None for any reason at all.

    A missing file, an empty file, text that isn't JSON, JSON with the
    wrong shape or version, or a saved result whose own invariants don't
    hold are all the same answer: nothing usable is here yet.
    """
    try:
        raw = (config_dir / HARDLINK_FILE_NAME).read_text()
    except OSError:
        return None

    if not raw.strip():
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or payload.get("version") != _RESULT_VERSION:
        return None

    try:
        return _result_from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _result_from_payload(payload: dict[str, object]) -> HardlinkResult:
    outcome = _require_choice(payload.get("outcome"), _OUTCOMES)
    reason = _require_optional_choice(payload.get("reason"), _REASONS)
    if (outcome in _NO_REASON_OUTCOMES) != (reason is None):
        raise ValueError(f"outcome {outcome!r} does not match reason {reason!r}")

    return HardlinkResult(
        outcome=cast(HardlinkOutcome, outcome),
        reason=cast(HardlinkReason | None, reason),
        folder=_require_optional_str(payload.get("folder")),
        technical=_require_optional_str(payload.get("technical")),
        checked_at=_require_str(payload.get("checked_at")),
    )


def _require_choice(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"expected one of {sorted(allowed)}, got {value!r}")
    return value


def _require_optional_choice(value: object, allowed: frozenset[str]) -> str | None:
    return None if value is None else _require_choice(value, allowed)


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _require_optional_str(value: object) -> str | None:
    return None if value is None else _require_str(value)


def is_due(saved: HardlinkResult | None, now: datetime) -> bool:
    """Whether a check should run: never checked, unparseable, or a day old.

    A broken `checked_at` (a hand-edited file, or a future build that wrote
    it differently) means "run again", the same as never having checked.
    """
    if saved is None:
        return True
    try:
        checked_at = datetime.fromisoformat(saved.checked_at)
        return now - checked_at >= timedelta(hours=24)
    except (ValueError, TypeError):
        return True


# --- the monitor: one owner of "check now", "after every deploy/add" and -----
# --- "once a day" - Marrquee has no background loop, so this rides on ------
# --- whatever already happens (a deploy, an add, or the Hub's own poll) -----


# A sleeping NAS disk can take ~10-15s to spin up; a dead network mount can
# hang forever. Diagnostics waits this long for a fresh answer, then says
# "still checking" rather than let a hung drive hang the page.
DRIVE_CHECK_PAGE_WAIT_SECONDS: Final = 15.0


class HardlinkTrigger(Protocol):
    """What a deploy or an add needs from the monitor: "go check, and don't
    make me wait for it". `DeployManager` depends on only this - never the
    concrete `HardlinkMonitor` - so a recording fake can stand in for it in
    every deploy/add test without pulling in a real filesystem probe.
    """

    def request_check(self) -> object: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HardlinkMonitor:
    """The one owner of the drive check's saved result and its one, shared,
    in-flight task.

    There is no scheduler and no lock: with one process and one event loop,
    "at most one check in flight, everyone else joins it" IS the lock, and
    "once a day" rides entirely on `refresh_if_due` being asked by whatever
    already reaches the Hub (its own poll) or a deploy/add's own success.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        link: LinkFn = os.link,
        chown: storage.ChownFn = os.chown,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._settings = settings
        self._link = link
        self._chown = chown
        self._clock = clock
        self._task: asyncio.Task[HardlinkResult] | None = None

    def latest(self) -> HardlinkResult | None:
        """The latest saved result, re-read fresh on every call - never raises."""
        return load_hardlink_result(self._settings.config_dir)

    def _in_flight_task(self) -> asyncio.Task[HardlinkResult] | None:
        """The currently-running task, or None if there isn't one.

        A task from a DEAD loop (a bare `TestClient` hands each request a
        fresh one) is never "in flight" - joining it would wait forever for
        a loop nothing is iterating any more, so it is treated exactly like
        no task at all.
        """
        task = self._task
        if task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop():
            return task
        return None

    def request_check(self) -> asyncio.Task[HardlinkResult]:
        """Start a check, or hand back the one already running.

        Must be called from a running loop.
        """
        in_flight = self._in_flight_task()
        if in_flight is not None:
            return in_flight
        new_task = asyncio.get_running_loop().create_task(self._run_and_save())
        self._task = new_task
        return new_task

    def refresh_if_due(self) -> None:
        """Ask for a check only if none is running and the saved result is
        missing, unparseable, or a day old - never raises.
        """
        if self._in_flight_task() is None and is_due(self.latest(), self._clock()):
            self.request_check()

    async def check_now(
        self, *, wait_seconds: float = DRIVE_CHECK_PAGE_WAIT_SECONDS
    ) -> HardlinkResult | None:
        """Await a check up to `wait_seconds`, then give up on WAITING (not
        on the check itself - `asyncio.shield` keeps it running, and its
        result still gets saved, for whoever asks next).
        """
        try:
            return await asyncio.wait_for(asyncio.shield(self.request_check()), wait_seconds)
        except TimeoutError:
            return None

    async def _run_and_save(self) -> HardlinkResult:
        now = self._clock()
        install = load_state(self._settings.config_dir)
        result = await asyncio.to_thread(
            run_hardlink_check,
            self._settings,
            install,
            now=now,
            link=self._link,
            chown=self._chown,
        )
        try:
            save_hardlink_result(self._settings.config_dir, result)
        except OSError:
            logger.warning("could not save the drive-check result", exc_info=True)
        return result
