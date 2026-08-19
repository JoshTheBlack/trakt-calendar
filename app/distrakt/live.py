"""The live computation: a stored roster record plus what the provider says
about it right now, merged into the flat shape the renderers read.

ONE JOB — turn records into shows. It reads (watch history, season detail) and
writes nothing, which is what lets the month payload, the freeze pass and the
rollover bucketing all call it without any of them knowing about the others.
"""
from __future__ import annotations

import asyncio
import logging

from . import counts, discord_fmt
from .. import providers
from ..perftrace import span
from .store import Bucket, record_key

logger = logging.getLogger(__name__)

# The fields a stored record carries a LAST-KNOWN copy of, and that a live pass
# normally replaces. Named once because the degraded path below falls back to
# exactly this set — a list that drifted would render a mix of live and stale
# numbers with nothing saying which was which.
_LIVE_FIELDS = ("total", "cadence", "premiere", "finale", "started_airing", "finished_airing")


def live_key(rec: dict) -> tuple[str, int]:
    """The (title, season) pair the watch-history lookups are keyed on.

    The flat item key rather than the triple, because these lookups cross a JSON
    boundary (the cached watch state) where a tuple cannot survive as a dict key.
    """
    return (str(record_key(rec)), int(rec["season"]))


def detail_source(rec: dict, settings) -> str | None:
    """Which service is asked how long a season is, or None when none can be.

    DECIDED BY WHICH ID THE RECORD CARRIES, in the registry's declared order, and
    NOT by which services the account has linked. A season's episode total is
    CATALOGUE data — the same for everyone, no token involved — so a title known
    to Trakt is asked of Trakt whether or not this viewer signed in with it, and
    a Simkl-only title is asked of Simkl. Tying it to the linked accounts would
    make a public fact depend on a private one and leave a Simkl-only roster
    showing every season as having no episodes.

    AND THE FIRST SOURCE THAT CAN ACTUALLY BE ASKED, NOT SIMPLY THE FIRST ONE
    NAMED. A record's ids say who KNOWS the title; whether this instance holds
    that service's catalogue credential is a separate fact, and reading only the
    first made a row carrying two ids give up on the second answer it had in
    hand. Blanking one service's client id then emptied almost an entire roster
    on an instance whose other service could have answered most of it. None means
    every source this record names is one this instance cannot ask right now (or
    the record names none at all), which is a state to RENDER — see
    `_merge_unavailable` — rather than a zero to write down.

    ONE SOURCE PER TITLE, NOT BOTH. Paying a second catalogue call per season to
    discover the two services count episodes slightly differently would spend the
    instance's budget on a disagreement nothing renders: what a viewer is shown
    two of is what they have WATCHED (see counts.py), which is a fact about them
    and genuinely differs.

    THROUGH THE CALENDAR'S OWN `detail_source.choose`, which is already this
    question with this answer: the same ids, the same declared order and the same
    per-port catalogue predicate, written for the detail modal on both the
    signed-in and the public calendar. A second implementation here would be a
    second place for "who can describe this title" to drift, and the drift would
    be invisible — each side renders something plausible on its own page.
    """
    # Function-local for the name, not for a cycle: this module's own function is
    # called `detail_source` too, and a module-level import would have to be
    # aliased into something neither name means.
    from ..calendar import detail_source as catalogue_answerer
    chosen = catalogue_answerer.choose(settings, rec.get("ids") or {})
    return str(chosen[0]) if chosen else None


def named_sources(rec: dict) -> list[str]:
    """Every registered source this record carries an id for, in declared order,
    whether or not this instance can ask it anything.

    The pair to `detail_source` above and the reason the two are separate: that
    one answers "who can be asked", this one answers "who KNOWS this title", and
    the difference between them is exactly the sentence a row has to say when it
    could not be refreshed — a service that is absent from the settings reads
    nothing like a service that is down.
    """
    ids = rec.get("ids") or {}
    return [str(source) for source in providers.registered()
            if str(ids.get(str(source)) or "").strip()]


