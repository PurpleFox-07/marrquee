"""The break-glass "run qBittorrent without a VPN" rule: the typed phrase,
the three confirmation stages, and the one file that records the owner
chose it.

Deliberately a leaf module - stdlib, `state.write_json_atomic` and `words`
only - so nothing about compose, deploy or the Hub has to be imported just
to answer "did the owner confirm this". `without_vpn_confirmed` is the one
place `build_stack_plan`'s refusal is allowed to bend for, so it never
raises: a missing, unreadable or malformed file always reads as "no", the
safe side of that one deliberate exception.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from marrquee.state import write_json_atomic
from marrquee.words import WITHOUT_VPN_PHRASE, WITHOUT_VPN_PROBLEM_MISMATCH

logger = logging.getLogger(__name__)

_FILE_NAME = "without_vpn.json"
_VERSION = 1


def _path(config_dir: Path) -> Path:
    return config_dir / _FILE_NAME


def without_vpn_confirmed(config_dir: Path) -> bool:
    """Whether the owner has confirmed running qBittorrent without a VPN.

    Never raises, and re-reads the file on every call - a "Use a VPN
    instead" undo, or a fresh confirmation, must be seen by the very next
    caller, never a cached answer. Any problem at all (a missing file, a
    directory sitting where the file should be, unreadable permissions,
    garbage JSON, the wrong version, a blank `confirmed_at`) reads as
    False - the safe side, the same side Story 5's refusal already stands
    on.
    """
    try:
        raw = _path(config_dir).read_text()
    except OSError:
        return False

    if not raw.strip():
        return False

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False

    if not isinstance(payload, dict) or payload.get("version") != _VERSION:
        return False

    confirmed_at = payload.get("confirmed_at")
    return isinstance(confirmed_at, str) and confirmed_at != ""


def save_without_vpn(config_dir: Path, *, now: datetime) -> None:
    """Record that the owner has confirmed the third, typed step.

    Raises on a genuine write failure (a full disk, a read-only mount) -
    the route calling this must learn the confirmation did NOT land,
    rather than sending the owner on as if it had.
    """
    write_json_atomic(_path(config_dir), {"version": _VERSION, "confirmed_at": now.isoformat()})


def clear_without_vpn(config_dir: Path) -> None:
    """Remove the confirmation, without ever raising.

    A missing file is already the state this is trying to reach, so that's
    not an error; any other OSError (permissions, a mid-flight NAS hiccup)
    is logged rather than raised - clearing this record is always a
    best-effort tidy-up, never something a caller must handle.
    """
    try:
        _path(config_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        logger.warning("could not clear the without-VPN confirmation: %s", error)


def _normal(text: str) -> str:
    """Fold whitespace and case, and drop one trailing full stop - nothing
    looser than that (no prefix match, no edit distance).
    """
    return " ".join(text.split()).casefold().rstrip(".")


def phrase_matches(typed: str) -> bool:
    """Whether `typed` is the owner's phrase, ignoring capitalisation,
    repeated/leading/trailing spaces and one trailing full stop.
    """
    return _normal(typed) == _normal(WITHOUT_VPN_PHRASE)


@dataclass(frozen=True)
class StageOutcome:
    """Where one posted break-glass stage lands next."""

    confirmed: bool
    stage: Literal[1, 2, 3]
    problem: str | None


def next_stage(posted_stage: str, typed: str) -> StageOutcome:
    """Walk the three break-glass stages from whatever was just posted.

    Anything that isn't `"1"`, `"2"` or `"3"` resets to stage 1 rather than
    guessing - a stray or tampered `stage` value must never be read as
    further along than the owner has actually confirmed.
    """
    if posted_stage == "1":
        return StageOutcome(confirmed=False, stage=2, problem=None)
    if posted_stage == "2":
        return StageOutcome(confirmed=False, stage=3, problem=None)
    if posted_stage == "3":
        if phrase_matches(typed):
            return StageOutcome(confirmed=True, stage=3, problem=None)
        return StageOutcome(confirmed=False, stage=3, problem=WITHOUT_VPN_PROBLEM_MISMATCH)
    return StageOutcome(confirmed=False, stage=1, problem=None)
