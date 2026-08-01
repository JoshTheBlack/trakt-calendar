"""Backfilling months the tracker was never running for, from Trakt watch history.

The tracker only ever grows forward: a month is created when it is reached, and
a month nobody was tracking at the time stays permanently empty (see
distrakt.can_initialize). That is the right default — it stops backward
month-nav from silently inventing history — but it leaves an account that
started tracking in July with no January to June at all, and nothing for the
ranker import to read for the first half of the year.

This fills those months in from the one record that does go back: the user's own
Trakt watch history.

WHAT A BACKFILLED MONTH CONTAINS: the seasons FINISHED during it, and nothing
else. "Finished during M" is the same rule the live tracker uses for its
Completed bucket — every episode of the season watched, and the last of them
watched inside M — so a season finished in May lands in May and in no other
month, and the "remove it from the months before" problem never arises. Shows
that were in progress but never finished are not recorded: nothing reads them
(app/ranker_import only ever asks for `bucket == 'completed'`), and inventing a
Cleanup/Keepup verdict for a month nobody was watching would be a guess.

Each month is written CLOSED, exactly as a month that had been tracked and then
frozen: the ranker import and the month view both read a frozen month straight
from its stored rows with no further Trakt calls.

TWO PHASES, because writing six months of history off one button is not
something to do on a mis-click, and because the sweep is far too expensive to
run twice: `survey` does all the Trakt work and stores the plan it would write
in the blob cache; `apply` writes back exactly that plan. The client sees a
summary and confirms; it never hands the records back, so nothing it says can
end up in storage.

COST: one paged history sweep for the whole range (not per month — the sweep is
the expensive part and it is not month-scoped), then one progress call per show
seen and one season call per candidate season. A few hundred rate-limited calls
for half a year, once.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from . import watch_history
# The roster is reached through this package's own public surface, the same names
# an outside caller uses, rather than through the modules those names are defined
# in — a backfill job has no business knowing which half of the tracker
# `load_month` or `save_month` lives in.
from .. import distrakt
from .. import cache, db, providers
from ..providers.base import Media, resolve_key

logger = logging.getLogger(__name__)

# How long a surveyed plan stays applicable. Long enough to read the summary and
# decide, short enough that "apply" cannot land a plan built from watch history
# as it stood yesterday.
PLAN_TTL_SECONDS = 30 * 60

# A survey is bounded to keep one button from sweeping an entire viewing life.
MAX_MONTHS = 24


def _plan_key(user_id: int) -> str:
    return f"distrakt_backfill_plan:{int(user_id)}"


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def valid_month(value) -> str | None:
    """'YYYY-MM' or None. Not an exception: these come from a form."""
    text = str(value or "").strip()
    return text if _MONTH_RE.match(text) else None


def _prev_month(month_key: str) -> str:
    year, month = int(month_key[:4]), int(month_key[5:7])
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def month_range(start_month: str, end_month: str) -> list[str]:
    """Every 'YYYY-MM' from start to end inclusive, or [] if either is unusable
    or they are the wrong way round."""
    start, end = valid_month(start_month), valid_month(end_month)
    if not start or not end:
        return []
    out: list[str] = []
    year, month = int(start[:4]), int(start[5:7])
    while f"{year:04d}-{month:02d}" <= end and len(out) < MAX_MONTHS:
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def default_range(today: date | None = None) -> tuple[str, str]:
    """The range to offer: January of the current year up to last month.

    Deliberately NOT clamped to stop before whatever is already tracked. It used
    to be, which made sense when this could only ever run once — but a month
    with content in it is skipped anyway, films are collected across the whole
    range, and an emptied month is refillable, so a range that shrank as months
    filled up left the useful part of a re-run unreachable from the default.
    The current month is excluded: it is the tracker's own to bucket.
    """
    today = today or date.today()
    start = f"{today.year:04d}-01"
    end = _prev_month(f"{today.year:04d}-{today.month:02d}")
    return (start, max(start, end))


async def survey(user_id: int, settings, start_month: str, end_month: str,
                 today: date | None = None) -> dict:
    """Work out what a backfill would write, WITHOUT writing any of it.

    Returns the plan, which is also stored for `apply` to pick up:
        {"range": [start, end], "months": {"YYYY-MM": [record, ...]},
         "movies": [...], "skipped": [...], "shows_seen": n, "seasons_seen": n}

    SEASONS AND FILMS ARE SCOPED DIFFERENTLY, on purpose.

    A month that already HOLDS SOMETHING is skipped for SEASONS: it carries
    decisions — abandons, manual adds, a row taken off by hand — that watch
    history knows nothing about, and rebuilding it from a sweep would throw them
    away. An EMPTY month is not one of those. Emptying a month leaves its month
    row behind, and judging by the row meant a month you had cleared out could
    never be refilled; judging by its contents means clearing one is how you ask
    for it to be built again.

    FILMS are collected across the WHOLE range regardless. They are per-play
    watch history rather than a monthly verdict, so there is nothing about an
    existing month for them to overwrite, and scoping them to the months being
    written made the films in every other month unreachable: once the seasons
    had been written the range had no writable months left, and re-running to
    pick up films reported nothing to do. Films already recorded at the same
    date or later are left out, so a second run offers only what is genuinely
    missing.
    """
    today = today or date.today()
    months = month_range(start_month, end_month)
    if not months:
        return _empty_plan(start_month, end_month)

    existing = await distrakt.months_with_shows(user_id)
    wanted = [m for m in months if m not in existing]
    skipped = [m for m in months if m in existing]

    # ONE paged history sweep for the whole range, read twice — once for the
    # seasons, once for the films.
    port = providers.for_tracker()
    if port is None:
        return _empty_plan(start_month, end_month)
    start_day = date(int(months[0][:4]), int(months[0][5:7]), 1)
    events = await port.fetch_history(settings, start_at=start_day.isoformat())
    films, films_known = await _split_films(user_id, port.movie_plays_from(events), set(months))

    by_month: dict[str, list[dict]] = {m: [] for m in wanted}
    seasons_seen = 0
    show_ids: list[int] = []
    if wanted:
        # Identity per (title, season), and the source ids to ask about. A
        # candidate naming no shared id is dropped here: it could not be stored
        # under an identity afterwards, so surveying it would promise a row the
        # apply step could not write.
        by_key: dict[tuple[str, int], dict] = {}
        for candidate in port.watched_progress_from(events):
            key = resolve_key(Media.SHOW, candidate.get("ids") or {})
            if key is not None:
                by_key[(str(key), int(candidate["season"]))] = candidate
        show_ids = list(dict.fromkeys(
            int(c["ids"]["trakt"]) for c in by_key.values() if c.get("ids", {}).get("trakt")
        ))

        progress = await _progress_by_show(settings, port, show_ids)
        totals = await _season_totals(settings, list(by_key.values()))

        wanted_set = set(wanted)
        for (key, season), ident in by_key.items():
            seasons_seen += 1
            source_id = int(ident["ids"]["trakt"]) if ident.get("ids", {}).get("trakt") else None
            episodes = (progress.get(source_id) or {}).get(season) or {}
            detail = totals.get((key, season)) or {}
            total = int(detail.get("total") or 0)
            if not total or len(episodes) < total:
                continue  # not finished at all -> belongs to no month
            finished_on = max((str(w)[:10] for w in episodes.values() if w), default="")
            month_key = finished_on[:7]
            if not month_key or month_key not in wanted_set:
                continue  # finished outside the range (or on a date Trakt cannot name)
            by_month[month_key].append(_completed_record(ident, season, total, detail))

    plan = {
        "range": [months[0], months[-1]],
        "months": {m: rows for m, rows in by_month.items() if rows},
        "movies": films,
        # Reported, not dropped: a run that found six films and wrote none of
        # them because it already had them must SAY that, or it reads exactly
        # like a run that could not see them at all.
        "movies_known": films_known,
        "skipped": skipped,
        "shows_seen": len(show_ids),
        "seasons_seen": seasons_seen,
    }
    await cache.set(_plan_key(user_id), plan)
    logger.info("distrakt backfill survey: user %s, %s..%s, %d month(s) of seasons, %d film(s)",
                user_id, months[0], months[-1], len(plan["months"]), len(films))
    return plan


def summarize(plan: dict) -> dict:
    """The plan as the confirmation dialog needs it: per month, how many seasons
    and which shows, with no roster records in it.

    The records stay server-side deliberately — the client's job is to say yes,
    not to hand back what should be stored.
    """
    months = plan.get("months") or {}
    movies = plan.get("movies") or []
    known = plan.get("movies_known") or []

    def _label(movie: dict) -> str:
        text = str(movie.get("title") or "Untitled")
        return f"{text} ({movie['year']})" if movie.get("year") else text

    by_month: dict[str, list[str]] = {}
    for movie in movies:
        by_month.setdefault(str(movie.get("watched_at") or "")[:7], []).append(_label(movie))
    known_by_month: dict[str, int] = {}
    for movie in known:
        key = str(movie.get("watched_at") or "")[:7]
        known_by_month[key] = known_by_month.get(key, 0) + 1

    # Films are listed month by month beside the seasons, not summed into one
    # number at the end: they are recorded by the same sweep, and a count nobody
    # can see the contents of is not something to confirm.
    keys = sorted(set(months) | set(by_month) | set(known_by_month))
    return {
        "range": plan.get("range") or [],
        "months": [
            {
                "month": key,
                "count": len(months.get(key) or []),
                "titles": sorted(
                    f"{r.get('title') or 'Untitled'} S{int(r.get('season') or 0):02d}"
                    for r in (months.get(key) or [])
                ),
                "movie_count": len(by_month.get(key) or []),
                "movie_titles": sorted(by_month.get(key) or []),
                "movie_known": known_by_month.get(key, 0),
            }
            for key in keys
        ],
        "movies": len(movies),
        "movies_known": len(known),
        "skipped": plan.get("skipped") or [],
        "shows_seen": plan.get("shows_seen") or 0,
        "seasons_seen": plan.get("seasons_seen") or 0,
        "total": sum(len(rows) for rows in months.values()),
    }


async def apply(user_id: int, plan: dict | None = None) -> dict:
    """Write a surveyed plan: one closed month per entry, plus the movie plays
    the same sweep saw. Returns {"months": [...], "shows": n, "movies": n}.

    Reads the plan the survey stored rather than taking one from the caller, so
    the only records that can be written are ones this module worked out itself.
    """
    plan = plan or await cache.get(_plan_key(user_id), PLAN_TTL_SECONDS)
    if not plan or not isinstance(plan, dict):
        raise BackfillExpired("Nothing surveyed to write — run the check again.")
    months = plan.get("months") or {}
    plan_movies = plan.get("movies") or []
    if not months and not plan_movies:
        return {"months": [], "shows": 0, "movies": 0}

    # Movies first, so each month's frozen **Movies** section can be built from
    # the same state a genuinely frozen month would have snapshotted. Recorded
    # even when no month has a finished season in it: they are watch history in
    # their own right, nothing else is ever going to record them for these
    # months, and the ranker reads them straight from that table.
    movie_count = await watch_history.record_movie_watches(user_id, plan_movies)
    state = await watch_history.load_state(user_id)

    written: list[str] = []
    show_count = 0
    filled = await distrakt.months_with_shows(user_id)
    for month_key in sorted(months):
        rows = months[month_key] or []
        if not rows:
            continue
        if month_key in filled:
            continue  # filled since the survey ran — never overwrite real content
        # An existing but EMPTY month is written into rather than replaced, so a
        # month row that has been around a while keeps whatever else is on it.
        doc = await distrakt.load_month(user_id, month_key) or distrakt.new_month_doc(month_key)
        doc["shows"] = list(rows)
        mstart, mend = watch_history.month_bounds(month_key)
        doc["movies"] = watch_history.movies_in_range(state, mstart, mend)
        doc["closed"] = True
        doc["totals_refreshed_at"] = db.now()
        await distrakt.save_month(user_id, doc)
        written.append(month_key)
        show_count += len(rows)

    # A month that was ALREADY closed and has just gained films keeps everything
    # it decided and gets its **Movies** section brought up to date — otherwise
    # the plan says "March: 2 films" and March goes on showing none.
    refreshed = 0
    if movie_count:
        for month_key in month_range(*(plan.get("range") or ["", ""])):
            if month_key in written:
                continue
            doc = await distrakt.load_month(user_id, month_key)
            if doc is None or not doc.get("closed"):
                continue  # an open month recomputes its films on every load
            mstart, mend = watch_history.month_bounds(month_key)
            films = watch_history.movies_in_range(state, mstart, mend)
            if films != (doc.get("movies") or []):
                await distrakt.set_month_movies(user_id, month_key, films)
                refreshed += 1

    await cache.set(_plan_key(user_id), None)
    logger.info("distrakt backfill applied: user %s wrote %d month(s), %d show(s), "
                "%d film(s), refreshed %d closed month(s)",
                user_id, len(written), show_count, movie_count, refreshed)
    return {"months": written, "shows": show_count, "movies": movie_count}


class BackfillExpired(Exception):
    """The surveyed plan is gone (expired, already applied, never run)."""


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _empty_plan(start_month: str, end_month: str) -> dict:
    return {"range": [start_month, end_month], "months": {}, "movies": [],
            "skipped": [], "shows_seen": 0, "seasons_seen": 0}


def _completed_record(ident: dict, season: int, total: int, detail: dict) -> dict:
    """One finished season as a frozen roster row.

    watched == total by construction: this row only exists because every episode
    was watched, and a frozen month's counts are never recomputed, so writing
    anything else here would render as a part-watched season forever.
    """
    return {
        "media": Media.SHOW,
        "ids": dict(ident.get("ids") or {}),
        "title": str(ident.get("title") or ""),
        "season": int(season),
        "network": str(ident.get("network") or ""),
        "abandoned": False,
        "abandoned_form": None,
        "watched": total,
        "total": total,
        "cadence": detail.get("cadence"),
        "premiere": detail.get("premiere"),
        "finale": detail.get("finale"),
        "bucket": distrakt.Bucket.COMPLETED,
        "started_airing": True,
        "finished_airing": True,
        "added_by": distrakt.ADDED_BY_HISTORY,
    }


async def _progress_by_show(settings, port, show_ids: list[int]) -> dict[int, dict]:
    """{source id: {season: {episode: watched_at}}} for every title in one go.

    From the per-title progress record rather than from the history sweep, because
    the sweep only sees plays inside the range: a season whose earlier episodes
    were watched in December and whose last one landed in January IS finished in
    January, and counting only what the sweep saw would miss it.
    """
    if not show_ids:
        return {}
    details = await port.fetch_progress_details(settings, show_ids)
    return {
        int(source_id): {int(season): eps for season, eps in (detail or {}).items()}
        for source_id, detail in details.items()
    }


async def _season_totals(settings, candidates: list[dict]) -> dict[tuple[str, int], dict]:
    """{(item key, season): season detail} — the episode total that decides
    whether a season is finished, plus the dates a frozen row carries."""
    from ..providers.trakt.detail import fetch_season_detail
    from ..providers.trakt.transport import shared_client
    if not candidates:
        return {}
    client = shared_client()
    keys = [(str(resolve_key(Media.SHOW, c["ids"])), int(c["season"])) for c in candidates]
    details = await asyncio.gather(*(
        fetch_season_detail(settings, c["ids"].get("trakt"), c["season"], client=client)
        for c in candidates
    ), return_exceptions=True)
    out: dict[tuple[str, int], dict] = {}
    for key, detail in zip(keys, details):
        if isinstance(detail, Exception):
            logger.warning("distrakt backfill: season lookup failed for %s", key)
            continue
        out[key] = detail or {}
    return out


async def _split_films(user_id: int, plays: list[dict],
                       months: set[str]) -> tuple[list[dict], list[dict]]:
    """The films in the range, split into (not recorded yet, already recorded).

    Both halves are kept. Only the first is written, so a second run adds only
    what is genuinely missing — but the second is what lets the plan say "six
    films, already recorded" instead of showing nothing and looking broken.

    A film whose stored play is OLDER than the one just seen counts as new: that
    is a later watch, and the fold keeps the later date.
    """
    state = await watch_history.load_state(user_id)
    known = {key: str((m or {}).get("watched_at") or "")
             for key, m in (state.get("movies") or {}).items()}
    fresh, seen = [], []
    for film in plays:
        # Films are not gated on a roster the way episodes are, and the routine
        # sync only ever looks back to the start of the CURRENT month — so for any
        # month before tracking began they are recorded nowhere. The ranker reads
        # them straight from that table, and this sweep is the one thing that can
        # see them.
        key = resolve_key(Media.MOVIE, film.get("ids") or {})
        if key is None or str(film["watched_at"])[:7] not in months:
            continue
        target = fresh if str(film["watched_at"]) > known.get(str(key), "") else seen
        target.append(film)
    return fresh, seen
