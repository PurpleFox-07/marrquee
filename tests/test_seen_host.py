"""Tests for the address the owner opens Marrquee at.

`remember_host` is what the Hub and deploy screen call on every GET
(Chunk 5); this module only ever reads and writes `seen_host.json`, so
these tests drive it directly with no route involved.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from marrquee.seen_host import SEEN_HOST_FILE, load_seen_host, remember_host


def test_remember_host_saves_the_bare_host(tmp_path: Path) -> None:
    remember_host(tmp_path, "nas.local:7788")

    assert load_seen_host(tmp_path) == "nas.local"


def test_remember_host_does_nothing_for_none(tmp_path: Path) -> None:
    remember_host(tmp_path, None)

    assert load_seen_host(tmp_path) is None
    assert not (tmp_path / SEEN_HOST_FILE).exists()


def test_remember_host_skips_localhost(tmp_path: Path) -> None:
    remember_host(tmp_path, "localhost:7788")

    assert load_seen_host(tmp_path) is None


def test_remember_host_skips_loopback_ipv4(tmp_path: Path) -> None:
    remember_host(tmp_path, "127.0.0.1:7788")
    remember_host(tmp_path, "127.5.6.7:7788")

    assert load_seen_host(tmp_path) is None


def test_remember_host_skips_bracketed_ipv6_loopback(tmp_path: Path) -> None:
    remember_host(tmp_path, "[::1]:7788")

    assert load_seen_host(tmp_path) is None


def test_remember_host_keeps_a_real_ipv6_host(tmp_path: Path) -> None:
    remember_host(tmp_path, "[2001:db8::1]:7788")

    assert load_seen_host(tmp_path) == "[2001:db8::1]"


def test_remember_host_skips_an_unchanged_host(tmp_path: Path) -> None:
    remember_host(tmp_path, "nas.local:7788")
    written_at = (tmp_path / SEEN_HOST_FILE).stat().st_mtime_ns

    remember_host(tmp_path, "nas.local:9999")

    assert load_seen_host(tmp_path) == "nas.local"
    assert (tmp_path / SEEN_HOST_FILE).stat().st_mtime_ns == written_at


def test_remember_host_updates_a_genuinely_new_host(tmp_path: Path) -> None:
    remember_host(tmp_path, "nas.local:7788")
    remember_host(tmp_path, "192.168.1.50:7788")

    assert load_seen_host(tmp_path) == "192.168.1.50"


def test_remember_host_ignores_garbage_authority(tmp_path: Path) -> None:
    remember_host(tmp_path, "not a host!!")

    assert load_seen_host(tmp_path) is None


def test_the_file_is_root_only(tmp_path: Path) -> None:
    remember_host(tmp_path, "nas.local:7788")

    mode = stat.S_IMODE((tmp_path / SEEN_HOST_FILE).stat().st_mode)
    assert mode == 0o600


def test_the_file_holds_only_a_version_and_a_host(tmp_path: Path) -> None:
    remember_host(tmp_path, "nas.local:7788")

    payload = json.loads((tmp_path / SEEN_HOST_FILE).read_text())
    assert payload == {"version": 1, "host": "nas.local"}


def test_load_seen_host_never_raises_on_junk(tmp_path: Path) -> None:
    (tmp_path / SEEN_HOST_FILE).write_text("not json at all")

    assert load_seen_host(tmp_path) is None


def test_load_seen_host_rejects_the_wrong_version(tmp_path: Path) -> None:
    (tmp_path / SEEN_HOST_FILE).write_text(json.dumps({"version": 2, "host": "nas.local"}))

    assert load_seen_host(tmp_path) is None


def test_load_seen_host_missing_file_is_none(tmp_path: Path) -> None:
    assert load_seen_host(tmp_path / "does-not-exist") is None


@pytest.mark.skipif(os.name == "nt", reason="permission bits are POSIX-only")
def test_remember_host_swallows_an_oserror_on_write(tmp_path: Path) -> None:
    # A read-only directory fails the temp-file write `write_json_atomic`
    # makes before its atomic `os.replace` - never raises out of
    # `remember_host`, and never logs the host itself either.
    config_dir = tmp_path / "ro"
    config_dir.mkdir()
    os.chmod(config_dir, 0o500)
    try:
        remember_host(config_dir, "nas.local:7788")
    finally:
        os.chmod(config_dir, 0o700)

    assert load_seen_host(config_dir) is None
