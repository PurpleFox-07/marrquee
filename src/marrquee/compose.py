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
from marrquee.catalog import (
    CatalogApp,
    apps_in_order,
    description_for,
    get_app,
    require_port,
    riders_of,
)
from marrquee.config import Settings
from marrquee.plex import plex_secrets_host_path
from marrquee.state import InstallState
from marrquee.storage import ChownFn, to_host_view
from marrquee.vpn import GluetunConfig, build_gluetun_config
from marrquee.words import (
    COMPOSE_FILE_HEADER_COMMENT,
    DATA_MOUNT_COMMENT,
    DOWNLOADER_COMPOSE_COMMENT,
    DOWNLOADER_NO_VPN_COMPOSE_COMMENT,
    MEDIA_LIBRARY_MOUNT_COMMENT,
    PLEX_COMPOSE_COMMENT,
    PLEX_SECRETS_MOUNT_COMMENT,
    RECYCLARR_COMPOSE_COMMENT,
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
# Plex's own read-only library mount - distinct from `_DATA_MOUNT_SUFFIX`
# (":/data"), which the arr apps' whole shared `/data` mount ends with, so
# neither one is ever mistaken for the other's comment.
_MEDIA_LIBRARY_MOUNT_SUFFIX = ":/data/media:ro"
# Plex's own secrets mount ends in the same ":/run/secrets:ro" Gluetun's
# does - this longer, host-path-anchored tail is what tells the two apart,
# and it must be checked before `_VPN_SECRETS_MOUNT_SUFFIX` below.
_PLEX_SECRETS_MOUNT_TAIL = "/marrquee/plex:/run/secrets:ro"
_COMMENT_WIDTH = 78


@dataclass(frozen=True)
class ServicePlan:
    """Everything the compose file needs to say about one running app.

    `cap_add`/`devices` default to empty - only Gluetun's branch of
    `_service_plan` ever sets them, so every arr service's rendered output
    stays exactly as it was before this story. `network_mode`, when set
    (qBittorrent's branch, riding Gluetun's network namespace instead of
    getting one of its own), makes `_render_service` write a
    `network_mode:` line and skip that service's `networks:` block entirely
    - compose refuses a service that names both. `user`, when set
    (Recyclarr's branch), makes `_render_service` write a `user:` line right
    after `container_name:` - the one app that must run as the drive owner
    instead of the image's own default user, because its image ignores
    PUID/PGID entirely.
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
    network_mode: str | None = None
    user: str | None = None


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
    state: InstallState,
    answers: Mapping[str, Mapping[str, str]] | None = None,
    *,
    without_vpn: bool = False,
) -> StackPlan:
    """Turn a saved `InstallState` into the stack this compose file describes.

    `answers` is the saved per-app question answers (`questions.load_answers`)
    - every caller passes them, even when nothing in `state.app_ids` needs
    one yet, so a later app added to `state` never has to be threaded
    through as a special case. Pure and total given the same three inputs:
    the same `InstallState`, the same answers and the same `without_vpn`
    always produce the same `StackPlan`, which is what keeps a re-deploy
    from producing a spurious diff in the file the owner reads.

    The ONE place "the downloader never runs outside the VPN" is enforced
    for compose: an app with `network_via` set (qBittorrent) but whose
    companion (Gluetun) isn't part of this same install raises `ValueError`
    rather than rendering a service with nothing to share a network with -
    UNLESS `without_vpn` is True and the missing companion is itself the
    VPN (`kind == "vpn"`), the one deliberate break-glass exception. An app
    riding a non-VPN companion that's missing (there is none today) still
    raises regardless of `without_vpn`.
    """
    if state.storage_root is None:
        raise ValueError("cannot build a stack plan before a storage root is chosen")

    root = PurePosixPath(state.storage_root)
    apps = apps_in_order(state.app_ids)
    for app in apps:
        if app.network_via is not None and app.network_via not in state.app_ids:
            if without_vpn and get_app(app.network_via).kind == "vpn":
                continue
            raise ValueError(f"{app.id} needs {app.network_via}")

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
    # Checked before the arr-only api_keys lookup below: neither Gluetun nor
    # qBittorrent has an API key handed to it through the `*__AUTH__*`
    # environment those apps get - faking either would be exactly the "arr
    # fields on a non-arr app" this kind split exists to avoid.
    if app.kind == "vpn":
        return _vpn_service_plan(app, state, root, answers)

    if app.kind == "downloader":
        return _downloader_service_plan(app, state, root)

    if app.kind == "sync":
        return _recyclarr_service_plan(app, state, root)

    if app.kind == "media_server":
        return _plex_service_plan(app, state, root)

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
        ports=((require_port(app), require_port(app)),),
        environment=tuple(environment),
        volumes=tuple(volumes),
        comment=app.description,
    )


def gluetun_config_for(
    app: CatalogApp, state: InstallState, answers: Mapping[str, Mapping[str, str]]
) -> GluetunConfig:
    """Build the one `GluetunConfig` both `_vpn_service_plan` (compose) and
    `DeployManager._write_vpn_secrets` (deploy, right before every Gluetun
    bring-up) build `app`'s Gluetun service from.

    `write_vpn_secrets` rewrites Gluetun's secrets folder to hold EXACTLY
    the files it's handed, deleting anything else on every call - the two
    call sites building this differently (one never knowing qBittorrent
    exists) would silently delete qBittorrent's key and its port-sync
    script the very next time Gluetun reconnects. There is exactly one
    place a `downloader_key` is derived from `state`, and both callers go
    through it.
    """
    try:
        control_key = state.api_keys[app.id]
    except KeyError:
        raise ValueError(f"no control-server key has been generated yet for {app.id!r}") from None

    downloader_key = state.api_keys.get("qbittorrent") if "qbittorrent" in state.app_ids else None

    return build_gluetun_config(
        answers.get(app.id, {}),
        control_key=control_key,
        timezone=state.timezone,
        puid=state.puid,
        pgid=state.pgid,
        downloader_key=downloader_key,
    )


def _vpn_service_plan(
    app: CatalogApp,
    state: InstallState,
    root: PurePosixPath,
    answers: Mapping[str, Mapping[str, str]],
) -> ServicePlan:
    config = gluetun_config_for(app, state, answers)

    config_mount = f"{root / 'marrquee' / 'apps' / app.id}{_VPN_CONFIG_MOUNT_SUFFIX}"
    secrets_mount = f"{vpn_secrets_host_path(root)}{_VPN_SECRETS_MOUNT_SUFFIX}"
    # A rider (qBittorrent) shares this container's network namespace and
    # has no `ports:` block of its own - Gluetun publishes its port instead.
    ports = tuple(
        (require_port(rider), require_port(rider)) for rider in riders_of(app.id, state.app_ids)
    )

    return ServicePlan(
        app_id=app.id,
        service=app.id,
        image=app.image,
        container_name=app.id,
        ports=ports,
        environment=config.environment,
        volumes=(config_mount, secrets_mount),
        comment=app.description,
        cap_add=("NET_ADMIN",),
        devices=("/dev/net/tun:/dev/net/tun",),
    )


def _downloader_service_plan(
    app: CatalogApp, state: InstallState, root: PurePosixPath
) -> ServicePlan:
    """qBittorrent's own branch: it takes no API key through the
    environment either way - its door is the key pre-written into its own
    settings file (`qbittorrent.write_qbit_conf`), never the arr
    `*__AUTH__*` shape.

    Normally it rides its companion's whole network namespace instead of
    joining `marrquee` or publishing a port on its own. The one deliberate
    exception is a confirmed break-glass install: `build_stack_plan`'s own
    refusal is the only gate on this branch existing at all, so by the
    time this runs, an absent `network_via` companion always means the
    owner has actually confirmed running without one.
    """
    via_present = app.network_via is not None and app.network_via in state.app_ids
    port = require_port(app)

    environment: tuple[tuple[str, str], ...] = (
        ("PUID", str(state.puid)),
        ("PGID", str(state.pgid)),
        ("TZ", state.timezone),
        ("UMASK", state.umask),
        ("WEBUI_PORT", str(port)),
    )

    config_mount = f"{root / 'marrquee' / 'apps' / app.id}{_CONFIG_MOUNT_SUFFIX}"
    volumes = [config_mount]
    if app.needs_data_mount:
        volumes.append(f"{root / 'data'}{_DATA_MOUNT_SUFFIX}")

    description = description_for(app, state.app_ids)
    if via_present:
        return ServicePlan(
            app_id=app.id,
            service=app.id,
            image=app.image,
            container_name=app.id,
            ports=(),
            environment=environment,
            volumes=tuple(volumes),
            comment=f"{description} {DOWNLOADER_COMPOSE_COMMENT}",
            network_mode=f"service:{app.network_via}",
        )

    # No VPN companion: an ordinary service on `marrquee`, publishing its
    # own port. The incoming BitTorrent port is deliberately never
    # published here - that would open the NAS to the internet for a mode
    # that is already an emergency.
    return ServicePlan(
        app_id=app.id,
        service=app.id,
        image=app.image,
        container_name=app.id,
        ports=((port, port),),
        environment=environment,
        volumes=tuple(volumes),
        comment=f"{description} {DOWNLOADER_NO_VPN_COMPOSE_COMMENT}",
    )


def _recyclarr_service_plan(
    app: CatalogApp, state: InstallState, root: PurePosixPath
) -> ServicePlan:
    """Recyclarr's own branch: no port, no API key in its environment (its
    keys travel inside `recyclarr.yml`, rewritten fresh before every sync -
    see `recyclarr.write_recyclarr_config`), and `user:` instead of
    PUID/PGID - the image documents that it doesn't support those and runs
    as whatever `user:` compose gives it.

    Every value here is written out explicitly rather than left to the
    image's own defaults, so a later image update can't silently change
    what Marrquee promised the owner it configured.
    """
    config_mount = f"{root / 'marrquee' / 'apps' / app.id}{_CONFIG_MOUNT_SUFFIX}"
    environment: tuple[tuple[str, str], ...] = (
        ("TZ", state.timezone),
        ("CRON_SCHEDULE", "@daily"),
        ("RECYCLARR_CONFIG_DIR", "/config"),
        ("RECYCLARR_DATA_DIR", "/config"),
        ("RECYCLARR_CREATE_CONFIG", "false"),
    )

    return ServicePlan(
        app_id=app.id,
        service=app.id,
        image=app.image,
        container_name=app.id,
        user=f"{state.puid}:{state.pgid}",
        ports=(),
        environment=environment,
        volumes=(config_mount,),
        comment=f"{app.description} {RECYCLARR_COMPOSE_COMMENT}",
    )


def _plex_service_plan(app: CatalogApp, state: InstallState, root: PurePosixPath) -> ServicePlan:
    """Plex's own branch: host networking, no key in its environment, and a
    claim code read through a file rather than written into this file.

    linuxserver's own README recommends `network_mode: host` over publishing
    Plex's port on a bridge network - bridged, Plex would see itself at a
    172.x address and present that to every LAN TV and phone, breaking the
    direct play and local discovery the owner asked for. Host networking
    means no `ports:` and no `networks:` of its own (`ServicePlan.ports=()`
    and `network_mode="host"` are what make `_render_service` skip both).
    `VERSION=docker` and `FILE__PLEX_CLAIM` are linuxserver's own baseimage
    convention for handing a container a secret without ever writing it
    into this file - the claim code is only good for a few minutes, and a
    compose.yaml value would go stale the moment the owner reread it. This
    branch never reads `state.api_keys`: the owner's own Plex account,
    saved separately by the sign-in door, is Plex's only credential.
    """
    config_mount = f"{root / 'marrquee' / 'apps' / app.id}{_CONFIG_MOUNT_SUFFIX}"
    media_mount = f"{root / 'data' / 'media'}{_MEDIA_LIBRARY_MOUNT_SUFFIX}"
    secrets_mount = f"{plex_secrets_host_path(root)}{_VPN_SECRETS_MOUNT_SUFFIX}"

    environment: tuple[tuple[str, str], ...] = (
        ("PUID", str(state.puid)),
        ("PGID", str(state.pgid)),
        ("TZ", state.timezone),
        ("UMASK", state.umask),
        ("VERSION", "docker"),
        ("FILE__PLEX_CLAIM", "/run/secrets/plex_claim"),
    )

    return ServicePlan(
        app_id=app.id,
        service=app.id,
        image=app.image,
        container_name=app.id,
        ports=(),
        environment=environment,
        volumes=(config_mount, media_mount, secrets_mount),
        comment=f"{app.description} {PLEX_COMPOSE_COMMENT}",
        network_mode="host",
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
    if service.user is not None:
        lines.append(f"    user: {_quoted(service.user)}")
    lines.append("    restart: unless-stopped")
    if service.network_mode is not None:
        # Compose refuses a service that names both `network_mode:` and
        # `networks:` - this service shares another container's whole
        # network namespace instead of joining one of its own, so the
        # `networks:` block below is skipped entirely for it.
        lines.append(f"    network_mode: {_quoted(service.network_mode)}")
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
        elif volume.endswith(_MEDIA_LIBRARY_MOUNT_SUFFIX):
            lines.extend(
                f"      {comment}" for comment in _comment_lines(MEDIA_LIBRARY_MOUNT_COMMENT)
            )
        # Checked before `_VPN_SECRETS_MOUNT_SUFFIX` below: both end in the
        # same ":/run/secrets:ro", and this longer, host-path-anchored tail
        # is what tells Plex's own secrets mount apart from Gluetun's.
        elif volume.endswith(_PLEX_SECRETS_MOUNT_TAIL):
            lines.extend(
                f"      {comment}" for comment in _comment_lines(PLEX_SECRETS_MOUNT_COMMENT)
            )
        elif volume.endswith(_VPN_SECRETS_MOUNT_SUFFIX):
            lines.extend(
                f"      {comment}" for comment in _comment_lines(VPN_SECRETS_MOUNT_COMMENT)
            )
        lines.append(f"      - {_quoted(volume)}")
    if service.network_mode is None:
        # A service with no `networks:` of its own would join compose's own
        # implicit "default" network instead of this named one - listing it
        # explicitly here is what lets every app find the others (and
        # Marrquee itself, once it joins this same network) by name.
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
