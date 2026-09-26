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
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import pytest
import yaml

from marrquee import compose, vpn, words
from marrquee.catalog import get_app
from marrquee.config import Settings
from marrquee.qbittorrent import PORT_SYNC_SCRIPT_NAME, QBIT_KEY_SECRET_NAME
from marrquee.state import STATE_VERSION, InstallState

# Obviously-fake, digit-repeated "keys" - never anything that looks like a
# real generated API key.
_PROWLARR_KEY = "1" * 32
_SONARR_KEY = "2" * 32
_RADARR_KEY = "3" * 32
_GLUETUN_KEY = "4" * 32
_QBIT_KEY = "qbt_" + "5" * 28


def _fixture_state(app_ids: tuple[str, ...] = ("prowlarr", "sonarr", "radarr")) -> InstallState:
    all_keys = {
        "prowlarr": _PROWLARR_KEY,
        "sonarr": _SONARR_KEY,
        "radarr": _RADARR_KEY,
        "gluetun": _GLUETUN_KEY,
        "qbittorrent": _QBIT_KEY,
    }
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


def _gluetun_answers(**overrides: str) -> dict[str, dict[str, str]]:
    """A minimal, always-valid answer set for Gluetun's own VPN step.

    Every field `check_vpn_answers` looks at is present (blank where the
    chosen provider/type doesn't need it) - the same shape
    `check_vpn_answers` itself normalises a real answer into.
    """
    answers = {
        "provider": "mullvad",
        "vpn_type": "openvpn",
        "openvpn_user": "ci-user-7f3a",
        "openvpn_password": "pw-9d1c",
        "wireguard_private_key": "",
        "wireguard_addresses": "",
        "wireguard_preshared_key": "",
        "server_countries": "",
    }
    answers.update(overrides)
    return {"gluetun": answers}


def _rendered_doc(
    state: InstallState, answers: Mapping[str, Mapping[str, str]] | None = None
) -> dict[str, object]:
    plan = compose.build_stack_plan(state, answers)
    text = compose.render_compose(plan)
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    return doc


# Captured from the renderer before this story's ServicePlan/render_compose
# changes landed - the one thing a three-arr-app deploy must never see is a
# diff caused by Gluetun's own compose branch existing elsewhere in the code.
_GOLDEN_THREE_APP_COMPOSE = """\
# This file describes your media server. Marrquee wrote it, and you can read it.
# Every folder here is a real folder on your drive.
services:
  prowlarr:
    # Your search sources, managed in one place.
    image: "lscr.io/linuxserver/prowlarr:latest"
    container_name: prowlarr
    restart: unless-stopped
    ports:
      - "9696:9696"
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: "Etc/UTC"
      UMASK: "002"
      PROWLARR__AUTH__APIKEY: "11111111111111111111111111111111"
      PROWLARR__AUTH__METHOD: "Forms"
      PROWLARR__AUTH__REQUIRED: "Enabled"
    volumes:
      - "/volume1/media/marrquee/apps/prowlarr:/config"
    networks:
      - marrquee

  sonarr:
    # Finds and organizes your TV shows.
    image: "lscr.io/linuxserver/sonarr:latest"
    container_name: sonarr
    restart: unless-stopped
    ports:
      - "8989:8989"
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: "Etc/UTC"
      UMASK: "002"
      SONARR__AUTH__APIKEY: "22222222222222222222222222222222"
      SONARR__AUTH__METHOD: "Forms"
      SONARR__AUTH__REQUIRED: "Enabled"
    volumes:
      - "/volume1/media/marrquee/apps/sonarr:/config"
      # Every app that touches media shares this one /data folder. That's what makes a
      # finished download show up in your library instantly, instead of being copied
      # twice.
      - "/volume1/media/data:/data"
    networks:
      - marrquee

  radarr:
    # Finds and organizes your movies.
    image: "lscr.io/linuxserver/radarr:latest"
    container_name: radarr
    restart: unless-stopped
    ports:
      - "7878:7878"
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: "Etc/UTC"
      UMASK: "002"
      RADARR__AUTH__APIKEY: "33333333333333333333333333333333"
      RADARR__AUTH__METHOD: "Forms"
      RADARR__AUTH__REQUIRED: "Enabled"
    volumes:
      - "/volume1/media/marrquee/apps/radarr:/config"
      # Every app that touches media shares this one /data folder. That's what makes a
      # finished download show up in your library instantly, instead of being copied
      # twice.
      - "/volume1/media/data:/data"
    networks:
      - marrquee

networks:
  marrquee:
    name: marrquee
    attachable: true
"""


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
    assert prowlarr_env["PROWLARR__AUTH__METHOD"] == "Forms"
    assert prowlarr_env["PROWLARR__AUTH__REQUIRED"] == "Enabled"

    sonarr_env = _environment(services["sonarr"])
    assert sonarr_env["SONARR__AUTH__APIKEY"] == _SONARR_KEY
    assert sonarr_env["SONARR__AUTH__METHOD"] == "Forms"
    assert sonarr_env["SONARR__AUTH__REQUIRED"] == "Enabled"

    radarr_env = _environment(services["radarr"])
    assert radarr_env["RADARR__AUTH__APIKEY"] == _RADARR_KEY
    assert radarr_env["RADARR__AUTH__METHOD"] == "Forms"
    assert radarr_env["RADARR__AUTH__REQUIRED"] == "Enabled"

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


