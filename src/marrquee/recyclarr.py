"""Recyclarr's own config: the guide-backed quality profiles it creates in
Sonarr and Radarr, and turning the owner's install plus saved answers into
the exact `recyclarr.yml` Recyclarr reads.

Rendering is hand-written string assembly, never a YAML library, for the
same reason as `compose.py`: the runtime image must not carry a YAML parser
it never calls, and this is the one place the config is derived from - a
`quality_profiles.trash_id` that syncs the wrong CFs is a bug to catch here,
not in a template `include:` nobody reads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import textwrap
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Protocol

from marrquee import storage, words
from marrquee.catalog import RECYCLARR_APP_ID, apps_in_order, require_port
from marrquee.config import Settings
from marrquee.docker_client import DockerEngine
from marrquee.questions import (
    DEFAULT_QUALITY,
    MOVIE_QUALITY_FIELD,
    TV_QUALITY_FIELD,
    Quality,
    load_answers,
)
from marrquee.state import InstallState, load_state
from marrquee.storage import ChownFn, to_host_view

logger = logging.getLogger(__name__)

_COMMENT_WIDTH = 78


@dataclass(frozen=True)
class GuideProfile:
    """One TRaSH-guide quality profile Recyclarr creates, pinned by hand
    against the config-templates master and TRaSH's own profile JSON
    rather than included by name, so the exact qualities, CFs and scores
    synced are visible in this file instead of hidden behind a template
    version that can change underneath an `include:`.
    """

    trash_id: str
    name: str
    quality_definition: Literal["series", "movie"]
    cf_groups: tuple[str, ...]


GUIDE_PROFILES: Mapping[tuple[str, Quality], GuideProfile] = {
    ("sonarr", "1080p"): GuideProfile(
        trash_id="72dae194fc92bf828f32cde7744e51a1",
        name="WEB-1080p",
        quality_definition="series",
        cf_groups=(
            "158188097a58d7687dee647e04af0da3",
            "74aff4168620ed49dcc67e92b2c2a5b4",
            "85fae4a2294965b75710ef2989c850eb",
            "59c3af66780d08332fdc64e68297098f",
        ),
    ),
    ("sonarr", "4k"): GuideProfile(
        trash_id="d1498e7d189fbe6c7110ceaabb7473e6",
        name="WEB-2160p",
        quality_definition="series",
        cf_groups=(
            "e3f37512790f00d0e89e54fe5e790d1c",
            "74aff4168620ed49dcc67e92b2c2a5b4",
            "85fae4a2294965b75710ef2989c850eb",
            "59c3af66780d08332fdc64e68297098f",
        ),
    ),
    ("radarr", "1080p"): GuideProfile(
        trash_id="d1d67249d3890e49bc12e275d989a7e9",
        name="HD Bluray + WEB",
        quality_definition="movie",
        cf_groups=(
            "f8bf8eab4617f12dfdbd16303d8da245",
            "a3ac6af01d78e4f21fcb75f601ac96df",
        ),
    ),
    ("radarr", "4k"): GuideProfile(
        trash_id="64fb5f9858489bdac2af690e27c8f42f",
        name="UHD Bluray + WEB",
        quality_definition="movie",
        cf_groups=(
            "ff204bbcecdd487d1cefcefdbf0c278d",
            "a3ac6af01d78e4f21fcb75f601ac96df",
        ),
    ),
}

# The only two apps Recyclarr ever configures, and the field each one's
# quality answer is saved under - `chosen_quality`/`render_recyclarr_config`
# both read this instead of a second, parallel list of ids.
_QUALITY_FIELD_BY_APP: Mapping[str, str] = {
    "sonarr": TV_QUALITY_FIELD,
    "radarr": MOVIE_QUALITY_FIELD,
}


def chosen_quality(app_id: str, answers: Mapping[str, Mapping[str, str]]) -> Quality:
    """The saved 1080p/4K choice for `app_id`, or `DEFAULT_QUALITY` for
    anything else - a missing answer, an app this module doesn't
    configure, or a value that isn't one of the two options (this build's
    form never posts one, but a stale save from a future build might).
    """
    field = _QUALITY_FIELD_BY_APP.get(app_id)
    if field is None:
        return DEFAULT_QUALITY
    saved = answers.get(app_id, {}).get(field)
    if saved == "1080p":
        return "1080p"
    if saved == "4k":
        return "4k"
    return DEFAULT_QUALITY


def quality_profile_name(app_id: str, answers: Mapping[str, Mapping[str, str]]) -> str | None:
    """The exact profile name Recyclarr creates for `app_id`, so another app
    can point new requests at the right quality profile by name - `None`
    for anything that isn't Sonarr or Radarr. This only derives what the
    name WOULD be from the saved answer; a caller must still check that
    Recyclarr is actually installed before trusting the name exists in
    either app.
    """
    if app_id not in _QUALITY_FIELD_BY_APP:
        return None
    return GUIDE_PROFILES[(app_id, chosen_quality(app_id, answers))].name


def _comment_lines(text: str, width: int = _COMMENT_WIDTH) -> list[str]:
    return [f"# {wrapped}" for wrapped in textwrap.wrap(text, width=width)]


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_recyclarr_config(state: InstallState, answers: Mapping[str, Mapping[str, str]]) -> str:
    """The exact `recyclarr.yml` text for this install, derived only from
    `state.app_ids`/`state.api_keys` and the saved quality answers - never
    from anything Recyclarr itself has ever done.

    Raises `ValueError` when neither Sonarr nor Radarr is installed - there
    is nothing for Recyclarr to configure, and a caller that reaches this
    with such a state has a bug upstream.
    """
    apps = [app for app in apps_in_order(state.app_ids) if app.id in _QUALITY_FIELD_BY_APP]
    if not apps:
        raise ValueError("recyclarr config needs an installed sonarr or radarr")

    lines = [*_comment_lines(words.RECYCLARR_CONFIG_HEADER_COMMENT)]
    for app in apps:
        profile = GUIDE_PROFILES[(app.id, chosen_quality(app.id, answers))]
        base_url = f"http://{app.id}:{require_port(app)}"
        api_key = state.api_keys[app.id]
        lines.append(f"{app.id}:")
        lines.append(f"  {app.id}:")
        lines.append(f"    base_url: {_quoted(base_url)}")
        lines.append(f"    api_key: {_quoted(api_key)}")
        lines.append("    quality_definition:")
        lines.append(f"      type: {_quoted(profile.quality_definition)}")
        lines.append("    quality_profiles:")
        lines.append(f"      - trash_id: {_quoted(profile.trash_id)}")
        lines.append(f"        name: {_quoted(profile.name)}")
        lines.append("        reset_unmatched_scores:")
        lines.append("          enabled: true")
        lines.append("    custom_format_groups:")
        lines.append("      add:")
        lines.extend(f"        - trash_id: {_quoted(group)}" for group in profile.cf_groups)
    return "\n".join(lines) + "\n"


def recyclarr_config_host_path(root: PurePosixPath) -> PurePosixPath:
    """Where the rendered config lives, as a HOST path under `root` - the
    same `marrquee/apps/recyclarr` folder every other app's own folder
    lives in, so it is created and chowned to the drive's owner the same
    way, and mounted at `/config` by the compose service.
    """
    return root / "marrquee" / "apps" / "recyclarr" / "recyclarr.yml"


def _write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def write_recyclarr_config(
    settings: Settings,
    state: InstallState,
    answers: Mapping[str, Mapping[str, str]],
    *,
    chown: ChownFn = os.chown,
) -> None:
    """Render and atomically rewrite `recyclarr.yml`, so it is always
    current before Marrquee starts the next sync.

    Written the same way the compose file is: a sibling temp file
    `chmod`'d `0600` before it has any content worth reading, then landed
    with `os.replace` - it carries the same API keys the compose file does.
    Chowned to the install's puid/pgid, since Recyclarr reads it as that
    user, never as root. A share that doesn't support `chown` must never
    turn a successful write into a failed one, so that failure alone is
    swallowed and logged with no values. `storage._safe_join` is what turns
    a symlink planted where this file should live into a loud
    `PathEscapesRoot` instead of a silent write somewhere else on the
    drive - that, like a bad render, is left to propagate.
    """
    if state.storage_root is None:
        raise ValueError("recyclarr config needs a storage root")
    root = PurePosixPath(state.storage_root)
    host_path = recyclarr_config_host_path(root)
    container_root = to_host_view(settings, str(root))
    relative = host_path.relative_to(root)
    path = storage._safe_join(container_root, relative)

    _write_atomic_text(path, render_recyclarr_config(state, answers))
    try:
        chown(path, state.puid, state.pgid)
    except OSError:
        logger.warning("could not chown recyclarr.yml to the drive owner")


def remove_recyclarr_config(settings: Settings, root: PurePosixPath) -> None:
    """Delete Marrquee's own copy of `recyclarr.yml`, without ever raising.

    Called when a Recyclarr add is cancelled - Marrquee's derived copy of
    every arr app's key has no reason to sit on disk for an app that no
    longer exists. `lstat` (never `stat`) is what keeps this from following
    a symlink planted in the file's own place; it unlinks only a genuine
    regular file, never a directory or anything else found there. A
    missing folder is simply nothing to do; any other filesystem hiccup (a
    permissions error, a symlink `_safe_join` refuses) must never turn an
    otherwise successful cancel into a failed one, so it is only logged.
    """
    try:
        container_root = to_host_view(settings, str(root))
        relative = recyclarr_config_host_path(root).relative_to(root)
        path = storage._safe_join(container_root, relative)
        entry_stat = path.lstat()
    except FileNotFoundError:
        return
    except (OSError, storage.PathEscapesRoot) as error:
        logger.warning("could not remove recyclarr.yml: %s", error)
        return

    if not stat.S_ISREG(entry_stat.st_mode):
        return
    try:
        path.unlink()
    except OSError as error:
        logger.warning("could not remove recyclarr.yml: %s", error)


# --- Reading Recyclarr's own verdict back out of its per-run log files --------

# v8.7.2's own log sink names every CLI run's log
# `recyclarr_<yyyy-MM-dd_HH-mm-ss>.debug.log` and prefixes every line with
# `[HH:mm:ss LVL] ` - both patterns are pinned by hand against that exact
# tag, not derived, since a later Recyclarr release could change either one.
_LOG_FILE_NAME_PATTERN = re.compile(r"^recyclarr_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.debug\.log$")
_LOG_FAILURE_LINE_PATTERN = re.compile(r"^\[\d{2}:\d{2}:\d{2} (ERR|FTL)\] ")

# Read at most the newest log's last few MiB - a debug log grows without
# bound over many instances, and the verdict this module cares about (any
# ERR/FTL line at all) is exactly as true from the tail as from the whole
# file, at a fraction of the cost.
_LOG_TAIL_BYTES = 4 * 1024 * 1024


def recyclarr_log_dir(root: PurePosixPath) -> PurePosixPath:
    """Where Recyclarr writes one file per CLI run - a cron sync and a
    Marrquee-triggered Sync now land in the same folder, so this is the
    one place both are ever read back from.
    """
    return root / "marrquee" / "apps" / "recyclarr" / "logs" / "cli"


@dataclass(frozen=True)
class SyncRecord:
    """The newest run this module can find, and its verdict.

    `finished_at` is the log file's own `st_mtime`, not anything parsed out
    of its name - the file name is local time with no offset, and mtime
    sidesteps guessing which timezone that was ever written in.
    """

    finished_at: datetime
    ok: bool


def read_last_sync(settings: Settings, storage_root: str) -> SyncRecord | None:
    """The newest recyclarr CLI log under `storage_root`, and whether it
    logged a failure - never raises.

    A missing log folder, a folder with no matching file name, a symlink
    `_safe_join` refuses, or a file that can't be opened all mean the same
    thing: nothing usable is here yet.
    """
    try:
        root = PurePosixPath(storage_root)
        container_root = to_host_view(settings, storage_root)
        relative = recyclarr_log_dir(root).relative_to(root)
        log_dir = storage._safe_join(container_root, relative)
        entries = tuple(log_dir.iterdir())
    except (OSError, storage.PathEscapesRoot):
        return None

    newest_path: Path | None = None
    newest_mtime = -1.0
    for entry in entries:
        if not _LOG_FILE_NAME_PATTERN.match(entry.name):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_path = entry

    if newest_path is None:
        return None

    tail = _read_tail(newest_path)
    if tail is None:
        return None

    ok = not any(_LOG_FAILURE_LINE_PATTERN.match(line) for line in tail.splitlines())
    return SyncRecord(finished_at=datetime.fromtimestamp(newest_mtime, tz=UTC), ok=ok)


def _read_tail(path: Path, *, max_bytes: int = _LOG_TAIL_BYTES) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


# --- Deciding when a sync is worth asking for ----------------------------------


def sync_wanted_after(app_ids: Iterable[str], changed: str | None) -> bool:
    """Whether the thing that just finished should trigger a sync.

    `changed=None` is a full deploy (every installed app just came up, or
    was confirmed already running) - it always asks, as long as Recyclarr
    is installed at all. A single app add/change only asks when the
    changed app is Recyclarr itself or one of the two apps its config
    depends on; nothing else (Prowlarr, qBittorrent, Gluetun) ever affects
    what `recyclarr.yml` should hold.
    """
    ids = frozenset(app_ids)
    if RECYCLARR_APP_ID not in ids:
        return False
    return changed is None or changed in (RECYCLARR_APP_ID, "sonarr", "radarr")


# --- The monitor: writes the config, runs the sync, remembers the verdict -----


SyncState = Literal["syncing", "never", "ok", "late", "failed"]


@dataclass(frozen=True)
class SyncStatus:
    """What the Hub needs to know about Recyclarr right now.

    `start_failed` is set the moment Marrquee itself couldn't even start a
    run (a bad config write, or Docker refusing the exec) - `run_failed` is
    the broader signal that covers those same cases AND a run that DID
    start but didn't actually succeed. A real sync's own exit code is not
    trusted as the sole proof of success: a non-zero exit, a timeout, or a
    clean exit whose own newest log still shows an ERR/FTL line are all
    `run_failed` too. Both clear the moment a newer, ok log appears - the
    same "stale until proven otherwise" shape `HardlinkResult` already
    uses.
    """

    syncing: bool
    last: SyncRecord | None
    start_failed: bool
    run_failed: bool


class RecyclarrTrigger(Protocol):
    """What a deploy or an add needs from the monitor: "sync, and don't
    make me wait for it" - the same narrow seam `HardlinkTrigger` gives
    `DeployManager` for the drive check.
    """

    def request_sync(self) -> object: ...


class RecyclarrControl(Protocol):
    """What the Hub needs from the monitor: ask for a sync, and read its
    current status - one seam a test can stand in for with a single fake,
    whether it is exercising the deploy engine or a Hub route.
    """

    def request_sync(self) -> object: ...
    async def status(self) -> SyncStatus: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RecyclarrMonitor:
    """The one owner of "sync now", of never running two syncs at once, and
    of the last run's own remembered verdict.

    There is no scheduler and no lock, the same shape as `HardlinkMonitor`:
    with one process and one event loop, "at most one sync in flight,
    everyone else joins it" IS the lock. A request that arrives while a
    sync is already running is never dropped: it sets `_again`, which the
    in-flight run itself checks right before it would otherwise finish, so
    the config it rewrites and the sync it runs are always for the LATEST
    request, never a stale one collected while the previous run was still
    going.
    """

    SYNC_POLL_SECONDS: ClassVar[float] = 2.0
    SYNC_TIMEOUT_SECONDS: ClassVar[float] = 900.0

    def __init__(
        self,
        settings: Settings,
        engine: DockerEngine,
        *,
        chown: ChownFn = os.chown,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        after_sync: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._chown = chown
        self._clock = clock
        self._sleep = sleep
        # Awaited once per run that actually earns a clean verdict (exit 0
        # AND a log that doesn't read as failed) - never on a failed run,
        # and never allowed to turn a real success into a reported failure.
        self._after_sync = after_sync
        self._task: asyncio.Task[bool] | None = None
        self._again = False
        self._start_failed_at: datetime | None = None
        self._run_failed_at: datetime | None = None

    def _in_flight_task(self) -> asyncio.Task[bool] | None:
        """The currently-running task, or None if there isn't one.

        A task from a DEAD loop (a bare `TestClient` hands each request a
        fresh one) is never "in flight" - joining it would wait forever for
        a loop nothing is iterating any more, so it is treated exactly like
        no task at all, the same rule `HardlinkMonitor._in_flight_task`
        applies for the drive check.
        """
        task = self._task
        if task is not None and not task.done() and task.get_loop() is asyncio.get_running_loop():
            return task
        return None

    def request_sync(self) -> asyncio.Task[bool]:
        """Start a sync, or hand back the one already running (asking it to
        run once more after it finishes, so this request is never lost).

        Must be called from a running loop.
        """
        in_flight = self._in_flight_task()
        if in_flight is not None:
            self._again = True
            return in_flight
        new_task = asyncio.get_running_loop().create_task(self._run_until_settled())
        self._task = new_task
        return new_task

    async def status(self) -> SyncStatus:
        """The current truth, re-read fresh on every call - never raises."""
        install = load_state(self._settings.config_dir)
        last = (
            None
            if install is None or install.storage_root is None
            else await asyncio.to_thread(read_last_sync, self._settings, install.storage_root)
        )
        return SyncStatus(
            syncing=self._in_flight_task() is not None,
            last=last,
            start_failed=self._is_stale_failure(self._start_failed_at, last),
            run_failed=self._is_stale_failure(self._run_failed_at, last),
        )

    @staticmethod
    def _is_stale_failure(failed_at: datetime | None, last: SyncRecord | None) -> bool:
        if failed_at is None:
            return False
        return last is None or last.finished_at < failed_at

    async def _run_until_settled(self) -> bool:
        """The task body `request_sync` creates: run once, then keep running
        again for as long as a request arrived while the previous run was
        still going - never awaited by a caller, so this alone decides when
        the task is finally done.
        """
        result = await self._run_once()
        while self._again:
            self._again = False
            result = await self._run_once()
        return result

    def _mark_start_failed(self) -> None:
        now = self._clock()
        self._start_failed_at = now
        self._run_failed_at = now

    async def _run_once(self) -> bool:
        install = load_state(self._settings.config_dir)
        if (
            install is None
            or install.storage_root is None
            or RECYCLARR_APP_ID not in install.app_ids
        ):
            return False

        try:
            await asyncio.to_thread(
                write_recyclarr_config,
                self._settings,
                install,
                load_answers(self._settings.config_dir),
                chown=self._chown,
            )
        except (OSError, ValueError, storage.PathEscapesRoot):
            logger.warning("recyclarr sync: could not write recyclarr.yml", exc_info=True)
            self._mark_start_failed()
            return False

        start_result = await self._engine.exec_start(RECYCLARR_APP_ID, ("recyclarr", "sync"))
        if not start_result.ok or start_result.exec_id is None:
            logger.warning("recyclarr sync: could not start the exec: %s", start_result.detail)
            self._mark_start_failed()
            return False

        exit_code = await self._await_exec(start_result.exec_id)
        if exit_code is None or exit_code != 0:
            self._run_failed_at = self._clock()
            return False

        # A real Recyclarr release is not proven to exit non-zero for a
        # single failed instance sync - the exit code alone is never
        # trusted as proof of success, so the freshly written log is
        # checked too before this run is allowed to clear the verdict.
        log = await asyncio.to_thread(read_last_sync, self._settings, install.storage_root)
        if log is not None and not log.ok:
            self._run_failed_at = self._clock()
            return False

        self._run_failed_at = None
        if self._after_sync is not None:
            try:
                await self._after_sync()
            except Exception:
                # A refresh that can't run is never this run's own failure -
                # the sync itself genuinely succeeded, so the verdict this
                # method returns must say so regardless.
                logger.warning("recyclarr sync: after_sync hook failed", exc_info=True)
        return True

    async def _await_exec(self, exec_id: str) -> int | None:
        """Poll until the exec is no longer running, or give up at the
        timeout - `None` covers both a timeout and Docker forgetting the
        exec id entirely, since neither is a code this module trusts.
        """
        start = self._clock()
        while True:
            state = await self._engine.exec_inspect(exec_id)
            if not state.running:
                return state.exit_code if state.known else None
            if (self._clock() - start).total_seconds() >= self.SYNC_TIMEOUT_SECONDS:
                logger.warning("recyclarr sync: timed out waiting for the exec to finish")
                return None
            await self._sleep(self.SYNC_POLL_SECONDS)
