"""Whether the host has a graphics chip Jellyfin can use for hardware
transcoding (VA-API).

Marrquee's own container only ever mounts `/volume1` and the Docker socket -
`/dev` inside it is its own, not the host's - so the only honest way to
answer "does the NAS have a graphics chip" is to ask the Docker daemon
itself, through `DockerEngine.probe_host_path` (docker_client.py). This
module never talks to Docker directly; it only orchestrates that one probe
and remembers a definitive answer for the life of the process.
"""

from __future__ import annotations

import asyncio
from typing import Final

from marrquee.docker_client import DockerEngine

# The exact device node Jellyfin opens for VA-API - classifying this node,
# not the whole `/dev/dri` folder, matters because a folder holding only
# `card0` (a display-only device, no render node) would still look present
# even though Jellyfin has nothing usable to open.
GRAPHICS_DEVICE_NODE: Final = "/dev/dri/renderD128"
GRAPHICS_DEVICE_DIR: Final = "/dev/dri"


class GraphicsChipCheck:
    """Asks Docker, at most once per definitive answer, whether the host has
    a graphics chip.

    A present/absent answer is cached for the life of the process - the
    host's own hardware never changes while Marrquee is running. An
    inconclusive answer (no self container, a probe that couldn't tell, or
    one that raised outright) is never cached, so a transient hiccup keeps
    the question honestly retryable rather than freezing it at "no chip"
    forever. An `asyncio.Lock` means concurrent callers (the wizard and the
    Hub can both ask in the same moment) share one probe rather than racing
    Docker with two.
    """

    def __init__(self, engine: DockerEngine) -> None:
        self._engine = engine
        self._cached: bool | None = None
        self._lock = asyncio.Lock()

    async def has_chip(self) -> bool:
        async with self._lock:
            if self._cached is not None:
                return self._cached

            try:
                self_id = await self._engine.self_container_id()
                if self_id is None:
                    return False
                probe = await self._engine.probe_host_path(self_id, GRAPHICS_DEVICE_NODE)
            except Exception:
                # Never let a broken or unexpected engine take the wizard or
                # the Hub down with it - "couldn't tell" and "no chip" look
                # the same to an owner either way, and this answer is never
                # cached, so the next ask gets a fresh try.
                return False

            if probe.result == "unknown":
                return False

            self._cached = probe.result == "present"
            return self._cached
