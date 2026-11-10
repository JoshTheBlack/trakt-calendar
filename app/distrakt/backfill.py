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
else — one completed record apiece. "Finished during M" is the same rule the live
tracker settles a season by: every episode of the season watched, and the last of
them watched inside M, so a season finished in May lands in May and in no other
month. Shows that were in progress but never finished are not recorded: what
somebody is part-way through is a fact about them TODAY and belongs on their own
list, not on a month they were not being tracked in, and inventing a verdict for
a month nobody was watching would be a guess.

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

from . import arrival, live, watch_history
# The roster is reached through this package's own public surface, the same names
# an outside caller uses, rather than through the modules those names are defined
# in — a backfill job has no business knowing which half of the tracker
# `load_month` or `save_month` lives in.
from .. import distrakt
from .. import cache, clock, db
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
    year, month = distrakt.parse_month_key(month_key)
    return distrakt.month_key(year - 1, 12) if month == 1 else distrakt.month_key(year, month - 1)


def month_range(start_month: str, end_month: str) -> list[str]:
    """Every 'YYYY-MM' from start to end inclusive, or [] if either is unusable
    or they are the wrong way round."""
    start, end = valid_month(start_month), valid_month(end_month)
    if not start or not end:
        return []
    out: list[str] = []
    year, month = distrakt.parse_month_key(start)
    while distrakt.month_key(year, month) <= end and len(out) < MAX_MONTHS:
        out.append(distrakt.month_key(year, month))
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
    today = today or clock.today()
    start = f"{today.year:04d}-01"
    end = _prev_month(f"{today.year:04d}-{today.month:02d}")
    return (start, max(start, end))


