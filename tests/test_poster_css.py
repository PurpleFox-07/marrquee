"""Structural tests for `poster.css` and `deploy.css` - the shared poster
tile and the Deploy screen's own per-state, per-phase rules.

Colour-literal, `:has()` and undeclared-variable checks already run
automatically over every stylesheet in `test_app_css.py` (it globs
`static/css/*.css`, so these two files are covered there too without a
second copy of that test here). What's specific to this story: dimming
must never reach a tile's own words, and neither file ever reaches for an
id selector.
"""

from __future__ import annotations

import re
from pathlib import Path

_CSS_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "css"
_POSTER_CSS = _CSS_DIR / "poster.css"
_DEPLOY_CSS = _CSS_DIR / "deploy.css"

_ID_SELECTOR_RE = re.compile(r"#[a-zA-Z][\w-]*")


def _rule_blocks(css: str) -> list[str]:
    """Every rule's raw text, split naively on `}` - good enough here
    because neither file ever nests a `grayscale(` rule inside `@media`,
    which is the one case a brace-counting split would be needed for.
    """
    return [block for block in css.split("}") if "{" in block]


def test_the_dim_only_ever_reaches_the_art_layer() -> None:
    for path in (_POSTER_CSS, _DEPLOY_CSS):
        for block in _rule_blocks(path.read_text()):
            selector, _, body = block.partition("{")
            if "grayscale(" in body:
                assert "::before" in selector, (
                    f"{path.name}: {selector.strip()!r} dims more than the art layer"
                )


def test_poster_and_deploy_css_carry_no_id_selector() -> None:
    for path in (_POSTER_CSS, _DEPLOY_CSS):
        assert _ID_SELECTOR_RE.search(path.read_text()) is None, path.name


def test_deploy_css_holds_a_reduced_motion_block_that_removes_the_starting_lift() -> None:
    css = _DEPLOY_CSS.read_text()

    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transform: none" in css