# --- every arr app always asks for the one saved login -----------------------


def test_every_arr_app_always_asks_no_external_anywhere_in_the_rendered_text() -> None:
    """diverges-from-existing: this story replaces the LAN-open defaults
    with a login that is always required - the rendered text itself, not
    just the plan object, must never carry the old values.
    """
    state = _fixture_state()
    plan = compose.build_stack_plan(state)

    text = compose.render_compose(plan)

    assert "External" not in text
    assert "DisabledForLocalAddresses" not in text
    assert text.count('"Forms"') == 3
    assert text.count('"Enabled"') == 3


# --- Prowlarr touches no media -----------------------------------------------


def test_prowlarr_gets_no_data_mount() -> None:
    doc = _rendered_doc(_fixture_state())

    prowlarr_volumes = _volumes(_services(doc)["prowlarr"])
    assert not any(volume.endswith(":/data") for volume in prowlarr_volumes)
    assert any(volume.endswith(":/config") for volume in prowlarr_volumes)


# --- consistent identity across every container ------------------------------


def test_puid_pgid_tz_and_umask_are_identical_across_every_arr_service() -> None:
    doc = _rendered_doc(_fixture_state())

    for service in _services(doc).values():
        env = _environment(service)
        assert env["PUID"] == "1000"
        assert env["PGID"] == "1000"
        assert env["TZ"] == "Etc/UTC"
        assert env["UMASK"] == "002"


def test_gluetun_carries_the_same_puid_pgid_and_tz_but_no_umask() -> None:
    doc = _rendered_doc(_fixture_state(("prowlarr", "gluetun")), _gluetun_answers())

    gluetun_env = _environment(_services(doc)["gluetun"])
    assert gluetun_env["PUID"] == "1000"
    assert gluetun_env["PGID"] == "1000"
    assert gluetun_env["TZ"] == "Etc/UTC"
    assert "UMASK" not in gluetun_env


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


# --- Gluetun gets its own compose branch, never faked arr fields -------------


