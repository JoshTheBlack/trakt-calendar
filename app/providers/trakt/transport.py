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
from ... import http_pool
from ...config import Settings
from ..base import SourceUnavailable

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

API_BASE = "https://api.trakt.tv"


class TraktError(SourceUnavailable):
    """Trakt could not answer.

    Its base is the app-wide "a source could not answer" contract, so a caller
    reading several sources can degrade whichever one failed without naming this
    one. Every `except TraktError` already written keeps its exact meaning.
    """


class TraktRateLimitError(TraktError):
    """Trakt returned 429 and send's retry/backoff budget was exhausted.

    A DISTINCT type (not a generic retryable flag) so callers can tell "Trakt is
    rate-limiting us" apart from every other failure and react deliberately: the
    distrakt fan-out degrades the one affected show to an explicit "unavailable,
    try refreshing" state, and a shared-prerequisite hit degrades the whole month
    to its last-known totals plus a notice — never a fabricated 0/0 rendered as
    truth, never a hard 500. Subclasses TraktError, so an existing
    `except TraktError` still catches it (e.g. the calendar read path, which
    correctly degrades to its stale cached window)."""


# The statuses that are about WHO ASKED rather than about WHAT WAS ASKED FOR.
# Trakt answers 401 for a token it will not accept and 403 for one it accepts but
# will not honour, and neither is a property of the path that happened to be
# called first: the same token on any other path gets the same answer. A caller
# that tolerates ONE call failing — the per-show progress fan-out does, so that a
# single missing show does not sink a whole re-baseline — must NOT tolerate one of
# these, because "this show could not be read" and "nothing this token asks for
# can be read" are different facts with opposite consequences. Tolerating the
# second once per show tolerates it a hundred and forty-six times and calls the
# result a read of an empty library, which is how a viewer's stored counts get
# replaced with "watched nothing".
CREDENTIAL_STATUSES = (401, 403)


def is_credential_failure(error: TraktError) -> bool:
    """True when this failure says the CREDENTIAL is not usable.

    Lives here because the statuses are Trakt's, and reading them is what this
    module is for; a caller asks the question rather than comparing numbers of
    its own, so the answer has one place to change.
    """
    return error.status in CREDENTIAL_STATUSES


def api_headers(settings: Settings, paginate: bool = True) -> dict:
    """Trakt request headers. `paginate=False` OMITS the X-Pagination-* headers.

    THERE IS NO CHEAP WHOLE-LIBRARY READ BEHIND `paginate=False`, and this
    docstring used to claim there was. It said that omitting the pagination
    headers made /sync/watched/shows return "the full watched library WITH
    seasons in one call". Measured against the live API, that is false in every
    variant that could plausibly matter: with the headers and without them, with
    `extended=full`, with `extended=noseasons`, with `limit=2000` and with
    `page=1&limit=2000`, the response is the same 250 show-level rows carrying
    `plays`, `last_watched_at`, `last_updated_at`, `reset_at` and the show — and
    `seasons[]` on not one of them. The endpoint paginates unconditionally and
    caps a page at 250. `extended=noseasons` behaving identically to the default
    is the control that says the seasons are already absent rather than being
    stripped by something sent here.

    So per-season completion comes from one /shows/{id}/progress/watched call per
    show and from nothing else (see sync.fetch_watched_map, whose docstring has
    always been the accurate one, and sync.fetch_play_counts, which is what the
    show-level rows are actually good for: learning WHICH shows changed).

    What `paginate=False` still does is what its name says — leave the
    X-Pagination-* request headers off, for the calls that page through query
    parameters instead or that want the whole response in one piece.
    """
    headers = {
        "trakt-api-version": "2",
        "trakt-api-key": settings.trakt_client_id,
        "Content-Type": "application/json",
        "User-Agent": "trakt-new-shows-py/2.0",
    }
    # THE BEARER IS OMITTED, NOT EMPTIED, WHEN THERE IS NO TOKEN. An empty bearer
    # is not an anonymous request, it is an invalid one: httpx refuses to send the
    # literal value "Bearer " and raises `Illegal header value b'Bearer '` before
    # a socket is opened. That failed EVERY Trakt call for an account with no
    # token, including the public catalogue lookups in detail.py — a season's
    # episode list, a title's overview and cast — which authenticate with the
    # `trakt-api-key` header above (the INSTANCE's client id) and want no bearer
    # at all. Only the per-person reads under /sync/ need one, and a caller with
    # no token was never going to get a useful answer out of those anyway; it now
    # gets Trakt's own 401 instead of a client-side crash.
    token = settings.trakt_access_token.strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if paginate:
        headers["X-Pagination-Page"] = "1"
        headers["X-Pagination-Limit"] = str(settings.pagination_limit)
    return headers


# TRAKT'S OWN POOL. Constructing a client re-does the SSL trust store load and
# the DNS/TLS handshake, so per-call clients dominated the distrakt load; one
# pooled client pays that once. The pooling itself now comes from
# app/http_pool.py, because every other outbound service needs the same thing and
# two of them had taken to borrowing THIS client rather than declaring their own.
#
# SEPARATE FROM EVERY OTHER SERVICE'S POOL, which is the point of per-service
# pools: TMDB image downloads are slow and bursty, and when they shared these
# eight connections a poster warm could leave a calendar refresh queued behind it.
_POOL_LIMIT = 8

