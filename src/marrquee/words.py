"""Every sentence Marrquee shows the owner, in one file they can read end to
end.

No other module composes user-facing copy - they import a constant or call
a function from here instead. That keeps a wording change a one-line diff
in this file rather than a hunt through templates and engine code, and it
means `WORDS_INVENTORY` (below) can be the owner's whole review surface for
what the app says to them.

Each feature area below gets its own fenced, commented section and its own
inventory tuple, appended into `WORDS_INVENTORY` without touching an
earlier section's lines.
"""

from __future__ import annotations

# Imported privately (aliased) so this module's own namespace stays "every
# public name here is a sentence a reader should review" - a bare `Callable`
# import would otherwise show up as a public name with nothing to say.
from collections.abc import Callable as _Callable

# =============================================================================
# Deploy Engine: app progress, deploy phases, storage refusals, failures
# =============================================================================

# --- App progress chip - the deploy screen's own short labels ---------------
STATUS_CHIP_WAITING = "Waiting"
STATUS_CHIP_STARTING = "Starting…"
STATUS_CHIP_DONE = "Ready"
# The mockup never shows a failed app - the whole point of this product is
# "every app started, first try" - so this fourth chip has no mockup hook to
# match. It only needs to read clearly next to the other three.
STATUS_CHIP_ERROR = "Error"


# --- App progress line - one sentence per state, naming the app -------------
def app_line_downloading(app_name: str) -> str:
    return f"Downloading {app_name} - this only happens the first time"


def app_line_starting(app_name: str) -> str:
    return f"Starting {app_name}"


def app_line_warming_up(app_name: str) -> str:
    return f"{app_name} is waking up"


def app_note_slow_start(app_name: str) -> str:
    return f"{app_name} is taking a little longer than usual - this is normal on first start."


def app_line_done(app_name: str) -> str:
    return f"{app_name} is ready"


def app_line_error(app_name: str) -> str:
    return f"{app_name} couldn't start"


# --- Deploy phase headlines ---------------------------------------------------
PHASE_HEADLINE_READY = "Nothing to set up yet."
PHASE_HEADLINE_RUNNING = "Starting your apps, one at a time."
PHASE_HEADLINE_WIRING = "Connecting everything together."
PHASE_HEADLINE_FINALE = "Your media server is live. Every app started, first try."


# --- Per-app headline - what a screen reader hears while one app is turn ----
def app_headline_starting(app_name: str) -> str:
    return f"Starting {app_name}..."


def app_headline_done(app_name: str) -> str:
    return f"{app_name} is ready."


# --- Storage refusals - why a typed path was turned down ---------------------
def refusal_populated_target(path: str) -> str:
    return (
        f"There are already files in {path}. Marrquee only ever sets up a fresh "
        "library, and it will never move or delete files you already have. Make "
        "a new empty folder on this drive and use that instead."
    )


# Deliberately no argument: the wizard never lets this box submit empty, so
# this only fires from a live check-as-you-type call made before a single
# character has landed - "did you mean" wording would make no sense here.
REFUSAL_PATH_EMPTY = "Type the folder for your big drive to check it."


def refusal_path_missing(path: str) -> str:
    return f"I can't find {path} on this machine. Check the spelling - did you mean one of these?"


def refusal_not_a_folder(path: str) -> str:
    return f"{path} is a file, not a folder. Pick a folder."


def refusal_not_writable(path: str) -> str:
    return (
        f"Marrquee isn't allowed to save anything in {path}. On your NAS, check "
        "that this shared folder allows changes."
    )


def refusal_system_path(path: str) -> str:
    return (
        f"{path} is part of the machine's own system, not a storage drive. Pick "
        "a folder on your big drive - on most NAS boxes it starts with /volume1."
    )


def refusal_name_clash(name: str) -> str:
    return (
        f"Your NAS already has an app called {name}. Marrquee will never touch "
        "an app you set up yourself. Stop or rename it, then try again."
    )


# One of the chosen app ids didn't match anything in the catalog - a real
# wizard never sends this, so it only ever fires against a malformed or
# out-of-date caller, not a normal owner mistake.
REFUSAL_UNKNOWN_APP = (
    "One of the apps you chose isn't one Marrquee knows about. Choose from "
    "the list Marrquee offers."
)

# --- Storage check - the wizard's live, as-you-type verdict -------------------
STORAGE_CHECK_OK_MESSAGE = (
    "This folder works. Marrquee will build your media folders inside it, "
    "and it will never touch anything already there."
)

# Keyed by StorageCheck's own reason strings (kept as plain `str`, not the
# `StorageCheckReason` type, so this leaf module never has to import from
# storage.py just to pick a sentence). "empty" is handled before this map is
# consulted - see `storage_check_message` below.
_STORAGE_CHECK_MESSAGES: dict[str, _Callable[[str], str]] = {
    "not_absolute": refusal_path_missing,
    "system_path": refusal_system_path,
    "missing": refusal_path_missing,
    "not_a_folder": refusal_not_a_folder,
    "not_writable": refusal_not_writable,
}


