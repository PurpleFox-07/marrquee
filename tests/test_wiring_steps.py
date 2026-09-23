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

PROWLARR = catalog.get_app("prowlarr")
SONARR = catalog.get_app("sonarr")
RADARR = catalog.get_app("radarr")

PROWLARR_KEY = "p" * 32
SONARR_KEY = "s" * 32
RADARR_KEY = "r" * 32

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
