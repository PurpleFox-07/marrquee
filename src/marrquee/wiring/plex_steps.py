"""Plex's own wiring steps: the Movies and TV Shows libraries, and the
server-wide "never transcode video" setting.

Both functions here mirror `wiring/steps.py`'s own shape - look before you
write, and report an honest `StepOutcome` - but talk to a `PlexServer`
instead of an arr app's `ArrClient`, since Plex's API shape (form-encoded
query params, no JSON body) is its own thing. Neither function ever raises;
a dead Plex or a refused setting is a real answer, never a crash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from marrquee.config import Settings
from marrquee.plex import (
    ExistingPlex,
    FolderState,
    PlexResponse,
    PlexSection,
    PlexServer,
    parse_plex_sections,
    probe_folder,
    update_existing_plex,
)
from marrquee.storage import container_media_path
from marrquee.wiring.steps import StepOutcome
from marrquee.words import (
    EXISTING_PLEX_LIBRARY_MOVIES,
    EXISTING_PLEX_LIBRARY_TV,
    EXISTING_PLEX_NOTE_ADDED,
    EXISTING_PLEX_NOTE_CANT_SEE,
    EXISTING_PLEX_TOKEN_REFUSED,
    PLEX_LIBRARY_MOVIES,
    PLEX_LIBRARY_TV,
    PLEX_NOTE_DIRECT_PLAY,
    WIRING_EXISTING_PLEX_MISSING,
    WIRING_NOTE_ALREADY_CONNECTED,
    wiring_failure_refused,
    wiring_failure_unreachable,
)

_LIBRARY_SECTIONS_PATH = "/library/sections"
_PREFS_PATH = "/:/prefs"

# A first-cut default the owner can change later in Plex's own settings -
# Plex has no per-owner language preference Marrquee could read instead.
_LIBRARY_LANGUAGE: Final = "en-US"

PLEX_DIRECT_PLAY_PREF: Final = "TranscoderCanOnlyRemuxVideo"


@dataclass(frozen=True)
class PlexLibrarySpec:
    """One library Marrquee makes sure Plex has - the folder it points at,
    and the agent/scanner pair Plex itself uses for that kind of content.
    """

    media_folder: str
    plex_type: Literal["movie", "show"]
    title: str
    agent: str
    scanner: str


PLEX_LIBRARIES: tuple[PlexLibrarySpec, ...] = (
    PlexLibrarySpec(
        media_folder="movies",
        plex_type="movie",
        title=PLEX_LIBRARY_MOVIES,
        agent="tv.plex.agents.movie",
        scanner="Plex Movie",
    ),
    PlexLibrarySpec(
        media_folder="tv",
        plex_type="show",
        title=PLEX_LIBRARY_TV,
        agent="tv.plex.agents.series",
        scanner="Plex TV Series",
    ),
)


def _plex_transient(status: int) -> bool:
    # 401 is included (unlike the arr steps' own `_transient`) - the owner's
    # token may still be settling in right after the claim, so a single 401
    # is worth the engine's existing bounded retry, not an instant refusal.
    return status == 0 or status == 401 or 500 <= status < 600


def _plex_technical(response: PlexResponse) -> str:
    return response.detail or f"HTTP {response.status}"


def _plex_read_failure(response: PlexResponse) -> StepOutcome:
    return StepOutcome(
        state="error",
        note=wiring_failure_unreachable("Plex"),
        technical=_plex_technical(response),
        changed=False,
        transient=_plex_transient(response.status),
    )


def _plex_write_failure(response: PlexResponse, *, technical: str) -> StepOutcome:
    if _plex_transient(response.status):
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Plex"),
            technical=technical,
            changed=False,
            transient=True,
        )
    return StepOutcome(
        state="error",
        note=wiring_failure_refused("Plex"),
        technical=technical,
        changed=False,
        transient=False,
    )


# --- Libraries: Movies and TV Shows, each at most once -----------------------


def _library_exists(sections: tuple[PlexSection, ...], spec: PlexLibrarySpec) -> bool:
    target = str(container_media_path(spec.media_folder))
    return any(
        section.type == spec.plex_type and target in section.locations for section in sections
    )


def plex_library_params(
    spec: PlexLibrarySpec, *, title: str, location: str
) -> list[tuple[str, str]]:
    """The exact form-encoded params `POST /library/sections` wants - shared
    by both the managed Plex's own step and the existing-Plex one, so both
    ever send only one query shape.
    """
    return [
        ("name", title),
        ("type", spec.plex_type),
        ("agent", spec.agent),
        ("scanner", spec.scanner),
        ("language", _LIBRARY_LANGUAGE),
        ("location", location),
    ]


async def ensure_plex_libraries(server: PlexServer, base_url: str, token: str) -> StepOutcome:
    """Make sure Plex has a Movies and a TV Shows library, each pointing at
    the shared data root's own media folder, creating only what's missing.

    A library is found by (type, folder path), never by name - a library the
    owner renamed in Plex is still recognised and left alone.
    """
    listing = await server.request("GET", base_url, _LIBRARY_SECTIONS_PATH, token)
    if not listing.ok:
        return _plex_read_failure(listing)

    sections = parse_plex_sections(listing.payload)
    changed = False
    for spec in PLEX_LIBRARIES:
        if _library_exists(sections, spec):
            continue
        created = await server.request(
            "POST",
            base_url,
            _LIBRARY_SECTIONS_PATH,
            token,
            params=plex_library_params(
                spec, title=spec.title, location=str(container_media_path(spec.media_folder))
            ),
        )
        if not created.ok:
            return _plex_write_failure(
                created, technical=f"library {spec.title}: HTTP {created.status}"
            )
        changed = True

    if not changed:
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )
    return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)


# --- Direct play: the server-wide "never transcode video" setting -----------


def _find_pref(payload: object, pref_id: str) -> tuple[bool, object]:
    """Whether `pref_id` is present in `payload`'s `Setting[]`, and its value.

    The `bool` half of the pair is what tells a missing setting apart from
    one that's merely off - both read as "not truthy" otherwise.
    """
    if not isinstance(payload, dict):
        return False, None
    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        return False, None
    settings = container.get("Setting")
    if not isinstance(settings, list):
        return False, None
    for entry in settings:
        if isinstance(entry, dict) and entry.get("id") == pref_id:
            return True, entry.get("value")
    return False, None


def _is_truthy_pref(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return False


async def ensure_plex_direct_play(server: PlexServer, base_url: str, token: str) -> StepOutcome:
    """Make sure Plex never transcodes video server-side, reading the
    setting back after writing it - Plex Pass may gate whether this is even
    accepted, so the read-back is the only honest verdict.
    """
    read = await server.request("GET", base_url, _PREFS_PATH, token)
    if not read.ok:
        return _plex_read_failure(read)

    found, value = _find_pref(read.payload, PLEX_DIRECT_PLAY_PREF)
    if found and _is_truthy_pref(value):
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )

    written = await server.request(
        "PUT", base_url, _PREFS_PATH, token, params=((PLEX_DIRECT_PLAY_PREF, "1"),)
    )
    if not written.ok:
        return _plex_write_failure(
            written, technical=f"{PLEX_DIRECT_PLAY_PREF}: HTTP {written.status}"
        )

    confirm = await server.request("GET", base_url, _PREFS_PATH, token)
    if not confirm.ok:
        return _plex_read_failure(confirm)

    confirmed, confirmed_value = _find_pref(confirm.payload, PLEX_DIRECT_PLAY_PREF)
    if confirmed and _is_truthy_pref(confirmed_value):
        return StepOutcome(
            state="done", note=PLEX_NOTE_DIRECT_PLAY, technical=None, changed=True, transient=False
        )

    return StepOutcome(
        state="error",
        note=wiring_failure_refused("Plex"),
        technical=f"{PLEX_DIRECT_PLAY_PREF}: HTTP {confirm.status}",
        changed=False,
        transient=False,
    )


# --- The owner's OWN Plex: only where it can actually see the folder --------

# Which of Marrquee's own titles goes with which `PlexLibrarySpec` - the
# owner's Plex keeps its own naming for everything else, so these are the
# only two titles this module ever sends for an existing-Plex library.
_EXISTING_PLEX_TITLES: Final[Mapping[str, str]] = {
    "movie": EXISTING_PLEX_LIBRARY_MOVIES,
    "show": EXISTING_PLEX_LIBRARY_TV,
}


def _existing_plex_technical(action: str, response: PlexResponse) -> str:
    return f"{action}: HTTP {response.status}"


def _existing_plex_failure(response: PlexResponse, *, action: str) -> StepOutcome:
    """Classify a failed call against the owner's OWN Plex.

    Unlike the managed Plex's own `_plex_transient`, a 401 here is never
    worth retrying - it means the owner's sign-in was refused outright, not
    that Plex is merely still waking up.
    """
    if response.status == 0 or response.status >= 500:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Plex"),
            technical=_existing_plex_technical(action, response),
            changed=False,
            transient=True,
        )
    if response.status == 401:
        return StepOutcome(
            state="error",
            note=EXISTING_PLEX_TOKEN_REFUSED,
            technical=_existing_plex_technical(action, response),
            changed=False,
            transient=False,
        )
    return StepOutcome(
        state="error",
        note=wiring_failure_refused("Plex"),
        technical=_existing_plex_technical(action, response),
        changed=False,
        transient=False,
    )


def _covering_section(sections: tuple[PlexSection, ...], path: str) -> PlexSection | None:
    """The first already-existing section whose own location is `path` or an
    ancestor of it - "never touch existing libraries" means a folder already
    inside one of them is left alone, whatever that library is named.
    """
    target = PurePosixPath(path)
    for section in sections:
        for location in section.locations:
            location_path = PurePosixPath(location)
            if location_path == target or location_path in target.parents:
                return section
    return None


async def ensure_existing_plex_libraries(
    server: PlexServer,
    record: ExistingPlex,
    settings: Settings,
    root: PurePosixPath,
    config_dir: Path,
) -> StepOutcome:
    """Prove the owner's own Plex is still the one Marrquee connected to,
    then add "Movies (Marrquee)" and "TV Shows (Marrquee)" wherever that
    Plex can actually see the folder - never duplicating a folder some
    other library already covers, and never touching anything else already
    in that Plex.

    Each folder's own visibility is re-proven every run, since what that
    Plex can see may change between one wiring run and the next (the owner
    may have only just mapped the folder into its container).
    """
    identity = await server.identity(record.base_url)
    if identity is None or identity.machine_id != record.machine_id:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Plex"),
            technical=(
                "identity: no answer" if identity is None else f"identity: {identity.machine_id}"
            ),
            changed=False,
            transient=identity is None,
        )

    listing = await server.request("GET", record.base_url, _LIBRARY_SECTIONS_PATH, record.token)
    if not listing.ok:
        return _existing_plex_failure(listing, action="library sections")
    sections = parse_plex_sections(listing.payload)

    folders: dict[str, FolderState] = dict(record.folders)
    section_keys: dict[str, str] = dict(record.sections)
    added_locations: dict[str, str] = {}

    for spec in PLEX_LIBRARIES:
        seen = await probe_folder(
            server, record.base_url, record.token, settings, root, spec.media_folder
        )
        if seen.state == "unknown":
            return StepOutcome(
                state="error",
                note=wiring_failure_unreachable("Plex"),
                technical=seen.technical,
                changed=False,
                transient=True,
            )
        if seen.state == "not_seen":
            folders[spec.media_folder] = "not_seen"
            continue

        assert seen.path is not None  # "seen" always carries the path that matched
        covering = _covering_section(sections, seen.path)
        if covering is not None:
            folders[spec.media_folder] = "already"
            section_keys[spec.media_folder] = covering.key
            continue

        title = _EXISTING_PLEX_TITLES[spec.plex_type]
        created = await server.request(
            "POST",
            record.base_url,
            _LIBRARY_SECTIONS_PATH,
            record.token,
            params=plex_library_params(spec, title=title, location=seen.path),
        )
        if not created.ok:
            return _existing_plex_failure(created, action=f"library {title}")
        folders[spec.media_folder] = "added"
        added_locations[spec.media_folder] = seen.path

    if added_locations:
        refreshed = await server.request(
            "GET", record.base_url, _LIBRARY_SECTIONS_PATH, record.token
        )
        if not refreshed.ok:
            return _existing_plex_failure(refreshed, action="library sections")
        refreshed_sections = parse_plex_sections(refreshed.payload)
        for media_folder, location in added_locations.items():
            match = _covering_section(refreshed_sections, location)
            if match is not None:
                section_keys[media_folder] = match.key

    try:
        saved = update_existing_plex(config_dir, folders=folders, sections=section_keys)
    except OSError as error:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Plex"),
            technical=f"could not save: {type(error).__name__}",
            changed=False,
            transient=False,
        )
    if not saved:
        return StepOutcome(
            state="error",
            note=WIRING_EXISTING_PLEX_MISSING,
            technical="no saved existing-plex record",
            changed=False,
            transient=False,
        )

    if any(state == "not_seen" for state in folders.values()):
        return StepOutcome(
            state="skipped",
            note=EXISTING_PLEX_NOTE_CANT_SEE,
            technical=None,
            changed=bool(added_locations),
            transient=False,
        )
    if not added_locations:
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )
    return StepOutcome(
        state="done", note=EXISTING_PLEX_NOTE_ADDED, technical=None, changed=True, transient=False
    )
