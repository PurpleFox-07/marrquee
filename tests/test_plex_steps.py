"""Tests for Plex's own wiring steps: the Movies and TV Shows libraries, and
the server-wide "never transcode video" setting - each proven with
`FakePlexServer`, no network and no real Plex.
"""

from __future__ import annotations

from marrquee import words
from marrquee.plex import FakePlexServer, PlexResponse
from marrquee.wiring.plex_steps import (
    PLEX_DIRECT_PLAY_PREF,
    PLEX_LIBRARIES,
    ensure_plex_direct_play,
    ensure_plex_libraries,
)
from marrquee.wiring.steps import StepOutcome

_BASE_URL = "http://192.168.1.10:32400"
_TOKEN = "owner-token"

_LIBRARY_SECTIONS_PATH = "/library/sections"
_PREFS_PATH = "/:/prefs"


def _ok(payload: object, status: int = 200) -> PlexResponse:
    return PlexResponse(ok=True, status=status, payload=payload, detail=None)


def _failed(status: int, detail: str | None = None) -> PlexResponse:
    return PlexResponse(ok=False, status=status, payload=None, detail=detail)


def _no_libraries() -> PlexResponse:
    return _ok({"MediaContainer": {"Directory": []}})


def _library(plex_type: str, path: str, *, title: str | None = None) -> dict[str, object]:
    entry: dict[str, object] = {"type": plex_type, "Location": [{"path": path}]}
    if title is not None:
        entry["title"] = title
    return entry


# --- ensure_plex_libraries ---------------------------------------------------


async def test_libraries_are_created_with_the_exact_params() -> None:
    """FIRST: an empty Plex sends the exact POST params for Movies, then TV
    Shows - the shape python-plexapi and the current agent/scanner names use.
    """
    server = FakePlexServer(
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_no_libraries()],
            ("POST", _LIBRARY_SECTIONS_PATH): [_ok(None), _ok(None)],
        }
    )

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome.state == "done"
    assert outcome.changed is True
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 2
    assert list(posts[0][2]) == [
        ("name", "Movies"),
        ("type", "movie"),
        ("agent", "tv.plex.agents.movie"),
        ("scanner", "Plex Movie"),
        ("language", "en-US"),
        ("location", "/data/media/movies"),
    ]
    assert list(posts[1][2]) == [
        ("name", "TV Shows"),
        ("type", "show"),
        ("agent", "tv.plex.agents.series"),
        ("scanner", "Plex TV Series"),
        ("language", "en-US"),
        ("location", "/data/media/tv"),
    ]


async def test_an_existing_movie_library_is_not_duplicated_even_when_renamed() -> None:
    existing = _ok(
        {
            "MediaContainer": {
                "Directory": [_library("movie", "/data/media/movies", title="My Films")]
            }
        }
    )
    server = FakePlexServer(
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [existing],
            ("POST", _LIBRARY_SECTIONS_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome.state == "done"
    assert outcome.changed is True  # TV Shows still needs creating
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert dict(posts[0][2])["name"] == "TV Shows"


async def test_both_libraries_already_present_are_already_connected() -> None:
    both = _ok(
        {
            "MediaContainer": {
                "Directory": [
                    _library("movie", "/data/media/movies"),
                    _library("show", "/data/media/tv"),
                ]
            }
        }
    )
    server = FakePlexServer(script={("GET", _LIBRARY_SECTIONS_PATH): [both]})

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome == StepOutcome(
        state="done",
        note=words.WIRING_NOTE_ALREADY_CONNECTED,
        technical=None,
        changed=False,
        transient=False,
    )
    assert not any(call[0] == "POST" for call in server.calls)


async def test_a_wrong_typed_directory_at_the_same_path_does_not_count() -> None:
    """A `show` library happening to share the movies folder's path must
    never be mistaken for the Movies library - the unit is (type, path).
    """
    wrong_type = _ok({"MediaContainer": {"Directory": [_library("show", "/data/media/movies")]}})
    server = FakePlexServer(
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [wrong_type],
            ("POST", _LIBRARY_SECTIONS_PATH): [_ok(None), _ok(None)],
        }
    )

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome.changed is True
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 2


async def test_library_read_failure_is_unreachable() -> None:
    server = FakePlexServer(script={("GET", _LIBRARY_SECTIONS_PATH): [_failed(0)]})

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Plex")


async def test_library_write_failure_that_is_5xx_is_transient() -> None:
    server = FakePlexServer(
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_no_libraries()],
            ("POST", _LIBRARY_SECTIONS_PATH): [_failed(503)],
        }
    )

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Plex")


