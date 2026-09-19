"""The seam between the deploy engine and however the deployed apps get
connected together.

The deploy engine owns this seam and ships `NoWiringYet` as its default -
there is nothing to wire yet, since deploying apps and connecting them to
each other are two different jobs. A real `WiringRunner` can replace
`NoWiringYet` later without the deploy engine changing how it calls one: it
always constructs a `DeployManager` with a `wiring=` runner and always calls
`run(state, emit)` the same way.

This package never imports from `deploy` - the dependency runs one
direction only, so the wiring seam stays a plain, testable interface rather
than growing a circular link back into the engine that owns it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from marrquee.state import InstallState

WiringStepState = Literal["running", "done", "skipped", "error"]


@dataclass(frozen=True)
class WiringStep:
    """One step of connecting the deployed apps together, reported as it happens.

    `technical` is the only field here that ever carries a raw API string,
    and it travels no further than the deploy engine's own emit adapter -
    which strips it into the diagnostics file before the step reaches a
    snapshot anyone can see. `involved` names the app ids this step touches,
    so a screen can highlight exactly those apps while the step runs.
    """

    index: int
    total: int
    key: str
    line: str
    state: WiringStepState
    chip: str
    note: str | None
    technical: str | None
    involved: tuple[str, ...] = ()


class WiringRunner(Protocol):
    """Connects the deployed apps together, reporting progress as it goes.

    Must never raise: by the time this runs, every app is already `done` -
    a wiring problem is not a deploy failure, so every problem here has to
    become an emitted step with `state="error"` instead of an exception that
    could turn an otherwise-successful deploy into one that looks failed.
    """

    async def run(self, state: InstallState, emit: Callable[[WiringStep], None]) -> None: ...


class NoWiringYet:
    """The default runner for as long as nothing needs wiring together.

    Completes immediately without emitting a single step, so the deploy
    engine's wiring phase is near-instant until a real runner replaces this
    one.
    """

    async def run(self, state: InstallState, emit: Callable[[WiringStep], None]) -> None:
        return None
