"""Tests for GraphicsChipCheck: does the host have a graphics chip Jellyfin
can use for hardware transcoding?

Nothing here talks to a real Docker daemon - `_ScriptedProbeEngine` and
`_ExplodingProbeEngine` are minimal doubles that implement only the two
`DockerEngine` methods `GraphicsChipCheck` actually calls, the same
`cast(DockerEngine, ...)` pattern `test_plex.py`'s `_FakeGatewayEngine` uses.
"""

from __future__ import annotations

from typing import cast

from marrquee.docker_client import DockerEngine, DockerStatus, FakeDockerEngine, HostPathProbe
from marrquee.graphics_chip import GRAPHICS_DEVICE_NODE, GraphicsChipCheck


class _ScriptedProbeEngine:
    """Drains a scripted list of `probe_host_path` answers in order, then
    repeats the last one - the same "drain then repeat" shape every other
    scripted fake in this codebase follows.
    """

    def __init__(
        self, results: list[HostPathProbe], *, self_container_id: str | None = "marrquee"
    ) -> None:
        self._results = list(results)
        self._self_container_id = self_container_id
        self.self_container_calls = 0
        self.probe_calls = 0

    async def self_container_id(self) -> str | None:
        self.self_container_calls += 1
        return self._self_container_id

    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe:
        self.probe_calls += 1
        index = min(self.probe_calls - 1, len(self._results) - 1)
        return self._results[index]


class _ExplodingProbeEngine:
    """A DockerEngine double whose probe always raises - proves `has_chip`
    survives a broken or unexpected engine instead of crashing its caller.
    """

    def __init__(self) -> None:
        self.probe_calls = 0

    async def self_container_id(self) -> str | None:
        return "marrquee"

    async def probe_host_path(self, self_container: str, host_path: str) -> HostPathProbe:
        self.probe_calls += 1
        raise RuntimeError("the docker socket vanished mid-probe")


async def test_has_chip_true_only_when_the_host_reports_the_device_node_present() -> None:
    with_chip = FakeDockerEngine(DockerStatus(connected=True), host_paths={GRAPHICS_DEVICE_NODE})
    without_chip = FakeDockerEngine(DockerStatus(connected=True))

    assert await GraphicsChipCheck(with_chip).has_chip() is True
    assert await GraphicsChipCheck(without_chip).has_chip() is False


async def test_has_chip_probes_the_exact_render_node() -> None:
    fake = FakeDockerEngine(
        DockerStatus(connected=True),
        self_container_id="marrquee",
        host_paths={GRAPHICS_DEVICE_NODE},
    )

    await GraphicsChipCheck(fake).has_chip()

    assert ("probe_host_path", ("marrquee", GRAPHICS_DEVICE_NODE)) in fake.calls


async def test_has_chip_retries_an_unknown_answer_but_caches_a_definitive_one() -> None:
    engine = _ScriptedProbeEngine(
        [
            HostPathProbe(result="unknown", detail="daemon hiccup"),
            HostPathProbe(result="present", detail=None),
        ]
    )
    check = GraphicsChipCheck(cast(DockerEngine, engine))

    first = await check.has_chip()
    second = await check.has_chip()
    third = await check.has_chip()

    assert (first, second, third) == (False, True, True)
    # The cached "present" answer from the second call means the third
    # call never probes again.
    assert engine.probe_calls == 2


async def test_has_chip_caches_a_definitive_absent_answer_too() -> None:
    engine = _ScriptedProbeEngine([HostPathProbe(result="absent", detail=None)])
    check = GraphicsChipCheck(cast(DockerEngine, engine))

    first = await check.has_chip()
    second = await check.has_chip()

    assert (first, second) == (False, False)
    assert engine.probe_calls == 1


async def test_has_chip_is_false_without_probing_when_there_is_no_self_container() -> None:
    engine = _ScriptedProbeEngine(
        [HostPathProbe(result="present", detail=None)], self_container_id=None
    )
    check = GraphicsChipCheck(cast(DockerEngine, engine))

    assert await check.has_chip() is False
    assert engine.probe_calls == 0


async def test_has_chip_survives_a_probe_that_raises_and_never_caches_it() -> None:
    engine = _ExplodingProbeEngine()
    check = GraphicsChipCheck(cast(DockerEngine, engine))

    first = await check.has_chip()
    second = await check.has_chip()

    assert (first, second) == (False, False)
    assert engine.probe_calls == 2