def unavailable_note(rec: dict, settings, *, asked: str | None) -> str:
    """The whole sentence a row draws in place of its counts when this pass could
    not refresh them.

    COMPOSED HERE, SENT AS ONE STRING, AND THE BROWSER ONLY DRAWS IT. The row
    used to carry a bare `unavailable` boolean and the client owned the words
    ("unavailable — refresh to retry"), which was already one sentence living
    where it could not be checked — and it is now two sentences, because "this
    service could not be reached" and "this service is not configured" are
    different facts with different remedies: one asks for a refresh, the other
    asks the operator to open Settings, and offering the first for the second is
    how an afternoon goes into looking for an outage that was never there. Two
    hardcoded strings in the browser with a flag to choose between them would be
    the same duplication twice over, so the server says the sentence and the
    client escapes it — the shape `routes._unkeyable_reason` already uses for the
    other refusal a viewer reads.

    `asked` is the source that WAS asked and failed, or None when nobody could be
    asked at all — the caller knows which of the two happened and this function
    cannot work it out afterwards.
    """
    labels = source_labels()
    if asked is not None:
        return f"couldn't reach {labels.get(asked, asked)} — refresh to retry"
    knows = [labels.get(name, name) for name in named_sources(rec)]
    if not knows:
        # No id any registered source issues, so there is nobody to configure
        # that would help — see providers.base.MATCH_SOURCES for how a record
        # comes to be filed under an id no service can be asked by.
        return "no service can be asked about this title"
    joined = knows[0] if len(knows) == 1 else " and ".join(knows)
    verb = "isn't" if len(knows) == 1 else "aren't"
    return f"{joined} {verb} configured — showing the last counts we had"


async def season_detail(settings, rec: dict, *, fresh: bool = False, client=None):
    """One record's season summary, from whichever source can answer for it.

    PUBLIC BECAUSE THE ADD ROUTES ASK IT TOO. "How long is this season" is one
    question with one answer, and the manual add used to ask Trakt directly —
    which handed a Simkl-only title `fetch_season_detail(None, ...)` and stored
    it with no episode total at all. A second source-picking rule beside
    `detail_source` would be a second place for "who can answer for this record"
    to drift, so there is one and both the live pass and the add routes call it.
    """
    from ..providers import season as season_rules
    from ..providers.simkl import detail as simkl_detail
    from ..providers.trakt.detail import fetch_season_detail
    ids = rec.get("ids") or {}
    source = detail_source(rec, settings)
    if source == "simkl":
        return await simkl_detail.fetch_season_detail(
            settings, ids.get("simkl"), rec["season"], rec.get("media") or "show")
    if source is None:
        return season_rules.empty_season(int(rec["season"]))
    return await fetch_season_detail(settings, ids.get("trakt"), rec["season"],
                                     fresh=fresh, client=client)


async def fetch_season_details(settings, records: list[dict], *, fresh: bool,
                               allow_degrade: bool, sources: list | None = None) -> list:
    """One season lookup per record, in parallel, in the records' own order.

    `allow_degrade` decides what a single failure does: captured as a result the
    caller renders around, or raised so the caller aborts the whole pass. That is
    the only difference, and it is the caller's policy rather than this
    function's — see compute_live_shows.

    A RECORD NO SOURCE CAN BE ASKED ABOUT IS NOT ASKED, AND COMES BACK None. It
    is not a failure and must not be one — `allow_degrade=False` exists to abort
    on a lookup that MIGHT have worked, and a missing credential will not fix
    itself before the retry — but neither is it a season with no episodes. The
    old shape asked anyway, the unconfigured service answered nothing, and a
    zeroed season landed in a frozen month as though it were measured. None says
    "nobody was asked" and every caller renders or stores its record's own
    last-known fields instead.

    `sources` is `detail_source`'s answer per record when the caller has already
    worked it out (compute_live_shows needs it again, to say which service went
    quiet), and is computed here otherwise. One answer either way — asking twice
    is how the row's sentence and the lookup come to disagree.
    """
    from ..providers.trakt.transport import shared_client
    if sources is None:
        sources = [detail_source(rec, settings) for rec in records]
    # The app-wide shared client for the whole fan-out (no per-call client).
    client = shared_client()
    askable = [index for index, source in enumerate(sources) if source]
    answers = await asyncio.gather(*(
        season_detail(settings, records[index], fresh=fresh, client=client)
        for index in askable
    ), return_exceptions=allow_degrade)
    details: list = [None] * len(records)
    for index, answer in zip(askable, answers):
        details[index] = answer
    return details


