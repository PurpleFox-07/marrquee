"""Marrquee - a self-hosted web wizard that deploys the arr media stack.

This is the single place the app's version is written down. Everything else
that needs it - the package metadata hatchling builds, the health check, the
page footer - reads it from here so the two can never disagree.
"""

__version__: str = "0.1.0"
