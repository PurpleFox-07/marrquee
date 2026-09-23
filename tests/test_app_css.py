"""Tests that every non-token stylesheet only draws from tokens.css, carries
no colour literals, and never reaches for a `:has()` selector.

No CSS parser is needed: one regex pulls every `var(--name)` reference out
of a stylesheet, another pulls every `--name:` declaration out of
tokens.css, and the set difference between them is the drift a hand-written
colour or a typo'd variable name would produce. Each stylesheet under
`static/css/` (other than tokens.css itself, which is generated and has no
`var()` uses to check) is walked the same way, so a new one added by a later
story is covered automatically.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CSS_DIR = _REPO_ROOT / "src" / "marrquee" / "static" / "css"
_TOKENS_CSS_PATH = _CSS_DIR / "tokens.css"

_VAR_USE_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
_VAR_DECLARATION_RE = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)
_COLOUR_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}|rgb\(|rgba\(|hsl\(")
_HAS_SELECTOR_RE = re.compile(r":has\(")


def _stylesheets() -> list[Path]:
    return sorted(path for path in _CSS_DIR.glob("*.css") if path.name != "tokens.css")


@pytest.mark.parametrize("path", _stylesheets(), ids=lambda path: path.name)
def test_stylesheet_contains_no_colour_literals(path: Path) -> None:
    assert _COLOUR_LITERAL_RE.search(path.read_text()) is None


@pytest.mark.parametrize("path", _stylesheets(), ids=lambda path: path.name)
def test_stylesheet_uses_no_has_selector(path: Path) -> None:
    assert _HAS_SELECTOR_RE.search(path.read_text()) is None


@pytest.mark.parametrize("path", _stylesheets(), ids=lambda path: path.name)
def test_every_var_used_is_declared_in_tokens_css(path: Path) -> None:
    used = set(_VAR_USE_RE.findall(path.read_text()))
    declared = set(_VAR_DECLARATION_RE.findall(_TOKENS_CSS_PATH.read_text()))

    undeclared = used - declared
    assert undeclared == set(), f"{path.name} uses undeclared variables: {undeclared}"


def test_app_css_carries_a_prefers_reduced_motion_block() -> None:
    css = (_CSS_DIR / "app.css").read_text()

    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transition: none !important" in css
    assert "animation: none !important" in css
