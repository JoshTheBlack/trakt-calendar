"""Async Trakt API client + response normalizer.

Fetches a month of calendar items for the selected endpoint and normalizes the
(differently-shaped) show/movie responses into one uniform `Item` dict the
template can render regardless of endpoint (requirement D).
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


def _headers(settings: Settings, paginate: bool = True) -> dict:
    """Trakt request headers. `paginate=False` OMITS the X-Pagination-* headers.

    This matters for /sync/watched/shows: sending pagination headers switches it
    into a PAGINATED, show-level response (100/page) that DROPS the nested
    seasons[]/episodes[] breakdown we count — which manifested as every watched
    count coming back 0. Non-paginated, it returns the full watched library WITH
    seasons in one call (BUILD_PLAN §2b's "ONE call")."""
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
    resp = await shared_client().get(url, headers=_headers(settings, paginate=False))
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

    # Network filter (requirement C: configurable) — case-sensitive match Trakt naming.
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
# Phase 2: per-show detail lookups (cast, episodes) for the tile chip + modal.
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
):
    """GET a Trakt path (with disk caching keyed by path+params). Returns parsed JSON or None.

    `ttl_seconds` overrides the default detail TTL (used for the short-lived
    distrakt season calls); `fresh=True` skips the cache read but still refreshes it.
    `raise_errors=True` raises TraktError instead of silently returning None — used
    by callers (search, seasons) where a swallowed 401 previously looked identical
    to a genuine "no results" response, making auth failures invisible in the UI.
    """
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    ttl = ttl_seconds if ttl_seconds is not None else settings.cache_ttl_minutes * 60
    if not fresh:
        cached = cache.get(url, ttl)
        if cached is not None:
            _perf.debug("cacheHIT  %s", path)  # DEBUG: 1 line/season, noisy on warm loads
            return cached
    t0 = _time.perf_counter()
    try:
        resp = await client.get(url, headers=_headers(settings))
    except httpx.HTTPError as exc:
        logger.warning("Trakt GET %s failed: %s", path, exc)
        if raise_errors:
            raise TraktError(f"Could not reach Trakt: {exc}") from exc
        return None
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
    cache.set(url, data)
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
    """Compact season info for a tile (requirement F). Movies have no seasons."""
    if media == "movie" or season is None:
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    tz = ZoneInfo(settings.timezone)
    episodes = await _cached_get(shared_client(), settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"})
    if not isinstance(episodes, list):
        return {"episode_count": None, "first_aired": None, "last_aired": None, "next_aired": None}
    return {"season": season, **_summarize_season(episodes, tz)}


async def fetch_details(settings: Settings, media: str, trakt_id: str, season: int | None) -> dict:
    """Full detail payload for the modal (requirement G): overview, cast, episode list."""
    tz = ZoneInfo(settings.timezone)
    base = "movies" if media == "movie" else "shows"
    client = shared_client()
    tasks = {
        "info": _cached_get(client, settings, f"{base}/{trakt_id}", {"extended": "full"}),
        "people": _cached_get(client, settings, f"{base}/{trakt_id}/people", {"extended": "full"}),
    }
    if media != "movie" and season is not None:
        tasks["episodes"] = _cached_get(client, settings, f"shows/{trakt_id}/seasons/{season}", {"extended": "full"})
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
        "trailer": info.get("trailer") or "",
        "homepage": info.get("homepage") or "",
        "season": season,
        "cast": cast,
        "episodes": episodes,
    }


# ---------------------------------------------------------------------------
# Phase 3: distrakt (hidden tracker) data layer — search, watched counts, and
# season cadence/date derivation (BUILD_PLAN §2–§4). Shows only.
# ---------------------------------------------------------------------------

# Season calls get a SHORT TTL (§3): totals grow over time, so a day-old total is
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
    """Format a date as 'M/D' with no leading zeros (§4). None stays None so the
    renderer can decide to show '?/?' for an unknown date."""
    return f"{d.month}/{d.day}" if d else None


