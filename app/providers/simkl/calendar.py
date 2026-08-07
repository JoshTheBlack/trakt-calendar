"""Simkl's calendar, and the normalizer that turns what it returns into the
uniform `Record` every calendar source produces.

UNLIKE THE REST OF THIS PACKAGE, THIS IS NOT api.simkl.com. Simkl's calendar is
a set of pre-baked, edge-cached JSON files on a separate CDN host
(data.simkl.in), unauthenticated — no token, no client_id — and regenerated on
Simkl's own schedule every few hours. Nothing here reads settings.simkl_client_id
or settings.simkl_access_token; `settings` is accepted only because CalendarPort
says every source takes one.

ONE FILE SHAPE, TWO WAYS TO ADDRESS IT. The CDN serves a rolling "forward"
file per media kind (tv.json, anime.json, movie_release.json) and, separately,
one monthly archive per kind at /calendar/{YYYY}/{M}/{kind}.json. This module
reads ONLY the monthly archives, for every window — see DEVIATION below.

THE THREE FILES DO NOT MAP ONE-TO-ONE ONTO THE APP'S FIVE ENDPOINTS, IN EITHER
DIRECTION. Three show endpoints are derivations over the union of tv.json and
anime.json; and the movies endpoint is a derivation too, because Simkl files an
anime FILM on its anime calendar rather than its movie one. Which calendar an
anime entry belongs on is decided here, at the fill, from the entry's own
`anime_type` — see `is_anime_film`.

NORMALIZING HAPPENS HERE, ONCE, ON THE WAY IN, exactly as it does in the Trakt
package beside this one: the calendar cache stores Records and knows nothing
about Simkl's field layout, which is what lets a second source fill the same
cache instead of the cache growing a branch per source.

NOTHING IN THIS MODULE FILTERS. Filtering is the calendar feature's job and
happens on the far side of the cache — see trakt/calendar.py's docstring, which
says the same thing for the same reason.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import date, datetime, timedelta

from ...config import Settings
from ...endpoints import Endpoint
from ..base import Media, Record, Source, collect_ids
from . import transport
from .transport import SimklError

logger = logging.getLogger(__name__)
_perf = logging.getLogger("app.perf")

CDN_BASE = "https://data.simkl.in/calendar"

# The three underlying archive files, one per media kind Simkl's calendar
# publishes. Keyed by the name this module uses internally, not by the app's
# own endpoint keys — several app endpoints are different DERIVATIONS over the
# same file (see fetch_window).
_TV_FILE = "tv.json"
_ANIME_FILE = "anime.json"
_MOVIE_FILE = "movie_release.json"

# The app endpoints this source can answer, as the SHOW-shaped ones (they all
# derive from the union of tv.json and anime.json, just with a different
# filter over it). 'shows/finales' is deliberately absent — nothing in Simkl's
# calendar or catalog flags a season or series finale, and deriving one from
# the per-title episode lists is future work that needs its own drain
# infrastructure.
_SHOW_DERIVATIONS = frozenset({"shows/new", "shows/premieres", "shows"})

# WHAT anime.json CALLS A FILM. Simkl files an anime FILM on its anime
# calendar and not on its movie one — measured against the live CDN, no entry
# any month's anime.json marks a movie appears in that month's
# movie_release.json — so 'movies' is a derivation over TWO files, exactly as
# the show endpoints are, rather than movie_release.json alone.
#
# THE FIELD IS ON THE CALENDAR ENTRY, WHICH IS THE WHOLE REASON THIS CAN BE
# DECIDED HERE. `anime_type` is also on the per-title detail payload, and that
# copy arrives only with enrichment — minutes after a window is stored, which
# is why app/calendar/filter.py's `prune_disguised_films` has to run at READ.
# The CALENDAR file states it up front: measured over five consecutive months
# of live archives, all 1605 anime.json entries carried an `anime_type`, none
# was absent, and the values seen were tv 852, ona 727, movie 21, ova 4 and
# special 1. tv.json carries the field on nothing at all (0 of 24,693
# entries), which is consistent — a film that is not anime is already in
# movie_release.json. So the fill knows, and an entry can be routed to the
# calendar it belongs on instead of being stored on the wrong one and taken
# off it again later.
#
# EVERY OTHER VALUE IS A SERIAL FORMAT AND STAYS ON THE SHOW ENDPOINTS. `ona`
# is Original Net Animation — an anime released to the web, a series — and
# `ova`, `tv` and `special` are likewise episodic; only `movie` is a film.
# The same list, for the same reason, is written out in
# app/calendar/filter.py's `prune_disguised_films`.
_ANIME_FILM_TYPE = "movie"


def is_anime_film(entry: dict) -> bool:
    """Whether one anime.json entry is a FILM, per the file's own `anime_type`.

    The single place that reads the field, so "which calendar does this entry
    belong on" has one answer and the two sides of the split below cannot
    drift into disagreeing — an entry both sides claimed would render twice
    and one neither claimed would vanish, which is the defect this routing
    exists to end.
    """
    return str(entry.get("anime_type") or "").strip().lower() == _ANIME_FILM_TYPE


# ---------------------------------------------------------------------------
# fetching the CDN files, with conditional GET
# ---------------------------------------------------------------------------

def _cdn_cache_key(url: str) -> str:
    return f"simkl-cdn:{url}"


async def _conditional_get(url: str) -> list:
    """One CDN file's entries, using ETag / If-Modified-Since so an unchanged
    file costs a 304 rather than the megabytes it actually holds.

    Cached through the kernel's blob cache (app/cache.py), keyed by URL, with
    NO ttl_seconds cutoff on the read: the freshness question here is answered
    by Simkl's own ETag, not by our clock, so the stored copy is read
    regardless of its age and only ever replaced when the CDN says it changed.

    Raises SimklError — which IS SourceUnavailable — for anything that is not a
    200 or a 304 against a copy we actually hold, so a caller degrades this
    source rather than storing an empty answer as fact.
    """
    from ... import cache  # deferred: providers/ reads the kernel's cache module

    key = _cdn_cache_key(url)
    stored = await cache.get_stale(key)
    headers = {"User-Agent": transport.USER_AGENT}
    if isinstance(stored, dict):
        etag = stored.get("etag")
        last_modified = stored.get("last_modified")
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    t0 = _time.perf_counter()
    resp = await transport.send(
        transport.cdn_client(), "GET", url,
        pool=transport.CDN_POOL, headers=headers, timeout=45,
    )
    _perf.debug("netGET    %s -> %s  %.0fms", url, resp.status_code,
               (_time.perf_counter() - t0) * 1000.0)

    if resp.status_code == 304:
        if isinstance(stored, dict) and isinstance(stored.get("data"), list):
            # THE WHOLE POINT: the stored copy is served and NO second request
            # is made — a 304 carries no body to parse anyway.
            return stored["data"]
        raise SimklError(
            f"Simkl's calendar CDN answered 304 for {url} with no cached copy "
            f"to serve — the cache row must have been evicted between requests.")
    if resp.status_code == 404:
        # A month outside the published archive range. Not an error: the
        # caller (fetch_window) only asks for months inside the declared
        # Capabilities window, and Capabilities.covers() is what decides that
        # — so a 404 reaching here means the archive genuinely has nothing for
        # this month, which is an empty answer, not a refusal.
        return []
    if resp.status_code != 200:
        raise SimklError(
            f"Simkl's calendar CDN returned HTTP {resp.status_code} for {url}.", resp.status_code)
    try:
        data = resp.json()
    except ValueError:
        raise SimklError(f"Simkl's calendar CDN returned an unreadable response for {url}.")
    if not isinstance(data, list):
        data = []
    await cache.set(key, {
        "etag": resp.headers.get("ETag"),
        "last_modified": resp.headers.get("Last-Modified"),
        "data": data,
    })
    return data


def _archive_url(year: int, month: int, filename: str) -> str:
    # {M} carries NO leading zero — measured against the live CDN, and a
    # zero-padded month 404s.
    return f"{CDN_BASE}/{year}/{month}/{filename}"


async def _fetch_file(year: int, month: int, filename: str) -> list[dict]:
    entries = await _conditional_get(_archive_url(year, month, filename))
    return _dedupe_file_entries([e for e in entries if isinstance(e, dict)])


def _dedupe_file_entries(entries: list[dict]) -> list[dict]:
    """First occurrence of each (simkl_id, season, episode, date) tuple.

    Live measurement found no duplicates of this tuple within one file, in
    either a forward file or an archive month — but "verified today" is not
    "guaranteed tomorrow", and a caller reading a stale-but-still-served
    cached copy has no way to re-verify it. Movies have no episode
    coordinate, so their tuple is just (simkl_id, None, None, date), which is
    exactly the identity a re-listed release under a different rank would
    still collide on.
    """
    seen: set[tuple] = set()
    out: list[dict] = []
    for entry in entries:
        ids = entry.get("ids") or {}
        episode = entry.get("episode") or {}
        key = (ids.get("simkl_id"), episode.get("season"), episode.get("episode"), entry.get("date"))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _months_covering(start: date, days: int) -> list[tuple[int, int]]:
    """Every (year, month) the archive scheme needs fetching to cover
    [start, start + days)."""
    end = start + timedelta(days=max(days, 1) - 1)
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


# ---------------------------------------------------------------------------
# normalizing raw entries into Records
# ---------------------------------------------------------------------------

def _poster_url(path) -> str | None:
    """The full poster URL from Simkl's partial path ("19/198912937a2b29daaf"),
    or None. The path is NOT a URL on its own; the `_m` suffix asks Simkl's
    image host for the medium size, matched to what the calendar cards
    actually draw."""
    if not path:
        return None
    return f"https://simkl.in/posters/{path}_m.jpg"


def _simkl_ids(raw: dict) -> dict:
    """Simkl's ids block, remapped onto the app's ID_KEYS namespace.
    `simkl_id` -> `simkl`; everything else already matches. `collect_ids` drops
    the nulls Simkl sends EXPLICITLY (an anime entry's absent tmdb id is a real
    `"tmdb": null` in the payload, not an omitted key) as well as any key
    genuinely missing, so an anime entry with no tmdb reads the same either
    way."""
    return collect_ids({
        "simkl": raw.get("simkl_id"),
        "slug": raw.get("slug"),
        "tmdb": raw.get("tmdb"),
        "imdb": raw.get("imdb"),
        "mal": raw.get("mal"),
    })


def _record_id(ids_raw: dict) -> str:
    return str(ids_raw.get("slug") or ids_raw.get("simkl_id") or "")


def to_show_record(entry: dict) -> Record | None:
    """One tv.json or anime.json entry as a Record, or None when it cannot be
    read at all.

    AN ENTRY WITH NO `episode` OBJECT AT ALL — measured live, one anime-movie
    entry inside anime.json was shaped this way — falls out of this naturally:
    `episode.get(...)` on `{}` is None either way, so the record comes out
    with no episode coordinate rather than raising.
    """
    ids_raw = entry.get("ids") or {}
    date_raw = entry.get("date")
    if not date_raw:
        return None
    try:
        dt = datetime.fromisoformat(str(date_raw))
    except ValueError:
        return None

    episode = entry.get("episode") or {}
    ep_season = episode.get("season")
    ep_number = episode.get("episode")
    ep_label = None
    if ep_season is not None and ep_number is not None:
        ep_label = f"S{int(ep_season):02d}E{int(ep_number):02d}"

    return Record(
        source=Source.SIMKL,
        media=Media.SHOW,
        id=_record_id(ids_raw),
        ids=_simkl_ids(ids_raw),
        # Simkl's own `url` is used AS GIVEN — a percent-encoded movie slug is
        # the same trap here as on the movie side (see to_movie_record), and
        # the entry's own url is already correctly encoded.
        detail_url=entry.get("url") or "https://simkl.com",
        title=entry.get("title") or "Untitled",
        air_ts=dt.timestamp(),
        poster=_poster_url(entry.get("poster")),
        episode_label=ep_label,
        episode_title="",
        season=int(ep_season) if ep_season is not None else None,
        episode_number=int(ep_number) if ep_number is not None else None,
        # Nothing else is on the calendar file at all: no genres, no network,
        # no country, no certification, no overview, no runtime. Left at
        # Record's defaults rather than guessed — a later enrichment pass
        # against Simkl's per-title detail endpoints is what fills these in.
        # `enriched=False` says so explicitly, so the per-viewer filter in
        # app/calendar/filter.py can tell this apart from a title that
        # genuinely has none of those values (see app/calendar/enrich.py).
        enriched=False,
    )


def to_movie_record(entry: dict) -> Record | None:
    """One movie_release.json entry as a Record, or None when it cannot be read.

    USES `release_date`, NOT `date`. Every movie entry's `date` is
    00:00:00 in the FILE's fixed offset (-04:00), which is not a real instant —
    it is a release DATE dressed as a timestamp. Parsing `release_date`
    directly at UTC midnight and setting `date_only=True` is what makes the
    same release land on the same calendar date for a viewer in Los Angeles and
    one in Auckland; parsing `date` and converting it would move the release a
    day for anyone west of the file's -04:00.
    """
    ids_raw = entry.get("ids") or {}
    release_date = entry.get("release_date")
    if not release_date:
        return None
    try:
        dt = datetime.fromisoformat(f"{release_date}T00:00:00+00:00")
    except ValueError:
        return None

    return Record(
        source=Source.SIMKL,
        media=Media.MOVIE,
        id=_record_id(ids_raw),
        ids=_simkl_ids(ids_raw),
        detail_url=entry.get("url") or "https://simkl.com",
        title=entry.get("title") or "Untitled",
        air_ts=dt.timestamp(),
        date_only=True,
        poster=_poster_url(entry.get("poster")),
        # See to_show_record's identical note just above.
        enriched=False,
    )


def to_anime_film_record(entry: dict) -> Record | None:
    """One anime.json entry that `is_anime_film` accepted, as a MOVIE-media
    Record for the movies calendar, or None when it cannot be read.

    READS `date`, NOT `release_date` — THE OPPOSITE OF to_movie_record, AND THE
    DIFFERENCE IS MEASURED RATHER THAN STYLISTIC. In movie_release.json the
    entry's `date` is a fiction (00:00:00 in the file's fixed -04:00 offset, a
    release DATE dressed as a timestamp) and `release_date` is the truth, which
    is why that normalizer reads the latter. In anime.json it is the other way
    round: `date` is a real instant carrying a real +09:00 offset — sampled
    values include 00:00, 08:00 and 23:00 JST, not one constant midnight — and
    `release_date` is the title's ORIGINAL release, which for a re-listing can
    be nowhere near the day the calendar is listing it on. Three of the 21 film
    entries measured across five months disagree by months or years: Girls und
    Panzer das Finale is calendared 2026-10-09 with a `release_date` of
    2017-12-09, and Kidou Keisatsu Patlabor EZY 2026-08-14 against 2026-05-15.
    Dating those from `release_date` would put them outside the window that
    fetched them, `in_window` in app/calendar/cache.py would trim them away,
    and the title would disappear again — the exact failure routing it here is
    meant to fix, moved one step later.

    NO `date_only`, FOR THE SAME REASON. A real offset converts correctly, so
    the film lands on the same local day for a viewer that the anime calendar
    would have shown it on; pinning it to a calendar date instead would move
    the title as a side effect of routing it, and routing is all this does.
    """
    ids_raw = entry.get("ids") or {}
    date_raw = entry.get("date")
    if not date_raw:
        return None
    try:
        dt = datetime.fromisoformat(str(date_raw))
    except ValueError:
        return None

    return Record(
        source=Source.SIMKL,
        media=Media.MOVIE,
        id=_record_id(ids_raw),
        ids=_simkl_ids(ids_raw),
        detail_url=entry.get("url") or "https://simkl.com",
        title=entry.get("title") or "Untitled",
        air_ts=dt.timestamp(),
        poster=_poster_url(entry.get("poster")),
        # NO EPISODE COORDINATE, even where the anime entry carried one. The
        # `episode` object on a film entry is Simkl's calendar shape rather
        # than a statement about the title — measured, films appear with
        # episode 1, episode 2, episode 5 and no episode object at all — and a
        # film drawn with an SxxEyy chip is the same untruth on the movies
        # calendar that a film on Series Premieres was on the series one.
        # `media=MOVIE` also means `resolve_key` keys this on the movie
        # identity space, so it can still merge with a Trakt film of the same
        # title where both know a shared id.
        #
        # `media` IS WHAT THE ENRICHMENT DRAIN PICKS AN ENDPOINT FROM
        # (app/calendar/enrich.py keys simkl_titles on (simkl_id, media)), so
        # a film routed here is looked up through GET /movies/{id} rather than
        # GET /tv/{id}. Verified live against ten real anime-film ids: every
        # one answered completely, by the same 302 to /anime/{id} the /tv/
        # lookup already relies on, which the existing redirect classification
        # in transport.py routes onto CATALOG_POOL without a change.
        enriched=False,
    )


def _anime_new(entries: list[dict]) -> list[dict]:
    """The anime entries that qualify for 'shows/new': episode 1, AND no
    earlier-dated episode 1 of the same title known within what was fetched.

    Anime carries no season at all (measured live: none of the anime.json
    entries sampled carried `episode.season`), so 'first episode' cannot be
    S01E01 the way it is for tv — it is "the
    earliest-dated episode 1 this title has". A title re-listed at episode 1
    on a later date (a rerun, a re-air) is NOT a second premiere and must not
    count as one twice; keeping only the earliest-dated candidate per
    simkl_id is what a single archive read can honestly say about that.
    """
    earliest: dict = {}
    for entry in entries:
        episode = entry.get("episode") or {}
        if episode.get("episode") != 1:
            continue
        sid = (entry.get("ids") or {}).get("simkl_id")
        current = earliest.get(sid)
        if current is None or (entry.get("date") or "") < (current.get("date") or ""):
            earliest[sid] = entry
    return list(earliest.values())


def _tv_new(entries: list[dict]) -> list[dict]:
    """The tv entries that qualify for 'shows/new': S01E01 exactly — a show's
    very first episode, as opposed to 'shows/premieres', which is episode 1 of
    ANY season (a returning show's new season)."""
    out = []
    for entry in entries:
        episode = entry.get("episode") or {}
        if episode.get("season") == 1 and episode.get("episode") == 1:
            out.append(entry)
    return out


def _premieres(entries: list[dict]) -> list[dict]:
    return [e for e in entries if (e.get("episode") or {}).get("episode") == 1]


# ---------------------------------------------------------------------------
# the port
# ---------------------------------------------------------------------------

async def fetch_window(endpoint: Endpoint, settings: Settings, start: date, days: int) -> list[Record]:
    """What Simkl's calendar says airs in [start, start + days), as Records.

    READS ONLY THE MONTHLY ARCHIVE FILES, for every window, including the
    current and near-future months — the CDN also serves a forward "rolling"
    file per media kind (tv.json, anime.json, movie_release.json with no date
    in the path), and this module does not read it. Measured live: the
    rolling files cover exactly the same ground the current and next couple
    of archive months already do, and the current month's archive was present
    and correctly sized on the day this was written. Reading one scheme
    uniformly means one conditional-GET target per file per month, computed
    the same way whether the window is a year behind today or three months
    ahead, rather than two different fetch paths that would otherwise both
    need reconciling for the month they overlap.

    Raises SimklError (a SourceUnavailable) when a file this window needs could
    not be read at all. Never returns an empty list for a failure — an empty
    answer must mean "Simkl genuinely had nothing here", never "Simkl could not
    be asked", because the calendar cache stores whichever one this returns as
    fact for the window's whole TTL.
    """
    months = _months_covering(start, days)

    if endpoint.key == "movies":
        # TWO FILES, BECAUSE SIMKL PUTS AN ANIME FILM ON THE ANIME CALENDAR.
        # movie_release.json is the whole of the non-anime answer, and
        # anime.json holds the films Simkl never lists there — see
        # `is_anime_film` above for the measurement and for why the file's own
        # `anime_type` can be trusted at fill time. The anime file is small
        # next to the movie one (66 to 699 entries a month against 459 to
        # 3674) and the show endpoints are already fetching it for these same
        # months through the same conditional GET, so the second read is
        # ordinarily a 304 against a copy the blob cache already holds.
        movie_results, anime_results = await asyncio.gather(
            asyncio.gather(*(_fetch_file(year, month, _MOVIE_FILE) for year, month in months)),
            asyncio.gather(*(_fetch_file(year, month, _ANIME_FILE) for year, month in months)),
        )
        raw = [e for batch in movie_results for e in batch]
        films = [e for batch in anime_results for e in batch if is_anime_film(e)]
        records = [to_movie_record(e) for e in raw]
        records += [to_anime_film_record(e) for e in films]
        return [r for r in records if r is not None]

    if endpoint.key not in _SHOW_DERIVATIONS:
        # 'shows/finales' and anything else this source has not declared in
        # its Capabilities.endpoints. The cache fill never asks for one of
        # these — capabilities.answers() keeps it from being asked — so this
        # is a safety net, not a path anything reaches today.
        return []

    tv_results, anime_results = await asyncio.gather(
        asyncio.gather(*(_fetch_file(year, month, _TV_FILE) for year, month in months)),
        asyncio.gather(*(_fetch_file(year, month, _ANIME_FILE) for year, month in months)),
    )
    tv_entries = [e for batch in tv_results for e in batch]
    # THE OTHER HALF OF THE SPLIT ABOVE, AND IT HAS TO BE THE OTHER HALF OF THE
    # SAME PREDICATE. A film the movies branch claims must leave the show
    # derivations here, or one title renders on both calendars; a film neither
    # claims disappears. Removing it at the FILL is what makes the show window
    # right for every viewer from the moment it is stored — the read-time
    # `prune_disguised_films` stays as the backstop for a window filled before
    # this split existed, and for a title the calendar file leaves unlabelled
    # and only enrichment turns out to call a film.
    anime_entries = [e for batch in anime_results for e in batch if not is_anime_film(e)]

    if endpoint.key == "shows":
        raw = tv_entries + anime_entries
    elif endpoint.key == "shows/premieres":
        raw = _premieres(tv_entries) + _premieres(anime_entries)
    else:  # "shows/new"
        raw = _tv_new(tv_entries) + _anime_new(anime_entries)

    return [r for r in (to_show_record(e) for e in raw) if r is not None]
