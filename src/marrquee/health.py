"""What Docker says about a deployed app, in four honest states.

The Hub only ever needs one thing per app: is it up? Docker's own
`ContainerSnapshot.state` has seven values plus "the request failed
outright", and none of those shapes distinguish "the container is gone"
from "Docker didn't answer" on their own - `exists=False` is the same
whichever happened, and only `detail` tells them apart (see
`docker_client.SocketDockerEngine.inspect`). Collapsing that ambiguity the
wrong way would show "Down" for a NAS that's simply asleep, so this module
exists to do it the one honest way and nowhere else.

Read-only on purpose: `read_health` calls `inspect` and nothing else,
matching Story 1's rule that a status check never starts, stops or changes
a container.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from marrquee.docker_client import ContainerSnapshot, DockerEngine

HubState = Literal["up", "starting", "down", "unknown"]


@dataclass(frozen=True)
class AppHealth:
    """One app's Docker-reported state, for the pure view layer to word.

    `finished_at` is Docker's raw timestamp string, copied through
    unchanged - turning it into "2 hours ago" is `hub.py`'s job, not this
    module's. There is deliberately no `detail` field: `ContainerSnapshot`'s
    raw technical string never leaves this module, so "no jargon on screen"
    is enforced by the type itself.
    """

    app_id: str
    state: HubState
    exists: bool
    finished_at: str | None


def _state_of(result: object) -> tuple[HubState, bool, str | None]:
    """Map one `inspect` outcome to (state, exists, finished_at).

    Checked in this exact order because a container that's gone
    (`exists=False, detail=None`, Docker's 404 shape) and a container Docker
    couldn't be asked about (`exists=False`, `detail` set - a connection
    error, a bad status, a bad body) look identical except for `detail`.
    Reading `detail` before `state` is what keeps "Down" from ever meaning
    "we don't actually know".
    """
    if not isinstance(result, ContainerSnapshot):
        # asyncio.gather(return_exceptions=True) hands back the exception
        # itself for a failed call - never a state we can trust.
        return "unknown", False, None

    if result.exists is False:
        if result.detail is None:
            return "down", False, None
        return "unknown", False, None

    match result.state:
        case "running":
            return "up", True, result.finished_at
        case "restarting":
            return "starting", True, result.finished_at
        case "created" | "paused" | "exited" | "dead" | "removing":
            return "down", True, result.finished_at
        case None:
            return "unknown", True, result.finished_at


async def read_health(engine: DockerEngine, app_ids: Sequence[str]) -> tuple[AppHealth, ...]:
    """Ask Docker about every deployed app's container, in one batch.

    The container name asked for is the app id, matching the compose file's
    service naming. Each `inspect` runs concurrently and a single app's
    failure (an exception, a bad reply) never touches another app's
    result - `return_exceptions=True` is what makes that true.
    """
    results = await asyncio.gather(
        *(engine.inspect(app_id) for app_id in app_ids), return_exceptions=True
    )
    healths = []
    for app_id, result in zip(app_ids, results, strict=True):
        state, exists, finished_at = _state_of(result)
        healths.append(
            AppHealth(app_id=app_id, state=state, exists=exists, finished_at=finished_at)
        )
    return tuple(healths)
