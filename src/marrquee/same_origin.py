"""A cross-site POST guard, refusing before a single route ever runs.

Before this story, no route checked where a request came from - Marrquee's
Hub had nothing worth stealing yet. Now a POST can save a password, and the
Hub itself stays open with no login of its own, so any page in any tab can
otherwise fire a form post at it. The check below is the algorithm Go
1.25's `http.CrossOriginProtection` ships: a browser's own Fetch Metadata
header first, an `Origin`-vs-`Host` comparison as a fallback for older
browsers, and "allowed" for a client that sends neither - that's not a
loophole, it's what lets curl (CI, the owner's own scripts) keep working
with no change on their side.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from marrquee.words import CROSS_SITE_REFUSED

# Only these methods can change anything - a cross-site GET can't do more
# than a same-origin one already could (open the page), so checking it
# would refuse nothing real while breaking every plain link into the app.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def request_is_cross_site(
    *, sec_fetch_site: str | None, origin: str | None, host: str | None
) -> bool:
    """Whether this request should be treated as coming from somewhere
    other than Marrquee's own page.

    `Sec-Fetch-Site` is authoritative when a browser sends it: anything
    other than `same-origin` (a plain form post or fetch from Marrquee's
    own page) or `none` (a browser-initiated request with no initiator,
    such as typing the address bar) is cross-site. Without it, `Origin`
    is the fallback - `null` (a sandboxed iframe, a redirected cross-origin
    request) and a missing `Host` are both refused outright, and otherwise
    the two are compared case-insensitively. A request with neither header
    - no browser sat between the client and Marrquee - is allowed; that is
    exactly what a `curl` call (CI, a script) looks like, and it never had
    anything to spoof in the first place.
    """
    if sec_fetch_site is not None:
        return sec_fetch_site.lower() not in ("same-origin", "none")
    if origin is not None:
        if origin == "null" or host is None:
            return True
        return urlsplit(origin).netloc.lower() != host.lower()
    return False


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope["headers"]:
        if key == name:
            return value.decode("latin-1")
    return None


class SameOriginGuard:
    """A pure ASGI middleware: refuses a cross-site unsafe-method request
    before it reaches routing, with no body ever read or buffered.

    Deliberately not `BaseHTTPMiddleware` - the refusal only ever needs the
    headers already sitting in `scope`, so there is nothing to gain (and a
    streaming body to lose) by wrapping the request/response cycle instead
    of answering directly at the ASGI layer.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        if request_is_cross_site(
            sec_fetch_site=_header(scope, b"sec-fetch-site"),
            origin=_header(scope, b"origin"),
            host=_header(scope, b"host"),
        ):
            response = PlainTextResponse(CROSS_SITE_REFUSED, status_code=403)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