async def survey(user_id: int, settings, start_month: str, end_month: str,
                 today: date | None = None) -> dict:
    """Work out what a backfill would write, WITHOUT writing any of it.

    Returns the plan, which is also stored for `apply` to pick up:
        {"range": [start, end], "months": {"YYYY-MM": [record, ...]},
         "replayed": {"YYYY-MM": {"<key>:<season>": day}},
         "movies": [...], "replacing": {"YYYY-MM": n},
         "shows_seen": n, "seasons_seen": n}

    EVERY MONTH IN THE RANGE IS SURVEYED, INCLUDING ONES THAT ALREADY HOLD
    RECORDS. A month with contents used to be left out of the sweep entirely, on
    the reasoning that it carried decisions — abandons, manual adds, a row taken
    off by hand — that a rebuild would throw away. That reasoning was about
    REBUILDING, and the write no longer rebuilds: apply() upserts record by
    record, so a month keeps everything the plan does not mention. The skip was
    the only thing standing between "this month has one wrong row on it" and
    "this month can never be imported again", which is exactly the corner an
    account with a bad month ends up in.

    WHAT IT WOULD REPLACE IS COUNTED AND REPORTED, per month. A viewer confirming
    a merge is agreeing to overwrite as well as to add, and a summary that only
    ever said "4 finished seasons" could not tell the two apart. `replacing`
    counts the planned records that land on a record the month already holds at
    the same address — same kind, same title, same season.

    NOTHING IS SURVEYED FOR THE CURRENT MONTH OR LATER. Everything this writes is
    a season FINISHED inside the month, written onto a month marked closed, and
    neither claim can be made about a month still running — the tracker is still
    bucketing it. While months with contents were skipped this was true by
    accident, because the month in progress always had something on it.

    FILMS are collected across the WHOLE surveyed range. They are per-play watch
    history rather than a monthly verdict, and scoping them to the months being
    written made the films in every other month unreachable: once the seasons had
    been written the range had no writable months left, and re-running to pick up
    films reported nothing to do. Films already recorded at the same date or later
    are left out, so a second run offers only what is genuinely missing.

    SHAPED FOR A SELECTIVE IMPORT. The plan is a list of records per month and
    apply() writes what it is given, so choosing which titles to import is a
    filter over `months` between the two halves — no part of the write needs to
    know it happened.
    """
    today = today or clock.today()
    # Every day this decides is the VIEWER's day, not UTC's — see
    # watch_history.local_day for what reading a play's raw timestamp costs.
    tz = watch_history.viewer_tz(settings)
    # A month key compares as a string because both halves are zero-padded, which
    # is the same property walk_settled's ordering leans on.
    this_month = distrakt.month_key(today.year, today.month)
    months = [m for m in month_range(start_month, end_month) if m < this_month]
    if not months:
        return _empty_plan(start_month, end_month)

    wanted = list(months)

    # ONE paged history sweep for the whole range, read twice — once for the
    # seasons, once for the films.
    #
    # THE PRIMARY SOURCE ALONE, and it is a deliberate narrowing. A backfill is a
    # PROPOSAL the viewer confirms in a dialog that lists what it found; two
    # services' histories merged into it would need the dialog to be able to show
    # a disagreement, and there is nowhere in that flow for one to be resolved.
    # Once a month is built from it, the ordinary sync reads every admitted
    # source and fills the second one in — so nothing is lost, it just arrives on
    # the next load rather than in the survey.
    ports = await watch_history.tracker_ports(settings, user_id)
    if not ports:
        return _empty_plan(start_month, end_month)
    source, port = ports[0]
    start_day = distrakt.month_first_day(months[0])
    events = await port.fetch_history(settings, start_at=start_day.isoformat())
    films, films_known = await _split_films(
        user_id, port.movie_plays_from(events), set(months), tz)

    by_month: dict[str, list[dict]] = {m: [] for m in wanted}
    # {replay month: {"<item key>:<season>": {title, season, day, finished_in}}} —
    # the finales watched again after the viewing that finished them, kept under
    # the month the REPLAY happened in, which is the month somebody surveying it
    # is asking about. Beside the records rather than on them: nothing is written
    # for a replay, and this is only something the confirmation says.
    replayed: dict[str, dict[str, str]] = {}
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
        # THE SURVEYING SOURCE'S OWN ID, never another service's — the same
        # rule the watch cache follows. A candidate that source cannot name is
        # dropped rather than asked about with an id belonging to a different
        # catalogue, which would answer about a different title.
        name = str(source)
        show_ids = list(dict.fromkeys(
            int(c["ids"][name]) for c in by_key.values() if c.get("ids", {}).get(name)
        ))

        progress = await _progress_by_show(settings, port, show_ids)
        viewings = _viewing_days(events, tz)
        completed_in = await _completed_months(user_id)
        # WHAT EACH SEASON WOULD BE FILED AS is arrival.describe's, the same for
        # every way onto the tracker: a history event names no network and only
        # the surveying service's ids, and these rows are settled verdicts that
        # nothing revisits. Its season lookup is also the episode total that
        # decides whether a season was finished at all, so the decision and the
        # row it writes come from one answer. A lookup that fails reads as no
        # total, and the season is left out rather than guessed at.
        arrivals = await arrival.describe(
            settings, [_as_record(ident) for ident in by_key.values()],
            order=await live.totals_order(user_id))

        wanted_set = set(wanted)
        for ((key, season), ident), arrived in zip(by_key.items(), arrivals):
            seasons_seen += 1
            source_id = int(ident["ids"][name]) if ident.get("ids", {}).get(name) else None
            episodes = (progress.get(source_id) or {}).get(season) or {}
            total = int(arrived.detail.get("total") or 0)
            if not total or len(episodes) < total:
                continue  # not finished at all -> belongs to no month
            finished_on, again = _finished_on(
                episodes, viewings.get((key, season)) or {}, tz)
            month_key = finished_on[:7]
            held = completed_in.get((key, season))
            if held and month_key and month_key > held:
                # A FINALE THIS TRACKER ALREADY RECORDED FINISHED, SEEN AGAIN.
                # The sweep starts at the range, so a replay whose first viewing
                # came earlier is the only viewing it saw, and taken as the finish
                # it filed the season a second time on the replay's month
                # (measured: A Knight of the Seven Kingdoms, finished in
                # February, finale watched again 8 July). It is a replay: said
                # on that month, filed nowhere.
                _note_replay(replayed, wanted_set, arrived.record, finished_on, held)
                continue
            if again:
                _note_replay(replayed, wanted_set, arrived.record, again, month_key)
            if not month_key or month_key not in wanted_set:
                continue  # finished outside the range (or on a date Trakt cannot name)
            by_month[month_key].append(_completed_record(arrived.record, total))

    written_months = {m: rows for m, rows in by_month.items() if rows}
    plan = {
        "range": [months[0], months[-1]],
        "months": written_months,
        "replayed": replayed,
        "movies": films,
        # Reported, not dropped: a run that found six films and wrote none of
        # them because it already had them must SAY that, or it reads exactly
        # like a run that could not see them at all.
        "movies_known": films_known,
        "replacing": await _replacements(user_id, written_months),
        "shows_seen": len(show_ids),
        "seasons_seen": seasons_seen,
        # HOW MANY MONTHS THE SEASON PASS WAS RUN OVER AT ALL. `seasons_seen` of
        # 0 has two completely different causes — the pass ran and history named
        # nothing, or the pass never ran because every month was skipped — and a
        # reader told "0 season(s)" cannot tell which. Only the caller that ran
        # the pass knows, so it says so rather than being inferred from anything
        # else the plan carries.
        "months_read": len(wanted),
    }
    await cache.set(_plan_key(user_id), plan)
    logger.info("distrakt backfill survey: user %s, %s..%s, %d month(s) of seasons, %d film(s)",
                user_id, months[0], months[-1], len(plan["months"]), len(films))
    return plan