async def test_library_write_failure_that_is_4xx_is_refused() -> None:
    server = FakePlexServer(
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_no_libraries()],
            ("POST", _LIBRARY_SECTIONS_PATH): [_failed(400)],
        }
    )

    outcome = await ensure_plex_libraries(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_refused("Plex")
    assert "Movies" in (outcome.technical or "")


def test_plex_libraries_are_movies_then_tv_shows() -> None:
    assert [spec.title for spec in PLEX_LIBRARIES] == [
        words.PLEX_LIBRARY_MOVIES,
        words.PLEX_LIBRARY_TV,
    ]
    assert [spec.media_folder for spec in PLEX_LIBRARIES] == ["movies", "tv"]
    assert [spec.plex_type for spec in PLEX_LIBRARIES] == ["movie", "show"]


# --- ensure_plex_direct_play -------------------------------------------------


def _prefs(value: object) -> PlexResponse:
    return _ok({"MediaContainer": {"Setting": [{"id": PLEX_DIRECT_PLAY_PREF, "value": value}]}})


def _prefs_missing() -> PlexResponse:
    return _ok({"MediaContainer": {"Setting": []}})


async def test_direct_play_already_on_sends_no_put() -> None:
    server = FakePlexServer(script={("GET", _PREFS_PATH): [_prefs(True)]})

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome == StepOutcome(
        state="done",
        note=words.WIRING_NOTE_ALREADY_CONNECTED,
        technical=None,
        changed=False,
        transient=False,
    )
    assert not any(call[0] == "PUT" for call in server.calls)


async def test_direct_play_put_then_readback_true_is_done_with_the_honesty_note() -> None:
    server = FakePlexServer(
        script={
            ("GET", _PREFS_PATH): [_prefs(False), _prefs(True)],
            ("PUT", _PREFS_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome.state == "done"
    assert outcome.changed is True
    assert outcome.note == words.PLEX_NOTE_DIRECT_PLAY
    puts = [call for call in server.calls if call[0] == "PUT"]
    assert len(puts) == 1
    assert puts[0][2] == ((PLEX_DIRECT_PLAY_PREF, "1"),)


async def test_direct_play_readback_false_is_an_error_naming_the_pref() -> None:
    server = FakePlexServer(
        script={
            ("GET", _PREFS_PATH): [_prefs(False), _prefs(False)],
            ("PUT", _PREFS_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_refused("Plex")
    assert PLEX_DIRECT_PLAY_PREF in (outcome.technical or "")


async def test_direct_play_readback_missing_setting_is_an_error() -> None:
    server = FakePlexServer(
        script={
            ("GET", _PREFS_PATH): [_prefs(False), _prefs_missing()],
            ("PUT", _PREFS_PATH): [_ok(None)],
        }
    )

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert PLEX_DIRECT_PLAY_PREF in (outcome.technical or "")


async def test_direct_play_read_failure_is_unreachable() -> None:
    server = FakePlexServer(script={("GET", _PREFS_PATH): [_failed(0)]})

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Plex")


async def test_a_401_response_is_transient() -> None:
    server = FakePlexServer(script={("GET", _PREFS_PATH): [_failed(401)]})

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is True


async def test_direct_play_write_failure_that_is_5xx_is_transient() -> None:
    server = FakePlexServer(
        script={
            ("GET", _PREFS_PATH): [_prefs(False)],
            ("PUT", _PREFS_PATH): [_failed(503)],
        }
    )

    outcome = await ensure_plex_direct_play(server, _BASE_URL, _TOKEN)

    assert outcome.state == "error"
    assert outcome.transient is True
