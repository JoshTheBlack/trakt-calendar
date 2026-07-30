"""Cookie policy, and the client-address policy it depends on.

These two are one job rather than two: whether the session cookie gets the
Secure flag depends on whether the browser reached this app over TLS, and
answering that behind a reverse proxy is the same trusted-peer question that
decides whose address a rate limiter counts against.
"""
from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from fastapi import Request, Response

from ..config import TRUSTED_PROXY_IPS_DEFAULT, Settings, load_settings
from . import sessions

logger = logging.getLogger(__name__)

COOKIE_NAME = "tns_session"
# The `__Host-` prefix is enforced by the browser: it requires Secure, Path=/,
# and no Domain attribute, and in exchange it stops a sibling subdomain from
# overwriting the cookie. It is only legal when the cookie really is Secure,
# hence two names rather than one.
COOKIE_NAME_SECURE = "__Host-tns_session"


def use_secure_cookie(settings: Settings, request: Request | None = None) -> bool:
    """Whether the session cookie gets the Secure flag.

    "always" is the default and does not consult the request at all. That is
    deliberate: behind a TLS-terminating reverse proxy the app itself is served
    over plain HTTP, so scheme detection reports "http" and would ship session
    cookies WITHOUT Secure on exactly the deployments that most need it. "never"
    exists for genuine plain-HTTP LAN use and "auto" for anyone who wants the
    detection anyway.
    """
    mode = (getattr(settings, "cookie_secure", "always") or "always").strip().lower()
    if mode == "never":
        return False
    if mode == "auto":
        return request_is_https(request, settings)
    return True


def browser_scheme(request: Request | None) -> str | None:
    """The scheme the BROWSER used, per its own headers — or None if it didn't say.

    This is deliberately not `request.url.scheme`, which behind a TLS-terminating
    proxy reports the plain HTTP hop between the proxy and this app rather than
    the HTTPS the browser is actually on. `Origin` carries the browser's own
    origin and is set on every mutating fetch; `Referer` covers navigations.

    Crucially it does NOT depend on `trusted_proxy_ips` being correct, which is
    what makes it usable where `X-Forwarded-Proto` is not: an instance whose
    proxy list is still at the default ignores forwarded headers entirely, and
    that is exactly the instance most likely to be misconfigured.
    """
    if request is None:
        return None
    for header in ("origin", "referer"):
        value = (request.headers.get(header) or "").strip()
        # A sandboxed or privacy-stripped Origin arrives as the literal "null".
        if not value or value.lower() == "null":
            continue
        scheme = urlsplit(value).scheme.lower()
        if scheme in ("http", "https"):
            return scheme
    return None


def detect_cookie_secure(request: Request | None) -> str:
    """The `cookie_secure` value this browser's connection calls for.

    "never" only when the browser positively reports plain HTTP. Anything else —
    HTTPS, or a request that says nothing at all — resolves to "always", because
    the cost of being wrong is asymmetric: a needlessly Secure cookie fails
    loudly and immediately during setup, while a needlessly insecure one fails
    silently and permanently for every user afterwards.
    """
    return "never" if browser_scheme(request) == "http" else "always"


def request_is_https(request: Request | None, settings: Settings) -> bool:
    """Whether the browser reached this app over TLS.

    Public because the same answer is needed when reconstructing this instance's
    own origin: behind a TLS-terminating proxy the request itself arrives over
    plain HTTP, so anything comparing against a browser-sent `Origin` has to
    resolve the scheme the same way this does or it will disagree with every
    real request.
    """
    if request is None:
        return True  # nothing to inspect: fail closed and keep Secure on
    if request.url.scheme == "https":
        return True
    if peer_is_trusted_proxy(request, settings):
        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto == "https":
            return True
    return False


def session_cookie_name(settings: Settings, request: Request | None = None) -> str:
    return COOKIE_NAME_SECURE if use_secure_cookie(settings, request) else COOKIE_NAME


def read_session_cookie(request: Request, settings: Settings | None = None) -> str | None:
    """The session id from the request, checking both cookie names so that
    flipping the Secure policy doesn't log the whole instance out."""
    cfg = settings or load_settings()
    preferred = session_cookie_name(cfg, request)
    other = COOKIE_NAME if preferred == COOKIE_NAME_SECURE else COOKIE_NAME_SECURE
    return request.cookies.get(preferred) or request.cookies.get(other)


