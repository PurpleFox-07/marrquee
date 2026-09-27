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
def app_line_getting(app_name: str) -> str:
    return f"Getting {app_name} - this only happens the first time"


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
    "and it will never move, change or delete anything you put there."
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


def failure_get_failed(name: str) -> str:
    return (
        f"Marrquee couldn't get {name}. Check that this machine can reach "
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
    "writes inside the one folder you choose, and never moves, changes or "
    "deletes anything you put there."
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
    "app_line_getting",
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
    "failure_get_failed",
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
    "It never moves, changes or deletes anything you put there. Already "
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
    "This page checks your setup's health: whether Marrquee can reach "
    "Docker and save its settings, and whether your drive can move "
    "downloads instantly. If a line isn't green, it says what to do."
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
HUB_DIAGNOSTICS_LINK = "Check your setup's health"


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

# =============================================================================
# One login - the owner's single username and password, applied to every
# app Marrquee installs (Prowlarr, Sonarr, Radarr - Plex is the exception
# and always uses its own account)
# =============================================================================

WIZARD_STEP_LOGIN = "Your login"

LOGIN_STEP_TITLE = "Choose one login for your apps"
LOGIN_STEP_LEDE = (
    "Every app Marrquee installs asks for this username and password. Your "
    "browser can remember it, so you only type it once per device."
)

LOGIN_PLEX_NOTE = "Plex is the exception: it always uses your own Plex account."

LOGIN_RESET_TITLE = "Choose a new login"
LOGIN_RESET_LEDE = (
    "Choose a new username and password. Every app Marrquee installs will switch over to it."
)

LOGIN_USERNAME_LABEL = "Username"
LOGIN_USERNAME_HINT = (
    "3 to 32 characters: letters, numbers, dots, dashes or underscores. "
    "Capitals become small letters."
)
LOGIN_PASSWORD_LABEL = "Password"
LOGIN_PASSWORD_HINT = "At least 8 characters, with no space at the start or end."
LOGIN_PASSWORD_AGAIN_LABEL = "Type it again"

LOGIN_PROBLEM_USERNAME = (
    "Usernames are 3 to 32 characters: letters, numbers, dots, dashes or underscores."
)
LOGIN_PROBLEM_PASSWORD_SHORT = "That password is too short - use at least 8 characters."
LOGIN_PROBLEM_PASSWORD_LONG = "That password is too long - use at most 128 characters."
LOGIN_PROBLEM_PASSWORD_SPACES = "Passwords can't start or end with a space."
LOGIN_PROBLEM_MISMATCH = "Those two passwords don't match."

LOGIN_SAVE_BUTTON = "Save login"

CHANGE_TITLE = "Change login"
CHANGE_LEDE = (
    "Change the username and/or password every app uses. You'll need your current password."
)
CHANGE_CURRENT_LABEL = "Current password"
CHANGE_NEW_PASSWORD_HINT = "Leave both boxes blank to keep your password."
CHANGE_PROBLEM_CURRENT_BLANK = "Enter your current password to change it."
CHANGE_PROBLEM_WRONG_CURRENT = "That's not your current password."
CHANGE_PROBLEM_NOTHING = "Change the username or password - or both - before saving."
CHANGE_SAVE_BUTTON = "Save changes"


def hub_login_summary(username: str) -> str:
    return f"Your apps' login: {username}"


HUB_LOGIN_CHANGE_LINK = "Change login"
HUB_LOGIN_FORGOT_LINK = "Forgot your password?"


def hub_login_line_putting(name: str) -> str:
    return f"Putting your login on {name}…"


def hub_login_line_restarting(name: str) -> str:
    return f"Restarting {name} with your login…"


HUB_LOGIN_APPLYING = "Putting your login on your apps…"


def hub_login_pending(names: _Sequence[str]) -> str:
    """Names every app still waiting for the saved login, the same "never a
    bare count" rule `wiring_finale_note` follows - a reader has to be able
    to act on this line without opening Diagnostics first.
    """
    lines = list(names)
    if len(lines) == 1:
        return f"{lines[0]} is still waiting for your login. Try again below."
    joined = ", ".join(lines)
    return f"{joined} are still waiting for your login. Try again below."


LOGIN_TRY_AGAIN = "Try again"

HUB_LOGIN_RESET_REMINDER = (
    "You reset your apps' login with the MARRQUEE_RESET_LOGIN line in your compose "
    "configuration. Delete that line and Redeploy once you're signed in with your "
    "new login."
)

HUB_LOGIN_BUSY = "Marrquee is already working on your login. Wait for it to finish."

HUB_INSTALL_LOGIN_FIRST = "Choose your apps' login first, before adding another app."
HUB_INSTALL_BUSY_LOGIN = (
    "Marrquee is putting your login on your apps. Wait for it to finish, then add another."
)

REFUSAL_NO_LOGIN = "Choose your apps' login before deploying."

FINALE_SIGN_IN = "Sign in with the username and password you chose."

CROSS_SITE_REFUSED = "Marrquee refused this request: it didn't come from this site."

_LOGIN_WORDS: tuple[str, ...] = (
    "WIZARD_STEP_LOGIN",
    "LOGIN_STEP_TITLE",
    "LOGIN_STEP_LEDE",
    "LOGIN_PLEX_NOTE",
    "LOGIN_RESET_TITLE",
    "LOGIN_RESET_LEDE",
    "LOGIN_USERNAME_LABEL",
    "LOGIN_USERNAME_HINT",
    "LOGIN_PASSWORD_LABEL",
    "LOGIN_PASSWORD_HINT",
    "LOGIN_PASSWORD_AGAIN_LABEL",
    "LOGIN_PROBLEM_USERNAME",
    "LOGIN_PROBLEM_PASSWORD_SHORT",
    "LOGIN_PROBLEM_PASSWORD_LONG",
    "LOGIN_PROBLEM_PASSWORD_SPACES",
    "LOGIN_PROBLEM_MISMATCH",
    "LOGIN_SAVE_BUTTON",
    "CHANGE_TITLE",
    "CHANGE_LEDE",
    "CHANGE_CURRENT_LABEL",
    "CHANGE_NEW_PASSWORD_HINT",
    "CHANGE_PROBLEM_CURRENT_BLANK",
    "CHANGE_PROBLEM_WRONG_CURRENT",
    "CHANGE_PROBLEM_NOTHING",
    "CHANGE_SAVE_BUTTON",
    "hub_login_summary",
    "HUB_LOGIN_CHANGE_LINK",
    "HUB_LOGIN_FORGOT_LINK",
    "hub_login_line_putting",
    "hub_login_line_restarting",
    "HUB_LOGIN_APPLYING",
    "hub_login_pending",
    "LOGIN_TRY_AGAIN",
    "HUB_LOGIN_RESET_REMINDER",
    "HUB_LOGIN_BUSY",
    "HUB_INSTALL_LOGIN_FIRST",
    "HUB_INSTALL_BUSY_LOGIN",
    "REFUSAL_NO_LOGIN",
    "FINALE_SIGN_IN",
    "CROSS_SITE_REFUSED",
)

# =============================================================================
# end One login section
# =============================================================================

# =============================================================================
# VPN - Gluetun's provider list, the questions it asks, and how it reports
# whether the tunnel is actually protecting a download
# =============================================================================

VPN_DESCRIPTION = (
    "Keeps your downloads private: everything your downloader sends goes "
    "through your VPN company, and nothing gets out if the VPN drops."
)

VPN_STEP_TITLE = "Your VPN"
VPN_STEP_LEDE = (
    "Choose the VPN company you already pay for, then enter its login. "
    "Marrquee proves the tunnel is really protecting your downloads before it uses it."
)

VPN_PROVIDER_LABEL = "VPN company"
VPN_PROVIDER_PLACEHOLDER = "Choose your VPN company"
VPN_PROVIDER_NEEDS_FILES = "needs extra key files Marrquee can't take yet"
VPN_GUIDE_LINK = "Where do I find these? Open the guide for your VPN company"
VPN_GUIDE_INDEX_URL = "https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers"

VPN_TYPE_LABEL = "Connection type"
VPN_TYPE_OPENVPN = "OpenVPN"
VPN_TYPE_OPENVPN_HINT = "Works with almost every VPN company."
VPN_TYPE_WIREGUARD = "WireGuard"
VPN_TYPE_WIREGUARD_HINT = "Faster, when your VPN company supports it."

VPN_OPENVPN_USER_LABEL = "OpenVPN username"
VPN_OPENVPN_USER_HINT = (
    "Most companies show a separate VPN username on their website - it's "
    "often not your account email."
)
VPN_OPENVPN_PASSWORD_LABEL = "OpenVPN password"
VPN_OPENVPN_PASSWORD_HINT = "From the same page as your username, not your account password."

VPN_WIREGUARD_KEY_LABEL = "WireGuard private key"
VPN_WIREGUARD_KEY_HINT = "A base64 key, exactly 32 bytes once decoded."
VPN_WIREGUARD_ADDRESS_LABEL = "WireGuard address"
VPN_WIREGUARD_ADDRESS_HINT = "Given by your VPN company, like 10.64.0.2/32."
VPN_WIREGUARD_PSK_LABEL = "WireGuard pre-shared key"
VPN_WIREGUARD_PSK_HINT = "Optional - only fill this in if your VPN company gave you one."

VPN_COUNTRIES_LABEL = "Server country"
VPN_COUNTRIES_HINT = "Leave empty and your VPN picks for you. Use English names, like Netherlands."

VPN_PROBLEM_PICK_PROVIDER = "Choose your VPN company before continuing."


def vpn_problem_provider_unavailable(label: str) -> str:
    return f"{label} isn't available yet - {VPN_PROVIDER_NEEDS_FILES}."


def vpn_problem_type_unsupported(label: str, type_label: str) -> str:
    return f"{label} doesn't support {type_label}. Choose the other connection type."


VPN_PROBLEM_OPENVPN_USER = "Enter your OpenVPN username."
VPN_PROBLEM_OPENVPN_PASSWORD = "Enter your OpenVPN password."
VPN_PROBLEM_WG_KEY = (
    "That WireGuard private key isn't valid - it should be a base64 key, 32 bytes once decoded."
)
VPN_PROBLEM_WG_ADDRESS = "This VPN company needs a WireGuard address, like 10.64.0.2/32."
VPN_PROBLEM_WG_PSK = (
    "That WireGuard pre-shared key isn't valid - it should be a base64 key, 32 bytes once decoded."
)
VPN_PROBLEM_TOO_LONG = "That's too long - use at most 256 characters."

VPN_LINE_CONNECTING = "Connecting to your VPN company…"


def vpn_line_protected_place(place: str) -> str:
    return f"Protected - your downloads appear to come from {place}"


VPN_LINE_PROTECTED = "Protected - your downloads go through your VPN."
VPN_LINE_TUNNEL_DOWN = (
    "Your VPN tunnel dropped. Nothing downloads until it reconnects - it keeps trying by itself."
)
VPN_LINE_NOT_SURE = "Marrquee can't tell yet whether your VPN is protecting your downloads."
VPN_NOTE_SLOW = "Still working - your VPN company can take a minute or two to connect."


def failure_vpn_refused(company: str) -> str:
    return (
        f"{company} refused your VPN login. Check the username and password you "
        "saved, then press Try again."
    )


def failure_vpn_settings_refused(company: str) -> str:
    return f"{company} refused these VPN settings. Check them, then press Try again."


def failure_vpn_not_connected(minutes: int) -> str:
    return (
        f"Your VPN never connected after {minutes} minutes. Check your VPN login "
        "and settings, then press Try again."
    )


FAILURE_VPN_NO_TUN = (
    "This machine doesn't offer a VPN tunnel device. Check that /dev/net/tun is "
    "available, then press Try again."
)

VPN_SECRETS_MOUNT_COMMENT = (
    "Your VPN login lives only in this folder, root-only on this machine. Gluetun "
    "reads it directly - it never appears in this file."
)

_VPN_WORDS: tuple[str, ...] = (
    "VPN_DESCRIPTION",
    "VPN_STEP_TITLE",
    "VPN_STEP_LEDE",
    "VPN_PROVIDER_LABEL",
    "VPN_PROVIDER_PLACEHOLDER",
    "VPN_PROVIDER_NEEDS_FILES",
    "VPN_GUIDE_LINK",
    "VPN_GUIDE_INDEX_URL",
    "VPN_TYPE_LABEL",
    "VPN_TYPE_OPENVPN",
    "VPN_TYPE_OPENVPN_HINT",
    "VPN_TYPE_WIREGUARD",
    "VPN_TYPE_WIREGUARD_HINT",
    "VPN_OPENVPN_USER_LABEL",
    "VPN_OPENVPN_USER_HINT",
    "VPN_OPENVPN_PASSWORD_LABEL",
    "VPN_OPENVPN_PASSWORD_HINT",
    "VPN_WIREGUARD_KEY_LABEL",
    "VPN_WIREGUARD_KEY_HINT",
    "VPN_WIREGUARD_ADDRESS_LABEL",
    "VPN_WIREGUARD_ADDRESS_HINT",
    "VPN_WIREGUARD_PSK_LABEL",
    "VPN_WIREGUARD_PSK_HINT",
    "VPN_COUNTRIES_LABEL",
    "VPN_COUNTRIES_HINT",
    "VPN_PROBLEM_PICK_PROVIDER",
    "vpn_problem_provider_unavailable",
    "vpn_problem_type_unsupported",
    "VPN_PROBLEM_OPENVPN_USER",
    "VPN_PROBLEM_OPENVPN_PASSWORD",
    "VPN_PROBLEM_WG_KEY",
    "VPN_PROBLEM_WG_ADDRESS",
    "VPN_PROBLEM_WG_PSK",
    "VPN_PROBLEM_TOO_LONG",
    "VPN_LINE_CONNECTING",
    "vpn_line_protected_place",
    "VPN_LINE_PROTECTED",
    "VPN_LINE_TUNNEL_DOWN",
    "VPN_LINE_NOT_SURE",
    "VPN_NOTE_SLOW",
    "failure_vpn_refused",
    "failure_vpn_settings_refused",
    "failure_vpn_not_connected",
    "FAILURE_VPN_NO_TUN",
    "VPN_SECRETS_MOUNT_COMMENT",
)

# =============================================================================
# end VPN section
# =============================================================================

# =============================================================================
# qBittorrent - the downloader's own catalog description, its one seeding
# question, and the Hub words for a paused poster and "Change seeding"
# =============================================================================

QBITTORRENT_DESCRIPTION = "Downloads what Sonarr and Radarr find - only ever through your VPN."

DOWNLOADER_COMPOSE_COMMENT = (
    "It shares your VPN's network and has no port of its own - Gluetun publishes its page instead."
)

PORT_SYNC_SCRIPT_COMMENT = (
    "Written by Marrquee. Your VPN runs this when it forwards a port, so qBittorrent listens on it."
)

SEEDING_STEP_TITLE = "How long should qBittorrent keep sharing?"
SEEDING_STEP_LEDE = (
    "Sharing a finished download is called seeding. Some sites - especially private "
    "ones - require a minimum sharing time or ratio, so check your site's rules. When "
    "it's done, Sonarr and Radarr tidy up the download, and your library copy is "
    "never touched."
)

SEEDING_LABEL = "How long to share"

SEEDING_GOOD_NEIGHBOR = "Be a good neighbor"
SEEDING_GOOD_NEIGHBOR_HINT = "Share for 7 days. A good default."
SEEDING_SAVE_SPACE = "Save my disk space"
SEEDING_SAVE_SPACE_HINT = "Stop once you've shared as much as you downloaded, or after 7 days."
SEEDING_PRIVATE = "I use private sites"
SEEDING_PRIVATE_HINT = "Share for 30 days - many private sites require it."
SEEDING_OWN = "Set my own numbers"
SEEDING_OWN_HINT = "Use this if your site has seeding rules."

SEEDING_RATIO_LABEL = "Ratio"
SEEDING_RATIO_HINT = (
    "How much to share back, compared to what you downloaded - 1.0 means share "
    "exactly as much as you took."
)
SEEDING_DAYS_LABEL = "Days to share"
SEEDING_DAYS_HINT = "How many days to keep sharing after the download finishes."

SEEDING_PROBLEM_RATIO = (
    "Enter a ratio between 0.1 and 100, with up to 2 decimal places (for example, 1.5)."
)
SEEDING_PROBLEM_DAYS = "Enter a whole number of days between 1 and 365."
SEEDING_PROBLEM_OWN_EMPTY = "Enter a ratio, a number of days, or both."

HUB_CHIP_PAUSED = "Paused"
HUB_LINE_PAUSED_FOR_VPN = "Paused - waiting for the VPN"
HUB_CHANGE_SEEDING = "Change seeding"
HUB_SEEDING_PANEL_TITLE = "Change how long qBittorrent shares"
HUB_SEEDING_SAVE = "Save seeding"
HUB_SEEDING_BUSY = (
    "Marrquee is busy with another change. Try again in a minute - nothing was changed."
)


def wiring_line_downloader_settings(name: str) -> str:
    return f"Setting up {name}"


def wiring_line_download_client(partner: str, downloader: str) -> str:
    return f"Connecting {partner} to {downloader}"


_QBIT_WORDS: tuple[str, ...] = (
    "QBITTORRENT_DESCRIPTION",
    "DOWNLOADER_COMPOSE_COMMENT",
    "PORT_SYNC_SCRIPT_COMMENT",
    "SEEDING_STEP_TITLE",
    "SEEDING_STEP_LEDE",
    "SEEDING_LABEL",
    "SEEDING_GOOD_NEIGHBOR",
    "SEEDING_GOOD_NEIGHBOR_HINT",
    "SEEDING_SAVE_SPACE",
    "SEEDING_SAVE_SPACE_HINT",
    "SEEDING_PRIVATE",
    "SEEDING_PRIVATE_HINT",
    "SEEDING_OWN",
    "SEEDING_OWN_HINT",
    "SEEDING_RATIO_LABEL",
    "SEEDING_RATIO_HINT",
    "SEEDING_DAYS_LABEL",
    "SEEDING_DAYS_HINT",
    "SEEDING_PROBLEM_RATIO",
    "SEEDING_PROBLEM_DAYS",
    "SEEDING_PROBLEM_OWN_EMPTY",
    "HUB_CHIP_PAUSED",
    "HUB_LINE_PAUSED_FOR_VPN",
    "HUB_CHANGE_SEEDING",
    "HUB_SEEDING_PANEL_TITLE",
    "HUB_SEEDING_SAVE",
    "HUB_SEEDING_BUSY",
    "wiring_line_downloader_settings",
    "wiring_line_download_client",
)

# =============================================================================
# end qBittorrent section
# =============================================================================

# =============================================================================
# VPN changes - the break-glass "run without a VPN" phrase and its three
# steps, the permanent Hub badge and "Add your VPN", and "Change VPN"
# =============================================================================

WITHOUT_VPN_PHRASE = "I understand my real address will be visible"
QUESTION_NO_VPN_LINK = "Can't use a VPN right now?"
WITHOUT_VPN_STEP1_TITLE = "Run qBittorrent without a VPN?"
WITHOUT_VPN_STEP1_BODY = (
    "Without a VPN, everyone you download from and share with can see your home's "
    "internet address, and your internet company can see what you download. Marrquee "
    "strongly recommends a VPN."
)
WITHOUT_VPN_STEP1_GO = "Continue without a VPN"
WITHOUT_VPN_STEP2_TITLE = "Are you sure?"
WITHOUT_VPN_STEP2_BODY = (
    "Some internet companies send warnings, slow down or cut off connections that "
    "share files without a VPN. You can add a VPN later from your Hub, and Marrquee "
    "will move qBittorrent behind it."
)
WITHOUT_VPN_STEP2_GO = "I'm sure"
WITHOUT_VPN_STEP3_TITLE = "Type this to confirm"
WITHOUT_VPN_STEP3_BODY = "Type the sentence below exactly, then press Run without a VPN."
WITHOUT_VPN_TYPED_LABEL = "The sentence"
WITHOUT_VPN_STEP3_GO = "Run without a VPN"
WITHOUT_VPN_USE_VPN = "Use a VPN instead"
WITHOUT_VPN_PROBLEM_MISMATCH = (
    "That doesn't match. Type the sentence exactly as shown - or choose Use a VPN instead."
)
WITHOUT_VPN_ROW_NOTE = "You chose to run qBittorrent without a VPN."
WITHOUT_VPN_UNDO = "Use a VPN after all"
QBITTORRENT_DESCRIPTION_NO_VPN = (
    "Downloads what Sonarr and Radarr find. Running without a VPN - add one from your Hub."
)
DOWNLOADER_NO_VPN_COMPOSE_COMMENT = (
    "Running WITHOUT a VPN, as you confirmed on your Hub. Add your VPN there to move it behind one."
)
HUB_NO_VPN_BADGE = "Running without VPN"
HUB_NO_VPN_BADGE_ACTION = "Add your VPN"
HUB_CHANGE_VPN = "Change VPN"
HUB_VPN_ADD_TITLE = "Add your VPN"
HUB_VPN_ADD_LEDE = (
    "Marrquee connects to your VPN, proves it works, then moves qBittorrent behind it. "
    "Downloads pause for a minute or two while it moves."
)
HUB_VPN_ADD_SUBMIT = "Connect this VPN"
HUB_VPN_CHANGE_TITLE = "Change VPN"
HUB_VPN_CHANGE_LEDE = (
    "These settings replace your current VPN. Type your VPN username again; leave a "
    "password box empty to keep the one you saved. Downloads pause until the new VPN is "
    "proven."
)
HUB_VPN_CHANGE_SUBMIT = "Save and reconnect"
HUB_VPN_BUSY = "Marrquee is busy with another change. Try again in a minute - nothing was changed."
HUB_CHIP_VPN_CHANGING = "Changing…"
HUB_CHIP_VPN_CHANGE_FAILED = "Didn't connect"
HUB_KEEP_WITHOUT_VPN = "Keep running without VPN"
HUB_LINE_RESTARTING_WITHOUT_VPN = "Starting qBittorrent again without a VPN…"


_VPN_CHANGE_WORDS: tuple[str, ...] = (
    "WITHOUT_VPN_PHRASE",
    "QUESTION_NO_VPN_LINK",
    "WITHOUT_VPN_STEP1_TITLE",
    "WITHOUT_VPN_STEP1_BODY",
    "WITHOUT_VPN_STEP1_GO",
    "WITHOUT_VPN_STEP2_TITLE",
    "WITHOUT_VPN_STEP2_BODY",
    "WITHOUT_VPN_STEP2_GO",
    "WITHOUT_VPN_STEP3_TITLE",
    "WITHOUT_VPN_STEP3_BODY",
    "WITHOUT_VPN_TYPED_LABEL",
    "WITHOUT_VPN_STEP3_GO",
    "WITHOUT_VPN_USE_VPN",
    "WITHOUT_VPN_PROBLEM_MISMATCH",
    "WITHOUT_VPN_ROW_NOTE",
    "WITHOUT_VPN_UNDO",
    "QBITTORRENT_DESCRIPTION_NO_VPN",
    "DOWNLOADER_NO_VPN_COMPOSE_COMMENT",
    "HUB_NO_VPN_BADGE",
    "HUB_NO_VPN_BADGE_ACTION",
    "HUB_CHANGE_VPN",
    "HUB_VPN_ADD_TITLE",
    "HUB_VPN_ADD_LEDE",
    "HUB_VPN_ADD_SUBMIT",
    "HUB_VPN_CHANGE_TITLE",
    "HUB_VPN_CHANGE_LEDE",
    "HUB_VPN_CHANGE_SUBMIT",
    "HUB_VPN_BUSY",
    "HUB_CHIP_VPN_CHANGING",
    "HUB_CHIP_VPN_CHANGE_FAILED",
    "HUB_KEEP_WITHOUT_VPN",
    "HUB_LINE_RESTARTING_WITHOUT_VPN",
)

# =============================================================================
# end VPN changes section
# =============================================================================

# =============================================================================
# Your drive - proving a finished download can become a library file
# without using the space twice (a hard link), in the owner's own words
# =============================================================================

LINK_TEST_FILE_TEXT = (
    "Marrquee made this file to check that your drive can move downloads "
    "instantly. It deletes it straight away - if you can see it, you can delete it."
)

# --- Diagnostics' "Your drive" section - one row per outcome, in plain words -

DRIVE_SECTION_TITLE = "Your drive"
DRIVE_WORKS_TITLE = "Downloads move into your library instantly"
DRIVE_WORKS_DETAIL = (
    "Marrquee just tested your drive: a finished download becomes a "
    "library file without using any extra space."
)
DRIVE_NOT_NEEDED_TITLE = "Not needed yet"
DRIVE_NOT_NEEDED_DETAIL = (
    "Marrquee tests this once you have qBittorrent and Sonarr or Radarr - "
    "they're the apps that move finished downloads into your library."
)
DRIVE_STILL_CHECKING_TITLE = "Still checking your drive"
DRIVE_STILL_CHECKING_DETAIL = "Your drive may be waking up. Choose Check again in a minute."
DRIVE_COPIES_TITLE = "Downloads are being copied, not moved"
DRIVE_COULDNT_CHECK_TITLE = "Marrquee couldn't test your drive"


def drive_reason_different_drives(folder: str) -> str:
    return (
        f"{folder} is on a different drive or share from your downloads, so "
        "every finished download is copied - using double the space."
    )


def drive_reason_no_hard_links(folder: str) -> str:
    return (
        f"The drive holding {folder} can't make instant links (some network "
        "shares and USB drives can't), so every finished download is copied."
    )


def drive_reason_not_allowed(folder: str) -> str:
    return f"Marrquee wasn't allowed to test {folder}."


def drive_reason_drive_full(folder: str) -> str:
    return f"The drive holding {folder} is full, so Marrquee couldn't finish testing it."


def drive_reason_folder_missing(folder: str) -> str:
    return f"{folder} doesn't exist yet, so Marrquee couldn't test it."


def drive_reason_folder_elsewhere(folder: str) -> str:
    return (
        f"{folder} is a shortcut to somewhere else, so Marrquee won't test "
        "it - and your apps can't follow it either."
    )


def drive_reason_unexpected(folder: str) -> str:
    return f"Marrquee couldn't test {folder} for an unexpected reason."


DRIVE_TODO_SAME_DRIVE = (
    "Keep your downloads and your library inside the one folder you chose "
    "for Marrquee, on the same drive. Don't mount a separate drive or share "
    "inside it."
)
DRIVE_TODO_NATIVE_DRIVE = (
    "Move your big drive folder onto a drive that supports instant links - "
    "most internal NAS drives do; some network shares and USB drives don't."
)
DRIVE_TODO_PERMISSIONS = (
    "Check that the folder Marrquee uses is owned by the account you "
    "installed Marrquee with, then choose Check again."
)
DRIVE_TODO_FREE_SPACE = "Free up some space on that drive, then choose Check again."
DRIVE_TODO_REBUILD_FOLDER = (
    "Redeploy from the setup wizard so Marrquee can rebuild the missing "
    "folder, then choose Check again."
)
DRIVE_TODO_REAL_FOLDER = "Point Marrquee at the real folder instead of a shortcut to it."
DRIVE_TODO_ASK_FOR_HELP = (
    "Choose Check again - if it keeps happening, the project page has a place to report it."
)


def drive_technical(text: str) -> str:
    return f"Technical detail: {text}"


# --- The Hub's own amber note - the same words the poll and the page both
# draw, since both read them from `hub_view` through the one builder,
# `read_hub_view` -------------------------------------------------------------

HUB_DRIVE_NOTE_COPIES = "Downloads are being copied, not moved - using double space"
HUB_DRIVE_NOTE_UNCHECKED = "Marrquee couldn't test your drive - see why"


_DRIVE_WORDS: tuple[str, ...] = (
    "LINK_TEST_FILE_TEXT",
    "DRIVE_SECTION_TITLE",
    "DRIVE_WORKS_TITLE",
    "DRIVE_WORKS_DETAIL",
    "DRIVE_NOT_NEEDED_TITLE",
    "DRIVE_NOT_NEEDED_DETAIL",
    "DRIVE_STILL_CHECKING_TITLE",
    "DRIVE_STILL_CHECKING_DETAIL",
    "DRIVE_COPIES_TITLE",
    "DRIVE_COULDNT_CHECK_TITLE",
    "drive_reason_different_drives",
    "drive_reason_no_hard_links",
    "drive_reason_not_allowed",
    "drive_reason_drive_full",
    "drive_reason_folder_missing",
    "drive_reason_folder_elsewhere",
    "drive_reason_unexpected",
    "DRIVE_TODO_SAME_DRIVE",
    "DRIVE_TODO_NATIVE_DRIVE",
    "DRIVE_TODO_PERMISSIONS",
    "DRIVE_TODO_FREE_SPACE",
    "DRIVE_TODO_REBUILD_FOLDER",
    "DRIVE_TODO_REAL_FOLDER",
    "DRIVE_TODO_ASK_FOR_HELP",
    "drive_technical",
    "HUB_DRIVE_NOTE_COPIES",
    "HUB_DRIVE_NOTE_UNCHECKED",
)

# =============================================================================
# end Your drive section
# =============================================================================

# =============================================================================
# Recyclarr - keeping Sonarr and Radarr's quality settings matched to the
# TRaSH guides, in the owner's own words
# =============================================================================

RECYCLARR_DESCRIPTION = (
    "Keeps Sonarr and Radarr's quality settings matching the TRaSH guides - "
    "the settings experienced users rely on - and checks for updates every day."
)
RECYCLARR_NEEDS_ARR = "needs Sonarr or Radarr"

QUALITY_TV_STEP_TITLE = "Quality for TV shows"
QUALITY_MOVIE_STEP_TITLE = "Quality for movies"
QUALITY_TV_STEP_LEDE = (
    "Recyclarr adds a quality profile to Sonarr that follows the TRaSH guides, "
    "and keeps it up to date every day. Shows you already have are left alone - "
    "choose the new profile when you add a show. If you change Sonarr's "
    "quality settings by hand, Recyclarr puts them back on its next sync."
)
QUALITY_MOVIE_STEP_LEDE = (
    "Recyclarr adds a quality profile to Radarr that follows the TRaSH guides, "
    "and keeps it up to date every day. Movies you already have are left alone - "
    "choose the new profile when you add a movie. If you change Radarr's "
    "quality settings by hand, Recyclarr puts them back on its next sync."
)
QUALITY_LABEL = "Quality"
QUALITY_1080P = "1080p (Full HD)"
QUALITY_4K = "4K (Ultra HD)"
QUALITY_TV_1080P_HINT = "Looks great on most TVs. About 1-4 GB per episode."
QUALITY_TV_4K_HINT = "For a 4K TV and plenty of space. About 5-20 GB per episode."
QUALITY_MOVIE_1080P_HINT = "Looks great on most TVs. About 5-15 GB per movie."
QUALITY_MOVIE_4K_HINT = "For a 4K TV and plenty of space. About 20-60 GB per movie."
RECYCLARR_CONFIG_HEADER_COMMENT = (
    "Written by Marrquee for Recyclarr. Marrquee rewrites this file every time "
    "it starts a sync, so edits made here don't last."
)
RECYCLARR_COMPOSE_COMMENT = (
    "Recyclarr has no web page. It reads recyclarr.yml in this app's folder, "
    "which Marrquee writes, and syncs once a day by itself."
)

RECYCLARR_LINE_SYNCING = "Syncing quality settings…"
RECYCLARR_LINE_NEVER = "Not synced yet - press Sync now."
RECYCLARR_LINE_COULDNT_START = "Marrquee couldn't start a sync. Press Sync now to try again."
RECYCLARR_CHIP_NEEDS_LOOK = "Needs a look"
HUB_SYNC_NOW = "Sync now"


def recyclarr_line_last_synced(when: str) -> str:
    return f"Last synced {when}"


def recyclarr_line_late(when: str) -> str:
    return f"Last synced {when} - it should sync every day. Press Sync now."


def recyclarr_line_failed(when: str) -> str:
    return f"The last sync, {when}, didn't work. Press Sync now to try again."


def recyclarr_line_app_down(name: str) -> str:
    return f"Couldn't update {name} because it's down. Start {name} again, then press Sync now."


_RECYCLARR_WORDS: tuple[str, ...] = (
    "RECYCLARR_DESCRIPTION",
    "RECYCLARR_NEEDS_ARR",
    "QUALITY_TV_STEP_TITLE",
    "QUALITY_MOVIE_STEP_TITLE",
    "QUALITY_TV_STEP_LEDE",
    "QUALITY_MOVIE_STEP_LEDE",
    "QUALITY_LABEL",
    "QUALITY_1080P",
    "QUALITY_4K",
    "QUALITY_TV_1080P_HINT",
    "QUALITY_TV_4K_HINT",
    "QUALITY_MOVIE_1080P_HINT",
    "QUALITY_MOVIE_4K_HINT",
    "RECYCLARR_CONFIG_HEADER_COMMENT",
    "RECYCLARR_COMPOSE_COMMENT",
    "RECYCLARR_LINE_SYNCING",
    "RECYCLARR_LINE_NEVER",
    "RECYCLARR_LINE_COULDNT_START",
    "RECYCLARR_CHIP_NEEDS_LOOK",
    "HUB_SYNC_NOW",
    "recyclarr_line_last_synced",
    "recyclarr_line_late",
    "recyclarr_line_failed",
    "recyclarr_line_app_down",
)

# =============================================================================
# end Recyclarr section
# =============================================================================

# =============================================================================
# Plex - a media server linked to the owner's own Plex account, in the
# owner's own words. The Alignment rule: never call Plex a "media server" on
# its own - that phrase stays reserved for the whole setup.
# =============================================================================

PLEX_DESCRIPTION = (
    "Streams your movies and shows to your TV, phone and browser, signed in "
    "with your own Plex account."
)
PLEX_EXCLUDES_JELLYFIN = (
    "you already have Jellyfin - you can have Plex or Jellyfin, and for now the choice stays"
)

PLEX_STEP_TITLE = "Sign in with Plex"
PLEX_STEP_LEDE = (
    "Marrquee sets up a new Plex server on this NAS and links it to your Plex "
    "account: press Sign in with Plex, approve Marrquee on plex.tv, and you "
    "come straight back here. Plex is set to play your files exactly as they "
    "are - it never re-encodes video. One setting Marrquee can't reach: each "
    "Plex app on your TV, phone or computer has its own Quality setting. Set "
    "it to Original (or Maximum), or that device may not be able to play a "
    "video Plex won't shrink for it."
)
PLEX_ACCOUNT_LABEL = "Your Plex account"
PLEX_SIGN_IN_BUTTON = "Sign in with Plex"
PLEX_SIGN_IN_OTHER = "Use a different Plex account"


def plex_signed_in_as(name: str) -> str:
    return f"Signed in to Plex as {name}."


PLEX_SIGN_IN_HINT = "Opens plex.tv. Approve Marrquee there and you come straight back."
PLEX_PROBLEM_SIGN_IN_FIRST = (
    "Press Sign in with Plex first - Marrquee needs your Plex account to set up your server."
)
PLEX_SIGN_IN_DIDNT_FINISH = "Plex didn't confirm the sign-in. Press Sign in with Plex to try again."

PLEX_COMPOSE_COMMENT = (
    "Plex uses your NAS's own network (host networking) so your TVs and phones find "
    "it at home. The first time it starts, Marrquee hands it a one-time code that "
    "links it to your Plex account, then deletes the code."
)
MEDIA_LIBRARY_MOUNT_COMMENT = (
    "Your Movies and TV folders, read-only: Plex can show and play everything, but "
    "can never change or delete your files."
)
PLEX_SECRETS_MOUNT_COMMENT = (
    "A folder only Marrquee can read. It holds Plex's one-time link code for a few "
    "minutes during the first start, and is empty the rest of the time."
)

FAILURE_PLEX_SIGN_IN_NEEDED = (
    "plex.tv didn't accept Marrquee's sign-in for your account. Press Cancel, add "
    "Plex again and sign in with Plex."
)
FAILURE_PLEX_NOT_CLAIMED = (
    "Your new Plex server started, but plex.tv didn't link it to your account in "
    "time. Press Try again - Marrquee asks plex.tv for a fresh link."
)
FAILURE_PLEX_PORT_TAKEN = (
    "Something on your NAS already answers on Plex's port, 32400 - usually a Plex "
    "you installed before. Stop it from your NAS's app list, then press Try again."
)


def wiring_line_libraries(name: str) -> str:
    return f"Adding your Movies and TV Shows to {name}"


PLEX_LIBRARY_MOVIES = "Movies"
PLEX_LIBRARY_TV = "TV Shows"

WIRING_LINE_PLEX_DIRECT_PLAY = "Setting Plex to play files as they are"
PLEX_NOTE_DIRECT_PLAY = (
    "Plex now never re-encodes video. Set Quality to Original in each Plex app on your devices."
)
WIRING_PLEX_SIGN_IN_NEEDED = (
    "Marrquee has no Plex sign-in saved. Add Plex again and sign in with Plex."
)


_PLEX_WORDS: tuple[str, ...] = (
    "PLEX_DESCRIPTION",
    "PLEX_EXCLUDES_JELLYFIN",
    "PLEX_STEP_TITLE",
    "PLEX_STEP_LEDE",
    "PLEX_ACCOUNT_LABEL",
    "PLEX_SIGN_IN_BUTTON",
    "PLEX_SIGN_IN_OTHER",
    "plex_signed_in_as",
    "PLEX_SIGN_IN_HINT",
    "PLEX_PROBLEM_SIGN_IN_FIRST",
    "PLEX_SIGN_IN_DIDNT_FINISH",
    "PLEX_COMPOSE_COMMENT",
    "MEDIA_LIBRARY_MOUNT_COMMENT",
    "PLEX_SECRETS_MOUNT_COMMENT",
    "FAILURE_PLEX_SIGN_IN_NEEDED",
    "FAILURE_PLEX_NOT_CLAIMED",
    "FAILURE_PLEX_PORT_TAKEN",
    "wiring_line_libraries",
    "PLEX_LIBRARY_MOVIES",
    "PLEX_LIBRARY_TV",
    "WIRING_LINE_PLEX_DIRECT_PLAY",
    "PLEX_NOTE_DIRECT_PLAY",
    "WIRING_PLEX_SIGN_IN_NEEDED",
)

# =============================================================================
# end Plex section
# =============================================================================

# =============================================================================
# Jellyfin - a media server signed in with the owner's one Marrquee login,
# in the owner's own words. The same Alignment rule as Plex's section: never
# call Jellyfin a "media server" on its own - that phrase stays reserved for
# the whole setup.
# =============================================================================

JELLYFIN_DESCRIPTION = (
    "Streams your movies and shows to your TV, phone and browser. Free and "
    "open source - you sign in with your Marrquee login."
)
JELLYFIN_EXCLUDES_PLEX = (
    "you already have Plex - you can have Plex or Jellyfin, and for now the choice stays"
)

JELLYFIN_GRAPHICS_STEP_TITLE = "Use your NAS's graphics chip?"
JELLYFIN_GRAPHICS_STEP_LEDE = (
    "Your NAS has a graphics chip. When a TV or phone can't play a video as "
    "it is, Jellyfin converts it while you watch. The graphics chip does "
    "that quickly and quietly. Without it, your NAS's main processor does "
    "the work, which is slower and can stutter on big 4K files. For now, "
    "this choice stays once Jellyfin is added."
)
JELLYFIN_GRAPHICS_LABEL = "Graphics chip"
JELLYFIN_GRAPHICS_YES = "Yes, use the graphics chip"
JELLYFIN_GRAPHICS_YES_HINT = "Faster video conversion, and less work for your NAS."
JELLYFIN_GRAPHICS_NO = "No, don't use it"
JELLYFIN_GRAPHICS_NO_HINT = "Jellyfin still plays everything - it just converts video more slowly."

JELLYFIN_SERVER_NAME = "Marrquee"

JELLYFIN_COMPOSE_COMMENT = (
    "Jellyfin uses your NAS's own network (host networking) so your TVs and phones find "
    "it at home. Marrquee finished Jellyfin's first-time setup for you: its admin is your "
    "Marrquee login."
)
JELLYFIN_MEDIA_LIBRARY_MOUNT_COMMENT = (
    "Your Movies and TV folders, read-only: Jellyfin can show and play everything, but "
    "can never change or delete your files."
)
JELLYFIN_GRAPHICS_DEVICE_COMMENT = (
    "Your NAS's graphics chip, passed to Jellyfin because you chose to use it for converting video."
)

FAILURE_JELLYFIN_NOT_OURS = (
    "Jellyfin is already set up with a different admin - its settings folder, "
    "marrquee/apps/jellyfin on your drive, is left over from an earlier Jellyfin. Sign in "
    "to that Jellyfin as its admin and change the admin's name and password to your "
    "Marrquee login, then press Try again."
)
FAILURE_JELLYFIN_SETUP_REFUSED = (
    "Jellyfin started, but refused Marrquee's first-time setup. Press Try again - if it "
    "happens again, the details are on the Diagnostics page."
)
FAILURE_JELLYFIN_PORT_TAKEN = (
    "Something on your NAS already answers on Jellyfin's port, 8096 - usually a Jellyfin "
    "you installed before. Stop it from your NAS's app list, then press Try again."
)

JELLYFIN_LIBRARY_MOVIES = "Movies"
JELLYFIN_LIBRARY_TV = "TV Shows"
WIRING_LINE_JELLYFIN_GRAPHICS = "Setting Jellyfin to use your graphics chip"
JELLYFIN_NOTE_GRAPHICS = "Jellyfin now uses your NAS's graphics chip to convert video."
WIRING_JELLYFIN_NOT_SET_UP = (
    "Marrquee has no key for Jellyfin yet. Press Connect again after Jellyfin finishes starting."
)


_JELLYFIN_WORDS: tuple[str, ...] = (
    "JELLYFIN_DESCRIPTION",
    "JELLYFIN_EXCLUDES_PLEX",
    "JELLYFIN_GRAPHICS_STEP_TITLE",
    "JELLYFIN_GRAPHICS_STEP_LEDE",
    "JELLYFIN_GRAPHICS_LABEL",
    "JELLYFIN_GRAPHICS_YES",
    "JELLYFIN_GRAPHICS_YES_HINT",
    "JELLYFIN_GRAPHICS_NO",
    "JELLYFIN_GRAPHICS_NO_HINT",
    "JELLYFIN_SERVER_NAME",
    "JELLYFIN_COMPOSE_COMMENT",
    "JELLYFIN_MEDIA_LIBRARY_MOUNT_COMMENT",
    "JELLYFIN_GRAPHICS_DEVICE_COMMENT",
    "FAILURE_JELLYFIN_NOT_OURS",
    "FAILURE_JELLYFIN_SETUP_REFUSED",
    "FAILURE_JELLYFIN_PORT_TAKEN",
    "JELLYFIN_LIBRARY_MOVIES",
    "JELLYFIN_LIBRARY_TV",
    "WIRING_LINE_JELLYFIN_GRAPHICS",
    "JELLYFIN_NOTE_GRAPHICS",
    "WIRING_JELLYFIN_NOT_SET_UP",
)

# =============================================================================
# end Jellyfin section
# =============================================================================

# =============================================================================
# Your own Plex - the owner's existing Plex, connected but never deployed. The
# same Alignment rule as Plex's and Jellyfin's sections: never call it a
# "media server" on its own - that phrase stays reserved for the whole setup.
# =============================================================================

EXISTING_PLEX_DESCRIPTION = "The Plex you already had, connected with your Plex account."


def existing_plex_description(name: str) -> str:
    return f"Your own Plex, {name}."


EXISTING_PLEX_EXCLUDES_PLEX = (
    "Marrquee already set up a new Plex for you - you can have one Plex or "
    "Jellyfin, and for now the choice stays"
)
EXCLUDED_BY_EXISTING_PLEX = (
    "you've connected your own Plex - you can have Plex or Jellyfin, and for now the choice stays"
)

FAILURE_EXISTING_PLEX_UNREACHABLE = (
    "Marrquee couldn't reach your Plex at the address it found. Check that Plex is "
    "running, then press Try again - or Cancel and connect it again from the +."
)
EXISTING_PLEX_DISCONNECT_BUSY = "Marrquee is busy right now. Try again when it's finished."


def existing_plex_disconnect_needed(name: str) -> str:
    return f"{name} uses your Plex. Plex has to stay connected while {name} is installed."


EXISTING_PLEX_LIBRARY_MOVIES = "Movies (Marrquee)"
EXISTING_PLEX_LIBRARY_TV = "TV Shows (Marrquee)"
EXISTING_PLEX_NOTE_ADDED = (
    "Your Plex now has Movies (Marrquee) and TV Shows (Marrquee). Your other "
    "libraries weren't touched."
)
EXISTING_PLEX_NOTE_CANT_SEE = (
    "Your Plex can't see Marrquee's folders yet, so no libraries were added."
)
EXISTING_PLEX_TOKEN_REFUSED = (
    "Your Plex didn't accept Marrquee's sign-in. Disconnect it and connect it again from the +."
)
WIRING_EXISTING_PLEX_MISSING = (
    "Marrquee lost the details of your Plex. Disconnect it and connect it again from the +."
)
EXISTING_PLEX_LINE_DOWN = (
    "Marrquee can't reach your Plex from the NAS - it may still work from your device."
)

EXISTING_PLEX_ROW_TITLE = "Connect a Plex you already have"
EXISTING_PLEX_SIGN_IN_HINT = (
    "Opens plex.tv. Approve Marrquee there, then pick your Plex server - no addresses to type."
)
EXISTING_PLEX_PICK_TITLE = "Pick your Plex server"
EXISTING_PLEX_PICK_LEDE = (
    "These are the Plex servers on your Plex account. Marrquee adds Movies (Marrquee) and TV "
    "Shows (Marrquee) to the one you pick and never changes your other libraries."
)
EXISTING_PLEX_CONNECT_BUTTON = "Connect"
EXISTING_PLEX_OFFLINE_HINT = "Plex says this server is offline right now."


def existing_plex_replace_link(label: str) -> str:
    return f"Replace my '{label}' link card with this Plex"


def existing_plex_unreachable(name: str) -> str:
    return (
        f"Marrquee couldn't reach '{name}' from your NAS. Make sure it's switched on and on "
        "the same network, then try again."
    )


EXISTING_PLEX_NO_SERVERS = (
    "Your Plex account has no Plex server of its own yet. Sign in with the account that owns "
    "your server."
)
EXISTING_PLEX_LIST_FAILED = (
    "Marrquee couldn't get your server list from plex.tv. Try again in a minute."
)
EXISTING_PLEX_SIGN_IN_AGAIN = "Plex needs you to sign in again."
EXISTING_PLEX_MANAGE = "Manage"
EXISTING_PLEX_MANAGE_ARIA = "Manage your Plex connection"
EXISTING_PLEX_FOLDER_OK = "Movies (Marrquee) and TV Shows (Marrquee) are in your Plex."


def existing_plex_cant_see_help(path: str) -> str:
    return (
        f"Your Plex can't open {path}, so Marrquee didn't add its libraries. If Plex runs in "
        f"your NAS's Docker app, give its container the folder {path} and set the path inside "
        f"the container to the same {path}, restart Plex, then press Check again. If Plex runs "
        "on another computer, it can't see these folders - you can add them yourself from a "
        "network share."
    )


EXISTING_PLEX_CHECK_AGAIN = "Check again"
EXISTING_PLEX_DISCONNECT = "Disconnect"
EXISTING_PLEX_DISCONNECT_NOTE = (
    "Disconnecting only takes Plex off your Hub. Your Plex and every library in it, including "
    "Marrquee's, stay exactly as they are."
)


_EXISTING_PLEX_WORDS: tuple[str, ...] = (
    "EXISTING_PLEX_DESCRIPTION",
    "existing_plex_description",
    "EXISTING_PLEX_EXCLUDES_PLEX",
    "EXCLUDED_BY_EXISTING_PLEX",
    "FAILURE_EXISTING_PLEX_UNREACHABLE",
    "EXISTING_PLEX_DISCONNECT_BUSY",
    "existing_plex_disconnect_needed",
    "EXISTING_PLEX_LIBRARY_MOVIES",
    "EXISTING_PLEX_LIBRARY_TV",
    "EXISTING_PLEX_NOTE_ADDED",
    "EXISTING_PLEX_NOTE_CANT_SEE",
    "EXISTING_PLEX_TOKEN_REFUSED",
    "WIRING_EXISTING_PLEX_MISSING",
    "EXISTING_PLEX_LINE_DOWN",
    "EXISTING_PLEX_ROW_TITLE",
    "EXISTING_PLEX_SIGN_IN_HINT",
    "EXISTING_PLEX_PICK_TITLE",
    "EXISTING_PLEX_PICK_LEDE",
    "EXISTING_PLEX_CONNECT_BUTTON",
    "EXISTING_PLEX_OFFLINE_HINT",
    "existing_plex_replace_link",
    "existing_plex_unreachable",
    "EXISTING_PLEX_NO_SERVERS",
    "EXISTING_PLEX_LIST_FAILED",
    "EXISTING_PLEX_SIGN_IN_AGAIN",
    "EXISTING_PLEX_MANAGE",
    "EXISTING_PLEX_MANAGE_ARIA",
    "EXISTING_PLEX_FOLDER_OK",
    "existing_plex_cant_see_help",
    "EXISTING_PLEX_CHECK_AGAIN",
    "EXISTING_PLEX_DISCONNECT",
    "EXISTING_PLEX_DISCONNECT_NOTE",
)

# =============================================================================
# end Your own Plex section
# =============================================================================

# =============================================================================
# Seerr - asking for movies and shows. The same Alignment rule as Plex's,
# Jellyfin's and Your own Plex's sections: Plex and Jellyfin are never called
# a "media server".
# =============================================================================

SEERR_DESCRIPTION = (
    "Lets you - and the people you share Plex or Jellyfin with - ask for movies and shows. "
    "Your requests start straight away; theirs wait for you to approve them."
)
SEERR_NEEDS_PLEX_OR_JELLYFIN = "needs Plex or Jellyfin first"
SEERR_NEEDS_ARR = "needs Sonarr or Radarr first"
SEERR_COMPOSE_COMMENT = (
    "Marrquee finished Seerr's first-time setup and connected it to Sonarr, Radarr and your "
    "Plex or Jellyfin. You sign in to Seerr with Plex or Jellyfin - it has no separate password."
)
SEERR_CONFIG_MOUNT_COMMENT = (
    "Seerr's settings and your requests. Seerr runs as its own user, number 1000, so Marrquee "
    "gives this folder to that user."
)
FAILURE_SEERR_NOT_OURS = (
    "Seerr is already set up for a different Plex or Jellyfin - its settings folder, "
    "marrquee/apps/seerr on your drive, is left over from an earlier Seerr. Rename that "
    "folder in your NAS's file manager, then press Try again."
)
FAILURE_SEERR_SETUP_REFUSED = (
    "Seerr started, but refused Marrquee's first-time setup. Press Try again - if it "
    "happens again, the details are on the Diagnostics page."
)


def wiring_line_seerr(name: str) -> str:
    return f"Connecting Seerr to {name}"


def seerr_note_libraries(name: str) -> str:
    return f"Seerr now sees what's in your {name}, so it shows what you already have."


def seerr_note_no_libraries(name: str) -> str:
    return (
        f"{name} has no Movies or TV Shows library yet, so Seerr can't tell what you already have."
    )


def seerr_note_profile(arr: str, profile: str) -> str:
    return f'New requests go to {arr} with the "{profile}" quality profile.'


def seerr_failure_cant_reach(name: str) -> str:
    return (
        f"Seerr couldn't reach {name} to finish connecting. Your apps are running fine - "
        f"check that {name} is running, then try again."
    )


def seerr_failure_no_folder(name: str) -> str:
    return (
        f"{name} doesn't have its library folder yet, so Seerr can't send it requests. "
        f"Connect {name} again first, then Seerr."
    )


def seerr_failure_no_profiles(name: str) -> str:
    return f"{name} has no quality profiles, so Seerr can't send it requests."


SEERR_FAILURE_WRONG_PLEX = (
    "Seerr reached a different Plex than the one you connected. Press Connect again."
)


_SEERR_WORDS: tuple[str, ...] = (
    "SEERR_DESCRIPTION",
    "SEERR_NEEDS_PLEX_OR_JELLYFIN",
    "SEERR_NEEDS_ARR",
    "SEERR_COMPOSE_COMMENT",
    "SEERR_CONFIG_MOUNT_COMMENT",
    "FAILURE_SEERR_NOT_OURS",
    "FAILURE_SEERR_SETUP_REFUSED",
    "wiring_line_seerr",
    "seerr_note_libraries",
    "seerr_note_no_libraries",
    "seerr_note_profile",
    "seerr_failure_cant_reach",
    "seerr_failure_no_folder",
    "seerr_failure_no_profiles",
    "SEERR_FAILURE_WRONG_PLEX",
)

# =============================================================================
# end Seerr section
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
    *_LOGIN_WORDS,
    *_VPN_WORDS,
    *_QBIT_WORDS,
    *_VPN_CHANGE_WORDS,
    *_DRIVE_WORDS,
    *_RECYCLARR_WORDS,
    *_PLEX_WORDS,
    *_JELLYFIN_WORDS,
    *_EXISTING_PLEX_WORDS,
    *_SEERR_WORDS,
)
