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

from marrquee.catalog import CatalogApp
from marrquee.wiring.arr_client import ArrClient, ArrResponse
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


def app_base_url(app: CatalogApp) -> str:
    """Where Marrquee, and every other app on the same network, reaches `app`.

    Every app's compose service name and container name are both its
    catalog id, so this is the same address the deploy engine's own
    readiness probe already reaches each app at.
    """
    return f"http://{app.id}:{app.port}"


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
    base_url = app_base_url(app)
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
    base_url = app_base_url(prowlarr)
    applications_path = f"{prowlarr.api_base}/{_APPLICATIONS_PATH}"

    listing = await client.request("GET", base_url, applications_path, prowlarr_key)
    if not listing.ok:
        return _unreachable_outcome(target.name, listing)

    entries = listing.payload if isinstance(listing.payload, list) else []
    existing = _find_application(entries, target)

    desired_prowlarr_url = app_base_url(prowlarr)
    desired_base_url = app_base_url(target)

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
    target_base_url = app_base_url(target)
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
