"""Jellyfin's own wiring steps: the Movies and TV Shows libraries, and the
VA-API hardware-transcode setting Jellyfin uses only when the owner asked
for the graphics chip.

Mirrors `wiring/plex_steps.py`'s own shape - look before you write, and
report an honest `StepOutcome` - but talks to a `JellyfinServer` instead of
an arr app's `ArrClient` or a `PlexServer`, since Jellyfin's API shape (form
params for the library endpoint, a whole-object read-modify-write for
encoding) is its own thing. Neither function ever raises; a dead Jellyfin
or a refused setting is a real answer, never a crash.

Unlike Plex's own read failures (always "unreachable", whatever the
status), a definite Jellyfin refusal - a stale or wrong key included -
is reported as "refused", never "unreachable": `_jellyfin_failure` is the
one place that decision is made, for a read or a write alike.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from marrquee.graphics_chip import GRAPHICS_DEVICE_NODE
from marrquee.jellyfin import (
    JellyfinLibrary,
    JellyfinResponse,
    JellyfinServer,
    parse_virtual_folders,
)
from marrquee.storage import container_media_path
from marrquee.wiring.steps import StepOutcome
from marrquee.words import (
    JELLYFIN_LIBRARY_MOVIES,
    JELLYFIN_LIBRARY_TV,
    JELLYFIN_NOTE_GRAPHICS,
    WIRING_NOTE_ALREADY_CONNECTED,
    wiring_failure_refused,
    wiring_failure_unreachable,
)

_VIRTUAL_FOLDERS_PATH = "/Library/VirtualFolders"
_ENCODING_CONFIG_PATH = "/System/Configuration/encoding"

JELLYFIN_HW_ACCEL = "vaapi"
JELLYFIN_HW_DECODING_CODECS: tuple[str, ...] = ("h264", "hevc", "mpeg2video", "vc1", "vp9")

_HW_ACCEL_FIELD = "HardwareAccelerationType"
_VAAPI_DEVICE_FIELD = "VaapiDevice"


def _jellyfin_transient(status: int) -> bool:
    return status == 0 or status >= 500


def _jellyfin_technical(response: JellyfinResponse) -> str:
    return response.detail or f"HTTP {response.status}"


def _jellyfin_failure(response: JellyfinResponse, *, technical: str | None = None) -> StepOutcome:
    """The one rule every failed Jellyfin call in this module follows: a
    dead or overloaded server is worth the engine's own retry budget, while
    any considered refusal - a stale key included - is real and final.
    """
    resolved_technical = technical if technical is not None else _jellyfin_technical(response)
    if _jellyfin_transient(response.status):
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable("Jellyfin"),
            technical=resolved_technical,
            changed=False,
            transient=True,
        )
    return StepOutcome(
        state="error",
        note=wiring_failure_refused("Jellyfin"),
        technical=resolved_technical,
        changed=False,
        transient=False,
    )


# --- Libraries: Movies and TV Shows, each at most once -----------------------


@dataclass(frozen=True)
class JellyfinLibrarySpec:
    """One library Marrquee makes sure Jellyfin has - the shared media
    folder it points at, and Jellyfin's own collection-type vocabulary for
    that kind of content.
    """

    media_folder: str
    collection_type: Literal["movies", "tvshows"]
    title: str


JELLYFIN_LIBRARIES: tuple[JellyfinLibrarySpec, ...] = (
    JellyfinLibrarySpec(
        media_folder="movies", collection_type="movies", title=JELLYFIN_LIBRARY_MOVIES
    ),
    JellyfinLibrarySpec(media_folder="tv", collection_type="tvshows", title=JELLYFIN_LIBRARY_TV),
)


def _library_exists(libraries: tuple[JellyfinLibrary, ...], spec: JellyfinLibrarySpec) -> bool:
    target = str(container_media_path(spec.media_folder))
    return any(
        library.collection_type == spec.collection_type and target in library.locations
        for library in libraries
    )


async def ensure_jellyfin_libraries(
    server: JellyfinServer, base_url: str, api_key: str
) -> StepOutcome:
    """Make sure Jellyfin has a Movies and a TV Shows library, each pointing
    at the shared data root's own media folder, creating only what's
    missing.

    A library is found by (collection type, folder path), never by name -
    a library the owner renamed in Jellyfin is still recognised and left
    alone.
    """
    listing = await server.request("GET", base_url, _VIRTUAL_FOLDERS_PATH, token=api_key)
    if not listing.ok:
        return _jellyfin_failure(listing)

    libraries = parse_virtual_folders(listing.payload)
    changed = False
    for spec in JELLYFIN_LIBRARIES:
        if _library_exists(libraries, spec):
            continue
        created = await server.request(
            "POST",
            base_url,
            _VIRTUAL_FOLDERS_PATH,
            token=api_key,
            params=(
                ("name", spec.title),
                ("collectionType", spec.collection_type),
                ("paths", str(container_media_path(spec.media_folder))),
                ("refreshLibrary", "true"),
            ),
            json_body={"LibraryOptions": {}},
        )
        if not created.ok:
            return _jellyfin_failure(
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


# --- Graphics: VA-API on the exact device node, read back to prove it took --


def _reads_as_vaapi(value: object) -> bool:
    # v12.1 serializes `HardwareAccelerationType` as the string "vaapi", but
    # its enum's underlying integer form (5) is also accepted - either
    # spelling of the same setting counts as "on".
    return value == JELLYFIN_HW_ACCEL or value == 5


def _already_uses_the_chip(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        _reads_as_vaapi(payload.get(_HW_ACCEL_FIELD))
        and payload.get(_VAAPI_DEVICE_FIELD) == GRAPHICS_DEVICE_NODE
    )


async def ensure_jellyfin_graphics(
    server: JellyfinServer, base_url: str, api_key: str
) -> StepOutcome:
    """Make sure Jellyfin transcodes through VA-API on the exact device node
    a "yes" answer promised, reading the setting back after writing it -
    only the read-back is honest proof it actually took.
    """
    read = await server.request("GET", base_url, _ENCODING_CONFIG_PATH, token=api_key)
    if not read.ok:
        return _jellyfin_failure(read)

    if _already_uses_the_chip(read.payload):
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )

    # A read-modify-write: posting back only the changed fields would
    # silently drop every other encoding setting already in place, owner
    # edits to the codec list included.
    config = read.payload if isinstance(read.payload, dict) else {}
    updated = dict(config)
    updated[_HW_ACCEL_FIELD] = JELLYFIN_HW_ACCEL
    updated[_VAAPI_DEVICE_FIELD] = GRAPHICS_DEVICE_NODE
    updated["EnableHardwareEncoding"] = True
    updated["HardwareDecodingCodecs"] = list(JELLYFIN_HW_DECODING_CODECS)

    written = await server.request(
        "POST", base_url, _ENCODING_CONFIG_PATH, token=api_key, json_body=updated
    )
    if not written.ok:
        return _jellyfin_failure(written, technical=f"{_HW_ACCEL_FIELD}: HTTP {written.status}")

    confirm = await server.request("GET", base_url, _ENCODING_CONFIG_PATH, token=api_key)
    if not confirm.ok:
        return _jellyfin_failure(confirm)

    if _already_uses_the_chip(confirm.payload):
        return StepOutcome(
            state="done", note=JELLYFIN_NOTE_GRAPHICS, technical=None, changed=True, transient=False
        )

    return StepOutcome(
        state="error",
        note=wiring_failure_refused("Jellyfin"),
        technical=f"{_HW_ACCEL_FIELD}: HTTP {confirm.status}",
        changed=False,
        transient=False,
    )
