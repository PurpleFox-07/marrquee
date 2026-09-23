"""Application settings and the config-folder health check.

`Settings` is read once at startup from environment variables - the same
four values the install command sets (`docker run -e ...` or the compose
file). Keeping them in one frozen dataclass means every later story asks
one object for "where do I write things?" and "what port am I on?" instead
of reading `os.environ` in a dozen places.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_MIN_PORT = 1
_MAX_PORT = 65535


@dataclass(frozen=True)
class Settings:
    """The handful of values Marrquee needs before it can do anything else."""

    config_dir: Path = Path("/config")
    docker_socket: Path = Path("/var/run/docker.sock")
    host: str = "0.0.0.0"
    port: int = 7788
    # Where the host filesystem is mounted inside our container. Its env var
    # is MARRQUEE_HOST_MOUNT, not MARRQUEE_HOST_ROOT or anything one
    # character away from MARRQUEE_HOST (the bind address above) - the two
    # are unrelated and a near-miss name would be a support ticket waiting
    # to happen.
    host_mount: Path = Path("/host")
    compose_binary: Path = Path("/usr/local/bin/docker-compose")
    stack_project: str = "marrquee-apps"  # never "marrquee" - see compose._STACK_PROJECT

    @staticmethod
    def from_env(env: Mapping[str, str] | None = None) -> Settings:
        """Build Settings from environment variables, falling back to defaults.

        `env=None` reads the real process environment; tests pass an explicit
        mapping so they never depend on (or leak into) `os.environ`.
        """
        if env is None:
            env = os.environ

        defaults = Settings()
        raw_port = env.get("MARRQUEE_PORT")
        return Settings(
            config_dir=Path(env["MARRQUEE_CONFIG_DIR"])
            if "MARRQUEE_CONFIG_DIR" in env
            else defaults.config_dir,
            docker_socket=Path(env["MARRQUEE_DOCKER_SOCKET"])
            if "MARRQUEE_DOCKER_SOCKET" in env
            else defaults.docker_socket,
            host=env.get("MARRQUEE_HOST", defaults.host),
            port=_parse_port(raw_port) if raw_port is not None else defaults.port,
            host_mount=Path(env["MARRQUEE_HOST_MOUNT"])
            if "MARRQUEE_HOST_MOUNT" in env
            else defaults.host_mount,
            compose_binary=Path(env["MARRQUEE_COMPOSE_BINARY"])
            if "MARRQUEE_COMPOSE_BINARY" in env
            else defaults.compose_binary,
            stack_project=env.get("MARRQUEE_STACK_PROJECT", defaults.stack_project),
        )


def _parse_port(raw_value: str) -> int:
    """Parse MARRQUEE_PORT, raising a plain-language ValueError on anything bad.

    An unbindable port is a typo the owner made in their install command;
    failing loudly at startup with a readable sentence beats silently binding
    something surprising.
    """
    message = (
        f"MARRQUEE_PORT must be a whole number between {_MIN_PORT} and {_MAX_PORT}, "
        f"got {raw_value!r}."
    )
    try:
        port = int(raw_value)
    except ValueError:
        raise ValueError(message) from None
    if not (_MIN_PORT <= port <= _MAX_PORT):
        raise ValueError(message)
    return port


@dataclass(frozen=True)
class ConfigDirStatus:
    """The result of checking whether Marrquee can save settings to disk."""

    ok: bool
    path: Path
    failure: Literal["cannot_create", "not_writable"] | None
    detail: str | None


_PROBE_FILE_NAME = ".marrquee-write-check"


def ensure_config_dir(path: Path) -> ConfigDirStatus:
    """Create the config folder if needed and prove it is writable.

    This never raises: every failure comes back as a `ConfigDirStatus` so a
    problem here becomes a plain-language line on the alive page instead of a
    crash-looping container that shows the owner nothing.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return ConfigDirStatus(ok=False, path=path, failure="cannot_create", detail=str(error))

    probe_path = path / _PROBE_FILE_NAME
    try:
        probe_path.write_text("")
    except OSError as error:
        return ConfigDirStatus(ok=False, path=path, failure="not_writable", detail=str(error))
    finally:
        probe_path.unlink(missing_ok=True)

    return ConfigDirStatus(ok=True, path=path, failure=None, detail=None)
