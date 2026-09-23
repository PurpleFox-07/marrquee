"""Tests for the seam between the deploy engine and however apps get wired
together.

The real wiring behaviour (`WiringEngine`, `plan_wiring`, the two connection
types) lives in `tests/test_wiring_engine.py` and `tests/test_wiring_steps.py`.
This module only pins the shared seam itself - `WiringStep`, `WiringRunner`
and `NoWiringYet`, the do-nothing runner `DeployManager` still defaults to -
so a later change to any of those three fails here before it fails anywhere
downstream.
"""

from __future__ import annotations

from marrquee.state import InstallState
from marrquee.wiring import NoWiringYet, WiringRunner, WiringStep

_INSTALL_STATE = InstallState(
    version=1,
    storage_root="/volume1/media",
    app_ids=("sonarr",),
    api_keys={"sonarr": "a" * 32},
    puid=1000,
    pgid=1000,
    umask="002",
    timezone="Etc/UTC",
    created="2026-09-19T00:00:00+00:00",
)


def test_wiring_step_carries_every_field_the_wiring_engine_needs() -> None:
    step = WiringStep(
        index=1,
        total=2,
        key="qbittorrent-to-sonarr",
        line="Connecting Sonarr to your downloader",
        state="running",
        chip="Connecting…",
        note=None,
        technical=None,
    )

    assert step.index == 1
    assert step.total == 2
    assert step.involved == ()  # defaulted, so a step that names no app still constructs


def test_wiring_step_can_name_which_apps_it_touches() -> None:
    step = WiringStep(
        index=1,
        total=1,
        key="sonarr-to-prowlarr",
        line="Connecting Sonarr to Prowlarr",
        state="done",
        chip="Connected",
        note=None,
        technical=None,
        involved=("sonarr", "prowlarr"),
    )

    assert step.involved == ("sonarr", "prowlarr")


async def test_no_wiring_yet_completes_without_emitting_a_single_step() -> None:
    runner = NoWiringYet()
    emitted: list[WiringStep] = []

    await runner.run(_INSTALL_STATE, emitted.append)

    assert emitted == []


def test_no_wiring_yet_satisfies_the_wiring_runner_protocol() -> None:
    """Structural assertion: mypy rejects this file if NoWiringYet ever
    drifts out of step with the WiringRunner protocol.
    """

    def _accepts(runner: WiringRunner) -> WiringRunner:
        return runner

    assert _accepts(NoWiringYet()) is not None
