"""Getting a real answer out of Simkl, and nothing else.

Two pooled clients, the request headers, the pacing and retry rules Simkl's
published quota asks for, and the disk-cached GET every data call routes
through. Like the Trakt transport beside it, this module deliberately does NOT
interpret what an answer MEANS — a calendar file, a catalog record and a watch
history are all just parsed JSON here.

WHY TWO POOLS RATHER THAN ONE. Simkl publishes two different regimes and a
single pool could only honour the stricter of them. The Cloudflare-cached paths
(the CDN calendar files, the catalog and episode lookups) explicitly allow
parallel requests; everything under /sync/ and /users/ explicitly does not, and
POST there is capped at one request per second. Sizing one pool for the second
would make a month's worth of catalog lookups take minutes; sizing it for the
first would break the rule that matters. So the two regimes are two pools, and
which one a call belongs to is stated at the call rather than guessed here.
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

API_BASE = "https://api.simkl.com"

# Sent on every request. Simkl's docs ask for an application name and version
# alongside the client id so an instance misbehaving can be identified and told
# about it rather than silently blocked; the User-Agent is the same courtesy for
# anything reading raw logs.
APP_NAME = "trakt-new-shows"
APP_VERSION = "2.0"
USER_AGENT = "trakt-new-shows-py/2.0"


class SimklError(SourceUnavailable):
    """Simkl could not answer. Every caller that writes `except SimklError` is
    saying "Simkl could not answer", not "the transport layer raised".

    Its base is the app-wide "a source could not answer" contract, the same one
    TraktError derives from, so a caller reading BOTH sources can degrade
    whichever failed without an except clause per service."""


class SimklRateLimitError(SimklError):
    """Simkl returned 429 and send's retry/backoff budget was exhausted.

    "You went too fast just now" — a condition about THIS burst of requests,
    which clears on its own in seconds. A distinct type for the same reason
    TraktRateLimitError is one: a caller can degrade the one affected title or
    the one affected month deliberately, instead of treating a temporary
    slow-down as a hard failure of the source.
    """


class SimklBlockedError(SimklError):
    """Simkl returned 412 — this instance's client id is throttle-blocked.

    A DIFFERENT FAILURE FROM 429 AND IT MUST NOT BE RETRIED THE SAME WAY. A 429
    is about one burst; a 412 is Simkl saying the whole application is currently
    refused, for every user on this box at once. Retrying into it makes the
    block worse rather than clearing it, so it opens the circuit breaker below
    and is raised again, without a request, until the cooldown passes.
    """


def api_headers(settings: Settings) -> dict:
    """Simkl request headers.

    The Authorization header is added only when there is a token to send, because
    the calendar and catalog halves of this source are unauthenticated by design
    — sending an empty bearer would turn a public lookup into a rejected one.
    """
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "app-name": APP_NAME,
        "app-version": APP_VERSION,
    }
    token = (settings.simkl_access_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def api_params(settings: Settings, params: dict | None = None) -> dict:
    """`params` with this instance's client id added.

    Simkl takes the client id as a QUERY PARAMETER rather than a header, which
    means it is part of the URL — and the response cache is keyed by URL, so it
    is also part of the cache key. That is correct: a different client id is a
    different application talking to Simkl, and its answers are not this one's
    to serve.
    """
    merged = dict(params or {})
    merged["client_id"] = settings.simkl_client_id
    return merged


# ---------------------------------------------------------------------------
# The two pools, and the POST pacer that sits beside them because it answers the
# same question: how hard may this app lean on this service.
# ---------------------------------------------------------------------------

# THE CLOUDFLARE-CACHED HALF: the CDN calendar files and the catalog / episode
# lookups. Simkl's docs name these as the endpoints where parallel requests are
# allowed, because they are served from the edge rather than the origin. Sized
# for throughput — a month's window can reference several hundred distinct
# titles — while staying under the 10 GET/second ceiling that applies to every
# path regardless of caching.
CATALOG_POOL = http_pool.Pool("simkl", max_connections=8, timeout=30, concurrency=6)

# EVERYTHING UNDER /sync/ AND /users/. The docs mandate SEQUENTIAL requests off
# the cached paths, so this pool admits exactly one request at a time. Two
# connections rather than one only so a keep-alive that has gone stale does not
# stall the next call behind it.
SYNC_POOL = http_pool.Pool("simkl-sync", max_connections=2, timeout=30, concurrency=1)

# A SEMAPHORE IS NOT ENOUGH FOR THE POST CAP, which is why this exists at all.
# `concurrency=1` bounds how many requests are IN FLIGHT, not how fast they
# LEAVE: two POSTs issued back to back both go out inside the same second and
# the second one is over the published 1 POST/second limit. So a POST is held
# until at least this long after the previous one finished. The margin over a
# flat second is deliberate — the cap is enforced on Simkl's clock, not ours,
# and a request that leaves 999ms after the last one by our reckoning is a
# coin flip by theirs.
POST_MIN_INTERVAL = 1.05

# The monotonic instant the next POST may leave. Module state rather than
# per-pool state because the cap is per client id: it is one budget however many
# callers there are, and a second copy of this number would let two of them
# spend it twice. Monotonic seconds, so it survives the suite's fresh loop per
# test in a way an asyncio primitive bound at import would not.
_post_ready_at = 0.0

# 412 is an INSTANCE-WIDE refusal, so the response to it is instance-wide too:
# stop calling Simkl entirely until this deadline passes. Fifteen minutes is a
# starting point chosen to be long enough that a blocked client id is not being
# hammered while blocked, and short enough that an instance recovers without an
# operator noticing anything.
BLOCK_COOLDOWN_SECONDS = 900.0
_blocked_until = 0.0


def catalog_client() -> httpx.AsyncClient:
    """The pooled client for the Cloudflare-cached half. Callers must NOT close
    it — the pool owns its lifetime and http_pool.aclose_all closes it at
    shutdown.

    Kept as a named function rather than collapsed into CATALOG_POOL.client() so
    there is one stable seam the test suite can hand a recording double.
    """
    return CATALOG_POOL.client()


def sync_client() -> httpx.AsyncClient:
    """The pooled client for /sync/ and /users/. Same ownership rule as
    catalog_client, and deliberately a different client: sharing one would let a
    bulk catalog read hold every connection while a personal read waits."""
    return SYNC_POOL.client()


# ---------------------------------------------------------------------------
# The circuit breaker.
# ---------------------------------------------------------------------------

def _blocked_seconds_remaining() -> float:
    """How long the breaker stays open, or 0.0 when Simkl may be called."""
    return max(0.0, _blocked_until - _time.monotonic())


def _open_breaker(path: str) -> None:
    global _blocked_until
    _blocked_until = _time.monotonic() + BLOCK_COOLDOWN_SECONDS
    # WARNING, NOT DEBUG. This is a fact about the instance's relationship with
    # Simkl — every user on this box has just lost the source for a quarter of
    # an hour — and not a timing detail. The same reasoning the 429 line below
    # carries, one step more serious.
    logger.warning(
        "Simkl refused this instance's client id on %s (HTTP 412). Every Simkl "
        "call is refused locally for the next %.0fs rather than retried, because "
        "retrying into a block extends it.", path, BLOCK_COOLDOWN_SECONDS)


def _close_breaker() -> None:
    """Let Simkl be called again immediately. Nothing in the app calls this —
    the deadline is what normally closes the breaker — but a test that has just
    opened it must be able to leave the module as it found it."""
    global _blocked_until
    _blocked_until = 0.0


# ---------------------------------------------------------------------------
# The one low-level sender every Simkl call routes through.
# ---------------------------------------------------------------------------

# Bounded two ways, whichever is hit first: a small attempt count AND a
# wall-clock budget, so a viewer-initiated read returns an answer in a bounded
# window rather than hanging behind a large Retry-After. The budget is ELAPSED
# time — request time plus any backoff sleep — not cumulative sleep alone.
_SEND_MAX_ATTEMPTS = 3
_SEND_MAX_ELAPSED = 30.0


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Retry-After as a positive float, or None when it is missing, non-numeric,
    negative, or absurd. None means "no usable wait" — the caller falls back to
    its exponential step rather than sleeping on a value it cannot trust. An
    HTTP-date form simply fails the float parse and takes the same fallback."""
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


