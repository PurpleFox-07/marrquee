"""The only module allowed to touch the host filesystem the owner mounted in.

The owner gave Marrquee a read-write view of their NAS's shared-folder
root(s) (for example `/volume1` mounted at `/host/volume1`) so that a typed
path can be checked and its folders built without spawning a helper
container per keystroke. That capability is only safe because this
one module enforces the promise the rest of the product makes: Marrquee
only ever `mkdir`s and `chown`s folders it created itself, and it is
structurally incapable of deleting, moving or renaming anything - proven by
`test_storage_py_has_no_deletion_calls` scanning this file's own source.

Every function here that touches the filesystem resolves the path it is
about to use and checks it really lands inside the folder the owner chose,
before doing anything with it. That is what stands between a `..`, a
symlink planted by something else on the drive, or a root that is itself a
symlink, and a write that lands somewhere the owner never agreed to.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import posixpath
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from marrquee.catalog import apps_in_order
from marrquee.config import Settings
from marrquee.state import write_json_atomic
from marrquee.words import MARKER_WHAT_IS_THIS

logger = logging.getLogger(__name__)

# System locations that are never a media drive, even though the `/host`
# mount makes them technically visible and (for some) writable. Checked
# before any filesystem call, so a typo like "/usr" is refused for what it
# is rather than accepted because "it looks fine".
REFUSED_ROOTS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
    }
)

MARKER_NAME = "marrquee-root.json"
_MARKER_VERSION = 1

_FALLBACK_ID = 1000
_FALLBACK_TIMEZONE = "Etc/UTC"
_DEFAULT_UMASK = "002"

ChownFn = Callable[[Path, int, int], None]
ChmodFn = Callable[[Path, int], None]

StorageCheckReason = Literal[
    "empty",
    "not_absolute",
    "system_path",
    "not_shared",
    "missing",
    "not_a_folder",
    "not_writable",
]


class PathEscapesRoot(RuntimeError):
    """A path this module was about to touch resolved outside the chosen root.

    This should never happen on a normal deploy - it means a folder inside
    the owner's chosen root is (or contains) a symlink pointing somewhere
    else, planted before or during this run. Refusing loudly here is what
    keeps "only ever writes inside the one folder you choose" true even
    when the filesystem underneath has been tampered with.
    """


@dataclass(frozen=True)
class StorageCheck:
    """The truthful answer to "can Marrquee use this folder?" - never an exception."""

    ok: bool
    host_path: PurePosixPath | None
    exists: bool
    is_dir: bool
    writable: bool
    free_bytes: int | None
    total_bytes: int | None
    reason: StorageCheckReason | None
    suggestions: tuple[str, ...]
    detail: str | None
    suggested_path: PurePosixPath | None = None


@dataclass(frozen=True)
class FreshnessCheck:
    """Whether a target is a fresh library, or one Marrquee already set up."""

    ok: bool
    reason: Literal["already_has_files"] | None = None
    occupied: tuple[str, ...] = ()


@dataclass(frozen=True)
class FolderReport:
    """Which folders this call actually created - the only ones safe to chown."""

    created: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Marker:
    """What a previous run recorded about a storage root it built.

    The deploy engine reads this back to tell "a container by this name is
    one we created ourselves on an earlier attempt" apart from "someone
    else's app happens to share the name" - the difference between a safe
    resume and a refusal.
    """

    created: str
    app_ids: tuple[str, ...]


@dataclass(frozen=True)
class DerivedIds:
    """PUID/PGID/UMASK/timezone, derived from the owner's own storage folder."""

    puid: int
    pgid: int
    umask: str
    timezone: str


# --- Path translation: the one place a host path and a container path meet -


def to_host_view(settings: Settings, typed: str) -> Path:
    """Translate an absolute HOST path the owner typed into our container view.

    `..` and repeated slashes are collapsed here, before the join, not
    after - joining an uncollapsed "../../etc/passwd" onto the mount would
    let the OS walk straight back out of it the moment the path is opened.
    """
    if not typed or not typed.startswith("/"):
        raise ValueError(f"expected an absolute host path, got {typed!r}")

    normalized = posixpath.normpath(typed)
    relative = normalized.lstrip("/")
    return settings.host_mount / relative if relative else settings.host_mount


