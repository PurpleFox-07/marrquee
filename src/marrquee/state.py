"""What Marrquee remembers between restarts: the owner's install choices.

`load_state` is built around one property: whatever shape the settings
folder is in - untouched, mid-write, deleted, from a future build this one
has never heard of - it renders as "nothing chosen yet" rather than a stack
trace on the first page a real user sees.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

STATE_VERSION = 1

_STATE_FILE_NAME = "install.json"


@dataclass(frozen=True)
class InstallState:
    """Everything the owner chose during install, and what Marrquee generated for it."""

    version: int
    storage_root: str | None
    app_ids: tuple[str, ...]
    api_keys: Mapping[str, str]
    puid: int
    pgid: int
    umask: str
    timezone: str
    created: str


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Write JSON that either fully lands on disk or doesn't exist yet.

    This file can hold secrets (API keys), so every write goes to a sibling
    temp file, is `chmod`'d to `0600` before it has any content worth
    reading, then lands with `os.replace` - atomic on the same filesystem,
    so a reader never sees a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2))
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def save_state(config_dir: Path, state: InstallState) -> None:
    """Persist an InstallState to `<config_dir>/install.json`."""
    write_json_atomic(config_dir / _STATE_FILE_NAME, _to_payload(state))


def load_state(config_dir: Path) -> InstallState | None:
    """Read the persisted InstallState, or None for any reason at all.

    A missing file, an empty file, text that isn't JSON, JSON with the wrong
    shape, or a `version` this build doesn't recognise are all the same
    answer: nothing usable is here yet, not an exception.
    """
    try:
        raw = (config_dir / _STATE_FILE_NAME).read_text()
    except OSError:
        return None

    if not raw.strip():
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return None

    try:
        return _from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def new_api_key() -> str:
    """A fresh API key, the same 32-lowercase-hex-character shape the arr apps
    generate for themselves.
    """
    return secrets.token_hex(16)


def _to_payload(state: InstallState) -> dict[str, object]:
    return {
        "version": state.version,
        "storage_root": state.storage_root,
        "app_ids": list(state.app_ids),
        "api_keys": dict(state.api_keys),
        "puid": state.puid,
        "pgid": state.pgid,
        "umask": state.umask,
        "timezone": state.timezone,
        "created": state.created,
    }


def _from_payload(payload: dict[str, object]) -> InstallState:
    return InstallState(
        version=STATE_VERSION,
        storage_root=_require_optional_str(payload.get("storage_root")),
        app_ids=tuple(_require_str_list(payload.get("app_ids"))),
        api_keys=_require_str_dict(payload.get("api_keys")),
        puid=_require_int(payload.get("puid")),
        pgid=_require_int(payload.get("pgid")),
        umask=_require_str(payload.get("umask")),
        timezone=_require_str(payload.get("timezone")),
        created=_require_str(payload.get("created")),
    )


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _require_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _require_str(value)


def _require_int(value: object) -> int:
    # bool is an int subclass in Python; excluded so a stray `true`/`false`
    # in the file can't silently become 1/0 for a PUID/PGID.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected a whole number, got {value!r}")
    return value


def _require_str_list(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"expected a list of strings, got {value!r}")
    return value


def _require_str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item_value, str) for key, item_value in value.items()
    ):
        raise TypeError(f"expected a mapping of strings, got {value!r}")
    return value