def _merge_available(rec: dict, detail: dict, watched: dict[str, int], settings,
                     asked=()) -> dict:
    show = {**rec, "key": str(record_key(rec)), "unavailable": False,
            "unavailable_source": "", "unavailable_note": ""}
    show.update({field: detail[field] for field in _LIVE_FIELDS})
    _apply_counts(show, rec, watched, settings, asked)
    return show


def _merge_unavailable(rec: dict, watched: dict[str, int], settings, asked=(), *,
                       source: str | None) -> dict:
    """This one title's totals are not this pass's. Render it from its stored
    record's last-known fields and flag it, rather than presenting a fabricated
    0/0 as real.

    TWO WAYS TO GET HERE AND THE ROW SAYS WHICH. `source` is the service that was
    asked and could not answer — a rate limit, an outage — or None when no source
    this record names could be asked at all, which is a settings problem and not
    an outage. Both render identically otherwise, because the record's last-known
    numbers are the best answer in either case; what differs is the sentence, and
    `unavailable_note` carries it whole (see that function for why the browser
    does not compose it).

    `unavailable_source` NAMES ONLY A SERVICE THAT WAS ACTUALLY ASKED. Which one
    it was is decided per record (see detail_source), so the row is the only
    place that knows, and a page full of these otherwise says "unavailable" over
    and over without ever saying who was quiet — see unreadable_detail_sources,
    which is what turns these into the banner. A source nobody could ask is left
    out of it deliberately: the banner's sentence is "that service could not be
    reached", and a service absent from the settings is not down.
    """
    show = {**rec, "key": str(record_key(rec)), "unavailable": True,
            "unavailable_source": source or "",
            "unavailable_note": unavailable_note(rec, settings, asked=source)}
    show.update({
        "total": int(rec.get("total") or 0),
        "cadence": rec.get("cadence"),
        "premiere": rec.get("premiere"),
        "finale": rec.get("finale"),
        "started_airing": bool(rec.get("started_airing")),
        "finished_airing": bool(rec.get("finished_airing")),
    })
    _apply_counts(show, rec, watched, settings, asked)
    return show


def source_order() -> tuple[str, ...]:
    """The registry's declared source order, as bare names. The FIRST entry a
    season actually has a number from is that season's primary — the one number
    a frozen month and the announcement post carry."""
    return tuple(str(source) for source in providers.registered())


def unreadable_detail_sources(shows) -> list[str]:
    """The services that could not answer a CATALOGUE question on this pass, in
    the registry's declared order.

    A SECOND WAY FOR A SOURCE TO GO QUIET, and it needs saying out loud because
    it looks nothing like the first. watch_history.unreadable_sources names a
    service whose HISTORY could not be read — one person's watched episodes. This
    names one whose EPISODE COUNTS could not be read, which is public catalogue
    data asked of whichever service the record carries an id for (see
    detail_source) and therefore fails independently of what the viewer linked.
    Both end up in the same banner because to a reader they are the same
    sentence: that service could not be reached, so what you are looking at is
    the other one's numbers.

    Without this, a whole roster whose only source is unreachable renders every
    row flagged unavailable with nothing anywhere on the page naming what to fix.

    A SERVICE THAT WAS NEVER ASKED IS NOT NAMED HERE. A record whose only source
    has no catalogue credential on this instance carries no `unavailable_source`
    at all (see _merge_unavailable), so it cannot reach this list — the banner
    says "could not be reached", and a service missing from the settings is not
    down. That row says so itself, in its own sentence, because it is the row
    that knows which service it wanted.
    """
    down = {show.get("unavailable_source") for show in shows if show.get("unavailable")}
    return [name for name in source_order() if name in down]


def source_labels() -> dict[str, str]:
    """What to call each source on screen, read off the providers themselves so a
    badge can never spell a service differently from the rest of the app."""
    return {str(source): provider.label
            for source, provider in providers.registered().items()}


