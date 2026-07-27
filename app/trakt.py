"""Async Trakt API client + response normalizer.

Fetches a month of calendar items for the selected endpoint and normalizes the
(differently-shaped) show/movie responses into one uniform `Item` dict the
template can render regardless of endpoint.
"""
from __future__ import annotations

import asyncio
import calendar
import logging
import time as _time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from . import cache
from . import calendar_filter
from .config import Settings
from .endpoints import Endpoint

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


def _build_url(endpoint: Endpoint, settings: Settings, start_date: str, days: int) -> str:
    # genres/countries are NOT sent as query params any more: the calendar cache
    # stores the complete unfiltered result and those become read-time per-user
    # filters, so one viewer can include JP/KR shows and another exclude them from
    # the same cached data. The equivalent filtering is reproduced client-side in
    # fetch_calendar (and the cached read path) via app/calendar_filter.py.
    path = f"{API_BASE}/calendars/all/{endpoint.path}/{start_date}/{days}"
    return f"{path}?{urlencode({'extended': 'full,images'})}"


async def fetch_calendar(endpoint: Endpoint, settings: Settings, year: int, month: int) -> list[dict]:
    """Fetch and normalize a month of calendar items for the given endpoint."""
    days = calendar.monthrange(year, month)[1]
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year:04d}-{month:02d}-{days:02d}"
    url = _build_url(endpoint, settings, start_date, days)

    # Calendar endpoints ignore pagination headers and return the whole range in
    # one response (verified live), so they are not sent here; a warning fires if
    # Trakt ever starts paginating.
    t0 = _time.perf_counter()
    resp = await _send(shared_client(), "GET", url, headers=_headers(settings, paginate=False))
    _perf.debug("netGET    calendar/%s/%s..%s -> %s  %.0fms", endpoint.key, start_date, end_date,
                resp.status_code, (_time.perf_counter() - t0) * 1000.0)
    if resp.status_code == 401:
        raise TraktError("Trakt rejected the credentials (401). Check Client ID / Access Token in Settings.", 401)
    if resp.status_code != 200:
        raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
    if resp.headers.get("x-pagination-page-count"):
        logger.warning("Trakt calendar endpoint returned pagination headers for %s; response may be truncated.", url)

    try:
        raw = resp.json()
    except ValueError:
        raise TraktError("Trakt API returned an unreadable response.")
    if not isinstance(raw, list):
        raw = []

    # Trakt used to filter by the genres/countries query params server-side;
    # those are no longer sent, so the same filtering is reproduced here on the
    # raw genre slugs (before normalization, which would rewrite "game-show" to
    # "Game Show"), giving an item set identical to what Trakt returned before.
    raw = calendar_filter.filter_entries(raw, endpoint.media, settings.genres, settings.countries)

    tz = ZoneInfo(settings.timezone)
    items = [normalize(entry, endpoint, tz) for entry in raw]
    items = [i for i in items if i and start_date <= i["air_date"] <= end_date]

    # Network filter: an operator-configured allow-list, matched case-sensitively
    # against Trakt's own network naming.
    if settings.network_filter:
        allow = set(settings.network_filter)
        items = [i for i in items if i["network"] in allow]

    items.sort(key=lambda i: i["air_ts"])
    return items


def _poster(media: dict) -> str | None:
    imgs = media.get("images") or {}
    posters = imgs.get("poster") or []
    if posters:
        url = posters[0]
        if not url.startswith("http"):
            url = "https://" + url
        return url
    return None


