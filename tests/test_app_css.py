"""Tests that app.css only draws from tokens.css and carries no colour literals.

No CSS parser is needed: one regex pulls every `var(--name)` reference out of
app.css, another pulls every `--name:` declaration out of tokens.css, and the
set difference between them is the drift a hand-written colour or a typo'd
variable name would produce.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_APP_CSS_PATH = _REPO_ROOT / "src" / "marrquee" / "static" / "css" / "app.css"
_TOKENS_CSS_PATH = _REPO_ROOT / "src" / "marrquee" / "static" / "css" / "tokens.css"

_VAR_USE_RE = re.compile(r"var\(\s*(--[a-z0-9-]+)")
_VAR_DECLARATION_RE = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.MULTILINE)
_COLOUR_LITERAL_RE = re.compile(r"#[0-9a-fA-F]{3,8}|rgb\(|rgba\(|hsl\(")


def _app_css() -> str:
    return _APP_CSS_PATH.read_text()


def test_app_css_contains_no_colour_literals() -> None:
    assert _COLOUR_LITERAL_RE.search(_app_css()) is None


def test_every_var_used_in_app_css_is_declared_in_tokens_css() -> None:
    used = set(_VAR_USE_RE.findall(_app_css()))
    declared = set(_VAR_DECLARATION_RE.findall(_TOKENS_CSS_PATH.read_text()))

    undeclared = used - declared
    assert undeclared == set(), f"app.css uses undeclared variables: {undeclared}"


def test_app_css_carries_a_prefers_reduced_motion_block() -> None:
    css = _app_css()

    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transition: none !important" in css
    assert "animation: none !important" in css