def from_host_view(settings: Settings, container_path: Path) -> PurePosixPath:
    """The inverse of `to_host_view`: our container path back to a host path."""
    relative = container_path.relative_to(settings.host_mount)
    return PurePosixPath("/") / relative


_ROOT = PurePosixPath("/")


def _is_system_path(candidate: PurePosixPath) -> bool:
    """Whether `candidate` is, or is inside, one of `REFUSED_ROOTS`.

    "/" gets an equality-only check: `is_relative_to("/")` is true of every
    absolute path, so treating it like the other entries would refuse
    everything.
    """
    for refused in REFUSED_ROOTS:
        refused_path = PurePosixPath(refused)
        if candidate == refused_path:
            return True
        if refused_path != _ROOT and candidate.is_relative_to(refused_path):
            return True
    return False


def _probe_exists(path: Path) -> bool:
    """Whether `path` exists, treating any `OSError` the OS itself raises
    while trying to find out (a segment too long to stat, a filesystem that
    refuses to answer) the same as "not there".

    A typed path is exactly the kind of input this module cannot pre-check
    the length or shape of before asking the filesystem, so every probe
    below goes through a helper like this one rather than calling `Path`'s
    methods directly - the checker's whole contract is that it never raises.
    """
    try:
        return path.exists()
    except OSError:
        return False


def _probe_is_dir(path: Path) -> bool:
    """`Path.is_dir`, with the same "any OSError means no" rule as `_probe_exists`."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _probe_child_names(path: Path) -> tuple[str, ...]:
    """`path`'s real children's names, or none at all if listing them fails."""
    try:
        return tuple(child.name for child in path.iterdir())
    except OSError:
        return ()


def _closest_existing_ancestor(
    container_path: Path, container_root: Path
) -> tuple[Path, str | None]:
    """The deepest real folder on the way to `container_path`, and the name
    that's missing right past it (or `None` when nothing is missing at all).

    Climbing all the way to `container_root` before giving up is what tells
    "Marrquee can't see this drive at all" (the climb reaches the mount
    itself) apart from "this folder is a typo inside a drive it can see"
    (the climb stops partway down) - the one fact `not_shared` and `missing`
    each need.
    """
    ancestor = container_path
    while ancestor != container_root and not _probe_exists(ancestor):
        ancestor = ancestor.parent

    if not _probe_is_dir(ancestor):
        return ancestor, None

    remaining = container_path.relative_to(ancestor).parts
    if not remaining:
        return ancestor, None
    return ancestor, remaining[0]


def _missing_child_suggestions(ancestor: Path, target_name: str | None) -> tuple[str, ...]:
    """Up to three of `ancestor`'s real children whose name is close to `target_name`."""
    if target_name is None:
        return ()
    return tuple(difflib.get_close_matches(target_name, _probe_child_names(ancestor), n=3))


def _suggested_path(
    host_path: PurePosixPath,
    ancestor: Path,
    container_root: Path,
    target_name: str | None,
    suggestions: tuple[str, ...],
) -> PurePosixPath | None:
    """An existing, full host folder close to what was typed, or `None`.

    Swaps the missing segment for the closest match in place, so a typo
    deep inside an otherwise-real path still points at a real folder;
    falls back to the matched folder itself when that swapped path doesn't
    exist (nothing further down was ever there to swap). Never touches the
    filesystem for anything other than an existence check, so nothing
    outside this module has to.
    """
    if target_name is None or not suggestions:
        return None

    ancestor_host_path = PurePosixPath("/", *ancestor.relative_to(container_root).parts)
    rest = host_path.relative_to(ancestor_host_path).parts[1:]
    best_match = suggestions[0]

    swapped = ancestor_host_path.joinpath(best_match, *rest)
    swapped_container_path = ancestor.joinpath(best_match, *rest)
    if _probe_is_dir(swapped_container_path):
        return swapped
    return ancestor_host_path / best_match


