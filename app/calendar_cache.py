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

  - `genres`/`countries`/`show_certifications`/`movie_certifications` ARE NO
    LONGER SENT TO TRAKT AS QUERY PARAMS, but they DO apply once, at fetch
    time, as the instance-wide content floor: an item any of those four
    Settings fields excludes is filtered out of the raw response before it is
    ever pruned and stored, so it never reaches api_cache at all (see
    app/calendar_filter.py). Every OTHER filter dimension — a signed-in
    viewer's own genre/country/certification/network choices — is a separate,
    read-time layer applied per viewer against that same floored cache.

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

import asyncio
import calendar as _calendar
import json
import logging
import time as _time
import zlib
from datetime import date, datetime, timedelta, timezone
from itertools import groupby
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import artwork, calendar_filter, db, trakt
from .cache import COMPRESS_LEVEL
from .endpoints import ENDPOINTS, Endpoint
from .providers.base import Item

logger = logging.getLogger(__name__)
# Same "app.perf" logger app/trakt.py's _cached_get already uses for its own
# netGET/cacheHIT lines — one DEBUG channel for every outbound Trakt call,
# regardless of which module made it. Enable it with LOG_LEVEL=DEBUG.
_perf = logging.getLogger("app.perf")

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
# Every scalar the normalizer or the genre/country/certification filter reads
# off the media object. `genres`, `country`, and `certification` feed the
# filter; the rest are display fields.
_MEDIA_KEYS = (
    "title", "year", "network", "country", "language", "runtime",
    "status", "rating", "genres", "overview", "certification",
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


def _poster_sighting(media: dict, media_key: str):
    """(media_key, tmdb, 'trakt', url) for one pruned media object, or None when
    it lacks either id — the pair the ranker's poster registry is keyed on."""
    tmdb_id = (media.get("ids") or {}).get("tmdb")
    poster = trakt._poster(media)
    if not tmdb_id or not poster:
        return None
    return (media_key, int(tmdb_id), "trakt", poster)


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
    """Fetch one 7-day window from Trakt, floor-filtered, PRUNED, and TRIMMED to
    the window's own 7 days.

    No `genres`/`countries` query params (Trakt's server-side filtering is gone;
    see filter_entries below for why it is reproduced here instead) and no
    pagination headers (calendar endpoints ignore them and return the whole
    window in one response). Logs a warning if Trakt ever starts paginating.

    The trim is not tidiness. Trakt does not honour the `days` bound it is given
    (see in_window), so consecutive windows overlap by days or weeks; storing
    what arrived would mean caching the same airings several times over and
    handing the page duplicate cards for every one of them.
    """
    url = (
        f"{trakt.API_BASE}/calendars/all/{trakt.calendar_path(endpoint)}/{start.isoformat()}/{WINDOW_DAYS}"
        f"?{urlencode({'extended': 'full,images'})}"
    )
    t0 = _time.perf_counter()
    resp = await trakt._send(trakt.shared_client(), "GET", url, headers=trakt._headers(settings, paginate=False))
    _perf.debug("netGET    calendar/%s/%s -> %s  %.0fms", endpoint.key, start.isoformat(),
                resp.status_code, (_time.perf_counter() - t0) * 1000.0)
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
    # The instance-wide content floor: an operator who excludes a genre,
    # country, or certification here means it never enters the shared cache for
    # ANY viewer, not just their own — applied on the raw entries, before
    # pruning, the same way trakt.py's uncached fetch path already reproduces
    # Trakt's old server-side genre/country filtering (see calendar_filter.py).
    certifications = (
        settings.show_certifications if endpoint.media == "show" else settings.movie_certifications
    )
    raw = calendar_filter.filter_entries(
        raw, endpoint.media, settings.genres, settings.countries, certifications,
    )
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
    # Recorded only on a genuine fetch, not on every cache-hit read: the URL
    # arrived here already, so this is the point a lookup is "paid for" rather
    # than a per-view cost added to the hot render path.
    sightings = (
        s for s in (
            _poster_sighting(entry.get(endpoint.media) or {}, endpoint.media)
            for entry in entries
        ) if s is not None
    )
    await artwork.record_poster_urls(sightings)
    return entries, ts


# ---------------------------------------------------------------------------
# the assembled read path
# ---------------------------------------------------------------------------

def day_label(day: date) -> str:
    """The heading a day's block carries ("Friday, 03 July").

    Shared rather than formatted at each call site: a day that fails to load is
    still announced by its date, and a placeholder or an error whose heading is
    spelled differently from the real one reads as a different day."""
    return day.strftime("%A, %d %B")


def _local_span_utc_range(tz: ZoneInfo, start_date: date, end_date: date) -> tuple[date, date]:
    """The UTC date range whose aligned windows cover the viewer-LOCAL day span
    [start_date, end_date], padded a day each side.

    A local day is viewer-dependent in UTC — an item at 02:00 UTC on the 1st is
    the previous local day for a UTC-8 viewer and this one for a UTC+2 one, and a
    single local day can even straddle two UTC windows once the offset is applied
    — so the span is padded a day each side in the viewer's tz and then expressed
    in UTC, where the windows live. The final trim back to the exact local span
    happens after normalization, never in UTC.
    """
    local_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    local_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=tz)
    utc_start = (local_start - timedelta(days=1)).astimezone(timezone.utc).date()
    utc_end = (local_end + timedelta(days=1)).astimezone(timezone.utc).date()
    return utc_start, utc_end


