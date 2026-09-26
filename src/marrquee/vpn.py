"""Gluetun's facts: every VPN company it offers, what a valid answer to the
VPN questions looks like, and what Gluetun itself is handed once an answer
is accepted.

This is a leaf, like `catalog.py`: it imports only the standard library and
`words`, so nothing about the question-answering machinery (`questions.py`)
or the deploy engine can ever leak back into what a VPN answer *is*. The
adapter that turns a `VpnAnswerCheck` into a `QuestionCheck`, and the
`QuestionStep` that asks for it, both live in `questions.py` instead - that
is what keeps this module free of the one import (`questions`) that would
create a cycle.

Hand-off for whatever runs behind this tunnel: a `CatalogApp` that sets
`network_via="gluetun"` joins this container's network instead of getting
its own, and reads its forwarded port through
`vpn_control.GluetunControl.forwarded_port`. Nothing about that app is
built here - this module only ever describes Gluetun itself.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from marrquee.words import (
    VPN_LINE_PROTECTED,
    VPN_PROBLEM_OPENVPN_PASSWORD,
    VPN_PROBLEM_OPENVPN_USER,
    VPN_PROBLEM_PICK_PROVIDER,
    VPN_PROBLEM_TOO_LONG,
    VPN_PROBLEM_WG_ADDRESS,
    VPN_PROBLEM_WG_KEY,
    VPN_PROBLEM_WG_PSK,
    VPN_PROVIDER_NEEDS_FILES,
    vpn_line_protected_place,
    vpn_problem_provider_unavailable,
    vpn_problem_type_unsupported,
)

VPN_APP_ID: Final = "gluetun"

VpnType = Literal["openvpn", "wireguard"]

_WIKI_BASE_URL: Final = "https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers"

# The answer field names, in the order the VPN step asks for them - also the
# order `check_vpn_answers` normalises its returned `answers` mapping in, so
# every caller (the saved-answers file, `missing_step`) sees the same 8 keys
# every time, whether or not a given field applies to the chosen provider.
_ANSWER_FIELDS: Final[tuple[str, ...]] = (
    "provider",
    "vpn_type",
    "openvpn_user",
    "openvpn_password",
    "wireguard_private_key",
    "wireguard_addresses",
    "wireguard_preshared_key",
    "server_countries",
)

_MAX_VALUE_LENGTH: Final = 256


@dataclass(frozen=True)
class VpnProvider:
    """One VPN company Gluetun can tunnel through.

    `value` is Gluetun's own `VPN_SERVICE_PROVIDER` spelling - it goes
    straight into the rendered environment, so it is never re-derived from
    `label`. `unavailable`, when set, is the plain-language reason a
    provider is listed but disabled (Cyberghost and VPN Secure need key
    files a text box can't take yet) - the provider stays in the list
    rather than disappearing, so "every provider Gluetun supports" stays
    true even for the two Marrquee can't drive.
    """

    value: str
    label: str
    wiki_slug: str
    openvpn: bool
    wireguard: bool
    wireguard_needs_address: bool
    port_forwarding: bool
    openvpn_password_required: bool = True
    unavailable: str | None = None


# Gluetun v3.41.3 `internal/constants/providers/providers.go` `All()`, with
# each flag read off that provider's own gluetun-wiki page (2026-09-24). A
# later Gluetun release changing this list is a deliberate edit here, not a
# silent drift - `tests/test_vpn.py` pins the value set.
VPN_PROVIDERS: Final[tuple[VpnProvider, ...]] = (
    VpnProvider(
        value="airvpn",
        label="AirVPN",
        wiki_slug="airvpn",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=True,
        port_forwarding=False,
    ),
    VpnProvider(
        value="cyberghost",
        label="CyberGhost",
        wiki_slug="cyberghost",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
        unavailable=VPN_PROVIDER_NEEDS_FILES,
    ),
    VpnProvider(
        value="expressvpn",
        label="ExpressVPN",
        wiki_slug="expressvpn",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="fastestvpn",
        label="FastestVPN",
        wiki_slug="fastestvpn",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=True,
        port_forwarding=False,
    ),
    VpnProvider(
        value="giganews",
        label="Giganews",
        wiki_slug="giganews",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="hidemyass",
        label="HideMyAss",
        wiki_slug="hidemyass",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="ipvanish",
        label="IPVanish",
        wiki_slug="ipvanish",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="ivpn",
        label="IVPN",
        wiki_slug="ivpn",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=True,
        port_forwarding=False,
        openvpn_password_required=False,
    ),
    VpnProvider(
        value="mullvad",
        label="Mullvad",
        wiki_slug="mullvad",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=True,
        port_forwarding=False,
    ),
    VpnProvider(
        value="nordvpn",
        label="NordVPN",
        wiki_slug="nordvpn",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="privado",
        label="Privado VPN",
        wiki_slug="privado",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="private internet access",
        label="Private Internet Access",
        wiki_slug="private-internet-access",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=True,
    ),
    VpnProvider(
        value="privatevpn",
        label="PrivateVPN",
        wiki_slug="privatevpn",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=True,
    ),
    VpnProvider(
        value="protonvpn",
        label="ProtonVPN",
        wiki_slug="protonvpn",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=False,
        port_forwarding=True,
    ),
    VpnProvider(
        value="purevpn",
        label="PureVPN",
        wiki_slug="purevpn",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="slickvpn",
        label="SlickVPN",
        wiki_slug="slickvpn",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="surfshark",
        label="Surfshark",
        wiki_slug="surfshark",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=True,
        port_forwarding=False,
    ),
    VpnProvider(
        value="torguard",
        label="TorGuard",
        wiki_slug="torguard",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="vpnsecure",
        label="VPN Secure",
        wiki_slug="vpn-secure",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
        unavailable=VPN_PROVIDER_NEEDS_FILES,
    ),
    VpnProvider(
        value="vpn unlimited",
        label="VPN Unlimited",
        wiki_slug="vpn-unlimited",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="vyprvpn",
        label="VyprVPN",
        wiki_slug="vyprvpn",
        openvpn=True,
        wireguard=False,
        wireguard_needs_address=False,
        port_forwarding=False,
    ),
    VpnProvider(
        value="windscribe",
        label="Windscribe",
        wiki_slug="windscribe",
        openvpn=True,
        wireguard=True,
        wireguard_needs_address=True,
        port_forwarding=False,
    ),
)

# A plain dict rather than a frozen mapping so a test can `monkeypatch.setitem`
# one entry to exercise a branch the real provider table can't reach on its
# own (see `test_vpn.py`'s port-forwarding test) - no code here ever
# reassigns the dict itself, only looks values up in it.
_PROVIDERS_BY_VALUE: dict[str, VpnProvider] = {
    provider.value: provider for provider in VPN_PROVIDERS
}


def provider_wiki_url(provider: VpnProvider) -> str:
    """The gluetun-wiki page documenting how to fill in `provider`'s fields."""
    return f"{_WIKI_BASE_URL}/{provider.wiki_slug}.md"


def find_provider(value: str) -> VpnProvider | None:
    """The provider matching `value`, or `None` for an unrecognised or blank one.

    Used by the deploy engine to name the company in a tunnel failure
    sentence - by the time that code runs, `check_vpn_answers` has already
    accepted `value`, so a `None` here can't actually happen in practice, but
    the lookup stays total rather than raising for a background task that
    must never crash on a value it doesn't recognise.
    """
    return _PROVIDERS_BY_VALUE.get(value)


@dataclass(frozen=True)
class VpnAnswerCheck:
    """The outcome of checking one posted set of VPN answers.

    `answers` always carries all 8 field names (blank for anything not
    asked of the chosen provider), whether or not the check passed - a
    partial mapping would make `questions.missing_step` think an
    inapplicable field (WireGuard's key, say, for an OpenVPN answer) is
    still unanswered, and re-ask for it forever.
    """

    ok: bool
    answers: Mapping[str, str]
    problem: str | None
    field: str | None


def _cleaned_answers(answers: Mapping[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for name in _ANSWER_FIELDS:
        raw = answers.get(name, "")
        cleaned[name] = raw.strip() if isinstance(raw, str) else ""
    return cleaned


def _decoded_wireguard_key(value: str) -> bytes | None:
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def check_vpn_answers(answers: Mapping[str, str]) -> VpnAnswerCheck:
    """Whether a posted set of VPN answers is acceptable, and why not.

    Pure and never raises - every rule below is checked in a fixed order,
    and the first failing rule wins.
    """
    cleaned = _cleaned_answers(answers)

    def refuse(problem: str, field_name: str) -> VpnAnswerCheck:
        return VpnAnswerCheck(ok=False, answers=cleaned, problem=problem, field=field_name)

    provider_value = cleaned["provider"]
    provider = _PROVIDERS_BY_VALUE.get(provider_value) if provider_value else None
    if provider is None:
        return refuse(VPN_PROBLEM_PICK_PROVIDER, "provider")
    if provider.unavailable is not None:
        return refuse(vpn_problem_provider_unavailable(provider.label), "provider")

    vpn_type = cleaned["vpn_type"]
    type_offered = (vpn_type == "openvpn" and provider.openvpn) or (
        vpn_type == "wireguard" and provider.wireguard
    )
    if not type_offered:
        type_label = "OpenVPN" if vpn_type == "openvpn" else "WireGuard"
        return refuse(vpn_problem_type_unsupported(provider.label, type_label), "vpn_type")

    if vpn_type == "openvpn":
        if not cleaned["openvpn_user"]:
            return refuse(VPN_PROBLEM_OPENVPN_USER, "openvpn_user")
        if not cleaned["openvpn_password"] and provider.openvpn_password_required:
            return refuse(VPN_PROBLEM_OPENVPN_PASSWORD, "openvpn_password")
    else:
        key = _decoded_wireguard_key(cleaned["wireguard_private_key"])
        if key is None or len(key) != 32:
            return refuse(VPN_PROBLEM_WG_KEY, "wireguard_private_key")

        preshared = cleaned["wireguard_preshared_key"]
        if preshared:
            decoded_preshared = _decoded_wireguard_key(preshared)
            if decoded_preshared is None or len(decoded_preshared) != 32:
                return refuse(VPN_PROBLEM_WG_PSK, "wireguard_preshared_key")

        if provider.wireguard_needs_address:
            address = cleaned["wireguard_addresses"]
            if not address or "/" not in address:
                return refuse(VPN_PROBLEM_WG_ADDRESS, "wireguard_addresses")

    for name in _ANSWER_FIELDS:
        if len(cleaned[name]) > _MAX_VALUE_LENGTH:
            return refuse(VPN_PROBLEM_TOO_LONG, name)

    return VpnAnswerCheck(ok=True, answers=cleaned, problem=None, field=None)


@dataclass(frozen=True)
class GluetunConfig:
    """What Gluetun is handed for one bring-up: the compose-visible
    environment, and the secret files it reads natively from
    `/run/secrets`.

    `secret_files` never shows in `repr()` - a stray log of this value
    (or a persisted snapshot) must never carry a credential.
    """

    environment: tuple[tuple[str, str], ...]
    secret_files: Mapping[str, str] = field(repr=False)


_CONTROL_SERVER_TOML_PATH: Final = "/run/secrets/control-server.toml"

# The one place that decides which routes Marrquee's own control-server key
# is allowed to call - a later app that needs a new route appends it here,
# never in a second auth file.
CONTROL_ROUTES: Final[tuple[str, ...]] = (
    "GET /v1/vpn/status",
    "GET /v1/publicip/ip",
    "GET /v1/portforward",
)


def _control_server_toml(control_key: str) -> str:
    routes = ", ".join(f'"{route}"' for route in CONTROL_ROUTES)
    return (
        "[[roles]]\n"
        'name = "marrquee"\n'
        f"routes = [{routes}]\n"
        'auth = "apikey"\n'
        f'apikey = "{control_key}"\n'
    )


def _with_pmp_suffix(username: str) -> str:
    return username if username.endswith("+pmp") else f"{username}+pmp"


def build_gluetun_config(
    answers: Mapping[str, str],
    *,
    control_key: str,
    timezone: str,
    puid: int,
    pgid: int,
) -> GluetunConfig:
    """Turn one saved answer set into what Gluetun is actually handed.

    Raises `ValueError` (with no answer value in the message) when the
    answers don't pass `check_vpn_answers` - a caller building compose from
    stale or missing answers must fail loudly, never render a tunnel with
    settings Gluetun would refuse anyway.
    """
    check = check_vpn_answers(answers)
    if not check.ok:
        raise ValueError(f"invalid VPN answers: refused on field {check.field!r}")

    a = check.answers
    provider = _PROVIDERS_BY_VALUE[a["provider"]]
    vpn_type = a["vpn_type"]

    port_forwarding_on = provider.port_forwarding and not (
        provider.value == "privatevpn" and vpn_type == "wireguard"
    )
    port_forward_only_on = port_forwarding_on and provider.value in (
        "private internet access",
        "protonvpn",
    )

    environment: list[tuple[str, str]] = [
        ("VPN_SERVICE_PROVIDER", provider.value),
        ("VPN_TYPE", vpn_type),
    ]
    if a["server_countries"]:
        environment.append(("SERVER_COUNTRIES", a["server_countries"]))
    environment.extend(
        [
            ("VPN_PORT_FORWARDING", "on" if port_forwarding_on else "off"),
            ("PORT_FORWARD_ONLY", "on" if port_forward_only_on else "off"),
            ("FIREWALL_ENABLED_DISABLING_IT_SHOOTS_YOU_IN_YOUR_FOOT", "on"),
            ("HEALTH_RESTART_VPN", "on"),
            ("PUBLICIP_ENABLED", "on"),
            ("HTTP_CONTROL_SERVER_AUTH_CONFIG_FILEPATH", _CONTROL_SERVER_TOML_PATH),
            ("TZ", timezone),
            ("PUID", str(puid)),
            ("PGID", str(pgid)),
        ]
    )

    secret_files: dict[str, str] = {"control-server.toml": _control_server_toml(control_key)}
    if vpn_type == "openvpn":
        user = a["openvpn_user"]
        if provider.value == "protonvpn":
            user = _with_pmp_suffix(user)
        secret_files["openvpn_user"] = user
        secret_files["openvpn_password"] = a["openvpn_password"]
        if port_forwarding_on and provider.value == "private internet access":
            secret_files["vpn_port_forwarding_username"] = user
            secret_files["vpn_port_forwarding_password"] = a["openvpn_password"]
    else:
        secret_files["wireguard_private_key"] = a["wireguard_private_key"]
        if a["wireguard_addresses"]:
            secret_files["wireguard_addresses"] = a["wireguard_addresses"]
        if a["wireguard_preshared_key"]:
            secret_files["wireguard_preshared_key"] = a["wireguard_preshared_key"]

    return GluetunConfig(environment=tuple(environment), secret_files=secret_files)


def secret_values(answers: Mapping[str, str]) -> tuple[str, ...]:
    """Every credential value that could show up in Gluetun's own logs or a
    rendered secret file, for redaction only - never used to build config.

    Includes the `+pmp`-suffixed form of the OpenVPN username unconditionally
    (not just for ProtonVPN): redacting a value that was never actually
    written anywhere is harmless, but skipping it for the one provider that
    needed it would not be.
    """
    values = [
        value
        for name in (
            "openvpn_user",
            "openvpn_password",
            "wireguard_private_key",
            "wireguard_preshared_key",
        )
        if (value := answers.get(name, ""))
    ]
    user = answers.get("openvpn_user", "")
    if user:
        values.append(_with_pmp_suffix(user))
    return tuple(values)


@dataclass(frozen=True)
class TunnelPlace:
    """Where Gluetun's own public-IP lookup says a download would appear to
    come from. The address itself is kept only long enough to build this
    value - `tunnel_place_line` never shows it, and no caller persists it.
    """

    public_ip: str
    city: str
    region: str
    country: str


def tunnel_place_line(place: TunnelPlace | None) -> str:
    """The Hub tile's line for a proven tunnel.

    Names the city and country when both are known, falls back to
    whichever one is, and falls back again to the plain "protected" line
    when neither is - the tunnel is still proven either way, since Docker's
    health check and Gluetun's own status already stand behind it; this
    only decides how specific the sentence can honestly be.
    """
    if place is None:
        return VPN_LINE_PROTECTED
    city = place.city.strip()
    country = place.country.strip()
    if city and country:
        return vpn_line_protected_place(f"{city}, {country}")
    if city:
        return vpn_line_protected_place(city)
    if country:
        return vpn_line_protected_place(country)
    return VPN_LINE_PROTECTED
