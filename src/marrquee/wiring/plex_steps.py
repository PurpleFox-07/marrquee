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
from typing import Final, Literal

from marrquee.plex import PlexResponse, PlexServer
from marrquee.storage import container_media_path
from marrquee.wiring.steps import StepOutcome
from marrquee.words import (
    PLEX_LIBRARY_MOVIES,
    PLEX_LIBRARY_TV,
    PLEX_NOTE_DIRECT_PLAY,
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


def _directories(payload: object) -> list[Mapping[str, object]]:
    if not isinstance(payload, dict):
        return []
    container = payload.get("MediaContainer")
    if not isinstance(container, dict):
        return []
    directories = container.get("Directory")
    if not isinstance(directories, list):
        return []
    return [entry for entry in directories if isinstance(entry, dict)]


def _library_exists(directories: list[Mapping[str, object]], spec: PlexLibrarySpec) -> bool:
    target = str(container_media_path(spec.media_folder))
    for directory in directories:
        if directory.get("type") != spec.plex_type:
            continue
        locations = directory.get("Location")
        if not isinstance(locations, list):
            continue
        if any(isinstance(entry, dict) and entry.get("path") == target for entry in locations):
            return True
    return False


async def ensure_plex_libraries(server: PlexServer, base_url: str, token: str) -> StepOutcome:
    """Make sure Plex has a Movies and a TV Shows library, each pointing at
    the shared data root's own media folder, creating only what's missing.

    A library is found by (type, folder path), never by name - a library the
    owner renamed in Plex is still recognised and left alone.
    """
    listing = await server.request("GET", base_url, _LIBRARY_SECTIONS_PATH, token)
    if not listing.ok:
        return _plex_read_failure(listing)

    directories = _directories(listing.payload)
    changed = False
    for spec in PLEX_LIBRARIES:
        if _library_exists(directories, spec):
            continue
        created = await server.request(
            "POST",
            base_url,
            _LIBRARY_SECTIONS_PATH,
            token,
            params=(
                ("name", spec.title),
                ("type", spec.plex_type),
                ("agent", spec.agent),
                ("scanner", spec.scanner),
                ("language", _LIBRARY_LANGUAGE),
                ("location", str(container_media_path(spec.media_folder))),
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