def normalize(entry: dict, endpoint: Endpoint, tz: ZoneInfo) -> dict | None:
    """Turn a raw Trakt calendar entry into the uniform Item shape."""
    media = entry.get(endpoint.media) or {}
    aired_raw = entry.get("first_aired") or entry.get("released")
    if not aired_raw or not media:
        return None

    # `released` (movies) is a plain date; `first_aired` is an ISO UTC timestamp.
    try:
        if "T" in str(aired_raw):
            dt = datetime.fromisoformat(str(aired_raw).replace("Z", "+00:00")).astimezone(tz)
        else:
            dt = datetime.fromisoformat(f"{aired_raw}T00:00:00+00:00").astimezone(tz)
    except ValueError:
        return None

    ids = media.get("ids") or {}
    episode = entry.get("episode") or {}
    ep_label = None
    ep_season = episode.get("season") if episode else None
    ep_number = episode.get("number") if episode else None
    if ep_season is not None and ep_number is not None:
        ep_label = f"S{int(ep_season):02d}E{int(ep_number):02d}"

    # Full overview is sent; cards clamp it via CSS, the poster-only panel scrolls it.
    overview = (media.get("overview") or "").strip()

    return {
        "media": endpoint.media,
        "id": ids.get("slug") or str(ids.get("trakt") or ""),
        "trakt_slug": ids.get("slug") or "",
        "trakt_id": ids.get("trakt"),
        "tvdb": ids.get("tvdb"),
        "tmdb": ids.get("tmdb"),
        "title": media.get("title") or "Untitled",
        "year": media.get("year") or "",
        "network": media.get("network") or "",
        "country": (media.get("country") or "").upper(),
        "language": (media.get("language") or "").upper(),
        "runtime": media.get("runtime"),
        "status": media.get("status") or "",
        "rating": round(float(media["rating"]), 1) if media.get("rating") else None,
        "genres": [g.replace("-", " ").title() for g in (media.get("genres") or [])],
        "certification": (media.get("certification") or "").upper(),
        "overview": overview,
        "poster": _poster(media),
        "air_date": dt.strftime("%Y-%m-%d"),
        "air_ts": dt.timestamp(),
        "air_display": dt.strftime("%d %b %Y"),
        "air_time": dt.strftime("%H:%M"),
        "day_of_week": dt.strftime("%A"),
        "episode_label": ep_label,
        "episode_title": episode.get("title") or "",
        "season": int(ep_season) if ep_season is not None else None,
        "episode_number": int(ep_number) if ep_number is not None else None,
        "trakt_url": (
            f"https://trakt.tv/{'movies' if endpoint.media == 'movie' else 'shows'}/{ids.get('slug')}"
            if ids.get("slug") else "https://trakt.tv"
        ),
    }


# ---------------------------------------------------------------------------
# Per-show detail lookups (cast, episodes) for the tile chip + modal.
# ---------------------------------------------------------------------------

def _headshot(person: dict) -> str | None:
    imgs = (person.get("images") or {}).get("headshot") or []
    if imgs:
        url = imgs[0]
        return url if url.startswith("http") else "https://" + url
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
        data = resp.json()
    except ValueError:
        logger.warning("Trakt GET %s -> unreadable JSON body", path)
        if raise_errors:
            raise TraktError("Trakt API returned an unreadable response.")
        return None
    if not private:
        await cache.set(url, data)
    return data