def storage_check_message(reason: str | None, path: str) -> str:
    """The right sentence for a `StorageCheck`'s `reason`, or the all-clear
    message when `reason` is None (`ok=True`).
    """
    if reason is None:
        return STORAGE_CHECK_OK_MESSAGE
    if reason == "empty":
        return REFUSAL_PATH_EMPTY
    return _STORAGE_CHECK_MESSAGES.get(reason, refusal_path_missing)(path)


# --- Deploy failures - a plain headline plus what to do next -----------------
FAILURE_DOCKER_UNREACHABLE = (
    "Marrquee can't reach Docker right now, so it can't start your apps. Make "
    "sure Docker is running, then press Try again."
)


def failure_download_failed(name: str) -> str:
    return (
        f"Marrquee couldn't download {name}. Check that this machine can reach "
        "the internet, then press Try again."
    )


def failure_never_became_ready(name: str) -> str:
    return (
        f"{name} started but never answered. Press Try again - if it keeps "
        "happening, the details below are what to send for help."
    )


def failure_port_in_use(name: str, port: int) -> str:
    return (
        f"Something else on this machine is already using port {port}, which "
        f"{name} needs. Stop that, then press Try again."
    )


def failure_compose_failed(name: str) -> str:
    return (
        f"Marrquee couldn't start {name}. Press Try again - if it keeps "
        "happening, the details below are what to send for help."
    )


# --- Resting-state refusal ----------------------------------------------------
REFUSAL_NOTHING_CHOSEN = "Choose your apps and your big drive first."


# --- App descriptions - the app catalog's own beginner one-liners -----------
PROWLARR_DESCRIPTION = "Your search sources, managed in one place."
SONARR_DESCRIPTION = "Finds and organizes your TV shows."
RADARR_DESCRIPTION = "Finds and organizes your movies."


# --- Compose file and install-file comments -----------------------------------
COMPOSE_FILE_HEADER_COMMENT = (
    "This file describes your media server. Marrquee wrote it, and you can "
    "read it. Every folder here is a real folder on your drive."
)
HOST_MOUNT_COMMENT = (
    "This lets Marrquee see your drives, so it can check the folder you type "
    "and build the media folders inside it. Marrquee only ever writes inside "
    "the one folder you choose."
)
DATA_MOUNT_COMMENT = (
    "Every app that touches media shares this one /data folder. That's what "
    "makes a finished download show up in your library instantly, instead of "
    "being copied twice."
)

# --- Marker file - how a re-run is told apart from someone else's library ----
MARKER_WHAT_IS_THIS = (
    "Marrquee built this folder. This file marks it as ours, so running the "
    "deploy again here is treated as a repeat setup, never as someone else's "
    "library."
)

_DEPLOY_ENGINE_WORDS: tuple[str, ...] = (
    "STATUS_CHIP_WAITING",
    "STATUS_CHIP_STARTING",
    "STATUS_CHIP_DONE",
    "app_line_downloading",
    "app_line_starting",
    "app_line_warming_up",
    "app_note_slow_start",
    "app_line_done",
    "app_line_error",
    "PHASE_HEADLINE_READY",
    "PHASE_HEADLINE_RUNNING",
    "PHASE_HEADLINE_WIRING",
    "PHASE_HEADLINE_FINALE",
    "app_headline_starting",
    "app_headline_done",
    "refusal_populated_target",
    "refusal_path_missing",
    "refusal_not_a_folder",
    "refusal_not_writable",
    "refusal_system_path",
    "refusal_name_clash",
    "REFUSAL_UNKNOWN_APP",
    "REFUSAL_PATH_EMPTY",
    "STORAGE_CHECK_OK_MESSAGE",
    "storage_check_message",
    "STATUS_CHIP_ERROR",
    "FAILURE_DOCKER_UNREACHABLE",
    "failure_download_failed",
    "failure_never_became_ready",
    "failure_port_in_use",
    "failure_compose_failed",
    "REFUSAL_NOTHING_CHOSEN",
    "PROWLARR_DESCRIPTION",
    "SONARR_DESCRIPTION",
    "RADARR_DESCRIPTION",
    "COMPOSE_FILE_HEADER_COMMENT",
    "HOST_MOUNT_COMMENT",
    "DATA_MOUNT_COMMENT",
    "MARKER_WHAT_IS_THIS",
)

# =============================================================================
# end Deploy Engine section
# =============================================================================

# The full review surface: every public name above, in one tuple. A later
# feature area adds its own fenced section above this line, then extends
# this tuple with its own `_..._WORDS` name - never editing an earlier
# section's entries.
WORDS_INVENTORY: tuple[str, ...] = (*_DEPLOY_ENGINE_WORDS,)
