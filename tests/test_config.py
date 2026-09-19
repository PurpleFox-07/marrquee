"""Tests for Settings and ensure_config_dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from marrquee.config import Settings, ensure_config_dir


def test_settings_defaults_are_the_documented_ones() -> None:
    settings = Settings()

    assert settings.config_dir == Path("/config")
    assert settings.docker_socket == Path("/var/run/docker.sock")
    assert settings.host == "0.0.0.0"
    assert settings.port == 7788
    assert settings.host_mount == Path("/host")
    assert settings.compose_binary == Path("/usr/local/bin/docker-compose")
    assert settings.stack_project == "marrquee"


def test_settings_from_env_overrides_each_field() -> None:
    env = {
        "MARRQUEE_CONFIG_DIR": "/data/marrquee-config",
        "MARRQUEE_DOCKER_SOCKET": "/run/docker.sock",
        "MARRQUEE_HOST": "127.0.0.1",
        "MARRQUEE_PORT": "9000",
        "MARRQUEE_HOST_MOUNT": "/mnt/host",
        "MARRQUEE_COMPOSE_BINARY": "/opt/bin/docker-compose",
        "MARRQUEE_STACK_PROJECT": "media-stack",
    }

    settings = Settings.from_env(env)

    assert settings.config_dir == Path("/data/marrquee-config")
    assert settings.docker_socket == Path("/run/docker.sock")
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.host_mount == Path("/mnt/host")
    assert settings.compose_binary == Path("/opt/bin/docker-compose")
    assert settings.stack_project == "media-stack"


def test_settings_from_env_keeps_every_predecessor_default_when_only_new_vars_are_set() -> None:
    settings = Settings.from_env({"MARRQUEE_HOST_MOUNT": "/mnt/host"})

    assert settings.config_dir == Path("/config")
    assert settings.docker_socket == Path("/var/run/docker.sock")
    assert settings.host == "0.0.0.0"
    assert settings.port == 7788
    assert settings.host_mount == Path("/mnt/host")
    assert settings.compose_binary == Path("/usr/local/bin/docker-compose")
    assert settings.stack_project == "marrquee"


def test_settings_from_env_none_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARRQUEE_PORT", "1234")

    settings = Settings.from_env(None)

    assert settings.port == 1234


def test_a_non_numeric_marrquee_port_raises_value_error_naming_the_variable_and_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        Settings.from_env({"MARRQUEE_PORT": "not-a-number"})

    message = str(excinfo.value)
    assert "MARRQUEE_PORT" in message
    assert "not-a-number" in message


@pytest.mark.parametrize("bad_port", ["0", "70000"])
def test_a_marrquee_port_out_of_range_raises_value_error(bad_port: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        Settings.from_env({"MARRQUEE_PORT": bad_port})

    message = str(excinfo.value)
    assert "MARRQUEE_PORT" in message
    assert bad_port in message


def test_ensure_config_dir_creates_a_missing_folder_and_reports_ok(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config"

    status = ensure_config_dir(target)

    assert status.ok is True
    assert status.path == target
    assert status.failure is None
    assert target.is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file mode bits")
def test_ensure_config_dir_reports_not_writable_instead_of_raising(tmp_path: Path) -> None:
    target = tmp_path / "locked"
    target.mkdir()
    target.chmod(0o555)

    try:
        status = ensure_config_dir(target)
    finally:
        # Restore write permission so pytest can clean up tmp_path afterward.
        target.chmod(0o755)

    assert status.ok is False
    assert status.failure == "not_writable"