def check_storage_root(settings: Settings, typed: str) -> StorageCheck:
    """Check whether a typed path is a usable, fresh-enough storage root.

    Never raises: every way this can fail comes back as a `reason`, because
    the wizard calls this on every keystroke and an exception there is a
    broken screen, not a helpful one. Checks run from the most actionable
    reason to the most technical one, so the owner always sees the thing
    they can fix first.
    """
    if not typed.strip():
        return _refusal("empty")
    if not typed.startswith("/"):
        return _refusal("not_absolute")

    normalized = posixpath.normpath(typed)
    host_path = PurePosixPath(normalized)
    if _is_system_path(host_path):
        return _refusal("system_path", host_path=host_path)

    container_path = to_host_view(settings, normalized)

    if not _probe_exists(container_path):
        ancestor, target_name = _closest_existing_ancestor(container_path, settings.host_mount)
        suggestions = _missing_child_suggestions(ancestor, target_name)
        suggested_path = _suggested_path(
            host_path, ancestor, settings.host_mount, target_name, suggestions
        )
        # The climb reached the mount itself without finding anything real:
        # not even the first folder of the typed path is one Marrquee's
        # install file mounts in. That's a different problem from a typo
        # inside a drive it can see, and it gets its own reason and wording.
        reason: StorageCheckReason = "not_shared" if ancestor == settings.host_mount else "missing"
        return _refusal(
            reason, host_path=host_path, suggestions=suggestions, suggested_path=suggested_path
        )

    if not _probe_is_dir(container_path):
        return _refusal("not_a_folder", host_path=host_path, exists=True)

    # A folder that exists and passed the lexical system-path check above
    # can still *resolve* onto one, if it (or an ancestor) is a symlink.
    # Resolving once here, before anything else touches it, is what makes a
    # symlinked root a decided case instead of an open question.
    try:
        resolved_host_path = from_host_view(settings, container_path.resolve())
    except ValueError:
        return _refusal("system_path", host_path=host_path, exists=True, is_dir=True)
    if _is_system_path(resolved_host_path):
        return _refusal("system_path", host_path=host_path, exists=True, is_dir=True)

    try:
        with tempfile.NamedTemporaryFile(dir=container_path):
            pass
    except OSError as error:
        return _refusal(
            "not_writable", host_path=host_path, exists=True, is_dir=True, detail=str(error)
        )

    usage = os.statvfs(container_path)
    return StorageCheck(
        ok=True,
        host_path=host_path,
        exists=True,
        is_dir=True,
        writable=True,
        free_bytes=usage.f_bavail * usage.f_frsize,
        total_bytes=usage.f_blocks * usage.f_frsize,
        reason=None,
        suggestions=(),
        detail=None,
    )


def shared_roots(settings: Settings) -> tuple[PurePosixPath, ...]:
    """The top-level folders Marrquee can actually see under the host mount.

    This is what the field hint and a `not_shared` refusal name instead of
    guessing - since the install file mounts only the drives the owner
    listed, pointing at a folder outside this list would send them looking
    somewhere Marrquee could never find it. Never raises: a mount that
    isn't there yet just has nothing to offer.
    """
    try:
        entries = list(settings.host_mount.iterdir())
    except OSError:
        return ()

    names = sorted(
        entry.name for entry in entries if entry.is_dir() and f"/{entry.name}" not in REFUSED_ROOTS
    )
    return tuple(PurePosixPath("/", name) for name in names)


def _refusal(
    reason: StorageCheckReason,
    *,
    host_path: PurePosixPath | None = None,
    exists: bool = False,
    is_dir: bool = False,
    suggestions: tuple[str, ...] = (),
    detail: str | None = None,
    suggested_path: PurePosixPath | None = None,
) -> StorageCheck:
    return StorageCheck(
        ok=False,
        host_path=host_path,
        exists=exists,
        is_dir=is_dir,
        writable=False,
        free_bytes=None,
        total_bytes=None,
        reason=reason,
        suggestions=suggestions,
        detail=detail,
        suggested_path=suggested_path,
    )


