"""The per-app question registry: what an app needs to ask before it can be
added, and how a posted answer is checked.

`QUESTION_STEPS` ships empty in this story - no catalog app has a question
of its own yet. It exists now so the wizard and the Hub's "+" panel can
both draw from `question_steps_for`/`find_step` instead of each inventing
its own per-app form, and so a later story (the VPN details a downloader
needs, the Plex account claim) adds one registry entry rather than a new
screen in two places.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from marrquee.catalog import apps_in_order
from marrquee.state import write_json_atomic
from marrquee.words import QUESTION_PICK_ONE

_ANSWERS_FILE_NAME = "answers.json"
_ANSWERS_VERSION = 1

_STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

FieldKind = Literal["text", "password", "choice"]


@dataclass(frozen=True)
class QuestionOption:
    """One radio choice inside a `choice` field."""

    value: str
    label: str
    hint: str = ""


@dataclass(frozen=True)
class QuestionField:
    """One control on a question step - a text box, a password box, or a
    set of radio choices.
    """

    name: str
    label: str
    kind: FieldKind
    hint: str = ""
    options: tuple[QuestionOption, ...] = ()
    default: str = ""


@dataclass(frozen=True)
class QuestionCheck:
    """The outcome of checking one step's posted answers.

    `answers` holds the cleaned values worth saving - on a refusal that's
    whatever passed before the first problem. `problem` and `field` are
    either both `None` (an accepted answer) or both set (a named field with
    a plain-language reason), never a partial pair.
    """

    ok: bool
    answers: Mapping[str, str]
    problem: str | None
    field: str | None


@dataclass(frozen=True)
class QuestionStep:
    """One screen's worth of questions for one app.

    Drawn by the same partial in both the wizard and the Hub's "+" panel,
    so the two can never quietly ask something different.
    """

    app_id: str
    step_id: str
    title: str
    lede: str
    fields: tuple[QuestionField, ...]
    check: Callable[[Mapping[str, str]], QuestionCheck]

    def __post_init__(self) -> None:
        # `step_id` becomes a URL fragment, an HTML id and a data-attribute
        # value, so it has to already be safe everywhere it lands - one
        # check here rather than trusting every future caller to remember.
        if not _STEP_ID_PATTERN.match(self.step_id):
            raise ValueError(f"invalid step_id: {self.step_id!r}")
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate field names in step {self.step_id!r}: {names!r}")


# Ships empty - no catalog app has a question yet. Read only through
# `question_steps_for`/`find_step`, both of which look up this name from
# the module's own globals at call time, so a test can monkeypatch
# `marrquee.questions.QUESTION_STEPS` and have both functions see the
# replacement. No other module may import this name directly.
QUESTION_STEPS: tuple[QuestionStep, ...] = ()


def question_steps_for(app_ids: Iterable[str]) -> tuple[QuestionStep, ...]:
    """Every registered step for `app_ids`, in catalog order and then in
    the order each app's own steps were declared.
    """
    order_index = {app.id: index for index, app in enumerate(apps_in_order(app_ids))}
    matching = [step for step in QUESTION_STEPS if step.app_id in order_index]
    matching.sort(key=lambda step: order_index[step.app_id])
    return tuple(matching)


def find_step(app_id: str, step_id: str) -> QuestionStep | None:
    """The one registered step matching both ids, or `None`."""
    for step in QUESTION_STEPS:
        if step.app_id == app_id and step.step_id == step_id:
            return step
    return None


def check_step(
    step: QuestionStep, posted: Mapping[str, str], saved: Mapping[str, str]
) -> QuestionCheck:
    """Turn a posted form into a `QuestionCheck` for `step`, never raising.

    Only `step`'s own field names ever reach `step.check` - anything else
    in `posted` is dropped. `text` and `choice` values are stripped;
    `password` never is, and a blank `password` keeps whatever was already
    saved for that field instead of overwriting it with nothing.
    """
    cleaned: dict[str, str] = {}
    for field in step.fields:
        raw = posted.get(field.name, "")
        if not isinstance(raw, str):
            raw = ""
        value = raw.strip() if field.kind in ("text", "choice") else raw
        if field.kind == "password" and value == "":
            value = saved.get(field.name, value)
        if field.kind == "choice":
            allowed = {option.value for option in field.options}
            if value not in allowed:
                return QuestionCheck(
                    ok=False, answers=cleaned, problem=QUESTION_PICK_ONE, field=field.name
                )
        cleaned[field.name] = value
    return step.check(cleaned)


def missing_step(
    app_ids: Iterable[str], answers: Mapping[str, Mapping[str, str]]
) -> QuestionStep | None:
    """The first registered step (catalog then declared order) that still
    has at least one of its fields unanswered for the given apps.
    """
    for step in question_steps_for(app_ids):
        saved = answers.get(step.app_id, {})
        if any(field.name not in saved for field in step.fields):
            return step
    return None


def load_answers(config_dir: Path) -> Mapping[str, Mapping[str, str]]:
    """Read `<config_dir>/answers.json`, or `{}` for any reason at all.

    Mirrors `state.load_state`'s layered try/except: a missing file, an
    empty file, text that isn't JSON, the wrong version, or JSON in a shape
    this build doesn't recognise are all "nothing saved yet", never an
    exception a caller has to guard against.
    """
    try:
        raw = (config_dir / _ANSWERS_FILE_NAME).read_text()
    except OSError:
        return {}

    if not raw.strip():
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if not isinstance(payload, dict) or payload.get("version") != _ANSWERS_VERSION:
        return {}

    try:
        return _from_answers_payload(payload)
    except (KeyError, TypeError, ValueError):
        return {}


def save_step_answers(config_dir: Path, app_id: str, answers: Mapping[str, str]) -> None:
    """Merge `answers` into `app_id`'s saved map and write the file back.

    A merge, not a replace - posting one step's answers must never erase a
    different step's already-saved fields for the same app.
    """
    existing = load_answers(config_dir)
    apps = {
        existing_id: dict(existing_answers) for existing_id, existing_answers in existing.items()
    }
    apps.setdefault(app_id, {})
    apps[app_id].update(answers)
    write_json_atomic(
        config_dir / _ANSWERS_FILE_NAME,
        {"version": _ANSWERS_VERSION, "apps": apps},
    )


def _from_answers_payload(payload: dict[str, object]) -> dict[str, dict[str, str]]:
    apps = payload.get("apps")
    if not isinstance(apps, dict):
        raise TypeError(f"expected a mapping of apps, got {apps!r}")
    result: dict[str, dict[str, str]] = {}
    for app_id, app_answers in apps.items():
        if not isinstance(app_id, str):
            raise TypeError(f"expected a string app id, got {app_id!r}")
        if not isinstance(app_answers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in app_answers.items()
        ):
            raise TypeError(f"expected a mapping of strings, got {app_answers!r}")
        result[app_id] = dict(app_answers)
    return result
