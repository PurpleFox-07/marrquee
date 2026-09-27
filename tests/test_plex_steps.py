"""Tests for Plex's own wiring steps: the Movies and TV Shows libraries, and
the server-wide "never transcode video" setting - each proven with
`FakePlexServer`, no network and no real Plex.
"""

from __future__ import annotations

import secrets
from pathlib import Path, PurePosixPath

import pytest

from marrquee import words
from marrquee.config import Settings
from marrquee.plex import (
    PROBE_FOLDER_PREFIX,
    ExistingPlex,
    FakePlexServer,
    PlexIdentity,
    PlexResponse,
    browse_path,
    load_existing_plex,
    save_existing_plex,
)
from marrquee.storage import container_media_path, host_media_path
from marrquee.wiring.plex_steps import (
    PLEX_DIRECT_PLAY_PREF,
    PLEX_LIBRARIES,
    ensure_existing_plex_libraries,
    ensure_plex_direct_play,
    ensure_plex_libraries,
    plex_library_params,
)
from marrquee.wiring.steps import StepOutcome

_BASE_URL = "http://192.168.1.10:32400"
_TOKEN = "owner-token"

_LIBRARY_SECTIONS_PATH = "/library/sections"
_PREFS_PATH = "/:/prefs"

_EXISTING_BASE_URL = "http://192.168.1.20:32400"
_EXISTING_TOKEN = "owner-plex-token"


def _ok(payload: object, status: int = 200) -> PlexResponse:
    return PlexResponse(ok=True, status=status, payload=payload, detail=None)


def _failed(status: int, detail: str | None = None) -> PlexResponse:
    return PlexResponse(ok=False, status=status, payload=None, detail=detail)


def _no_libraries() -> PlexResponse:
    return _ok({"MediaContainer": {"Directory": []}})


def _library(
    plex_type: str, path: str, *, title: str | None = None, key: str = "1"
) -> dict[str, object]:
    entry: dict[str, object] = {"key": key, "type": plex_type, "Location": [{"path": path}]}
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


def test_plex_library_params_shape() -> None:
    """`ensure_plex_libraries` and the existing-Plex step both send this
    exact param shape - only the title and location ever differ between them.
    """
    spec = PLEX_LIBRARIES[0]

    assert plex_library_params(spec, title="Custom Title", location="/x/y") == [
        ("name", "Custom Title"),
        ("type", "movie"),
        ("agent", "tv.plex.agents.movie"),
        ("scanner", "Plex Movie"),
        ("language", "en-US"),
        ("location", "/x/y"),
    ]


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


# --- ensure_existing_plex_libraries: the owner's own, already-running Plex --


def _existing_plex_settings_and_root(tmp_path: Path) -> tuple[Settings, PurePosixPath]:
    (tmp_path / "volume1" / "media" / "data" / "media" / "movies").mkdir(parents=True)
    (tmp_path / "volume1" / "media" / "data" / "media" / "tv").mkdir(parents=True)
    return Settings(host_mount=tmp_path), PurePosixPath("/volume1/media")


def _existing_plex_record(**overrides: object) -> ExistingPlex:
    fields: dict[str, object] = {
        "machine_id": "m1",
        "name": "Den",
        "base_url": _EXISTING_BASE_URL,
        "port": 32400,
        "on_this_nas": False,
        "token": _EXISTING_TOKEN,
        "folders": {"movies": "unchecked", "tv": "unchecked"},
        "sections": {},
        "replaces_link": None,
    }
    fields.update(overrides)
    return ExistingPlex(**fields)  # type: ignore[arg-type]


def _section(key: str, plex_type: str, path: str, title: str) -> dict[str, object]:
    return {"key": key, "type": plex_type, "title": title, "Location": [{"path": path}]}


def _sections_payload(entries: list[dict[str, object]]) -> PlexResponse:
    return _ok({"MediaContainer": {"Directory": entries}})


def _seen_at(candidate: str, marker_name: str) -> PlexResponse:
    return _ok(
        {"MediaContainer": {"Path": [{"path": f"{candidate}/{marker_name}", "title": marker_name}]}}
    )


def _not_seen() -> PlexResponse:
    return _ok({"MediaContainer": {"Path": []}})