# --- The planned folder tree: pure, derived only from the chosen apps ------


def plan_folders(app_ids: Iterable[str]) -> tuple[PurePosixPath, ...]:
    """The expert TRaSH-shaped tree, relative to the (not-yet-known) root.

    One shared `data/torrents` and `data/media` root across every app is
    what makes a finished download move into the library atomically -
    `data/torrents` is planned even for apps with no downloader yet, so a
    later cycle never has to restructure an owner's live library to add
    one.
    """
    apps = apps_in_order(app_ids)

    media_types: list[str] = []
    for app in apps:
        for media_folder in app.media_folders:
            if media_folder not in media_types:
                media_types.append(media_folder)

    planned = [PurePosixPath("data", "torrents", media) for media in media_types]
    planned += [PurePosixPath("data", "media", media) for media in media_types]
    planned.append(PurePosixPath("marrquee"))
    planned += [PurePosixPath("marrquee", "apps", app.id) for app in apps]
    return tuple(planned)


def _safe_join(container_root: Path, relative: PurePosixPath) -> Path:
    """Join `relative` onto `container_root`, proving the result lands inside it.

    Resolving is what catches a folder along the way that turned out to be
    a symlink pointing somewhere else - lexical checks alone can't see
    that.
    """
    root_resolved = container_root.resolve()
    candidate = (container_root / str(relative)).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise PathEscapesRoot(f"{relative} resolves outside {container_root}")
    return candidate


def _marker_path(container_root: Path) -> Path:
    return _safe_join(container_root, PurePosixPath("marrquee") / MARKER_NAME)


def check_fresh_start(
    settings: Settings, root: PurePosixPath, app_ids: Iterable[str]
) -> FreshnessCheck:
    """Refuse a target that holds someone else's library, accept our own re-runs.

    Keyed to our own marker rather than to emptiness: a re-deploy has to
    work without ever mistaking an owner's existing library for a fresh
    target, or a fresh target for one to adopt.

    A planned folder that turns out to be a link pointing outside the root
    is refused as occupied rather than raised: it is something already
    there that Marrquee didn't make, and a refusal is what lets the install
    save and the deploy re-check answer with a sentence instead of a crash.
    """
    container_root = to_host_view(settings, str(root))

    try:
        marker = _marker_path(container_root)
    except PathEscapesRoot:
        return FreshnessCheck(ok=False, reason="already_has_files", occupied=("marrquee",))
    if marker.is_file():
        return FreshnessCheck(ok=True)

    occupied: list[str] = []
    for relative in plan_folders(app_ids):
        if not str(relative).startswith("data/media/"):
            continue
        try:
            candidate = _safe_join(container_root, relative)
        except PathEscapesRoot:
            occupied.append(str(relative))
            continue
        if candidate.is_dir() and any(candidate.iterdir()):
            occupied.append(str(relative))

    if occupied:
        return FreshnessCheck(ok=False, reason="already_has_files", occupied=tuple(occupied))
    return FreshnessCheck(ok=True)


def write_marker(
    settings: Settings,
    root: PurePosixPath,
    app_ids: Iterable[str],
    puid: int,
    pgid: int,
    *,
    chown: ChownFn = os.chown,
) -> Path:
    """Write the marker that tells a future run "Marrquee built this".

    Chowned to the drive's own owner, the same as every folder
    `build_folders` creates - the marker carries no secret, unlike the
    compose file, but there is no reason for it to be any less readable by
    the owner who might open it from their NAS's Files app. A share that
    doesn't support `chown` must never turn a successful deploy into a
    failed one, so that failure is swallowed and logged, never raised.
    """
    container_root = to_host_view(settings, str(root))
    marker_path = _marker_path(container_root)
    write_json_atomic(
        marker_path,
        {
            "created": datetime.now(UTC).isoformat(),
            "version": _MARKER_VERSION,
            "app_ids": [app.id for app in apps_in_order(app_ids)],
            "what_is_this": MARKER_WHAT_IS_THIS,
        },
    )
    try:
        chown(marker_path, puid, pgid)
    except OSError as error:
        logger.warning("could not chown the marker file to %s:%s: %s", puid, pgid, error)
    return marker_path


