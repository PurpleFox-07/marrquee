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

# =============================================================================
# Deploy screen: the ready ticket, the running posters, the finale and the
# calm failure - every sentence new to this story
# =============================================================================

DEPLOY_TITLE = "Deploy · Marrquee"
DEPLOY_EYEBROW = "Opening night"

# The lead line, then the line the template puts in the gradient span - two
# separate lines, unlike the wizard headlines' single line with a gradient
# word inside it.
DEPLOY_HEADLINE: tuple[str, str] = ("Tonight's feature:", "your media server")

DEPLOY_LEDE = "Every app takes the stage, one at a time. Roll the reel when you're ready."


def bill_line(count: int, first_name: str) -> str:
    return f"{count} apps on the bill tonight, starting with {first_name}."


def bill_one_app(name: str) -> str:
    return f"One app on the bill tonight: {name}."


DOWNLOADS_LABEL = "Downloads"
DEPLOY_BUTTON = "Deploy your media server"
DEPLOY_MICROCOPY = "This usually takes a few minutes. You can leave this page open."
RUN_SUB = "Each poster lights up as its app comes online."

# The single source of truth for the wiring count - both the server and the
# script's own `data-count-template` attribute read this exact template, so
# the two can never quietly say something different.
WIRING_STEP_TEMPLATE = "Step {index} of {total}"


def wiring_step_label(index: int, total: int) -> str:
    return WIRING_STEP_TEMPLATE.format(index=index, total=total)


FINALE_BADGE = "Every app, first try"
FINALE_HEADLINE = "Now showing: your media server"
FINALE_SUB = "Full house. Every app came on, first try."
FINALE_CTA = "Go to your Hub"
SHOWTIMES_TITLE = "Or open anything directly"


def open_app_label(name: str) -> str:
    return f"Open {name}"


DEPLOY_TRY_AGAIN = "Try again"
DEPLOY_AGAIN = "Deploy again"
SEE_TECHNICAL_DETAILS = "See the technical details"
NOSCRIPT_REFRESH_NOTE = (
    "Your browser has JavaScript switched off, so this page refreshes itself "
    "every few seconds to show how far along the deploy is."
)

# --- Diagnostics page: the Last problem section --------------------------------
LAST_PROBLEM_TITLE = "Last problem"
LAST_PROBLEM_INTRO = (
    "This is what went wrong the last time you pressed Deploy. If someone is "
    "helping you, copy it and send it to them. Marrquee has already hidden "
    "your apps' secret keys."
)
LAST_PROBLEM_EMPTY = (
    "Nothing has gone wrong. If a deploy ever runs into trouble, the details will show up here."
)
COPY_BUTTON = "Copy"
COPY_DONE = "Copied"
COPY_BLOCKED = (
    "Your browser wouldn't let Marrquee copy this. The text is selected - "
    "press Ctrl+C (or Cmd+C on a Mac) to copy it."
)

_DEPLOY_SCREEN_WORDS: tuple[str, ...] = (
    "DEPLOY_TITLE",
    "DEPLOY_EYEBROW",
    "DEPLOY_HEADLINE",
    "DEPLOY_LEDE",
    "bill_line",
    "bill_one_app",
    "DOWNLOADS_LABEL",
    "DEPLOY_BUTTON",
    "DEPLOY_MICROCOPY",
    "RUN_SUB",
    "WIRING_STEP_TEMPLATE",
    "wiring_step_label",
    "FINALE_BADGE",
    "FINALE_HEADLINE",
    "FINALE_SUB",
    "FINALE_CTA",
    "SHOWTIMES_TITLE",
    "open_app_label",
    "DEPLOY_TRY_AGAIN",
    "DEPLOY_AGAIN",
    "SEE_TECHNICAL_DETAILS",
    "NOSCRIPT_REFRESH_NOTE",
    "LAST_PROBLEM_TITLE",
    "LAST_PROBLEM_INTRO",
    "LAST_PROBLEM_EMPTY",
    "COPY_BUTTON",
    "COPY_DONE",
    "COPY_BLOCKED",
)

# =============================================================================
# end Deploy screen section
# =============================================================================

# =============================================================================
# Hub: the home page after a deploy
# =============================================================================

HUB_TITLE = "Your media server · Marrquee"
HUB_EYEBROW = "Now playing"

# lead, gradient word(s) - the template renders `lead<span
# class="accent">words</span>`, the same pattern the Deploy headline uses.
HUB_HEADLINE: tuple[str, str] = ("Your", "media server")

HUB_LEDE = "Everything you set up, one click away."

# --- Status row - the words next to every poster's dot -----------------------
HUB_STATUS_LABEL = "Status:"
HUB_CHIP_UP = "Up"
HUB_CHIP_STARTING = "Starting"
HUB_CHIP_DOWN = "Down"
HUB_CHIP_UNKNOWN = "Not sure"