def summarize(plan: dict, tz) -> dict:
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
        by_month.setdefault(watch_history.local_day(movie.get("watched_at"), tz)[:7],
                            []).append(_label(movie))
    known_by_month: dict[str, int] = {}
    for movie in known:
        key = watch_history.local_day(movie.get("watched_at"), tz)[:7]
        known_by_month[key] = known_by_month.get(key, 0) + 1

    # Films are listed month by month beside the seasons, not summed into one
    # number at the end: they are recorded by the same sweep, and a count nobody
    # can see the contents of is not something to confirm.
    replayed = plan.get("replayed") or {}

    def _season_label(record: dict) -> str:
        return f"{record.get('title') or 'Untitled'} S{int(record.get('season') or 0):02d}"

    def _replay_lines(month: str) -> list[str]:
        """SAID ON THE MONTH IT HAPPENED, NOT DECIDED FOR THEM. A finale watched
        again is the month's own viewing, so a survey of that month names it and
        where the season's finish is filed; nothing is written for it. A value in
        the shape plans had before (a bare day) is skipped, not misread."""
        return sorted(
            f"{_season_label(note)} — finale watched again "
            f"{watch_history.day_label(note['day'])} (finished in {note['finished_in']})"
            for note in (replayed.get(month) or {}).values() if isinstance(note, dict))

    keys = sorted(set(months) | set(by_month) | set(known_by_month) | set(replayed))
    rows = [
        {
            "month": key,
            "count": len(months.get(key) or []),
            "titles": sorted(_season_label(r) for r in (months.get(key) or [])),
            "replays": _replay_lines(key),
            "movie_count": len(by_month.get(key) or []),
            "movie_titles": sorted(by_month.get(key) or []),
            "movie_known": known_by_month.get(key, 0),
            # HOW MANY OF THAT COUNT ARE OVERWRITES. A merge asks the viewer to
            # agree to two different things at once, and "4 finished" on its own
            # cannot say that two of them will land on rows the month already has.
            "replacing": int((plan.get("replacing") or {}).get(key, 0)),
        }
        for key in keys
    ]
    return {
        "range": plan.get("range") or [],
        "months": rows,
        "movies": len(movies),
        "movies_known": len(known),
        "replacing": sum((plan.get("replacing") or {}).values()),
        "shows_seen": plan.get("shows_seen") or 0,
        "seasons_seen": plan.get("seasons_seen") or 0,
        "months_read": plan.get("months_read") or 0,
        "total": sum(len(records) for records in months.values()),
        # HOW MANY MONTHS WOULD ACTUALLY GAIN SOMETHING, which is not the length
        # of the list above: that list also carries months whose only content is
        # films this account already has, drawn so a reader can see the sweep
        # reached them. Confirming a plan that writes nothing is the shape this
        # exists to keep off the page.
        "to_write": sum(1 for row in rows if row["count"] or row["movie_count"]),
    }


