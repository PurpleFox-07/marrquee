"""The Docker Compose file the owner can actually read.

`build_stack_plan` turns the owner's saved choices into a `StackPlan` - the
one object the compose file is rendered from, so "the file describes what
runs" is a fact about the code rather than something that has to be kept in
sync by hand.

Rendering is hand-written string assembly, never a YAML library: the
runtime image must not carry a YAML parser it never calls, and no YAML
emitter preserves the plain-language comments that are the entire point of
this file. Tests are free to parse the *output* with PyYAML - a dev-only
dependency - to prove it is valid.
"""

from __future__ import annotations

import logging
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from marrquee.catalog import CatalogApp, apps_in_order
from marrquee.config import Settings
from marrquee.state import InstallState
from marrquee.storage import ChownFn, to_host_view
from marrquee.words import COMPOSE_FILE_HEADER_COMMENT, DATA_MOUNT_COMMENT

logger = logging.getLogger(__name__)

# The compose-spec guarantees that a network's `name:` is used literally,
# never scoped with a project name - that makes "connect our own container
# to this network" a deterministic target rather than a guess. The project
# name mirrors it for the same reason: both need to stay the same on every
# deploy, regardless of anything else, so they are fixed constants rather
# than something `build_stack_plan` derives per call.
_STACK_PROJECT = "marrquee"
_NETWORK_NAME = "marrquee"

_CONFIG_MOUNT_SUFFIX = ":/config"
_DATA_MOUNT_SUFFIX = ":/data"
_COMMENT_WIDTH = 78


@dataclass(frozen=True)
class ServicePlan:
    """Everything the compose file needs to say about one running app."""

    app_id: str
    service: str
    image: str
    container_name: str
    host_port: int
    container_port: int
    environment: tuple[tuple[str, str], ...]
    volumes: tuple[str, ...]
    comment: str


@dataclass(frozen=True)
class StackPlan:
    """The whole stack, in the one shape the compose file is rendered from.

    `puid`/`pgid` are carried here (copied straight from the `InstallState`
    that built this plan) so `write_compose` can chown the file it writes to
    the drive's own owner without re-deriving anything - the value is
    already in hand by the time a `StackPlan` exists.
    """

    project: str
    network: str
    storage_root: PurePosixPath
    services: tuple[ServicePlan, ...]
    generated_at: str
    puid: int
    pgid: int


def build_stack_plan(state: InstallState) -> StackPlan:
    """Turn a saved `InstallState` into the stack this compose file describes.

    Pure and total: the same `InstallState` always produces the same
    `StackPlan`, which is what keeps a re-deploy from producing a spurious
    diff in the file the owner reads.
    """
    if state.storage_root is None:
        raise ValueError("cannot build a stack plan before a storage root is chosen")

    root = PurePosixPath(state.storage_root)
    apps = apps_in_order(state.app_ids)
    services = tuple(_service_plan(app, state, root) for app in apps)

    return StackPlan(
        project=_STACK_PROJECT,
        network=_NETWORK_NAME,
        storage_root=root,
        services=services,
        generated_at=state.created,
        puid=state.puid,
        pgid=state.pgid,
    )


def _service_plan(app: CatalogApp, state: InstallState, root: PurePosixPath) -> ServicePlan:
    try:
        api_key = state.api_keys[app.id]
    except KeyError:
        raise ValueError(f"no API key has been generated yet for {app.id!r}") from None

    environment = (
        ("PUID", str(state.puid)),
        ("PGID", str(state.pgid)),
        ("TZ", state.timezone),
        ("UMASK", state.umask),
        (f"{app.env_prefix}__AUTH__APIKEY", api_key),
        (f"{app.env_prefix}__AUTH__METHOD", "External"),
        (f"{app.env_prefix}__AUTH__REQUIRED", "DisabledForLocalAddresses"),
    )

    config_mount = f"{root / 'marrquee' / 'apps' / app.id}{_CONFIG_MOUNT_SUFFIX}"
    volumes = [config_mount]
    if app.needs_data_mount:
        # The SAME source string every data-mounting app uses - one shared
        # filesystem is what makes a finished download move into the
        # library instantly instead of being copied a second time.
        volumes.append(f"{root / 'data'}{_DATA_MOUNT_SUFFIX}")

    return ServicePlan(
        app_id=app.id,
        service=app.id,
        image=app.image,
        container_name=app.id,
        host_port=app.port,
        container_port=app.port,
        environment=environment,
        volumes=tuple(volumes),
        comment=app.description,
    )


