"""Tests for the generated Docker Compose file - the thing that actually
creates the containers.

The file is rendered by hand as plain text (no YAML library at runtime), so
these tests parse the *output* with PyYAML - a dev-only dependency - to
prove it really is valid YAML with the shape the owner's Docker daemon
needs, while the plain-language comments (the whole point of the file) are
asserted on literally.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

import pytest
import yaml

from marrquee import compose, words
from marrquee.config import Settings
from marrquee.state import STATE_VERSION, InstallState

# Obviously-fake, digit-repeated "keys" - never anything that looks like a
# real generated API key.
_PROWLARR_KEY = "1" * 32
_SONARR_KEY = "2" * 32
_RADARR_KEY = "3" * 32


def _fixture_state(app_ids: tuple[str, ...] = ("prowlarr", "sonarr", "radarr")) -> InstallState:
    all_keys = {"prowlarr": _PROWLARR_KEY, "sonarr": _SONARR_KEY, "radarr": _RADARR_KEY}
    return InstallState(
        version=STATE_VERSION,
        storage_root="/volume1/media",
        app_ids=app_ids,
        api_keys={app_id: all_keys[app_id] for app_id in app_ids},
        puid=1000,
        pgid=1000,
        umask="002",
        timezone="Etc/UTC",
        created="2026-09-19T00:00:00+00:00",
    )


def _rendered_doc(state: InstallState) -> dict[str, object]:
    plan = compose.build_stack_plan(state)
    text = compose.render_compose(plan)
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    return doc


def _services(doc: dict[str, object]) -> dict[str, dict[str, object]]:
    services = doc["services"]
    assert isinstance(services, dict)
    return services


def _environment(service: dict[str, object]) -> dict[str, str]:
    environment = service["environment"]
    assert isinstance(environment, dict)
    return environment


def _volumes(service: dict[str, object]) -> list[str]:
    volumes = service["volumes"]
    assert isinstance(volumes, list)
    return volumes


def _comment_text(text: str) -> str:
    """Every `#`-prefixed line, rejoined into one sentence-preserving string.

    The renderer wraps long comments across several `#` lines, so a
    verbatim-text assertion has to undo the wrapping the same way it was
    done - one space between words, never one space per line break.
    """
    words_in_comments = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            words_in_comments.append(stripped.removeprefix("#").strip())
    return " ".join(words_in_comments)


# --- the acceptance vehicle: parses, carries our keys, one shared /data -----


def test_rendered_file_parses_and_carries_our_keys_and_one_shared_data_root() -> None:
    state = _fixture_state()
    doc = _rendered_doc(state)

    services = _services(doc)
    assert set(services) == {"prowlarr", "sonarr", "radarr"}

    prowlarr_env = _environment(services["prowlarr"])
    assert prowlarr_env["PROWLARR__AUTH__APIKEY"] == _PROWLARR_KEY
    assert prowlarr_env["PROWLARR__AUTH__METHOD"] == "External"
    assert prowlarr_env["PROWLARR__AUTH__REQUIRED"] == "DisabledForLocalAddresses"

    sonarr_env = _environment(services["sonarr"])
    assert sonarr_env["SONARR__AUTH__APIKEY"] == _SONARR_KEY
    assert sonarr_env["SONARR__AUTH__METHOD"] == "External"

    radarr_env = _environment(services["radarr"])
    assert radarr_env["RADARR__AUTH__APIKEY"] == _RADARR_KEY
    assert radarr_env["RADARR__AUTH__METHOD"] == "External"

    sonarr_volumes = _volumes(services["sonarr"])
    radarr_volumes = _volumes(services["radarr"])
    sonarr_data_mount = next(v for v in sonarr_volumes if v.endswith(":/data"))
    radarr_data_mount = next(v for v in radarr_volumes if v.endswith(":/data"))
    assert sonarr_data_mount == radarr_data_mount == "/volume1/media/data:/data"

    networks = doc["networks"]
    assert isinstance(networks, dict)
    marrquee_network = networks["marrquee"]
    assert isinstance(marrquee_network, dict)
    assert marrquee_network["name"] == "marrquee"


# --- Prowlarr touches no media -----------------------------------------------


def test_prowlarr_gets_no_data_mount() -> None:
    doc = _rendered_doc(_fixture_state())

    prowlarr_volumes = _volumes(_services(doc)["prowlarr"])
    assert not any(volume.endswith(":/data") for volume in prowlarr_volumes)
    assert any(volume.endswith(":/config") for volume in prowlarr_volumes)


# --- consistent identity across every container ------------------------------


def test_puid_pgid_tz_and_umask_are_identical_across_every_service() -> None:
    doc = _rendered_doc(_fixture_state())

    for service in _services(doc).values():
        env = _environment(service)
        assert env["PUID"] == "1000"
        assert env["PGID"] == "1000"
        assert env["TZ"] == "Etc/UTC"
        assert env["UMASK"] == "002"


def test_container_names_and_restart_policy_match_the_catalog() -> None:
    doc = _rendered_doc(_fixture_state())

    for app_id, service in _services(doc).items():
        assert service["container_name"] == app_id
        assert service["restart"] == "unless-stopped"

    assert "version" not in doc


# --- every service explicitly joins the network Marrquee later connects to --
#
# A service with no `networks:` attribute of its own joins compose's own
# implicit `default` network, not an arbitrary custom-named one sitting
# alongside it in the top-level `networks:` block - so without this, the
# `marrquee` network the engine connects itself to (`connect_network` in
# `deploy.py`) would have no other containers on it at all, and Marrquee
# could never resolve `sonarr`/`radarr`/`prowlarr` by name.


def test_every_service_explicitly_joins_the_marrquee_network() -> None:
    doc = _rendered_doc(_fixture_state())

    for service in _services(doc).values():
        assert service["networks"] == ["marrquee"]


def test_a_single_chosen_app_still_joins_the_marrquee_network() -> None:
    doc = _rendered_doc(_fixture_state(("radarr",)))

    assert _services(doc)["radarr"]["networks"] == ["marrquee"]


# --- the single highest-consequence path bug in the story --------------------


def test_no_path_in_the_file_starts_with_host() -> None:
    plan = compose.build_stack_plan(_fixture_state())
    text = compose.render_compose(plan)

    assert "/host" not in text


# --- the file is teaching surface, not just machine input --------------------


def test_the_file_names_each_app_in_the_owners_language() -> None:
    plan = compose.build_stack_plan(_fixture_state())
    text = compose.render_compose(plan)

    comments = _comment_text(text)
    assert words.COMPOSE_FILE_HEADER_COMMENT in comments
    assert words.PROWLARR_DESCRIPTION in comments
    assert words.SONARR_DESCRIPTION in comments
    assert words.RADARR_DESCRIPTION in comments
    assert words.DATA_MOUNT_COMMENT in comments


# --- determinism: the whole point of "no spurious diffs on re-deploy" -------


def test_rendering_is_deterministic() -> None:
    state = _fixture_state()

    first = compose.render_compose(compose.build_stack_plan(state))
    second = compose.render_compose(compose.build_stack_plan(state))

    assert first == second


# --- the classic silent YAML surprise ---------------------------------------


def test_umask_002_survives_yaml_parsing_as_a_string_not_an_int() -> None:
    doc = _rendered_doc(_fixture_state())

    umask = _environment(_services(doc)["sonarr"])["UMASK"]
    assert umask == "002"
    assert isinstance(umask, str)


# --- choosing one app yields a valid file with one service ------------------


def test_choosing_one_app_yields_a_valid_file_with_one_service() -> None:
    doc = _rendered_doc(_fixture_state(("radarr",)))

    assert set(_services(doc)) == {"radarr"}


def test_generated_at_comes_from_the_saved_state_not_the_wall_clock() -> None:
    """`generated_at` has to come from something the InstallState already
    carries - anything read from the wall clock would make build_stack_plan
    produce a different StackPlan for the same input on every call, which is
    exactly the non-determinism this function promises not to have.
    """
    state = _fixture_state()

    plan = compose.build_stack_plan(state)

    assert plan.generated_at == state.created


# --- build_stack_plan is total: a missing storage root is a clear failure --


def test_build_stack_plan_refuses_a_state_with_no_storage_root_chosen() -> None:
    state = _fixture_state()
    incomplete = InstallState(
        version=state.version,
        storage_root=None,
        app_ids=state.app_ids,
        api_keys=state.api_keys,
        puid=state.puid,
        pgid=state.pgid,
        umask=state.umask,
        timezone=state.timezone,
        created=state.created,
    )

    with pytest.raises(ValueError, match="storage root"):
        compose.build_stack_plan(incomplete)


# --- compose_file_host_path / write_compose ----------------------------------


def test_compose_file_host_path_is_marrquee_compose_yaml_under_the_root() -> None:
    assert compose.compose_file_host_path(PurePosixPath("/volume1/media")) == PurePosixPath(
        "/volume1/media/marrquee/compose.yaml"
    )


def test_write_compose_writes_atomically_and_is_not_world_readable(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)
    plan = compose.build_stack_plan(_fixture_state())

    # Never really chown in a test - this test cares about the write, not
    # the ownership, which has its own dedicated tests below.
    written_path = compose.write_compose(settings, plan, chown=lambda *_: None)

    assert written_path == tmp_path / "volume1" / "media" / "marrquee" / "compose.yaml"
    assert written_path.is_file()
    mode = written_path.stat().st_mode & 0o777
    assert mode == 0o600
    assert not any(written_path.parent.glob(".*.tmp"))


def test_write_compose_never_puts_the_api_key_in_an_exception_message(tmp_path: Path) -> None:
    """A write that fails at the OS level must never leak a key into the error.

    A folder sitting where the compose file needs to go forces `write_text`
    to fail with a plain OS error - whatever that error says, it must never
    happen to contain the secret we were about to write.
    """
    settings = Settings(host_mount=tmp_path)
    compose_path = tmp_path / "volume1" / "media" / "marrquee" / "compose.yaml"
    compose_path.mkdir(parents=True)  # a directory, not a file, sits in the way
    plan = compose.build_stack_plan(_fixture_state())

    with pytest.raises(OSError) as excinfo:
        compose.write_compose(settings, plan, chown=lambda *_: None)

    assert _PROWLARR_KEY not in str(excinfo.value)
    assert _SONARR_KEY not in str(excinfo.value)
    assert _RADARR_KEY not in str(excinfo.value)


# --- write_compose chowns the file to the drive's own owner, not root -------
#
# Marrquee's container runs as root, so a file it writes with `os.chown`
# never called would land root:root - unreadable to the owner of the drive,
# even though the whole point of this file is that the owner can open and
# read it. `os.chmod` stays 0600 (the file carries every app's API key), but
# the *owner* of that 0600 becomes the drive's own puid/pgid instead of root.


def test_write_compose_chowns_the_file_it_wrote_to_the_states_puid_and_pgid(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)
    plan = compose.build_stack_plan(_fixture_state())  # puid=1000, pgid=1000

    calls: list[tuple[Path, int, int]] = []
    written_path = compose.write_compose(
        settings, plan, chown=lambda path, uid, gid: calls.append((path, uid, gid))
    )

    assert calls == [(written_path, 1000, 1000)]


def test_write_compose_mode_stays_owner_only_after_chowning(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)
    plan = compose.build_stack_plan(_fixture_state())

    written_path = compose.write_compose(settings, plan, chown=lambda *_: None)

    mode = written_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_compose_swallows_a_chown_failure_and_still_returns_a_valid_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A NAS share that doesn't support `chown` must never turn a successful
    deploy into a failed one - the file is still there and still valid, just
    root-owned, and that fact is logged (for `docker logs`) rather than
    raised.
    """
    settings = Settings(host_mount=tmp_path)
    (tmp_path / "volume1" / "media" / "marrquee").mkdir(parents=True)
    plan = compose.build_stack_plan(_fixture_state())

    def _raising_chown(path: Path, uid: int, gid: int) -> None:
        raise PermissionError("this NAS share does not support chown")

    with caplog.at_level(logging.WARNING):
        written_path = compose.write_compose(settings, plan, chown=_raising_chown)

    assert written_path.is_file()
    assert "chown" in caplog.text.lower()
