"""Tests for Jellyfin's own wiring steps: the Movies and TV Shows libraries,
and the VA-API graphics setting - each proven with `FakeJellyfinServer`, no
network and no real Jellyfin.
"""

from __future__ import annotations

from marrquee import words
from marrquee.graphics_chip import GRAPHICS_DEVICE_NODE
from marrquee.jellyfin import FakeJellyfinServer, JellyfinResponse
from marrquee.wiring.jellyfin_steps import (
    JELLYFIN_HW_ACCEL,
    JELLYFIN_HW_DECODING_CODECS,
    JELLYFIN_LIBRARIES,
    ensure_jellyfin_graphics,
    ensure_jellyfin_libraries,
)
from marrquee.wiring.steps import StepOutcome

_BASE_URL = "http://192.168.1.10:8096"
_KEY = "admin-key"

_VIRTUAL_FOLDERS_PATH = "/Library/VirtualFolders"
_ENCODING_PATH = "/System/Configuration/encoding"


def _ok(payload: object, status: int = 200) -> JellyfinResponse:
    return JellyfinResponse(ok=True, status=status, payload=payload, detail=None)


def _failed(status: int, detail: str | None = None) -> JellyfinResponse:
    return JellyfinResponse(ok=False, status=status, payload=None, detail=detail)


def _no_libraries() -> JellyfinResponse:
    return _ok([])


def _library(
    collection_type: str | None, *locations: str, name: str = "Untitled"
) -> dict[str, object]:
    return {
        "Name": name,
        "ItemId": "1",
        "CollectionType": collection_type,
        "Locations": list(locations),
    }


# --- ensure_jellyfin_libraries ------------------------------------------------


async def test_libraries_are_created_with_the_exact_params() -> None:
    """FIRST: an empty Jellyfin sends the exact POST params for Movies, then
    TV Shows, each with an empty `LibraryOptions` body.
    """
    server = FakeJellyfinServer(
        script={
            ("GET", _VIRTUAL_FOLDERS_PATH): [_no_libraries()],
            ("POST", _VIRTUAL_FOLDERS_PATH): [_ok(None), _ok(None)],
        }
    )

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.state == "done"
    assert outcome.changed is True
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 2
    assert list(posts[0][2]) == [
        ("name", "Movies"),
        ("collectionType", "movies"),
        ("paths", "/data/media/movies"),
        ("refreshLibrary", "true"),
    ]
    assert list(posts[1][2]) == [
        ("name", "TV Shows"),
        ("collectionType", "tvshows"),
        ("paths", "/data/media/tv"),
        ("refreshLibrary", "true"),
    ]
    assert server.bodies == [None, {"LibraryOptions": {}}, {"LibraryOptions": {}}]
    assert all(call[3] for call in server.calls)  # every call carried a token


