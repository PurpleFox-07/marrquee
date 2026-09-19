"""The install-save: turns a typed path and a set of chosen apps into a
saved `InstallState`.

This is a plain, importable function rather than a route handler. Story 4
adds a no-JavaScript install page for owners whose NAS Docker app has
trouble with the wizard's own fetch calls, and that page needs to call
exactly this same validate-then-save logic - never a second copy of it,
and never one that regenerates an API key an app is already using.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from marrquee.catalog import CATALOG, apps_in_order
from marrquee.config import Settings
from marrquee.state import InstallState, load_state, new_api_key, save_state
from marrquee.storage import check_fresh_start, check_storage_root, derive_ids
from marrquee.words import (
    REFUSAL_NOTHING_CHOSEN,
    REFUSAL_UNKNOWN_APP,
    refusal_populated_target,
    storage_check_message,
)

_KNOWN_APP_IDS = frozenset(app.id for app in CATALOG)

InstallResultKind = Literal["ok", "invalid_input", "refused"]


@dataclass(frozen=True)
class InstallResult:
    """The outcome of trying to save the owner's install choices.

    `ok=False` always carries a plain-language `message` and never touches
    disk - a rejected install leaves nothing half-saved behind. `kind` lets
    a caller (a JSON route, a plain HTML form handler) pick the right
    response without re-deriving why the save was refused: `invalid_input`
    for a request that doesn't even name real apps, `refused` for a real
    path that Marrquee genuinely can't use right now.
    """

    ok: bool
    kind: InstallResultKind
    message: str
    state: InstallState | None


def install_apps(settings: Settings, path: str, app_ids: list[str]) -> InstallResult:
    """Validate, then save, the owner's chosen apps and storage root.

    Re-saving keeps whatever API key an app already has instead of
    generating a new one - regenerating would silently invalidate anything
    already wired to the old key, in a way that looks like a Story 3 bug
    rather than a Story 2 one.
    """
    if not app_ids:
        return InstallResult(
            ok=False, kind="invalid_input", message=REFUSAL_NOTHING_CHOSEN, state=None
        )

    unknown = [app_id for app_id in app_ids if app_id not in _KNOWN_APP_IDS]
    if unknown:
        return InstallResult(
            ok=False, kind="invalid_input", message=REFUSAL_UNKNOWN_APP, state=None
        )

    storage_check = check_storage_root(settings, path)
    if not storage_check.ok:
        message = storage_check_message(storage_check.reason, path)
        return InstallResult(ok=False, kind="refused", message=message, state=None)

    if storage_check.host_path is None:
        # `ok=True` always carries a host_path - this branch exists only so
        # the type checker knows `root` below is never None, not because
        # this can actually happen.
        raise RuntimeError("check_storage_root reported ok=True without a host_path")
    root = storage_check.host_path

    ordered_ids = tuple(app.id for app in apps_in_order(app_ids))

    freshness = check_fresh_start(settings, root, ordered_ids)
    if not freshness.ok:
        return InstallResult(
            ok=False, kind="refused", message=refusal_populated_target(path), state=None
        )

    derived = derive_ids(settings, root)
    existing = load_state(settings.config_dir)
    existing_keys = dict(existing.api_keys) if existing is not None else {}
    api_keys = {app_id: existing_keys.get(app_id, new_api_key()) for app_id in ordered_ids}
    created = existing.created if existing is not None else datetime.now(UTC).isoformat()

    state = InstallState(
        version=1,
        storage_root=str(root),
        app_ids=ordered_ids,
        api_keys=api_keys,
        puid=derived.puid,
        pgid=derived.pgid,
        umask=derived.umask,
        timezone=derived.timezone,
        created=created,
    )
    save_state(settings.config_dir, state)
    return InstallResult(ok=True, kind="ok", message="", state=state)
