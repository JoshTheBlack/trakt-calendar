"""Global, UTC calendar cache and the read path over it.

Calendar data is the same for everyone, so it is cached once — per (endpoint,
7-day window) — and every viewer reads from the same rows. The design, all
locked by live measurement against the real Trakt API:

  - FETCH IN 7-DAY WINDOWS aligned to a fixed epoch, NOT to "today", so two
    viewers looking at the same month hit the same cache rows. A month view is
    five or six window reads, each cached and TTL'd independently.

  - STORE RAW, PRUNED, UTC. Trakt's entries are kept verbatim (raw ISO-UTC
    timestamps, no timezone conversion and no normalization), pruned to only the
    fields the normalizer and the filters read. `extended=full,images` returns a
    great deal the app never touches; pruning is the single biggest size lever
    and also stops the cache growing when Trakt adds fields.

  - NO FILTERING AT FETCH TIME. `genres` and `countries` are no longer sent to
    Trakt; the window holds the complete worldwide result, and all filtering —
    genres, countries, networks — happens at read time, per viewer, against the
    cached blob (see app/calendar_filter.py).

  - NO PAGINATION HEADERS. Trakt's calendar endpoints ignore them and return the
    whole window in one response (verified live); a warning is logged if a
    pagination header ever appears, in case that changes.

  - TRIM EACH WINDOW TO ITS OWN 7 DAYS. The request is the documented shape
    (/calendars/{target}/{path}/{start_date}/{days}), but Trakt treats `days` as
    a floor, not a ceiling — measured live, a 7-day window came back carrying
    entries two months past its end — so neighbouring windows overlap heavily.
    Without the trim a month read concatenates those overlaps and renders the
    same episode two or three times (see in_window / dedupe_entries).

The cache blob and the detail-lookup cache share one table (api_cache); this
module owns the calendar keys and the per-window TTL. THE READ PATH — read_month
plus the window helpers below — is what the authenticated calendar route and the
public share pages both call: pass allow_fetch=False on a share page and it
serves whatever is cached (even stale, even empty) and never touches Trakt.
"""
from __future__ import annotations

import calendar as _calendar
import json
import logging
import zlib
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import calendar_filter, db, trakt
from .cache import COMPRESS_LEVEL
from .endpoints import Endpoint

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7

# A fixed reference point the 7-day windows tile out from. Any fixed date works;
# a Monday is chosen so a window starts on a Monday, which reads naturally. What
# matters is only that it never depends on "today", so every viewer's month maps
# to the same window rows.
_EPOCH = date(2001, 1, 1)  # a Monday


