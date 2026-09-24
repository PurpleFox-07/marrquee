"""Tests for link-card records: load/save, validation and display helpers.

`load_links` must never raise - a settings folder the owner deleted, a
half-written file, or a record shape this build has never heard of are all
real states, and each one has to render as "no links yet" rather than crash
the Hub.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from marrquee.links import (
    LINK_COUNT_MAX,
    LINK_ID_RE,
    LINK_LABEL_MAX,
    LINK_URL_MAX,
    LINKS_VERSION,
    LinkCard,
    check_link,
    link_address,
    link_glyph,
    load_links,
    new_link_id,
    save_links,
)


def _card(id_: str, label: str, url: str) -> LinkCard:
    return LinkCard(id=id_, label=label, url=url)


# --- load_links: any bad shape reads as no links, never raises -------------


@pytest.mark.parametrize(
    "make_file",
    [
        pytest.param(lambda path: None, id="missing"),
        pytest.param(lambda path: path.write_text(""), id="empty"),
        pytest.param(lambda path: path.write_text("[]"), id="top-level-list"),
        pytest.param(
            lambda path: path.write_text(
                '{"version": 2, "links": [{"id": "0123456789abcdef", '
                '"label": "A", "url": "http://a"}]}'
            ),
            id="future-version",
        ),
        pytest.param(
            lambda path: path.write_text(
                '{"version": 1, "links": [{"id": "0123456789abcdef", '
                '"label": 5, "url": "http://a"}]}'
            ),
            id="non-string-label",
        ),
        pytest.param(
            lambda path: path.write_text(
                '{"version": 1, "links": [{"id": "zz", "label": "A", "url": "http://a"}]}'
            ),
            id="bad-id",
        ),
        pytest.param(
            lambda path: path.write_text(
                '{"version": 1, "links": ['
                '{"id": "0123456789abcdef", "label": "A", "url": "http://a"}, '
                '{"id": "0123456789abcdef", "label": "B", "url": "http://b"}'
                "]}"
            ),
            id="duplicate-id",
        ),
    ],
)
def test_a_wrong_shape_file_reads_as_no_links(
    tmp_path: Path, make_file: Callable[[Path], object]
) -> None:
    make_file(tmp_path / "links.json")

    assert load_links(tmp_path) == ()


def test_a_non_json_file_reads_as_no_links(tmp_path: Path) -> None:
    (tmp_path / "links.json").write_text("{not json")

    assert load_links(tmp_path) == ()


# --- save then load ----------------------------------------------------------


def test_save_then_load_round_trips_in_order(tmp_path: Path) -> None:
    cards = (
        _card(new_link_id(), "Router", "http://192.168.1.1"),
        _card(new_link_id(), "Home Assistant", "http://homeassistant.local:8123"),
        _card(new_link_id(), "Pi-hole", "http://pi.hole"),
    )

    save_links(tmp_path, cards)

    assert load_links(tmp_path) == cards


def test_links_file_is_written_0600(tmp_path: Path) -> None:
    save_links(tmp_path, (_card(new_link_id(), "Router", "http://192.168.1.1"),))

    written_files = list(tmp_path.iterdir())
    assert len(written_files) == 1
    links_file = written_files[0]
    assert links_file.name == "links.json"
    mode = links_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_new_link_id_matches_the_id_pattern_and_two_calls_differ() -> None:
    first = new_link_id()
    second = new_link_id()

    assert LINK_ID_RE.match(first)
    assert first != second


def test_links_version_is_1() -> None:
    assert LINKS_VERSION == 1


# --- check_link: normalising ------------------------------------------------


def test_bare_host_port_and_bare_ip_get_http_scheme() -> None:
    result = check_link("NAS", "nas.local:5000")
    assert result.ok
    assert result.url == "http://nas.local:5000"

    ip_result = check_link("NAS", "192.168.1.20")
    assert ip_result.ok
    assert ip_result.url == "http://192.168.1.20"


def test_a_url_with_an_existing_scheme_is_kept_as_typed() -> None:
    result = check_link("Example", "HTTPS://Example.com/x")

    assert result.ok
    assert result.url == "HTTPS://Example.com/x"


# --- check_link: javascript: and other unsafe schemes -----------------------


def test_javascript_scheme_never_becomes_a_link() -> None:
    no_scheme = check_link("Evil", "javascript:alert(1)")
    assert not no_scheme.ok
    assert no_scheme.problem == "url_invalid"

    with_scheme = check_link("Evil", "javascript://x")
    assert not with_scheme.ok
    assert with_scheme.problem == "url_not_web"

    ftp = check_link("Evil", "ftp://x")
    assert not ftp.ok
    assert ftp.problem == "url_not_web"


# --- check_link: logins, spaces, bad ports, empty hosts ----------------------


def test_a_login_in_the_url_is_refused() -> None:
    result = check_link("Router", "http://u:p@h")

    assert not result.ok
    assert result.problem == "url_has_login"


@pytest.mark.parametrize(
    "url",
    ["http://a b", "http://", "http://h:0", "http://h:99999"],
)
def test_a_space_bad_port_or_empty_host_is_invalid(url: str) -> None:
    result = check_link("Router", url)

    assert not result.ok
    assert result.problem == "url_invalid"


# --- check_link: label and URL limits ----------------------------------------


def test_a_label_over_the_limit_is_refused() -> None:
    result = check_link("x" * (LINK_LABEL_MAX + 1), "http://a")

    assert not result.ok
    assert result.problem == "label_too_long"


def test_a_whitespace_only_label_is_missing() -> None:
    result = check_link("   ", "http://a")

    assert not result.ok
    assert result.problem == "label_missing"


def test_an_empty_url_is_missing() -> None:
    result = check_link("Router", "   ")

    assert not result.ok
    assert result.problem == "url_missing"


def test_a_url_over_the_limit_is_refused() -> None:
    long_host = "h" * (LINK_URL_MAX + 1)
    result = check_link("Router", f"http://{long_host}")

    assert not result.ok
    assert result.problem == "url_too_long"


def test_link_count_max_is_50() -> None:
    assert LINK_COUNT_MAX == 50


# --- check_link: a refusal keeps what was typed ------------------------------


def test_a_refusal_keeps_the_trimmed_label_and_the_url_exactly_as_typed() -> None:
    result = check_link(" X ", "ftp://x")

    assert not result.ok
    assert result.label == "X"
    assert result.url == "ftp://x"


# --- link_glyph ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Home Assistant", "HA"),
        ("Pi-hole", "PI"),
        ("router", "RO"),
        ("x", "X"),
        ("★★", "★★"),
    ],
)
def test_glyph_rule(label: str, expected: str) -> None:
    assert link_glyph(label) == expected


# --- link_address --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://home.example.com/admin", "home.example.com"),
        ("http://192.168.1.20:8123", "192.168.1.20:8123"),
        ("http://[fe80::1]:80/", "[fe80::1]:80"),
    ],
)
def test_address_drops_the_scheme_path_and_query(url: str, expected: str) -> None:
    assert link_address(url) == expected
