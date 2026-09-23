"""Where the owner's browser actually is - built from the request, never
guessed.

`AppProgress` carries only a port (see its own docstring for why), and
`AppHealth` carries no address at all: both are built server-side with no
browser request attached, so neither can ever know the address a phone on
the same network would need. Every clickable link is built here instead,
per request, from whichever address the browser used to reach Marrquee in
the first place.

Standard library only, and nothing here imports from the rest of the
project - this module is shared unchanged between the Deploy screen and the
Hub.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

# A bare hostname or IPv4 literal: starts and ends with a letter or digit,
# with only letters, digits, dots and hyphens in between. This is what's
# left of RFC 1123 once a leading hyphen (never a real host) is ruled out.
_HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

_PROXY_HEADER_NAMES = ("x-forwarded-host", "x-forwarded-proto", "forwarded")


def host_only(authority: str) -> str | None:
    """The bare host from a `Host` or `X-Forwarded-Host` value, with any
    port, userinfo and path discarded - or `None` when nothing trustworthy
    can be read out of it.

    Takes the first entry of a comma-separated proxy chain, so
    `X-Forwarded-Host: nas.local, proxy.internal` yields the client-facing
    host, never an inner hop. Refuses a bare, unbracketed IPv6 literal
    before ever calling `urlsplit` on it: `urlsplit("//2001:db8::1").hostname`
    quietly returns `"2001"`, which would otherwise become a real (wrong)
    link.
    """
    first = authority.split(",", 1)[0].strip()
    if not first:
        return None

    bracketed = first.startswith("[")
    if not bracketed and first.count(":") > 1:
        return None

    try:
        parsed = urlsplit(f"//{first}")
    except ValueError:
        return None

    host = parsed.hostname
    if not host:
        return None

    if bracketed:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
        return f"[{host}]"

    if not _HOSTNAME_RE.match(host):
        return None
    return host


def app_url(authority: str | None, port: int) -> str | None:
    """`http://<host>:<port>/` built from the address the browser used, or
    `None` when no trustworthy host can be worked out.

    Always `http` - the arr apps only ever publish plain HTTP on their own
    ports, so inheriting `https` from a proxied Marrquee would produce a
    link guaranteed to fail. Never invents a host: there is no `localhost`
    fallback and no configured address, because a fabricated link is worse
    than the page's own "couldn't work out an address" wording.
    """
    if authority is None:
        return None
    host = host_only(authority)
    if host is None:
        return None
    return f"http://{host}:{port}/"


def proxy_suspected(headers: Mapping[str, str]) -> bool:
    """`True` when a header only a reverse proxy would add is present.

    `X-Forwarded-For` is deliberately excluded: ordinary LAN equipment adds
    it without rewriting the host Marrquee sees, and a false proxy warning
    on the owner's own NAS would be a bad first impression for nothing.
    """
    return any(name in headers for name in _PROXY_HEADER_NAMES)


def authority_from_headers(headers: Mapping[str, str]) -> str | None:
    """The address the browser used to reach Marrquee: a proxy's forwarded
    host when one is present, otherwise the plain `Host` header, otherwise
    `None`.

    The one place every page reads this, so two pages can never quietly
    disagree about which header wins.
    """
    forwarded = headers.get("x-forwarded-host")
    if forwarded:
        return forwarded
    host = headers.get("host")
    return host if host else None
