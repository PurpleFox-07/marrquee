"""The seam between the deploy engine and however the deployed apps get
connected together.

`DeployManager`'s own default stays `NoWiringYet` - a `DeployManager` built
on its own (the shape most of this project's tests use) does no real wiring
and touches no network. The live app wires for real: `create_app` builds a
`WiringEngine` (from `marrquee.wiring.engine`) and hands it in as `wiring=`
instead. Either way, the deploy engine always constructs a `DeployManager`
with a `wiring=` runner and always calls `run(state, emit)` the same way.

This package never imports from `deploy` - the dependency runs one
direction only, so the wiring seam stays a plain, testable interface rather
than growing a circular link back into the engine that owns it. It also
never imports `marrquee.wiring.engine` - that module imports from here, and
importing it back would be a circular import.
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
    """`DeployManager`'s own default runner - not "nothing is wired anywhere".

    Completes immediately without emitting a single step, so any test that
    builds a `DeployManager` directly touches no network and waits no real
    second. The live app never sees this one: `create_app` hands in a real
    `WiringEngine` instead.
    """

    async def run(self, state: InstallState, emit: Callable[[WiringStep], None]) -> None:
        return None
