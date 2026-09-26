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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from marrquee import storage
from marrquee.catalog import CatalogApp, apps_in_order
from marrquee.config import Settings
from marrquee.state import InstallState
from marrquee.storage import ChownFn, to_host_view
from marrquee.vpn import build_gluetun_config
from marrquee.words import (
    COMPOSE_FILE_HEADER_COMMENT,
    DATA_MOUNT_COMMENT,
    VPN_SECRETS_MOUNT_COMMENT,
)

logger = logging.getLogger(__name__)

# The compose-spec guarantees that a network's `name:` is used literally,
# never scoped with a project name - that makes "connect our own container
# to this network" a deterministic target rather than a guess. The project
# name mirrors it for the same reason: both need to stay the same on every
# deploy, regardless of anything else, so they are fixed constants rather
# than something `build_stack_plan` derives per call.
# Deliberately NOT "marrquee": that is the name owners give the NAS Docker
# app project that runs Marrquee itself, and a NAS "Redeploy" of that
# project removes any other container carrying the same project label -
# which would take Prowlarr, Sonarr and Radarr down with it.
_STACK_PROJECT = "marrquee-apps"
_NETWORK_NAME = "marrquee"

_CONFIG_MOUNT_SUFFIX = ":/config"
_DATA_MOUNT_SUFFIX = ":/data"
# Gluetun's own compose example mounts its config folder at /gluetun, not
# /config - keeping the suffix distinct is what stops a later refactor from
# quietly folding the two mounts together and handing Gluetun a path it
# never asked for.
_VPN_CONFIG_MOUNT_SUFFIX = ":/gluetun"
# Read-only: Gluetun reads its login from here, but nothing it does should
# ever be able to write back into Marrquee's own root-only copy of it.
_VPN_SECRETS_MOUNT_SUFFIX = ":/run/secrets:ro"
_COMMENT_WIDTH = 78


@dataclass(frozen=True)
class ServicePlan:
    """Everything the compose file needs to say about one running app.

    `cap_add`/`devices` default to empty - only Gluetun's branch of
    `_service_plan` ever sets them, so every arr service's rendered output
    stays exactly as it was before this story.
    """

    app_id: str
    service: str
    image: str
    container_name: str
    ports: tuple[tuple[int, int], ...]
    environment: tuple[tuple[str, str], ...]
    volumes: tuple[str, ...]
    comment: str
    cap_add: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()


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


def build_stack_plan(
    state: InstallState, answers: Mapping[str, Mapping[str, str]] | None = None
) -> StackPlan:
    """Turn a saved `InstallState` into the stack this compose file describes.

    `answers` is the saved per-app question answers (`questions.load_answers`)
    - every caller passes them, even when nothing in `state.app_ids` needs
    one yet, so a later app added to `state` never has to be threaded
    through as a special case. Pure and total given the same two inputs:
    the same `InstallState` and the same answers always produce the same
    `StackPlan`, which is what keeps a re-deploy from producing a spurious
    diff in the file the owner reads.
    """
    if state.storage_root is None:
        raise ValueError("cannot build a stack plan before a storage root is chosen")

    root = PurePosixPath(state.storage_root)
    apps = apps_in_order(state.app_ids)
    resolved_answers: Mapping[str, Mapping[str, str]] = answers if answers is not None else {}
    services = tuple(_service_plan(app, state, root, resolved_answers) for app in apps)

    return StackPlan(
        project=_STACK_PROJECT,
        network=_NETWORK_NAME,
        storage_root=root,
        services=services,
        generated_at=state.created,
        puid=state.puid,
        pgid=state.pgid,
    )


def _service_plan(
    app: CatalogApp,
    state: InstallState,
    root: PurePosixPath,
    answers: Mapping[str, Mapping[str, str]],
) -> ServicePlan:
    # Checked before the arr-only api_keys lookup below: Gluetun has no API
    # key of its own to authenticate an arr app's login, and takes none of
    # the `*__AUTH__*` environment those apps get - faking either would be
    # exactly the "arr fields on a non-arr app" this kind split exists to
    # avoid.
    if app.kind == "vpn":
        return _vpn_service_plan(app, state, root, answers)

    try:
        api_key = state.api_keys[app.id]
    except KeyError:
        raise ValueError(f"no API key has been generated yet for {app.id!r}") from None

    environment: list[tuple[str, str]] = [
        ("PUID", str(state.puid)),
        ("PGID", str(state.pgid)),
        ("TZ", state.timezone),
        ("UMASK", state.umask),
        (f"{app.env_prefix}__AUTH__APIKEY", api_key),
    ]
    if app.login_kind == "arr":
        # Every app that takes the one saved login always asks for it - the
        # owner's env-set choice, not something an app's own config.xml can
        # override (`ConfigFileProvider` reads these before anything on
        # disk). The exact case matters: `Enum.TryParse` is case-sensitive.
        environment.append((f"{app.env_prefix}__AUTH__METHOD", "Forms"))
        environment.append((f"{app.env_prefix}__AUTH__REQUIRED", "Enabled"))

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
        ports=((app.port, app.port),),
        environment=tuple(environment),
        volumes=tuple(volumes),
        comment=app.description,
    )


