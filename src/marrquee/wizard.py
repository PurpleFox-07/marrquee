"""The wizard's pure presentation logic - which apps, which platform warning,
how much room in plain words, which sentence answers which storage check.

Nothing here touches FastAPI, a template or the filesystem: every function
takes plain values in and returns plain values out, so the two screens'
routes (in `routes/wizard.py`) are thin - they call these functions and
render the result - and every decision they make can be tested without a
running app or a real folder.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from marrquee import words
from marrquee.catalog import CATALOG, apps_in_order
from marrquee.config import Settings
from marrquee.docker_client import HostKind
from marrquee.state import InstallState
from marrquee.storage import (
    FreshnessCheck,
    StorageCheck,
    check_fresh_start,
    check_storage_root,
    host_timezone,
    shared_roots,
)

DEFAULT_APP_IDS: tuple[str, ...] = tuple(app.id for app in CATALOG)


def parse_app_ids(raw: str | Iterable[str]) -> tuple[str, ...]:
    """A csv string or any sequence of ids, turned into catalog order.

    Never raises: the wizard calls this on both a submitted form and a live
    keystroke re-check, and an exception in either is a broken screen, not
    a helpful one. Unknown ids and duplicates are dropped rather than
    flagged - `apps_in_order` does that filtering for every other caller
    in the codebase, and the wizard follows the same rule.
    """
    candidates = raw.split(",") if isinstance(raw, str) else raw
    cleaned = (candidate.strip() for candidate in candidates)
    return tuple(app.id for app in apps_in_order(candidate for candidate in cleaned if candidate))


@dataclass(frozen=True)
class WizardStep:
    """One pill in the three-step progress row both screens share."""

    number: int
    label: str


WIZARD_STEPS: tuple[WizardStep, ...] = (
    WizardStep(1, words.WIZARD_STEP_APPS),
    WizardStep(2, words.WIZARD_STEP_DRIVE),
    WizardStep(3, words.WIZARD_STEP_DEPLOY),
)


def platform_warning(kind: HostKind) -> str | None:
    """The Docker Desktop caution, or nothing - the warning is Docker
    Desktop-specific, never shown for a NAS, a plain Linux box, or a host
    kind Marrquee doesn't recognise.
    """
    if kind == "docker_desktop":
        return words.WIZARD_PLATFORM_WARNING
    return None


def free_space_words(free_bytes: int) -> str:
    """The free-space phrase for a byte count, in the units an owner's own
    NAS dashboard already uses: one decimal past a terabyte, a whole number
    of gigabytes above that, and a plain "less than 1 GB" below it.
    """
    if free_bytes >= 10**12:
        return words.free_space_terabytes(free_bytes / 10**12)
    if free_bytes >= 10**9:
        return words.free_space_gigabytes(round(free_bytes / 10**9))
    return words.FREE_SPACE_UNDER_ONE_GB


@dataclass(frozen=True)
class DriveMessage:
    """The one sentence (and, sometimes, a one-click fix) a storage check
    earns - the shape the server-rendered result and the live JSON check
    both carry.
    """

    tone: Literal["ok", "problem", "unknown"]
    glyph: str
    text: str
    suggestion: str | None
    guidance: str | None


_GLYPH_OK = "✓"
_GLYPH_PROBLEM = "✕"
_GLYPH_UNKNOWN = "…"


def _problem(text: str) -> DriveMessage:
    return DriveMessage(
        tone="problem", glyph=_GLYPH_PROBLEM, text=text, suggestion=None, guidance=None
    )


def _first_segment(path: str) -> str:
    """The first real folder of a host path, e.g. "/volume2/media" -> "/volume2"."""
    parts = PurePosixPath(path).parts
    return f"/{parts[1]}" if len(parts) > 1 else path


def drive_message(
    check: StorageCheck,
    freshness: FreshnessCheck | None = None,
    *,
    shared: Sequence[PurePosixPath] = (),
) -> DriveMessage:
    """The right words for a `StorageCheck`, pure and total.

    Never reads `check.detail` - that field is a raw technical string for
    logs only, and putting it in front of the owner is exactly the mistake
    this function exists to prevent. `freshness` is only meaningful when
    `check` itself is ok; a populated target outranks an otherwise-clean
    folder because a library already there is worse news than a slow disk.
    """
    path = str(check.host_path) if check.host_path is not None else ""

    if check.reason == "empty":
        return _problem(words.REFUSAL_PATH_EMPTY)
    if check.reason == "not_absolute":
        return _problem(words.WIZARD_NOT_WHOLE_PATH)
    if check.reason == "system_path":
        return _problem(words.refusal_system_path(path))
    if check.reason == "not_a_folder":
        return _problem(words.refusal_not_a_folder(path))
    if check.reason == "not_writable":
        return _problem(words.refusal_not_writable(path))

    if check.reason in ("missing", "not_shared") and check.suggested_path is not None:
        suggestion = str(check.suggested_path)
        guidance = (
            words.wizard_other_drive_hint(_first_segment(path))
            if check.reason == "not_shared"
            else None
        )
        return DriveMessage(
            tone="problem",
            glyph=_GLYPH_PROBLEM,
            text=words.wizard_did_you_mean(suggestion),
            suggestion=suggestion,
            guidance=guidance,
        )
    if check.reason == "missing":
        return _problem(words.WIZARD_MISSING_NO_SUGGESTION)
    if check.reason == "not_shared":
        shared_names = [str(root) for root in shared]
        return _problem(words.refusal_not_shared(path, shared_names))

    # check.reason is None past this point: the folder itself checked out.
    if freshness is not None and not freshness.ok:
        return _problem(words.refusal_populated_target(path))
    if check.free_bytes is None:
        return DriveMessage(
            tone="unknown",
            glyph=_GLYPH_UNKNOWN,
            text=words.WIZARD_FOUND_ROOM_UNKNOWN,
            suggestion=None,
            guidance=None,
        )
    return DriveMessage(
        tone="ok",
        glyph=_GLYPH_OK,
        text=words.wizard_folder_found(free_space_words(check.free_bytes)),
        suggestion=None,
        guidance=None,
    )


def assess(settings: Settings, typed: str, app_ids: Sequence[str]) -> tuple[bool, DriveMessage]:
    """Check a typed path the one way the form post and the live check agree on.

    Never raises: `check_storage_root` and `check_fresh_start` are both
    total, and this function stays total too so a bad path is always a kind
    sentence, never a broken screen.
    """
    check = check_storage_root(settings, typed)
    freshness: FreshnessCheck | None = None
    if check.ok and app_ids and check.host_path is not None:
        freshness = check_fresh_start(settings, check.host_path, app_ids)

    usable = check.ok and (freshness is None or freshness.ok)
    message = drive_message(check, freshness, shared=shared_roots(settings))
    return usable, message


# --- Time zone: a fixed list everywhere, old names hidden -------------------

# The nine time zone database regions the dropdown groups by - fixed and
# alphabetical, so the group order on screen never depends on what happens
# to be in the file this run. "Etc" is deliberately excluded: its one
# offered zone (UTC) gets its own "Other" group instead, built by hand
# below, rather than reading the database's much longer Etc/GMT+N list.
_TIMEZONE_REGIONS: tuple[str, ...] = (
    "Africa",
    "America",
    "Antarctica",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)

_UTC_KEY = "Etc/UTC"


def _load_timezone_data() -> tuple[frozenset[str], dict[str, str]]:
    """Read `tzdata`'s own zone table once, at import, into two fixed maps.

    `tzdata.zi`'s `Z` lines are the database's canonical zones and its `L`
    lines are backward-compatible names for one of them (`L Asia/Kolkata
    Asia/Calcutta` reads as "Asia/Calcutta is an old name for Asia/Kolkata").
    The owner chose to hide those old names from the dropdown rather than
    list a place twice, so only `Z` names become `TIMEZONE_KEYS`, and every
    `L` name whose region the dropdown offers becomes a `TIMEZONE_ALIASES`
    entry instead - reachable by a saved or posted old name, but never shown
    as its own option.
    """
    zi_path = importlib.resources.files("tzdata").joinpath("zoneinfo", "tzdata.zi")
    keys: set[str] = {_UTC_KEY}
    aliases: dict[str, str] = {}

    for line in zi_path.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "Z" and len(fields) >= 2:
            name = fields[1]
            if name.split("/", 1)[0] in _TIMEZONE_REGIONS:
                keys.add(name)
        elif fields[0] == "L" and len(fields) == 3:
            target, link = fields[1], fields[2]
            if link.split("/", 1)[0] in _TIMEZONE_REGIONS:
                aliases[link] = target

    return frozenset(keys), aliases


TIMEZONE_KEYS, TIMEZONE_ALIASES = _load_timezone_data()


def timezone_choice(raw: str | None, default: str) -> str:
    """`raw` translated through `TIMEZONE_ALIASES` and checked against
    `TIMEZONE_KEYS`, or `default` when it names nothing Marrquee offers.

    Never raises - a posted or saved zone this build doesn't recognise (an
    old name from before `tzdata` was pinned, a typo, an empty field) is
    simply not good enough to keep, not a reason to fail the whole save.
    """
    if not raw:
        return default
    candidate = TIMEZONE_ALIASES.get(raw, raw)
    return candidate if candidate in TIMEZONE_KEYS else default


def default_timezone(
    settings: Settings, state: InstallState | None
) -> tuple[str, Literal["saved", "host"]]:
    """The zone to pre-fill with, and where it came from.

    A returning owner's saved choice outranks any guess - it is the one
    answer that is definitely right. Only when there is nothing saved does
    the host's own time zone file get a say (rarely mounted, so this is
    usually absent), and only when even that names nothing Marrquee offers
    does the select fall back to plain UTC.
    """
    if state is not None and state.timezone in TIMEZONE_KEYS:
        return state.timezone, "saved"

    host_zone = host_timezone(settings)
    if host_zone in TIMEZONE_KEYS:
        return host_zone, "host"
    return _UTC_KEY, "host"


@dataclass(frozen=True)
class TimezoneGroup:
    """One `<optgroup>` the drive screen's time zone select renders."""

    label: str
    options: tuple[tuple[str, str], ...]


def _timezone_option_label(key: str) -> str:
    """A zone key with its region dropped and its separators turned to
    prose - "America/Argentina/Buenos_Aires" reads as "Argentina - Buenos
    Aires", a place name rather than a database path.
    """
    _region, _, rest = key.partition("/")
    return rest.replace("/", " - ").replace("_", " ")


def timezone_groups() -> tuple[TimezoneGroup, ...]:
    """Every offered zone, grouped and labelled the way the select renders it."""
    groups = []
    for region in _TIMEZONE_REGIONS:
        prefix = f"{region}/"
        options = tuple(
            sorted(
                (
                    (key, _timezone_option_label(key))
                    for key in TIMEZONE_KEYS
                    if key.startswith(prefix)
                ),
                key=lambda option: option[1],
            )
        )
        groups.append(TimezoneGroup(label=words.TIMEZONE_REGION_LABELS[region], options=options))

    groups.append(
        TimezoneGroup(
            label=words.TIMEZONE_REGION_LABELS["Other"],
            options=((_UTC_KEY, words.WIZARD_TIMEZONE_UTC),),
        )
    )
    return tuple(groups)