def window_start(day: date) -> date:
    """The start date of the fixed 7-day window containing `day`."""
    offset = (day - _EPOCH).days
    return _EPOCH + timedelta(days=(offset // WINDOW_DAYS) * WINDOW_DAYS)


def aligned_windows(range_start: date, range_end: date) -> list[date]:
    """Every aligned window start covering [range_start, range_end] inclusive."""
    start = window_start(range_start)
    out: list[date] = []
    current = start
    while current <= range_end:
        out.append(current)
        current += timedelta(days=WINDOW_DAYS)
    return out


def _entry_utc_date(entry: dict) -> str:
    """The YYYY-MM-DD an entry airs on, in UTC, straight off the stored string.

    Sliced rather than parsed: the cache stores Trakt's raw ISO-UTC timestamp
    verbatim, so the first ten characters already are the UTC date, and the
    windows this feeds are UTC-aligned.
    """
    return str(entry.get("first_aired") or entry.get("released") or "")[:10]


def in_window(entry: dict, start: date) -> bool:
    """Whether an entry belongs to the 7-day window beginning `start`.

    NEEDED BECAUSE TRAKT OVERRUNS THE `days` IT IS GIVEN. The request shape is
    exactly the documented one — /calendars/{target}/{path}/{start_date}/{days} —
    and `days` is honoured as a floor but not as a ceiling. Measured live against
    /calendars/all/shows/ from 2026-07-06:

        days=1  ->   89 entries spanning 4 days
        days=3  ->  206 entries spanning 6 days
        days=7  ->  404 entries spanning 17 days, out to 2026-07-27
        days=14 ->  793 entries spanning out to 2026-09-05

    Every show endpoint does it (new, premieres, finales, shows); movies happened
    to come back clean, which is a small dataset rather than a promise. The
    `end_date` query filter does NOT constrain it — same 404 entries, same span —
    so there is no server-side way to ask for less.

    What IS reliable: the response never starts before `start_date`, and always
    covers the range asked for. So the window owning a date always returns that
    date, and trimming the rest is lossless — verified by count on a real month
    (1691 cards with 207 duplicates -> 1484, exactly the duplicates removed).

    Windows tile contiguously, so every UTC date falls in exactly one, and an
    entry Trakt handed to the wrong window is one an adjacent window also
    returns. Without this trim a month read concatenates those overlaps and
    renders the same episode two or three times.

    Trimmed on the entry's top-level `first_aired` (or `released` for movies),
    which is also what the normalizer renders the card from. Checked live across
    every endpoint: it never disagrees with `episode.first_aired`.
    """
    day = _entry_utc_date(entry)
    if not day:
        return False
    return start.isoformat() <= day < (start + timedelta(days=WINDOW_DAYS)).isoformat()


def entry_identity(entry: dict, media_key: str) -> tuple:
    """What makes two calendar entries the same airing.

    The immutable Trakt id rather than the slug (a slug is user-changeable), plus
    the episode coordinates and the air time — a show legitimately appears many
    times in a month, and only the same episode at the same moment is a repeat.
    """
    media = entry.get(media_key) or {}
    ids = media.get("ids") or {}
    episode = entry.get("episode") or {}
    return (
        ids.get("trakt") or ids.get("slug"),
        episode.get("season"),
        episode.get("number"),
        entry.get("first_aired") or entry.get("released"),
    )


def dedupe_entries(entries: list[dict], media_key: str) -> list[dict]:
    """First occurrence of each airing, order preserved."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in entries:
        identity = entry_identity(entry, media_key)
        if identity in seen:
            continue
        seen.add(identity)
        out.append(entry)
    return out


def cache_key(endpoint_key: str, start: date) -> str:
    """The api_cache key for one window. Nothing but endpoint and window start —
    the cached data is complete and unfiltered, so there is no filter dimension
    to key on."""
    return f"calendar:{endpoint_key}:{start.isoformat()}"


# ---------------------------------------------------------------------------
# pruning — keep only what the normalizer and the filters read
# ---------------------------------------------------------------------------

# The immutable ids the normalizer emits (slug, trakt, tvdb, tmdb).
_MEDIA_ID_KEYS = ("slug", "trakt", "tvdb", "tmdb")
# Every scalar the normalizer or the genre/country filter reads off the media
# object. `genres` and `country` feed the filter; the rest are display fields.
_MEDIA_KEYS = (
    "title", "year", "network", "country", "language", "runtime",
    "status", "rating", "genres", "overview",
)
_EPISODE_KEYS = ("season", "number", "title")


def _prune_media(media: dict) -> dict:
    out = {k: media.get(k) for k in _MEDIA_KEYS if k in media}
    ids = media.get("ids") or {}
    out["ids"] = {k: ids.get(k) for k in _MEDIA_ID_KEYS if k in ids}
    # The normalizer's poster picker reads only images.poster; fanart, logos and
    # the rest of the extended image set are dropped, which is most of the bytes.
    poster = (media.get("images") or {}).get("poster")
    if poster:
        out["images"] = {"poster": poster}
    return out


def prune_entry(entry: dict, media_key: str) -> dict | None:
    """Reduce one raw Trakt calendar entry to the fields the read path consumes,
    or None when it carries no media object (which the normalizer would drop)."""
    media = entry.get(media_key)
    if not isinstance(media, dict):
        return None
    out: dict = {media_key: _prune_media(media)}
    # Both timestamps are kept verbatim — no conversion — because the normalizer
    # takes whichever is present and converts it into the viewer's tz at read
    # time. (`released` is a plain date on movies; `first_aired` an ISO UTC ts.)
    if entry.get("first_aired") is not None:
        out["first_aired"] = entry["first_aired"]
    if entry.get("released") is not None:
        out["released"] = entry["released"]
    episode = entry.get("episode")
    if isinstance(episode, dict):
        out["episode"] = {k: episode.get(k) for k in _EPISODE_KEYS if k in episode}
    return out


# ---------------------------------------------------------------------------
# fetch + store + read of one window
# ---------------------------------------------------------------------------

def _compress(entries) -> bytes:
    return zlib.compress(json.dumps(entries, separators=(",", ":")).encode("utf-8"), COMPRESS_LEVEL)


def _decompress(blob) -> list[dict]:
    data = json.loads(zlib.decompress(blob).decode("utf-8"))
    return data if isinstance(data, list) else []


async def fetch_window_raw(endpoint: Endpoint, settings, start: date) -> list[dict]:
    """Fetch one 7-day window from Trakt, UNFILTERED, PRUNED, and TRIMMED to the
    window's own 7 days.

    No `genres`/`countries` query params (all filtering is read-time now) and no
    pagination headers (calendar endpoints ignore them and return the whole
    window in one response). Logs a warning if Trakt ever starts paginating.

    The trim is not tidiness. Trakt does not honour the `days` bound it is given
    (see in_window), so consecutive windows overlap by days or weeks; storing
    what arrived would mean caching the same airings several times over and
    handing the page duplicate cards for every one of them.
    """
    url = (
        f"{trakt.API_BASE}/calendars/all/{endpoint.path}/{start.isoformat()}/{WINDOW_DAYS}"
        f"?{urlencode({'extended': 'full,images'})}"
    )
    resp = await trakt.shared_client().get(url, headers=trakt._headers(settings, paginate=False))
    if resp.status_code == 401:
        raise trakt.TraktError(
            "Trakt rejected the credentials (401). Check Client ID / Access Token in Settings.", 401,
        )
    if resp.status_code != 200:
        raise trakt.TraktError(f"Trakt API returned HTTP {resp.status_code}.", resp.status_code)
    if resp.headers.get("x-pagination-page-count"):
        # Calendar endpoints have never paginated (verified live); if that ever
        # changes this window is silently truncated, so make it loud.
        logger.warning(
            "Trakt calendar response carried pagination headers (page-count=%s) for %s; "
            "the window may be truncated.",
            resp.headers.get("x-pagination-page-count"), url,
        )
    try:
        raw = resp.json()
    except ValueError:
        raise trakt.TraktError("Trakt API returned an unreadable response.")
    if not isinstance(raw, list):
        return []
    pruned: list[dict] = []
    overrun = 0
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        item = prune_entry(entry, endpoint.media)
        if item is None:
            continue
        if not in_window(item, start):
            overrun += 1
            continue
        pruned.append(item)
    if overrun:
        logger.debug(
            "Trakt returned %d entr(ies) outside the %s window starting %s; trimmed.",
            overrun, endpoint.key, start,
        )
    return dedupe_entries(pruned, endpoint.media)


async def read_cached_window(endpoint_key: str, start: date) -> tuple[list[dict], int] | None:
    """The cached (entries, cached_at) for one window, or None when absent."""
    row = await db.fetch_one(
        "SELECT payload, cached_at FROM api_cache WHERE cache_key = ?",
        (cache_key(endpoint_key, start),),
    )
    if row is None:
        return None
    try:
        return _decompress(row["payload"]), int(row["cached_at"])
    except (zlib.error, ValueError):
        return None


async def store_window(endpoint_key: str, start: date, entries: list[dict],
                       ttl_seconds: int, now: int) -> None:
    blob = _compress(entries)
    await db.execute(
        "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, byte_size) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET "
        "payload = excluded.payload, cached_at = excluded.cached_at, "
        "ttl_seconds = excluded.ttl_seconds, byte_size = excluded.byte_size",
        (cache_key(endpoint_key, start), blob, now, ttl_seconds, len(blob)),
    )


def _ttl_seconds(settings) -> int:
    try:
        return max(0, int(settings.calendar_cache_ttl_minutes)) * 60
    except (TypeError, ValueError):
        return 600


async def load_window(endpoint: Endpoint, settings, start: date, *,
                      allow_fetch: bool = True, now: int | None = None) -> tuple[list[dict], int | None]:
    """Return (entries, cached_at) for one window.

    Fetches and caches when the window is missing or past its TTL and allow_fetch
    is set. A public share page passes allow_fetch=False: it serves whatever is
    cached — even stale, even nothing (returning [], None) — and never calls
    Trakt, so an unauthenticated visitor can never spend the instance's rate
    limit. cached_at is None only when nothing was cached and nothing was fetched.
    """
    ts = db.now() if now is None else now
    ttl = _ttl_seconds(settings)
    cached = await read_cached_window(endpoint.key, start)
    if cached is not None:
        entries, cached_at = cached
        if not allow_fetch or (ts - cached_at) <= ttl:
            return entries, cached_at
    elif not allow_fetch:
        return [], None
    try:
        entries = await fetch_window_raw(endpoint, settings, start)
    except trakt.TraktError:
        if cached is not None:  # serve the stale copy rather than nothing
            return cached
        raise
    await store_window(endpoint.key, start, entries, ttl, ts)
    return entries, ts


# ---------------------------------------------------------------------------
# the assembled read path
# ---------------------------------------------------------------------------

def _month_utc_range(tz: ZoneInfo, year: int, month: int) -> tuple[date, date]:
    """The UTC date range whose windows cover the viewer's LOCAL month ±1 day.

    The month boundary is viewer-dependent — an item at 02:00 UTC on the 1st is
    the previous month for a UTC-8 viewer and this month for a UTC+2 one — so the
    range is padded a day each side in the viewer's tz and then expressed in UTC,
    where the windows live. The final trim back to the exact local month happens
    after normalization, never in UTC.
    """
    days = _calendar.monthrange(year, month)[1]
    local_start = datetime(year, month, 1, tzinfo=tz)
    local_end = datetime(year, month, days, 23, 59, 59, tzinfo=tz)
    utc_start = (local_start - timedelta(days=1)).astimezone(timezone.utc).date()
    utc_end = (local_end + timedelta(days=1)).astimezone(timezone.utc).date()
    return utc_start, utc_end


async def read_month(endpoint: Endpoint, settings, *, tz: ZoneInfo, year: int, month: int,
                     genres: str = "", countries: str = "", network_filter=None,
                     allow_fetch: bool = True, now: int | None = None) -> tuple[list[dict], int | None]:
    """Produce one viewer's normalized, filtered, month-trimmed calendar items.

    The read path in order: figure the UTC window range covering the viewer's
    local month ±1 day; load each aligned window (fetching+caching, or cache-only
    on a share page); apply the per-user genre/country filter to the RAW entries
    (before normalization, on the raw slugs); normalize the survivors into the
    viewer's tz; trim to the viewer's LOCAL month; apply the network filter; sort
    by air time. Returns (items, as_of) where as_of is the oldest contributing
    window's cached_at — the "data as of" timestamp for a share page — or None
    when nothing was cached and nothing fetched.

    The not-watching overlay and the hide/card/day-packing view preferences are
    the caller's to apply: those are per-request view concerns, not part of the
    shared data model this returns.
    """
    utc_start, utc_end = _month_utc_range(tz, year, month)
    entries: list[dict] = []
    as_of: int | None = None
    for start in aligned_windows(utc_start, utc_end):
        window_entries, cached_at = await load_window(
            endpoint, settings, start, allow_fetch=allow_fetch, now=now,
        )
        entries.extend(window_entries)
        if cached_at is not None:
            as_of = cached_at if as_of is None else min(as_of, cached_at)

    # Belt and braces over the trim in fetch_window_raw. That one keeps NEW
    # windows disjoint; this one also covers windows cached BEFORE the trim
    # existed, which overlap and would otherwise keep rendering doubled cards
    # until their TTL expired. It is a no-op once every window has been refetched.
    entries = dedupe_entries(entries, endpoint.media)

    kept = calendar_filter.filter_entries(entries, endpoint.media, genres, countries)

    items: list[dict] = []
    for entry in kept:
        item = trakt.normalize(entry, endpoint, tz)
        if item is None:
            continue
        air_date = item["air_date"]  # YYYY-MM-DD, already in the viewer's tz
        if int(air_date[:4]) == year and int(air_date[5:7]) == month:
            items.append(item)

    if network_filter:
        allow = set(network_filter)
        items = [i for i in items if i["network"] in allow]
    items.sort(key=lambda i: i["air_ts"])
    return items, as_of
