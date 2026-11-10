"""TMDB's calendars: what airs in a span of days, as Records.

THREE OF THE APP'S FIVE, each answered from what TMDB publishes:
  - Series Premieres: discover/tv by `first_air_date` — a show whose first
    episode airs in the span, as its S01E01;
  - Movie Premieres: discover/movie by `release_date` in the instance's region
    (Settings.tmdb_region), release types 2|3|4 — a film with a limited,
    theatrical or digital release there in the span;
  - Season Premieres: TMDB has no such listing, so it is derived, behind the
    request (premieres.py).
Season Finales and All Episodes are Trakt's and Simkl's: TMDB lists neither, and
deriving them would cost a request per show per week.

EVERY TMDB DATE IS A DAY, so every record is `date_only` (normalize.to_record):
a card TMDB describes is drawn on TMDB's day and borrows a time only from a
service whose instant falls on it (app/calendar/resolve.py). A LISTING IS SPARSE
— a discover entry names no network, status or certification — and says so
(`enriched=False`); the title store's refresh queue completes it by describing
the title (Capabilities.describes_sparse_listings).

A FILM IS DATED BY THE ENTRY'S OWN `release_date`, which is its PRIMARY release —
not its release in the region the listing was narrowed to. MEASURED over ten
weeks: the two are the same day for 1,037 of the 1,042 films whose first US
release of those types fell in their week, and the cache's trim to the window
then drops the films the listing names for a LATER release there (digital after
theatrical, a re-release) — each of which was listed in its own first week. One
day per film, the day the other services' film calendars give, and no request
per film to find it.

NOTHING HERE FILTERS AND NOTHING TRIMS: the calendar cache applies the content
floor and trims to the window for every source alike (app/calendar/cache.py).
"""
from __future__ import annotations

from datetime import date, timedelta

from ...config import Settings
from ...endpoints import Endpoint
from ..base import Media, Record
from . import discover, normalize, premieres

# The calendars this source answers — Capabilities.endpoints, stated where the
# answering is.
ENDPOINTS = frozenset({"shows/new", "shows/premieres", "movies"})

# TMDB's release types a Movie Premieres card is: limited theatrical (2),
# theatrical (3), digital (4). A premiere (1) is a festival screening, a
# physical release (5) and a TV airing (6) are not what a premieres calendar is
# for.
FILM_RELEASE_TYPES = "2|3|4"


async def fetch_window(endpoint: Endpoint, settings: Settings, start: date,
                       days: int) -> list[Record]:
    """What TMDB says airs in [start, start + days), as Records — or
    base.SourcePending for Season Premieres not worked out yet. Raises
    transport.TmdbError when TMDB could not be asked."""
    first, last = start, start + timedelta(days=max(int(days), 1) - 1)
    if endpoint.key == "shows/new":
        listed = await discover.entries(settings, "tv", "first_air_date", first, last)
        return _records(listed, Media.SHOW, 1, "first_air_date")
    if endpoint.key == "movies":
        listed = await discover.entries(settings, "movie", "release_date", first, last,
                                        region=settings.tmdb_region,
                                        with_release_type=FILM_RELEASE_TYPES)
        return _records(listed, Media.MOVIE, None, "release_date")
    if endpoint.key == "shows/premieres":
        return await premieres.window(settings, start, days)
    # The fill asks only what ENDPOINTS declares (Capabilities.answers), so this
    # is a caller asking for something TMDB never said it publishes.
    raise ValueError(f"TMDB publishes no {endpoint.key!r} calendar.")


def _records(listed: list[dict], media: Media, season: int | None,
             date_field: str) -> list[Record]:
    out = []
    for entry in listed:
        record = normalize.to_record(entry, media, season, str(entry.get(date_field) or ""))
        if record is not None:
            out.append(record)
    return out