def _summarize_season(episodes: list[dict], tz: ZoneInfo) -> dict:
    """Reduce a season's episode list to the tile summary: count + first/last/next air dates."""
    aired, upcoming, total = [], [], 0
    now = datetime.now(tz)
    for ep in episodes or []:
        total += 1
        fa = ep.get("first_aired")
        if not fa:
            continue
        try:
            dt = datetime.fromisoformat(str(fa).replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            continue
        (aired if dt <= now else upcoming).append(dt)
    return {
        "episode_count": total,
        "first_aired": min(aired).strftime("%d %b %Y") if aired else None,
        "last_aired": max(aired).strftime("%d %b %Y") if aired else None,
        "next_aired": min(upcoming).strftime("%d %b %Y") if upcoming else None,
    }


async def fetch_tile_info(settings: Settings, media: str, trakt_id: str, season: int | None) -> dict:
    """Compact season info for a tile. Movies have no seasons."""
    if media == "movie" or season is None:
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    tz = ZoneInfo(settings.timezone)
    episodes = await _cached_get(shared_client(), settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"})
    if not isinstance(episodes, list):
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    return {"season": season, **_summarize_season(episodes, tz)}


async def fetch_details(settings: Settings, media: str, trakt_id: str, season: int | None,
                        cache_only: bool = False) -> dict:
    """Full detail payload for the modal: overview, cast, episode list.

    `cache_only=True` serves purely from cache and never calls Trakt — the mode a
    public share page uses so a visitor's click reuses what the owner's own views
    already cached rather than spending the owner's rate limit. Fields with no
    cached source come back empty, and the modal renders around them."""
    tz = ZoneInfo(settings.timezone)
    base = "movies" if media == "movie" else "shows"
    client = shared_client()
    tasks = {
        "info": _cached_get(client, settings, f"{base}/{trakt_id}", {"extended": "full"}, cache_only=cache_only),
        "people": _cached_get(client, settings, f"{base}/{trakt_id}/people", {"extended": "full"}, cache_only=cache_only),
    }
    if media != "movie" and season is not None:
        tasks["episodes"] = _cached_get(client, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"}, cache_only=cache_only)
    results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    info = results.get("info") or {}
    people = results.get("people") or {}
    episodes_raw = results.get("episodes") or []

    cast = []
    for member in (people.get("cast") or [])[:16]:
        person = member.get("person") or {}
        character = member.get("character") or (member.get("characters") or [""])[0]
        cast.append({
            "name": person.get("name") or "",
            "character": character,
            "headshot": _headshot(person),
        })

    episodes = []
    for ep in episodes_raw if isinstance(episodes_raw, list) else []:
        fa = ep.get("first_aired")
        air_display = ""
        if fa:
            try:
                air_display = datetime.fromisoformat(str(fa).replace("Z", "+00:00")).astimezone(tz).strftime("%d %b %Y")
            except ValueError:
                air_display = ""
        episodes.append({
            "number": ep.get("number"),
            "title": ep.get("title") or f"Episode {ep.get('number')}",
            "air_display": air_display,
            "rating": round(float(ep["rating"]), 1) if ep.get("rating") else None,
            "overview": (ep.get("overview") or "").strip(),
        })

    return {
        "title": info.get("title") or "",
        "year": info.get("year") or "",
        "overview": (info.get("overview") or "").strip(),
        "status": (info.get("status") or "").replace("_", " ").title(),
        "network": info.get("network") or "",
        "runtime": info.get("runtime"),
        "genres": [g.replace("-", " ").title() for g in (info.get("genres") or [])],
        "rating": round(float(info["rating"]), 1) if info.get("rating") else None,
        "certification": (info.get("certification") or "").upper(),
        "trailer": info.get("trailer") or "",
        "homepage": info.get("homepage") or "",
        "season": season,
        "cast": cast,
        "episodes": episodes,
    }


# ---------------------------------------------------------------------------
# distrakt (hidden tracker) data layer — search, watched counts, and season
# cadence/date derivation. Shows only.
# ---------------------------------------------------------------------------

# Season calls get a SHORT TTL: totals grow over time, so a day-old total is
# fine, but we don't want to hold a season's episode list for the 12h detail TTL.
SEASON_CACHE_TTL_SECONDS = 24 * 60 * 60

# date.weekday(): Mon=0 .. Sun=6. Explicit map so cadence is locale-independent.
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_air_date(first_aired, tz: ZoneInfo) -> date | None:
    """Trakt's ISO-UTC `first_aired` -> local calendar date, or None if missing."""
    if not first_aired:
        return None
    try:
        return datetime.fromisoformat(str(first_aired).replace("Z", "+00:00")).astimezone(tz).date()
    except ValueError:
        return None


def _md(d: date | None) -> str | None:
    """Format a date as 'M/D' with no leading zeros. None stays None so the
    renderer can decide to show '?/?' for an unknown date."""
    return f"{d.month}/{d.day}" if d else None


def _derive_season(episodes: list[dict], tz: ZoneInfo, now: datetime | None = None) -> dict:
    """Pure derivation of the cadence/date fields from a season's raw episode
    list. No I/O — unit-tested directly.

    Rules:
      total (y)  = every episode Trakt currently reports (dated or not).
      premiere   = first KNOWN air date.
      finale     = last KNOWN air date, but ONLY once the season is fully
                   scheduled; an unscheduled tail leaves it unknown -> '?/?'.
      cadence    = 'b' when every episode shares one air date (binge), else the
                   weekday abbrev of the airing pattern (mode of known weekdays);
                   None when no air dates are known yet.
      started/finished_airing compare premiere/finale against `now`.
    """
    episodes = episodes or []
    total = len(episodes)
    now_date = (now or datetime.now(tz)).date()

    known = sorted(d for d in (_parse_air_date(ep.get("first_aired"), tz) for ep in episodes) if d)
    fully_scheduled = total > 0 and len(known) == total

    premiere_date = known[0] if known else None
    # Finale is only meaningful when nothing is left unscheduled; an unscheduled
    # tail leaves it unknown so the renderer shows a "?/?" date.
    finale_date = known[-1] if (fully_scheduled and known) else None

    if not known:
        cadence = None
    elif fully_scheduled and len(set(known)) == 1:
        cadence = "b"  # binge: all episodes share one air date
    else:
        weekday = Counter(d.weekday() for d in known).most_common(1)[0][0]
        cadence = _WEEKDAY_ABBR[weekday]

    return {
        "total": total,
        "cadence": cadence,
        "premiere": _md(premiere_date),
        "finale": _md(finale_date),
        "started_airing": premiere_date is not None and premiere_date <= now_date,
        "finished_airing": finale_date is not None and finale_date <= now_date,
        "air_dates": [d.isoformat() for d in known],
    }


def _empty_season(season: int) -> dict:
    return {
        "season": season, "total": 0, "cadence": None, "premiere": None,
        "finale": None, "started_airing": False, "finished_airing": False, "air_dates": [],
    }


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


async def fetch_season_detail(settings: Settings, trakt_id, season: int, fresh: bool = False,
                              client: httpx.AsyncClient | None = None) -> dict:
    """One /shows/{id}/seasons/{season}?extended=full call (short TTL) reduced to
    the fields: total (y), cadence, premiere, finale, started/finished. Pass a
    shared `client` when batching (else a throwaway one is created)."""
    tz = ZoneInfo(settings.timezone)
    c = client or shared_client()
    episodes = await _cached_get(
        c, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"},
        ttl_seconds=SEASON_CACHE_TTL_SECONDS, fresh=fresh,
    )
    if not isinstance(episodes, list):
        return _empty_season(season)
    return {"season": season, **_derive_season(episodes, tz)}


async def fetch_watched_map(settings: Settings, trakt_ids) -> dict[tuple[int, int], int]:
    """Per-season watched-episode counts (the live `x`), keyed {(trakt_id, season):
    completed} — via ONE /shows/{id}/progress/watched call per UNIQUE show.

    Why not the aggregate /sync/watched/shows? An audit showed it returning
    show-level rows (plays + show, capped ~100/page) WITHOUT the seasons[]/
    episodes[] breakdown — paginated or not — so every count came back 0. The
    per-show progress endpoint is the authoritative source of a user's season
    completion and always includes `seasons[].completed`.

    Never cached — both because `x` is live and because a progress record belongs
    to whoever's token asked for it, and the cache is keyed by URL alone. One
    shared httpx client pools the fan-out. Errored/absent shows just contribute
    no keys (that show renders 0)."""
    unique = sorted({int(t) for t in trakt_ids if t is not None})
    if not unique:
        return {}
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    client = shared_client()
    results = await asyncio.gather(*(
        _cached_get(client, settings, f"shows/{tid}/progress/watched", params, private=True)
        for tid in unique
    ))
    lookup: dict[tuple[int, int], int] = {}
    for tid, res in zip(unique, results):
        if not isinstance(res, dict):
            continue
        for season in res.get("seasons") or []:
            num = season.get("number")
            if num is None:
                continue
            lookup[(tid, int(num))] = int(season.get("completed") or 0)
    logger.info("fetch_watched_map: %d show(s) -> %d (trakt_id,season) key(s)", len(unique), len(lookup))
    return lookup


# ---------------------------------------------------------------------------
# Incremental watch-history cache primitives (app/watch_history.py). The cache
# baselines a show once (progress -> completed episode numbers), then applies
# only NEW plays from /users/me/history since the last sync, gated by
# /sync/last_activities. Movies come through the same history sweep.
# ---------------------------------------------------------------------------

async def fetch_last_activities(settings: Settings) -> dict:
    """/sync/last_activities -> the small per-type "last changed at" beacon blob
    (fixed size, independent of library size). Used to gate the history sync."""
    res = await _cached_get(shared_client(), settings, "sync/last_activities", {}, private=True)
    return res if isinstance(res, dict) else {}


async def fetch_show_progress_detail(settings: Settings, trakt_id,
                                     client: httpx.AsyncClient | None = None) -> dict[int, list[int]]:
    """/shows/{id}/progress/watched -> {season_number: [completed episode numbers]}.
    The per-show baseline: authoritative, deduped completion straight from Trakt.
    Never cached — this is one person's viewing, and the cache key is the URL.
    Pass a shared `client` when batching."""
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    c = client or shared_client()
    res = await _cached_get(c, settings, f"shows/{trakt_id}/progress/watched", params, private=True)
    out: dict[int, list[int]] = {}
    if isinstance(res, dict):
        for season in res.get("seasons") or []:
            num = season.get("number")
            if num is None:
                continue
            eps = sorted({
                int(e["number"]) for e in (season.get("episodes") or [])
                if e.get("completed") and e.get("number") is not None
            })
            out[int(num)] = eps
    return out


async def fetch_show_tmdb(settings: Settings, trakt_id, client: httpx.AsyncClient | None = None) -> int | None:
    """The TMDB id for a Trakt show (/shows/{id} -> ids.tmdb). Backfills distrakt
    records added before tmdb was stored. Cached (ids never change)."""
    c = client or shared_client()
    data = await _cached_get(c, settings, f"shows/{trakt_id}", {})
    tmdb = ((data or {}).get("ids") or {}).get("tmdb")
    return int(tmdb) if tmdb else None


async def fetch_history(settings: Settings, start_at: str | None = None,
                        limit: int = 100, max_pages: int = 50) -> list[dict]:
    """/users/me/history (ALL types) -> chronological watch EVENTS, newest first,
    optionally since `start_at` (YYYY-MM-DD). Pages via ?page/?limit, following the
    X-Pagination-Page-Count header. Each event is an episode or movie play; the
    caller dedupes. `start_at` at day granularity means each sync may re-see the
    day's earlier events — harmless, since applying them is idempotent."""
    events: list[dict] = []
    page = 1
    client = shared_client()
    while page <= max_pages:
        params = {"limit": str(limit), "page": str(page)}
        if start_at:
            params["start_at"] = start_at
        url = f"{API_BASE}/users/me/history?{urlencode(params)}"
        t0 = _time.perf_counter()
        try:
            resp = await _send(client, "GET", url, headers=_headers(settings, paginate=False))
        except httpx.HTTPError as exc:
            logger.warning("fetch_history: request failed: %s", exc)
            break
        _perf.debug("netGET    users/me/history?page=%s -> %s  %.0fms", page,
                    resp.status_code, (_time.perf_counter() - t0) * 1000.0)
        if resp.status_code != 200:
            logger.warning("fetch_history: HTTP %s: %s", resp.status_code, resp.text[:200])
            break
        try:
            batch = resp.json()
        except ValueError:
            break
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        try:
            page_count = int(resp.headers.get("x-pagination-page-count") or 1)
        except (TypeError, ValueError):
            page_count = 1
        if page >= page_count:
            break
        page += 1
    logger.info("fetch_history(start_at=%s): %d event(s) over %d page(s)", start_at, len(events), page)
    return events


def _parse_watched_ts(value) -> datetime | None:
    """Trakt's ISO-UTC last_watched_at -> aware datetime (UTC), or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def fetch_watched_progress(settings: Settings, since_days: int | None = 60) -> list[dict]:
    """Recently-active seasons from watch HISTORY (/users/me/history), as
    [{trakt_id, tmdb, season, watched, slug, title, network}].

    Uses the history event log rather than /sync/watched/shows — that aggregate
    returns show-level rows WITHOUT seasons for some accounts (the same bug that
    zeroed the watch counts), so it surfaced no candidates. `start_at` bounds it
    to the last `since_days` (a date). `watched` here is the count of DISTINCT
    episodes seen in that window per (show, season) — a recency signal; the
    caller checks it against the season total to decide in-progress vs completed.
    """
    start_at = None
    if since_days is not None:
        start_at = (datetime.now(timezone.utc).date() - timedelta(days=since_days)).isoformat()
    events = await fetch_history(settings, start_at=start_at)

    agg: dict[tuple[int, int], dict] = {}
    for ev in events:
        if ev.get("type") != "episode":
            continue
        show = ev.get("show") or {}
        ep = ev.get("episode") or {}
        ids = show.get("ids") or {}
        tid, season, num = ids.get("trakt"), ep.get("season"), ep.get("number")
        if tid is None or season is None or int(season) == 0:  # skip specials
            continue
        rec = agg.setdefault((int(tid), int(season)), {
            "eps": set(), "tmdb": ids.get("tmdb"), "slug": ids.get("slug") or "",
            "title": show.get("title") or "", "network": show.get("network") or "",
        })
        if num is not None:
            rec["eps"].add(int(num))
    out = [{
        "trakt_id": tid, "tmdb": rec["tmdb"], "season": season, "watched": len(rec["eps"]),
        "slug": rec["slug"], "title": rec["title"], "network": rec["network"],
    } for (tid, season), rec in agg.items()]
    logger.info("fetch_watched_progress(since_days=%s) -> %d recent season(s) from history", since_days, len(out))
    return out


async def fetch_show_seasons(settings: Settings, trakt_id) -> list[dict]:
    """/shows/{id}/seasons?extended=full -> [{season, episode_count}] for
    seasons Trakt has actually populated with episodes (skips season 0/
    specials and any season with zero KNOWN episodes at all). Powers the
    add-show flow's season picker.

    Filters on `episode_count` (Trakt's total planned/known episode count for
    the season), NOT `aired_episodes`. A season that hasn't premiered yet has
    aired_episodes=0 but a real episode_count once Trakt has announced it —
    filtering on aired_episodes wrongly hid every not-yet-aired season from
    the picker, which is exactly a season 1 that has not started airing yet.
    Fixed once manual add-show on an unaired season turned out to be broken."""
    results = await _cached_get(
        shared_client(), settings, f"shows/{trakt_id}/seasons", {"extended": "full"}, raise_errors=True,
    )
    out = []
    for entry in results if isinstance(results, list) else []:
        num = entry.get("number")
        episode_count = entry.get("episode_count") or 0
        if num is None or num == 0 or episode_count <= 0:
            continue
        out.append({"season": int(num), "episode_count": int(episode_count)})
    out.sort(key=lambda s: s["season"])
    logger.info("fetch_show_seasons(%s) -> %d usable season(s)", trakt_id, len(out))
    return out


SEARCH_MEDIA = ("show", "movie")


def ids_map(media: dict) -> dict:
    """Every id Trakt knows for a title, with the empty ones dropped.

    The whole map travels rather than the one id a given caller happens to want:
    an id we discard here is one a future match against another service cannot
    use, and re-fetching it costs a call we have already paid for.
    """
    ids = media.get("ids") or {}
    return {key: value for key, value in ids.items() if value not in (None, "")}


async def search_titles(settings: Settings, media: str, query: str) -> list[dict]:
    """/search/{show|movie}?query=... -> [{media, ids, title, year, network,
    runtime, overview}], newest-match-first as Trakt orders it.

    ONE implementation for both media types. The two searches differ only in the
    path segment and in which key the result object hangs under, so a second
    copy shaped for movies would drift from this one the first time either is
    touched. Empty query returns [] without a call.
    """
    if media not in SEARCH_MEDIA:
        raise ValueError(f"Unknown media type {media!r}.")
    q = (query or "").strip()
    if not q:
        return []
    results = await _cached_get(
        shared_client(), settings, f"search/{media}", {"query": q, "extended": "full"},
        raise_errors=True,
    )

    out = []
    for entry in results if isinstance(results, list) else []:
        item = entry.get(media) or {}
        ids = ids_map(item)
        if not ids:
            # Nothing to identify it by, so nothing downstream could store,
            # dedupe or look up artwork for it.
            continue
        out.append({
            "media": media,
            "ids": ids,
            "title": item.get("title") or "",
            "year": item.get("year"),
            # Movies have no network and shows no runtime worth showing, so each
            # simply comes back empty for the other — the caller decides which
            # it renders.
            "network": item.get("network") or "",
            "runtime": item.get("runtime"),
            "overview": (item.get("overview") or "").strip(),
        })
    logger.info("search_titles(%s, %r) -> %d raw / %d usable result(s)", media, q,
                len(results) if isinstance(results, list) else 0, len(out))
    return out


async def search_shows(settings: Settings, query: str) -> list[dict]:
    """/search/show?query=... -> compact [{trakt_id, slug, title, year, network}]
    for the add-show flow. Empty query returns []."""
    out = []
    for entry in await search_titles(settings, "show", query):
        ids = entry["ids"]
        tid = ids.get("trakt")
        if tid is None:
            continue
        out.append({
            "trakt_id": int(tid),
            "tmdb": ids.get("tmdb"),
            "slug": ids.get("slug") or "",
            "title": entry["title"],
            "year": entry["year"],
            "network": entry["network"],
        })
    return out


async def fetch_movie_summary(settings: Settings, trakt_id) -> dict | None:
    """/movies/{id}?extended=full,images -> the raw movie object, or None.

    The id resolution step for a movie known only by its Trakt id: ids never
    change, so this caches like any other detail lookup. `images` is asked for
    because the same response then answers "what is this movie's poster URL"
    without a second call.
    """
    data = await _cached_get(
        shared_client(), settings, f"movies/{trakt_id}", {"extended": "full,images"},
    )
    return data if isinstance(data, dict) else None


async def fetch_ratings(settings: Settings) -> list[dict]:
    """/sync/ratings -> everything the token's owner has rated, shows and movies
    together, as Trakt's own entries ({type, rating, rated_at, show|movie}).

    PRIVATE TO WHOEVER'S TOKEN ASKED, so it is never written to the shared
    URL-keyed cache — two accounts asking send the identical URL and would
    otherwise be served each other's ratings.

    Pagination headers are deliberately NOT sent: this endpoint returns the
    whole set in one response, and asking for a page would silently cap a large
    library at the pagination limit with nothing in the response to say so.
    """
    url = f"{API_BASE}/sync/ratings?{urlencode({'extended': 'full'})}"
    resp = await _send(shared_client(), "GET", url, headers=_headers(settings, paginate=False))
    if resp.status_code == 401:
        raise TraktError("Trakt rejected the credentials (401).", 401)
    if resp.status_code != 200:
        raise TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        raise TraktError("Trakt API returned an unreadable response.") from None
    entries = data if isinstance(data, list) else []
    logger.info("fetch_ratings -> %d rated item(s)", len(entries))
    return entries
