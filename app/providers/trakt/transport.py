"""Getting a real answer out of Trakt, and nothing else.

The pooled client, the request headers, the 429 retry/backoff loop, and the
disk-cached GET every data-API call routes through. Deliberately does NOT
interpret what an answer MEANS — a calendar month, a progress record and a
search result are all just parsed JSON here, and the module that asked for one
is the module that knows how to read it.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from urllib.parse import urlencode

import httpx

from ... import cache
from ...config import Settings

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

API_BASE = "https://api.trakt.tv"


class TraktError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TraktRateLimitError(TraktError):
    """Trakt returned 429 and _send's retry/backoff budget was exhausted.

    A DISTINCT type (not a generic retryable flag) so callers can tell "Trakt is
    rate-limiting us" apart from every other failure and react deliberately: the
    distrakt fan-out degrades the one affected show to an explicit "unavailable,
    try refreshing" state, and a shared-prerequisite hit degrades the whole month
    to its last-known totals plus a notice — never a fabricated 0/0 rendered as
    truth, never a hard 500. Subclasses TraktError, so an existing
    `except TraktError` still catches it (e.g. the calendar read path, which
    correctly degrades to its stale cached window)."""


def _headers(settings: Settings, paginate: bool = True) -> dict:
    """Trakt request headers. `paginate=False` OMITS the X-Pagination-* headers.

    This matters for /sync/watched/shows: sending pagination headers switches it
    into a PAGINATED, show-level response (100/page) that DROPS the nested
    seasons[]/episodes[] breakdown we count — which manifested as every watched
    count coming back 0. Non-paginated, it returns the full watched library WITH
    seasons in one call."""
    headers = {
        "Authorization": f"Bearer {settings.trakt_access_token}",
        "trakt-api-version": "2",
        "trakt-api-key": settings.trakt_client_id,
        "Content-Type": "application/json",
        "User-Agent": "trakt-new-shows-py/2.0",
    }
    if paginate:
        headers["X-Pagination-Page"] = "1"
        headers["X-Pagination-Limit"] = str(settings.pagination_limit)
    return headers


# One httpx.AsyncClient reused for the whole app lifetime. Constructing a client
# is expensive on Windows (~250-290ms loading the SSL trust store) and each new
# one re-does the DNS/TLS handshake, so per-call/per-batch clients dominated the
# distrakt load. A single shared, connection-pooled client pays that cost ONCE.
# Keyed by event loop so test isolation (fresh loop per test) can't reuse a client
# bound to a dead loop.
_POOL_LIMIT = 8
_shared: dict = {"loop": None, "client": None}


def shared_client() -> httpx.AsyncClient:
    """The app-wide pooled Trakt client (created lazily on first use, on the
    current running loop). Callers must NOT close it — see aclose_shared_client."""
    loop = asyncio.get_event_loop()
    client = _shared["client"]
    if client is None or client.is_closed or _shared["loop"] is not loop:
        limits = httpx.Limits(max_connections=_POOL_LIMIT, max_keepalive_connections=_POOL_LIMIT)
        _shared["client"] = httpx.AsyncClient(timeout=30, limits=limits)
        _shared["loop"] = loop
    return _shared["client"]


async def aclose_shared_client() -> None:
    """Close the shared client (call on app shutdown)."""
    client = _shared["client"]
    _shared["client"] = None
    _shared["loop"] = None
    if client is not None and not client.is_closed:
        await client.aclose()


# ---------------------------------------------------------------------------
# The one low-level sender every Trakt data-API call routes through: transport +
# the 429 retry/backoff loop, and NOTHING else. It deliberately does not
# interpret any non-429 status — a 200/401/404 comes straight back for the caller
# to judge, exactly as a bare client.get would — so the SRP split between "get a
# real answer out of Trakt" and "decide what that answer means" is preserved.
# ---------------------------------------------------------------------------

# One _send call is bounded two ways, whichever it hits first: a small attempt
# count AND a wall-clock budget. A user-initiated Refresh must return an answer in
# a bounded window even when Trakt hands back a large Retry-After (its own docs
# cite a 254s example from the wild) rather than hang silently. The budget is
# ELAPSED time — request time PLUS any backoff sleep — not cumulative sleep alone,
# so three attempts each near the client timeout can't quietly run past it.
_SEND_MAX_ATTEMPTS = 3
_SEND_MAX_ELAPSED = 30.0

# Smooth the distrakt Refresh fan-out's opening burst. A large roster fires ~2
# requests per tracked show all at once; the connection pool caps how many are
# in flight but not how fast they leave, so hundreds can go out in the same few
# milliseconds — well over the 1000-per-5-minute average — before any 429 comes
# back to self-correct. This gate paces that burst without serializing it, sized
# well under _POOL_LIMIT. The retry/backoff loop is the real defense against the
# 5-minute window; this just keeps the opening spike from tripping it needlessly.
# Loop-keyed for the SAME reason shared_client() is: a Semaphore created at import
# time binds to whatever loop is current then and raises "bound to a different
# event loop" under the test suite's fresh-loop-per-test isolation.
_SEND_CONCURRENCY = 4
_send_sem: dict = {"loop": None, "sem": None}


def _rate_limit_semaphore() -> asyncio.Semaphore:
    """The app-wide outbound-request gate, created lazily on the running loop."""
    loop = asyncio.get_event_loop()
    sem = _send_sem["sem"]
    if sem is None or _send_sem["loop"] is not loop:
        sem = asyncio.Semaphore(_SEND_CONCURRENCY)
        _send_sem["sem"] = sem
        _send_sem["loop"] = loop
    return sem


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Trakt's Retry-After (documented as a plain count of seconds) as a positive
    float, or None when it is missing, non-numeric, negative, or absurd.

    None means "no usable wait" — the caller falls back to its exponential step
    rather than sleep(None), a negative sleep, or an hour-long one. Trakt does not
    use the HTTP-date form on this header, but an upstream security layer (e.g.
    Cloudflare) can send a 429 shaped differently; a date string just fails the
    float parse and takes the same fallback."""
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        return None
    if secs <= 0 or secs > 86400:
        return None
    return secs


