"""The two connection types this story wires: a Prowlarr application entry,
and a library (root) folder inside Sonarr or Radarr.

Both functions here know nothing about ordering, emitting progress or
retrying - that is the engine's job (Chunk 3). Each one only knows how to
look before it writes, so calling either any number of times in a row is
safe: a second call finds what the first one left behind and changes
nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from marrquee.catalog import CatalogApp, app_host, require_port
from marrquee.wiring.arr_client import ArrClient, ArrResponse
from marrquee.wiring.qbit_client import QbitClient, QbitResponse, preferences_form
from marrquee.words import (
    WIRING_NOTE_ALREADY_CONNECTED,
    wiring_failure_folder,
    wiring_failure_prowlarr_too_old,
    wiring_failure_refused,
    wiring_failure_unreachable,
)

_ROOT_FOLDER_PATH = "rootfolder"
_APPLICATIONS_PATH = "applications"
_APPLICATIONS_SCHEMA_PATH = "applications/schema"

_QBIT_PREFERENCES_PATH = "app/preferences"
_QBIT_SET_PREFERENCES_PATH = "app/setPreferences"
_QBIT_CATEGORIES_PATH = "torrents/categories"
_QBIT_CREATE_CATEGORY_PATH = "torrents/createCategory"
_QBIT_EDIT_CATEGORY_PATH = "torrents/editCategory"

_DOWNLOAD_CLIENT_PATH = "downloadclient"
_DOWNLOAD_CLIENT_SCHEMA_PATH = "downloadclient/schema"

# Sonarr's and Radarr's own name for qBittorrent's download-client
# implementation - the same string on both, since both share the same
# `QBittorrentSettings` class.
_QBIT_IMPLEMENTATION_NAME = "QBittorrent"

# The float tolerance `ensure_qbit_preferences` compares within - qBittorrent
# round-trips a ratio as a 64-bit float, so an exact `==` would flag its own
# unchanged answer as a difference and POST forever.
_PREFERENCE_FLOAT_TOLERANCE = 0.001

# Prowlarr's own field names for the two properties that mean "the target
# app couldn't be reached" - anything else Prowlarr's live test rejects is a
# "the target app said no" problem instead, and the owner needs a different
# next step for each.
_UNREACHABLE_PROPERTY_NAMES = frozenset({"baseurl", "prowlarrurl"})


@dataclass(frozen=True)
class StepOutcome:
    """What one attempt at a connection came back with.

    `transient` is true only for a status of 0 (no reply at all) or a 5xx -
    the two cases worth retrying. A 400 is a considered answer, not a
    hiccup, so it is never transient.
    """

    state: Literal["done", "skipped", "error"]
    note: str | None
    technical: str | None
    changed: bool
    transient: bool


def app_base_url(app: CatalogApp, present: Iterable[str]) -> str:
    """Where Marrquee, and every other app on the same network, reaches `app`.

    Every app's compose service name and container name are both its
    catalog id, so an ordinary app is reachable at its own id - the same
    address the deploy engine's own readiness probe already reaches it at.
    An app that rides another one's network instead (qBittorrent, sharing
    Gluetun's `network_mode: service:gluetun`, only while Gluetun is part
    of `present`) has no DNS name of its own; `app_host` is what routes it
    to the app whose network it actually joined.
    """
    return f"http://{app_host(app, present)}:{require_port(app)}"


def _transient(status: int) -> bool:
    return status == 0 or 500 <= status < 600


def _failure_technical(response: ArrResponse) -> str:
    """The raw detail behind a failed call - for diagnostics only, never a note.

    A validation failure names each rejected field; anything else (a dead
    app, a 5xx) has no field to name, so the response's own free-text detail
    is all there is.
    """
    if response.failures:
        return "; ".join(
            f"{failure.property_name}: {failure.error_message}" for failure in response.failures
        )
    return response.detail or f"HTTP {response.status}"


def _unreachable_outcome(app_name: str, response: ArrResponse) -> StepOutcome:
    return StepOutcome(
        state="error",
        note=wiring_failure_unreachable(app_name),
        technical=_failure_technical(response),
        changed=False,
        transient=_transient(response.status),
    )


def _field(resource: Mapping[str, object], name: str) -> object | None:
    """The value of one `fields[]` entry by name, never by index."""
    fields = resource.get("fields")
    if not isinstance(fields, list):
        return None
    for entry in fields:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry.get("value")
    return None


def _with_field(resource: Mapping[str, object], name: str, value: object) -> dict[str, object]:
    """A copy of `resource` with one `fields[]` entry's value set (or added).

    Never mutates `resource` or its `fields` list - the schema list this
    reads from is reused for every app wired in a run, so a shared entry
    must come out of this function unchanged.
    """
    fields = resource.get("fields")
    new_fields: list[object] = []
    found = False
    if isinstance(fields, list):
        for entry in fields:
            if isinstance(entry, dict) and entry.get("name") == name:
                new_fields.append({**entry, "value": value})
                found = True
            else:
                new_fields.append(entry)
    if not found:
        new_fields.append({"name": name, "value": value})
    return {**resource, "fields": new_fields}


# --- Root folder: one library folder inside Sonarr or Radarr -----------------


async def ensure_root_folder(
    client: ArrClient,
    app: CatalogApp,
    api_key: str,
    *,
    container_path: PurePosixPath,
    host_path: PurePosixPath,
) -> StepOutcome:
    """Make sure `app` has `container_path` as a root folder, writing only if needed."""
    assert app.network_via is None  # arr apps always answer on their own network
    base_url = app_base_url(app, ())
    path = f"{app.api_base}/{_ROOT_FOLDER_PATH}"

    listing = await client.request("GET", base_url, path, api_key)
    if not listing.ok:
        return _unreachable_outcome(app.name, listing)

    existing = listing.payload if isinstance(listing.payload, list) else []
    for entry in existing:
        if not isinstance(entry, dict):
            continue
        entry_path = entry.get("path")
        if isinstance(entry_path, str) and PurePosixPath(entry_path) == container_path:
            return StepOutcome(
                state="done",
                note=WIRING_NOTE_ALREADY_CONNECTED,
                technical=None,
                changed=False,
                transient=False,
            )

    created = await client.request(
        "POST", base_url, path, api_key, json_body={"path": str(container_path)}
    )
    if created.ok:
        return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)

    if _is_already_configured(created):
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )

    if _transient(created.status):
        return _unreachable_outcome(app.name, created)

    return StepOutcome(
        state="error",
        note=wiring_failure_folder(app.name, str(host_path)),
        technical=_failure_technical(created),
        changed=False,
        transient=False,
    )


def _is_already_configured(response: ArrResponse) -> bool:
    return any(
        failure.property_name == "Path" and "already configured" in failure.error_message.lower()
        for failure in response.failures
    )


# --- Application: Prowlarr's connection to Sonarr or Radarr ------------------


async def ensure_application(
    client: ArrClient,
    prowlarr: CatalogApp,
    prowlarr_key: str,
    target: CatalogApp,
    target_key: str,
) -> StepOutcome:
    """Make sure Prowlarr has a full-sync application entry pointing at `target`."""
    assert prowlarr.network_via is None  # arr apps always answer on their own network
    assert target.network_via is None
    base_url = app_base_url(prowlarr, ())
    applications_path = f"{prowlarr.api_base}/{_APPLICATIONS_PATH}"

    listing = await client.request("GET", base_url, applications_path, prowlarr_key)
    if not listing.ok:
        return _unreachable_outcome(target.name, listing)

    entries = listing.payload if isinstance(listing.payload, list) else []
    existing = _find_application(entries, target)

    desired_prowlarr_url = app_base_url(prowlarr, ())
    desired_base_url = app_base_url(target, ())

    if existing is not None:
        if (
            _field(existing, "prowlarrUrl") == desired_prowlarr_url
            and existing.get("syncLevel") == "fullSync"
        ):
            return StepOutcome(
                state="done",
                note=WIRING_NOTE_ALREADY_CONNECTED,
                technical=None,
                changed=False,
                transient=False,
            )

        updated = dict(existing)
        updated["syncLevel"] = "fullSync"
        updated = _with_field(updated, "prowlarrUrl", desired_prowlarr_url)
        updated = _with_field(updated, "baseUrl", desired_base_url)
        updated = _with_field(updated, "apiKey", target_key)

        put_path = f"{applications_path}/{existing.get('id')}"
        result = await client.request("PUT", base_url, put_path, prowlarr_key, json_body=updated)
        if result.ok:
            return StepOutcome(
                state="done", note=None, technical=None, changed=True, transient=False
            )
        return _application_write_failure(target.name, result)

    schema_path = f"{prowlarr.api_base}/{_APPLICATIONS_SCHEMA_PATH}"
    schema_response = await client.request("GET", base_url, schema_path, prowlarr_key)
    if not schema_response.ok:
        return _unreachable_outcome(target.name, schema_response)

    schema_entries = schema_response.payload if isinstance(schema_response.payload, list) else []
    template = _find_schema(schema_entries, target)
    if template is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_prowlarr_too_old(target.name),
            technical=f"no schema entry for implementation {target.name!r}",
            changed=False,
            transient=False,
        )

    new_entry: dict[str, object] = {key: value for key, value in template.items() if key != "id"}
    new_entry["name"] = target.name
    new_entry["syncLevel"] = "fullSync"
    new_entry = _with_field(new_entry, "prowlarrUrl", desired_prowlarr_url)
    new_entry = _with_field(new_entry, "baseUrl", desired_base_url)
    new_entry = _with_field(new_entry, "apiKey", target_key)

    result = await client.request(
        "POST", base_url, applications_path, prowlarr_key, json_body=new_entry
    )
    if result.ok:
        return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)
    return _application_write_failure(target.name, result)


def _find_application(entries: Iterable[object], target: CatalogApp) -> dict[str, object] | None:
    """The existing application entry for `target`, or None.

    Matches by implementation and address first - the pairing that actually
    proves this entry talks to `target`. Falls back to a name match so a
    hand-edited or oddly-configured entry is still found and repaired
    instead of duplicated.
    """
    assert target.network_via is None  # arr apps always answer on their own network
    target_base_url = app_base_url(target, ())
    fallback: dict[str, object] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("implementation") == target.name and _field(entry, "baseUrl") == (
            target_base_url
        ):
            return entry
        if fallback is None and entry.get("name") == target.name:
            fallback = entry
    return fallback


def _find_schema(entries: Iterable[object], target: CatalogApp) -> dict[str, object] | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("implementation") == target.name:
            return entry
    return None


def _application_write_failure(target_name: str, response: ArrResponse) -> StepOutcome:
    if _transient(response.status):
        return _unreachable_outcome(target_name, response)

    if any(
        failure.property_name.lower() in _UNREACHABLE_PROPERTY_NAMES
        for failure in response.failures
    ):
        note = wiring_failure_unreachable(target_name)
    else:
        note = wiring_failure_refused(target_name)

    return StepOutcome(
        state="error",
        note=note,
        technical=_failure_technical(response),
        changed=False,
        transient=False,
    )


# --- qBittorrent's own settings: seeding limits and per-app categories --------


def _qbit_failure_technical(response: QbitResponse) -> str:
    return response.detail or f"HTTP {response.status}"


def _qbit_unreachable_outcome(app_name: str, response: QbitResponse) -> StepOutcome:
    return StepOutcome(
        state="error",
        note=wiring_failure_unreachable(app_name),
        technical=_qbit_failure_technical(response),
        changed=False,
        transient=_transient(response.status),
    )


def _qbit_write_failure(app_name: str, response: QbitResponse) -> StepOutcome:
    if _transient(response.status):
        return _qbit_unreachable_outcome(app_name, response)
    return StepOutcome(
        state="error",
        note=wiring_failure_refused(app_name),
        technical=_qbit_failure_technical(response),
        changed=False,
        transient=False,
    )


def _preferences_match(current: Mapping[str, object], desired: Mapping[str, object]) -> bool:
    """Whether every key `desired` cares about already reads the same in
    `current` - a float is compared with tolerance, since qBittorrent hands
    a ratio back as a 64-bit float that never quite equals the value Marrquee
    sent.
    """
    for key, value in desired.items():
        current_value = current.get(key)
        if isinstance(value, float) or isinstance(current_value, float):
            try:
                if abs(float(current_value) - float(value)) > _PREFERENCE_FLOAT_TOLERANCE:  # type: ignore[arg-type]
                    return False
            except (TypeError, ValueError):
                return False
        elif current_value != value:
            return False
    return True


async def ensure_qbit_preferences(
    client: QbitClient,
    app: CatalogApp,
    api_key: str,
    prefs: Mapping[str, object],
    *,
    present: Iterable[str],
) -> StepOutcome:
    """Make sure qBittorrent's global preferences hold `prefs`, writing only
    on a genuine difference.

    Marrquee re-asserts its own seeding choice (and its listen port) on
    every wiring run, so an owner's hand-edit outside `prefs`'s own keys is
    left alone - only the keys Marrquee actually cares about are compared.
    `present` is the install's own app ids, so this reaches qBittorrent at
    whichever host it actually answers on.
    """
    base_url = app_base_url(app, present)
    prefs_path = f"{app.api_base}/{_QBIT_PREFERENCES_PATH}"
    set_prefs_path = f"{app.api_base}/{_QBIT_SET_PREFERENCES_PATH}"

    listing = await client.request("GET", base_url, prefs_path, api_key)
    if not listing.ok:
        return _qbit_unreachable_outcome(app.name, listing)

    current = listing.payload if isinstance(listing.payload, dict) else {}
    if _preferences_match(current, prefs):
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )

    written = await client.request(
        "POST", base_url, set_prefs_path, api_key, form=preferences_form(prefs)
    )
    if written.ok:
        return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)
    return _qbit_write_failure(app.name, written)


def _category_form(media_folder: str, save_path: str) -> dict[str, str]:
    return {"category": media_folder, "savePath": save_path}


async def ensure_qbit_category(
    client: QbitClient,
    app: CatalogApp,
    api_key: str,
    *,
    media_folder: str,
    save_path: str,
    present: Iterable[str],
) -> StepOutcome:
    """Make sure qBittorrent has a category named `media_folder` saving to
    `save_path`, creating or repairing it as needed.

    `media_folder` and `save_path` are handed in by the caller rather than
    known here - this module never hardcodes which media folders exist,
    that is the catalog's and the wiring plan's job. `present` is the
    install's own app ids, so this reaches qBittorrent at whichever host it
    actually answers on.
    """
    base_url = app_base_url(app, present)
    categories_path = f"{app.api_base}/{_QBIT_CATEGORIES_PATH}"

    listing = await client.request("GET", base_url, categories_path, api_key)
    if not listing.ok:
        return _qbit_unreachable_outcome(app.name, listing)

    categories = listing.payload if isinstance(listing.payload, dict) else {}
    existing = categories.get(media_folder)
    if isinstance(existing, dict) and existing.get("savePath") == save_path:
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )

    write_path_suffix = _QBIT_CREATE_CATEGORY_PATH if existing is None else _QBIT_EDIT_CATEGORY_PATH
    write_path = f"{app.api_base}/{write_path_suffix}"
    written = await client.request(
        "POST", base_url, write_path, api_key, form=_category_form(media_folder, save_path)
    )
    if written.ok:
        return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)
    return _qbit_write_failure(app.name, written)


# --- Download client: Sonarr's or Radarr's connection to qBittorrent ---------


def _find_download_client(
    entries: Iterable[object], downloader: CatalogApp, *, present: Iterable[str]
) -> dict[str, object] | None:
    """The existing download-client entry pointing at `downloader`, or None.

    Matches by implementation and host first - the pairing that actually
    proves this entry talks to `downloader`. Falls back to a name match so a
    hand-edited entry is still found and repaired instead of duplicated -
    including one still pointing at its OLD host (a mover whose VPN just
    changed, or an add whose host just became `qbittorrent` instead of
    `gluetun`), which counts as a difference and is PUT to the new one.
    """
    target_host = app_host(downloader, present)
    fallback: dict[str, object] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("implementation") == _QBIT_IMPLEMENTATION_NAME and _field(entry, "host") == (
            target_host
        ):
            return entry
        if fallback is None and entry.get("name") == downloader.name:
            fallback = entry
    return fallback


def _find_download_client_schema(entries: Iterable[object]) -> dict[str, object] | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("implementation") == _QBIT_IMPLEMENTATION_NAME:
            return entry
    return None


def _download_client_matches(
    existing: Mapping[str, object],
    *,
    host: str,
    port: int,
    category_field: str,
    category: str,
) -> bool:
    return (
        _field(existing, "host") == host
        and _field(existing, "port") == port
        and _field(existing, category_field) == category
        and existing.get("enable") is True
        and existing.get("removeCompletedDownloads") is True
    )


def _with_download_client_fields(
    resource: Mapping[str, object],
    downloader: CatalogApp,
    qbit_key: str,
    *,
    category_field: str,
    category: str,
    present: Iterable[str],
) -> dict[str, object]:
    """`resource` with every field Marrquee asserts on every wiring run set.

    `apiKey` is always sent, never compared - Sonarr and Radarr both mask a
    saved key back as `********`, so a real key would look "different"
    forever if this were a look-before-you-write field like the others.
    """
    updated: dict[str, object] = dict(resource)
    updated["name"] = downloader.name
    updated["enable"] = True
    updated["priority"] = 1
    updated["removeCompletedDownloads"] = True
    updated["removeFailedDownloads"] = True
    updated = _with_field(updated, "host", app_host(downloader, present))
    updated = _with_field(updated, "port", require_port(downloader))
    updated = _with_field(updated, "useSsl", False)
    updated = _with_field(updated, "urlBase", "")
    updated = _with_field(updated, "apiKey", qbit_key)
    updated = _with_field(updated, "username", "")
    updated = _with_field(updated, "password", "")
    updated = _with_field(updated, category_field, category)
    return updated


async def ensure_download_client(
    client: ArrClient,
    partner: CatalogApp,
    partner_key: str,
    downloader: CatalogApp,
    qbit_key: str,
    *,
    category_field: str,
    category: str,
    present: Iterable[str],
) -> StepOutcome:
    """Make sure `partner` (Sonarr or Radarr) has a download-client entry
    for `downloader` (qBittorrent), with the key and "remove completed" on.

    Every failure here - unreachable or refused - is reported against
    `partner`: this call only ever proves whether Marrquee's own request to
    `partner` succeeded, never whether `partner` can in turn reach
    `downloader` (that live round-trip is Sonarr's own connection test, a
    Pitch condition proven for real in Chunk 7). `present` is the install's
    own app ids - it decides `downloader`'s host (`gluetun` behind the VPN,
    `qbittorrent` on its own network), never `partner`'s, which always
    answers on its own arr network.
    """
    assert partner.network_via is None  # arr apps always answer on their own network
    base_url = app_base_url(partner, ())
    path = f"{partner.api_base}/{_DOWNLOAD_CLIENT_PATH}"

    listing = await client.request("GET", base_url, path, partner_key)
    if not listing.ok:
        return _unreachable_outcome(partner.name, listing)

    entries = listing.payload if isinstance(listing.payload, list) else []
    existing = _find_download_client(entries, downloader, present=present)
    host = app_host(downloader, present)

    if existing is not None:
        if _download_client_matches(
            existing,
            host=host,
            port=require_port(downloader),
            category_field=category_field,
            category=category,
        ):
            return StepOutcome(
                state="done",
                note=WIRING_NOTE_ALREADY_CONNECTED,
                technical=None,
                changed=False,
                transient=False,
            )

        updated = _with_download_client_fields(
            existing,
            downloader,
            qbit_key,
            category_field=category_field,
            category=category,
            present=present,
        )
        put_path = f"{path}/{existing.get('id')}"
        result = await client.request("PUT", base_url, put_path, partner_key, json_body=updated)
        if result.ok:
            return StepOutcome(
                state="done", note=None, technical=None, changed=True, transient=False
            )
        return _download_client_write_failure(partner.name, result)

    schema_path = f"{partner.api_base}/{_DOWNLOAD_CLIENT_SCHEMA_PATH}"
    schema_response = await client.request("GET", base_url, schema_path, partner_key)
    if not schema_response.ok:
        return _unreachable_outcome(partner.name, schema_response)

    schema_entries = schema_response.payload if isinstance(schema_response.payload, list) else []
    template = _find_download_client_schema(schema_entries)
    if template is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_refused(partner.name),
            technical=f"no schema entry for implementation {_QBIT_IMPLEMENTATION_NAME!r}",
            changed=False,
            transient=False,
        )

    new_entry: dict[str, object] = {key: value for key, value in template.items() if key != "id"}
    new_entry = _with_download_client_fields(
        new_entry,
        downloader,
        qbit_key,
        category_field=category_field,
        category=category,
        present=present,
    )

    result = await client.request("POST", base_url, path, partner_key, json_body=new_entry)
    if result.ok:
        return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)
    return _download_client_write_failure(partner.name, result)


def _download_client_write_failure(partner_name: str, response: ArrResponse) -> StepOutcome:
    if _transient(response.status):
        return _unreachable_outcome(partner_name, response)
    return StepOutcome(
        state="error",
        note=wiring_failure_refused(partner_name),
        technical=_failure_technical(response),
        changed=False,
        transient=False,
    )
