"""Getting a real answer out of TMDB, and nothing else.

The pooled client, the credential, the 429 circuit, and the disk-cached GET every
catalogue read routes through. Like the Trakt and Simkl transports beside it, it
deliberately does NOT interpret what an answer MEANS — a show, a season and a
search page are all parsed JSON here, and normalize.py is the one reader of each.

EVERY REQUEST HERE IS A PUBLIC CATALOGUE READ, authenticated with the INSTANCE's
read token, and every answer is the same whoever asked — so every answer caches
in the shared, URL-keyed response cache (CLAUDE.md's response-cache invariant).
There is no `private` flag because nothing here reads a person's data. A read
made with a VIEWER's TMDB token is a different thing and must not ride
`cached_get`: two people asking for their own watchlist send the same URL.

THE TOKEN TRAVELS IN THE Authorization HEADER AND NOWHERE ELSE. Not in a query
string (where it reaches access logs and the cache key), not in a log line, not
in an exception message, not in a perftrace span. TMDB also accepts a v3 key as
`?api_key=`, and this module will not send one: the Settings field refuses it
(app/config.py's `tmdb_token_error`) and `Settings.tmdb_configured` reads a
stored one as unset.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from urllib.parse import urlencode

import httpx

from ... import cache, db
from ... import http_pool
from ...config import Settings
from ...perftrace import span
from ..base import SourceUnavailable

logger = logging.getLogger(__name__)

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

# HOW LONG ANYTHING OBTAINED FROM TMDB MAY BE KEPT. TMDB's API terms (section
# 1.C) say an app may not "cache, for longer than 6 months, any information
# obtained through or from TMDB". A CEILING, not a preference, and it binds every
# store downstream of this client: the response cache, the poster and logo tiles,
# the poster registry's TMDB rows. Each of those reads THIS constant rather than
# restating six months, so the ceiling has one place to change.
MAX_CACHE_SECONDS = 180 * 24 * 60 * 60

# WHAT A TMDB ROW IN THE RESPONSE CACHE IS WRITTEN WITH, so it is gone by the
# ceiling. The sweep (app/cache.py) deletes a row at its retain plus its own
# grace period, so a row written with the plain ceiling would outlive it by the
# grace — three months past what the terms allow. A row still in use is
# re-stamped by every refresh and 304, which is re-obtaining it, so the clock
# only ever runs on rows nothing has asked TMDB about for this long.
CACHE_RETAIN_SECONDS = MAX_CACHE_SECONDS - cache.TTL_GRACE_SECONDS

# HOW LONG TMDB'S "NOT FOUND" IS BELIEVED before it is asked again: a day, the
# same re-ask clock the Trakt and Simkl lookups by shared id keep. A stale tmdb
# id on a card or a tracker row would otherwise cost a request on every panel
# open and every tracker pass, to learn the same 404. A title TMDB adds tomorrow
# is found tomorrow.
NOT_FOUND_SECONDS = 24 * 60 * 60

# How long a catalogue answer is served before it is revalidated. TMDB's own
# responses say `cache-control: public, max-age` of 1.3-6.6 hours (measured
# 2026-10-21); six hours sits inside that, and a revalidation is a conditional
# GET that costs TMDB no body when nothing moved.
DEFAULT_TTL_SECONDS = 6 * 60 * 60

# HOW HARD THIS APP LEANS ON TMDB. TMDB publishes no number — its rate-limiting
# page says the ceiling is "somewhere in the 40 requests per second range" and
# may change, and asks callers to respect the 429. MEASURED 2026-10-22 with the
# real detail calls this package makes: 1,000 cold at 20 in flight ran at 40 a
# second and 600 more at 48, with no 429 and a slow tail near two seconds; a
# warm rerun reached 124 a second, still with no 429. Twenty in flight is
# therefore the bound, and the 429 circuit below is what answers TMDB if that
# ever stops being true.
#
# ONE POOL FOR THE API AND THE IMAGE CDN. httpx pools per host internally, so
# api.themoviedb.org and image.tmdb.org already get connections of their own out
# of it. Only API calls take the gate: an image download is not an API request
# and TMDB's limit is not about them.
_CONCURRENCY = 20
POOL = http_pool.Pool("tmdb", max_connections=_CONCURRENCY, timeout=20,
                      concurrency=_CONCURRENCY)

# The longest a request will wait for the 429 circuit to close before giving up
# on itself. A page asking TMDB must still answer in bounded time; a background
# job asks again on its next pass.
_MAX_WAIT_SECONDS = 10.0
# What an open circuit lasts when TMDB's 429 names no Retry-After, and the most
# any Retry-After is believed: a header asking for an hour is a misconfigured
# edge, not a reason to stop describing titles for an hour.
_DEFAULT_RETRY_AFTER = 2.0
_MAX_RETRY_AFTER = 60.0


class TmdbError(SourceUnavailable):
    """TMDB could not answer. The app-wide "a source could not answer" contract,
    so a caller reading several services degrades this one without naming it."""


class TmdbRateLimited(TmdbError):
    """TMDB answered 429, or the circuit a 429 opened is still open past what
    this request may wait. Raised rather than retried in a loop: a burst of
    callers each retrying on their own is the storm the circuit exists to stop."""


class TmdbNotFound(Exception):
    """TMDB answered 404: it has no such title, season or page.

    DELIBERATELY NOT A TmdbError. "TMDB could not be asked" and "TMDB was asked
    and has nothing" are different facts with different consequences — a season
    TMDB does not hold is handed to the next service (detail_source.for_season),
    while a TMDB that is down leaves the row on its last-known numbers. Deriving
    this from SourceUnavailable would let a caller's `except SourceUnavailable`
    swallow the one answer it most needs to act on.
    """


class _Circuit:
    """When the next request may leave, after a 429.

    A WALL-CLOCK FACT, held as a monotonic deadline and nothing bound to an event
    loop: TMDB's "slow down" is about real seconds whichever loop asked.
    """

    __slots__ = ("_until",)

    def __init__(self) -> None:
        self._until = 0.0

    def wait(self) -> float:
        """Seconds until the circuit closes; 0 when it is closed."""
        return max(0.0, self._until - _time.monotonic())

    def open_for(self, seconds: float) -> None:
        self._until = max(self._until, _time.monotonic() + seconds)


_CIRCUIT = _Circuit()


def _token(settings: Settings) -> str:
    """The instance's read token, or "" when none usable is stored. Asked through
    `tmdb_configured`, so a stored v3 key — which this module will not send —
    reads as no token rather than as a 401 on every call."""
    return settings.tmdb_api_key.strip() if settings.tmdb_configured else ""


def _retry_after(resp: httpx.Response) -> float:
    """TMDB's Retry-After as seconds, bounded; the default when absent or unreadable."""
    try:
        seconds = float(resp.headers.get("retry-after") or "")
    except ValueError:
        return _DEFAULT_RETRY_AFTER
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER) or _DEFAULT_RETRY_AFTER


