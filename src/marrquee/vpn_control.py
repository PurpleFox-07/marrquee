"""Gluetun's control server: the authenticated read side of the tunnel.

Docker's own health check (`ContainerSnapshot.health`) only says the
container itself is alive - it says nothing about whether the tunnel is
actually up, or where a download would appear to come from. Gluetun answers
both from inside its own firewall (which lets traffic out only through the
tunnel), but every route needs the key `vpn.build_gluetun_config` wrote into
`control-server.toml`, sent as `X-API-Key`.

`classify_tunnel` and `looks_like_missing_tun` are the only places that know
Gluetun's own words (`AUTH_FAILED` in its logs, the compose device error) -
the deploy engine that drives the tunnel loop never string-matches a log or
a compose error itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Literal, Protocol

import httpx

from marrquee.docker_client import ContainerSnapshot
from marrquee.vpn import TunnelPlace

# Gluetun's own container, on the `marrquee` network its compose service
# joins - Marrquee reaches it by that name, the same way every arr app's
# readiness probe reaches its own container.
GLUETUN_CONTROL_URL: Final = "http://gluetun:8000"

TunnelVerdict = Literal["refused", "settings_refused", "connecting", "healthy", "gone"]

# Gluetun's own signal for "the login you gave me is wrong" - openvpn/logs.go
# matches this exact substring and adds "Your credentials might be wrong".
# WireGuard has no equivalent signal, so a bad WireGuard key falls through to
# the tunnel loop's own 120s timeout instead.
_AUTH_FAILED_MARKER: Final = "AUTH_FAILED"

_MISSING_DEVICE_PHRASES: Final = ("no such file", "error gathering device information")


class GluetunControl(Protocol):
    """Answers the three questions the tunnel loop and the Hub tile need
    from Gluetun's own control server. None of them ever raises - a
    connection error, a wrong key or an unexpected body all come back as
    `None`, the same "not proven yet" answer as a tunnel that's still
    connecting.
    """

    async def vpn_status(self, api_key: str) -> str | None: ...
    async def public_ip(self, api_key: str) -> TunnelPlace | None: ...
    async def forwarded_port(self, api_key: str) -> int | None: ...


class HttpGluetunControl:
    """Talks to the real Gluetun control server over plain HTTP.

    `transport` is injectable the same way `HttpReadinessProbe`'s is - so
    tests can prove the exact request (path, header) without a real
    Gluetun. A short default timeout matters here more than for most
    probes: this call runs inside the Hub's own status refresh, where a
    hung Gluetun must never make the whole page wait.
    """

    def __init__(
        self, *, timeout: float = 3.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def _get_json(self, path: str, api_key: str) -> object:
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.get(
                    f"{GLUETUN_CONTROL_URL}{path}", headers={"X-API-Key": api_key}
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def vpn_status(self, api_key: str) -> str | None:
        payload = await self._get_json("/v1/vpn/status", api_key)
        if not isinstance(payload, dict):
            return None
        status = payload.get("status")
        return status if isinstance(status, str) else None

    async def public_ip(self, api_key: str) -> TunnelPlace | None:
        payload = await self._get_json("/v1/publicip/ip", api_key)
        if not isinstance(payload, dict):
            return None
        public_ip = payload.get("public_ip")
        if not isinstance(public_ip, str) or not public_ip:
            return None
        return TunnelPlace(
            public_ip=public_ip,
            city=_str_or_blank(payload.get("city")),
            region=_str_or_blank(payload.get("region")),
            country=_str_or_blank(payload.get("country")),
        )

    async def forwarded_port(self, api_key: str) -> int | None:
        payload = await self._get_json("/v1/portforward", api_key)
        if not isinstance(payload, dict):
            return None
        ports = payload.get("ports")
        value = ports[0] if isinstance(ports, list) and ports else payload.get("port")
        return value if isinstance(value, int) and value > 0 else None


def _str_or_blank(raw: object) -> str:
    return raw if isinstance(raw, str) else ""


class NoGluetunControl:
    """`DeployManager`'s own default - mirrors `wiring.NoWiringYet` and
    `login_apply.NoLoginApplier`: every method answers `None`, the same
    "not proven yet" answer a real Gluetun gives before its tunnel is up.
    A `DeployManager` built on its own (the shape most tests use) never
    claims a tunnel it never actually asked Gluetun about.
    """

    async def vpn_status(self, api_key: str) -> str | None:
        return None

    async def public_ip(self, api_key: str) -> TunnelPlace | None:
        return None

    async def forwarded_port(self, api_key: str) -> int | None:
        return None


class FakeGluetunControl:
    """A scriptable GluetunControl for tests - no network involved.

    `statuses`, when given, drains in order and then repeats its last value
    - the same shape `FakeDockerEngine.frames` uses - so a test can model a
    tunnel whose reported status changes tick by tick. `calls` records every
    `(method, api_key)` pair, in order, so a test can prove Gluetun is asked
    only when a VPN is actually installed, and with the right key.
    """

    def __init__(
        self,
        *,
        status: str | None = "running",
        place: TunnelPlace | None = None,
        port: int | None = None,
        statuses: Sequence[str | None] = (),
    ) -> None:
        self._status = status
        self._place = place
        self._port = port
        self._statuses = list(statuses)
        self._status_position = 0
        self.calls: list[tuple[str, str]] = []

    async def vpn_status(self, api_key: str) -> str | None:
        self.calls.append(("vpn_status", api_key))
        if not self._statuses:
            return self._status
        position = self._status_position
        if position < len(self._statuses) - 1:
            self._status_position = position + 1
        return self._statuses[position]

    async def public_ip(self, api_key: str) -> TunnelPlace | None:
        self.calls.append(("public_ip", api_key))
        return self._place

    async def forwarded_port(self, api_key: str) -> int | None:
        self.calls.append(("forwarded_port", api_key))
        return self._port


def classify_tunnel(snapshot: ContainerSnapshot, logs: str) -> TunnelVerdict:
    """Turn one moment's Docker evidence into a verdict, pure and total.

    Checked in this order because AUTH_FAILED can show up in the log of a
    container Docker still calls "running" (Gluetun keeps retrying) - the
    log has to win before a health check that hasn't caught up yet reports
    a false "healthy".
    """
    if not snapshot.exists:
        return "gone"
    if _AUTH_FAILED_MARKER in logs:
        return "refused"
    if snapshot.state in ("exited", "dead", "restarting"):
        return "settings_refused"
    if snapshot.state == "running" and snapshot.health == "healthy":
        return "healthy"
    return "connecting"


def looks_like_missing_tun(compose_output: str) -> bool:
    """Is this compose failure the one Gluetun-specific fix exists for -
    the host has no `/dev/net/tun` device to hand the container?

    The only compose failure this codebase gives a VPN-specific answer to:
    every other compose error keeps its ordinary `compose_failed` handling.
    """
    lowered = compose_output.lower()
    if "/dev/net/tun" not in lowered:
        return False
    return any(phrase in lowered for phrase in _MISSING_DEVICE_PHRASES)