async def assemble_range(endpoint: Endpoint, settings, *, tz: ZoneInfo,
                         start_date: date, end_date: date,
                         genres: str = "", countries: str = "",
                         show_certifications: str = "", movie_certifications: str = "",
                         network_filter=None, not_watching_ids: set[str] | None = None,
                         allow_fetch: bool = True, now: int | None = None,
                         ) -> tuple[list[dict], dict]:
    """Assemble one viewer's calendar for the local day span [start_date, end_date].

    The single place the cache is turned into view-ready days. It reads ONLY the
    aligned windows that cover the span — not the whole month — normalizes ONLY
    those entries into the viewer's tz, trims to [start_date, end_date], groups by
    local day, and returns (grouped, meta). A whole-month read is just
    assemble_range(first_of_month, last_of_month); a single day is
    assemble_range(d, d).

    The read path in order: figure the UTC window range covering the span ±1 day
    (a viewer-local day can straddle two UTC windows, so the padding matters);
    load every covering window CONCURRENTLY; apply the per-user
    genre/country/certification filter to the RAW entries (before normalization,
    on the raw slugs); normalize the survivors into the viewer's tz; trim to the
    LOCAL span; apply the network filter; sort by air time; group by local day.

    RESILIENT BUT LOUD on a window Trakt can't supply. The windows load through a
    single asyncio.gather; a window that raised (nothing cached AND the fetch
    failed) is skipped so the rest of the span still renders, and meta['partial']
    is set so the caller can say the data is incomplete. Only a span where EVERY
    window failed raises TraktError — there is genuinely nothing to show. (A
    public share read passes allow_fetch=False, where a missing window returns
    empty rather than raising, so it never trips the partial path.)

    `show_certifications`/`movie_certifications` are two separate specs (the two
    vocabularies don't overlap); the one matching `endpoint.media` applies here.

    `meta` carries: total, watching, not_watching (from not_watching_ids, if
    given — otherwise every item counts as watching), show_ids (the span's full,
    de-duped, air-ordered item-id list, which is what is-new diffs against and
    must never be taken from a partially-rendered DOM), as_of (the oldest
    contributing window's cached_at, or None), and partial.

    The hide/card/day-packing view preferences remain the caller's to apply:
    those are per-request view concerns, not part of the data model returned.
    """
    utc_start, utc_end = _local_span_utc_range(tz, start_date, end_date)
    windows = aligned_windows(utc_start, utc_end)

    # gather preserves argument order, so `results` stays in ascending window
    # order and extending `entries` in that order keeps the windows ordered —
    # which dedupe_entries below relies on to keep the SAME copy of an
    # overlapping airing the old sequential loop did (first window wins).
    # return_exceptions=True both lets a single window fail without aborting the
    # span and stops a still-running sibling fetch from surfacing as an
    # "exception was never retrieved" warning.
    results = await asyncio.gather(
        *(load_window(endpoint, settings, start, allow_fetch=allow_fetch, now=now)
          for start in windows),
        return_exceptions=True,
    )

    entries: list[dict] = []
    as_of: int | None = None
    errored = 0
    first_error: trakt.TraktError | None = None
    for result in results:
        if isinstance(result, trakt.TraktError):
            # load_window already served a stale copy when it had one, so getting
            # here means this window had nothing cached AND its fetch failed.
            errored += 1
            first_error = first_error or result
            continue
        if isinstance(result, BaseException):
            # An unexpected failure (not a Trakt reachability problem) is a real
            # bug, not a degraded window — surface it instead of hiding it behind
            # the partial flag.
            raise result
        window_entries, cached_at = result
        entries.extend(window_entries)
        if cached_at is not None:
            as_of = cached_at if as_of is None else min(as_of, cached_at)

    if errored and errored == len(windows):
        # Every window failed and none had a cached copy: there is no degraded
        # span to render, so surface it as the caller's hard error.
        raise first_error
    partial = errored > 0

    # Belt and braces over the trim in fetch_window_raw. That one keeps NEW
    # windows disjoint; this one also covers windows cached BEFORE the trim
    # existed, which overlap and would otherwise keep rendering doubled cards
    # until their TTL expired. It is a no-op once every window has been refetched.
    entries = dedupe_entries(entries, endpoint.media)

    certifications = show_certifications if endpoint.media == "show" else movie_certifications
    kept = calendar_filter.filter_entries(entries, endpoint.media, genres, countries, certifications)

    items: list[Item] = []
    for entry in kept:
        item = trakt.normalize(entry, endpoint, tz)
        if item is None:
            continue
        air_day = date.fromisoformat(item.air_date)  # already in the viewer's tz
        if start_date <= air_day <= end_date:
            items.append(item)

    if network_filter:
        allow = set(network_filter)
        items = [i for i in items if i.network in allow]
    items.sort(key=lambda i: i.air_ts)

    grouped = [
        {"date": day,
         "label": day_label(date.fromisoformat(day)),
         "items": list(rows)}
        for day, rows in groupby(items, key=lambda i: i.air_date)
    ]

    nw = not_watching_ids or set()
    not_watching_count = sum(1 for i in items if i.id in nw)
    meta = {
        "total": len(items),
        "watching": len(items) - not_watching_count,
        "not_watching": not_watching_count,
        # De-duped, first-airing order: one show airing a dozen times in a month
        # is one show as far as "which of these is new since last time" goes, and
        # this list is stored per user per view.
        "show_ids": list(dict.fromkeys(i.id for i in items)),
        "as_of": as_of,
        "partial": partial,
    }
    return grouped, meta


