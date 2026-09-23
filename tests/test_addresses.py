"""Tests for `addresses.py` - the one place Marrquee decides what address a
browser reached it on, and what a chosen app's own link should be.

This module is standard library only and imports nothing from the rest of
the project (Story 6 imports it unchanged), so every test here builds
values by hand rather than through `create_app` or a fixture.
"""

from __future__ import annotations

import pytest

from marrquee.addresses import app_url, authority_from_headers, host_only, proxy_suspected

# --- host_only: the address table --------------------------------------------

_HOST_ONLY_TABLE = [
    ("192.168.1.50:7788", "192.168.1.50"),
    ("192.168.1.50", "192.168.1.50"),
    ("nas.local:7788", "nas.local"),
    ("nas.local", "nas.local"),
    ("[2001:db8::1]:7788", "[2001:db8::1]"),
    ("[2001:db8::1]", "[2001:db8::1]"),
    ("NAS.Local:7788", "nas.local"),
    ("localhost:7788", "localhost"),
    ("nas.local:7788, proxy.internal", "nas.local"),
    ("evil.host/path", "evil.host"),
    ("u@h.local", "h.local"),
    ("", None),
    ("2001:db8::1", None),  # unbracketed IPv6 - urlsplit alone would say '2001'
    ("::1", None),
    ("[bad", None),
    ("-bad.local", None),  # doesn't look like a real host
]


@pytest.mark.parametrize(("authority", "expected"), _HOST_ONLY_TABLE)
def test_the_address_table(authority: str, expected: str | None) -> None:
    assert host_only(authority) == expected


def test_marrquees_own_port_is_always_discarded_never_appended() -> None:
    # `authority` carries whatever port the browser used to reach
    # Marrquee's own port (7788 here); the link built is for the app's
    # port, never Marrquee's.
    assert app_url("192.168.1.50:7788", 8989) == "http://192.168.1.50:8989/"


def test_an_ipv6_host_comes_back_bracketed_in_the_url() -> None:
    assert app_url("[2001:db8::1]:7788", 8989) == "http://[2001:db8::1]:8989/"


def test_app_url_never_returns_localhost_unless_the_request_itself_said_localhost() -> None:
    assert app_url(None, 8989) is None
    assert app_url("", 8989) is None
    assert app_url("localhost:7788", 8989) == "http://localhost:8989/"


def test_app_url_is_none_when_the_host_cannot_be_trusted() -> None:
    assert app_url("[bad", 8989) is None
    assert app_url("2001:db8::1", 8989) is None


# --- proxy_suspected -----------------------------------------------------------


def test_proxy_suspected_true_for_forwarded_headers() -> None:
    assert proxy_suspected({"x-forwarded-host": "nas.local"})
    assert proxy_suspected({"x-forwarded-proto": "https"})
    assert proxy_suspected({"forwarded": "for=1.2.3.4"})


def test_proxy_suspected_ignores_x_forwarded_for() -> None:
    assert not proxy_suspected({"x-forwarded-for": "1.2.3.4"})


def test_proxy_suspected_false_with_nothing_unusual() -> None:
    assert not proxy_suspected({"host": "192.168.1.50:7788"})
    assert not proxy_suspected({})


# --- authority_from_headers ----------------------------------------------------


def test_authority_from_headers_prefers_the_forwarded_host() -> None:
    headers = {"x-forwarded-host": "nas.local", "host": "192.168.1.50:7788"}
    assert authority_from_headers(headers) == "nas.local"


def test_authority_from_headers_falls_back_to_host() -> None:
    assert authority_from_headers({"host": "192.168.1.50:7788"}) == "192.168.1.50:7788"


def test_authority_from_headers_is_none_with_neither_header() -> None:
    assert authority_from_headers({}) is None
