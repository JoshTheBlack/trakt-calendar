"""The optional import of titles somebody has already finished.

THE ONLY MODULE IN THIS FEATURE THAT KNOWS THE OTHER ONE EXISTS. Everything else
— the board data layer, the routes, the renderer — works identically for an
account that has never touched it, which is the whole point of the separation:
this file could be deleted and the rankings feature would lose one convenience
button and nothing else.

That is also why the queries below read those tables directly instead of the
aggregation living beside them. Confining the coupling to one module makes it
visible and removable; spreading a ranker-shaped query into the other feature's
data layer would make each one harder to change without thinking about the
other.

WHAT COUNTS AS FINISHED is not re-derived here. A closed month has its verdict
already stored; an open one is computed with the same function that renders it,
so the import can never disagree with what the user is looking at.

AVAILABILITY IS ABSENCE, NOT REFUSAL. An account without the other feature is
told nothing about it — no disabled button, no explanatory error. The route
answers 404, exactly as it would for a source that does not exist.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import artwork, auth, db, discord_fmt, trakt
from .config import load_settings
from .ranker_sources import Media, TitleRef, int_or_none

logger = logging.getLogger(__name__)

# A movie row carries no tmdb of its own, so importing movies costs one id
# lookup apiece the first time. Bounded so a large library cannot turn one click
# into hundreds of provider calls; the rest import on the next run, by which
# time the first batch is cached.
MAX_MOVIE_LOOKUPS = 200

# How many of those lookups are in flight at once. Matches the fan-out the rest
# of the app settled on for provider calls; higher just spends the same rate
# limit faster.
_LOOKUP_FAN_OUT = 4


async def tracker_available(user_id: int) -> bool:
    """Whether to offer the import at all: the account must be approved for the
    other feature AND actually have finished something in it. Approval without
    data would offer a button that imports nothing."""
    user = await auth.get_user(user_id)
    if user is None or not user["distrakt_approved"]:
        return False
    return bool(await _has_data(user_id))


async def _has_data(user_id: int) -> bool:
    show = await db.fetch_value(
        "SELECT 1 FROM distrakt_shows WHERE user_id = ? LIMIT 1", (user_id,))
    if show:
        return True
    return bool(await db.fetch_value(
        "SELECT 1 FROM distrakt_movie_watches WHERE user_id = ? LIMIT 1", (user_id,)))


async def available_years(user_id: int, media: Media) -> list[int]:
    """The years this account has finished something in, newest first.

    The filter offers only these because a year with nothing behind it is a
    choice that can only disappoint. The year is the one it was WATCHED in, not
    the one the title came out — see finished_titles.
    """
    if media is Media.MOVIE:
        rows = await db.fetch_all(
            "SELECT DISTINCT substr(watched_at, 1, 4) AS y FROM distrakt_movie_watches "
            " WHERE user_id = ? AND watched_at IS NOT NULL AND watched_at <> ''",
            (user_id,),
        )
    else:
        rows = await db.fetch_all(
            "SELECT DISTINCT substr(month, 1, 4) AS y FROM distrakt_shows WHERE user_id = ?",
            (user_id,),
        )
    years = {int(row["y"]) for row in rows if (row["y"] or "").isdigit()}
    return sorted(years, reverse=True)


class TrackerFinishedTitles:
    """`FinishedTitlesSource` over the other feature's own records."""

    async def finished_titles(
        self, user_id: int, *, media: Media, year: int | None = None,
    ) -> list[TitleRef]:
        if media is Media.MOVIE:
            return await _finished_movies(user_id, year)
        return await _finished_shows(user_id, year)


def finished_titles_source() -> TrackerFinishedTitles:
    """The seam a route asks for, so no route names the implementation."""
    return TrackerFinishedTitles()


# ---------------------------------------------------------------------------
# shows
# ---------------------------------------------------------------------------

async def _finished_shows(user_id: int, year: int | None) -> list[TitleRef]:
    """One TitleRef per show with at least one finished season.

    SEASON AND EPISODE COUNTS MEAN "HOW MUCH OF THIS YOU FINISHED", not how long
    the show ran: seasons is how many of them were completed, episodes is the
    total across exactly those seasons. A reader who assumes otherwise will
    wonder why a long-running show imports as two seasons.

    Identity comes from the most recent record, because a title's name, network
    and ids are all things the other feature refreshes as it goes — the newest
    row is the one that saw them last.
    """
    rows = await db.fetch_all(
        "SELECT s.*, m.closed AS month_closed "
        "  FROM distrakt_shows s "
        "  JOIN distrakt_months m ON m.user_id = s.user_id AND m.month = s.month "
        " WHERE s.user_id = ? "
        " ORDER BY s.month",
        (user_id,),
    )

    # trakt_id -> {identity fields, the completed seasons and their totals}
    finished: dict[int, dict[str, Any]] = {}
    for row in rows:
        if year is not None and not str(row["month"]).startswith(f"{year:04d}"):
            continue
        if _bucket(row) != "completed":
            continue
        entry = finished.setdefault(int(row["trakt_id"]), {"seasons": {}})
        # Later months overwrite earlier ones because the rows arrive in month
        # order, which is what makes this "from the most recent row".
        entry.update({
            "title": row["title"] or "",
            "network": row["network"] or "",
            "tmdb": row["tmdb"],
            "slug": row["slug"] or "",
        })
        # Keyed by season so the same season finished across two months (a split
        # cour, or a correction) counts once rather than doubling the episodes.
        entry["seasons"][int(row["season"])] = int(row["total"] or 0)

    refs = []
    for trakt_id, entry in finished.items():
        seasons = entry["seasons"]
        refs.append(TitleRef(
            media=Media.SHOW,
            title=entry["title"],
            ids=_ids(trakt_id, entry["slug"], entry["tmdb"]),
            network=entry["network"],
            season_count=len(seasons),
            episode_count=sum(seasons.values()),
        ))
    logger.info("tracker import: %d finished show(s) for user %s (year=%s)",
                len(refs), user_id, year)
    return refs


