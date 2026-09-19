"""Tests that the shipped artefacts actually ship what the app needs.

Two packaging traps only ever show up in production: templates missing from
a non-editable install, and the install files (compose, README, Dockerfile)
quietly drifting from the port `Settings` actually binds. Both are checkable
without Docker being installed anywhere - the first by building the real
wheel and looking inside it, the second by reading `Settings` and the
install files as plain text/YAML and comparing them.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path

import yaml

from marrquee.config import Settings

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE_PATH = _REPO_ROOT / "Dockerfile"
_DOCKERIGNORE_PATH = _REPO_ROOT / ".dockerignore"
_COMPOSE_PATH = _REPO_ROOT / "compose.install.yaml"
_README_PATH = _REPO_ROOT / "README.md"

_IMAGE_NAME = "ghcr.io/purplefox-07/marrquee:latest"
_SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"
_CONFIG_MOUNT = "marrquee-config:/config"
_HOST_MOUNT = "/:/host"
_EXPECTED_MOUNTS = {_SOCKET_MOUNT, _CONFIG_MOUNT, _HOST_MOUNT}


def _dockerfile_text() -> str:
    return _DOCKERFILE_PATH.read_text()


def _dockerignore_lines() -> list[str]:
    return [
        line.strip()
        for line in _DOCKERIGNORE_PATH.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _compose_doc() -> dict[str, object]:
    doc = yaml.safe_load(_COMPOSE_PATH.read_text())
    assert isinstance(doc, dict)
    return doc


def _compose_service() -> dict[str, object]:
    services = _compose_doc()["services"]
    assert isinstance(services, dict)
    service = services["marrquee"]
    assert isinstance(service, dict)
    return service


def _compose_volumes() -> list[str]:
    volumes = _compose_service()["volumes"]
    assert isinstance(volumes, list)
    return volumes


def _readme_text() -> str:
    return _README_PATH.read_text()


def _docker_run_block(readme: str) -> str:
    r"""The `docker run ...` invocation, joined across its `\`-continued lines.

    The README writes the command as a readable multi-line block for a
    first-time developer to paste as-is; this collapses it back to one
    string so its flags can be asserted on regardless of line breaks.
    """
    match = re.search(r"docker run(?:.*\\\n)*.*", readme)
    assert match is not None, "README has no `docker run` command"
    return match.group(0)


def test_the_built_wheel_contains_templates_css_and_font(tmp_path: Path) -> None:
    """FIRST TEST: this is the trap that only shows up in production.

    A multi-stage build that copies only the built venv into the runtime
    image has no `src/` tree to fall back on - if these files aren't inside
    the wheel itself, the shipped container serves a 500 on its first
    request, and nothing short of building the real wheel proves otherwise.
    """
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
    )
    wheel_path = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel_path).namelist()

    for expected in (
        "marrquee/templates/base.html",
        "marrquee/templates/alive.html",
        "marrquee/static/css/tokens.css",
        "marrquee/static/css/app.css",
        "marrquee/static/fonts/Inter-Variable.woff2",
    ):
        assert expected in names, f"{expected} is missing from the built wheel"


def test_compose_publishes_the_port_settings_actually_uses() -> None:
    port = Settings().port

    assert _compose_service()["ports"] == [f"{port}:{port}"]


def test_compose_mounts_the_docker_socket_the_settings_volume_and_the_host_root() -> None:
    """Exactly three mounts: the socket, the config volume, and the host root.

    The host-root mount lets Marrquee see the owner's drives so it can check
    a typed path and build folders inside it, without the install command
    ever needing to change later.
    """
    assert set(_compose_volumes()) == _EXPECTED_MOUNTS


def test_compose_names_the_published_image_and_a_restart_policy() -> None:
    service = _compose_service()

    assert service["image"] == _IMAGE_NAME
    assert service["restart"] == "unless-stopped"


def test_compose_declares_no_obsolete_version_key() -> None:
    assert "version" not in _compose_doc()


def test_readme_docker_run_line_matches_the_compose_file() -> None:
    """Checked in both directions, so the two install paths cannot drift.

    Every mount the compose file wires in must appear in the README's
    `docker run` line, and the README must not carry a mount the compose
    file doesn't.
    """
    block = _docker_run_block(_readme_text())
    port = Settings().port

    assert _IMAGE_NAME in block
    assert f"-p {port}:{port}" in block

    readme_mounts = {mount for mount in _EXPECTED_MOUNTS if f"-v {mount}" in block}
    assert readme_mounts == _EXPECTED_MOUNTS
    assert set(_compose_volumes()) == _EXPECTED_MOUNTS


def test_dockerfile_exposes_the_port_settings_uses_and_has_no_user_directive() -> None:
    dockerfile = _dockerfile_text()

    assert f"EXPOSE {Settings().port}" in dockerfile
    assert not re.search(r"^USER\s", dockerfile, re.MULTILINE)


def test_dockerignore_excludes_craft_and_git_but_not_source_files() -> None:
    ignored = _dockerignore_lines()

    assert ".git" in ignored
    assert ".craft" in ignored
    assert "tests" in ignored
    assert "tools" in ignored

    for kept in ("src", "pyproject.toml", "uv.lock"):
        assert kept not in ignored, f"{kept} must not be excluded - the build needs it"
