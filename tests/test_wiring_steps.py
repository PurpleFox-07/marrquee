"""Tests for the two connection types: a Prowlarr application entry, and a
library (root) folder inside Sonarr or Radarr.

Every scenario runs against `FakeArrClient` - no network, no Docker, no arr
app. Each test either proves "look before you write" (a second run makes no
second write) or proves a refusal turns into plain words with the raw
detail kept in a separate field.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from marrquee import catalog, storage, words
from marrquee.wiring import steps
from marrquee.wiring.arr_client import ArrFailure, ArrResponse, FakeArrClient
from marrquee.wiring.qbit_client import FakeQbitClient, QbitResponse

PROWLARR = catalog.get_app("prowlarr")
SONARR = catalog.get_app("sonarr")
RADARR = catalog.get_app("radarr")
QBITTORRENT = catalog.get_app("qbittorrent")

PROWLARR_KEY = "p" * 32
SONARR_KEY = "s" * 32
RADARR_KEY = "r" * 32
QBIT_KEY = "qbt_" + "k" * 28

_ROOT = "/volume1/media"


def _ok(payload: object = None, status: int = 200) -> ArrResponse:
    return ArrResponse(ok=True, status=status, payload=payload, failures=(), detail=None)


def _created(payload: object = None) -> ArrResponse:
    return ArrResponse(ok=True, status=201, payload=payload, failures=(), detail=None)


def _failed(
    status: int, *, failures: tuple[ArrFailure, ...] = (), detail: str | None = None
) -> ArrResponse:
    return ArrResponse(ok=False, status=status, payload=None, failures=failures, detail=detail)


# The shape Prowlarr's `applications/schema` returns: a `disabled` template
# per implementation, an empty value per field, Prowlarr's own category
# defaults already filled in.
_SCHEMA: list[object] = [
    {
        "id": 0,
        "implementation": "Sonarr",
        "configContract": "SonarrSettings",
        "syncLevel": "disabled",
        "fields": [
            {"name": "prowlarrUrl", "value": ""},
            {"name": "baseUrl", "value": ""},
            {"name": "apiKey", "value": ""},
            {"name": "syncCategories", "value": [5000, 5010]},
        ],
    },
    {
        "id": 0,
        "implementation": "Radarr",
        "configContract": "RadarrSettings",
        "syncLevel": "disabled",
        "fields": [
            {"name": "prowlarrUrl", "value": ""},
            {"name": "baseUrl", "value": ""},
            {"name": "apiKey", "value": ""},
            {"name": "syncCategories", "value": [2000, 2010]},
        ],
    },
]


# --- ensure_root_folder --------------------------------------------------


async def test_an_existing_root_folder_is_left_alone() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _ok([{"id": 1, "path": "/data/media/tv/"}])
            ],
        }
    )

    outcome = await steps.ensure_root_folder(
        fake,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert fake.calls == [("GET", "http://sonarr:8989", "api/v3/rootfolder", None)]


async def test_a_missing_root_folder_is_created_once() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [
                _created({"id": 2, "path": "/data/media/tv"})
            ],
        }
    )

    outcome = await steps.ensure_root_folder(
        fake,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    assert fake.calls == [
        ("GET", "http://sonarr:8989", "api/v3/rootfolder", None),
        ("POST", "http://sonarr:8989", "api/v3/rootfolder", {"path": "/data/media/tv"}),
    ]


async def test_a_trailing_slash_still_counts_as_present() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://radarr:7878", "api/v3/rootfolder"): [
                _ok([{"id": 3, "path": "/data/media/movies"}])
            ],
        }
    )

    outcome = await steps.ensure_root_folder(
        fake,
        RADARR,
        RADARR_KEY,
        container_path=PurePosixPath("/data/media/movies/"),
        host_path=storage.host_media_path(_ROOT, "movies"),
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert fake.calls == [("GET", "http://radarr:7878", "api/v3/rootfolder", None)]


async def test_an_already_configured_400_is_done_not_a_failure() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [
                _failed(
                    400,
                    failures=(
                        ArrFailure("Path", "Folder already configured as a root folder", False),
                    ),
                )
            ],
        }
    )

    outcome = await steps.ensure_root_folder(
        fake,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED


async def test_a_folder_the_app_refuses_names_the_owners_own_path() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [
                _failed(
                    400,
                    failures=(ArrFailure("Path", "Folder is not writable by user abc", False),),
                )
            ],
        }
    )

    outcome = await steps.ensure_root_folder(
        fake,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )

    assert outcome.state == "error"
    assert outcome.note is not None
    assert "/volume1/media/data/media/tv" in outcome.note
    assert outcome.note.count("/data/media/tv") == 1


# --- ensure_application ----------------------------------------------------


async def test_the_application_is_created_from_the_schema_with_fullsync() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([])],
            ("GET", "http://prowlarr:9696", "api/v1/applications/schema"): [_ok(_SCHEMA)],
            ("POST", "http://prowlarr:9696", "api/v1/applications"): [_created({"id": 5})],
        }
    )

    outcome = await steps.ensure_application(fake, PROWLARR, PROWLARR_KEY, SONARR, SONARR_KEY)

    assert outcome.state == "done"
    assert outcome.changed is True
    post_calls = [call for call in fake.calls if call[0] == "POST"]
    assert len(post_calls) == 1
    body = post_calls[0][3]
    assert isinstance(body, dict)
    assert body["syncLevel"] == "fullSync"
    assert body["name"] == "Sonarr"
    assert "id" not in body
    fields = {entry["name"]: entry["value"] for entry in body["fields"]}
    assert fields["prowlarrUrl"] == steps.app_base_url(PROWLARR)
    assert fields["baseUrl"] == steps.app_base_url(SONARR)
    assert fields["apiKey"] == SONARR_KEY
    assert fields["syncCategories"] == [5000, 5010]
    assert all("forceSave" not in call[2] for call in fake.calls)


async def test_an_existing_matching_application_is_left_alone() -> None:
    existing = {
        "id": 9,
        "name": "Sonarr",
        "implementation": "Sonarr",
        "syncLevel": "fullSync",
        "fields": [
            {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
            {"name": "baseUrl", "value": "http://sonarr:8989"},
            {"name": "apiKey", "value": "********"},
        ],
    }
    fake = FakeArrClient(
        {("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([existing])]}
    )

    outcome = await steps.ensure_application(fake, PROWLARR, PROWLARR_KEY, SONARR, SONARR_KEY)

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert fake.calls == [("GET", "http://prowlarr:9696", "api/v1/applications", None)]


async def test_an_application_pointing_somewhere_else_is_repaired_with_a_put() -> None:
    existing = {
        "id": 9,
        "name": "Sonarr",
        "implementation": "Sonarr",
        "syncLevel": "addOnly",
        "fields": [
            {"name": "prowlarrUrl", "value": "http://old-prowlarr:9696"},
            {"name": "baseUrl", "value": "http://sonarr:8989"},
            {"name": "apiKey", "value": "********"},
        ],
    }
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([existing])],
            ("PUT", "http://prowlarr:9696", "api/v1/applications/9"): [_ok({"id": 9})],
        }
    )

    outcome = await steps.ensure_application(fake, PROWLARR, PROWLARR_KEY, SONARR, SONARR_KEY)

    assert outcome.state == "done"
    assert outcome.changed is True
    put_calls = [call for call in fake.calls if call[0] == "PUT"]
    assert len(put_calls) == 1
    body = put_calls[0][3]
    assert isinstance(body, dict)
    assert body["syncLevel"] == "fullSync"
    fields = {entry["name"]: entry["value"] for entry in body["fields"]}
    assert fields["apiKey"] == SONARR_KEY
    assert fields["apiKey"] != "********"
    assert fields["prowlarrUrl"] == steps.app_base_url(PROWLARR)


async def test_a_schema_prowlarr_does_not_recognise_is_reported_not_guessed() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([])],
            ("GET", "http://prowlarr:9696", "api/v1/applications/schema"): [_ok([_SCHEMA[1]])],
        }
    )

    outcome = await steps.ensure_application(fake, PROWLARR, PROWLARR_KEY, SONARR, SONARR_KEY)

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_prowlarr_too_old("Sonarr")
    assert not any(call[0] == "POST" for call in fake.calls)


@pytest.mark.parametrize(
    ("property_name", "expects_unreachable"),
    [
        ("BaseUrl", True),
        ("baseurl", True),
        ("ProwlarrUrl", True),
        ("prowlarrurl", True),
        ("ApiKey", False),
        ("Name", False),
    ],
)
async def test_400_failures_classify_by_property_name_case_insensitively(
    property_name: str, expects_unreachable: bool
) -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://prowlarr:9696", "api/v1/applications"): [_ok([])],
            ("GET", "http://prowlarr:9696", "api/v1/applications/schema"): [_ok(_SCHEMA)],
            ("POST", "http://prowlarr:9696", "api/v1/applications"): [
                _failed(400, failures=(ArrFailure(property_name, "refused", False),))
            ],
        }
    )

    outcome = await steps.ensure_application(fake, PROWLARR, PROWLARR_KEY, SONARR, SONARR_KEY)

    assert outcome.state == "error"
    expected = (
        words.wiring_failure_unreachable("Sonarr")
        if expects_unreachable
        else words.wiring_failure_refused("Sonarr")
    )
    assert outcome.note == expected


async def test_a_5xx_or_no_answer_is_marked_transient_a_400_is_not() -> None:
    fake_500 = FakeArrClient(
        {("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_failed(500, detail="boom")]}
    )
    outcome_500 = await steps.ensure_root_folder(
        fake_500,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )
    assert outcome_500.transient is True

    fake_dead = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [
                _failed(0, detail="ConnectError: nope")
            ]
        }
    )
    outcome_dead = await steps.ensure_root_folder(
        fake_dead,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )
    assert outcome_dead.transient is True

    fake_400 = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
            ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [
                _failed(400, failures=(ArrFailure("Path", "not writable", False),))
            ],
        }
    )
    outcome_400 = await steps.ensure_root_folder(
        fake_400,
        SONARR,
        SONARR_KEY,
        container_path=storage.container_media_path("tv"),
        host_path=storage.host_media_path(_ROOT, "tv"),
    )
    assert outcome_400.transient is False


async def test_no_note_contains_a_status_code_a_property_name_a_url_or_an_api_key() -> None:
    scenarios = [
        _failed(400, failures=(ArrFailure("Path", "not writable", False),)),
        _failed(500, detail="server error"),
        _failed(0, detail="ConnectError: refused"),
    ]
    notes: list[str] = []
    for response in scenarios:
        fake = FakeArrClient(
            {
                ("GET", "http://sonarr:8989", "api/v3/rootfolder"): [_ok([])],
                ("POST", "http://sonarr:8989", "api/v3/rootfolder"): [response],
            }
        )
        outcome = await steps.ensure_root_folder(
            fake,
            SONARR,
            SONARR_KEY,
            container_path=storage.container_media_path("tv"),
            host_path=storage.host_media_path(_ROOT, "tv"),
        )
        assert outcome.note is not None
        notes.append(outcome.note)

    for note in notes:
        assert "400" not in note
        assert "500" not in note
        assert "propertyName" not in note
        assert "http://" not in note
        assert SONARR_KEY not in note


def test_no_media_folder_name_is_hardcoded_in_steps_py() -> None:
    source = Path(steps.__file__).read_text()

    for literal in ('"tv"', "'tv'", '"movies"', "'movies'"):
        assert literal not in source


# --- app_base_url: an app with no network of its own rides another's ---------


def test_app_base_url_for_an_ordinary_app_is_its_own_id() -> None:
    assert steps.app_base_url(SONARR) == "http://sonarr:8989"


def test_app_base_url_for_qbittorrent_is_gluetun() -> None:
    qbittorrent = catalog.get_app("qbittorrent")

    assert steps.app_base_url(qbittorrent) == "http://gluetun:8080"


# --- ensure_qbit_preferences --------------------------------------------------


def _qbit_ok(payload: object = None, status: int = 200) -> QbitResponse:
    return QbitResponse(ok=True, status=status, payload=payload, detail=None)


def _qbit_failed(status: int, detail: str | None = None) -> QbitResponse:
    return QbitResponse(ok=False, status=status, payload=None, detail=detail)


async def test_equal_preferences_never_post() -> None:
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [
                _qbit_ok({"max_ratio_act": 0, "max_ratio": 1.0})
            ],
        }
    )

    outcome = await steps.ensure_qbit_preferences(
        fake, QBITTORRENT, QBIT_KEY, {"max_ratio_act": 0, "max_ratio": 1.0}
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert fake.calls == [("GET", "http://gluetun:8080", "api/v2/app/preferences", None)]


async def test_a_float_within_tolerance_still_counts_as_equal() -> None:
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [
                _qbit_ok({"max_ratio": 1.0000009})
            ],
        }
    )

    outcome = await steps.ensure_qbit_preferences(fake, QBITTORRENT, QBIT_KEY, {"max_ratio": 1.0})

    assert outcome.state == "done"
    assert outcome.changed is False


async def test_a_changed_preference_posts_the_new_json() -> None:
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/app/preferences"): [
                _qbit_ok({"max_seeding_time": 100})
            ],
            ("POST", "http://gluetun:8080", "api/v2/app/setPreferences"): [_qbit_ok()],
        }
    )

    outcome = await steps.ensure_qbit_preferences(
        fake, QBITTORRENT, QBIT_KEY, {"max_seeding_time": 10080}
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    post_calls = [call for call in fake.calls if call[0] == "POST"]
    assert len(post_calls) == 1
    form = post_calls[0][3]
    assert form == {"json": '{"max_seeding_time":10080}'}


async def test_preferences_read_failure_is_unreachable_against_qbittorrent() -> None:
    fake = FakeQbitClient(
        {("GET", "http://gluetun:8080", "api/v2/app/preferences"): [_qbit_failed(0, "boom")]}
    )

    outcome = await steps.ensure_qbit_preferences(fake, QBITTORRENT, QBIT_KEY, {"max_ratio": 1.0})

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_unreachable("qBittorrent")
    assert outcome.transient is True


# --- ensure_qbit_category ------------------------------------------------------


async def test_an_existing_category_with_the_right_save_path_is_left_alone() -> None:
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [
                _qbit_ok({"tv": {"name": "tv", "savePath": "/data/torrents/tv"}})
            ],
        }
    )

    outcome = await steps.ensure_qbit_category(
        fake, QBITTORRENT, QBIT_KEY, media_folder="tv", save_path="/data/torrents/tv"
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert fake.calls == [("GET", "http://gluetun:8080", "api/v2/torrents/categories", None)]


async def test_a_missing_category_is_created() -> None:
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [_qbit_ok({})],
            ("POST", "http://gluetun:8080", "api/v2/torrents/createCategory"): [_qbit_ok()],
        }
    )

    outcome = await steps.ensure_qbit_category(
        fake, QBITTORRENT, QBIT_KEY, media_folder="tv", save_path="/data/torrents/tv"
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    post_calls = [call for call in fake.calls if call[0] == "POST"]
    assert post_calls == [
        (
            "POST",
            "http://gluetun:8080",
            "api/v2/torrents/createCategory",
            {"category": "tv", "savePath": "/data/torrents/tv"},
        )
    ]


async def test_a_category_with_the_wrong_save_path_is_edited() -> None:
    fake = FakeQbitClient(
        {
            ("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [
                _qbit_ok({"movies": {"name": "movies", "savePath": "/data/torrents/old"}})
            ],
            ("POST", "http://gluetun:8080", "api/v2/torrents/editCategory"): [_qbit_ok()],
        }
    )

    outcome = await steps.ensure_qbit_category(
        fake, QBITTORRENT, QBIT_KEY, media_folder="movies", save_path="/data/torrents/movies"
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    post_calls = [call for call in fake.calls if call[0] == "POST"]
    assert post_calls == [
        (
            "POST",
            "http://gluetun:8080",
            "api/v2/torrents/editCategory",
            {"category": "movies", "savePath": "/data/torrents/movies"},
        )
    ]


async def test_category_failures_are_reported_against_qbittorrent() -> None:
    fake = FakeQbitClient(
        {("GET", "http://gluetun:8080", "api/v2/torrents/categories"): [_qbit_failed(500)]}
    )

    outcome = await steps.ensure_qbit_category(
        fake, QBITTORRENT, QBIT_KEY, media_folder="tv", save_path="/data/torrents/tv"
    )

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_unreachable("qBittorrent")
    assert outcome.transient is True


# --- ensure_download_client -----------------------------------------------------

_DOWNLOAD_CLIENT_SCHEMA: list[object] = [
    {
        "id": 0,
        "name": "Deluge",
        "implementation": "Deluge",
        "fields": [{"name": "host", "value": ""}],
    },
    {
        "id": 0,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "fields": [
            {"name": "host", "value": ""},
            {"name": "port", "value": 8080},
            {"name": "useSsl", "value": False},
            {"name": "urlBase", "value": ""},
            {"name": "apiKey", "value": ""},
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
            {"name": "tvCategory", "value": ""},
        ],
    },
]

_RADARR_DOWNLOAD_CLIENT_SCHEMA: list[object] = [
    {
        "id": 0,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "fields": [
            {"name": "host", "value": ""},
            {"name": "port", "value": 8080},
            {"name": "useSsl", "value": False},
            {"name": "urlBase", "value": ""},
            {"name": "apiKey", "value": ""},
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
            {"name": "movieCategory", "value": ""},
        ],
    },
]


async def test_sonarr_creates_a_qbittorrent_download_client_from_the_schema() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/downloadclient"): [_ok([])],
            ("GET", "http://sonarr:8989", "api/v3/downloadclient/schema"): [
                _ok(_DOWNLOAD_CLIENT_SCHEMA)
            ],
            ("POST", "http://sonarr:8989", "api/v3/downloadclient"): [_created({"id": 7})],
        }
    )

    outcome = await steps.ensure_download_client(
        fake,
        SONARR,
        SONARR_KEY,
        QBITTORRENT,
        QBIT_KEY,
        category_field="tvCategory",
        category="tv",
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    post_calls = [call for call in fake.calls if call[0] == "POST"]
    assert len(post_calls) == 1
    body = post_calls[0][3]
    assert isinstance(body, dict)
    assert body["name"] == "qBittorrent"
    assert body["enable"] is True
    assert body["priority"] == 1
    assert body["removeCompletedDownloads"] is True
    assert body["removeFailedDownloads"] is True
    assert "id" not in body
    fields = {entry["name"]: entry["value"] for entry in body["fields"]}
    assert fields["host"] == "gluetun"
    assert fields["port"] == 8080
    assert fields["useSsl"] is False
    assert fields["urlBase"] == ""
    assert fields["apiKey"] == QBIT_KEY
    assert fields["username"] == ""
    assert fields["password"] == ""
    assert fields["tvCategory"] == "tv"


async def test_radarr_uses_moviecategory() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://radarr:7878", "api/v3/downloadclient"): [_ok([])],
            ("GET", "http://radarr:7878", "api/v3/downloadclient/schema"): [
                _ok(_RADARR_DOWNLOAD_CLIENT_SCHEMA)
            ],
            ("POST", "http://radarr:7878", "api/v3/downloadclient"): [_created({"id": 8})],
        }
    )

    outcome = await steps.ensure_download_client(
        fake,
        RADARR,
        RADARR_KEY,
        QBITTORRENT,
        QBIT_KEY,
        category_field="movieCategory",
        category="movies",
    )

    assert outcome.state == "done"
    body = [call for call in fake.calls if call[0] == "POST"][0][3]
    assert isinstance(body, dict)
    fields = {entry["name"]: entry["value"] for entry in body["fields"]}
    assert fields["movieCategory"] == "movies"


async def test_an_up_to_date_client_is_left_alone() -> None:
    existing = {
        "id": 3,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "enable": True,
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "fields": [
            {"name": "host", "value": "gluetun"},
            {"name": "port", "value": 8080},
            {"name": "tvCategory", "value": "tv"},
        ],
    }
    fake = FakeArrClient(
        {("GET", "http://sonarr:8989", "api/v3/downloadclient"): [_ok([existing])]}
    )

    outcome = await steps.ensure_download_client(
        fake,
        SONARR,
        SONARR_KEY,
        QBITTORRENT,
        QBIT_KEY,
        category_field="tvCategory",
        category="tv",
    )

    assert outcome.state == "done"
    assert outcome.changed is False
    assert outcome.note == words.WIRING_NOTE_ALREADY_CONNECTED
    assert fake.calls == [("GET", "http://sonarr:8989", "api/v3/downloadclient", None)]


async def test_a_client_pointing_elsewhere_is_put_back() -> None:
    existing = {
        "id": 3,
        "name": "qBittorrent",
        "implementation": "QBittorrent",
        "enable": True,
        "priority": 1,
        "removeCompletedDownloads": True,
        "removeFailedDownloads": True,
        "fields": [
            {"name": "host", "value": "gluetun"},
            {"name": "port", "value": 9999},
            {"name": "tvCategory", "value": "tv"},
        ],
    }
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/downloadclient"): [_ok([existing])],
            ("PUT", "http://sonarr:8989", "api/v3/downloadclient/3"): [_ok({"id": 3})],
        }
    )

    outcome = await steps.ensure_download_client(
        fake,
        SONARR,
        SONARR_KEY,
        QBITTORRENT,
        QBIT_KEY,
        category_field="tvCategory",
        category="tv",
    )

    assert outcome.state == "done"
    assert outcome.changed is True
    put_calls = [call for call in fake.calls if call[0] == "PUT"]
    assert len(put_calls) == 1
    body = put_calls[0][3]
    assert isinstance(body, dict)
    fields = {entry["name"]: entry["value"] for entry in body["fields"]}
    assert fields["port"] == 8080
    assert fields["apiKey"] == QBIT_KEY


async def test_a_missing_schema_entry_is_reported_against_the_partner() -> None:
    fake = FakeArrClient(
        {
            ("GET", "http://sonarr:8989", "api/v3/downloadclient"): [_ok([])],
            ("GET", "http://sonarr:8989", "api/v3/downloadclient/schema"): [_ok([])],
        }
    )

    outcome = await steps.ensure_download_client(
        fake,
        SONARR,
        SONARR_KEY,
        QBITTORRENT,
        QBIT_KEY,
        category_field="tvCategory",
        category="tv",
    )

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_refused("Sonarr")


async def test_download_client_unreachable_is_reported_against_the_partner() -> None:
    fake = FakeArrClient(
        {("GET", "http://sonarr:8989", "api/v3/downloadclient"): [_failed(0, detail="dead")]}
    )

    outcome = await steps.ensure_download_client(
        fake,
        SONARR,
        SONARR_KEY,
        QBITTORRENT,
        QBIT_KEY,
        category_field="tvCategory",
        category="tv",
    )

    assert outcome.state == "error"
    assert outcome.note == words.wiring_failure_unreachable("Sonarr")
    assert outcome.transient is True