async def _send(client: httpx.AsyncClient, method: str, url: str, *,
                headers: dict | None = None, json=None, timeout: float | None = None) -> httpx.Response:
    """Issue one Trakt request, retrying only on 429 within a bounded budget.

    Returns the httpx.Response for ANY non-429 status untouched, and lets network
    errors propagate unchanged — interpreting a 401/404/empty body is the caller's
    job, not this function's. On 429 it honors a numeric Retry-After when present
    (it wins over the exponential schedule), else backs off exponentially (1s, 2s,
    4s), retrying up to _SEND_MAX_ATTEMPTS within the _SEND_MAX_ELAPSED wall-clock
    budget. On exhaustion it raises TraktRateLimitError — never a fabricated
    response, never a bare None — so an unanswered request can't masquerade as a
    real one.

    `timeout` lets a caller keep a tighter per-request bound (the OAuth calls pass
    their own 15s); it is further clamped to the budget remaining so a single hung
    attempt can't blow the wall-clock cap."""
    path = url.split("?", 1)[0].replace(API_BASE, "") or url
    sem = _rate_limit_semaphore()
    start = _time.monotonic()
    # Hold the gate across the request AND the backoff sleep, not just the request:
    # during a 429 storm this makes the fan-out wait its turn instead of every
    # coroutine re-firing the instant a slot frees and tripping the limit again.
    async with sem:
        attempt = 0
        while True:
            attempt += 1
            remaining = _SEND_MAX_ELAPSED - (_time.monotonic() - start)
            if remaining <= 0:
                raise TraktRateLimitError(
                    f"Trakt rate limit not cleared within {_SEND_MAX_ELAPSED:.0f}s for {path}.", 429)
            attempt_timeout = remaining if timeout is None else min(timeout, remaining)
            # Dispatch to get/post (the shape every call site used before this
            # sender existed) rather than client.request, so the per-attempt timeout
            # bounds the request without changing how the call is issued.
            method_up = method.upper()
            if method_up == "GET":
                resp = await client.get(url, headers=headers, timeout=attempt_timeout)
            elif method_up == "POST":
                resp = await client.post(url, headers=headers, json=json, timeout=attempt_timeout)
            else:
                resp = await client.request(method, url, headers=headers, json=json, timeout=attempt_timeout)
            if resp.status_code != 429:
                return resp
            if attempt >= _SEND_MAX_ATTEMPTS:
                raise TraktRateLimitError(
                    f"Trakt still rate-limiting after {attempt} attempt(s) for {path}.", 429)
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = float(2 ** (attempt - 1))  # 1s, 2s, 4s — one storm's worth
            # Don't begin a sleep that would carry elapsed past the budget: stop and
            # raise now rather than sleeping most of the way in and raising anyway.
            if (_time.monotonic() - start) + wait > _SEND_MAX_ELAPSED:
                raise TraktRateLimitError(
                    f"Trakt Retry-After would exceed the {_SEND_MAX_ELAPSED:.0f}s budget for {path}.", 429)
            _perf.debug("netRETRY  %s attempt=%d wait=%.1fs (429)", path, attempt, wait)
            await asyncio.sleep(wait)


