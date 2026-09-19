"""Tests for the package's single source of truth for its version."""

import importlib.metadata

import marrquee


def test_package_reports_a_version() -> None:
    """The package imports and __version__ is a non-empty string."""
    assert isinstance(marrquee.__version__, str)
    assert marrquee.__version__ != ""


def test_package_metadata_matches_dunder_version() -> None:
    """Hatch's dynamic version wiring must read the same value __version__ holds.

    If pyproject.toml's [tool.hatch.version] pointed at the wrong file, or the
    package were installed from stale metadata, these two values would drift
    apart even though nothing about the running code changed.
    """
    assert importlib.metadata.version("marrquee") == marrquee.__version__