async def apply(user_id: int, settings, plan: dict | None = None) -> dict:
    """Write a surveyed plan: one closed month per entry, plus the movie plays
    the same sweep saw. Returns {"months": [...], "shows": n, "movies": n}.

    Reads the plan the survey stored rather than taking one from the caller, so
    the only records that can be written are ones this module worked out itself.

    RECORD BY RECORD, NEVER MONTH BY MONTH. Each planned record is upserted at
    its own address, so a month ends up holding the union of what it held and
    what the plan names: a record at the same address is overwritten, a record
    the plan does not mention is left exactly where it is. This used to hand
    save_month a whole doc, which replaces a month's records with the doc's —
    and because that would have destroyed anything already on the month, the
    only safe thing to do with a month that held records was to refuse it. The
    refusal was the bug: a month with one wrong row on it could never be
    imported again, by any route the app offers.

    WHICH MEANS RE-RUNNING IT RE-ADDS WHAT YOU DELETED, and that is the intended
    reading rather than an oversight. A backfill is an explicit act of taking
    what a service says and putting it on a month; nothing records that a row was
    removed on purpose, so nothing here can tell that apart from a row that was
    never imported. Choosing which titles to take is what a selective import is
    for — the plan is already a per-month list of records, so that filter goes
    between the survey and this, and nothing below needs to know.
    """
    tz = watch_history.viewer_tz(settings)
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
    # SWEPT, not folded: these plays come from a full history sweep the viewer
    # has just confirmed, so they supersede a stored date rather than only
    # advancing it — see record_movie_watches for the ratchet that caused.
    movie_count = await watch_history.record_movie_watches(
        user_id, plan_movies, swept=True)
    state = await watch_history.load_state(user_id)

    written: list[str] = []
    show_count = 0
    for month_key in sorted(months):
        rows = months[month_key] or []
        if not rows:
            continue
        for record in rows:
            await distrakt.add_month_record(user_id, month_key, record)
        await _record_films(user_id, month_key, state, tz)
        # Closed LAST and through its own verb, because closing is two columns on
        # the month row: handing save_month a doc to close the month with would
        # take the records just written back out again.
        await distrakt.set_month_closed(user_id, month_key)
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
            if await _record_films(user_id, month_key, state, tz):
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
    return {"range": [start_month, end_month], "months": {}, "replayed": {},
            "movies": [], "replacing": {}, "shows_seen": 0, "seasons_seen": 0,
            "months_read": 0}


def _address(record: dict) -> tuple:
    """What add_month_record would write this record OVER, if anything.

    The same four things that address a stored row — kind, identity and season —
    which is why this can be asked of a planned record and of a stored one with
    the one function. A record that cannot be keyed is given a unique address of
    its own so it matches nothing: it is about to be refused by the write anyway,
    and quietly counting it as an overwrite would overstate what the viewer is
    agreeing to.
    """
    try:
        key = distrakt.record_key(record)
    except Exception:
        return (id(record),)
    return (str(record.get("kind")), key.media, key.match_source, key.match_id,
            int(record.get("season") or 0))


async def _replacements(user_id: int, months: dict[str, list[dict]]) -> dict[str, int]:
    """Per month, how many planned records land on one the month already holds.

    One read per month with records in the plan, against the local database — the
    survey either side of this spends hundreds of rate-limited provider calls, so
    this is not the part worth being clever about.
    """
    out: dict[str, int] = {}
    for month_key, rows in months.items():
        if not rows:
            continue
        held = {_address(rec) for rec in await distrakt.month_records(user_id, month_key)}
        count = sum(1 for rec in rows if _address(rec) in held)
        if count:
            out[month_key] = count
    return out


