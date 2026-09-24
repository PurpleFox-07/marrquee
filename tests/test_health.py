"""Tests for `health`: what Marrquee can tell about each card, app or link.

Nothing here talks to a real Docker daemon or a real address -
`FakeDockerEngine` stands in for Docker, and `httpx.MockTransport` (or a
hand-written `LinkProbe`) stands in for the network, the same way it does
for the deploy engine's own readiness-probe tests.
"""

from __future__ import annotations

import httpx

from marrquee import health
from marrquee.docker_client import (
    ContainerSnapshot,
    ContainerState,
    DockerStatus,
    FakeDockerEngine,
)
from marrquee.health import AppHealth, HubState, LinkHealth, read_health, read_link_health
from marrquee.links import LinkCard

_STATUS = DockerStatus(connected=True, version="27.3.1")


def _snapshot(
    name: str,
    *,
    exists: bool,
    state: ContainerState | None = None,
    detail: str | None = None,
    finished_at: str | None = None,
) -> ContainerSnapshot:
    return ContainerSnapshot(
        name=name,
        exists=exists,
        state=state,
        exit_code=None,
        image=None,
        detail=detail,
        finished_at=finished_at,
    )


class _OneAppRaisesEngine(FakeDockerEngine):
    """A FakeDockerEngine whose `inspect` blows up for one named app only."""

    def __init__(self, *args: object, raises_for: str, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._raises_for = raises_for

    async def inspect(self, name: str) -> ContainerSnapshot:
        if name == self._raises_for:
            self.calls.append(("inspect", (name,)))
            raise RuntimeError("the socket vanished mid-request")
        return await super().inspect(name)


async def test_docker_unreachable_is_not_sure_but_a_missing_container_is_down():
    """FIRST TEST - the mapping's one real trap: a 404 (the container is
    genuinely gone) and a connection error (Docker didn't answer) both come
    back as `exists=False`, and only `detail` tells them apart. Checking
    `detail is None` and not truthiness matters because a real connection
    error can stringify to an empty string.
    """
    engine = FakeDockerEngine(
        _STATUS,
        containers={
            "gone": _snapshot("gone", exists=False, detail=None),
            "unreachable": _snapshot("unreachable", exists=False, detail="connection refused"),
            "empty-detail": _snapshot("empty-detail", exists=False, detail=""),
        },
    )

    results = await read_health(engine, ["gone", "unreachable", "empty-detail"])

    gone, unreachable, empty_detail = results
    assert gone == AppHealth(app_id="gone", state="down", exists=False, finished_at=None)
    assert unreachable.state == "unknown"
    assert unreachable.exists is False
    assert empty_detail.state == "unknown"
    assert empty_detail.exists is False


async def test_every_docker_status_maps_to_exactly_one_hub_state():
    table: dict[ContainerState | None, HubState] = {
        "running": "up",
        "restarting": "starting",
        "created": "down",
        "paused": "down",
        "exited": "down",
        "dead": "down",
        "removing": "down",
        None: "unknown",
    }

    containers = {
        name: _snapshot(name, exists=True, state=state)
        for name, state in ((str(state), state) for state in table)
    }
    engine = FakeDockerEngine(_STATUS, containers=containers)
    app_ids = [str(state) for state in table]

    results = await read_health(engine, app_ids)

    for app_id, reading in zip(app_ids, results, strict=True):
        expected_state = table[containers[app_id].state]
        assert reading.state == expected_state, (
            f"{app_id}: expected {expected_state}, got {reading.state}"
        )
        assert reading.exists is True


async def test_an_engine_that_raises_for_one_app_leaves_the_others_intact():
    engine = _OneAppRaisesEngine(
        _STATUS,
        containers={
            "sonarr": _snapshot("sonarr", exists=True, state="running"),
            "radarr": _snapshot("radarr", exists=True, state="running"),
        },
        raises_for="sonarr",
    )

    sonarr, radarr = await read_health(engine, ["sonarr", "radarr"])

    assert sonarr.state == "unknown"
    assert sonarr.app_id == "sonarr"
    assert radarr.state == "up"
    assert radarr.app_id == "radarr"


async def test_results_come_back_in_the_order_the_ids_were_given():
    engine = FakeDockerEngine(
        _STATUS,
        containers={
            "radarr": _snapshot("radarr", exists=True, state="running"),
            "sonarr": _snapshot("sonarr", exists=True, state="exited"),
            "prowlarr": _snapshot("prowlarr", exists=True, state="restarting"),
        },
    )

    results = await read_health(engine, ["sonarr", "prowlarr", "radarr"])

    assert [health.app_id for health in results] == ["sonarr", "prowlarr", "radarr"]
    assert [health.state for health in results] == ["down", "starting", "up"]


async def test_finished_at_passes_through_untouched():
    engine = FakeDockerEngine(
        _STATUS,
        containers={
            "radarr": _snapshot(
                "radarr", exists=True, state="exited", finished_at="2026-09-20T10:00:00.123456Z"
            ),
            "sonarr": _snapshot("sonarr", exists=True, state="running", finished_at=None),
        },
    )

    radarr, sonarr = await read_health(engine, ["radarr", "sonarr"])

    assert radarr.finished_at == "2026-09-20T10:00:00.123456Z"
    assert sonarr.finished_at is None


async def test_read_health_only_ever_calls_inspect():
    engine = FakeDockerEngine(
        _STATUS,
        containers={"sonarr": _snapshot("sonarr", exists=True, state="running")},
    )

    await read_health(engine, ["sonarr"])

    assert engine.calls == [("inspect", ("sonarr",))]


# --- Link health: HttpLinkProbe, FakeLinkProbe and read_link_health ----------


async def test_the_real_probe_trusts_self_signed_certs_uses_head_and_no_redirects(monkeypatch):
    """FIRST TEST - HttpLinkProbe's three defining choices, pinned by
    construction since no self-signed TLS server exists in this suite: it
    never checks the certificate, it sends the lightest possible request,
    and it never walks a redirect chain.
    """
    captured: dict[str, object] = {}

    class _RecordingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        async def __aenter__(self) -> _RecordingClient:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        async def head(self, url: str) -> httpx.Response:
            captured["method"] = "HEAD"
            captured["url"] = url
            return httpx.Response(200)

    monkeypatch.setattr(health.httpx, "AsyncClient", _RecordingClient)

    probe = health.HttpLinkProbe()
    result = await probe.check("http://192.168.1.20:8123")

    assert result is True
    assert captured["verify"] is False
    assert captured["follow_redirects"] is False
    assert captured["transport"] is None
    assert captured["method"] == "HEAD"
    assert captured["url"] == "http://192.168.1.20:8123"


async def test_any_http_status_counts_as_up() -> None:
    for status in (200, 302, 401, 405, 500):

        def handler(request: httpx.Request, status: int = status) -> httpx.Response:
            assert request.method == "HEAD"
            return httpx.Response(status)

        probe = health.HttpLinkProbe(transport=httpx.MockTransport(handler))

        assert await probe.check("http://example.com") is True


async def test_a_connection_failure_or_timeout_counts_as_down_through_read_link_health() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    link = LinkCard(id="0123456789abcdef", label="Router", url="http://192.168.1.1")

    refused_probe = health.HttpLinkProbe(transport=httpx.MockTransport(refused))
    timeout_probe = health.HttpLinkProbe(transport=httpx.MockTransport(timed_out))

    refused_result = await read_link_health(refused_probe, [link])
    timeout_result = await read_link_health(timeout_probe, [link])

    assert refused_result == (LinkHealth(link_id=link.id, state="down"),)
    assert timeout_result == (LinkHealth(link_id=link.id, state="down"),)


class _RaisesInvalidUrlProbe:
    """A LinkProbe that raises `httpx.InvalidURL` - deliberately NOT an
    `httpx.HTTPError` - to prove `read_link_health` catches any exception,
    not just the ones HTTP itself defines.
    """

    async def check(self, url: str) -> bool:
        raise httpx.InvalidURL("nope")


async def test_a_probe_raising_invalid_url_still_reads_down() -> None:
    link = LinkCard(id="fedcba9876543210", label="Bad", url="not-a-url")

    result = await read_link_health(_RaisesInvalidUrlProbe(), [link])

    assert result == (LinkHealth(link_id=link.id, state="down"),)


async def test_one_broken_link_never_touches_another_links_result() -> None:
    good = LinkCard(id="0000000000000001", label="Good", url="http://good")
    bad = LinkCard(id="0000000000000002", label="Bad", url="http://bad")

    class _OneRaisesProbe:
        async def check(self, url: str) -> bool:
            if url == "http://bad":
                raise RuntimeError("the socket vanished mid-request")
            return True

    result = await read_link_health(_OneRaisesProbe(), [bad, good])

    assert [reading.state for reading in result] == ["down", "up"]


async def test_fake_link_probe_scripts_per_url_and_records_every_call() -> None:
    probe = health.FakeLinkProbe(up={"http://down-one": False}, default=True)

    assert await probe.check("http://down-one") is False
    assert await probe.check("http://anything-else") is True
    assert probe.calls == ["http://down-one", "http://anything-else"]


async def test_read_link_health_returns_one_reading_per_link_in_order() -> None:
    first = LinkCard(id="1111111111111111", label="One", url="http://one")
    second = LinkCard(id="2222222222222222", label="Two", url="http://two")
    probe = health.FakeLinkProbe(up={"http://one": True, "http://two": False})

    result = await read_link_health(probe, [first, second])

    assert result == (
        LinkHealth(link_id=first.id, state="up"),
        LinkHealth(link_id=second.id, state="down"),
    )