async def read_month(endpoint: Endpoint, settings, *, tz: ZoneInfo, year: int, month: int,
                     genres: str = "", countries: str = "",
                     show_certifications: str = "", movie_certifications: str = "",
                     network_filter=None,
                     allow_fetch: bool = True, now: int | None = None) -> tuple[list[Item], int | None]:
    """One viewer's normalized, filtered, month-trimmed calendar items, as a flat
    (items, as_of) pair — the shape the calendar route, the share pages, and the
    distrakt import already unpack.

    A thin wrapper over assemble_range for the whole local month (its [1st, last]
    span): assemble_range owns the window math, the concurrent fetch, and the
    normalize/trim/group. This keeps the well-worn (items, as_of) return; a caller
    that also needs the partial-data flag or the per-span counts calls
    assemble_range directly and reads them off `meta`.
    """
    days = _calendar.monthrange(year, month)[1]
    grouped, meta = await assemble_range(
        endpoint, settings, tz=tz,
        start_date=date(year, month, 1), end_date=date(year, month, days),
        genres=genres, countries=countries,
        show_certifications=show_certifications, movie_certifications=movie_certifications,
        network_filter=network_filter, allow_fetch=allow_fetch, now=now,
    )
    items = [item for day in grouped for item in day["items"]]
    return items, meta["as_of"]


# ---------------------------------------------------------------------------
# heartbeat pre-warm
# ---------------------------------------------------------------------------

PREWARM_DAYS = 60

# In-memory only: resets on restart, which just causes one extra (harmless)
# warm right after a deploy rather than losing pre-warm state permanently.
_last_prewarm_at: int | None = None


async def prewarm_calendar_cache(settings, *, now: int | None = None) -> None:
    """Fill the shared window cache ahead of any viewer, GATED behind the
    calendar_prewarm_enabled setting and the calendar_cache_ttl_minutes floor.

    Below a 24h TTL the pre-warmed windows would expire before a viewer could
    ever benefit from them, so pre-warming is skipped entirely rather than
    spending a Trakt call for nothing. Runs at most once per TTL, tracked by an
    in-memory marker (see _last_prewarm_at).

    Warms at the WINDOW layer via load_window, not assemble_range/read_month:
    the cached rows are user-independent, so normalizing them for a fake viewer
    here would be wasted work — a real request normalizes on read. Every
    calendar endpoint is warmed across the aligned windows covering
    [now, now + PREWARM_DAYS], the same api_cache the live read path fills.
    """
    global _last_prewarm_at
    if not settings.calendar_prewarm_enabled:
        return
    if settings.calendar_cache_ttl_minutes < 1440:
        return
    ts = db.now() if now is None else now
    ttl = _ttl_seconds(settings)
    if _last_prewarm_at is not None and (ts - _last_prewarm_at) < ttl:
        return
    _last_prewarm_at = ts

    today = datetime.fromtimestamp(ts, tz=timezone.utc).date()
    windows = aligned_windows(today, today + timedelta(days=PREWARM_DAYS))
    await asyncio.gather(
        *(load_window(endpoint, settings, start, allow_fetch=True, now=ts)
          for endpoint in ENDPOINTS.values()
          for start in windows),
        return_exceptions=True,
    )
    # Visible at normal log level on purpose (not perftrace.span, which is
    # DEBUG): this spends the instance's Trakt budget on a schedule with no
    # viewer present, and an operator should be able to see that it ran.
    _perf.info(
        "calendar pre-warm: %d endpoint(s) x %d window(s) covering %s..+%dd",
        len(ENDPOINTS), len(windows), today.isoformat(), PREWARM_DAYS,
    )