def _derive_season(episodes: list[dict], tz: ZoneInfo, now: datetime | None = None) -> dict:
    """Pure derivation of the §4 cadence/date fields from a season's raw episode
    list. No I/O — unit-tested directly.

    Rules (§3/§4):
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
    # Finale is only meaningful when nothing is left unscheduled (§4 "?/?" tail).
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


async def fetch_season_detail(settings: Settings, trakt_id, season: int, fresh: bool = False,
                              client: httpx.AsyncClient | None = None) -> dict:
    """One /shows/{id}/seasons/{season}?extended=full call (short TTL) reduced to
    the §4 fields: total (y), cadence, premiere, finale, started/finished. Pass a
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
    completed} — via ONE /shows/{id}/progress/watched call per UNIQUE show (§2b).

    Why not the aggregate /sync/watched/shows? An audit showed it returning
    show-level rows (plays + show, capped ~100/page) WITHOUT the seasons[]/
    episodes[] breakdown — paginated or not — so every count came back 0. The
    per-show progress endpoint is the authoritative source of a user's season
    completion and always includes `seasons[].completed`.

    Never cached (live x). One shared httpx client pools the fan-out (backlog 1b).
    Errored/absent shows just contribute no keys (that show renders 0)."""
    unique = sorted({int(t) for t in trakt_ids if t is not None})
    if not unique:
        return {}
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    client = shared_client()
    results = await asyncio.gather(*(
        _cached_get(client, settings, f"shows/{tid}/progress/watched", params, fresh=True)
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
    res = await _cached_get(shared_client(), settings, "sync/last_activities", {}, fresh=True)
    return res if isinstance(res, dict) else {}


async def fetch_show_progress_detail(settings: Settings, trakt_id,
                                     client: httpx.AsyncClient | None = None) -> dict[int, list[int]]:
    """/shows/{id}/progress/watched -> {season_number: [completed episode numbers]}.
    The per-show baseline: authoritative, deduped completion straight from Trakt.
    Pass a shared `client` when batching."""
    params = {"hidden": "false", "specials": "false", "count_specials": "false"}
    c = client or shared_client()
    res = await _cached_get(c, settings, f"shows/{trakt_id}/progress/watched", params, fresh=True)
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
        try:
            resp = await client.get(url, headers=_headers(settings, paginate=False))
        except httpx.HTTPError as exc:
            logger.warning("fetch_history: request failed: %s", exc)
            break
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
    [{trakt_id, tmdb, season, watched, slug, title, network}] (§6 rollover step d).

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
    add-show flow's season picker (§2e); NOT in BUILD_PLAN's §7 module map —
    a small data-layer addition Chat 3 needed.

    Filters on `episode_count` (Trakt's total planned/known episode count for
    the season), NOT `aired_episodes`. A season that hasn't premiered yet has
    aired_episodes=0 but a real episode_count once Trakt has announced it —
    filtering on aired_episodes wrongly hid every not-yet-aired season from
    the picker, which is exactly the New Shows / Returning bucket's case (§4:
    "New Shows (S01, not yet started airing)"). Fixed post-Chat-4 once manual
    add-show on an unaired season turned out to be broken."""
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


async def search_shows(settings: Settings, query: str) -> list[dict]:
    """/search/show?query=... -> compact [{trakt_id, slug, title, year, network}]
    for the add-show flow (§2e). Empty query returns []."""
    q = (query or "").strip()
    if not q:
        return []
    results = await _cached_get(
        shared_client(), settings, "search/show", {"query": q, "extended": "full"}, raise_errors=True,
    )

    out = []
    for entry in results if isinstance(results, list) else []:
        show = entry.get("show") or {}
        ids = show.get("ids") or {}
        tid = ids.get("trakt")
        if tid is None:
            continue
        out.append({
            "trakt_id": int(tid),
            "tmdb": ids.get("tmdb"),
            "slug": ids.get("slug") or "",
            "title": show.get("title") or "",
            "year": show.get("year"),
            "network": show.get("network") or "",
        })
    logger.info("search_shows(%r) -> %d raw / %d usable result(s)", q, len(results) if isinstance(results, list) else 0, len(out))
    return out
