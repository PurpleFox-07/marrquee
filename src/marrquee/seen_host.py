"""The address the owner opens Marrquee at, remembered so Seerr's own
"Play on Jellyfin" and "Open in Sonarr/Radarr" links can use it instead of
an internal address (`http://sonarr:8989`) no phone or browser off the NAS
can ever open.

Chunk 5 calls `remember_host` from the Hub's and the deploy screen's own GET
handlers - this module only ever reads and writes the one small file that
records the result.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from pathlib import Path
from typing import Final

from marrquee.addresses import host_only
from marrquee.state import write_json_atomic

logger = logging.getLogger(__name__)

SEEN_HOST_FILE: Final = "seen_host.json"
_SEEN_HOST_VERSION: Final = 1

# `host_only` always lowercases a hostname and keeps IPv6 bracketed, so these
# are the only two loopback spellings ever worth comparing against literally.
_LOOPBACK_LITERALS: Final = frozenset({"localhost", "[::1]"})


def remember_host(config_dir: Path, authority: str | None) -> None:
    """Save the bare host the owner's browser just used to reach Marrquee.

    Never raises. Does nothing for `None`, a loopback address (useless to
    every OTHER device on the network), or a host that's already the one
    saved - so an ordinary page view writes nothing at all.
    """
    if authority is None:
        return
    host = host_only(authority)
    if host is None or _is_loopback(host):
        return
    if load_seen_host(config_dir) == host:
        return
    try:
        write_json_atomic(
            config_dir / SEEN_HOST_FILE, {"version": _SEEN_HOST_VERSION, "host": host}
        )
    except OSError as error:
        # Never log `host` itself - the owner's address is not something
        # worth putting in a log line, even one only Marrquee itself reads.
        logger.warning("could not save the owner's address: %s", type(error).__name__)


def load_seen_host(config_dir: Path) -> str | None:
    """Read back what `remember_host` last saved, or `None` for any reason
    at all - a missing file, a stale version, or anything unreadable.
    """
    try:
        raw = (config_dir / SEEN_HOST_FILE).read_text()
    except OSError:
        return None

    if not raw.strip():
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or not _is_current_version(payload.get("version")):
        return None

    host = payload.get("host")
    return host if isinstance(host, str) and host else None


def _is_current_version(value: object) -> bool:
    # bool is an int subclass in Python; excluded so a stray `true` in the
    # file can't silently be read as version 1.
    return isinstance(value, int) and not isinstance(value, bool) and value == _SEEN_HOST_VERSION


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_LITERALS:
        return True
    try:
        return ipaddress.IPv4Address(host).is_loopback
    except ValueError:
        return False
