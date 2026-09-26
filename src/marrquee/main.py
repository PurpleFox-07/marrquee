"""The FastAPI application factory.

`create_app` is the seam every later story's tests build on: pass a fake
`Settings` and a fake `DockerEngine` and get back a fully wired app that
never touches a real filesystem path or Docker socket.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from marrquee.config import Settings
from marrquee.deploy import DeployManager, HttpReadinessProbe, ReadinessProbe
from marrquee.docker_client import DockerEngine, SocketDockerEngine
from marrquee.hardlinks import HardlinkMonitor
from marrquee.health import HttpLinkProbe, LinkProbe
from marrquee.login_apply import HttpLoginApplier, LoginApplier
from marrquee.plex import HttpPlexServer, HttpPlexTv, PlexServer, PlexTv, plex_host_address
from marrquee.recyclarr import RecyclarrControl, RecyclarrMonitor
from marrquee.routes.alive import router as alive_router
from marrquee.routes.api import router as api_router
from marrquee.routes.deploy import router as deploy_router
from marrquee.routes.hub import router as hub_router
from marrquee.routes.plex import PlexSignInStore
from marrquee.routes.plex import router as plex_router
from marrquee.routes.wizard import router as wizard_router
from marrquee.same_origin import SameOriginGuard
from marrquee.vpn_control import GluetunControl, HttpGluetunControl
from marrquee.wiring import WiringRunner
from marrquee.wiring.engine import WiringEngine
from marrquee.wiring.qbit_client import HttpQbitClient, QbitClient

# Resolved from the installed package, not the repository: the runtime image
# copies only the built venv (no `src/` tree survives), so a path built from
# the repo root would work on a dev machine and break the moment it ships.
_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"


def create_app(
    settings: Settings | None = None,
    engine: DockerEngine | None = None,
    *,
    manager: DeployManager | None = None,
    probe: ReadinessProbe | None = None,
    wiring: WiringRunner | None = None,
    link_probe: LinkProbe | None = None,
    login_applier: LoginApplier | None = None,
    vpn_control: GluetunControl | None = None,
    qbit_client: QbitClient | None = None,
    hardlinks: HardlinkMonitor | None = None,
    recyclarr: RecyclarrControl | None = None,
    plex_tv: PlexTv | None = None,
    plex_server: PlexServer | None = None,
) -> FastAPI:
    """Build the Marrquee app.

    `settings=None` reads the real environment; `engine=None` talks to the
    real Docker socket those settings name, through the pinned compose
    binary those same settings point at. `manager=None` builds one
    `DeployManager` from `settings` and `engine` - a test that only needs to
    control readiness or wiring passes `probe=`/`wiring=` instead of
    building and injecting a whole manager itself. `wiring=None` builds a
    real `WiringEngine` here, at the one place the live app is assembled -
    `DeployManager`'s own default stays the do-nothing `NoWiringYet`, so a
    test that builds a `DeployManager` directly never touches the network.
    `login_applier=None` builds a real `HttpLoginApplier` the same way -
    `DeployManager`'s own default stays the honest `NoLoginApplier`.
    `link_probe=None` builds a real `HttpLinkProbe`, the same pattern as
    `probe` - a Hub test passes a `FakeLinkProbe` so no test ever reaches
    the network to check a link card. `vpn_control=None` builds one real
    `HttpGluetunControl()`, handed to a freshly-built `DeployManager` as
    `vpn=` AND kept on `app.state.vpn_control` for the Hub's own status read
    (Story 5) - the same single instance either way, so a test that passes
    its own `manager=` still needs to pass `vpn_control=` too if the Hub
    route under test should see that same fake. `qbit_client=None` builds
    one real `HttpQbitClient()` the same way - the ONE door every
    qBittorrent call goes through, shared by the deploy manager's own
    bring-up/readiness check and its `HttpLoginApplier`, and kept on
    `app.state.qbit_client` so a later chunk's `WiringEngine` can be handed
    that same instance without re-plumbing this seam. `hardlinks=None`
    builds one real `HardlinkMonitor(settings)`, handed to a freshly-built
    `DeployManager` as `hardlinks=` AND kept on `app.state.hardlinks` for
    the Hub and Diagnostics to read - the same single instance either way,
    so a test that passes its own `manager=` still needs to pass
    `hardlinks=` too if that manager should ask for the same checks.
    `recyclarr=None` builds one real `RecyclarrMonitor(settings, engine)` the
    same way, handed to a freshly-built `DeployManager` as `recyclarr=` AND
    kept on `app.state.recyclarr` for the Hub to read. `plex_tv=None` and
    `plex_server=None` each build one real `HttpPlexTv()`/`HttpPlexServer()`
    the same way, handed to a freshly-built `DeployManager` as
    `plex_tv=`/`plex_server=` AND kept on `app.state.plex_tv`/
    `app.state.plex_server` for the sign-in routes to share. `plex_server` is
    also handed to a freshly-built `WiringEngine` (only when no `wiring=` is
    injected) alongside a closure over `engine` that resolves Marrquee's own
    reachable address fresh on every wiring run - the wiring engine never
    caches that address itself. An injected `manager=` keeps whichever pair
    it was itself built with - a test that passes its own manager still
    needs its own `plex_tv=`/`plex_server=` too if a route under test should
    see that same fake. `app.state.plex_sign_in` is a fresh, in-memory
    `PlexSignInStore` every time - the sign-in round trip's own pending pins
    are never worth injecting or persisting.
    """
    if settings is None:
        settings = Settings.from_env()
    if engine is None:
        engine = SocketDockerEngine(settings.docker_socket, compose_binary=settings.compose_binary)
    if vpn_control is None:
        vpn_control = HttpGluetunControl()
    if qbit_client is None:
        qbit_client = HttpQbitClient()
    if hardlinks is None:
        hardlinks = HardlinkMonitor(settings)
    if recyclarr is None:
        recyclarr = RecyclarrMonitor(settings, engine)
    if plex_tv is None:
        plex_tv = HttpPlexTv()
    if plex_server is None:
        plex_server = HttpPlexServer()

    async def _plex_address() -> str | None:
        # Resolved fresh on every wiring run (see `WiringEngine.run`), never
        # cached here - Marrquee's own container can in principle change
        # which network's gateway is reachable between one run and the next.
        return await plex_host_address(engine, await engine.self_container_id())

    if manager is None:
        manager = DeployManager(
            settings,
            engine,
            probe=probe if probe is not None else HttpReadinessProbe(),
            wiring=(
                wiring
                if wiring is not None
                else WiringEngine(
                    qbit=qbit_client,
                    vpn=vpn_control,
                    config_dir=settings.config_dir,
                    plex=plex_server,
                    plex_address=_plex_address,
                )
            ),
            login=(
                login_applier if login_applier is not None else HttpLoginApplier(qbit=qbit_client)
            ),
            vpn=vpn_control,
            qbit=qbit_client,
            hardlinks=hardlinks,
            recyclarr=recyclarr,
            plex_tv=plex_tv,
            plex_server=plex_server,
        )
    if link_probe is None:
        link_probe = HttpLinkProbe()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await manager.resume_if_interrupted()
        yield

    app = FastAPI(title="Marrquee", lifespan=lifespan)
    app.add_middleware(SameOriginGuard)
    app.state.settings = settings
    app.state.docker_engine = engine
    app.state.deploy = manager
    app.state.link_probe = link_probe
    app.state.vpn_control = vpn_control
    app.state.qbit_client = qbit_client
    app.state.hardlinks = hardlinks
    app.state.recyclarr = recyclarr
    app.state.plex_tv = plex_tv
    app.state.plex_server = plex_server
    app.state.plex_sign_in = PlexSignInStore()
    app.state.templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    app.include_router(alive_router)
    app.include_router(api_router)
    app.include_router(deploy_router)
    app.include_router(hub_router)
    app.include_router(wizard_router)
    app.include_router(plex_router)

    return app
