"""Tests for `health.read_health`: Docker's answer, in four honest states.

Nothing here talks to a real Docker daemon - `FakeDockerEngine` stands in,
the same way it does for the deploy engine's own tests.
"""

from __future__ import annotations

from marrquee.docker_client import (
    ContainerSnapshot,
    ContainerState,
    DockerStatus,
    FakeDockerEngine,
)
from marrquee.health import AppHealth, HubState, read_health

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

    for app_id, health in zip(app_ids, results, strict=True):
        expected_state = table[containers[app_id].state]
        assert health.state == expected_state, (
            f"{app_id}: expected {expected_state}, got {health.state}"
        )
        assert health.exists is True


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
