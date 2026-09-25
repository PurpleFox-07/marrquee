"""Tests for `same_origin`: the pure cross-site check (`request_is_cross_site`)
and the ASGI middleware every unsafe-method route in the app sits behind.

The middleware test enumerates the live app's own routes rather than a
fixed list, so a future POST route is covered the moment it's registered -
no one has to remember to add it here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from marrquee.config import Settings
from marrquee.docker_client import DockerStatus, FakeDockerEngine
from marrquee.main import create_app
from marrquee.same_origin import request_is_cross_site
from marrquee.words import CROSS_SITE_REFUSED

# --- request_is_cross_site: the pure check -----------------------------------


def test_a_cross_site_fetch_metadata_header_is_refused() -> None:
    assert request_is_cross_site(sec_fetch_site="cross-site", origin=None, host="marrquee.local")


def test_a_same_site_fetch_metadata_header_is_still_refused() -> None:
    # "same-site" means "a related site, not this one" in the Fetch Metadata
    # spec - only "same-origin" (and the browser's own "none") are exempt.
    assert request_is_cross_site(sec_fetch_site="same-site", origin=None, host="marrquee.local")


def test_same_origin_and_none_fetch_metadata_are_allowed() -> None:
    assert not request_is_cross_site(
        sec_fetch_site="same-origin", origin=None, host="marrquee.local"
    )
    assert not request_is_cross_site(sec_fetch_site="none", origin=None, host="marrquee.local")


def test_a_null_origin_is_refused() -> None:
    assert request_is_cross_site(sec_fetch_site=None, origin="null", host="marrquee.local")


def test_a_mismatched_origin_is_refused() -> None:
    assert request_is_cross_site(
        sec_fetch_site=None, origin="http://evil.example:1234", host="marrquee.local:7788"
    )


def test_a_matching_origin_is_allowed() -> None:
    assert not request_is_cross_site(
        sec_fetch_site=None, origin="http://marrquee.local:7788", host="marrquee.local:7788"
    )


def test_an_origin_with_no_host_header_is_refused() -> None:
    assert request_is_cross_site(sec_fetch_site=None, origin="http://marrquee.local", host=None)


def test_no_browser_headers_at_all_is_allowed() -> None:
    # curl in CI, or any client with no browser behind it, sends neither
    # header - Fetch Metadata's own fallback, not a hole to close.
    assert not request_is_cross_site(sec_fetch_site=None, origin=None, host="marrquee.local")


# --- The middleware, wired into the live app ---------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(host_mount=tmp_path / "host", config_dir=tmp_path / "config")


def _client(tmp_path: Path) -> TestClient:
    settings = _settings(tmp_path)
    engine = FakeDockerEngine(DockerStatus(connected=True, version="27.3.1"))
    app = create_app(settings=settings, engine=engine)
    return TestClient(app)


def _unsafe_routes(client: TestClient) -> list[tuple[str, str]]:
    """Every `(method, path)` this app answers with POST/PUT/PATCH/DELETE,
    `{param}` segments filled with "x" so each path resolves to a real
    route with no resource needing to actually exist.

    This FastAPI version wraps each `include_router()` call in its own
    `_IncludedRouter` rather than flattening routes onto `app.routes`
    directly, so this walks one level into `original_router.routes` for
    anything that isn't already a plain `APIRoute` - `wizard_router.routes`
    elsewhere in this test suite reads the same underlying attribute
    directly, on one router instead of every included one.
    """
    unsafe = {"POST", "PUT", "PATCH", "DELETE"}
    pairs: list[tuple[str, str]] = []

    def walk(routes: object) -> None:
        for route in routes:  # type: ignore[attr-defined]
            if isinstance(route, APIRoute):
                methods = route.methods or ()
                if unsafe.intersection(methods):
                    path = route.path
                    for param in route.param_convertors:
                        path = path.replace("{" + param + "}", "x")
                    for method in unsafe.intersection(methods):
                        pairs.append((method, path))
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes)

    walk(client.app.routes)  # type: ignore[attr-defined]
    return pairs


@pytest.mark.parametrize("sec_fetch_site", ["cross-site", "same-site"])
def test_every_post_route_refuses_cross_site(tmp_path: Path, sec_fetch_site: str) -> None:
    client = _client(tmp_path)
    routes = _unsafe_routes(client)
    assert routes, "expected at least one unsafe-method route to check"

    for method, path in routes:
        response = client.request(method, path, headers={"Sec-Fetch-Site": sec_fetch_site})
        assert response.status_code == 403, f"{method} {path} was not refused"
        assert response.text == CROSS_SITE_REFUSED


def test_a_null_origin_is_refused_on_a_real_route(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/hub/login/retry", headers={"Origin": "null"})

    assert response.status_code == 403
    assert response.text == CROSS_SITE_REFUSED


def test_a_mismatched_origin_is_refused_on_a_real_route(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/hub/login/retry", headers={"Origin": "http://evil.example"}, follow_redirects=False
    )

    assert response.status_code == 403
    assert response.text == CROSS_SITE_REFUSED


def test_same_origin_post_passes_through_to_the_route(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/hub/login/retry",
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )

    # No login saved, nothing running: the route itself just redirects home -
    # what matters here is that the middleware let it through at all.
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_no_browser_headers_pass_through_like_ci_curl(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post("/hub/login/retry", follow_redirects=False)

    assert response.status_code == 303


def test_get_is_never_checked(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.get("/", headers={"Sec-Fetch-Site": "cross-site"}, follow_redirects=False)

    # A cross-site GET still redirects to setup, exactly as a same-origin one
    # would - the guard only ever looks at unsafe methods.
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/apps"
