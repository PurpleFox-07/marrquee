"""Tests for qBittorrent's own facts: the exact pre-written settings file, the
API-key format it insists on, the port-sync script Gluetun runs on every
reconnect, and its base preference keys.

`write_qbit_conf` is the one function here that touches a filesystem - the
same "never touch what we didn't create, resolve before trusting a path"
rules `test_storage.py` proves for `build_folders`/`write_marker` apply here
too, since this module reuses `storage._safe_join` directly rather than
re-inventing its own escape check.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

import pytest

from marrquee import qbittorrent
from marrquee.config import Settings
from marrquee.storage import PathEscapesRoot

_VALID_KEY = "qbt_" + "Ab3dEf7hJk9mNp2qRs5tUv8wXy1zAb"[:28]

_EXPECTED_CONF = (
    "[LegalNotice]\n"
    "Accepted=true\n"
    "\n"
    "[Preferences]\n"
    "Connection\\PortRangeMin=6881\n"
    "Connection\\UPnP=false\n"
    "Downloads\\SavePath=/data/torrents/\n"
    f"WebUI\\APIKey={_VALID_KEY}\n"
    "WebUI\\Address=*\n"
    "WebUI\\ServerDomains=*\n"
)


# --- render_qbit_conf: the FIRST TEST ----------------------------------------


def test_render_qbit_conf_is_exact() -> None:
    assert qbittorrent.render_qbit_conf(_VALID_KEY) == _EXPECTED_CONF


def test_render_qbit_conf_refuses_a_hex_key() -> None:
    hex_key = "a" * 32  # the arr apps' own key shape, not qBittorrent's

    with pytest.raises(ValueError):
        qbittorrent.render_qbit_conf(hex_key)


def test_render_qbit_conf_refuses_a_key_of_the_wrong_length() -> None:
    with pytest.raises(ValueError):
        qbittorrent.render_qbit_conf("qbt_tooshort")


def test_render_qbit_conf_never_names_the_rejected_key_in_the_error() -> None:
    secret_looking_key = "not-a-real-key-but-should-never-be-logged"

    with pytest.raises(ValueError) as excinfo:
        qbittorrent.render_qbit_conf(secret_looking_key)

    assert secret_looking_key not in str(excinfo.value)


# --- qbit_conf_host_path -------------------------------------------------


def test_qbit_conf_host_path_is_under_marrquee_apps_qbittorrent() -> None:
    root = PurePosixPath("/volume1/media")

    path = qbittorrent.qbit_conf_host_path(root)

    assert path == root / "marrquee" / "apps" / "qbittorrent" / "qBittorrent" / "qBittorrent.conf"


# --- write_qbit_conf: writes once, never overwrites, chowns only what it made -


def test_write_qbit_conf_writes_the_conf_and_returns_true(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media" / "marrquee" / "apps" / "qbittorrent").mkdir(parents=True)

    wrote = qbittorrent.write_qbit_conf(
        settings, root, _VALID_KEY, 1000, 1000, chown=lambda *_: None
    )

    relative_conf_path = qbittorrent.qbit_conf_host_path(PurePosixPath())
    conf_path = tmp_path / "volume1" / "media" / relative_conf_path
    assert wrote is True
    assert conf_path.read_text() == _EXPECTED_CONF


def test_write_qbit_conf_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    conf_dir = tmp_path / "volume1" / "media" / "marrquee" / "apps" / "qbittorrent" / "qBittorrent"
    conf_dir.mkdir(parents=True)
    conf_path = conf_dir / "qBittorrent.conf"
    conf_path.write_text("owner already changed this")

    wrote = qbittorrent.write_qbit_conf(
        settings, root, _VALID_KEY, 1000, 1000, chown=lambda *_: None
    )

    assert wrote is False
    assert conf_path.read_text() == "owner already changed this"


def test_write_qbit_conf_creates_the_qbittorrent_folder_and_chowns_only_that_and_the_file(
    tmp_path: Path,
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    apps_dir = tmp_path / "volume1" / "media" / "marrquee" / "apps" / "qbittorrent"
    apps_dir.mkdir(parents=True)  # build_folders' own precedent: this exists already

    recorded: list[tuple[Path, int, int]] = []
    qbittorrent.write_qbit_conf(
        settings,
        root,
        _VALID_KEY,
        4242,
        4343,
        chown=lambda path, uid, gid: recorded.append((path, uid, gid)),
    )

    chowned_paths = {path for path, _, _ in recorded}
    assert apps_dir not in chowned_paths  # pre-existing - never touched
    assert apps_dir / "qBittorrent" in chowned_paths
    assert apps_dir / "qBittorrent" / "qBittorrent.conf" in chowned_paths
    assert all(uid == 4242 and gid == 4343 for _, uid, gid in recorded)


def test_write_qbit_conf_swallows_a_chown_failure_instead_of_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    (tmp_path / "volume1" / "media" / "marrquee" / "apps" / "qbittorrent").mkdir(parents=True)

    def _raising_chown(path: Path, uid: int, gid: int) -> None:
        raise PermissionError("this NAS share does not support chown")

    with caplog.at_level(logging.WARNING):
        wrote = qbittorrent.write_qbit_conf(
            settings, root, _VALID_KEY, 1000, 1000, chown=_raising_chown
        )

    assert wrote is True
    assert "chown" in caplog.text.lower()


def test_write_qbit_conf_refuses_a_symlinked_folder(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    apps_dir = tmp_path / "volume1" / "media" / "marrquee" / "apps"
    apps_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (apps_dir / "qbittorrent").symlink_to(outside)

    with pytest.raises(PathEscapesRoot):
        qbittorrent.write_qbit_conf(settings, root, _VALID_KEY, 1000, 1000, chown=lambda *_: None)


def test_write_qbit_conf_propagates_an_oserror_from_the_write_itself(tmp_path: Path) -> None:
    settings = Settings(host_mount=tmp_path)
    root = PurePosixPath("/volume1/media")
    # The config folder for qbittorrent is a FILE, not a folder - mkdir(parents=True)
    # for the `qBittorrent` subfolder must raise, and that OSError must not be
    # swallowed the way a chown failure is.
    apps_root = tmp_path / "volume1" / "media" / "marrquee" / "apps"
    apps_root.mkdir(parents=True)
    (apps_root / "qbittorrent").write_text("not a folder")

    with pytest.raises(OSError):
        qbittorrent.write_qbit_conf(settings, root, _VALID_KEY, 1000, 1000, chown=lambda *_: None)


# --- QBIT_BASE_PREFERENCES ----------------------------------------------------


def test_qbit_base_preferences_sets_the_shared_save_path_and_automatic_management() -> None:
    assert qbittorrent.QBIT_BASE_PREFERENCES == {
        "save_path": "/data/torrents",
        "temp_path_enabled": False,
        "auto_tmm_enabled": True,
        "upnp": False,
        "max_ratio_act": 0,
    }


# --- port_sync_script: golden text, and it must actually be valid sh --------


def test_port_sync_script_is_exact() -> None:
    script = qbittorrent.port_sync_script(8080)

    assert script == (
        "# Written by Marrquee. Your VPN runs this when it forwards a port, "
        "so qBittorrent listens on it.\n"
        "key=$(cat /run/secrets/qbittorrent_api_key)\n"
        "exec wget -q -O /dev/null -T 5 --tries=3 --retry-connrefused "
        '--header="Authorization: Bearer $key" '
        '--post-data="json={\\"listen_port\\":$1}" '
        "http://127.0.0.1:8080/api/v2/app/setPreferences\n"
    )


def test_port_sync_script_uses_the_given_port() -> None:
    script = qbittorrent.port_sync_script(51413)

    assert "http://127.0.0.1:51413/api/v2/app/setPreferences" in script


def test_port_sync_script_passes_sh_n() -> None:
    script = qbittorrent.port_sync_script(8080)

    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        script_path = handle.name

    try:
        result = subprocess.run(["sh", "-n", script_path], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        Path(script_path).unlink()


def test_qbit_key_secret_name_and_script_name_constants() -> None:
    assert qbittorrent.QBIT_KEY_SECRET_NAME == "qbittorrent_api_key"
    assert qbittorrent.PORT_SYNC_SCRIPT_NAME == "marrquee-port-sync.sh"