def read_marker(settings: Settings, root: PurePosixPath) -> Marker | None:
    """Read back whatever `write_marker` last wrote for this root, or None.

    Never raises: a missing, unreadable or corrupt marker is answered the
    same way `load_state` answers a corrupt settings file - "nothing usable
    is here", not an exception a caller has to guard against.
    """
    container_root = to_host_view(settings, str(root))
    try:
        raw = _marker_path(container_root).read_text()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    app_ids = payload.get("app_ids")
    if not isinstance(app_ids, list) or not all(isinstance(item, str) for item in app_ids):
        return None
    created = payload.get("created")
    if not isinstance(created, str):
        return None

    return Marker(created=created, app_ids=tuple(app_ids))


def _all_directories_needed(relatives: Iterable[PurePosixPath]) -> list[PurePosixPath]:
    """Every planned leaf folder plus every ancestor it needs, parents first.

    Expanding ancestors explicitly (rather than `mkdir(parents=True)`) is
    what lets `build_folders` know about, and chown, every folder it
    creates - an implicitly-created parent would otherwise never appear in
    `report.created` and would silently keep the wrong ownership.
    """
    needed: set[PurePosixPath] = set()
    for relative in relatives:
        parts = relative.parts
        for depth in range(1, len(parts) + 1):
            needed.add(PurePosixPath(*parts[:depth]))
    return sorted(needed, key=lambda candidate: len(candidate.parts))


def build_folders(
    settings: Settings,
    root: PurePosixPath,
    app_ids: Iterable[str],
    puid: int,
    pgid: int,
    *,
    chown: ChownFn = os.chown,
    chmod: ChmodFn = os.chmod,
) -> FolderReport:
    """Create the missing parts of the planned tree, and chown only those.

    A folder that already existed before this call is never touched again -
    chowning it would be modifying something on the owner's drive that
    Marrquee did not create.
    """
    container_root = to_host_view(settings, str(root))

    created: list[Path] = []
    for relative in _all_directories_needed(plan_folders(app_ids)):
        target = _safe_join(container_root, relative)
        if not target.exists():
            target.mkdir()
            created.append(target)

    for target in created:
        chown(target, puid, pgid)
        chmod(target, 0o775)

    return FolderReport(created=tuple(created))


def host_timezone(settings: Settings) -> str:
    """The host's own time zone name, or the fallback - never raises.

    Reads `<host_mount>/etc/timezone`, a file the install only mounts when
    the owner's whole NAS is shared in - now that the install file mounts
    just the chosen drive(s), it is almost never there, which is exactly why
    the wizard now asks instead of trusting this alone. Split out of
    `derive_ids` so the drive screen can offer the same answer as a pre-fill
    without deriving PUID/PGID for a root that hasn't been chosen yet.
    """
    timezone_path = settings.host_mount / "etc" / "timezone"
    try:
        timezone = timezone_path.read_text().strip()
    except OSError:
        return _FALLBACK_TIMEZONE
    return timezone or _FALLBACK_TIMEZONE


def derive_ids(settings: Settings, root: PurePosixPath) -> DerivedIds:
    """PUID/PGID from the chosen root's own owner, a fixed UMASK, and the host's TZ.

    A root owned by `root` (uid 0) is treated the same as one we can't stat
    at all: falling back to 1000:1000 avoids ever handing the arr
    containers root-owned files by accident.
    """
    container_root = to_host_view(settings, str(root))
    try:
        stat_result = os.stat(container_root)
        puid, pgid = stat_result.st_uid, stat_result.st_gid
    except OSError:
        puid = pgid = _FALLBACK_ID

    if puid == 0:
        puid = pgid = _FALLBACK_ID

    return DerivedIds(puid=puid, pgid=pgid, umask=_DEFAULT_UMASK, timezone=host_timezone(settings))