# --- Poster line - one sentence per state, naming the app --------------------
def hub_line_starting(name: str) -> str:
    return f"{name} is starting up."


def hub_line_down_last_seen(name: str, when: str) -> str:
    return f"{name} stopped - last seen {when}."


def hub_line_down(name: str) -> str:
    return f"{name} isn't running right now."


def hub_line_gone(name: str) -> str:
    return f"{name} isn't on this machine any more."


def hub_line_unknown(name: str) -> str:
    return f"Marrquee couldn't check {name} just now."


def hub_line_no_address(name: str, port: int) -> str:
    return (
        "Marrquee couldn't work out this machine's address, so it can't make "
        f"a button for {name}. It's on port {port} of the same address you "
        "used to open Marrquee."
    )


def hub_open_app_aria(name: str, chip: str) -> str:
    return f"Open {name}. Status: {chip}. Opens in a new tab."


# --- Notes and banners - always in the HTML, shown or hidden by attributes ---
HUB_DOWN_NOTE = (
    "Something not running? Start it again from your NAS's own Docker app - "
    "Marrquee will show it here as soon as it's back."
)
HUB_DOCKER_BANNER = (
    "Marrquee can't reach Docker right now, so it can't tell you which apps "
    "are running. The buttons below still open your apps."
)
HUB_PROXY_BANNER = (
    "You opened Marrquee at an address that isn't your NAS's own. The "
    "buttons for Marrquee's apps use that same address with each app's own "
    "port, which may not work. Your own links aren't affected."
)
HUB_STALE_NOTE = "Marrquee couldn't check just now. Reload this page to see the latest."
HUB_NOSCRIPT_NOTE = (
    "Your browser has JavaScript switched off, so this page doesn't update "
    "by itself. Reload it to see the latest."
)

# --- Live region - the one sentence a screen reader hears on every change ----
HUB_ALL_UP = "All your apps are up."


def hub_some_up(n: int, total: int) -> str:
    return f"{n} of {total} apps are up."


HUB_NOTHING_SET_UP = "No apps are set up yet."

# --- Footer -------------------------------------------------------------------
HUB_DIAGNOSTICS_LINK = "Check Marrquee's own health"


def relative_time(seconds: float) -> str:
    """How long ago `seconds` was, in the plain phrases a beginner expects.

    Boundaries are the story's own: under a minute is "just now" rather than
    "0 minutes ago", and a gap of a day or more rounds down to whole days
    instead of ever showing an hour count past 24.
    """
    if seconds < 60:
        return "just now"
    if seconds < 120:
        return "a minute ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 7200:
        return "an hour ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hours ago"
    if seconds < 172800:
        return "yesterday"
    return f"{int(seconds // 86400)} days ago"


_HUB_WORDS: tuple[str, ...] = (
    "HUB_TITLE",
    "HUB_EYEBROW",
    "HUB_HEADLINE",
    "HUB_LEDE",
    "HUB_STATUS_LABEL",
    "HUB_CHIP_UP",
    "HUB_CHIP_STARTING",
    "HUB_CHIP_DOWN",
    "HUB_CHIP_UNKNOWN",
    "hub_line_starting",
    "hub_line_down_last_seen",
    "hub_line_down",
    "hub_line_gone",
    "hub_line_unknown",
    "hub_line_no_address",
    "hub_open_app_aria",
    "HUB_DOWN_NOTE",
    "HUB_DOCKER_BANNER",
    "HUB_PROXY_BANNER",
    "HUB_STALE_NOTE",
    "HUB_NOSCRIPT_NOTE",
    "HUB_ALL_UP",
    "hub_some_up",
    "HUB_NOTHING_SET_UP",
    "HUB_DIAGNOSTICS_LINK",
    "relative_time",
)

# =============================================================================
# end Hub section
# =============================================================================

# =============================================================================
# Hub: your own links
# =============================================================================

# --- Link form refusals - one sentence per marrquee.links.LinkProblem --------
LINK_PROBLEM_LABEL_MISSING = "Give this link a name, so you know which card is which."
LINK_PROBLEM_LABEL_TOO_LONG = "Keep the name to 40 characters or fewer."
LINK_PROBLEM_URL_MISSING = "Enter the address this card should open."
LINK_PROBLEM_URL_TOO_LONG = "That address is too long."
LINK_PROBLEM_URL_NOT_WEB = "Marrquee can only open web addresses (http:// or https://)."
LINK_PROBLEM_URL_HAS_LOGIN = (
    "Leave the username and password out - the site will ask for them when you open it."
)
LINK_PROBLEM_URL_INVALID = (
    "That doesn't look like an address. Try something like 192.168.1.20:8123 "
    "or https://example.com."
)
LINK_PROBLEM_TOO_MANY = "You already have 50 links - remove one to add another."

