"""Tests for the CSS custom-property generator.

These compare the committed CSS file to what the generator produces from
the current design values, so an edit to one without regenerating the
other fails the test suite instead of shipping a page with the wrong
colours.
"""

from __future__ import annotations

import re

import yaml

from tools.generate_tokens_css import OUTPUT_PATH, TOKENS_PATH, render_tokens_css

# Matches a single custom-property declaration line, e.g. "  --text-sm: 14px;"
_DECLARATION_RE = re.compile(r"--([a-z0-9-]+):\s*([^;]+);")


def _load_tokens() -> dict[str, object]:
    return yaml.safe_load(TOKENS_PATH.read_text())  # type: ignore[no-any-return]


def _committed_css() -> str:
    return OUTPUT_PATH.read_text()


def _declared_names_to_values() -> dict[str, str]:
    """All `--name: value;` pairs in the committed CSS file, by name."""
    return dict(_DECLARATION_RE.findall(_committed_css()))


def test_committed_tokens_css_matches_the_generator() -> None:
    tokens = _load_tokens()

    assert _committed_css() == render_tokens_css(tokens)


def test_every_colour_in_source_is_emitted_unprefixed() -> None:
    tokens = _load_tokens()
    declared = _declared_names_to_values()

    colors = tokens["colors"]
    assert isinstance(colors, dict)
    for key in colors:
        assert key in declared, f"--{key} was not emitted for colors.{key}"


def test_spacing_scale_survives_integer_yaml_keys() -> None:
    declared = _declared_names_to_values()

    assert "space-0" in declared
    assert declared["space-0"] == "0px"
    assert "space-24" in declared
    assert declared["space-24"] == "96px"


def test_font_sizes_gain_a_px_unit_and_weights_do_not() -> None:
    declared = _declared_names_to_values()

    assert declared["text-sm"] == "14px"
    assert declared["weight-semibold"] == "600"


def test_pill_radius_is_available() -> None:
    declared = _declared_names_to_values()

    assert declared["radius-full"] == "9999px"


def test_no_declaration_appears_twice() -> None:
    names = [name for name, _value in _DECLARATION_RE.findall(_committed_css())]

    assert len(names) == len(set(names)), "a custom property name was declared more than once"


def test_vendored_font_exists_and_tokens_css_points_at_it() -> None:
    font_path = OUTPUT_PATH.parent.parent / "fonts" / "Inter-Variable.woff2"

    assert font_path.exists()
    assert font_path.stat().st_size > 0

    css_text = _committed_css()
    assert "@font-face" in css_text

    match = re.search(r'url\("([^"]+)"\)', css_text)
    assert match is not None, "no url(...) found in the @font-face block"
    referenced_path = (OUTPUT_PATH.parent / match.group(1)).resolve()
    assert referenced_path == font_path.resolve()
