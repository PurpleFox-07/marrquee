"""Turns the design-values YAML file into CSS custom properties.

The design values live in exactly one file. This script reads that file
and writes `src/marrquee/static/css/tokens.css` from it, so every colour,
spacing value and shadow the app renders traces back to one source instead
of drifting into hand-edited copies.

Regenerate with: uv run python tools/generate_tokens_css.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
TOKENS_PATH = _REPO_ROOT / ".craft" / "design" / "tokens.yaml"
OUTPUT_PATH = _REPO_ROOT / "src" / "marrquee" / "static" / "css" / "tokens.css"

_HEADER_LINES = [
    "/* GENERATED FILE - do not edit by hand.",
    " * Source: .craft/design/tokens.yaml",
    " * Regenerate: uv run python tools/generate_tokens_css.py",
    " */",
]

# Inter is vendored alongside this file rather than loaded from a CDN, so the
# page renders correctly on a box with no internet access. The path is fixed
# relative to this generated file, not read from the YAML source.
_FONT_FACE_LINES = [
    "@font-face {",
    '  font-family: "Inter";',
    '  src: url("../fonts/Inter-Variable.woff2") format("woff2");',
    "  font-weight: 100 900;",
    "  font-style: normal;",
    "  font-display: swap;",
    "}",
]

_TYPOGRAPHY_PREFIXES = ("font-", "text-", "weight-", "leading-", "tracking-")
_TRANSITION_DURATION_KEYS = ("fast", "normal", "slow")


def _declare(name: str, value: object) -> str:
    """Format one `--name: value;` custom-property declaration."""
    return f"--{name}: {value};"


def _render_colors(colors: dict[str, str]) -> list[str]:
    # Unprefixed: matches the bare `--primary`, `--surface`, etc. that the
    # owner-approved preview markup already uses.
    return [_declare(key, value) for key, value in colors.items()]


def _render_spacing(spacing: dict[str, Any]) -> list[str]:
    declarations = []
    for key, value in spacing.items():
        if key == "unit":
            declarations.append(_declare("spacing-unit", value))
        elif key == "scale":
            # YAML parses these keys as integers (0, 1, 2, ...); stringify
            # them into the variable name.
            for scale_key, scale_value in value.items():
                declarations.append(_declare(f"space-{scale_key}", scale_value))
        else:
            # Named aliases: xs, sm, md, lg, xl, 2xl.
            declarations.append(_declare(f"space-{key}", value))
    return declarations


def _render_radius(radius: dict[str, str]) -> list[str]:
    return [_declare(f"radius-{key}", value) for key, value in radius.items()]


def _render_typography(typography: dict[str, Any]) -> list[str]:
    declarations = []
    for key, value in typography.items():
        if not key.startswith(_TYPOGRAPHY_PREFIXES):
            raise ValueError(f"Unrecognised typography token key: {key!r}")
        if key.startswith("text-"):
            declarations.append(_declare(key, f"{value}px"))
        else:
            # font-*, weight-*, leading-*, tracking-* all carry the correct
            # unit (or lack of one) already, so they pass through as-is.
            declarations.append(_declare(key, value))
    return declarations


def _render_shadows(shadows: dict[str, str]) -> list[str]:
    return [_declare(f"shadow-{key}", value) for key, value in shadows.items()]


def _render_transitions(transitions: dict[str, str]) -> list[str]:
    declarations = []
    for key, value in transitions.items():
        if key in _TRANSITION_DURATION_KEYS:
            declarations.append(_declare(f"transition-{key}", value))
        elif key.startswith("ease-"):
            declarations.append(_declare(key, value))
        else:
            raise ValueError(f"Unrecognised transitions token key: {key!r}")
    return declarations


def _render_z_index(z_index: dict[str, int]) -> list[str]:
    return [_declare(f"z-{key}", value) for key, value in z_index.items()]


def _render_breakpoints(breakpoints: dict[str, str]) -> list[str]:
    # CSS custom properties cannot be read inside a media query condition, so
    # these exist for reference (e.g. documentation, JS) rather than for use
    # in `@media (min-width: var(...))`, which would silently never match.
    comment = "/* Reference only - cannot be used inside @media conditions. */"
    return [comment, *(_declare(f"breakpoint-{key}", value) for key, value in breakpoints.items())]


def render_tokens_css(tokens: dict[str, Any]) -> str:
    """Render the parsed design-values document into CSS text.

    Pure and deterministic: the same document always produces the same
    string, which is what lets a test compare this function's output to the
    committed file and catch the moment they disagree.
    """
    declarations = [
        *_render_colors(tokens["colors"]),
        *_render_spacing(tokens["spacing"]),
        *_render_radius(tokens["radius"]),
        *_render_typography(tokens["typography"]),
        *_render_shadows(tokens["shadows"]),
        *_render_transitions(tokens["transitions"]),
        *_render_z_index(tokens["z-index"]),
        *_render_breakpoints(tokens["breakpoints"]),
    ]

    lines = [
        *_HEADER_LINES,
        "",
        *_FONT_FACE_LINES,
        "",
        ":root {",
        *(f"  {declaration}" for declaration in declarations),
        "}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    tokens = yaml.safe_load(TOKENS_PATH.read_text())
    css = render_tokens_css(tokens)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(css)
    print(f"Wrote {OUTPUT_PATH.relative_to(_REPO_ROOT)}")


if __name__ == "__main__":
    main()
