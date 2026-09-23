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
# or `Sequence` import would otherwise show up as a public name with nothing
# to say.
from collections.abc import Callable as _Callable
from collections.abc import Sequence as _Sequence

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


# Deliberately no argument: an empty path has nothing to name, so "did you
# mean" wording would make no sense here. No control on the wizard is ever
# disabled, so this can fire either from a live check-as-you-type call made
# before a single character has landed, or from a submitted, still-empty box.
REFUSAL_PATH_EMPTY = "Type the folder for your big drive to check it."


def refusal_path_missing(path: str) -> str:
    return f"I can't find {path} on this machine. Check the spelling - did you mean one of these?"


def refusal_not_shared(path: str, shared: _Sequence[str] = ()) -> str:
    """`path`'s first folder isn't one Marrquee's install file mounts in.

    This is a different problem from `refusal_path_missing`: nothing is
    misspelled, Marrquee simply has no view of that drive at all. Naming the
    folders it *can* see (when there are a handful) turns "check the
    spelling" - which would be wrong here - into "pick one of these
    instead".
    """
    if 1 <= len(shared) <= 3:
        roots = ", ".join(shared)
        return (
            f"Marrquee can't see {path} - it can only see folders inside {roots}. "
            "Pick a folder in there. If this folder is on another drive, that "
            "drive needs its own line in Marrquee's install file first - the "
            "project page shows how."
        )
    return (
        f"Marrquee can't see {path} on this machine. Pick a folder inside one of "
        "your NAS's shared folders - on most NAS boxes they start with /volume1."
    )


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
    "not_shared": refusal_not_shared,
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
    "This lets Marrquee see your shared folders, so it can check the folder "
    "you type and build the media folders inside it. Marrquee only ever "
    "writes inside the one folder you choose."
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
    "refusal_not_shared",
)

# =============================================================================
# end Deploy Engine section
# =============================================================================

# =============================================================================
# Wizard screens: pick your apps, where's your big drive
# =============================================================================

# --- Chrome shared by both screens --------------------------------------------
WIZARD_TITLE_APPS = "Choose your apps · Marrquee"
WIZARD_TITLE_DRIVE = "Where's your big drive? · Marrquee"
WIZARD_EYEBROW = "SETTING UP"
WIZARD_STEP_APPS = "Your apps"
WIZARD_STEP_DRIVE = "Your drive"
WIZARD_STEP_DEPLOY = "Deploy"
WIZARD_BACK = "Back"


# --- Screen 1: pick your apps --------------------------------------------------
# lead, gradient word(s), tail - the template renders
# `lead<span class="accent">words</span>tail`.
WIZARD_APPS_HEADLINE: tuple[str, str, str] = ("What do you want on your ", "media server", "?")
WIZARD_APPS_LEDE = "Tick the apps you'd like. You can always come back and add more later."
WIZARD_CONTINUE = "Continue"
WIZARD_PICK_AT_LEAST_ONE = (
    "Pick at least one app to carry on. Prowlarr is the one that finds "
    "things, so most people keep it ticked."
)
WIZARD_PLATFORM_WARNING = (
    "Marrquee looks like it's running on Docker Desktop. For now Marrquee is "
    "only tested on a NAS or a Linux machine - you can carry on, but things "
    "may not work as expected."
)


# --- Screen 2: where's your big drive ------------------------------------------
WIZARD_DRIVE_HEADLINE: tuple[str, str, str] = ("Where's your ", "big drive", "?")
WIZARD_DRIVE_LEDE = (
    "Type the folder where your films and shows should live. Marrquee makes "
    "its own folders inside it."
)
WIZARD_PATH_LABEL = "Folder on this machine"
WIZARD_PATH_PLACEHOLDER = "/volume1/media"


def wizard_path_hint(shared: _Sequence[str] = ()) -> str:
    """Built from the folders Marrquee can actually see, never a guess.

    The install file only mounts the drive(s) the owner listed, so naming
    /mnt or /srv here (the old wording) would point someone at a folder
    Marrquee could never find. When there's nothing to name, or too many
    to list plainly, the field hint falls back to the one guess that's
    right on most NAS boxes instead.
    """
    if 1 <= len(shared) <= 3:
        roots = ", ".join(shared)
        return (
            f"Marrquee can see the shared folders inside {roots}. Your big "
            f"drive's folder will be in there - for example {shared[0]}/media."
        )
    return "On most NAS boxes this starts with /volume1."


WIZARD_FRESH_START_NOTE = (
    "Marrquee only ever makes new, empty folders inside the one you choose. "
    "It never moves, changes or deletes anything you already have. Already "
    "have a library? Leave it exactly where it is - you can copy things into "
    "the new folders later, or point your media server at the old folder as "
    "well."
)