_LINK_PROBLEM_MESSAGES: dict[str, str] = {
    "label_missing": LINK_PROBLEM_LABEL_MISSING,
    "label_too_long": LINK_PROBLEM_LABEL_TOO_LONG,
    "url_missing": LINK_PROBLEM_URL_MISSING,
    "url_too_long": LINK_PROBLEM_URL_TOO_LONG,
    "url_not_web": LINK_PROBLEM_URL_NOT_WEB,
    "url_has_login": LINK_PROBLEM_URL_HAS_LOGIN,
    "url_invalid": LINK_PROBLEM_URL_INVALID,
    "too_many": LINK_PROBLEM_TOO_MANY,
}


def link_problem_message(problem: str) -> str:
    return _LINK_PROBLEM_MESSAGES[problem]


# --- Link card status - Marrquee checks from inside its own container, so ---
# "down" here is a hint about reachability, never a verdict that the site
# itself is broken.
HUB_LINK_LINE_DOWN = "Marrquee can't reach this from the NAS - it may still work from your device."

# --- Live region - links get their own sentence, counted apart from apps ----
HUB_LINKS_ALL_UP = "All your links are up."


def hub_links_some_down(down: int, total: int) -> str:
    verb = "is" if down == 1 else "are"
    return f"{down} of {total} links {verb} down."


# --- The "+" panel - two choices, then the install pane and the link form ----
HUB_PLUS_ARIA = "Add an app or a link"
HUB_PANEL_TITLE = "Add to your Hub"
HUB_PANEL_CLOSE = "Close"
HUB_PANEL_BACK = "Back"

HUB_PANEL_CHOOSE_INSTALL_TITLE = "Install an app"
HUB_PANEL_CHOOSE_INSTALL_ONE_LINER = "Add one of Marrquee's own apps."
HUB_PANEL_CHOOSE_LINK_TITLE = "Add a link"
HUB_PANEL_CHOOSE_LINK_ONE_LINER = (
    "Add a card for another Docker app, an IP address or any web shortcut."
)

# A partial install (the wizard allows choosing only some apps) makes "you
# already have everything" untrue, so the install pane has to pick between
# this sentence and the list of what's left to add.
HUB_INSTALL_ALL_DONE = "Everything Marrquee offers is already installed."

HUB_LINK_LABEL_FIELD = "Name on the card"
HUB_LINK_LABEL_HINT = "Shown under the card's glyph, the same way the apps above it are."
HUB_LINK_URL_FIELD = "Address"
HUB_LINK_URL_HINT = "A web address, or an IP address like 192.168.1.20:8123."
HUB_LINK_ADD_SUBMIT = "Add to my Hub"
HUB_LINK_SAVE_SUBMIT = "Save changes"
HUB_LINK_REMOVE_SUBMIT = "Remove this link"
HUB_LINK_REMOVE_NOTE = (
    "Removing a link only takes the card off your Hub - it doesn't uninstall or change anything."
)
HUB_LINK_EDIT_LABEL = "Edit"


def hub_link_edit_aria(label: str) -> str:
    return f"Edit {label}"


_HUB_LINK_WORDS: tuple[str, ...] = (
    "LINK_PROBLEM_LABEL_MISSING",
    "LINK_PROBLEM_LABEL_TOO_LONG",
    "LINK_PROBLEM_URL_MISSING",
    "LINK_PROBLEM_URL_TOO_LONG",
    "LINK_PROBLEM_URL_NOT_WEB",
    "LINK_PROBLEM_URL_HAS_LOGIN",
    "LINK_PROBLEM_URL_INVALID",
    "LINK_PROBLEM_TOO_MANY",
    "link_problem_message",
    "HUB_LINK_LINE_DOWN",
    "HUB_LINKS_ALL_UP",
    "hub_links_some_down",
    "HUB_PLUS_ARIA",
    "HUB_PANEL_TITLE",
    "HUB_PANEL_CLOSE",
    "HUB_PANEL_BACK",
    "HUB_PANEL_CHOOSE_INSTALL_TITLE",
    "HUB_PANEL_CHOOSE_INSTALL_ONE_LINER",
    "HUB_PANEL_CHOOSE_LINK_TITLE",
    "HUB_PANEL_CHOOSE_LINK_ONE_LINER",
    "HUB_INSTALL_ALL_DONE",
    "HUB_LINK_LABEL_FIELD",
    "HUB_LINK_LABEL_HINT",
    "HUB_LINK_URL_FIELD",
    "HUB_LINK_URL_HINT",
    "HUB_LINK_ADD_SUBMIT",
    "HUB_LINK_SAVE_SUBMIT",
    "HUB_LINK_REMOVE_SUBMIT",
    "HUB_LINK_REMOVE_NOTE",
    "HUB_LINK_EDIT_LABEL",
    "hub_link_edit_aria",
)

