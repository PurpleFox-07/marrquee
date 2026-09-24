"""Structural tests for `poster.css`, `deploy.css` and `hub.css` - the
shared poster tile plus the Deploy screen's and the Hub's own per-state
rules.

Colour-literal, `:has()` and undeclared-variable checks already run
automatically over every stylesheet in `test_app_css.py` (it globs
`static/css/*.css`, so these files are covered there too without a second
copy of that test here). What's specific to this story: dimming must never
reach a tile's own words, and none of the three ever reaches for an id
selector.
"""

from __future__ import annotations

import re
from pathlib import Path

_CSS_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "css"
_POSTER_CSS = _CSS_DIR / "poster.css"
_DEPLOY_CSS = _CSS_DIR / "deploy.css"
_HUB_CSS = _CSS_DIR / "hub.css"

_ID_SELECTOR_RE = re.compile(r"#[a-zA-Z][\w-]*")


def _rule_blocks(css: str) -> list[str]:
    """Every rule's raw text, split naively on `}` - good enough here
    because none of the three files ever nests a `grayscale(` rule inside
    `@media`, which is the one case a brace-counting split would be needed
    for.
    """
    return [block for block in css.split("}") if "{" in block]


def test_the_dim_only_ever_reaches_the_art_layer() -> None:
    for path in (_POSTER_CSS, _DEPLOY_CSS, _HUB_CSS):
        for block in _rule_blocks(path.read_text()):
            selector, _, body = block.partition("{")
            if "grayscale(" in body:
                assert "::before" in selector, (
                    f"{path.name}: {selector.strip()!r} dims more than the art layer"
                )


def test_poster_and_deploy_css_carry_no_id_selector() -> None:
    for path in (_POSTER_CSS, _DEPLOY_CSS, _HUB_CSS):
        assert _ID_SELECTOR_RE.search(path.read_text()) is None, path.name


def test_deploy_css_holds_a_reduced_motion_block_that_removes_the_starting_lift() -> None:
    css = _DEPLOY_CSS.read_text()

    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "transform: none" in css


def test_poster_css_declares_the_art_gradient_and_deploy_css_no_longer_does() -> None:
    assert "linear-gradient(155deg" in _POSTER_CSS.read_text()
    assert "linear-gradient(155deg" not in _DEPLOY_CSS.read_text()


def test_the_poster_grid_shows_no_list_bullets() -> None:
    # The Hub wraps each poster link in an <li>, so without this reset
    # every poster gets a stray bullet dot beside it.
    css = _POSTER_CSS.read_text()
    grid_block = css.split(".poster-grid {", 1)[1].split("}", 1)[0]
    assert "list-style: none" in grid_block
    assert "padding: 0" in grid_block


def _block_for(css: str, selector: str) -> str:
    """The first rule block whose selector line is exactly `selector`, read
    the same naive brace-split way as `_rule_blocks` above. A block's own
    leading comment (if any) is dropped before splitting, since it isn't
    part of the selector list.
    """
    for block in _rule_blocks(css):
        head, _, body = block.partition("{")
        head = head.rsplit("*/", 1)[-1]
        if selector in {part.strip() for part in head.split(",")}:
            return body
    raise AssertionError(f"no {selector!r} block found")


def test_the_plus_tiles_size_comes_from_padding_not_a_minimum() -> None:
    body = _block_for(_HUB_CSS.read_text(), ".hub-plus")

    assert "padding:" in body
    assert "min-height" not in body
    assert "min-width" not in body


def test_the_edit_pills_hit_area_is_extended_with_a_negative_inset_after() -> None:
    body = _block_for(_HUB_CSS.read_text(), ".hub-link-edit::after")

    assert "inset:" in body
    assert "-1" in body or "calc(" in body


def test_a_down_link_card_is_never_dimmed() -> None:
    body = _block_for(_HUB_CSS.read_text(), '.hub-link[data-state="down"]::before')

    assert "filter: none" in body
