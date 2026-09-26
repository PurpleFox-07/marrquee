"""qBittorrent's seeding rules: what a valid saved answer looks like, and
what qBittorrent's own preference keys it turns into.

This is a leaf, like `vpn.py`: it imports only the standard library and
`words`, so nothing about the question-answering machinery (`questions.py`)
can ever leak back into what a seeding answer *is*, and `questions.py` can
import this module with no cycle. The adapter that turns a `SeedingCheck`
into a `QuestionCheck`, and the `QuestionStep` that asks for it, both live
in `questions.py` instead - the same split `vpn.py` already uses for the
VPN step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final, Literal

from marrquee.words import (
    QUESTION_PICK_ONE,
    SEEDING_PROBLEM_DAYS,
    SEEDING_PROBLEM_OWN_EMPTY,
    SEEDING_PROBLEM_RATIO,
)

SeedingPreset = Literal["good_neighbor", "save_space", "private", "own"]

_PRESET_VALUES: Final[frozenset[str]] = frozenset({"good_neighbor", "save_space", "private", "own"})

_RATIO_MIN: Final = Decimal("0.1")
_RATIO_MAX: Final = Decimal("100")
_DAYS_MIN: Final = 1
_DAYS_MAX: Final = 365
_MINUTES_PER_DAY: Final = 1440


@dataclass(frozen=True)
class SeedingCheck:
    """The outcome of checking one posted seeding answer.

    `answers` always carries all three field names (`seeding`, `seed_ratio`,
    `seed_days`), whether or not the check passed - a partial mapping would
    make `questions.missing_step` think an unanswered field is still
    missing, and re-ask for it forever. For any preset other than "own",
    `seed_ratio`/`seed_days` are blanked to `""` rather than dropped, so a
    saved-then-changed answer never leaves a stale number behind for
    `missing_step` (or a re-rendered form) to trip over.
    """

    ok: bool
    answers: Mapping[str, str]
    problem: str | None
    field: str | None


def _clean(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _parse_ratio(raw: str) -> tuple[Decimal | None, str | None]:
    """A blank ratio is a valid absence; anything else must be a plain
    decimal (no comma, no scientific notation) with at most 2 places,
    between 0.1 and 100.
    """
    if raw == "":
        return None, None
    if "," in raw or "e" in raw.lower():
        return None, SEEDING_PROBLEM_RATIO
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, SEEDING_PROBLEM_RATIO
    if not value.is_finite():
        return None, SEEDING_PROBLEM_RATIO
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        return None, SEEDING_PROBLEM_RATIO
    if value < _RATIO_MIN or value > _RATIO_MAX:
        return None, SEEDING_PROBLEM_RATIO
    return value, None


def _parse_days(raw: str) -> tuple[int | None, str | None]:
    """A blank day count is a valid absence; anything else must be a plain
    whole number between 1 and 365.
    """
    if raw == "":
        return None, None
    if not raw.isdigit():
        return None, SEEDING_PROBLEM_DAYS
    value = int(raw)
    if value < _DAYS_MIN or value > _DAYS_MAX:
        return None, SEEDING_PROBLEM_DAYS
    return value, None


def check_seeding(answers: Mapping[str, str]) -> SeedingCheck:
    """Whether a posted seeding answer is acceptable, and why not.

    Pure and never raises - every rule below is checked in a fixed order,
    and the first failing rule wins.
    """
    preset = _clean(answers.get("seeding", ""))
    seed_ratio_raw = _clean(answers.get("seed_ratio", ""))
    seed_days_raw = _clean(answers.get("seed_days", ""))

    def refuse(problem: str, field_name: str) -> SeedingCheck:
        return SeedingCheck(
            ok=False,
            answers={"seeding": preset, "seed_ratio": seed_ratio_raw, "seed_days": seed_days_raw},
            problem=problem,
            field=field_name,
        )

    if preset not in _PRESET_VALUES:
        return refuse(QUESTION_PICK_ONE, "seeding")

    if preset != "own":
        return SeedingCheck(
            ok=True,
            answers={"seeding": preset, "seed_ratio": "", "seed_days": ""},
            problem=None,
            field=None,
        )

    ratio_value, ratio_problem = _parse_ratio(seed_ratio_raw)
    if ratio_problem is not None:
        return refuse(ratio_problem, "seed_ratio")

    days_value, days_problem = _parse_days(seed_days_raw)
    if days_problem is not None:
        return refuse(days_problem, "seed_days")

    if ratio_value is None and days_value is None:
        return refuse(SEEDING_PROBLEM_OWN_EMPTY, "seed_ratio")

    return SeedingCheck(
        ok=True,
        answers={"seeding": "own", "seed_ratio": seed_ratio_raw, "seed_days": seed_days_raw},
        problem=None,
        field=None,
    )


# --- qBittorrent's own preference keys ---------------------------------------

# Every result carries these two, regardless of preset - `max_ratio_act: 0`
# is qBittorrent's "Stop" action (never "remove with files"; Sonarr and
# Radarr are the ones that remove a torrent, and only after it has already
# stopped at these limits). Per-tracker overrides synced from Prowlarr are
# the owner's own choice and are never touched here.
_COMMON_PREFERENCES: Final[Mapping[str, object]] = {
    "max_inactive_seeding_time_enabled": False,
    "max_ratio_act": 0,
}

_GOOD_NEIGHBOR_PREFERENCES: Final[Mapping[str, object]] = {
    "max_ratio_enabled": False,
    "max_seeding_time_enabled": True,
    "max_seeding_time": 7 * _MINUTES_PER_DAY,
}
_SAVE_SPACE_PREFERENCES: Final[Mapping[str, object]] = {
    "max_ratio_enabled": True,
    "max_ratio": 1.0,
    "max_seeding_time_enabled": True,
    "max_seeding_time": 7 * _MINUTES_PER_DAY,
}
_PRIVATE_PREFERENCES: Final[Mapping[str, object]] = {
    "max_ratio_enabled": False,
    "max_seeding_time_enabled": True,
    "max_seeding_time": 30 * _MINUTES_PER_DAY,
}

_PRESET_PREFERENCES: Final[Mapping[str, Mapping[str, object]]] = {
    "good_neighbor": _GOOD_NEIGHBOR_PREFERENCES,
    "save_space": _SAVE_SPACE_PREFERENCES,
    "private": _PRIVATE_PREFERENCES,
}


def _own_preferences(answers: Mapping[str, str]) -> dict[str, object]:
    prefs: dict[str, object] = {}
    ratio_raw = answers.get("seed_ratio", "")
    if ratio_raw:
        prefs["max_ratio_enabled"] = True
        prefs["max_ratio"] = float(Decimal(ratio_raw))
    else:
        prefs["max_ratio_enabled"] = False

    days_raw = answers.get("seed_days", "")
    if days_raw:
        prefs["max_seeding_time_enabled"] = True
        prefs["max_seeding_time"] = int(days_raw) * _MINUTES_PER_DAY
    else:
        prefs["max_seeding_time_enabled"] = False
    return prefs


def seeding_preferences(answers: Mapping[str, str] | None) -> dict[str, object]:
    """qBittorrent's global seeding limits for one saved answer.

    `None` (nothing saved yet) or an answer that fails `check_seeding` both
    fall back to the "good neighbor" result - the same preset the question
    step shows checked by default, so a missing answer never behaves
    differently from an owner who explicitly chose the default.
    """
    if answers is not None:
        check = check_seeding(answers)
        if check.ok:
            preset = check.answers["seeding"]
            if preset == "own":
                return {**_own_preferences(check.answers), **_COMMON_PREFERENCES}
            return {**_PRESET_PREFERENCES[preset], **_COMMON_PREFERENCES}

    return {**_GOOD_NEIGHBOR_PREFERENCES, **_COMMON_PREFERENCES}