def set_session_cookie(
    response: Response,
    session_id: str,
    settings: Settings,
    request: Request | None = None,
) -> None:
    """Issue the session cookie: HttpOnly, SameSite=Lax, Path=/, no Domain, and
    Secure (with the `__Host-` name) unless the Secure policy says otherwise.

    Max-Age is the absolute cap rather than the sliding window. The session row
    is the authority on both clocks, so a cookie that outlives its row is simply
    rejected — whereas a cookie expiring at the sliding window would log out the
    active user that sliding exists to keep signed in.
    """
    secure = use_secure_cookie(settings, request)
    response.set_cookie(
        key=COOKIE_NAME_SECURE if secure else COOKIE_NAME,
        value=session_id,
        max_age=sessions.SESSION_ABSOLUTE_SECONDS,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(
    response: Response,
    settings: Settings,
    request: Request | None = None,
) -> None:
    """Delete both cookie names, since the browser may be holding either."""
    secure = use_secure_cookie(settings, request)
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=False)
    response.delete_cookie(
        COOKIE_NAME_SECURE, path="/", httponly=True, samesite="lax", secure=secure or True,
    )


# ---------------------------------------------------------------------------
# client IP
# ---------------------------------------------------------------------------

_FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")
_warned_default_proxy = False


def parse_trusted_networks(spec: str | None) -> list[ipaddress._BaseNetwork]:
    """Parse the comma-separated CIDR list.

    An unparseable entry is dropped with a warning rather than raising: a typo in
    an admin-editable settings field must not make the instance unbootable.
    """
    networks: list[ipaddress._BaseNetwork] = []
    for raw in (spec or "").split(","):
        token = raw.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Ignoring unparseable trusted_proxy_ips entry: %r", token)
    return networks


def _is_trusted(addr: str, networks: list[ipaddress._BaseNetwork]) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def peer_is_trusted_proxy(request: Request, settings: Settings) -> bool:
    """Whether the immediate peer is inside the configured trusted-proxy set —
    i.e. whether this request's forwarded headers are honored at all. Public
    because the Settings screen reports it back to the operator."""
    peer = request.client.host if request.client else None
    if not peer:
        return False
    return _is_trusted(peer, parse_trusted_networks(getattr(settings, "trusted_proxy_ips", "")))


def client_ip(request: Request, settings: Settings | None = None) -> str:
    """The caller's IP address, honoring X-Forwarded-For only when the immediate
    peer is a configured trusted proxy.

    Walks the forwarded chain right to left, skipping hops that are themselves
    trusted, so the result is the last address the trusted infrastructure
    actually observed rather than whatever the client claimed at the front of the
    header. An untrusted peer gets its own address and its forwarded headers
    ignored entirely — trusting them would let anyone spoof an IP and slip a
    per-IP rate limit.
    """
    cfg = settings or load_settings()
    peer = (request.client.host if request.client else "") or "unknown"
    networks = parse_trusted_networks(getattr(cfg, "trusted_proxy_ips", ""))
    _maybe_warn_default_proxy(request, cfg)

    if not _is_trusted(peer, networks):
        return peer

    forwarded = request.headers.get("x-forwarded-for") or ""
    chain = [p.strip() for p in forwarded.split(",") if p.strip()]
    if not chain:
        real = (request.headers.get("x-real-ip") or "").strip()
        return real or peer
    for candidate in reversed(chain):
        if not _is_trusted(candidate, networks):
            return candidate
    # Every hop is our own infrastructure; the leftmost is the closest thing to a
    # real client address available.
    return chain[0]


def _maybe_warn_default_proxy(request: Request, settings: Settings) -> None:
    """Warn once when the trusted-proxy list is still at its default while
    forwarded headers are actually arriving.

    That combination is almost always a misconfiguration, and it fails quietly in
    a way that matters: the headers get ignored, so every user collapses onto the
    proxy's address and per-IP rate limiting silently becomes global. Forwarded
    headers only exist per request, so this fires on the first request that shows
    the combination rather than at startup.
    """
    global _warned_default_proxy
    if _warned_default_proxy:
        return
    if (getattr(settings, "trusted_proxy_ips", "") or "").strip() != TRUSTED_PROXY_IPS_DEFAULT:
        return
    if not any(h in request.headers for h in _FORWARDED_HEADERS):
        return
    peer = request.client.host if request.client else "?"
    _warned_default_proxy = True
    logger.warning(
        "Forwarded headers are present but trusted_proxy_ips is still the default %s "
        "(request peer %s). The forwarded headers are being IGNORED, so every user "
        "looks like %s to rate limiting and the admin session list. Set "
        "trusted_proxy_ips in Settings to the proxy's address/CIDR.",
        TRUSTED_PROXY_IPS_DEFAULT, peer, peer,
    )