async def _record_films(user_id: int, month_key: str, state: dict, tz) -> bool:
    """Put the films watched during `month_key` onto its stored list, one at a
    time. True if it wrote any.

    ONE FILM AT A TIME rather than a list replacing the month's, for the reason
    add_month_movie spells out: a closed month's film list is that month's own
    record, and a write that re-derives the whole list from today's watch history
    takes out every film the history no longer reports. Adding film by film means
    a month gains what the sweep found and keeps what it already had.

    THE CALLER ESTABLISHES THAT THE MONTH IS CLOSED, or is closing it in the same
    breath — an open month recomputes its films on every load, and giving it a
    stored list would freeze an answer it is supposed to keep working out.

    A FILM THE MONTH ALREADY HOLDS, IDENTICALLY, IS NOT WRITTEN AGAIN, and the
    answer is what the caller counts as a month it brought up to date. Writing
    every film every time would make a re-run report that it refreshed six months
    it had not changed at all, in the one log line that says what it did.
    """
    mstart, mend = watch_history.month_bounds(month_key)
    films = watch_history.movies_in_range(state, mstart, mend, tz)
    if not films:
        return False
    doc = await distrakt.load_month(user_id, month_key) or {}
    held = {str(m.get("key") or ""): m for m in (doc.get("movies") or [])}
    wrote = False
    for film in films:
        if held.get(str(film.get("key") or "")) == film:
            continue
        await distrakt.add_month_movie(user_id, month_key, film)
        wrote = True
    return wrote


async def _completed_months(user_id: int) -> dict[tuple[str, int], str]:
    """{(item key, season): the earliest month this tracker records it COMPLETED}.

    Read once per survey, whole, because the survey asks it of every season it
    examines: whether a finale seen in the range is the one that finished the
    season or a replay of one finished before the range began, which the sweep
    alone cannot tell."""
    out: dict[tuple[str, int], str] = {}
    async for month, records in distrakt.walk_settled(user_id):
        for record in records:
            if record.get("kind") == distrakt.RecordKind.COMPLETED:
                address = (str(record["key"]), int(record["season"]))
                out[address] = min(month, out.get(address, month))
    return out


def _note_replay(replayed: dict, wanted: set[str], record: dict, day: str,
                 finished_in: str) -> None:
    """File one replayed finale under the month it was watched in, if that month
    is being surveyed. `finished_in` is the month the season is filed under, so
    the note can say where the finish went.

    ONLY A LATER MONTH'S. A second play of the finale inside the month that
    finished it is not a re-watch worth naming, and the live rule leaves it alone
    too (lifecycle, a season completed and watched again in the same month).
    Measured on a real history: four of the seven second plays were that — a
    service recording one viewing twice, or a finale finished off the next day."""
    if day[:7] not in wanted or day[:7] <= finished_in:
        return
    replayed.setdefault(day[:7], {})[_replay_address(record)] = {
        "title": str(record.get("title") or ""), "season": int(record.get("season") or 0),
        "day": day, "finished_in": finished_in}


def _replay_address(record: dict) -> str:
    """How the plan's `replayed` notes name a planned record: the identity it is
    filed under and its season, flat, because the plan is stored as JSON."""
    return f"{distrakt.record_key(record)}:{int(record.get('season') or 0)}"


def _viewing_days(events, tz) -> dict[tuple[str, int], dict[int, list[str]]]:
    """{(item key, season): {episode: [every day it was watched, oldest first]}},
    from the sweep's own events.

    THE ONE READING OF A HISTORY THAT KEEPS EVERY PLAY. A service's per-title
    progress holds one date per episode — the latest — so a finale watched again
    has lost its first viewing there; the sweep lists each play as an event of
    its own, which is the only place the two can be told apart.
    """
    out: dict[tuple[str, int], dict[int, set[str]]] = {}
    for play in watch_history.plays_in(events):
        day = watch_history.local_day(play.watched_at, tz)
        if day:
            out.setdefault((str(play.key), int(play.season)), {}) \
               .setdefault(int(play.number), set()).add(day)
    return {season: {number: sorted(days) for number, days in episodes.items()}
            for season, episodes in out.items()}