async def _pace_post() -> None:
    """Hold until this POST is allowed to leave under the 1/second cap."""
    wait = _post_ready_at - _time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)


def _post_sent() -> None:
    """Record that a POST has just been issued, so the next one waits."""
    global _post_ready_at
    _post_ready_at = _time.monotonic() + POST_MIN_INTERVAL


async def send(client: httpx.AsyncClient, method: str, url: str, *,
               pool: http_pool.Pool, headers: dict | None = None,
               json=None, timeout: float | None = None) -> httpx.Response:
    """Issue one Simkl request under that pool's rules, and return its response.

    `pool` IS NOT OPTIONAL AND MUST BE THE POOL `client` CAME FROM. The two
    travel together because they are two halves of one budget: the client owns
    the connections and the pool owns the gate that decides how many requests
    may be in flight through them. Passing one regime's client with the other's
    pool would honour neither rule, so the pairing is stated at every call site
    instead of being defaulted here.

    Returns the httpx.Response for any status this function does not act on, and
    lets network errors propagate unchanged — reading a 401 or an empty body is
    the caller's job. Three statuses are acted on:

      412  the client id is blocked instance-wide. Opens the breaker and raises
           SimklBlockedError. NEVER retried: see SimklBlockedError.
      429  backs off — a numeric Retry-After wins over the exponential schedule
           (1s, 2s, 4s) — and raises SimklRateLimitError once the attempt count
           or the wall-clock budget is spent, rather than a fabricated response.
      anything else, returned untouched.

    A POST is additionally paced to one per second (see POST_MIN_INTERVAL).
    """
    path = url.split("?", 1)[0].replace(API_BASE, "") or url
    remaining_block = _blocked_seconds_remaining()
    if remaining_block > 0:
        # Refused HERE, before the gate and before any socket: the whole point
        # of the breaker is that this request never reaches Simkl.
        raise SimklBlockedError(
            f"Simkl has blocked this instance's client id; not calling {path} for "
            f"another {remaining_block:.0f}s.", 412)
    method_up = method.upper()
    start = _time.monotonic()
    # The gate is held across the request AND the backoff sleep, not just the
    # request: during a 429 storm this makes callers wait their turn instead of
    # every coroutine re-firing the instant a slot frees and tripping the limit
    # again.
    async with pool.gate():
        attempt = 0
        while True:
            attempt += 1
            remaining = _SEND_MAX_ELAPSED - (_time.monotonic() - start)
            if remaining <= 0:
                raise SimklRateLimitError(
                    f"Simkl rate limit not cleared within {_SEND_MAX_ELAPSED:.0f}s for {path}.", 429)
            attempt_timeout = remaining if timeout is None else min(timeout, remaining)
            if method_up == "GET":
                resp = await client.get(url, headers=headers, timeout=attempt_timeout)
            elif method_up == "POST":
                await _pace_post()
                try:
                    resp = await client.post(url, headers=headers, json=json,
                                             timeout=attempt_timeout)
                finally:
                    # Recorded even when the request failed. The cap counts
                    # requests Simkl received, and a call that timed out on our
                    # side may well have arrived on theirs.
                    _post_sent()
            else:
                resp = await client.request(method, url, headers=headers, json=json,
                                            timeout=attempt_timeout)
            if resp.status_code == 412:
                _open_breaker(path)
                raise SimklBlockedError(
                    f"Simkl refused this instance's client id on {path} (HTTP 412).", 412)
            if resp.status_code != 429:
                return resp
            if attempt >= _SEND_MAX_ATTEMPTS:
                raise SimklRateLimitError(
                    f"Simkl still rate-limiting after {attempt} attempt(s) for {path}.", 429)
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = float(2 ** (attempt - 1))  # 1s, 2s, 4s — one storm's worth
            # Don't begin a sleep that would carry elapsed past the budget: stop
            # and raise now rather than sleeping most of the way in and raising
            # anyway.
            if (_time.monotonic() - start) + wait > _SEND_MAX_ELAPSED:
                logger.warning("Simkl rate-limited %s with a %.0fs Retry-After, over the "
                               "%.0fs budget — giving up on this call", path, wait, _SEND_MAX_ELAPSED)
                raise SimklRateLimitError(
                    f"Simkl Retry-After would exceed the {_SEND_MAX_ELAPSED:.0f}s budget for {path}.", 429)
            # WARNING, NOT DEBUG, AND ALWAYS. Being rate-limited is the
            # difference between "that read was slow" and "we were told to slow
            # down", and those have opposite fixes. Rare by construction — the
            # pools exist to keep it from happening — so a run of these lines is
            # itself the signal that a pool is sized wrong.
            logger.warning("Simkl rate-limited %s — attempt %d, waiting %.1fs before retry",
                           path, attempt, wait)
            await asyncio.sleep(wait)