# Smooth the distrakt Refresh fan-out's opening burst. A large roster fires ~2
# requests per tracked show all at once; the connection pool caps how many are
# in flight but not how fast they leave, so hundreds can go out in the same few
# milliseconds — well over the 1000-per-5-minute average — before any 429 comes
# back to self-correct. This gate paces that burst without serializing it, sized
# well under _POOL_LIMIT. The retry/backoff loop is the real defense against the
# 5-minute window; this just keeps the opening spike from tripping it needlessly.
# IT LIVES ON THE POOL because it is the same kind of fact — how hard may this
# app lean on this one service — and declaring it here keeps it beside the
# connection limit it is deliberately sized under. Trakt's QUOTA is Trakt's
# business; the 429 handling below stays here for the same reason.
_SEND_CONCURRENCY = 4

POOL = http_pool.Pool("trakt", max_connections=_POOL_LIMIT, timeout=30,
                      concurrency=_SEND_CONCURRENCY)


def shared_client() -> httpx.AsyncClient:
    """The pooled Trakt client (created lazily on first use, on the current
    running loop). Callers must NOT close it — see aclose_shared_client.

    Kept as a function rather than collapsed into POOL.client(): this is the seam
    the test suite patches to hand Trakt calls a recording double, and a name two
    dozen tests reach for is worth keeping stable.
    """
    return POOL.client()


async def aclose_shared_client() -> None:
    """Close the Trakt client (call on app shutdown)."""
    await POOL.aclose()


# ---------------------------------------------------------------------------
# The one low-level sender every Trakt data-API call routes through: transport +
# the 429 retry/backoff loop, and NOTHING else. It deliberately does not
# interpret any non-429 status — a 200/401/404 comes straight back for the caller
# to judge, exactly as a bare client.get would — so the SRP split between "get a
# real answer out of Trakt" and "decide what that answer means" is preserved.
# ---------------------------------------------------------------------------

# One send call is bounded two ways, whichever it hits first: a small attempt
# count AND a wall-clock budget. A user-initiated Refresh must return an answer in
# a bounded window even when Trakt hands back a large Retry-After (its own docs
# cite a 254s example from the wild) rather than hang silently. The budget is
# ELAPSED time — request time PLUS any backoff sleep — not cumulative sleep alone,
# so three attempts each near the client timeout can't quietly run past it.
_SEND_MAX_ATTEMPTS = 3
_SEND_MAX_ELAPSED = 30.0

def _rate_limit_semaphore() -> asyncio.Semaphore:
    """Trakt's outbound-request gate — see _SEND_CONCURRENCY, where it is sized.

    Created lazily on the running loop, which the pool handles: a Semaphore bound
    at import time raises "bound to a different event loop" under the suite's
    fresh-loop-per-test isolation.
    """
    return POOL.gate()


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


async def send(client: httpx.AsyncClient, method: str, url: str, *,
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
                # Logged as well as raised, because this branch can fire on the
                # FIRST attempt — a large Retry-After blows the budget before the
                # retry line below has said anything — and the caller may well
                # degrade the failure into a quiet "unavailable" on one title.
                logger.warning("Trakt rate-limited %s with a %.0fs Retry-After, over the "
                               "%.0fs budget — giving up on this call", path, wait, _SEND_MAX_ELAPSED)
                raise TraktRateLimitError(
                    f"Trakt Retry-After would exceed the {_SEND_MAX_ELAPSED:.0f}s budget for {path}.", 429)
            # WARNING, NOT DEBUG, AND ALWAYS. Being rate-limited is a fact about
            # the instance's relationship with Trakt, not a timing detail: it is
            # the difference between "that fan-out was slow" and "we were told to
            # slow down", and those have opposite fixes. It stayed invisible at
            # DEBUG through exactly the case that needed it — the same 77-show
            # re-baseline taking eight seconds once and under three the next time,
            # with nothing in the log to say which explanation was right.
            # Rare by construction: the concurrency gate above exists to keep this
            # from happening, so a run of these lines is itself the signal that the
            # gate is sized wrong.
            logger.warning("Trakt rate-limited %s — attempt %d, waiting %.1fs before retry",
                           path, attempt, wait)
            await asyncio.sleep(wait)


async def _fetch_json(client: httpx.AsyncClient, settings: Settings, url: str, path: str,
                      fresh: bool, raise_errors: bool):
    """One GET, reduced to "the parsed body, or None". No caching.

    Split out of cached_get so that function is only the CACHE POLICY —
    what to read, what to serve stale, what to write — and this one is only the
    call and what its answer means. The two change for different reasons: a new
    caching mode (cache_only was one) touches the policy alone, and a change in
    how Trakt reports a failure touches this alone.
    """
    t0 = _time.perf_counter()
    try:
        resp = await send(client, "GET", url, headers=api_headers(settings))
    except httpx.HTTPError as exc:
        # A transport failure means we never got a real answer from Trakt. Unlike a
        # 404 or an empty list, that is NOT "Trakt says there's nothing here", so it
        # must never collapse into the None that callers read as an empty result —
        # a season would then render a false 0 episodes, a progress record a false
        # 0 watched. Always raise, regardless of raise_errors. (An exhausted-retry
        # TraktRateLimitError from send is not an httpx error and propagates on its
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


async def cached_get(
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
    mirrors app/calendar/cache.py's load_window `not allow_fetch` bypass: a share visitor
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