def _apply_counts(show: dict, rec: dict, watched: dict[str, int], settings,
                  asked=()) -> None:
    """Put the watched counts on a live show, in all three forms it is read in.

    `watched` is the primary source's number and is what every existing reader
    already asks for — the bucket rule, the Discord post, a frozen month's column
    — so it keeps meaning exactly what it always did. `watched_by_source` beside
    it is the whole picture, and `counts` is the string the row draws, which is
    one number when the services agree and both when they do not. The label is
    built HERE rather than in the browser because the rule for it is the same
    rule a frozen month is written with, and a copy in JavaScript could not be
    tested against the one that matters.

    `asked` is which services were read for this account, which is what tells a
    season only one of two of them knows about from a season both agree on — see
    counts.counts_label. It is threaded down rather than looked up here because
    it is one answer for the whole pass, not one per row.
    """
    order = source_order()
    total = int(show.get("total") or 0)
    # RESOLVED ONCE, HERE, BEFORE ANY OF THE THREE FORMS IS WRITTEN. A service can
    # report a title finished without itemizing it, and that answer travels as a
    # claim rather than a number because only this side holds the season's total
    # (counts.ALL_EPISODES). This is the point where the total arrives, so it is
    # the point where the claim becomes a count — and everything downstream, the
    # bucket rule and a frozen month's stored breakdown included, sees plain
    # numbers it can compare and store.
    per_source = counts.resolve(watched, total)
    # `per_source or watched` so a caller holding one bare number instead of a
    # per-source map still gets it back, exactly as before.
    show["watched"] = counts.primary_count(per_source or watched, order, total)
    show["watched_by_source"] = dict(per_source)
    # The catalogue half, per source too, so a month frozen today can still say
    # which service's episode count it was measured against. One entry: a
    # season's total comes from one source (see detail_source). A record no
    # source could be asked about names none here — the total on it is its own
    # last-known number and no service is answering for it this pass.
    detail_from = detail_source(rec, settings)
    show["total_by_source"] = {detail_from: total} if detail_from else {}
    show["counts"] = counts.counts_label(watched, total, source_labels(), order, asked)


def _log_watched_coverage(records: list[dict], watched_lookup: dict, matched: int) -> None:
    """X/Y diagnostic: distinguishes an EMPTY watched lookup (no progress
    returned) from a NON-empty lookup that simply doesn't line up with the stored
    records (an id/season key mismatch), by printing a small sample of each."""
    logger.info(
        "compute_live_shows: %d record(s), watched-lookup has %d key(s), %d matched",
        len(records), len(watched_lookup), matched,
    )
    if records and matched == 0:
        logger.warning(
            "compute_live_shows: 0/%d records matched a watched count. "
            "sample record keys=%s ; sample watched-lookup=%s",
            len(records), [live_key(r) for r in records[:6]],
            list(watched_lookup.items())[:6],
        )