def wizard_folder_found(free: str) -> str:
    return f"Folder found - {free} free"


WIZARD_FOUND_ROOM_UNKNOWN = (
    "Folder found. Marrquee will check how much room it has when you press Deploy."
)
WIZARD_NOT_WHOLE_PATH = "Type the whole folder path, starting with a /. For example /volume1/media."


def wizard_did_you_mean(path: str) -> str:
    return f"We couldn't find that folder. Did you mean {path}?"


def wizard_use_suggestion(path: str) -> str:
    return f"Use {path}"


WIZARD_MISSING_NO_SUGGESTION = "We couldn't find that folder on this machine. Check the spelling."


def wizard_other_drive_hint(top: str) -> str:
    return (
        f"If {top} is a separate drive, Marrquee can't see it yet - it needs "
        "its own line in Marrquee's install file first. The project page "
        "shows how."
    )


WIZARD_CHECKING = "Checking…"
WIZARD_CONTINUE_TO_DEPLOY = "Continue to Deploy"


def free_space_terabytes(tb: float) -> str:
    return f"about {tb:.1f} TB"


def free_space_gigabytes(gb: int) -> str:
    return f"about {gb} GB"


FREE_SPACE_UNDER_ONE_GB = "less than 1 GB"


# --- Diagnostics page (formerly the alive page at "/") -------------------------
DIAGNOSTICS_LEDE = (
    "This page checks that Marrquee can reach Docker and save its settings. "
    "If a line is red, it says what to do."
)


# --- Screen 2: time zone, under the folder panel --------------------------------
WIZARD_TIMEZONE_LABEL = "Your time zone"
WIZARD_TIMEZONE_HINT = "So your apps' schedules and logs show your local time."
WIZARD_TIMEZONE_UTC = "UTC (the same everywhere)"

# Each dropdown heading, keyed by the time zone database's own region name -
# "Other" is not a database region; it is the one synthetic group that holds
# only the UTC option. `wizard.timezone_groups` reads this dict to label the
# group it builds for each region, in the same fixed order every time.
TIMEZONE_REGION_LABELS: dict[str, str] = {
    "Africa": "Africa",
    "America": "Americas",
    "Antarctica": "Antarctica",
    "Asia": "Asia",
    "Atlantic": "Atlantic",
    "Australia": "Australia",
    "Europe": "Europe",
    "Indian": "Indian Ocean",
    "Pacific": "Pacific",
    "Other": "Other",
}

_WIZARD_WORDS: tuple[str, ...] = (
    "WIZARD_TITLE_APPS",
    "WIZARD_TITLE_DRIVE",
    "WIZARD_EYEBROW",
    "WIZARD_STEP_APPS",
    "WIZARD_STEP_DRIVE",
    "WIZARD_STEP_DEPLOY",
    "WIZARD_BACK",
    "WIZARD_APPS_HEADLINE",
    "WIZARD_APPS_LEDE",
    "WIZARD_CONTINUE",
    "WIZARD_PICK_AT_LEAST_ONE",
    "WIZARD_PLATFORM_WARNING",
    "WIZARD_DRIVE_HEADLINE",
    "WIZARD_DRIVE_LEDE",
    "WIZARD_PATH_LABEL",
    "WIZARD_PATH_PLACEHOLDER",
    "wizard_path_hint",
    "WIZARD_FRESH_START_NOTE",
    "wizard_folder_found",
    "WIZARD_FOUND_ROOM_UNKNOWN",
    "WIZARD_NOT_WHOLE_PATH",
    "wizard_did_you_mean",
    "wizard_use_suggestion",
    "WIZARD_MISSING_NO_SUGGESTION",
    "wizard_other_drive_hint",
    "WIZARD_CHECKING",
    "WIZARD_CONTINUE_TO_DEPLOY",
    "free_space_terabytes",
    "free_space_gigabytes",
    "FREE_SPACE_UNDER_ONE_GB",
    "DIAGNOSTICS_LEDE",
    "WIZARD_TIMEZONE_LABEL",
    "WIZARD_TIMEZONE_HINT",
    "WIZARD_TIMEZONE_UTC",
    "TIMEZONE_REGION_LABELS",
)

# =============================================================================
# end Wizard screens section
# =============================================================================

# =============================================================================
# Wiring: connecting Prowlarr, Sonarr and Radarr together
# =============================================================================


# --- Step lines - what each connection step says while it runs ---------------
def wiring_line_app_sync(source_name: str, target_name: str) -> str:
    return f"Introducing {source_name} to {target_name}"