def render_compose(plan: StackPlan) -> str:
    """Render `plan` as a human-readable Docker Compose file.

    Fixed order - header, then services in catalog order, then the network
    block - so a diff between two deploys is something the owner could
    actually read.
    """
    lines: list[str] = [*_comment_lines(COMPOSE_FILE_HEADER_COMMENT), "services:"]

    for index, service in enumerate(plan.services):
        if index > 0:
            lines.append("")
        lines.extend(_render_service(service, plan.network))

    lines.append("")
    lines.append("networks:")
    lines.append(f"  {plan.network}:")
    lines.append(f"    name: {plan.network}")
    lines.append("    attachable: true")

    return "\n".join(lines) + "\n"


def _render_service(service: ServicePlan, network: str) -> list[str]:
    lines = [f"  {service.service}:"]
    lines.extend(f"    {comment}" for comment in _comment_lines(service.comment))
    lines.append(f"    image: {_quoted(service.image)}")
    lines.append(f"    container_name: {service.container_name}")
    lines.append("    restart: unless-stopped")
    lines.append("    ports:")
    lines.append(f"      - {_quoted(f'{service.host_port}:{service.container_port}')}")
    lines.append("    environment:")
    for key, value in service.environment:
        lines.append(f"      {key}: {_quoted(value)}")
    lines.append("    volumes:")
    for volume in service.volumes:
        if volume.endswith(_DATA_MOUNT_SUFFIX):
            lines.extend(f"      {comment}" for comment in _comment_lines(DATA_MOUNT_COMMENT))
        lines.append(f"      - {_quoted(volume)}")
    # A service with no `networks:` of its own would join compose's own
    # implicit "default" network instead of this named one - listing it
    # explicitly here is what lets every app find the others (and Marrquee
    # itself, once it joins this same network) by name.
    lines.append("    networks:")
    lines.append(f"      - {network}")
    return lines


def _comment_lines(text: str, width: int = _COMMENT_WIDTH) -> list[str]:
    """Wrap `text` into `# `-prefixed lines, so a long sentence stays readable."""
    return [f"# {wrapped}" for wrapped in textwrap.wrap(text, width=width)]


def _quoted(value: str) -> str:
    """A YAML double-quoted scalar for `value`.

    Every environment value, port pair and volume string goes through this -
    a bare `002` or a bare `yes` are the two classic silent YAML type
    surprises, and quoting everything is cheaper than remembering which
    values are at risk.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def compose_file_host_path(root: PurePosixPath) -> PurePosixPath:
    """Where the rendered compose file lives, as a HOST path under `root`."""
    return root / "marrquee" / "compose.yaml"


def write_compose(settings: Settings, plan: StackPlan, *, chown: ChownFn = os.chown) -> Path:
    """Render and atomically write `plan`'s compose file, returning its
    container-view path.

    The file carries every app's API key, so it is written the same way the
    install state is: to a sibling temp file, `chmod`'d to `0600` before it
    has any content worth reading, then landed with `os.replace` - never
    briefly world-readable, never half-written.

    Marrquee's own container runs as root, so without an explicit `chown`
    the file would land root:root - unreadable by the very owner the story
    calls this "the compose file the owner can actually read" for. It is
    chowned to `plan.puid`/`plan.pgid` (the drive's own owner) while staying
    mode `0600` - readable by that owner, still not world-readable, since it
    carries every app's key. A share that doesn't support `chown` (some NAS
    network filesystems) must never turn a successful deploy into a failed
    one, so that failure is swallowed and logged, never raised - the file is
    still there and still valid, just root-owned.
    """
    host_path = compose_file_host_path(plan.storage_root)
    container_path = to_host_view(settings, str(host_path))
    _write_atomic_text(container_path, render_compose(plan))
    try:
        chown(container_path, plan.puid, plan.pgid)
    except OSError as error:
        logger.warning("could not chown the compose file to %s:%s: %s", plan.puid, plan.pgid, error)
    return container_path


def _write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)
