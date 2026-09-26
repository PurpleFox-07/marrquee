"""Tests for `vpn_control.py`: Gluetun's authenticated control server client
and the tunnel verdict.

`httpx.MockTransport` stands in for the network - the same seam
`HttpReadinessProbe`'s own tests use - since there is no real Gluetun in
this suite. `classify_tunnel` and `looks_like_missing_tun` are pure, so they
are proven with plain values in and plain values out.
"""

from __future__ import annotations

import httpx

from marrquee.docker_client import ContainerHealth, ContainerSnapshot, ContainerState
from marrquee.vpn import TunnelPlace
from marrquee.vpn_control import (
    FakeGluetunControl,
    GluetunControl,
    HttpGluetunControl,
    NoGluetunControl,
    classify_tunnel,
    looks_like_missing_tun,
)


def _snapshot(
    *,
    exists: bool = True,
    state: ContainerState | None = "running",
    health: ContainerHealth | None = None,
) -> ContainerSnapshot:
    return ContainerSnapshot(
        name="gluetun",
        exists=exists,
        state=state,
        exit_code=None,
        image=None,
        detail=None,
        health=health,
    )


# --- HttpGluetunControl.vpn_status -------------------------------------------


async def test_vpn_status_reads_the_status_string_and_sends_the_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/vpn/status"
        assert request.headers["x-api-key"] == "the-control-key"
        return httpx.Response(200, json={"status": "running"})

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.vpn_status("the-control-key") == "running"


async def test_vpn_status_is_none_on_a_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.vpn_status("wrong-key") is None


async def test_vpn_status_never_raises_on_a_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.vpn_status("the-control-key") is None


async def test_vpn_status_is_none_on_a_non_dict_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "a", "dict"])

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.vpn_status("the-control-key") is None


# --- HttpGluetunControl.public_ip --------------------------------------------


async def test_public_ip_full_body_becomes_a_tunnel_place_and_sends_the_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/publicip/ip"
        assert request.headers["x-api-key"] == "the-control-key"
        return httpx.Response(
            200,
            json={
                "public_ip": "185.1.1.1",
                "city": "Amsterdam",
                "region": "North Holland",
                "country": "Netherlands",
            },
        )

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    place = await control.public_ip("the-control-key")

    assert place == TunnelPlace(
        public_ip="185.1.1.1", city="Amsterdam", region="North Holland", country="Netherlands"
    )


async def test_public_ip_missing_city_region_country_become_blank_strings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"public_ip": "185.1.1.1"})

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    place = await control.public_ip("the-control-key")

    assert place == TunnelPlace(public_ip="185.1.1.1", city="", region="", country="")


async def test_public_ip_is_none_when_the_address_itself_is_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"public_ip": "", "city": "Amsterdam"})

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.public_ip("the-control-key") is None


async def test_public_ip_is_none_on_a_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.public_ip("wrong-key") is None


async def test_public_ip_is_none_on_a_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.public_ip("the-control-key") is None


# --- HttpGluetunControl.forwarded_port ---------------------------------------


async def test_forwarded_port_prefers_the_first_of_ports() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/portforward"
        return httpx.Response(200, json={"port": 0, "ports": [51413]})

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.forwarded_port("the-control-key") == 51413


async def test_forwarded_port_is_none_when_port_and_ports_are_both_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"port": 0, "ports": []})

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.forwarded_port("the-control-key") is None


async def test_forwarded_port_is_none_on_a_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    control = HttpGluetunControl(transport=httpx.MockTransport(handler))

    assert await control.forwarded_port("the-control-key") is None


# --- NoGluetunControl ----------------------------------------------------------


async def test_no_gluetun_control_answers_none_to_everything() -> None:
    control = NoGluetunControl()
    assert await control.vpn_status("any-key") is None
    assert await control.public_ip("any-key") is None
    assert await control.forwarded_port("any-key") is None


# --- classify_tunnel ----------------------------------------------------------


def test_classify_a_gone_container_before_anything_else() -> None:
    snapshot = _snapshot(exists=False, state=None)

    assert classify_tunnel(snapshot, "") == "gone"


def test_classify_auth_failed_wins_over_a_running_healthy_container() -> None:
    """The one Gluetun-specific "your login is wrong" signal has to outrank
    a Docker health check that hasn't caught up yet - AUTH_FAILED means the
    tunnel will never come up no matter how long we wait.
    """
    snapshot = _snapshot(state="running", health="healthy")

    verdict = classify_tunnel(
        snapshot, "...AUTH: Received control message: AUTH_FAILED: cannot connect..."
    )

    assert verdict == "refused"


