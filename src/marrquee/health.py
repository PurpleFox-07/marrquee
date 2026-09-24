"""What Marrquee can tell about each card on the Hub - app or link.

The Hub only ever needs one thing per app: is it up? Docker's own
`ContainerSnapshot.state` has seven values plus "the request failed
outright", and none of those shapes distinguish "the container is gone"
from "Docker didn't answer" on their own - `exists=False` is the same
whichever happened, and only `detail` tells them apart (see
`docker_client.SocketDockerEngine.inspect`). Collapsing that ambiguity the
wrong way would show "Down" for a NAS that's simply asleep, so this module
exists to do it the one honest way and nowhere else.

A link card has no container to ask Docker about, so its answer comes from
`HttpLinkProbe` instead: one plain HTTP request, answered or not. Marrquee
checks from inside its own container, so a link that reads "down" only
means this NAS couldn't reach it just now - never that the site itself is
broken.

Read-only on purpose: `read_health` calls `inspect` and `read_link_health`
sends one `HEAD` - nothing in this module ever starts, stops or changes a
container, or writes to whatever a link points at.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from marrquee.docker_client import ContainerSnapshot, DockerEngine
from marrquee.links import LinkCard

HubState = Literal["up", "starting", "down", "unknown"]
LinkState = Literal["up", "down"]


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


@dataclass(frozen=True)
class LinkHealth:
    """One link card's checked state - Up or Down, no third option.

    Unlike a Docker app, a link has no "starting" or "not sure": either the
    address answered just now, or it didn't.
    """

    link_id: str
    state: LinkState


class LinkProbe(Protocol):
    """Answers "did this address answer at all?" for one link card."""

    async def check(self, url: str) -> bool: ...


class HttpLinkProbe:
    """Sends one `HEAD` to a link's URL and asks nothing else of it.

    `verify=False` is deliberate: a self-signed certificate is still an
    answer, and the owner's own gear is exactly the kind of thing that runs
    one. `follow_redirects=False` plus `HEAD` mean this never lands on a
    login page's redirect target or downloads a body it will never read -
    any response at all, of any status, means the address is up. Any
    exception (a refused connection, a timeout, an unparseable URL) is left
    to propagate - `read_link_health`'s `gather(return_exceptions=True)` is
    what turns it into "down", the same way `read_health` handles Docker.
    """

    def __init__(
        self, *, timeout: float = 3.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def check(self, url: str) -> bool:
        async with httpx.AsyncClient(
            verify=False,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            await client.head(url)
        return True


class FakeLinkProbe:
    """A scriptable LinkProbe for tests - no network involved.

    `up` scripts one answer per URL; a URL not in it falls back to
    `default`. `calls` records every URL asked, in order, so a test can
    prove a probe that should never run, didn't.
    """

    def __init__(self, *, up: Mapping[str, bool] | None = None, default: bool = True) -> None:
        self._up = dict(up or {})
        self._default = default
        self.calls: list[str] = []

    async def check(self, url: str) -> bool:
        self.calls.append(url)
        return self._up.get(url, self._default)


async def read_link_health(probe: LinkProbe, links: Sequence[LinkCard]) -> tuple[LinkHealth, ...]:
    """Check every link card concurrently, in the order given.

    One card's failure - a raised exception of any kind, including
    `httpx.InvalidURL`, which is deliberately not an `httpx.HTTPError` -
    never touches another card's result, matching `read_health`'s own
    `return_exceptions=True` pattern.
    """
    results = await asyncio.gather(
        *(probe.check(link.url) for link in links), return_exceptions=True
    )
    return tuple(
        LinkHealth(link_id=link.id, state="up" if result is True else "down")
        for link, result in zip(links, results, strict=True)
    )
