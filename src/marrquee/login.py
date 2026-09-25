"""The saved login: what is chosen, whether a candidate is acceptable, and
the QuestionSteps that ask for it.

This is a leaf, one step further than `catalog.py`: it holds the one
secret Marrquee keeps outside `install.json`, in its own root-only
`login.json`, so a version this build has never heard of never reads as
"nothing chosen" the way an unrecognised `install.json` does (that would
turn the owner's running NAS into a fresh wizard - see `state.py`'s
`STATE_VERSION` comment). Nothing here talks to Docker, HTTP or a route -
`login_apply.py` and the DeployManager own putting this login on an app;
the Hub and wizard own drawing it. This module only ever answers "what is
saved" and "is this one acceptable".

No other module may read or write `login.json` directly - every caller
goes through `load_login`/`save_login`/`record_applied` here, the same
single-owner rule `state.py` already holds for `install.json`.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from marrquee import words
from marrquee.catalog import apps_in_order
from marrquee.questions import QuestionCheck, QuestionField, QuestionStep
from marrquee.state import write_json_atomic

logger = logging.getLogger(__name__)

_LOGIN_FILE_NAME = "login.json"
_LOGIN_VERSION = 1

# The intersection of what every later app accepts (qBittorrent's username,
# Seerr's and Jellyfin's password floor). Kept as module constants so the
# hint words above and this check can never quietly drift apart from each
# other.
_USERNAME_MIN = 3
_USERNAME_MAX = 32
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")

_PASSWORD_MIN = 8
_PASSWORD_MAX = 128


@dataclass(frozen=True)
class SavedLogin:
    """The one username and password every login-taking app should carry.

    `password` is excluded from `repr` - a logged object or a traceback
    must never show it back, the same standard `InstallState`'s API keys
    already get from `_redact_secrets`.
    """

    username: str
    generation: int
    password: str = field(repr=False)


@dataclass(frozen=True)
class LoginRecord:
    """What `login.json` currently holds, or the empty shape when nothing
    has been saved yet.
    """

    login: SavedLogin | None
    applied: Mapping[str, int]
    reset_honored: str | None


LoginStatus = Literal["none", "reset", "set"]

_EMPTY_RECORD = LoginRecord(login=None, applied={}, reset_honored=None)


def load_login(config_dir: Path) -> LoginRecord:
    """Read `<config_dir>/login.json`, or the empty record for any reason
    at all.

    Mirrors `state.load_state`'s layered try/except: a missing file, an
    empty file, text that isn't JSON, the wrong version, or JSON in a
    shape this build doesn't recognise are all "nothing saved yet", never
    an exception a caller has to guard against. It re-reads the file on
    every call - nothing here caches it, so a login run started elsewhere
    (or a CI step that deletes the file) is always seen fresh.
    """
    try:
        raw = (config_dir / _LOGIN_FILE_NAME).read_text()
    except OSError:
        return _EMPTY_RECORD

    if not raw.strip():
        return _EMPTY_RECORD

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _EMPTY_RECORD

    if not isinstance(payload, dict) or payload.get("version") != _LOGIN_VERSION:
        return _EMPTY_RECORD

    try:
        return _from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return _EMPTY_RECORD


def save_login(
    config_dir: Path, username: str, password: str, *, honor_reset: str | None
) -> SavedLogin:
    """Save a new (or changed) login and bump the generation.

    `applied` is copied forward as-is rather than cleared: every app that
    was applied for the old generation is stale the moment this returns
    (`record_applied` only ever writes a match for the *current*
    generation), so leaving the numbers untouched is exactly what makes
    `pending_app_ids` see every app as pending again.
    """
    previous = load_login(config_dir)
    generation = (previous.login.generation if previous.login else 0) + 1
    reset_honored = honor_reset if honor_reset is not None else previous.reset_honored
    login = SavedLogin(username=username, generation=generation, password=password)
    _write(config_dir, login, previous.applied, reset_honored)
    return login


def record_applied(config_dir: Path, app_id: str, generation: int) -> None:
    """Mark `app_id` as carrying `generation`'s login.

    Drops the write when `generation` is no longer the file's current
    generation - a stale run (one that read the login before a Change
    saved a new one) can never mark a newer login as applied. Never
    raises: a write failure here means the Hub keeps naming the app as
    pending, which is the safe failure, but it must never crash the
    deploy or login run that's reporting it. Only `app_id` and the
    exception go to the log - never the login itself.
    """
    record = load_login(config_dir)
    if record.login is None or record.login.generation != generation:
        return
    applied = dict(record.applied)
    applied[app_id] = generation
    try:
        _write(config_dir, record.login, applied, record.reset_honored)
    except OSError:
        logger.exception("could not record that %s received the saved login", app_id)


def pending_app_ids(record: LoginRecord, installed: Iterable[str]) -> tuple[str, ...]:
    """Which of `installed`'s login-taking apps haven't received the
    current login, in catalog order - every one of them when nothing has
    been saved yet.
    """
    login_apps = [app for app in apps_in_order(installed) if app.login_kind != "none"]
    if record.login is None:
        return tuple(app.id for app in login_apps)
    generation = record.login.generation
    return tuple(app.id for app in login_apps if record.applied.get(app.id) != generation)


def login_status(record: LoginRecord, reset_value: str | None) -> LoginStatus:
    """`none` before anything is saved; `reset` while a saved login is
    still outstanding against `MARRQUEE_RESET_LOGIN`'s current value;
    `set` otherwise.
    """
    if record.login is None:
        return "none"
    if reset_value and reset_value != record.reset_honored:
        return "reset"
    return "set"


def reset_reminder(record: LoginRecord, reset_value: str | None) -> bool:
    """True while the owner should be told to delete the reset line and
    Redeploy: a login is saved, the reset line is still present, and it's
    the very value that login already honoured (so it won't reset again).
    """
    return record.login is not None and bool(reset_value) and reset_value == record.reset_honored


def password_matches(login: SavedLogin, typed: str) -> bool:
    """Constant-time comparison, so a timing side-channel can't shorten a
    guess at the owner's current password.
    """
    return hmac.compare_digest(login.password.encode("utf-8"), typed.encode("utf-8"))


def _write(
    config_dir: Path,
    login: SavedLogin,
    applied: Mapping[str, int],
    reset_honored: str | None,
) -> None:
    write_json_atomic(
        config_dir / _LOGIN_FILE_NAME,
        {
            "version": _LOGIN_VERSION,
            "username": login.username,
            "password": login.password,
            "generation": login.generation,
            "applied": dict(applied),
            "reset_honored": reset_honored,
        },
    )


def _from_payload(payload: dict[str, object]) -> LoginRecord:
    login = SavedLogin(
        username=_require_str(payload.get("username")),
        generation=_require_int(payload.get("generation")),
        password=_require_str(payload.get("password")),
    )
    applied = _require_int_dict(payload.get("applied"))
    reset_honored = _require_optional_str(payload.get("reset_honored"))
    return LoginRecord(login=login, applied=applied, reset_honored=reset_honored)


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _require_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _require_str(value)


def _require_int(value: object) -> int:
    # bool is an int subclass in Python; excluded so a stray `true`/`false`
    # in the file can't silently become 1/0 for a generation number.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected a whole number, got {value!r}")
    return value


def _require_int_dict(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise TypeError(f"expected a mapping, got {value!r}")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"expected a string key, got {key!r}")
        result[key] = _require_int(item)
    return result


# --- The rules, shared by every step below ----------------------------------


def _normalize_username(raw: str) -> str:
    return raw.strip().lower()


def _password_problem(password: str, password_again: str) -> tuple[str, str] | None:
    """The first rule `password`/`password_again` breaks, as
    `(problem, field)`, or `None` when both are acceptable.
    """
    if password != password.strip():
        return words.LOGIN_PROBLEM_PASSWORD_SPACES, "password"
    if len(password) < _PASSWORD_MIN:
        return words.LOGIN_PROBLEM_PASSWORD_SHORT, "password"
    if len(password) > _PASSWORD_MAX:
        return words.LOGIN_PROBLEM_PASSWORD_LONG, "password"
    if password_again != password:
        return words.LOGIN_PROBLEM_MISMATCH, "password_again"
    return None


def _check_login(answers: Mapping[str, str]) -> QuestionCheck:
    username = _normalize_username(answers.get("username", ""))
    if not _USERNAME_PATTERN.match(username):
        return QuestionCheck(
            ok=False, answers={}, problem=words.LOGIN_PROBLEM_USERNAME, field="username"
        )

    password = answers.get("password", "")
    password_again = answers.get("password_again", "")
    problem = _password_problem(password, password_again)
    if problem is not None:
        text, problem_field = problem
        return QuestionCheck(
            ok=False, answers={"username": username}, problem=text, field=problem_field
        )

    return QuestionCheck(
        ok=True,
        answers={"username": username, "password": password, "password_again": password_again},
        problem=None,
        field=None,
    )


def _check_change(answers: Mapping[str, str]) -> QuestionCheck:
    current_password = answers.get("current_password", "")
    if current_password == "":
        return QuestionCheck(
            ok=False,
            answers={},
            problem=words.CHANGE_PROBLEM_CURRENT_BLANK,
            field="current_password",
        )

    username = _normalize_username(answers.get("username", ""))
    if not _USERNAME_PATTERN.match(username):
        return QuestionCheck(
            ok=False,
            answers={"current_password": current_password},
            problem=words.LOGIN_PROBLEM_USERNAME,
            field="username",
        )

    password = answers.get("password", "")
    password_again = answers.get("password_again", "")
    if password == "" and password_again == "":
        # Both boxes blank means "keep the current password" - the Hub's
        # own Change route decides what "keep" means against the actually
        # saved value; this step only has to say the new-password rules
        # don't apply to a blank pair.
        return QuestionCheck(
            ok=True,
            answers={
                "current_password": current_password,
                "username": username,
                "password": "",
                "password_again": "",
            },
            problem=None,
            field=None,
        )

    problem = _password_problem(password, password_again)
    if problem is not None:
        text, problem_field = problem
        return QuestionCheck(
            ok=False,
            answers={"current_password": current_password, "username": username},
            problem=text,
            field=problem_field,
        )

    return QuestionCheck(
        ok=True,
        answers={
            "current_password": current_password,
            "username": username,
            "password": password,
            "password_again": password_again,
        },
        problem=None,
        field=None,
    )


_LOGIN_FIELDS: tuple[QuestionField, ...] = (
    QuestionField(
        name="username",
        label=words.LOGIN_USERNAME_LABEL,
        kind="text",
        hint=words.LOGIN_USERNAME_HINT,
    ),
    QuestionField(
        name="password",
        label=words.LOGIN_PASSWORD_LABEL,
        kind="password",
        hint=words.LOGIN_PASSWORD_HINT,
    ),
    QuestionField(name="password_again", label=words.LOGIN_PASSWORD_AGAIN_LABEL, kind="password"),
)

# Not registered in `questions.QUESTION_STEPS` - these ask for the one
# Marrquee-wide login, not a per-app answer, so the wizard and the Hub's
# panel each reach for these names directly instead of discovering them
# through `question_steps_for`.
LOGIN_STEP = QuestionStep(
    app_id="marrquee",
    step_id="login",
    title=words.LOGIN_STEP_TITLE,
    lede=words.LOGIN_STEP_LEDE,
    fields=_LOGIN_FIELDS,
    check=_check_login,
)

LOGIN_RESET_STEP = QuestionStep(
    app_id="marrquee",
    step_id="login",
    title=words.LOGIN_RESET_TITLE,
    lede=words.LOGIN_RESET_LEDE,
    fields=_LOGIN_FIELDS,
    check=_check_login,
)

CHANGE_STEP = QuestionStep(
    app_id="marrquee",
    step_id="change",
    title=words.CHANGE_TITLE,
    lede=words.CHANGE_LEDE,
    fields=(
        QuestionField(name="current_password", label=words.CHANGE_CURRENT_LABEL, kind="password"),
        QuestionField(
            name="username",
            label=words.LOGIN_USERNAME_LABEL,
            kind="text",
            hint=words.LOGIN_USERNAME_HINT,
        ),
        QuestionField(
            name="password",
            label=words.LOGIN_PASSWORD_LABEL,
            kind="password",
            hint=words.CHANGE_NEW_PASSWORD_HINT,
        ),
        QuestionField(
            name="password_again", label=words.LOGIN_PASSWORD_AGAIN_LABEL, kind="password"
        ),
    ),
    check=_check_change,
)