def _finished_on(episodes: dict, viewings: dict[int, list[str]], tz) -> tuple[str, str]:
    """`(the day a finished season was finished, the day its finale was watched
    again or "")`.

    THE FINALE'S DAY, NOT THE LATEST PLAY'S — see watch_history.finale_day, which
    this defers to for everything the progress record can say. One episode
    re-watched months later filed a season finished in February onto July's
    record.

    AND THE FINALE'S FIRST VIEWING when the sweep saw it watched more than once,
    because progress keeps only the latest and that is the re-watch. The later
    day comes back beside it so the confirmation can SAY the finale was watched
    again rather than silently choosing between the two: this is a plan somebody
    is about to confirm, and naming the choice made is cheaper than a question.
    A first viewing from before the sweep began cannot be seen, and the season
    is then filed by the one viewing the sweep did see.
    """
    finished = watch_history.finale_day({"": episodes}, tz)
    finale = max((int(number) for number in episodes), default=0)
    days = viewings.get(finale) or []
    if len(days) < 2:
        return finished, ""
    return days[0], days[-1]


def _as_record(ident: dict) -> dict:
    """A history candidate as the record arrival.describe is handed: what the
    sweep knows — the surveying service's ids, its title, the season."""
    return {"media": Media.SHOW, "ids": dict(ident.get("ids") or {}),
            "title": str(ident.get("title") or ""), "season": int(ident["season"]),
            "network": str(ident.get("network") or "")}


def _completed_record(described: dict, total: int) -> dict:
    """One finished, described season as that month's COMPLETED record.

    The kind is stated outright rather than left to be worked out later: this row
    exists precisely because the sweep proved every episode was watched inside the
    month, which is the whole of what "completed in M" means. A record whose kind
    had to be inferred afterwards from its counts would be a second opinion about
    something already known here.

    watched == total by construction, for the same reason, and a settled record's
    counts are never recomputed — so writing anything else would render as a
    part-watched season forever. The airing flags are stated for the same reason
    they are on a hand-filled past month: it finished, so it had started and
    ended, whatever a lookup made later says.
    """
    return {
        **described,
        "kind": distrakt.RecordKind.COMPLETED,
        "abandoned_form": None,
        "watched": total,
        "total": total,
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


async def _split_films(user_id: int, plays: list[dict],
                       months: set[str], tz) -> tuple[list[dict], list[dict]]:
    """The films in the range, split into (not recorded yet, already recorded).

    Both halves are kept. Only the first is written, so a second run adds only
    what is genuinely missing — but the second is what lets the plan say "six
    films, already recorded" instead of showing nothing and looking broken.

    A DIFFERENT DAY IS NEWS, NOT MERELY A LATER ONE. It used to be "later", on
    the reasoning that a later play is a re-watch and an earlier one is nothing to
    hear about — but that made the cache a ratchet, and the case it locked out is
    the one that matters most: a play RE-DATED at the source. MEASURED, on the
    account this was found on: a film watched on 23 August was dragged to 11
    September by a bad bulk import; the import was undone at both services, both
    reported 23 August again, and the tracker went on believing September, because
    August is earlier and earlier was not news. The film was invisible on August,
    unreachable from anywhere, and proof against every re-import. What the
    services say now is what happened; this is a cache.
    """
    state = await watch_history.load_state(user_id)
    known = {key: watch_history.local_day((m or {}).get("watched_at"), tz)
             for key, m in (state.get("movies") or {}).items()}
    fresh, seen = [], []
    for film in plays:
        # Films are not gated on a roster the way episodes are, and the routine
        # sync only ever looks back to the start of the CURRENT month — so for any
        # month before tracking began they are recorded nowhere. The ranker reads
        # them straight from that table, and this sweep is the one thing that can
        # see them.
        key = resolve_key(Media.MOVIE, film.get("ids") or {})
        day = watch_history.local_day(film.get("watched_at"), tz)
        if key is None or day[:7] not in months:
            continue
        # Compared as DAYS rather than as raw timestamps, because the two services
        # spell the same moment differently — "…:00Z" against "…:00.000Z" — and
        # comparing the strings reported an unchanged film as news on every run.
        target = fresh if day != known.get(str(key), "") else seen
        target.append(film)
    return fresh, seen