async def test_an_existing_movie_library_is_not_duplicated_even_when_renamed() -> None:
    existing = _ok([_library("movies", "/data/media/movies", name="My Films")])
    server = FakeJellyfinServer(
        script={
            ("GET", _VIRTUAL_FOLDERS_PATH): [existing],
            ("POST", _VIRTUAL_FOLDERS_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.state == "done"
    assert outcome.changed is True  # TV Shows still needs creating
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert dict(posts[0][2])["name"] == "TV Shows"


async def test_both_libraries_already_present_are_already_connected() -> None:
    both = _ok(
        [
            _library("movies", "/data/media/movies"),
            _library("tvshows", "/data/media/tv"),
        ]
    )
    server = FakeJellyfinServer(script={("GET", _VIRTUAL_FOLDERS_PATH): [both]})

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome == StepOutcome(
        state="done",
        note=words.WIRING_NOTE_ALREADY_CONNECTED,
        technical=None,
        changed=False,
        transient=False,
    )
    assert not any(call[0] == "POST" for call in server.calls)


async def test_a_wrong_typed_entry_at_the_same_path_does_not_count() -> None:
    """A `tvshows` entry happening to share the movies folder's path must
    never be mistaken for the Movies library - the unit is (type, path).
    """
    wrong_type = _ok([_library("tvshows", "/data/media/movies")])
    server = FakeJellyfinServer(
        script={
            ("GET", _VIRTUAL_FOLDERS_PATH): [wrong_type],
            ("POST", _VIRTUAL_FOLDERS_PATH): [_ok(None), _ok(None)],
        }
    )

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.changed is True
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 2


async def test_library_listing_failure_that_is_zero_is_transient_and_unreachable() -> None:
    server = FakeJellyfinServer(script={("GET", _VIRTUAL_FOLDERS_PATH): [_failed(0)]})

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Jellyfin")


async def test_library_listing_failure_that_is_401_is_refused_and_never_leaks_the_key() -> None:
    server = FakeJellyfinServer(script={("GET", _VIRTUAL_FOLDERS_PATH): [_failed(401)]})

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_refused("Jellyfin")
    assert _KEY not in (outcome.technical or "")


async def test_library_write_failure_that_is_5xx_is_transient() -> None:
    server = FakeJellyfinServer(
        script={
            ("GET", _VIRTUAL_FOLDERS_PATH): [_no_libraries()],
            ("POST", _VIRTUAL_FOLDERS_PATH): [_failed(503)],
        }
    )

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Jellyfin")


async def test_library_write_failure_that_is_4xx_is_refused() -> None:
    server = FakeJellyfinServer(
        script={
            ("GET", _VIRTUAL_FOLDERS_PATH): [_no_libraries()],
            ("POST", _VIRTUAL_FOLDERS_PATH): [_failed(400)],
        }
    )

    outcome = await ensure_jellyfin_libraries(server, _BASE_URL, _KEY)

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_refused("Jellyfin")
    assert "Movies" in (outcome.technical or "")


def test_jellyfin_libraries_are_movies_then_tv_shows() -> None:
    assert [spec.title for spec in JELLYFIN_LIBRARIES] == [
        words.JELLYFIN_LIBRARY_MOVIES,
        words.JELLYFIN_LIBRARY_TV,
    ]
    assert [spec.media_folder for spec in JELLYFIN_LIBRARIES] == ["movies", "tv"]
    assert [spec.collection_type for spec in JELLYFIN_LIBRARIES] == ["movies", "tvshows"]


# --- ensure_jellyfin_graphics -------------------------------------------------


def _encoding(hw_accel: object, device: object = GRAPHICS_DEVICE_NODE) -> JellyfinResponse:
    return _ok({"HardwareAccelerationType": hw_accel, "VaapiDevice": device, "Other": "kept"})


async def test_graphics_already_vaapi_sends_no_post() -> None:
    server = FakeJellyfinServer(script={("GET", _ENCODING_PATH): [_encoding("vaapi")]})

    outcome = await ensure_jellyfin_graphics(server, _BASE_URL, _KEY)

    assert outcome == StepOutcome(
        state="done",
        note=words.WIRING_NOTE_ALREADY_CONNECTED,
        technical=None,
        changed=False,
        transient=False,
    )
    assert not any(call[0] == "POST" for call in server.calls)


async def test_graphics_accepts_the_integer_five_as_vaapi() -> None:
    server = FakeJellyfinServer(script={("GET", _ENCODING_PATH): [_encoding(5)]})

    outcome = await ensure_jellyfin_graphics(server, _BASE_URL, _KEY)

    assert outcome.state == "done"
    assert outcome.changed is False
    assert not any(call[0] == "POST" for call in server.calls)


async def test_graphics_wrong_device_is_not_already_set() -> None:
    server = FakeJellyfinServer(
        script={
            ("GET", _ENCODING_PATH): [
                _encoding("vaapi", device="/dev/dri/renderD129"),
                _encoding("vaapi"),
            ],
            ("POST", _ENCODING_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_jellyfin_graphics(server, _BASE_URL, _KEY)

    assert outcome.state == "done"
    assert outcome.changed is True


async def test_graphics_post_then_readback_vaapi_is_done_and_keeps_other_settings() -> None:
    server = FakeJellyfinServer(
        script={
            ("GET", _ENCODING_PATH): [_encoding("none"), _encoding("vaapi")],
            ("POST", _ENCODING_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_jellyfin_graphics(server, _BASE_URL, _KEY)

    assert outcome.state == "done"
    assert outcome.changed is True
    assert outcome.note == words.JELLYFIN_NOTE_GRAPHICS
    posted = server.bodies[1]
    assert isinstance(posted, dict)
    assert posted["HardwareAccelerationType"] == JELLYFIN_HW_ACCEL
    assert posted["VaapiDevice"] == GRAPHICS_DEVICE_NODE
    assert posted["EnableHardwareEncoding"] is True
    assert posted["HardwareDecodingCodecs"] == list(JELLYFIN_HW_DECODING_CODECS)
    assert posted["Other"] == "kept"  # a read-modify-write, not a fresh object


async def test_graphics_readback_that_is_still_none_is_an_error_naming_the_setting() -> None:
    server = FakeJellyfinServer(
        script={
            ("GET", _ENCODING_PATH): [_encoding("none"), _encoding("none")],
            ("POST", _ENCODING_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_jellyfin_graphics(server, _BASE_URL, _KEY)

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_refused("Jellyfin")
    assert "HardwareAccelerationType" in (outcome.technical or "")


async def test_graphics_read_failure_is_unreachable_when_transient() -> None:
    server = FakeJellyfinServer(script={("GET", _ENCODING_PATH): [_failed(0)]})

    outcome = await ensure_jellyfin_graphics(server, _BASE_URL, _KEY)

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Jellyfin")