def wiring_line_root_folder(app_name: str, media_label: str) -> str:
    return f"Telling {app_name} where your {media_label} live"


# Keyed by the catalog's own `media_folders` entries - the plain-language
# name for each, reused anywhere a media folder needs a friendly label.
# Lowercase "movies" is deliberate: it reads naturally both inline ("your
# movies live") and as a standalone row label ("movies").
MEDIA_FOLDER_LABEL: dict[str, str] = {
    "tv": "TV shows",
    "movies": "movies",
}


# --- Step chips - one per state, chosen purely from state --------------------
WIRING_CHIP_RUNNING = "Connecting…"
WIRING_CHIP_DONE = "Connected"
WIRING_CHIP_SKIPPED = "Nothing to do"
WIRING_CHIP_ERROR = "Couldn't connect"


# --- Step note - a connection that needed no change ---------------------------
WIRING_NOTE_ALREADY_CONNECTED = "Already connected - nothing to change."


# --- Plan-level skips - honest, never a failure --------------------------------
WIRING_NOTHING_TO_CONNECT = "Nothing to connect this time."


def wiring_skip_no_prowlarr(app_name: str) -> str:
    return (
        f"You didn't add Prowlarr this time, so {app_name} has no search sources to connect to yet."
    )


WIRING_SKIP_PROWLARR_ALONE = (
    "Prowlarr is on its own for now - add Sonarr or Radarr later and Marrquee will connect them."
)


# --- Step note - a slow app the engine is still waiting for --------------------
def wiring_note_still_waking(app_name: str) -> str:
    return f"{app_name} is still waking up - Marrquee is waiting for it."


# --- Step failures - what happened, and what to do next -----------------------
def wiring_failure_unreachable(app_name: str) -> str:
    return (
        f"Marrquee couldn't reach {app_name} to finish connecting it. Your apps "
        "are running fine - press Deploy again and Marrquee will pick up where "
        "it left off."
    )


def wiring_failure_refused(app_name: str) -> str:
    return (
        f"{app_name} didn't accept the connection. Your apps are running fine - "
        "press Deploy again to retry. If it keeps happening, the Diagnostics "
        "page has the details to send for help."
    )


def wiring_failure_folder(app_name: str, host_path: str) -> str:
    return (
        f"{app_name} wouldn't accept the folder {host_path}. Check that folder "
        "exists on your drive, then press Deploy again."
    )


def wiring_failure_prowlarr_too_old(app_name: str) -> str:
    return (
        f"This version of Prowlarr doesn't know how to connect to {app_name}. "
        "Marrquee left it alone."
    )


# --- Finale note - names every failed connection, never a count ----------------
def wiring_finale_note(failed_lines: _Sequence[str]) -> str:
    """The finale screen shows only its own last wiring row, so a note that
    said "one connection didn't finish" without naming which one could point
    at a step that actually succeeded. Naming every failed line here keeps
    the note true standing on its own.
    """
    lines = list(failed_lines)
    if len(lines) == 1:
        return (
            f"Your apps are all running. One connection didn't finish: {lines[0]}. "
            "Press Deploy again to retry - it's safe to repeat."
        )
    joined = ", ".join(lines)
    return (
        f"Your apps are all running. These connections didn't finish: {joined}. "
        "Press Deploy again to retry - it's safe to repeat."
    )


_WIRING_WORDS: tuple[str, ...] = (
    "wiring_line_app_sync",
    "wiring_line_root_folder",
    "MEDIA_FOLDER_LABEL",
    "WIRING_CHIP_RUNNING",
    "WIRING_CHIP_DONE",
    "WIRING_CHIP_SKIPPED",
    "WIRING_CHIP_ERROR",
    "WIRING_NOTE_ALREADY_CONNECTED",
    "WIRING_NOTHING_TO_CONNECT",
    "wiring_skip_no_prowlarr",
    "WIRING_SKIP_PROWLARR_ALONE",
    "wiring_note_still_waking",
    "wiring_failure_unreachable",
    "wiring_failure_refused",
    "wiring_failure_folder",
    "wiring_failure_prowlarr_too_old",
    "wiring_finale_note",
)

# =============================================================================
# end Wiring section
# =============================================================================

# The full review surface: every public name above, in one tuple. A later
# feature area adds its own fenced section above this line, then extends
# this tuple with its own `_..._WORDS` name - never editing an earlier
# section's entries.
WORDS_INVENTORY: tuple[str, ...] = (*_DEPLOY_ENGINE_WORDS, *_WIZARD_WORDS, *_WIRING_WORDS)