def _bucket(row) -> str | None:
    """What state a record is in, without re-deriving the rule.

    A closed month's verdict was decided and stored when it froze, and must not
    be recomputed — its live counts stopped being refreshed at that moment. An
    open month has no stored verdict yet, so it is computed exactly as the other
    feature computes it for display.
    """
    if row["month_closed"]:
        return row["bucket"]
    record = {"abandoned": bool(row["abandoned"]), "season": row["season"]}
    live = {
        "watched": row["watched"], "total": row["total"],
        "started_airing": bool(row["started_airing"]),
        "finished_airing": bool(row["finished_airing"]),
        "cadence": row["cadence"],
    }
    return discord_fmt.bucket_of(record, live)


def _ids(trakt_id: int, slug: str, tmdb: Any) -> dict[str, Any]:
    """The provenance map for a record that was only ever stored with these
    three. Omitting the unknown keys keeps the map a record of what was actually
    known rather than a shape full of nulls."""
    ids: dict[str, Any] = {"trakt": int(trakt_id)}
    if slug:
        ids["slug"] = slug
    if tmdb:
        ids["tmdb"] = int(tmdb)
    return ids


# ---------------------------------------------------------------------------
# movies
# ---------------------------------------------------------------------------

async def _finished_movies(user_id: int, year: int | None) -> list[TitleRef]:
    """Watched movies, each resolved to a real id map.

    Those records carry a Trakt id, a title and a year and nothing else — no
    tmdb — so each one needs a summary lookup before it can be keyed on the
    artwork id or matched against another service later. The lookups are cached
    and happen ONCE per movie at import time, never at render time, and the
    poster URL that comes back with them is recorded so the tile it will need
    later is a lookup already paid for.
    """
    rows = await db.fetch_all(
        "SELECT trakt_id, title, year, watched_at FROM distrakt_movie_watches "
        " WHERE user_id = ? ORDER BY watched_at DESC",
        (user_id,),
    )
    wanted = [
        row for row in rows
        if year is None or str(row["watched_at"] or "").startswith(f"{year:04d}")
    ]
    if len(wanted) > MAX_MOVIE_LOOKUPS:
        logger.info("tracker import: capping %d movie lookups at %d for user %s",
                    len(wanted), MAX_MOVIE_LOOKUPS, user_id)
        wanted = wanted[:MAX_MOVIE_LOOKUPS]

    # The instance credential: a movie summary is public catalogue data, and the
    # per-user token this import runs under says nothing extra about it.
    settings = load_settings()
    # Fanned out rather than walked one at a time — a hundred sequential lookups
    # would be a minute-long request the first time an account imports. Bounded
    # so an import is a burst the provider can absorb rather than a flood that
    # earns the whole instance a rate limit.
    limit = asyncio.Semaphore(_LOOKUP_FAN_OUT)

    async def _one(row):
        async with limit:
            return await _movie_ref(settings, row)

    resolved = await asyncio.gather(*(_one(row) for row in wanted))
    refs = [ref for ref, _ in resolved]
    sightings = [sighting for _, sighting in resolved if sighting]
    if sightings:
        await artwork.record_poster_urls(sightings)
    logger.info("tracker import: %d watched movie(s) for user %s (year=%s)",
                len(refs), user_id, year)
    return refs


async def _movie_ref(settings, row) -> tuple[TitleRef, tuple | None]:
    """One watched-movie record as a TitleRef, plus any poster URL worth
    recording.

    A lookup that fails degrades to the Trakt id alone rather than dropping the
    movie: it still ranks perfectly well, it just has no poster until something
    else discovers its tmdb.
    """
    trakt_id = int(row["trakt_id"])
    ids: dict[str, Any] = {"trakt": trakt_id}
    title, year, runtime, poster = row["title"] or "", int_or_none(row["year"]), None, None
    try:
        summary = await trakt.fetch_movie_summary(settings, trakt_id)
    except trakt.TraktError as exc:
        logger.info("tracker import: no summary for movie %s (%s)", trakt_id, exc)
        summary = None
    if summary:
        ids = trakt.ids_map(summary) or ids
        title = summary.get("title") or title
        year = int_or_none(summary.get("year")) or year
        runtime = int_or_none(summary.get("runtime"))
        # The same extraction the calendar's own recording path uses, so a URL
        # observed here is stored identically to one observed there.
        poster = trakt._poster(summary)
    tmdb = int_or_none(ids.get("tmdb"))
    sighting = ("movie", tmdb, "trakt", poster) if (tmdb and poster) else None
    return TitleRef(media=Media.MOVIE, title=title, ids=ids, year=year, runtime=runtime), sighting
