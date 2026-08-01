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

from . import auth, db, distrakt, ranker_sources
from .providers.trakt import TraktError, calendar as trakt_calendar, detail as trakt_detail
from .config import load_settings
from .media import artwork
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


async def network_emojis(user_id: int) -> tuple[dict[str, str], str]:
    """This account's network -> emoji map and its default, or nothing at all.

    The Markdown export decorates each line with the emoji its author already
    chose for that network rather than inventing a second set of preferences for
    the same thing. It lives here for the same reason everything else in this
    file does: an account without the other feature must get plain text, not an
    error, and the rankings feature itself must not know that any of this
    exists.
    """
    user = await auth.get_user(user_id)
    if user is None or not user["distrakt_approved"]:
        return {}, ""
    return await distrakt.get_emoji_prefs(user_id)


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
    months = await db.fetch_all(
        "SELECT month, closed FROM distrakt_months WHERE user_id = ? ORDER BY month",
        (user_id,),
    )
    wanted = [row for row in months
              if year is None or str(row["month"]).startswith(f"{year:04d}")]

    # item key -> {identity fields, the completed seasons and their totals}
    finished: dict[str, dict[str, Any]] = {}
    # Months in order, so a later month's record overwrites an earlier one's —
    # which is what makes the identity fields come from the most recent row.
    for row in wanted:
        for record in await _completed_in(user_id, row["month"], bool(row["closed"])):
            entry = finished.setdefault(str(distrakt.record_key(record)), {"seasons": {}})
            entry.update({
                "title": record.get("title") or "",
                "network": record.get("network") or "",
                # The whole map the roster row carries, which is already the shape
                # a TitleRef wants — the tracker resolved these ids once and there
                # is nothing here to translate.
                "ids": dict(record.get("ids") or {}),
            })
            # Keyed by season so the same season finished across two months (a
            # split cour, or a correction) counts once rather than doubling the
            # episode total.
            entry["seasons"][int(record["season"])] = int(record.get("total") or 0)

    refs = [
        TitleRef(
            media=Media.SHOW,
            title=entry["title"],
            ids=entry["ids"],
            network=entry["network"],
            season_count=len(entry["seasons"]),
            episode_count=sum(entry["seasons"].values()),
        )
        for entry in finished.values()
    ]
    logger.info("tracker import: %d finished show(s) for user %s (year=%s)",
                len(refs), user_id, year)
    return refs


async def _completed_in(user_id: int, month: str, closed: bool) -> list[dict[str, Any]]:
    """The records finished during one month.

    A CLOSED MONTH ALREADY DECIDED. Its verdict was computed and stored the
    moment it froze, and its counts stopped being refreshed then — recomputing
    from them would unfinish shows that are finished.

    AN OPEN MONTH HAS NO STORED VERDICT, and the counts on its rows are not the
    live ones either: they are only written back when the month freezes, so a
    season finished this month sits at 0/0 in the database while the tracker's
    own screen shows it completed. Reading those rows directly is how this
    import came to report nothing for an account looking at six finished shows.
    So it asks the other feature to work the month out exactly as it does for
    display — one function, one answer, no second copy of the rule that can
    drift from what the user is looking at.

    That costs what viewing the month costs: an incremental history sync plus a
    per-record season lookup, both already cached. It happens once, on an
    explicit import, never on a render. With no usable credential the month is
    skipped rather than guessed at — an import that silently reported "nothing
    finished" is the bug this replaced.
    """
    if closed:
        rows = await db.fetch_all(
            "SELECT * FROM distrakt_shows WHERE user_id = ? AND month = ?", (user_id, month))
        # Read through the tracker's own row reader rather than as raw rows: the
        # id columns become the `ids` map every caller below expects, and this
        # module does not need a second opinion about the roster's storage shape.
        return [distrakt.row_to_show(row) for row in rows
                if row["bucket"] == distrakt.Bucket.COMPLETED]

    settings = await ranker_sources.user_trakt_settings(user_id)
    if settings is None or not settings.trakt_configured:
        logger.info("tracker import: skipping open month %s for user %s — no usable credential",
                    month, user_id)
        return []
    doc = await distrakt.load_month(user_id, month)
    records = (doc or {}).get("shows") or []
    if not records:
        return []
    # allow_degrade: one show whose season lookup fails is rendered from its
    # last-known fields rather than taking the whole import down with it.
    shows = await distrakt.compute_live_shows(
        user_id, records, settings, allow_degrade=True)
    return [show for show in shows
            if show.get("bucket") == distrakt.Bucket.COMPLETED]


# ---------------------------------------------------------------------------
# movies
# ---------------------------------------------------------------------------

async def _finished_movies(user_id: int, year: int | None) -> list[TitleRef]:
    """Watched movies, each resolved to a real id map.

    Those records carry the ids the tracker files them under, a title and a year,
    and no artwork id of their own unless the play happened to name one — so each
    still gets a summary lookup, which is also where the runtime and the poster URL
    come from. The lookups are cached and happen ONCE per movie at import time,
    never at render time, and the poster URL that comes back with them is recorded
    so the tile it will need later is a lookup already paid for.
    """
    rows = await db.fetch_all(
        "SELECT * FROM distrakt_movie_watches WHERE user_id = ? ORDER BY watched_at DESC",
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

    A lookup that fails degrades to the ids the row already carries rather than
    dropping the movie: it still ranks perfectly well, it just has no poster until
    something else discovers its tmdb.
    """
    # The id the row was FILED under, plus the source id a lookup is placed with —
    # after the re-key those are two different things, and a row with no source id
    # (a film some other service reported) simply skips the lookup.
    ids: dict[str, Any] = {row["match_source"]: row["match_id"]}
    trakt_id = int_or_none(row["trakt_id"])
    if trakt_id:
        ids["trakt"] = trakt_id
    title, year, runtime, poster = row["title"] or "", int_or_none(row["year"]), None, None
    summary = None
    if trakt_id:
        try:
            summary = await trakt_detail.fetch_movie_summary(settings, trakt_id)
        except TraktError as exc:
            logger.info("tracker import: no summary for movie %s (%s)", trakt_id, exc)
    if summary:
        ids = trakt_detail.ids_map(summary) or ids
        title = summary.get("title") or title
        year = int_or_none(summary.get("year")) or year
        runtime = int_or_none(summary.get("runtime"))
        # The same extraction the calendar's own recording path uses, so a URL
        # observed here is stored identically to one observed there.
        poster = trakt_calendar.poster(summary)
    tmdb = int_or_none(ids.get("tmdb"))
    sighting = ("movie", tmdb, "trakt", poster) if (tmdb and poster) else None
    return TitleRef(media=Media.MOVIE, title=title, ids=ids, year=year, runtime=runtime), sighting