def test_classify_auth_failed_wins_over_restarting_too() -> None:
    """AUTH_FAILED is checked before the exited/dead/restarting group, not
    just before "running and healthy" - a container Gluetun keeps restarting
    over bad credentials must still be reported as the wrong-login failure,
    not the generic settings-refused one.
    """
    snapshot = _snapshot(state="restarting")

    verdict = classify_tunnel(snapshot, "...AUTH: Received control message: AUTH_FAILED...")

    assert verdict == "refused"


def test_classify_restarting_is_settings_refused() -> None:
    snapshot = _snapshot(state="restarting")

    assert classify_tunnel(snapshot, "") == "settings_refused"


def test_classify_exited_and_dead_are_also_settings_refused() -> None:
    assert classify_tunnel(_snapshot(state="exited"), "") == "settings_refused"
    assert classify_tunnel(_snapshot(state="dead"), "") == "settings_refused"


def test_classify_running_and_healthy_is_healthy() -> None:
    snapshot = _snapshot(state="running", health="healthy")

    assert classify_tunnel(snapshot, "") == "healthy"


def test_classify_running_but_not_yet_healthy_is_connecting() -> None:
    assert classify_tunnel(_snapshot(state="running", health="starting"), "") == "connecting"
    assert classify_tunnel(_snapshot(state="running", health=None), "") == "connecting"


# --- looks_like_missing_tun ---------------------------------------------------


def test_looks_like_missing_tun_matches_the_compose_device_error() -> None:
    output = (
        "error gathering device information while adding custom device "
        "/dev/net/tun: no such file or directory"
    )

    assert looks_like_missing_tun(output) is True


def test_looks_like_missing_tun_is_case_insensitive() -> None:
    output = "ERROR GATHERING DEVICE INFORMATION for /DEV/NET/TUN"

    assert looks_like_missing_tun(output) is True


def test_looks_like_missing_tun_is_false_for_an_unrelated_compose_error() -> None:
    assert looks_like_missing_tun("pull access denied for qmcgaw/gluetun") is False


def test_looks_like_missing_tun_is_false_when_tun_is_mentioned_without_a_missing_file() -> None:
    assert looks_like_missing_tun("/dev/net/tun mounted and ready") is False


def test_looks_like_missing_tun_is_false_when_the_missing_phrase_names_something_else() -> None:
    assert looks_like_missing_tun("no such file or directory: /config/settings.json") is False


# --- FakeGluetunControl --------------------------------------------------------


async def test_fake_gluetun_control_satisfies_the_protocol_and_records_every_call() -> None:
    """Structural assertion: mypy rejects this file if FakeGluetunControl
    ever drifts out of step with the GluetunControl protocol.
    """

    def _accepts(control: GluetunControl) -> GluetunControl:
        return control

    fake = FakeGluetunControl(
        status="running",
        place=TunnelPlace(public_ip="1.2.3.4", city="", region="", country=""),
        port=51413,
    )
    assert _accepts(fake) is fake

    status = await fake.vpn_status("key")
    place = await fake.public_ip("key")
    port = await fake.forwarded_port("key")

    assert status == "running"
    assert place == TunnelPlace(public_ip="1.2.3.4", city="", region="", country="")
    assert port == 51413
    assert fake.calls == [
        ("vpn_status", "key"),
        ("public_ip", "key"),
        ("forwarded_port", "key"),
    ]


async def test_fake_gluetun_control_drains_scripted_statuses_then_repeats_the_last() -> None:
    """A tunnel that starts stopped and later comes up is state a fake must
    be able to model over several ticks, the same drain-then-repeat shape
    `FakeDockerEngine.frames` already uses for a container's health.
    """
    fake = FakeGluetunControl(statuses=["stopped", "running"])

    results = [await fake.vpn_status("key") for _ in range(3)]

    assert results == ["stopped", "running", "running"]


async def test_fake_gluetun_control_defaults_to_a_healthy_tunnel_with_no_place() -> None:
    fake = FakeGluetunControl()

    assert await fake.vpn_status("key") == "running"
    assert await fake.public_ip("key") is None
    assert await fake.forwarded_port("key") is None
