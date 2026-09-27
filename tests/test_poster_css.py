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


def test_reduced_motion_block_moved_to_poster_css_and_removes_the_starting_lift() -> None:
    poster_css = _POSTER_CSS.read_text()
    deploy_css = _DEPLOY_CSS.read_text()

    assert "@media (prefers-reduced-motion: reduce)" in poster_css
    assert "transform: none" in poster_css
    assert "@media (prefers-reduced-motion: reduce)" not in deploy_css


def test_poster_css_declares_the_art_gradient_and_deploy_css_no_longer_does() -> None:
    assert "linear-gradient(155deg" in _POSTER_CSS.read_text()
    assert "linear-gradient(155deg" not in _DEPLOY_CSS.read_text()


def test_spotlight_keyframes_are_declared_exactly_once_in_poster_css() -> None:
    """`@keyframes poster-spotlight` and `@keyframes poster-spin` moved
    (not copied) from `deploy.css` to `poster.css`, so the Deploy screen's
    own starting tile and the Hub's adding tile draw from one animation.
    """
    poster_css = _POSTER_CSS.read_text()
    deploy_css = _DEPLOY_CSS.read_text()

    assert poster_css.count("@keyframes poster-spotlight") == 1
    assert poster_css.count("@keyframes poster-spin") == 1
    assert "@keyframes" not in deploy_css


def test_the_starting_spotlight_covers_both_the_deploy_and_the_hub_tile() -> None:
    poster_css = _POSTER_CSS.read_text()

    assert (
        '.poster[data-state="starting"]:not(.hub-poster),\n'
        '.poster[data-add-state="starting"] {' in poster_css
    )


def test_the_linking_outline_covers_both_the_deploy_and_the_hub_tile() -> None:
    poster_css = _POSTER_CSS.read_text()

    assert '.poster[data-linking="true"],\n.poster[data-add-state="wiring"] {' in poster_css


def test_the_deploy_screens_own_starting_tile_carries_no_hub_poster_class() -> None:
    """The moved selector's `:not(.hub-poster)` guard only keeps excluding
    the Deploy screen's own tile if that tile's markup never gains the
    Hub's `hub-poster` class - this pins the fact directly in the
    template, with no need for a live render.
    """
    deploy_html = (
        Path(__file__).resolve().parents[1] / "src" / "marrquee" / "templates" / "deploy.html"
    ).read_text()

    assert 'class="poster"' in deploy_html
    assert "hub-poster" not in deploy_html


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


def test_the_plus_tile_shows_no_link_underline() -> None:
    # A link's default underline drew a stray dash under the "+" on the NAS.
    body = _block_for(_HUB_CSS.read_text(), ".hub-plus")
    assert "text-decoration: none" in body


def test_every_hub_row_is_the_same_height() -> None:
    body = _block_for(_HUB_CSS.read_text(), ".hub-stage .poster-grid")
    assert "grid-auto-rows: 1fr" in body


def test_app_posters_and_link_cards_fill_their_grid_cell() -> None:
    css = _HUB_CSS.read_text()
    # One shared rule block lists both item selectors.
    item_body = _block_for(css, ".hub-app-item")
    assert "display: flex" in item_body
    assert "flex-direction: column" in item_body
    assert "flex: 1" in _block_for(css, ".hub-app-item > .hub-poster")
    assert "flex: 1" in _block_for(css, ".hub-link-item > .hub-poster")


def test_the_vpn_down_rule_binds_error_and_never_borders_an_ordinary_down_poster() -> None:
    css = _HUB_CSS.read_text()

    body = _block_for(css, '.hub-poster[data-kind="vpn"][data-state="down"]')
    assert "border-color: var(--error);" in body

    # The plain Down poster (no `data-kind` in its own selector) must keep
    # its ordinary border - a VPN's own rule has to name `data-kind="vpn"`
    # explicitly rather than widening every Down poster's border colour.
    down_body = _block_for(css, '.hub-poster[data-state="down"]')
    assert "border-color: var(--error);" not in down_body


def test_the_vpn_down_poster_is_never_dimmed() -> None:
    selector = '.hub-poster[data-kind="vpn"][data-state="down"]::before'
    body = _block_for(_HUB_CSS.read_text(), selector)

    assert "filter: none" in body


def test_the_existing_plex_down_poster_is_never_dimmed() -> None:
    """The owner's own Plex keeps a working link even while Down - like the
    VPN's own Down rule beside it, its art must never dim the way an
    ordinary Down poster's does.
    """
    selector = '.hub-poster[data-managed="false"][data-state="down"]::before'
    body = _block_for(_HUB_CSS.read_text(), selector)

    assert "filter: none" in body


