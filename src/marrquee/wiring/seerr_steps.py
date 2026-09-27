"""Seerr's own wiring steps: its connection to Plex or Jellyfin, every
movie/TV library it can see there, and its Sonarr/Radarr entries.

Mirrors `wiring/steps.py`'s own shape - look before you write, and report an
honest `StepOutcome` - but talks to a `SeerrClient` instead of an arr app's
`ArrClient`. Neither function ever raises; a dead Seerr or a refused write
is a real answer, never a crash.

A status-level failure (Seerr didn't answer 2xx at all) is always reported
against "Seerr" itself - a 403 means Seerr's own key was rejected, which has
nothing to do with whichever media server or arr app the call was about. A
2xx answer whose CONTENT is wrong (a library still disabled, an echoed write
that doesn't match what was sent, a Plex that answers with someone else's
`machineId`) is reported against the app the content is about instead -
that failure is real and Seerr answered it, so retrying costs nothing but
the engine's own bounded budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from marrquee.catalog import (
    RECYCLARR_APP_ID,
    SEERR_APP_ID,
    CatalogApp,
    apps_in_order,
    require_port,
)
from marrquee.recyclarr import quality_profile_name
from marrquee.seerr import SEERR_API, SeerrClient, SeerrResponse, seerr_base_url
from marrquee.state import InstallState
from marrquee.storage import container_media_path
from marrquee.wiring.steps import StepOutcome
from marrquee.words import (
    SEERR_FAILURE_WRONG_PLEX,
    WIRING_NOTE_ALREADY_CONNECTED,
    seerr_failure_cant_reach,
    seerr_failure_no_folder,
    seerr_failure_no_profiles,
    seerr_note_libraries,
    seerr_note_no_libraries,
    seerr_note_profile,
    wiring_failure_refused,
    wiring_failure_unreachable,
)

# An entry Marrquee itself just created gets this profile when nothing more
# specific applies (no Recyclarr profile, and no existing entry to keep the
# choice from) - Seerr's own bundled TRaSH profile set always ships one
# named exactly this.
SEERR_FALLBACK_PROFILE: Final = "HD-1080p"


@dataclass(frozen=True)
class SeerrPlexTarget:
    """The `ip`/`port`/`useSsl` Seerr's own `settings/plex` (or, reused for
    the same three fields, `settings/jellyfin`) call wants.
    """

    ip: str
    port: int
    use_ssl: bool


def seerr_plex_target(base_url: str) -> SeerrPlexTarget | None:
    """`base_url` (plain http, or a `https://…plex.direct:port` address from
    a remote or https-only Plex) as the three fields Seerr's own connection
    call wants. `None` for anything that isn't a real http(s) URL. Pure -
    never touches the network.
    """
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        return None
    hostname = parsed.hostname
    if not hostname:
        return None
    ip = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return SeerrPlexTarget(ip=ip, port=port, use_ssl=parsed.scheme == "https")


def _path(suffix: str) -> str:
    return f"{SEERR_API}/{suffix}"


def _technical(method: str, path: str, response: SeerrResponse) -> str:
    """Built only from the method, path, status and `detail` - never
    `payload` or a request body - so a settings payload carrying Seerr's own
    key, an arr key, or a machine id can never reach diagnostics this way.
    """
    parts = [f"seerr: {method} {path} -> HTTP {response.status}"]
    if response.detail:
        parts.append(response.detail)
    return " ".join(parts)


def _seerr_transient(status: int) -> bool:
    return status == 0 or status >= 500


def _seerr_status_failure(method: str, path: str, response: SeerrResponse) -> StepOutcome:
    """The one fallback every non-2xx Seerr answer gets, unless the caller
    has a more specific note for this exact call. A 403 means Seerr itself
    rejected the request (a stale or wrong key) - never worth a retry, and
    never the target app's fault either.
    """
    if response.status == 403:
        return StepOutcome(
            state="error",
            note=wiring_failure_refused("Seerr"),
            technical=_technical(method, path, response),
            changed=False,
            transient=False,
        )
    return StepOutcome(
        state="error",
        note=wiring_failure_unreachable("Seerr"),
        technical=_technical(method, path, response),
        changed=False,
        transient=_seerr_transient(response.status),
    )


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


# --- the media-server connection, then its libraries --------------------------


async def ensure_seerr_media_server(
    client: SeerrClient,
    base_url: str,
    api_key: str,
    server: Literal["plex", "jellyfin"],
    *,
    plex_target: SeerrPlexTarget | None,
    expected_machine_id: str | None,
    owner_host: str | None,
    name: str,
) -> StepOutcome:
    """Make sure Seerr is pointed at `plex_target` for `server`, then that
    every movie/TV library it can see there is turned on.

    `plex_target` carries Jellyfin's own `ip`/`port` too - both server kinds
    share the same three connection fields from Seerr's point of view.
    `expected_machine_id` is only ever meaningful for Plex; Jellyfin has no
    such check. A caller with no target at all (the gateway isn't known yet,
    say) gets an honest, non-transient error rather than a crash.
    """
    if plex_target is None:
        return StepOutcome(
            state="error",
            note=wiring_failure_unreachable(name),
            technical=f"no address for seerr's {server}",
            changed=False,
            transient=False,
        )

    settings_path = _path(f"settings/{server}")
    read = await client.request("GET", base_url, settings_path, api_key=api_key)
    if not read.ok:
        return _seerr_status_failure("GET", settings_path, read)
    current = read.payload if isinstance(read.payload, dict) else {}

    if server == "plex":
        connection_outcome = await _ensure_plex_connection(
            client, base_url, api_key, settings_path, current, plex_target, expected_machine_id
        )
    else:
        connection_outcome = await _ensure_jellyfin_connection(
            client, base_url, api_key, settings_path, current, plex_target, owner_host
        )
    if isinstance(connection_outcome, StepOutcome):
        return connection_outcome
    changed = connection_outcome

    return await _ensure_seerr_libraries(
        client, base_url, api_key, server, name, current, changed=changed
    )


async def _ensure_plex_connection(
    client: SeerrClient,
    base_url: str,
    api_key: str,
    settings_path: str,
    current: Mapping[str, object],
    target: SeerrPlexTarget,
    expected_machine_id: str | None,
) -> StepOutcome | bool:
    """`True`/`False` for "changed", or a `StepOutcome` to return immediately."""
    machine_id = current.get("machineId")
    connected = (
        current.get("ip") == target.ip
        and current.get("port") == target.port
        and current.get("useSsl") == target.use_ssl
        and _nonempty_str(machine_id)
        and (expected_machine_id is None or machine_id == expected_machine_id)
    )
    if connected:
        return False

    write = await client.request(
        "POST",
        base_url,
        settings_path,
        api_key=api_key,
        json_body={"ip": target.ip, "port": target.port, "useSsl": target.use_ssl},
    )
    if not write.ok:
        return _seerr_status_failure("POST", settings_path, write)

    written = write.payload if isinstance(write.payload, dict) else {}
    written_machine_id = written.get("machineId")
    if not _nonempty_str(written_machine_id) or (
        expected_machine_id is not None and written_machine_id != expected_machine_id
    ):
        return StepOutcome(
            state="error",
            note=SEERR_FAILURE_WRONG_PLEX,
            technical=_technical("POST", settings_path, write),
            changed=False,
            transient=False,
        )
    return True


async def _ensure_jellyfin_connection(
    client: SeerrClient,
    base_url: str,
    api_key: str,
    settings_path: str,
    current: Mapping[str, object],
    target: SeerrPlexTarget,
    owner_host: str | None,
) -> StepOutcome | bool:
    external_hostname = f"http://{owner_host}:{target.port}" if owner_host else None
    connected = (
        current.get("ip") == target.ip
        and current.get("port") == target.port
        and current.get("useSsl") is False
        and current.get("urlBase") == ""
        and (external_hostname is None or current.get("externalHostname") == external_hostname)
    )
    if connected:
        return False

    body: dict[str, object] = {"ip": target.ip, "port": target.port, "useSsl": False, "urlBase": ""}
    if external_hostname is not None:
        body["externalHostname"] = external_hostname

    write = await client.request("POST", base_url, settings_path, api_key=api_key, json_body=body)
    if not write.ok:
        return _seerr_status_failure("POST", settings_path, write)
    return True


def _library_ids(payload: object) -> list[str]:
    if not isinstance(payload, list):
        return []
    ids: list[str] = []
    for entry in payload:
        if isinstance(entry, dict):
            library_id = entry.get("id")
            if isinstance(library_id, str) and library_id:
                ids.append(library_id)
    return ids


def _enabled_ids(payload: object) -> set[str]:
    if not isinstance(payload, list):
        return set()
    enabled: set[str] = set()
    for entry in payload:
        if isinstance(entry, dict) and entry.get("enabled") is True:
            library_id = entry.get("id")
            if isinstance(library_id, str):
                enabled.add(library_id)
    return enabled


def _libraries_confirmed(payload: object, ids: list[str]) -> bool:
    if not isinstance(payload, list):
        return False
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            return False
        library_id = entry.get("id")
        if not isinstance(library_id, str) or entry.get("enabled") is not True:
            return False
        seen.add(library_id)
    return seen == set(ids)


async def _ensure_seerr_libraries(
    client: SeerrClient,
    base_url: str,
    api_key: str,
    server: Literal["plex", "jellyfin"],
    name: str,
    current: Mapping[str, object],
    *,
    changed: bool,
) -> StepOutcome:
    before = _enabled_ids(current.get("libraries"))
    library_path = _path(f"settings/{server}/library")

    synced = await client.request(
        "GET", base_url, library_path, api_key=api_key, params=(("sync", "true"),)
    )
    if synced.status == 404:
        return StepOutcome(
            state="skipped",
            note=seerr_note_no_libraries(name),
            technical=None,
            changed=changed,
            transient=False,
        )
    if not synced.ok:
        return _seerr_status_failure("GET", library_path, synced)

    ids = _library_ids(synced.payload)
    if not ids:
        return StepOutcome(
            state="skipped",
            note=seerr_note_no_libraries(name),
            technical=None,
            changed=changed,
            transient=False,
        )

    # httpx encodes the comma; Express's own query parser decodes it back
    # into the same list `enable=a,b` describes.
    enabled = await client.request(
        "GET", base_url, library_path, api_key=api_key, params=(("enable", ",".join(ids)),)
    )
    if not enabled.ok:
        return _seerr_status_failure("GET", library_path, enabled)
    if not _libraries_confirmed(enabled.payload, ids):
        return StepOutcome(
            state="error",
            note=wiring_failure_refused(name),
            technical=_technical("GET", library_path, enabled),
            changed=changed,
            transient=False,
        )

    if set(ids) != before:
        sync_path = _path(f"settings/{server}/sync")
        started = await client.request(
            "POST", base_url, sync_path, api_key=api_key, json_body={"start": True}
        )
        if not started.ok:
            return _seerr_status_failure("POST", sync_path, started)
        return StepOutcome(
            state="done",
            note=seerr_note_libraries(name),
            technical=None,
            changed=True,
            transient=False,
        )

    if not changed:
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )
    return StepOutcome(state="done", note=None, technical=None, changed=True, transient=False)


# --- Sonarr/Radarr: the known key, the library folder, and a profile ---------


def _find_seerr_arr_entry(entries: list[object], arr: CatalogApp) -> dict[str, object] | None:
    """The existing Seerr entry for `arr`, or None.

    Matches by (hostname, port) first - the pairing that actually proves
    this entry talks to `arr` - excluding any 4K profile entry, which is a
    second, independent connection Marrquee never touches. Falls back to a
    name match so a hand-edited entry is still found and repaired instead of
    duplicated.
    """
    port = require_port(arr)
    fallback: dict[str, object] | None = None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("is4k") is True:
            continue
        if entry.get("hostname") == arr.id and entry.get("port") == port:
            return entry
        if fallback is None and entry.get("name") == arr.name:
            fallback = entry
    return fallback


def _choose_profile(
    profiles: list[object], preferred_profile: str | None, existing: Mapping[str, object] | None
) -> tuple[int, str] | None:
    """The quality profile a fresh or repaired entry gets - the preferred
    name first, then whatever the entry already had (as long as Seerr still
    lists it), then the fallback, then whatever Seerr lists first. `None`
    only when Seerr lists no profile for this app at all.
    """
    named: dict[str, int] = {}
    by_id: dict[int, str] = {}
    for entry in profiles:
        if not isinstance(entry, dict):
            continue
        profile_id = entry.get("id")
        profile_name = entry.get("name")
        if isinstance(profile_id, int) and not isinstance(profile_id, bool):
            if isinstance(profile_name, str):
                named[profile_name] = profile_id
                by_id[profile_id] = profile_name

    if preferred_profile is not None and preferred_profile in named:
        return named[preferred_profile], preferred_profile

    if existing is not None:
        existing_id = existing.get("activeProfileId")
        if isinstance(existing_id, int) and not isinstance(existing_id, bool):
            existing_name = by_id.get(existing_id)
            if existing_name is not None:
                return existing_id, existing_name

    if SEERR_FALLBACK_PROFILE in named:
        return named[SEERR_FALLBACK_PROFILE], SEERR_FALLBACK_PROFILE

    for entry in profiles:
        if isinstance(entry, dict):
            profile_id = entry.get("id")
            profile_name = entry.get("name")
            if isinstance(profile_id, int) and not isinstance(profile_id, bool):
                if isinstance(profile_name, str):
                    return profile_id, profile_name
    return None


def _folder_listed(root_folders: list[object], folder: str) -> bool:
    return any(isinstance(entry, dict) and entry.get("path") == folder for entry in root_folders)


def _owned_fields(
    arr: CatalogApp,
    arr_key: str,
    *,
    profile_id: int,
    profile_name: str,
    folder: str,
    owner_host: str | None,
) -> dict[str, object]:
    """The fields Marrquee asserts on every wiring run - compared against an
    existing entry, and always sent on a write. `externalUrl` is added only
    when the owner's own address is known; an existing entry's own
    `externalUrl` (or lack of one) is left untouched otherwise.
    """
    owned: dict[str, object] = {
        "hostname": arr.id,
        "port": require_port(arr),
        "apiKey": arr_key,
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": profile_id,
        "activeProfileName": profile_name,
        "activeDirectory": folder,
        "is4k": False,
        "isDefault": True,
        "syncEnabled": True,
    }
    if owner_host is not None:
        owned["externalUrl"] = f"http://{owner_host}:{require_port(arr)}"
    return owned


def _owned_keys_match(candidate: Mapping[str, object], owned: Mapping[str, object]) -> bool:
    return all(candidate.get(key) == value for key, value in owned.items())


def _new_entry_body(arr: CatalogApp, owned: Mapping[str, object]) -> dict[str, object]:
    """A brand-new entry's whole body: `owned`, plus Seerr's own sensible
    defaults for everything Marrquee never asserts again after this.
    """
    body: dict[str, object] = {
        "name": arr.name,
        **owned,
        "tags": [],
        "preventSearch": False,
        "tagRequests": False,
        "overrideRule": [],
    }
    if "externalUrl" not in body:
        body["externalUrl"] = ""
    if arr.id == "radarr":
        body["minimumAvailability"] = "released"
    else:
        body.update(
            {
                "seriesType": "standard",
                "animeSeriesType": "anime",
                "enableSeasonFolders": True,
                "monitorNewItems": "all",
            }
        )
    return body


async def ensure_seerr_arr(
    client: SeerrClient,
    base_url: str,
    api_key: str,
    arr: CatalogApp,
    arr_key: str,
    *,
    preferred_profile: str | None,
    owner_host: str | None,
) -> StepOutcome:
    """Make sure Seerr has `arr` (Sonarr or Radarr) as a non-4K default
    server, with the known key, its own library folder, and a profile -
    writing only what's actually needed, and never touching a field it
    doesn't own on an existing entry.
    """
    settings_path = _path(f"settings/{arr.id}")
    listing = await client.request("GET", base_url, settings_path, api_key=api_key)
    if not listing.ok:
        return _seerr_status_failure("GET", settings_path, listing)
    entries = listing.payload if isinstance(listing.payload, list) else []
    existing = _find_seerr_arr_entry(entries, arr)

    test_path = _path(f"settings/{arr.id}/test")
    test_body = {
        "hostname": arr.id,
        "port": require_port(arr),
        "apiKey": arr_key,
        "useSsl": False,
        "baseUrl": "",
    }
    tested = await client.request("POST", base_url, test_path, api_key=api_key, json_body=test_body)
    if not tested.ok:
        return StepOutcome(
            state="error",
            note=seerr_failure_cant_reach(arr.name),
            technical=_technical("POST", test_path, tested),
            changed=False,
            transient=_seerr_transient(tested.status),
        )

    payload = tested.payload if isinstance(tested.payload, dict) else {}
    profiles = payload.get("profiles")
    profiles = profiles if isinstance(profiles, list) else []
    root_folders = payload.get("rootFolders")
    root_folders = root_folders if isinstance(root_folders, list) else []

    chosen = _choose_profile(profiles, preferred_profile, existing)
    if chosen is None:
        return StepOutcome(
            state="error",
            note=seerr_failure_no_profiles(arr.name),
            technical=None,
            changed=False,
            transient=False,
        )
    profile_id, profile_name = chosen

    folder = str(container_media_path(arr.media_folders[0]))
    if not _folder_listed(root_folders, folder):
        return StepOutcome(
            state="error",
            note=seerr_failure_no_folder(arr.name),
            technical=None,
            changed=False,
            transient=False,
        )

    owned = _owned_fields(
        arr,
        arr_key,
        profile_id=profile_id,
        profile_name=profile_name,
        folder=folder,
        owner_host=owner_host,
    )

    if existing is not None and _owned_keys_match(existing, owned):
        return StepOutcome(
            state="done",
            note=WIRING_NOTE_ALREADY_CONNECTED,
            technical=None,
            changed=False,
            transient=False,
        )

    if existing is not None:
        write_method: Literal["PUT", "POST"] = "PUT"
        write_path = f"{settings_path}/{existing.get('id')}"
        write_body: dict[str, object] = {**existing, **owned}
    else:
        write_method = "POST"
        write_path = settings_path
        write_body = _new_entry_body(arr, owned)

    write = await client.request(
        write_method, base_url, write_path, api_key=api_key, json_body=write_body
    )
    if not write.ok:
        return _seerr_status_failure(write_method, write_path, write)

    echoed = write.payload if isinstance(write.payload, dict) else {}
    if not _owned_keys_match(echoed, owned):
        return StepOutcome(
            state="error",
            note=wiring_failure_refused(arr.name),
            technical=_technical(write_method, write_path, write),
            changed=False,
            transient=False,
        )

    return StepOutcome(
        state="done",
        note=seerr_note_profile(arr.name, profile_name),
        technical=None,
        changed=True,
        transient=False,
    )


# --- refresh_seerr_profiles: after a Marrquee-started Recyclarr sync ---------


async def refresh_seerr_profiles(
    client: SeerrClient,
    install: InstallState,
    answers: Mapping[str, Mapping[str, str]],
    owner_host: str | None,
) -> tuple[StepOutcome, ...]:
    """Move Seerr's Sonarr/Radarr defaults onto Recyclarr's own profile the
    moment a Marrquee-started sync actually creates it.

    Called only from `RecyclarrMonitor`'s own `after_sync` hook (main.py) -
    never from the wiring engine's own run, which would flash Seerr's
    poster back to "Connecting..." after every ordinary Sync now. Never
    raises: `ensure_seerr_arr` already turns every failure Seerr can answer
    with into a `StepOutcome`, so there is nothing left here to catch.

    `()` with no work to do - Seerr isn't installed, or Recyclarr isn't, so
    there's no profile this run could possibly have created.
    """
    if SEERR_APP_ID not in install.app_ids or RECYCLARR_APP_ID not in install.app_ids:
        return ()

    base_url = seerr_base_url()
    seerr_key = install.api_keys[SEERR_APP_ID]
    outcomes: list[StepOutcome] = []
    for arr in apps_in_order(install.app_ids):
        if arr.id not in ("sonarr", "radarr"):
            continue
        outcomes.append(
            await ensure_seerr_arr(
                client,
                base_url,
                seerr_key,
                arr,
                install.api_keys[arr.id],
                preferred_profile=quality_profile_name(arr.id, answers),
                owner_host=owner_host,
            )
        )
    return tuple(outcomes)