async def _fetch_json(client: httpx.AsyncClient, settings: Settings, url: str, path: str,
                      fresh: bool, raise_errors: bool):
    """One GET, reduced to "the parsed body, or None". No caching.

    Split out of _cached_get so that function is only the CACHE POLICY —
    what to read, what to serve stale, what to write — and this one is only the
    call and what its answer means. The two change for different reasons: a new
    caching mode (cache_only was one) touches the policy alone, and a change in
    how Trakt reports a failure touches this alone.
    """
    t0 = _time.perf_counter()
    try:
        resp = await _send(client, "GET", url, headers=_headers(settings))
    except httpx.HTTPError as exc:
        # A transport failure means we never got a real answer from Trakt. Unlike a
        # 404 or an empty list, that is NOT "Trakt says there's nothing here", so it
        # must never collapse into the None that callers read as an empty result —
        # a season would then render a false 0 episodes, a progress record a false
        # 0 watched. Always raise, regardless of raise_errors. (An exhausted-retry
        # TraktRateLimitError from _send is not an httpx error and propagates on its
        # own for the same reason — the affected caller degrades it deliberately.)
        logger.warning("Trakt GET %s failed: %s", path, exc)
        raise TraktError(f"Could not reach Trakt: {exc}") from exc
    _perf.debug("netGET    %s -> %s  %.0fms%s", path, resp.status_code,
                (_time.perf_counter() - t0) * 1000.0, " (fresh)" if fresh else " (miss)")
    if resp.status_code != 200:
        logger.warning("Trakt GET %s -> HTTP %s: %s", path, resp.status_code, resp.text[:200])
        if raise_errors:
            if resp.status_code == 401:
                raise TraktError("Trakt rejected the credentials (401). Check Client ID / Access Token in Settings.", 401)
            raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("Trakt GET %s -> unreadable JSON body", path)
        if raise_errors:
            raise TraktError("Trakt API returned an unreadable response.")
        return None


async def _cached_get(
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    params: dict,
    ttl_seconds: int | None = None,
    fresh: bool = False,
    raise_errors: bool = False,
    private: bool = False,
    cache_only: bool = False,
):
    """GET a Trakt path (with disk caching keyed by path+params). Returns parsed JSON or None.

    `ttl_seconds` overrides the default detail TTL (used for the short-lived
    distrakt season calls); `fresh=True` skips the cache read but still refreshes it.
    `raise_errors=True` raises TraktError instead of silently returning None — used
    by callers (search, seasons) where a swallowed 401 previously looked identical
    to a genuine "no results" response, making auth failures invisible in the UI.

    `cache_only=True` NEVER makes a network call: it returns the cached value —
    even if it's past its TTL — or None if there's no row at all. This is what
    lets a public share page reuse detail data the OWNER's own views already
    fetched and cached, without a public request ever spending the owner's Trakt
    rate limit — the read-only-cache half of the calendar cache's
    allow_fetch=False rule, applied to the detail lookups. Serving stale here
    mirrors calendar_cache.load_window's `not allow_fetch` bypass: a share visitor
    can never trigger a refresh, so stale-but-real data beats a blank card.

    `private=True` means the RESPONSE DEPENDS ON WHOSE TOKEN ASKED — a watch
    history, a progress record, an activity beacon. The cache is keyed by URL and
    shared by the whole instance, so such a response must never be written to it:
    two people asking for the same show's progress send the identical URL and
    would otherwise overwrite, and potentially be served, each other's viewing.
    """
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_minutes * 60
    if not fresh and not private:
        cached = await cache.get(url, ttl)
        if cached is not None:
            _perf.debug("cacheHIT  %s", path)  # DEBUG: 1 line/season, noisy on warm loads
            return cached
    if cache_only:
        # A public share request: never reach for the network. Fall back to
        # whatever's cached even past its TTL — stale beats a blank card, and
        # this caller can never trigger a refresh to fix a hard miss anyway.
        return await cache.get_stale(url)
    data = await _fetch_json(client, settings, url, path, fresh=fresh, raise_errors=raise_errors)
    if data is None:
        # None is how a swallowed failure comes back, and it is also what a
        # literal `null` body would parse to. Neither is worth storing: the read
        # above treats a cached None as a MISS, so such a row could never be
        # served as a hit anyway.
        return None
    if not private:
        await cache.set(url, data)
    return data