def _assert_only_get_and_post(server: FakePlexServer) -> None:
    assert {call[0] for call in server.calls} <= {"GET", "POST"}


async def test_adds_both_marrquee_libraries_at_the_path_plex_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIRST: both folders visible at their container paths - Plex gets both
    new libraries at the exact params, and the record remembers what was
    added along with the keys read back by a final GET.
    """
    settings, root = _existing_plex_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [
                _sections_payload([]),
                _sections_payload(
                    [
                        _section(
                            "1", "movie", container_movies, words.EXISTING_PLEX_LIBRARY_MOVIES
                        ),
                        _section("2", "show", container_tv, words.EXISTING_PLEX_LIBRARY_TV),
                    ]
                ),
            ],
            ("POST", _LIBRARY_SECTIONS_PATH): [_ok(None), _ok(None)],
            ("GET", browse_path(host_movies)): [_not_seen()],
            ("GET", browse_path(container_movies)): [_seen_at(container_movies, marker_name)],
            ("GET", browse_path(host_tv)): [_not_seen()],
            ("GET", browse_path(container_tv)): [_seen_at(container_tv, marker_name)],
        },
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "done"
    assert outcome.note == words.EXISTING_PLEX_NOTE_ADDED
    assert outcome.changed is True
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 2
    assert list(posts[0][2]) == [
        ("name", "Movies (Marrquee)"),
        ("type", "movie"),
        ("agent", "tv.plex.agents.movie"),
        ("scanner", "Plex Movie"),
        ("language", "en-US"),
        ("location", "/data/media/movies"),
    ]
    assert list(posts[1][2]) == [
        ("name", "TV Shows (Marrquee)"),
        ("type", "show"),
        ("agent", "tv.plex.agents.series"),
        ("scanner", "Plex TV Series"),
        ("language", "en-US"),
        ("location", "/data/media/tv"),
    ]
    record = load_existing_plex(config_dir)
    assert record is not None
    assert record.folders == {"movies": "added", "tv": "added"}
    assert record.sections == {"movies": "1", "tv": "2"}
    _assert_only_get_and_post(server)


async def test_a_folder_already_covered_by_an_existing_library_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing library already at the movies path covers it exactly -
    no POST for Movies, and Marrquee's library title is never even
    considered a match, only the folder location.
    """
    settings, root = _existing_plex_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    covering = _section("9", "movie", container_movies, "My Movies")
    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [
                _sections_payload([covering]),
                _sections_payload(
                    [covering, _section("5", "show", container_tv, words.EXISTING_PLEX_LIBRARY_TV)]
                ),
            ],
            ("POST", _LIBRARY_SECTIONS_PATH): [_ok(None)],
            ("GET", browse_path(host_movies)): [_not_seen()],
            ("GET", browse_path(container_movies)): [_seen_at(container_movies, marker_name)],
            ("GET", browse_path(host_tv)): [_not_seen()],
            ("GET", browse_path(container_tv)): [_seen_at(container_tv, marker_name)],
        },
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "done"
    posts = [call for call in server.calls if call[0] == "POST"]
    assert len(posts) == 1
    assert dict(posts[0][2])["name"] == "TV Shows (Marrquee)"
    record = load_existing_plex(config_dir)
    assert record is not None
    assert record.folders == {"movies": "already", "tv": "added"}
    assert record.sections == {"movies": "9", "tv": "5"}
    _assert_only_get_and_post(server)