# =============================================================================
# end Hub: your own links section
# =============================================================================

# =============================================================================
# Hub: install an app - the "+" panel's install pane and the wizard's
# question steps
# =============================================================================

HUB_INSTALL_LEDE = "Pick one of Marrquee's own apps to add to your Hub."
HUB_INSTALL_NEEDS_JS = (
    "Adding an app from here needs JavaScript. Switch it on, or add it from "
    "your NAS's own Docker app instead."
)


def hub_install_button(name: str) -> str:
    return f"Add {name}"


def hub_install_unavailable(reason: str) -> str:
    return f"Not available yet - {reason}"


def hub_install_busy(name: str) -> str:
    return f"Marrquee is already adding {name}. Wait for it to finish, then add another."


def hub_install_resolve_first(name: str) -> str:
    return f"{name} didn't finish installing. Sort that out first - its tile is above."


HUB_INSTALL_ALREADY = "That app is already installed."
HUB_INSTALL_NOT_READY = "Marrquee isn't ready to add an app yet. Finish setup first."
HUB_INSTALL_UNKNOWN = "Marrquee doesn't know that app."

HUB_CHIP_ADDING = "Adding…"
HUB_CHIP_ADD_FAILED = "Didn't install"


def hub_line_connecting(name: str) -> str:
    return f"Connecting {name} to your other apps…"


HUB_TRY_AGAIN = "Try again"
HUB_CANCEL_ADD = "Cancel"
HUB_CONNECT_AGAIN = "Connect again"


def hub_wiring_gap_note(name: str, failed_lines: _Sequence[str]) -> str:
    """`name` is up and reachable, but one or more of its wiring steps
    didn't finish. Names every failed line, the same "never a bare count"
    rule `wiring_finale_note` already follows, so the note stands on its
    own without a reader having to open Diagnostics first.
    """
    lines = list(failed_lines)
    if len(lines) == 1:
        return f"{name} is up, but one connection didn't finish: {lines[0]}."
    joined = ", ".join(lines)
    return f"{name} is up, but these connections didn't finish: {joined}."


def hub_cancel_failed(name: str) -> str:
    return (
        f"Marrquee couldn't remove {name}'s container. Try Cancel again, or remove it "
        "yourself from your NAS's own Docker app."
    )


def hub_announce_adding(name: str) -> str:
    return f"Adding {name}."


def hub_announce_add_failed(name: str) -> str:
    return f"{name} didn't install."


QUESTION_PICK_ONE = "Pick one of the options."
QUESTIONS_NEXT = "Next"
QUESTIONS_BACK = "Back"


def wizard_app_unavailable(name: str, reason: str) -> str:
    return f"{name} isn't available yet - {reason}"


HUB_SETUP_DONE_REFUSAL = "Marrquee is already set up. Add more apps from the + on your Hub."

_HUB_INSTALL_WORDS: tuple[str, ...] = (
    "HUB_INSTALL_LEDE",
    "HUB_INSTALL_NEEDS_JS",
    "hub_install_button",
    "hub_install_unavailable",
    "hub_install_busy",
    "hub_install_resolve_first",
    "HUB_INSTALL_ALREADY",
    "HUB_INSTALL_NOT_READY",
    "HUB_INSTALL_UNKNOWN",
    "HUB_CHIP_ADDING",
    "HUB_CHIP_ADD_FAILED",
    "hub_line_connecting",
    "HUB_TRY_AGAIN",
    "HUB_CANCEL_ADD",
    "HUB_CONNECT_AGAIN",
    "hub_wiring_gap_note",
    "hub_cancel_failed",
    "hub_announce_adding",
    "hub_announce_add_failed",
    "QUESTION_PICK_ONE",
    "QUESTIONS_NEXT",
    "QUESTIONS_BACK",
    "wizard_app_unavailable",
    "HUB_SETUP_DONE_REFUSAL",
)

# =============================================================================
# end Hub: install an app section
# =============================================================================

# The full review surface: every public name above, in one tuple. A later
# feature area adds its own fenced section above this line, then extends
# this tuple with its own `_..._WORDS` name - never editing an earlier
# section's entries.
WORDS_INVENTORY: tuple[str, ...] = (
    *_DEPLOY_ENGINE_WORDS,
    *_WIZARD_WORDS,
    *_WIRING_WORDS,
    *_DEPLOY_SCREEN_WORDS,
    *_HUB_WORDS,
    *_HUB_LINK_WORDS,
    *_HUB_INSTALL_WORDS,
)
