"""Tests for `vpn.py`: the provider table, the answer rules and what Gluetun
is handed.

Every function here is pure - no Docker, no filesystem, no FastAPI - so a
provider's flags, an answer's refusal, and Gluetun's own settings can all be
proven with plain values in and plain values out.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import tomllib
from pathlib import Path, PurePosixPath

import pytest

from marrquee import vpn
from marrquee.compose import clear_vpn_secrets, vpn_secrets_host_path, write_vpn_secrets
from marrquee.config import Settings
from marrquee.storage import PathEscapesRoot
from marrquee.vpn import (
    CONTROL_ROUTES,
    VPN_PROBLEM_PICK_PROVIDER,
    VPN_PROBLEM_TOO_LONG,
    VPN_PROBLEM_WG_ADDRESS,
    VPN_PROBLEM_WG_KEY,
    VPN_PROVIDER_NEEDS_FILES,
    GluetunConfig,
    TunnelPlace,
    VpnAnswerCheck,
    build_gluetun_config,
    check_vpn_answers,
    find_provider,
    provider_wiki_url,
    secret_values,
    tunnel_place_line,
)
from marrquee.words import VPN_LINE_PROTECTED

_ANSWER_FIELD_NAMES = frozenset(
    (
        "provider",
        "vpn_type",
        "openvpn_user",
        "openvpn_password",
        "wireguard_private_key",
        "wireguard_addresses",
        "wireguard_preshared_key",
        "server_countries",
    )
)

_EXPECTED_PROVIDER_VALUES = frozenset(
    (
        "airvpn",
        "cyberghost",
        "expressvpn",
        "fastestvpn",
        "giganews",
        "hidemyass",
        "ipvanish",
        "ivpn",
        "mullvad",
        "nordvpn",
        "privado",
        "private internet access",
        "privatevpn",
        "protonvpn",
        "purevpn",
        "slickvpn",
        "surfshark",
        "torguard",
        "vpnsecure",
        "vpn unlimited",
        "vyprvpn",
        "windscribe",
    )
)

_EXPECTED_WIKI_SLUGS = frozenset(
    (
        "airvpn",
        "cyberghost",
        "expressvpn",
        "fastestvpn",
        "giganews",
        "hidemyass",
        "ipvanish",
        "ivpn",
        "mullvad",
        "nordvpn",
        "privado",
        "private-internet-access",
        "privatevpn",
        "protonvpn",
        "purevpn",
        "slickvpn",
        "surfshark",
        "torguard",
        "vpn-secure",
        "vpn-unlimited",
        "vyprvpn",
        "windscribe",
    )
)


def _valid_wg_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


# --- The provider table -------------------------------------------------------


def test_provider_values_match_gluetuns_providers_all() -> None:
    assert len(vpn.VPN_PROVIDERS) == 22
    values = {provider.value for provider in vpn.VPN_PROVIDERS}
    assert values == _EXPECTED_PROVIDER_VALUES

    labels = [provider.label for provider in vpn.VPN_PROVIDERS]
    assert all(labels)
    assert len(labels) == len(set(labels))


def test_providers_are_sorted_by_label() -> None:
    labels = [provider.label for provider in vpn.VPN_PROVIDERS]
    assert labels == sorted(labels, key=str.casefold)


def test_find_provider_looks_up_by_value() -> None:
    provider = find_provider("protonvpn")
    assert provider is not None
    assert provider.label == "ProtonVPN"


def test_find_provider_is_none_for_unknown_or_blank() -> None:
    assert find_provider("") is None
    assert find_provider("not-a-real-provider") is None


def test_every_provider_links_its_own_wiki_page() -> None:
    slugs = {provider.wiki_slug for provider in vpn.VPN_PROVIDERS}
    assert slugs == _EXPECTED_WIKI_SLUGS

    for provider in vpn.VPN_PROVIDERS:
        assert provider_wiki_url(provider) == (
            f"https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/"
            f"{provider.wiki_slug}.md"
        )


def test_cyberghost_and_vpnsecure_are_disabled_with_a_reason() -> None:
    by_value = {provider.value: provider for provider in vpn.VPN_PROVIDERS}

    assert by_value["cyberghost"].unavailable == VPN_PROVIDER_NEEDS_FILES
    assert by_value["vpnsecure"].unavailable == VPN_PROVIDER_NEEDS_FILES

    others = [
        provider
        for provider in vpn.VPN_PROVIDERS
        if provider.value not in ("cyberghost", "vpnsecure")
    ]
    assert all(provider.unavailable is None for provider in others)


def test_wireguard_support_matches_the_verified_provider_flags() -> None:
    by_value = {provider.value: provider for provider in vpn.VPN_PROVIDERS}
    wireguard_providers = {value for value, provider in by_value.items() if provider.wireguard}

    assert wireguard_providers == {
        "airvpn",
        "fastestvpn",
        "ivpn",
        "mullvad",
        "nordvpn",
        "protonvpn",
        "surfshark",
        "windscribe",
    }
    needs_address = {
        value
        for value, provider in by_value.items()
        if provider.wireguard and provider.wireguard_needs_address
    }
    assert needs_address == wireguard_providers - {"nordvpn", "protonvpn"}


def test_port_forwarding_providers_match_the_verified_flags() -> None:
    by_value = {provider.value: provider for provider in vpn.VPN_PROVIDERS}
    forwarding = {value for value, provider in by_value.items() if provider.port_forwarding}

    assert forwarding == {"private internet access", "privatevpn", "protonvpn"}


def test_ivpn_is_the_only_provider_with_an_optional_openvpn_password() -> None:
    by_value = {provider.value: provider for provider in vpn.VPN_PROVIDERS}
    optional = {
        value for value, provider in by_value.items() if not provider.openvpn_password_required
    }

    assert optional == {"ivpn"}


# --- check_vpn_answers ---------------------------------------------------------


def test_blank_provider_is_refused_on_the_provider_field() -> None:
    result = check_vpn_answers({"provider": "", "vpn_type": "openvpn"})

    assert result.ok is False
    assert result.field == "provider"
    assert result.problem == VPN_PROBLEM_PICK_PROVIDER


def test_an_unknown_provider_value_is_refused_the_same_way_as_blank() -> None:
    result = check_vpn_answers({"provider": "not-a-real-company", "vpn_type": "openvpn"})

    assert result.ok is False
    assert result.field == "provider"
    assert result.problem == VPN_PROBLEM_PICK_PROVIDER


def test_a_disabled_provider_is_refused_with_its_reason() -> None:
    result = check_vpn_answers({"provider": "cyberghost", "vpn_type": "openvpn"})

    assert result.ok is False
    assert result.field == "provider"
    assert VPN_PROVIDER_NEEDS_FILES in (result.problem or "")


def test_wireguard_for_a_provider_that_only_offers_openvpn_is_refused() -> None:
    result = check_vpn_answers(
        {
            "provider": "expressvpn",
            "vpn_type": "wireguard",
            "wireguard_private_key": _valid_wg_key(),
        }
    )

    assert result.ok is False
    assert result.field == "vpn_type"


def test_openvpn_requires_a_username_and_a_password() -> None:
    missing_user = check_vpn_answers(
        {"provider": "mullvad", "vpn_type": "openvpn", "openvpn_user": "", "openvpn_password": "pw"}
    )
    assert missing_user.ok is False
    assert missing_user.field == "openvpn_user"

    missing_password = check_vpn_answers(
        {"provider": "mullvad", "vpn_type": "openvpn", "openvpn_user": "u", "openvpn_password": ""}
    )
    assert missing_password.ok is False
    assert missing_password.field == "openvpn_password"


def test_ivpn_openvpn_passes_with_a_blank_password() -> None:
    result = check_vpn_answers(
        {
            "provider": "ivpn",
            "vpn_type": "openvpn",
            "openvpn_user": "ivpn-user",
            "openvpn_password": "",
        }
    )

    assert result.ok is True
    assert result.answers["openvpn_password"] == ""


def test_a_31_byte_wireguard_key_is_refused_a_32_byte_one_passes() -> None:
    short_key = base64.b64encode(os.urandom(31)).decode()
    refused = check_vpn_answers(
        {
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "wireguard_private_key": short_key,
            "wireguard_addresses": "10.64.0.2/32",
        }
    )
    assert refused.ok is False
    assert refused.field == "wireguard_private_key"
    assert refused.problem == VPN_PROBLEM_WG_KEY

    good_key = _valid_wg_key()
    accepted = check_vpn_answers(
        {
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "wireguard_private_key": good_key,
            "wireguard_addresses": "10.64.0.2/32",
        }
    )
    assert accepted.ok is True


def test_a_wireguard_key_that_isnt_valid_base64_is_refused_not_raised() -> None:
    result = check_vpn_answers(
        {
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "wireguard_private_key": "not-base64-at-all!!",
            "wireguard_addresses": "10.64.0.2/32",
        }
    )

    assert result.ok is False
    assert result.field == "wireguard_private_key"


def test_a_bad_preshared_key_is_refused_when_one_is_given() -> None:
    result = check_vpn_answers(
        {
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "wireguard_private_key": _valid_wg_key(),
            "wireguard_addresses": "10.64.0.2/32",
            "wireguard_preshared_key": "still-not-base64!!",
        }
    )

    assert result.ok is False
    assert result.field == "wireguard_preshared_key"


def test_mullvad_wireguard_without_an_address_is_refused_protonvpn_without_one_passes() -> None:
    key = _valid_wg_key()

    refused = check_vpn_answers(
        {
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "wireguard_private_key": key,
            "wireguard_addresses": "",
        }
    )
    assert refused.ok is False
    assert refused.field == "wireguard_addresses"
    assert refused.problem == VPN_PROBLEM_WG_ADDRESS

    accepted = check_vpn_answers(
        {
            "provider": "protonvpn",
            "vpn_type": "wireguard",
            "wireguard_private_key": key,
            "wireguard_addresses": "",
        }
    )
    assert accepted.ok is True


def test_a_value_over_256_characters_is_refused() -> None:
    result = check_vpn_answers(
        {
            "provider": "mullvad",
            "vpn_type": "openvpn",
            "openvpn_user": "u",
            "openvpn_password": "p",
            "server_countries": "x" * 257,
        }
    )

    assert result.ok is False
    assert result.field == "server_countries"
    assert result.problem == VPN_PROBLEM_TOO_LONG


def test_check_vpn_answers_strips_every_value() -> None:
    result = check_vpn_answers(
        {
            "provider": " mullvad ",
            "vpn_type": " openvpn ",
            "openvpn_user": "  the-user  ",
            "openvpn_password": "  the-pass  ",
        }
    )

    assert result.ok is True
    assert result.answers["provider"] == "mullvad"
    assert result.answers["openvpn_user"] == "the-user"
    assert result.answers["openvpn_password"] == "the-pass"


def test_check_vpn_answers_never_raises_on_junk() -> None:
    result = check_vpn_answers({})
    assert isinstance(result, VpnAnswerCheck)
    assert result.ok is False


def test_answers_always_carry_all_eight_field_names_even_on_refusal() -> None:
    refused = check_vpn_answers({"provider": ""})
    assert set(refused.answers) == _ANSWER_FIELD_NAMES

    accepted = check_vpn_answers(
        {"provider": "mullvad", "vpn_type": "openvpn", "openvpn_user": "u", "openvpn_password": "p"}
    )
    assert set(accepted.answers) == _ANSWER_FIELD_NAMES
    assert accepted.answers["wireguard_private_key"] == ""
    assert accepted.answers["server_countries"] == ""


def test_protonvpn_openvpn_username_is_saved_exactly_as_typed() -> None:
    result = check_vpn_answers(
        {
            "provider": "protonvpn",
            "vpn_type": "openvpn",
            "openvpn_user": "proton-user",
            "openvpn_password": "proton-pw",
        }
    )

    assert result.ok is True
    assert result.answers["openvpn_user"] == "proton-user"


# --- build_gluetun_config -------------------------------------------------------


def _openvpn_answers(provider: str, *, user: str = "u", password: str = "p") -> dict[str, str]:
    return {
        "provider": provider,
        "vpn_type": "openvpn",
        "openvpn_user": user,
        "openvpn_password": password,
    }


def test_build_gluetun_config_raises_value_error_with_no_value_in_the_message() -> None:
    with pytest.raises(ValueError) as excinfo:
        build_gluetun_config(
            {
                "provider": "mullvad",
                "vpn_type": "openvpn",
                "openvpn_user": "",
                "openvpn_password": "",
            },
            control_key="the-control-key",
            timezone="UTC",
            puid=1000,
            pgid=1000,
        )

    assert "the-control-key" not in str(excinfo.value)


def test_environment_holds_no_credential_and_names_the_kill_switch_on() -> None:
    config = build_gluetun_config(
        _openvpn_answers("mullvad", user="ci-user-7f3a", password="pw-9d1c"),
        control_key="control-secret-key",
        timezone="Europe/Amsterdam",
        puid=1000,
        pgid=1000,
    )
    values = [value for _, value in config.environment]

    assert "ci-user-7f3a" not in values
    assert "pw-9d1c" not in values
    assert "control-secret-key" not in values
    env = dict(config.environment)
    assert env["FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT"] == "on"
    assert env["HEALTH_RESTART_VPN"] == "on"
    assert env["PUBLICIP_ENABLED"] == "on"
    assert env["VPN_SERVICE_PROVIDER"] == "mullvad"
    assert env["VPN_TYPE"] == "openvpn"
    assert env["TZ"] == "Europe/Amsterdam"
    assert env["PUID"] == "1000"
    assert env["PGID"] == "1000"
    assert "SERVER_COUNTRIES" not in env


def test_environment_order_matches_the_contract() -> None:
    config = build_gluetun_config(
        {**_openvpn_answers("mullvad"), "server_countries": "Netherlands"},
        control_key="k",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )

    names = [name for name, _ in config.environment]
    assert names == [
        "VPN_SERVICE_PROVIDER",
        "VPN_TYPE",
        "SERVER_COUNTRIES",
        "VPN_PORT_FORWARDING",
        "PORT_FORWARD_ONLY",
        "FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT",
        "HEALTH_RESTART_VPN",
        "PUBLICIP_ENABLED",
        "HTTP_CONTROL_SERVER_AUTH_CONFIG_FILEPATH",
        "TZ",
        "PUID",
        "PGID",
    ]


def test_port_forwarding_is_on_for_protonvpn_and_pia_off_for_nordvpn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proton = build_gluetun_config(
        _openvpn_answers("protonvpn"), control_key="k", timezone="UTC", puid=1000, pgid=1000
    )
    assert dict(proton.environment)["VPN_PORT_FORWARDING"] == "on"
    assert dict(proton.environment)["PORT_FORWARD_ONLY"] == "on"

    pia = build_gluetun_config(
        _openvpn_answers("private internet access"),
        control_key="k",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )
    assert dict(pia.environment)["VPN_PORT_FORWARDING"] == "on"
    assert dict(pia.environment)["PORT_FORWARD_ONLY"] == "on"

    nord = build_gluetun_config(
        _openvpn_answers("nordvpn"), control_key="k", timezone="UTC", puid=1000, pgid=1000
    )
    assert dict(nord.environment)["VPN_PORT_FORWARDING"] == "off"
    assert dict(nord.environment)["PORT_FORWARD_ONLY"] == "off"

    # PrivateVPN is OpenVPN-only in the real provider table, but the
    # environment formula still carries an explicit "not (privatevpn with
    # wireguard)" clause - patch the table to exercise that branch directly
    # rather than leaving it unreachable and untested.
    patched = dataclasses.replace(
        vpn._PROVIDERS_BY_VALUE["privatevpn"], wireguard=True, wireguard_needs_address=False
    )
    monkeypatch.setitem(vpn._PROVIDERS_BY_VALUE, "privatevpn", patched)
    privatevpn_wg = build_gluetun_config(
        {
            "provider": "privatevpn",
            "vpn_type": "wireguard",
            "wireguard_private_key": _valid_wg_key(),
        },
        control_key="k",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )
    assert dict(privatevpn_wg.environment)["VPN_PORT_FORWARDING"] == "off"


def test_protonvpn_openvpn_gets_pmp_in_the_secret_file_but_not_the_saved_answer() -> None:
    answers = _openvpn_answers("protonvpn", user="proton-user", password="proton-pw")

    check = check_vpn_answers(answers)
    assert check.answers["openvpn_user"] == "proton-user"

    config = build_gluetun_config(answers, control_key="k", timezone="UTC", puid=1000, pgid=1000)
    assert config.secret_files["openvpn_user"] == "proton-user+pmp"

    already_suffixed = _openvpn_answers("protonvpn", user="proton-user+pmp", password="proton-pw")
    config_again = build_gluetun_config(
        already_suffixed, control_key="k", timezone="UTC", puid=1000, pgid=1000
    )
    assert config_again.secret_files["openvpn_user"] == "proton-user+pmp"


def test_secret_files_for_openvpn_hold_exactly_user_password_and_control_toml() -> None:
    config = build_gluetun_config(
        _openvpn_answers("mullvad"), control_key="k", timezone="UTC", puid=1000, pgid=1000
    )

    assert set(config.secret_files) == {"openvpn_user", "openvpn_password", "control-server.toml"}


def test_secret_files_for_wireguard_hold_the_key_and_address_but_no_openvpn_names() -> None:
    config = build_gluetun_config(
        {
            "provider": "mullvad",
            "vpn_type": "wireguard",
            "wireguard_private_key": _valid_wg_key(),
            "wireguard_addresses": "10.64.0.2/32",
        },
        control_key="k",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )

    assert set(config.secret_files) == {
        "wireguard_private_key",
        "wireguard_addresses",
        "control-server.toml",
    }


def test_secret_files_for_pia_with_forwarding_also_write_the_port_forwarding_pair() -> None:
    config = build_gluetun_config(
        _openvpn_answers("private internet access", user="pia-user", password="pia-pw"),
        control_key="k",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )

    assert config.secret_files["vpn_port_forwarding_username"] == "pia-user"
    assert config.secret_files["vpn_port_forwarding_password"] == "pia-pw"


def test_control_server_toml_lists_exactly_control_routes_with_the_given_key() -> None:
    config = build_gluetun_config(
        _openvpn_answers("mullvad"),
        control_key="the-control-key",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )

    parsed = tomllib.loads(config.secret_files["control-server.toml"])
    roles = parsed["roles"]
    assert len(roles) == 1
    role = roles[0]
    assert role["name"] == "marrquee"
    assert tuple(role["routes"]) == CONTROL_ROUTES
    assert role["auth"] == "apikey"
    assert role["apikey"] == "the-control-key"


def test_repr_of_gluetun_config_shows_no_secret() -> None:
    config = build_gluetun_config(
        _openvpn_answers("mullvad", user="top-secret-user", password="top-secret-pw"),
        control_key="top-secret-control-key",
        timezone="UTC",
        puid=1000,
        pgid=1000,
    )

    rendered = repr(config)
    assert "top-secret-user" not in rendered
    assert "top-secret-pw" not in rendered
    assert "top-secret-control-key" not in rendered
    assert isinstance(config, GluetunConfig)


# --- secret_values -------------------------------------------------------------


def test_secret_values_includes_the_pmp_form_for_redaction() -> None:
    values = secret_values(
        {
            "openvpn_user": "my-user",
            "openvpn_password": "my-pw",
            "wireguard_private_key": "",
            "wireguard_preshared_key": "",
        }
    )

    assert "my-user" in values
    assert "my-pw" in values
    assert "my-user+pmp" in values


def test_secret_values_is_empty_for_blank_answers() -> None:
    assert secret_values({}) == ()


# --- TunnelPlace / tunnel_place_line -------------------------------------------


def test_tunnel_place_line_city_and_country() -> None:
    place = TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )

    assert tunnel_place_line(place) == (
        "Protected - your downloads appear to come from Amsterdam, Netherlands"
    )


def test_tunnel_place_line_country_only() -> None:
    place = TunnelPlace(public_ip="185.1.1.1", city="", region="", country="Netherlands")

    assert tunnel_place_line(place) == "Protected - your downloads appear to come from Netherlands"


def test_tunnel_place_line_falls_back_to_the_plain_protected_line() -> None:
    assert tunnel_place_line(None) == VPN_LINE_PROTECTED
    assert tunnel_place_line(TunnelPlace(public_ip="1.1.1.1", city="", region="", country="")) == (
        VPN_LINE_PROTECTED
    )


def test_vpn_provider_is_a_frozen_dataclass() -> None:
    provider = vpn.VPN_PROVIDERS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        provider.label = "changed"  # type: ignore[misc]


# --- vpn_secrets_host_path / write_vpn_secrets / clear_vpn_secrets ------------
#
# Gluetun reads its login from a folder Marrquee derives fresh from
# answers.json on every bring-up (compose.py's `write_vpn_secrets`) - it is
# never routed through `build_folders`, since that chowns to the drive's
# owner and this folder must stay root-only.


def test_vpn_secrets_host_path_is_marrquee_vpn_under_the_root() -> None:
    assert vpn_secrets_host_path(PurePosixPath("/volume1/media")) == PurePosixPath(
        "/volume1/media/marrquee/vpn"
    )


def _secrets_settings_and_root(tmp_path: Path) -> tuple[Settings, PurePosixPath]:
    (tmp_path / "volume1" / "media").mkdir(parents=True)
    return Settings(host_mount=tmp_path), PurePosixPath("/volume1/media")


def _secrets_folder(tmp_path: Path) -> Path:
    return tmp_path / "volume1" / "media" / "marrquee" / "vpn"


def test_write_vpn_secrets_makes_a_root_only_folder_of_root_only_files(tmp_path: Path) -> None:
    settings, root = _secrets_settings_and_root(tmp_path)

    write_vpn_secrets(
        settings, root, {"openvpn_user": "ci-user-7f3a", "control-server.toml": "top-secret"}
    )

    folder = _secrets_folder(tmp_path)
    assert folder.is_dir()
    assert (folder.stat().st_mode & 0o777) == 0o700
    for name, content in (("openvpn_user", "ci-user-7f3a"), ("control-server.toml", "top-secret")):
        written = folder / name
        assert written.is_file()
        assert (written.stat().st_mode & 0o777) == 0o600
        assert written.read_text() == content
    assert not any(folder.glob(".*.tmp"))


def test_write_vpn_secrets_removes_a_stale_file_no_longer_wanted(tmp_path: Path) -> None:
    settings, root = _secrets_settings_and_root(tmp_path)
    write_vpn_secrets(settings, root, {"wireguard_private_key": "k", "control-server.toml": "t"})

    write_vpn_secrets(settings, root, {"openvpn_user": "u", "control-server.toml": "t"})

    folder = _secrets_folder(tmp_path)
    assert not (folder / "wireguard_private_key").exists()
    assert (folder / "openvpn_user").is_file()


def test_write_vpn_secrets_never_chowns_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, root = _secrets_settings_and_root(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: calls.append((args, kwargs)))

    write_vpn_secrets(settings, root, {"control-server.toml": "t"})

    assert calls == []


def test_write_vpn_secrets_refuses_a_symlinked_secrets_folder(tmp_path: Path) -> None:
    settings, root = _secrets_settings_and_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    marrquee_dir = tmp_path / "volume1" / "media" / "marrquee"
    marrquee_dir.mkdir()
    (marrquee_dir / "vpn").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscapesRoot):
        write_vpn_secrets(settings, root, {"control-server.toml": "t"})


def test_clear_vpn_secrets_removes_every_file(tmp_path: Path) -> None:
    settings, root = _secrets_settings_and_root(tmp_path)
    write_vpn_secrets(settings, root, {"control-server.toml": "t", "openvpn_user": "u"})

    clear_vpn_secrets(settings, root)

    assert list(_secrets_folder(tmp_path).iterdir()) == []


def test_clear_vpn_secrets_never_raises_on_a_missing_folder(tmp_path: Path) -> None:
    settings, root = _secrets_settings_and_root(tmp_path)

    clear_vpn_secrets(settings, root)  # no folder was ever created - must not raise