async def compute_live_shows(user_id: int, records: list[dict], settings, fresh: bool = False,
                             watched_lookup: dict | None = None,
                             allow_degrade: bool = False,
                             completed_lookup: dict | None = None,
                             sources_read=()) -> list[dict]:
    """Merge each stored record with its live Trakt-derived fields into the flat
    "LIVE SHOW SHAPE" discord_fmt expects (+ computed `bucket`).

    Watched counts (`x`) come from `user_id`'s incremental watch-history cache
    (watch_history.py) — the caller may pass a pre-synced `watched_lookup`
    (avoids re-syncing when it also needs the movies from the same state); if
    omitted we sync here. Totals/dates (`y`, cadence, premiere/finale) come from
    one season call per record; `fresh=True` bypasses the 24h season cache.

    `allow_degrade` decides what a per-title season-detail failure does. Off (the
    default, used by the freeze pass and rollover bucketing) a failure propagates
    so the caller aborts and retries later — never persisting a rate-limited 0/0 as
    a permanent frozen total. On (the live open-month view) a failed title is
    marked `unavailable` and rendered from its LAST-KNOWN stored fields instead: a
    429 on one title must not read as that title genuinely having 0 episodes, and
    it must not sink the rest of the roster. The shared-prerequisite sync (watched
    counts) is NOT degraded here — its failure still propagates for the caller's
    top-level handler, because it can't be pinned on any one title.

    `sources_read` IS WHICH SERVICES WERE ASKED FOR THIS ACCOUNT, and it belongs
    beside `watched_lookup` because it describes the same read: it is what lets a
    row tell "only one of the two services knows this season" from "both of them
    agree", which arrive as the same single number and mean different things (see
    counts.counts_label). A caller that hands in a pre-synced lookup knows what it
    asked and says so; one that leaves the sync to this function does not have to.

    Every show that comes back carries `key`, whether or not the record handed in
    did: it is what the browser names a row by, and deriving it here means a
    record assembled anywhere is addressable once it has been through this."""
    from . import watch_history
    if not records:
        return []

    # WHO ANSWERS FOR EACH RECORD, RESOLVED ONCE FOR THE WHOLE PASS. The fetch
    # needs it to know which records to skip, and every row that comes back
    # short needs it to say WHICH service went quiet; resolving it twice is how
    # a row's sentence comes to name a service the lookup never asked.
    sources = [detail_source(rec, settings) for rec in records]

    if watched_lookup is None:
        with span("cls.sync+seasons", n=len(records), fresh=fresh):
            state, details = await asyncio.gather(
                watch_history.sync_and_baseline(settings, user_id, records, force=fresh),
                fetch_season_details(settings, records, fresh=fresh,
                                     allow_degrade=allow_degrade, sources=sources),
            )
        watched_lookup = watch_history.watched_map(state)
        completed_lookup = watch_history.season_completed_map(state)
        # WHICH SERVICES ANSWER FOR THIS ACCOUNT — asked here because this branch
        # has just synced them, so it is one preference read on a path that has
        # already gone to the database. The other branch is handed it by the
        # caller that did the sync (`sources_read`), which keeps a pre-synced
        # call free of storage entirely.
        sources_read = sources_read or await watch_history.tracker_sources(settings, user_id)
    else:
        with span("cls.season_gather", n=len(records), fresh=fresh):
            details = await fetch_season_details(
                settings, records, fresh=fresh, allow_degrade=allow_degrade,
                sources=sources)
    asked = tuple(str(source) for source in sources_read or ())

    shows = []
    matched = 0
    unreachable = 0
    unaskable = 0
    for rec, detail, source in zip(records, details, sources):
        key = live_key(rec)
        if key in watched_lookup:
            matched += 1
        # `detail is None` is a record NOBODY COULD BE ASKED ABOUT — it was never
        # fetched (see fetch_season_details) — and an Exception is a service that
        # was asked and failed. They render the same way, from the record's own
        # last-known fields, and differ in the sentence the row draws.
        if detail is None or isinstance(detail, Exception):
            failed = isinstance(detail, Exception)  # allow_degrade path only
            unreachable += failed
            unaskable += not failed
            # No live count for this title: fall back to the number its stored
            # record last settled on, filed under the source that can answer for
            # it so the row still renders one number rather than none.
            fallback = {source or "": int(rec.get("watched") or 0)}
            show = _merge_unavailable(rec, watched_lookup.get(key) or fallback,
                                      settings, asked, source=source if failed else None)
        else:
            show = _merge_available(rec, detail, watched_lookup.get(key) or {},
                                    settings, asked)
        show["bucket"] = discord_fmt.bucket_of(show, show)
        # WHEN the season was finished, and only for a season that IS finished:
        # on a partly-watched season the same date is just "last time I watched
        # something", which must not read as a completion. "" = not finished, or
        # finished on a date the history cache cannot name.
        show["completed_on"] = (
            (completed_lookup or {}).get(key, "")
            if show["bucket"] == Bucket.COMPLETED else ""
        )
        shows.append(show)

    if unreachable:
        logger.warning(
            "compute_live_shows: %d/%d title(s) rate-limited/unreachable this pass; "
            "rendered from last-known totals and flagged unavailable.",
            unreachable, len(records),
        )
    if unaskable:
        # A SEPARATE LINE BECAUSE IT IS A SEPARATE PROBLEM, and the one an
        # operator can actually fix: these titles are known only to services this
        # instance holds no catalogue credential for, so no amount of retrying
        # will count them. Nothing was requested for them either.
        logger.warning(
            "compute_live_shows: %d/%d title(s) name no source this instance can "
            "ask (catalogue credentials missing); rendered from last-known totals.",
            unaskable, len(records),
        )
    _log_watched_coverage(records, watched_lookup, matched)
    return shows