def test_gluetun_service_has_cap_add_the_tun_device_no_ports_and_joins_marrquee() -> None:
    doc = _rendered_doc(_fixture_state(("prowlarr", "gluetun")), _gluetun_answers())

    gluetun = _services(doc)["gluetun"]
    assert gluetun["cap_add"] == ["NET_ADMIN"]
    assert gluetun["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert "ports" not in gluetun
    assert gluetun["networks"] == ["marrquee"]

    # The arr services render exactly as before - the vpn branch never
    # touches them.
    prowlarr = _services(doc)["prowlarr"]
    assert "cap_add" not in prowlarr
    assert "devices" not in prowlarr
    assert prowlarr["ports"] == ["9696:9696"]


def test_no_secret_value_appears_anywhere_in_the_rendered_gluetun_file() -> None:
    answers = _gluetun_answers()
    plan = compose.build_stack_plan(_fixture_state(("prowlarr", "gluetun")), answers)

    text = compose.render_compose(plan)

    for value in vpn.secret_values(answers["gluetun"]):
        assert value not in text
    assert _GLUETUN_KEY not in text


def test_the_kill_switch_is_written_out_as_on() -> None:
    doc = _rendered_doc(_fixture_state(("prowlarr", "gluetun")), _gluetun_answers())

    gluetun_env = _environment(_services(doc)["gluetun"])
    assert gluetun_env["FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT"] == "on"


def test_the_secrets_volume_is_read_only_and_commented_in_plain_words() -> None:
    state = _fixture_state(("prowlarr", "gluetun"))
    answers = _gluetun_answers()
    doc = _rendered_doc(state, answers)

    gluetun_volumes = _volumes(_services(doc)["gluetun"])
    assert any(volume.endswith(":/run/secrets:ro") for volume in gluetun_volumes)
    assert any(volume.endswith(":/gluetun") for volume in gluetun_volumes)

    text = compose.render_compose(compose.build_stack_plan(state, answers))
    assert words.VPN_SECRETS_MOUNT_COMMENT in _comment_text(text)


def test_build_stack_plan_refuses_gluetun_with_no_saved_answers() -> None:
    state = _fixture_state(("prowlarr", "gluetun"))

    with pytest.raises(ValueError, match="invalid VPN answers") as excinfo:
        compose.build_stack_plan(state)  # answers=None -> {} -> nothing saved for gluetun

    assert _GLUETUN_KEY not in str(excinfo.value)


# --- qBittorrent rides Gluetun's network (name-the-network rule) ------------


def test_qbittorrent_rides_gluetuns_network_with_no_port_or_network_of_its_own() -> None:
    """diverges-from-existing FIRST TEST: qBittorrent shares Gluetun's whole
    network namespace instead of joining `marrquee` on its own, and Gluetun
    - not qBittorrent - is the one thing the LAN reaches it through.
    """
    state = _fixture_state(("prowlarr", "sonarr", "gluetun", "qbittorrent"))
    doc = _rendered_doc(state, _gluetun_answers())

    services = _services(doc)
    qbit = services["qbittorrent"]
    assert qbit["network_mode"] == "service:gluetun"
    assert "networks" not in qbit
    assert "ports" not in qbit

    gluetun = services["gluetun"]
    assert gluetun["networks"] == ["marrquee"]
    assert gluetun["ports"] == ["8080:8080"]


def test_qbittorrent_without_gluetun_is_refused() -> None:
    state = _fixture_state(("prowlarr", "sonarr", "qbittorrent"))

    with pytest.raises(ValueError, match="qbittorrent needs gluetun"):
        compose.build_stack_plan(state)


def test_no_qbittorrent_key_or_auth_env_anywhere_in_the_rendered_file() -> None:
    state = _fixture_state(("prowlarr", "sonarr", "gluetun", "qbittorrent"))
    plan = compose.build_stack_plan(state, _gluetun_answers())
    doc = yaml.safe_load(compose.render_compose(plan))

    text = compose.render_compose(plan)
    assert _QBIT_KEY not in text

    qbit_env = _environment(doc["services"]["qbittorrent"])
    assert not any("AUTH" in name for name in qbit_env)
    assert set(qbit_env) == {"PUID", "PGID", "TZ", "UMASK", "WEBUI_PORT"}
    assert qbit_env["WEBUI_PORT"] == "8080"


# --- qBittorrent without a VPN: the one deliberate exception ----------------


def test_qbittorrent_without_gluetun_renders_on_marrquee_with_its_own_port() -> None:
    """The one deliberate exception to the refusal below: a confirmed
    break-glass install runs qBittorrent as a normal app on the `marrquee`
    network instead of riding a VPN it doesn't have.
    """
    state = _fixture_state(("sonarr", "qbittorrent"))

    doc = yaml.safe_load(compose.render_compose(compose.build_stack_plan(state, without_vpn=True)))
    services = _services(doc)

    assert services["qbittorrent"]["networks"] == ["marrquee"]
    assert services["qbittorrent"]["ports"] == ["8080:8080"]
    assert "network_mode" not in services["qbittorrent"]
    assert "gluetun" not in services


def test_qbittorrent_without_gluetun_is_refused_unless_without_vpn() -> None:
    state = _fixture_state(("sonarr", "qbittorrent"))

    with pytest.raises(ValueError, match="qbittorrent needs gluetun"):
        compose.build_stack_plan(state)  # without_vpn defaults to False


def test_with_gluetun_present_without_vpn_changes_nothing() -> None:
    """The flag only ever matters for the one app whose companion is
    genuinely missing - with Gluetun actually installed, True and False
    must render byte-identical output.
    """
    state = _fixture_state(("prowlarr", "sonarr", "gluetun", "qbittorrent"))
    answers = _gluetun_answers()

    with_flag = compose.render_compose(compose.build_stack_plan(state, answers, without_vpn=True))
    without_flag = compose.render_compose(
        compose.build_stack_plan(state, answers, without_vpn=False)
    )

    assert with_flag == without_flag


def test_no_vpn_compose_comment_says_it_runs_without_a_vpn() -> None:
    state = _fixture_state(("sonarr", "qbittorrent"))

    text = compose.render_compose(compose.build_stack_plan(state, without_vpn=True))

    assert words.DOWNLOADER_NO_VPN_COMPOSE_COMMENT in _comment_text(text)
    assert words.DOWNLOADER_COMPOSE_COMMENT not in _comment_text(text)


def test_no_vpn_qbittorrent_incoming_bittorrent_port_is_not_published() -> None:
    state = _fixture_state(("sonarr", "qbittorrent"))

    text = compose.render_compose(compose.build_stack_plan(state, without_vpn=True))
    doc = yaml.safe_load(text)

    assert doc["services"]["qbittorrent"]["ports"] == ["8080:8080"]
    assert "6881" not in text


def test_forwarding_provider_adds_both_port_commands_non_forwarding_adds_none() -> None:
    """A non-forwarding provider still writes the key and the port-sync
    script - an owner switching to a forwarding provider later must not
    need qBittorrent's key regenerated to pick them up.
    """
    state = _fixture_state(("gluetun", "qbittorrent"))

    forwarding = compose.gluetun_config_for(
        get_app("gluetun"), state, _gluetun_answers(provider="protonvpn")
    )
    names = [name for name, _ in forwarding.environment]
    assert names[-2:] == ["VPN_PORT_FORWARDING_UP_COMMAND", "VPN_PORT_FORWARDING_DOWN_COMMAND"]
    forwarding_env = dict(forwarding.environment)
    script_path = "/run/secrets/" + PORT_SYNC_SCRIPT_NAME
    up_command = "/bin/sh " + script_path + " {{PORT}}"
    down_command = "/bin/sh " + script_path + " 0"
    assert forwarding_env["VPN_PORT_FORWARDING_UP_COMMAND"] == up_command
    assert forwarding_env["VPN_PORT_FORWARDING_DOWN_COMMAND"] == down_command
    assert forwarding.secret_files[QBIT_KEY_SECRET_NAME] == _QBIT_KEY
    assert PORT_SYNC_SCRIPT_NAME in forwarding.secret_files

    non_forwarding = compose.gluetun_config_for(get_app("gluetun"), state, _gluetun_answers())
    non_forwarding_names = [name for name, _ in non_forwarding.environment]
    assert "VPN_PORT_FORWARDING_UP_COMMAND" not in non_forwarding_names
    assert "VPN_PORT_FORWARDING_DOWN_COMMAND" not in non_forwarding_names
    assert non_forwarding.secret_files[QBIT_KEY_SECRET_NAME] == _QBIT_KEY
    assert PORT_SYNC_SCRIPT_NAME in non_forwarding.secret_files


def test_gluetun_config_for_writes_neither_qbit_file_when_qbittorrent_is_not_installed() -> None:
    state = _fixture_state(("gluetun",))

    config = compose.gluetun_config_for(get_app("gluetun"), state, _gluetun_answers())

    assert QBIT_KEY_SECRET_NAME not in config.secret_files
    assert PORT_SYNC_SCRIPT_NAME not in config.secret_files


def test_an_install_without_qbittorrent_renders_gluetun_exactly_as_before() -> None:
    doc = _rendered_doc(_fixture_state(("prowlarr", "gluetun")), _gluetun_answers())

    gluetun = _services(doc)["gluetun"]
    assert "network_mode" not in gluetun
    assert "ports" not in gluetun
    assert gluetun["networks"] == ["marrquee"]


def test_a_state_without_gluetun_renders_byte_identical_to_before() -> None:
    """diverges-from-existing: Gluetun's own compose branch must never leak
    into the rendering of a stack that doesn't include it.
    """
    text = compose.render_compose(compose.build_stack_plan(_fixture_state()))

    assert text == _GOLDEN_THREE_APP_COMPOSE


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


# --- the app stack never shares a compose project with Marrquee itself -----
#
# The owner's NAS Docker app runs Marrquee as a compose project the owner
# names - and "marrquee" is the obvious name. If the app stack used that
# same project name, the NAS app's own "Redeploy" would treat Prowlarr,
# Sonarr and Radarr as leftovers of Marrquee's project and remove them
# (found on the owner's Ugreen NAS, 2026-09-23).


def test_the_app_stack_project_is_not_the_name_owners_give_marrquee_itself() -> None:
    plan = compose.build_stack_plan(_fixture_state())

    assert plan.project != "marrquee"
    assert plan.project == "marrquee-apps"


def test_the_deploy_engine_and_the_stack_plan_agree_on_the_project_name() -> None:
    plan = compose.build_stack_plan(_fixture_state())

    assert Settings().stack_project == plan.project