async def test_an_ancestor_library_covers_both_folders_whatever_its_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library at the shared PARENT of both Marrquee folders already
    indexes everything under it - covered means equal-or-ancestor, whatever
    the library's own content type, so neither folder is ever duplicated.
    """
    settings, root = _existing_plex_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    covering = _section("3", "show", "/data/media", "Everything")
    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_sections_payload([covering])],
            ("GET", browse_path(host_movies)): [_not_seen()],
            ("GET", browse_path(container_movies)): [_seen_at(container_movies, marker_name)],
            ("GET", browse_path(host_tv)): [_not_seen()],
            ("GET", browse_path(container_tv)): [_seen_at(container_tv, marker_name)],
        },
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "done"
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert not any(call[0] == "POST" for call in server.calls)
    record = load_existing_plex(config_dir)
    assert record is not None
    assert record.folders == {"movies": "already", "tv": "already"}
    assert record.sections == {"movies": "3", "tv": "3"}
    _assert_only_get_and_post(server)


async def test_cannot_see_is_skipped_with_the_record_marked_not_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_sections_payload([])],
            ("GET", browse_path(host_movies)): [_not_seen()],
            ("GET", browse_path(container_movies)): [_not_seen()],
            ("GET", browse_path(host_tv)): [_not_seen()],
            ("GET", browse_path(container_tv)): [_not_seen()],
        },
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "skipped"
    assert outcome.note == words.EXISTING_PLEX_NOTE_CANT_SEE
    assert not any(call[0] == "POST" for call in server.calls)
    record = load_existing_plex(config_dir)
    assert record is not None
    assert record.folders == {"movies": "not_seen", "tv": "not_seen"}
    _assert_only_get_and_post(server)


async def test_a_different_server_at_the_saved_address_is_never_written_to(
    tmp_path: Path,
) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "not-m1")})

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_unreachable("Plex")
    assert server.calls == []
    record = load_existing_plex(config_dir)
    assert record == _existing_plex_record()


async def test_no_identity_answer_is_a_transient_error(tmp_path: Path) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(identities_by_url={})

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "error"
    assert outcome.transient is True
    assert server.calls == []


async def test_a_401_reading_sections_means_the_token_was_refused(
    tmp_path: Path,
) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={("GET", _LIBRARY_SECTIONS_PATH): [_failed(401)]},
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.EXISTING_PLEX_TOKEN_REFUSED
    _assert_only_get_and_post(server)


async def test_reading_sections_5xx_is_transient(tmp_path: Path) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={("GET", _LIBRARY_SECTIONS_PATH): [_failed(503)]},
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "error"
    assert outcome.transient is True
    assert outcome.note == words.wiring_failure_unreachable("Plex")


async def test_a_4xx_creating_a_library_is_refused_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    marker_name = f"{PROBE_FOLDER_PREFIX}deadbeef"
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))
    config_dir = tmp_path
    save_existing_plex(config_dir, _existing_plex_record())

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_sections_payload([])],
            ("POST", _LIBRARY_SECTIONS_PATH): [_failed(400)],
            ("GET", browse_path(host_movies)): [_not_seen()],
            ("GET", browse_path(container_movies)): [_seen_at(container_movies, marker_name)],
            ("GET", browse_path(host_tv)): [_not_seen()],
            ("GET", browse_path(container_tv)): [_seen_at(container_tv, marker_name)],
        },
    )

    outcome = await ensure_existing_plex_libraries(
        server, _existing_plex_record(), settings, root, config_dir
    )

    assert outcome.state == "error"
    assert outcome.transient is False
    assert outcome.note == words.wiring_failure_refused("Plex")
    assert "." not in (outcome.technical or "")


async def test_the_token_never_appears_in_any_technical_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _existing_plex_settings_and_root(tmp_path)
    monkeypatch.setattr(secrets, "token_hex", lambda n: "deadbeef")
    host_movies = str(host_media_path(str(root), "movies"))
    container_movies = str(container_media_path("movies"))
    host_tv = str(host_media_path(str(root), "tv"))
    container_tv = str(container_media_path("tv"))
    config_dir = tmp_path
    record = _existing_plex_record(token="super-secret-server-token")
    save_existing_plex(config_dir, record)

    server = FakePlexServer(
        identities_by_url={_EXISTING_BASE_URL: PlexIdentity(True, "m1")},
        script={
            ("GET", _LIBRARY_SECTIONS_PATH): [_failed(401)],
            ("GET", browse_path(host_movies)): [_not_seen()],
            ("GET", browse_path(container_movies)): [_not_seen()],
            ("GET", browse_path(host_tv)): [_not_seen()],
            ("GET", browse_path(container_tv)): [_not_seen()],
        },
    )

    outcome = await ensure_existing_plex_libraries(server, record, settings, root, config_dir)

    assert "super-secret-server-token" not in (outcome.technical or "")
    for _method, _path, params in server.calls:
        for _key, value in params:
            assert "super-secret-server-token" not in value