async def _fetch_json(client: httpx.AsyncClient, settings: Settings, url: str, path: str,
                      pool: http_pool.Pool, fresh: bool, raise_errors: bool):
    """One GET, reduced to "the parsed body, or None". No caching.

    Split out of cached_get so that function is only the CACHE POLICY and this
    one is only the call and what its answer means. The two change for different
    reasons: a new caching mode touches the policy alone, and a change in how
    Simkl reports a failure touches this alone.
    """
    t0 = _time.perf_counter()
    try:
        resp = await send(client, "GET", url, pool=pool, headers=api_headers(settings))
    except httpx.HTTPError as exc:
        # A transport failure means we never got a real answer. Unlike a 404 or
        # an empty list that is NOT "Simkl says there is nothing here", so it
        # must never collapse into the None callers read as an empty result.
        # Always raise, regardless of raise_errors. (SimklRateLimitError and
        # SimklBlockedError are not httpx errors and propagate on their own, for
        # the same reason — their callers degrade them deliberately.)
        logger.warning("Simkl GET %s failed: %s", path, exc)
        raise SimklError(f"Could not reach Simkl: {exc}") from exc
    _perf.debug("netGET    %s -> %s  %.0fms%s", path, resp.status_code,
                (_time.perf_counter() - t0) * 1000.0, " (fresh)" if fresh else " (miss)")
    if resp.status_code != 200:
        logger.warning("Simkl GET %s -> HTTP %s: %s", path, resp.status_code, resp.text[:200])
        if raise_errors:
            if resp.status_code == 401:
                raise SimklError(
                    "Simkl rejected the credentials (401). Simkl issues no refresh "
                    "token, so the link has to be made again.", 401)
            raise SimklError(f"Simkl API returned HTTP {resp.status_code}.", resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        logger.warning("Simkl GET %s -> unreadable JSON body", path)
        if raise_errors:
            raise SimklError("Simkl API returned an unreadable response.")
        return None


async def cached_get(
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    params: dict | None = None,
    *,
    pool: http_pool.Pool,
    ttl_seconds: int | None = None,
    fresh: bool = False,
    raise_errors: bool = False,
    private: bool = False,
    cache_only: bool = False,
):
    """GET a Simkl path (with disk caching keyed by the full URL). Returns parsed
    JSON or None.

    `private=True` MEANS THE RESPONSE DEPENDS ON WHOSE TOKEN ASKED — a watch
    history, a library bucket, an activity beacon. The cache is keyed by URL and
    shared by the whole instance, and Simkl carries the token in a HEADER, so
    every user's /sync/ request has the IDENTICAL URL: a response written to the
    cache without this flag would be served back to the wrong person. Every call
    under /sync/ and /users/ passes it. Nothing that reads personal data may
    reach this function without it.

    POST RESPONSES ARE NEVER CACHED AT ALL, which is why this function is GET
    only: /sync/watched and the search endpoints are POSTs whose meaning is in
    the request BODY, and a URL key cannot express that.

    `ttl_seconds` overrides the default detail TTL; `fresh=True` skips the cache
    read but still refreshes it; `raise_errors=True` raises SimklError instead of
    returning None, for callers where a swallowed 401 would look identical to a
    genuine empty result.

    `cache_only=True` NEVER makes a network call: it returns the cached value —
    even past its TTL — or None. This is what lets a public page reuse data the
    owner's own views already fetched without a stranger being able to make this
    instance spend its Simkl budget on demand.
    """
    url = f"{API_BASE}/{path}?{urlencode(api_params(settings, params))}"
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_minutes * 60
    if not fresh and not private:
        cached = await cache.get(url, ttl)
        if cached is not None:
            _perf.debug("cacheHIT  %s", path)
            return cached
    if cache_only:
        # Stale beats blank here: this caller can never trigger a refresh to fix
        # a hard miss anyway.
        return await cache.get_stale(url)
    data = await _fetch_json(client, settings, url, path, pool,
                             fresh=fresh, raise_errors=raise_errors)
    if data is None:
        # None is how a swallowed failure comes back, and it is also what a
        # literal `null` body would parse to. Neither is worth storing: the read
        # above treats a cached None as a MISS, so such a row could never be
        # served as a hit anyway.
        return None
    if not private:
        await cache.set(url, data)
    return data
