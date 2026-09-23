"""The container's entry point: `python -m marrquee` starts the app.

This is the only place the real app is served, so the host and port have
exactly one source of truth - the same `Settings` the rest of the app reads.
`tools/dev_fake_server.py` also calls `uvicorn.run`, but only to serve a
Docker-less demo copy for the Mac preview - it never ships.
"""

from __future__ import annotations

import uvicorn

from marrquee.config import Settings
from marrquee.main import create_app


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