def _vpn_service_plan(
    app: CatalogApp,
    state: InstallState,
    root: PurePosixPath,
    answers: Mapping[str, Mapping[str, str]],
) -> ServicePlan:
    try:
        control_key = state.api_keys[app.id]
    except KeyError:
        raise ValueError(f"no control-server key has been generated yet for {app.id!r}") from None

    config = build_gluetun_config(
        answers.get(app.id, {}),
        control_key=control_key,
        timezone=state.timezone,
        puid=state.puid,
        pgid=state.pgid,
    )

    config_mount = f"{root / 'marrquee' / 'apps' / app.id}{_VPN_CONFIG_MOUNT_SUFFIX}"
    secrets_mount = f"{vpn_secrets_host_path(root)}{_VPN_SECRETS_MOUNT_SUFFIX}"

    return ServicePlan(
        app_id=app.id,
        service=app.id,
        image=app.image,
        container_name=app.id,
        ports=(),
        environment=config.environment,
        volumes=(config_mount, secrets_mount),
        comment=app.description,
        cap_add=("NET_ADMIN",),
        devices=("/dev/net/tun:/dev/net/tun",),
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
    if service.cap_add:
        lines.append("    cap_add:")
        lines.extend(f"      - {_quoted(value)}" for value in service.cap_add)
    if service.devices:
        lines.append("    devices:")
        lines.extend(f"      - {_quoted(value)}" for value in service.devices)
    if service.ports:
        lines.append("    ports:")
        lines.extend(
            f"      - {_quoted(f'{host_port}:{container_port}')}"
            for host_port, container_port in service.ports
        )
    lines.append("    environment:")
    for key, value in service.environment:
        lines.append(f"      {key}: {_quoted(value)}")
    lines.append("    volumes:")
    for volume in service.volumes:
        if volume.endswith(_DATA_MOUNT_SUFFIX):
            lines.extend(f"      {comment}" for comment in _comment_lines(DATA_MOUNT_COMMENT))
        elif volume.endswith(_VPN_SECRETS_MOUNT_SUFFIX):
            lines.extend(
                f"      {comment}" for comment in _comment_lines(VPN_SECRETS_MOUNT_COMMENT)
            )
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


def vpn_secrets_host_path(root: PurePosixPath) -> PurePosixPath:
    """Where Gluetun's secret files live, as a HOST path under `root`."""
    return root / "marrquee" / "vpn"


def write_vpn_secrets(settings: Settings, root: PurePosixPath, files: Mapping[str, str]) -> None:
    """Rewrite Gluetun's secrets folder to hold exactly `files`.

    This is Marrquee's own derived copy of `answers.json`'s VPN answers,
    rebuilt from scratch on every bring-up - never `build_folders`, which
    chowns to the drive's owner. Gluetun reads its login from
    `/run/secrets`, so this folder must stay root-only even though the
    compose file it's mounted into is one the owner can read. Every file is
    replaced atomically (a sibling temp file, `chmod`'d `0600`, then
    `os.replace`); a file left over from an answer that no longer applies
    (a WireGuard key after a switch to OpenVPN, say) is deleted here rather
    than lingering for Gluetun to read by mistake. The folder itself is
    never chowned, and `storage._safe_join` is what turns a symlink planted
    where this folder should be into a loud `PathEscapesRoot` instead of a
    silent write somewhere else on the drive.
    """
    container_root = to_host_view(settings, str(root))
    relative = vpn_secrets_host_path(root).relative_to(root)
    folder = storage._safe_join(container_root, relative)

    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)

    wanted = set(files)
    for existing in folder.iterdir():
        if existing.is_file() and existing.name not in wanted:
            existing.unlink()

    for name, content in files.items():
        _write_atomic_text(folder / name, content)


def clear_vpn_secrets(settings: Settings, root: PurePosixPath) -> None:
    """Remove every file in Gluetun's secrets folder, without ever raising.

    Called when a VPN add is cancelled: Marrquee's own derived copy of a
    credential has no reason to sit on disk for an app that no longer
    exists. A filesystem hiccup here (a missing folder, a permissions
    error, a symlink `_safe_join` refuses) must never turn an otherwise
    successful cancel into a failed one, so every probe below is caught and
    only logged.
    """
    try:
        container_root = to_host_view(settings, str(root))
        relative = vpn_secrets_host_path(root).relative_to(root)
        folder = storage._safe_join(container_root, relative)
        for entry in folder.iterdir():
            if entry.is_file():
                entry.unlink()
    except (OSError, storage.PathEscapesRoot) as error:
        logger.warning("could not clear the VPN secrets folder: %s", error)