def url_for(settings: Settings, path: str, params: dict | None = None) -> str:
    """The full address of one catalogue read — which is also its cache key.

    THE LANGUAGE IS IN IT, ALWAYS. Every catalogue answer is written in the
    instance's TMDB language (Settings.tmdb_language), so the language is part
    of WHAT was asked and an answer in one language must never be served for a
    request in another. Putting it in the URL puts it in the key for free.

    NO CREDENTIAL IS IN IT, which is the other half of why the token rides the
    header: this string is stored, logged and traced.
    """
    query = {**(params or {}), "language": settings.tmdb_language}
    return f"{API_BASE}/{path.lstrip('/')}?{urlencode(sorted(query.items()))}"


async def _send(settings: Settings, url: str, path: str, etag: str) -> httpx.Response:
    """One GET, through the gate and the circuit. Network failures come back as
    TmdbError; a 429 opens the circuit and is retried once if the wait fits."""
    token = _token(settings)
    if not token:
        raise TmdbError("TMDB is not configured on this instance.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    for attempt in (1, 2):
        wait = _CIRCUIT.wait()
        if wait > _MAX_WAIT_SECONDS:
            raise TmdbRateLimited(f"TMDB asked us to slow down; {path} was not sent.", 429)
        if wait:
            await asyncio.sleep(wait)
        async with POOL.gate():
            with span("tmdb.get", path=path) as sp:
                try:
                    resp = await POOL.client().get(url, headers=headers)
                except httpx.HTTPError as exc:
                    # The exception's own text names the URL and never a header,
                    # so it is safe to log; it is still worded here rather than
                    # passed through, because nothing upstream needs httpx's type.
                    logger.warning("TMDB GET %s failed: %s", path, type(exc).__name__)
                    raise TmdbError(f"Could not reach TMDB for {path}.") from exc
                sp.set(status=resp.status_code)
        if resp.status_code != 429:
            return resp
        seconds = _retry_after(resp)
        _CIRCUIT.open_for(seconds)
        logger.warning("TMDB rate-limited %s (attempt %d); circuit open for %.1fs",
                       path, attempt, seconds)
    raise TmdbRateLimited(f"TMDB is still rate-limiting {path}.", 429)


async def cached_get(settings: Settings, path: str, params: dict | None = None, *,
                     ttl_seconds: int = DEFAULT_TTL_SECONDS, cache_only: bool = False,
                     only_if_cached: bool = False):
    """GET a catalogue path, through the shared response cache. Returns the parsed
    body; raises TmdbNotFound for a 404 and TmdbError for anything else that is
    not an answer.

    THE VALIDATOR IS STORED BESIDE THE BODY, as one cached value
    {"etag", "body"}, so a revalidation needs no schema: an answer past
    `ttl_seconds` is asked for again with If-None-Match, and TMDB's 304 (which
    carries no body) re-stamps the stored copy and serves it. Every TMDB answer
    carries a weak ETag (measured), so a refresh of an unchanged title costs TMDB
    nothing it would have to send.

    `cache_only=True` NEVER CALLS: the stored body however old, or None. What a
    public share page reads with, so a stranger's click spends nothing.

    `only_if_cached=True` never calls either, but answers None for anything the
    fresh read could not supply — no stale fallback. The same pair, and the same
    distinction, as the Trakt transport's flags of those names.
    """
    url = url_for(settings, path, params)
    stored = await cache.get(url, ttl_seconds)
    if _is_envelope(stored) and not _is_not_found(stored):
        return stored["body"]
    stale = await cache.get_stale(url)
    # A REMEMBERED 404 IS AN ANSWER, raised as the same TmdbNotFound a live one
    # is — the season handover and every "TMDB has no such title" reader depend
    # on that type — and never as a failure. Every flag honours it but one: a
    # caller that may not make a call still learns what TMDB already said...
    if _is_not_found(stale) and stale["at"] > db.now() - NOT_FOUND_SECONDS:
        # ...except a `cache_only` reader, which is served the last good answer
        # kept beside the marker. That reader is a public share page, drawing
        # what it drew before; a 404 that may be TMDB's passing mistake about a
        # real title must not blank a page nobody there can refill.
        if cache_only and _is_envelope(stale):
            return stale["body"]
        raise TmdbNotFound(path)
    if only_if_cached:
        return None
    if cache_only:
        return stale["body"] if _is_envelope(stale) else None
    etag = str(stale.get("etag") or "") if _is_envelope(stale) else ""
    started = _time.perf_counter()
    resp = await _send(settings, url, path, etag)
    logger.debug("TMDB GET %s -> %s in %.0fms", path, resp.status_code,
                 (_time.perf_counter() - started) * 1000.0)
    if resp.status_code == 304 and _is_envelope(stale):
        # TMDB has just said the stored answer is still its answer, which is
        # obtaining it again: a clean envelope, its clock restarted.
        body = stale["body"]
        await _store(url, stale.get("etag") or "", body)
        return body
    if resp.status_code == 404:
        await _remember_not_found(url, stale)
        raise TmdbNotFound(path)
    if resp.status_code == 401:
        # Worded without the token, which the response does not echo anyway.
        logger.warning("TMDB GET %s -> 401: the instance's read token was refused", path)
        raise TmdbError("TMDB refused the read token (401). Check it in Settings.", 401)
    if resp.status_code != 200:
        logger.warning("TMDB GET %s -> HTTP %s", path, resp.status_code)
        raise TmdbError(f"TMDB answered HTTP {resp.status_code} for {path}.", resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        raise TmdbError(f"TMDB answered {path} with an unreadable body.") from None
    await _store(url, resp.headers.get("etag") or "", body)
    return body


async def _store(url: str, etag: str, body) -> None:
    """One answer TMDB has just given, with its validator and the moment it was
    obtained — `at`, which is what lets a copy kept past a later 404 still be
    held to the six-month ceiling (`_remember_not_found`)."""
    await cache.set(url, {"etag": etag, "body": body, "at": db.now()},
                    retain_seconds=CACHE_RETAIN_SECONDS)


async def _remember_not_found(url: str, stale) -> None:
    """Remember TMDB's 404 for NOT_FOUND_SECONDS — and KEEP THE LAST GOOD ANSWER
    BESIDE IT, which `cache_only` readers go on being served.

    WHY KEEP IT: TMDB has answered 404 for real titles, briefly. Written in place
    of the stored answer, one such 404 cost every public share page reading this
    title (cache_only, never fetching) the copy it drew from, with no visitor
    able to bring it back. Live callers are unaffected: for the day, they get
    TmdbNotFound exactly as before.

    THE SIX-MONTH CEILING STILL HOLDS FOR THE KEPT COPY. Rewriting the row resets
    the sweep's clock, so the row is retained only until the KEPT body's own
    ceiling — six months from when TMDB last gave it (`at`), the sweep's grace
    included, exactly as if it had never been rewritten. A copy with no `at`
    (stored before answers carried one) has an unknown age and is not kept.
    """
    marker = {"not_found": True, "at": db.now()}
    # When the copy on hand was obtained: an answer's own `at`, or — a 404 again,
    # a day on — the age the last marker kept for it.
    obtained = (stale.get("body_at") if _is_not_found(stale) else stale.get("at")) \
        if _is_envelope(stale) else None
    left = (obtained + CACHE_RETAIN_SECONDS - db.now()
            if isinstance(obtained, int) else 0)
    if left > 0:
        marker.update(etag=stale.get("etag") or "", body=stale["body"], body_at=obtained)
        await cache.set(url, marker, retain_seconds=left)
    else:
        await cache.set(url, marker, retain_seconds=CACHE_RETAIN_SECONDS)


def _is_envelope(value) -> bool:
    return isinstance(value, dict) and "body" in value


def _is_not_found(value) -> bool:
    return isinstance(value, dict) and value.get("not_found") is True and "at" in value


def image_url(path: str | None, size: str = "w500") -> str:
    """A TMDB image path as its ORIGIN address on TMDB's CDN, "" for none. The
    caller proxies it for a browser (providers.base.proxied_image); a server-side
    download fetches the origin."""
    return f"{IMAGE_BASE}/{size}{path}" if path else ""


async def download(url: str) -> bytes | None:
    """GET raw bytes from any URL — a TMDB image, or a registry URL another
    service published. NO CREDENTIAL: image CDNs are unauthenticated, and a token
    sent to an address recorded from somebody else would hand them a TMDB
    credential. None on any failure, so a caller degrades one picture."""
    with span("tmdb.download") as sp:
        try:
            resp = await POOL.client().get(url)
        except httpx.HTTPError as exc:
            logger.warning("download %s failed: %s", url, exc)
            return None
        sp.set(status=resp.status_code, bytes=len(resp.content or b""))
    if resp.status_code != 200:
        logger.warning("download %s -> HTTP %s", url, resp.status_code)
        return None
    return resp.content
