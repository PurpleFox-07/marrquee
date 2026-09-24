"""Link cards: the owner's own bookmarks on the Hub.

A link card is not a catalog app - it has no compose service, no API key
and no wizard step. It lives in its own file (`links.json`) with the same
guarantee `install.json` gives `load_state`: whatever shape the file is in
- untouched, mid-write, hand-edited, from a version this build has never
heard of - `load_links` reads it as "no links yet" rather than raising, so
one bad record can never take the whole Hub down with it (that's what
`load_state` would do to the wizard).

This module only knows stdlib and `marrquee.state.write_json_atomic` - it
imports nothing else from the package, so `hub.py` and `health.py` can both
depend on it with no import cycle.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from marrquee.state import write_json_atomic

LINKS_VERSION = 1
LINK_LABEL_MAX = 40
LINK_URL_MAX = 2048
LINK_COUNT_MAX = 50
LINK_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_LINKS_FILE_NAME = "links.json"
_WEB_SCHEMES = ("http", "https")


@dataclass(frozen=True)
class LinkCard:
    """One saved link, as it lives in links.json and rides through the Hub."""

    id: str
    label: str
    url: str


LinkProblem = Literal[
    "label_missing",
    "label_too_long",
    "url_missing",
    "url_too_long",
    "url_not_web",
    "url_has_login",
    "url_invalid",
    "too_many",
]


@dataclass(frozen=True)
class LinkCheck:
    """The result of validating what the owner typed into the link form.

    `label` is always the trimmed label. `url` is the normalised URL when
    `ok` is True; otherwise it's the trimmed text exactly as typed, so a
    refusal can refill the box with what the owner already wrote.
    """

    ok: bool
    label: str
    url: str
    problem: LinkProblem | None


def new_link_id() -> str:
    """A fresh link id: 16 lowercase hex characters.

    Hex-only makes the id safe by construction everywhere it lands - an
    HTML attribute or a JS `[data-link="…"]` selector - with no escaping
    needed.
    """
    return secrets.token_hex(8)


def load_links(config_dir: Path) -> tuple[LinkCard, ...]:
    """Read the saved link cards, or `()` for any reason at all.

    A missing file, an empty file, text that isn't JSON, JSON with the
    wrong shape, an unknown version, a bad id or a duplicate id are all the
    same answer: no links yet, not an exception.
    """
    try:
        raw = (config_dir / _LINKS_FILE_NAME).read_text()
    except OSError:
        return ()

    if not raw.strip():
        return ()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ()

    if not isinstance(payload, dict) or payload.get("version") != LINKS_VERSION:
        return ()

    try:
        return _from_payload(payload)
    except (KeyError, TypeError, ValueError):
        return ()


def save_links(config_dir: Path, links: Sequence[LinkCard]) -> None:
    """Persist the link cards to `<config_dir>/links.json`, in order."""
    write_json_atomic(config_dir / _LINKS_FILE_NAME, _to_payload(links))


def check_link(label: str, url: str) -> LinkCheck:
    """Validate and normalise what the owner typed into the link form.

    Never raises. Does not check how many links already exist - a count
    refusal (`too_many`) is the route's call, once it knows how many are
    already saved.
    """
    trimmed_label = label.strip()
    trimmed_url = url.strip()

    if not trimmed_label:
        return LinkCheck(False, trimmed_label, trimmed_url, "label_missing")
    if len(trimmed_label) > LINK_LABEL_MAX:
        return LinkCheck(False, trimmed_label, trimmed_url, "label_too_long")
    if not trimmed_url:
        return LinkCheck(False, trimmed_label, trimmed_url, "url_missing")
    if len(trimmed_url) > LINK_URL_MAX:
        return LinkCheck(False, trimmed_label, trimmed_url, "url_too_long")

    # `urlsplit("nas.local:5000")` reads the host as a *scheme* ("nas.local"),
    # so the only safe test for "did the owner include a scheme?" is the
    # literal presence of "://" - not whatever urlsplit thinks it parsed.
    candidate = trimmed_url if "://" in trimmed_url else f"http://{trimmed_url}"

    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        # Covers both a malformed URL and a port that isn't a real port
        # number (e.g. the ".port" read on "http://javascript:alert(1)").
        return LinkCheck(False, trimmed_label, trimmed_url, "url_invalid")

    if parts.scheme.lower() not in _WEB_SCHEMES:
        return LinkCheck(False, trimmed_label, trimmed_url, "url_not_web")
    if parts.username is not None or parts.password is not None:
        return LinkCheck(False, trimmed_label, trimmed_url, "url_has_login")
    if parts.hostname is None or any(char.isspace() for char in parts.hostname):
        return LinkCheck(False, trimmed_label, trimmed_url, "url_invalid")
    if port == 0:
        return LinkCheck(False, trimmed_label, trimmed_url, "url_invalid")

    return LinkCheck(True, trimmed_label, candidate, None)


def link_glyph(label: str) -> str:
    """The card's badge: the first letter of the first two words, else the
    first two letters.

    "Home Assistant" -> "HA"; "Pi-hole" (one word once punctuation is
    dropped) -> "PI"; "router" -> "RO"; "x" -> "X"; a label with no
    letters or digits at all falls back to its own first two characters.
    """
    stripped = label.strip()
    cleaned_words = ["".join(char for char in word if char.isalnum()) for word in stripped.split()]
    words = [word for word in cleaned_words if word]

    if len(words) >= 2:
        glyph = words[0][0] + words[1][0]
    elif len(words) == 1:
        glyph = words[0][:2]
    else:
        glyph = stripped[:2]

    return glyph.upper()


def link_address(url: str) -> str:
    """The address a link card shows underneath its label: host[:port],
    scheme, path and query dropped. An IPv6 host keeps its brackets.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"

    try:
        port = parts.port
    except ValueError:
        port = None

    if port is not None:
        return f"{host}:{port}"
    return host


def _to_payload(links: Sequence[LinkCard]) -> dict[str, object]:
    return {
        "version": LINKS_VERSION,
        "links": [{"id": link.id, "label": link.label, "url": link.url} for link in links],
    }


def _from_payload(payload: dict[str, object]) -> tuple[LinkCard, ...]:
    raw_links = payload.get("links")
    if not isinstance(raw_links, list):
        raise TypeError(f"expected a list of links, got {raw_links!r}")

    cards: list[LinkCard] = []
    seen_ids: set[str] = set()
    for item in raw_links:
        card = _require_link(item)
        if card.id in seen_ids:
            raise ValueError(f"duplicate link id {card.id!r}")
        seen_ids.add(card.id)
        cards.append(card)
    return tuple(cards)


def _require_link(value: object) -> LinkCard:
    if not isinstance(value, dict):
        raise TypeError(f"expected a link object, got {value!r}")
    return LinkCard(
        id=_require_id(value.get("id")),
        label=_require_str(value.get("label")),
        url=_require_str(value.get("url")),
    )


def _require_id(value: object) -> str:
    if not isinstance(value, str) or not LINK_ID_RE.match(value):
        raise ValueError(f"expected a 16-character hex id, got {value!r}")
    return value


def _require_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value
