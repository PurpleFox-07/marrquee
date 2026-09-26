"""The per-app question registry: what an app needs to ask before it can be
added, and how a posted answer is checked.

`QUESTION_STEPS` carries Gluetun's VPN step, the first registered entry -
built here rather than in `vpn.py` so that module can stay a leaf (no
import of this one, so there's no cycle). The wizard and the Hub's "+"
panel both draw from `question_steps_for`/`find_step` instead of each
inventing its own per-app form, so a later app's question (the Plex account
claim) adds one registry entry rather than a new screen in two places.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from marrquee import seeding, vpn, words
from marrquee.catalog import apps_in_order
from marrquee.state import write_json_atomic
from marrquee.words import QUESTION_PICK_ONE

Quality = Literal["1080p", "4k"]
DEFAULT_QUALITY: Final[Quality] = "1080p"
TV_QUALITY_FIELD: Final = "tv_quality"
MOVIE_QUALITY_FIELD: Final = "movie_quality"

_ANSWERS_FILE_NAME = "answers.json"
_ANSWERS_VERSION = 1

_STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

FieldKind = Literal["text", "password", "choice", "list"]


@dataclass(frozen=True)
class QuestionOption:
    """One choice inside a `choice` or `list` field."""

    value: str
    label: str
    hint: str = ""
    url: str = ""
    disabled: bool = False


@dataclass(frozen=True)
class QuestionField:
    """One control on a question step - a text box, a password box, a set
    of radio choices, or a dropdown list.
    """

    name: str
    label: str
    kind: FieldKind
    hint: str = ""
    options: tuple[QuestionOption, ...] = ()
    default: str = ""
    guide_label: str = ""
    guide_url: str = ""
    # Non-empty only for a field the partial should wrap in its own
    # `data-field` div - a hook a show/hide rule can key off of. Every other
    # step (the login step among them) leaves this blank, so its rendered
    # markup never gains a wrapper it didn't already have.
    shown_when: str = ""


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
    # Set only on a step that belongs to one app (`app_id`) but is also
    # asked when a DIFFERENT app is being added - Recyclarr's quality
    # question, owned by Sonarr/Radarr (`sonarr.tv_quality`), asked again
    # when Recyclarr itself is added later. `None` for every step that is
    # only ever asked alongside its own app.
    asked_with: str | None = None

    def __post_init__(self) -> None:
        # `step_id` becomes a URL fragment, an HTML id and a data-attribute
        # value, so it has to already be safe everywhere it lands - one
        # check here rather than trusting every future caller to remember.
        if not _STEP_ID_PATTERN.match(self.step_id):
            raise ValueError(f"invalid step_id: {self.step_id!r}")
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate field names in step {self.step_id!r}: {names!r}")


# --- The VPN step: the only place `vpn.py`'s answer rules meet the shared
# question-answering machinery ------------------------------------------------


def _check_vpn_step(answers: Mapping[str, str]) -> QuestionCheck:
    result = vpn.check_vpn_answers(answers)
    return QuestionCheck(
        ok=result.ok, answers=result.answers, problem=result.problem, field=result.field
    )


def _provider_option(provider: vpn.VpnProvider) -> QuestionOption:
    label = provider.label
    if provider.unavailable is not None:
        label = f"{provider.label} - {provider.unavailable}"
    return QuestionOption(
        value=provider.value,
        label=label,
        url=vpn.provider_wiki_url(provider),
        disabled=provider.unavailable is not None,
    )


_VPN_PROVIDER_OPTIONS: tuple[QuestionOption, ...] = (
    QuestionOption(value="", label=words.VPN_PROVIDER_PLACEHOLDER, disabled=True),
    *(_provider_option(provider) for provider in vpn.VPN_PROVIDERS),
)

_VPN_FIELDS: tuple[QuestionField, ...] = (
    QuestionField(
        name="provider",
        label=words.VPN_PROVIDER_LABEL,
        kind="list",
        options=_VPN_PROVIDER_OPTIONS,
        guide_label=words.VPN_GUIDE_LINK,
        guide_url=words.VPN_GUIDE_INDEX_URL,
    ),
    QuestionField(
        name="vpn_type",
        label=words.VPN_TYPE_LABEL,
        kind="choice",
        options=(
            QuestionOption(
                value="openvpn", label=words.VPN_TYPE_OPENVPN, hint=words.VPN_TYPE_OPENVPN_HINT
            ),
            QuestionOption(
                value="wireguard",
                label=words.VPN_TYPE_WIREGUARD,
                hint=words.VPN_TYPE_WIREGUARD_HINT,
            ),
        ),
        default="openvpn",
    ),
    QuestionField(
        name="openvpn_user",
        label=words.VPN_OPENVPN_USER_LABEL,
        kind="text",
        hint=words.VPN_OPENVPN_USER_HINT,
    ),
    QuestionField(
        name="openvpn_password",
        label=words.VPN_OPENVPN_PASSWORD_LABEL,
        kind="password",
        hint=words.VPN_OPENVPN_PASSWORD_HINT,
    ),
    QuestionField(
        name="wireguard_private_key",
        label=words.VPN_WIREGUARD_KEY_LABEL,
        kind="password",
        hint=words.VPN_WIREGUARD_KEY_HINT,
    ),
    QuestionField(
        name="wireguard_addresses",
        label=words.VPN_WIREGUARD_ADDRESS_LABEL,
        kind="text",
        hint=words.VPN_WIREGUARD_ADDRESS_HINT,
    ),
    QuestionField(
        name="wireguard_preshared_key",
        label=words.VPN_WIREGUARD_PSK_LABEL,
        kind="password",
        hint=words.VPN_WIREGUARD_PSK_HINT,
    ),
    QuestionField(
        name="server_countries",
        label=words.VPN_COUNTRIES_LABEL,
        kind="text",
        hint=words.VPN_COUNTRIES_HINT,
    ),
)

# Not built in `vpn.py`: a `QuestionStep` has to import this module (for
# `QuestionStep`/`QuestionField`/`QuestionCheck` themselves), and `vpn.py`
# stays a leaf so nothing about the deploy engine or the compose builder
# can leak back into what a VPN answer *is*.
VPN_STEP = QuestionStep(
    app_id=vpn.VPN_APP_ID,
    step_id="vpn",
    title=words.VPN_STEP_TITLE,
    lede=words.VPN_STEP_LEDE,
    fields=_VPN_FIELDS,
    check=_check_vpn_step,
)


# --- The seeding step: the only place `seeding.py`'s answer rules meet the
# shared question-answering machinery, mirroring the VPN step above -------


def _check_seeding_step(answers: Mapping[str, str]) -> QuestionCheck:
    result = seeding.check_seeding(answers)
    return QuestionCheck(
        ok=result.ok, answers=result.answers, problem=result.problem, field=result.field
    )


_SEEDING_FIELDS: tuple[QuestionField, ...] = (
    QuestionField(
        name="seeding",
        label=words.SEEDING_LABEL,
        kind="choice",
        options=(
            QuestionOption(
                value="good_neighbor",
                label=words.SEEDING_GOOD_NEIGHBOR,
                hint=words.SEEDING_GOOD_NEIGHBOR_HINT,
            ),
            QuestionOption(
                value="save_space",
                label=words.SEEDING_SAVE_SPACE,
                hint=words.SEEDING_SAVE_SPACE_HINT,
            ),
            QuestionOption(
                value="private", label=words.SEEDING_PRIVATE, hint=words.SEEDING_PRIVATE_HINT
            ),
            QuestionOption(value="own", label=words.SEEDING_OWN, hint=words.SEEDING_OWN_HINT),
        ),
        default="good_neighbor",
    ),
    QuestionField(
        name="seed_ratio",
        label=words.SEEDING_RATIO_LABEL,
        kind="text",
        hint=words.SEEDING_RATIO_HINT,
        shown_when="seeding=own",
    ),
    QuestionField(
        name="seed_days",
        label=words.SEEDING_DAYS_LABEL,
        kind="text",
        hint=words.SEEDING_DAYS_HINT,
        shown_when="seeding=own",
    ),
)

SEEDING_STEP = QuestionStep(
    app_id="qbittorrent",
    step_id="seeding",
    title=words.SEEDING_STEP_TITLE,
    lede=words.SEEDING_STEP_LEDE,
    fields=_SEEDING_FIELDS,
    check=_check_seeding_step,
)


# --- The quality steps: owned by Sonarr/Radarr, asked whenever Recyclarr is
# (or will be) part of the install too. `check_step` has already refused any
# value outside the field's own options by the time this adapter runs, so it
# only ever has an accepted answer to hand back. -----------------------------


def _check_quality_step(answers: Mapping[str, str]) -> QuestionCheck:
    return QuestionCheck(ok=True, answers=answers, problem=None, field=None)


TV_QUALITY_STEP = QuestionStep(
    app_id="sonarr",
    step_id="quality",
    title=words.QUALITY_TV_STEP_TITLE,
    lede=words.QUALITY_TV_STEP_LEDE,
    fields=(
        QuestionField(
            name=TV_QUALITY_FIELD,
            label=words.QUALITY_LABEL,
            kind="choice",
            options=(
                QuestionOption("1080p", words.QUALITY_1080P, words.QUALITY_TV_1080P_HINT),
                QuestionOption("4k", words.QUALITY_4K, words.QUALITY_TV_4K_HINT),
            ),
            default="1080p",
        ),
    ),
    check=_check_quality_step,
    asked_with="recyclarr",
)

MOVIE_QUALITY_STEP = QuestionStep(
    app_id="radarr",
    step_id="quality",
    title=words.QUALITY_MOVIE_STEP_TITLE,
    lede=words.QUALITY_MOVIE_STEP_LEDE,
    fields=(
        QuestionField(
            name=MOVIE_QUALITY_FIELD,
            label=words.QUALITY_LABEL,
            kind="choice",
            options=(
                QuestionOption("1080p", words.QUALITY_1080P, words.QUALITY_MOVIE_1080P_HINT),
                QuestionOption("4k", words.QUALITY_4K, words.QUALITY_MOVIE_4K_HINT),
            ),
            default="1080p",
        ),
    ),
    check=_check_quality_step,
    asked_with="recyclarr",
)


# Read only through `question_steps_for`/`find_step`, both of which look up
# this name from the module's own globals at call time, so a test can
# monkeypatch `marrquee.questions.QUESTION_STEPS` and have both functions
# see the replacement. No other module may import this name directly.
QUESTION_STEPS: tuple[QuestionStep, ...] = (
    VPN_STEP,
    SEEDING_STEP,
    TV_QUALITY_STEP,
    MOVIE_QUALITY_STEP,
)


def question_steps_for(
    app_ids: Iterable[str], *, present: Iterable[str] = ()
) -> tuple[QuestionStep, ...]:
    """Every registered step that applies to `app_ids`, in catalog order
    and then in the order each app's own steps were declared.

    `present` names whatever is already installed (or, for the wizard,
    already ticked alongside `app_ids`) - a step is included either because
    its own app is in `app_ids` (the ordinary case), or because it is
    `asked_with` an app in `app_ids` and its own app is already part of the
    install (Recyclarr's add asking Sonarr's already-answered question
    again). The sort index is built from `app_ids | present` rather than
    `app_ids` alone, so a step whose own app is only in `present` (Sonarr,
    while adding Recyclarr) still has a catalog position to sort by instead
    of raising `KeyError`.
    """
    adding = set(app_ids)
    present_ids = set(present)
    everyone = adding | present_ids
    order_index = {app.id: index for index, app in enumerate(apps_in_order(everyone))}
    matching = [
        step
        for step in QUESTION_STEPS
        if (step.app_id in adding and (step.asked_with is None or step.asked_with in everyone))
        or (step.asked_with in adding and step.app_id in everyone)
    ]
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
    in `posted` is dropped. `text`, `choice` and `list` values are
    stripped; `password` never is, and a blank `password` keeps whatever
    was already saved for that field instead of overwriting it with
    nothing.
    """
    cleaned: dict[str, str] = {}
    for field in step.fields:
        raw = posted.get(field.name, "")
        if not isinstance(raw, str):
            raw = ""
        value = raw.strip() if field.kind in ("text", "choice", "list") else raw
        if field.kind == "password" and value == "":
            value = saved.get(field.name, value)
        if field.kind in ("choice", "list"):
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