def test_the_paused_dot_is_warning_not_error() -> None:
    css = _HUB_CSS.read_text()

    body = _block_for(css, '.hub-poster[data-paused="true"] .hub-dot')
    assert "background: var(--warning);" in body

    # It has to win by coming LATER in the cascade than the plain down-state
    # dot - same selector specificity, so source order is what decides.
    paused_index = css.index('.hub-poster[data-paused="true"] .hub-dot')
    down_index = css.index('.hub-poster[data-state="down"] .hub-dot')
    assert paused_index > down_index


def test_recyclarrs_late_and_failed_states_are_warning_not_error() -> None:
    css = _HUB_CSS.read_text()

    ring_body = _block_for(css, '.hub-poster[data-sync-state="failed"]')
    assert "border-color: var(--warning);" in ring_body
    assert "box-shadow" in ring_body

    dot_body = _block_for(css, '.hub-poster[data-sync-state="failed"] .hub-dot')
    assert "background: var(--warning);" in dot_body

    # No raw colour literal anywhere in either rule - tokens only.
    for body in (ring_body, dot_body):
        assert "#" not in body


def test_sync_now_is_hidden_while_syncing_not_removed() -> None:
    body = _block_for(
        _HUB_CSS.read_text(),
        '.hub-poster[data-sync-state="syncing"] ~ .hub-tile-actions [data-role="sync-now"]',
    )

    assert "display: none" in body


def test_the_tile_links_tap_target_comes_from_padding_not_a_fixed_height() -> None:
    body = _block_for(_HUB_CSS.read_text(), ".hub-tile-link")

    assert "padding-block: var(--space-3);" in body
    assert "height" not in body


def test_the_no_vpn_badge_gets_its_tap_target_from_padding_not_a_fixed_height() -> None:
    # Colour-literal and undeclared-variable checks already run over every
    # stylesheet in `test_app_css.py` - this only pins the one shape that's
    # specific to the badge: its padding-block comes from `--space-3`
    # (the shorthand's first value), never a fixed height.
    body = _block_for(_HUB_CSS.read_text(), ".no-vpn-badge")

    assert "padding: var(--space-3) var(--space-4);" in body
    assert "height" not in body


def test_the_drive_note_gets_its_tap_target_from_padding_not_a_fixed_height() -> None:
    body = _block_for(_HUB_CSS.read_text(), ".hub-drive-note")

    assert "padding: var(--space-3) var(--space-4);" in body
    assert "height" not in body


def test_the_drive_note_is_hidden_by_the_root_attribute_not_by_removing_it() -> None:
    """The poll needs the note ON the page to toggle it live - unlike the
    down-note and docker-banner rules right above it in hub.css, which the
    same pattern already covers, this pins the shape specific to the drive
    note's own hooks.
    """
    css = _HUB_CSS.read_text()

    hidden_body = _block_for(css, '[data-role="drive-note"]')
    assert "display: none;" in hidden_body

    shown_body = _block_for(css, '[data-drive-note="true"] [data-role="drive-note"]')
    assert "display: inline-flex;" in shown_body


def test_the_plex_servers_and_plex_panes_are_shown_by_their_own_panel_mode() -> None:
    css = _HUB_CSS.read_text()

    assert (
        '.hub-panel[data-panel-mode="plex-servers"] [data-panel-pane="plex-servers"],\n'
        '.hub-panel[data-panel-mode="plex"] [data-panel-pane="plex"] {' in css
    )


def test_the_plex_server_list_is_a_grid_of_surface_cards() -> None:
    css = _HUB_CSS.read_text()

    list_body = _block_for(css, ".plex-server-list")
    assert "display: grid;" in list_body
    assert "gap: var(--space-3);" in list_body

    card_body = _block_for(css, ".plex-server")
    assert "background: var(--surface);" in card_body
    assert "border: 1px solid var(--border);" in card_body
    assert "border-radius: var(--radius-lg);" in card_body
    assert "padding: var(--space-4);" in card_body

    name_body = _block_for(css, ".plex-server__name")
    assert "font-size: var(--text-base);" in name_body
    assert "font-weight: var(--weight-bold);" in name_body
    assert "color: var(--text-primary);" in name_body

    hint_body = _block_for(css, ".plex-server__hint")
    assert "font-size: var(--text-sm);" in hint_body
    assert "color: var(--text-secondary);" in hint_body


def test_the_replace_link_checkbox_uses_the_spotlight_accent() -> None:
    css = _HUB_CSS.read_text()

    label_body = _block_for(css, ".plex-server label")
    assert "font-size: var(--text-sm);" in label_body
    assert "color: var(--text-primary);" in label_body

    checkbox_body = _block_for(css, '.plex-server input[type="checkbox"]')
    assert "accent-color: var(--spotlight);" in checkbox_body


def test_disconnect_is_a_ghost_button_tinted_toward_error() -> None:
    body = _block_for(_HUB_CSS.read_text(), '[data-role="disconnect-form"] .btn-ghost')

    assert "color: var(--error);" in body
    assert "border-color: var(--error);" in body
