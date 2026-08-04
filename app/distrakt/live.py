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


def detail_source(rec: dict) -> str | None:
    """Which service is asked how long a season is, or None when none can be.

    DECIDED BY WHICH ID THE RECORD CARRIES, in the registry's declared order, and
    NOT by which services the account has linked. A season's episode total is
    CATALOGUE data — the same for everyone, no token involved — so a title known
    to Trakt is asked of Trakt whether or not this viewer signed in with it, and
    a Simkl-only title is asked of Simkl. Tying it to the linked accounts would
    make a public fact depend on a private one and leave a Simkl-only roster
    showing every season as having no episodes.

    ONE SOURCE PER TITLE, NOT BOTH. Paying a second catalogue call per season to
    discover the two services count episodes slightly differently would spend the
    instance's budget on a disagreement nothing renders: what a viewer is shown
    two of is what they have WATCHED (see counts.py), which is a fact about them
    and genuinely differs.
    """
    ids = rec.get("ids") or {}
    for source in providers.registered():
        if ids.get(str(source)) not in (None, ""):
            return str(source)
    return None


async def _season_detail(settings, rec: dict, *, fresh: bool, client):
    """One record's season summary, from whichever source can answer for it."""
    from ..providers import season as season_rules
    from ..providers.simkl import detail as simkl_detail
    from ..providers.trakt.detail import fetch_season_detail
    ids = rec.get("ids") or {}
    source = detail_source(rec)
    if source == "simkl":
        return await simkl_detail.fetch_season_detail(
            settings, ids.get("simkl"), rec["season"], rec.get("media") or "show")
    if source is None:
        return season_rules.empty_season(int(rec["season"]))
    return await fetch_season_detail(settings, ids.get("trakt"), rec["season"],
                                     fresh=fresh, client=client)


async def fetch_season_details(settings, records: list[dict], *, fresh: bool,
                               allow_degrade: bool) -> list:
    """One season lookup per record, in parallel, in the records' own order.

    `allow_degrade` decides what a single failure does: captured as a result the
    caller renders around, or raised so the caller aborts the whole pass. That is
    the only difference, and it is the caller's policy rather than this
    function's — see compute_live_shows.
    """
    from ..providers.trakt.transport import shared_client
    # The app-wide shared client for the whole fan-out (no per-call client).
    client = shared_client()
    return await asyncio.gather(*(
        _season_detail(settings, rec, fresh=fresh, client=client) for rec in records
    ), return_exceptions=allow_degrade)


def _merge_available(rec: dict, detail: dict, watched: dict[str, int],
                     asked=()) -> dict:
    show = {**rec, "key": str(record_key(rec)), "unavailable": False,
            "unavailable_source": ""}
    show.update({field: detail[field] for field in _LIVE_FIELDS})
    _apply_counts(show, rec, watched, asked)
    return show


def _merge_unavailable(rec: dict, watched: dict[str, int], asked=()) -> dict:
    """This one title's totals could not be fetched. Render it from its stored
    record's last-known fields and flag it, so the UI can say "unavailable,
    refresh to retry" rather than presenting a fabricated 0/0 as real.

    IT ALSO NAMES THE SERVICE THAT DID NOT ANSWER. Which one it was is decided
    per record (see detail_source), so the row is the only place that knows, and
    a page full of these otherwise says "unavailable" fifty-three times without
    ever saying who was quiet — see unreadable_detail_sources, which is what
    turns these into the banner.
    """
    show = {**rec, "key": str(record_key(rec)), "unavailable": True,
            "unavailable_source": detail_source(rec) or ""}
    show.update({
        "total": int(rec.get("total") or 0),
        "cadence": rec.get("cadence"),
        "premiere": rec.get("premiere"),
        "finale": rec.get("finale"),
        "started_airing": bool(rec.get("started_airing")),
        "finished_airing": bool(rec.get("finished_airing")),
    })
    _apply_counts(show, rec, watched, asked)
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
    row flagged "unavailable — refresh to retry" with nothing anywhere on the
    page naming what to fix.
    """
    down = {show.get("unavailable_source") for show in shows if show.get("unavailable")}
    return [name for name in source_order() if name in down]


def source_labels() -> dict[str, str]:
    """What to call each source on screen, read off the providers themselves so a
    badge can never spell a service differently from the rest of the app."""
    return {str(source): provider.label
            for source, provider in providers.registered().items()}


def _apply_counts(show: dict, rec: dict, watched: dict[str, int], asked=()) -> None:
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
    per_source = watched if isinstance(watched, dict) else {}
    total = int(show.get("total") or 0)
    show["watched"] = counts.primary_count(watched, order)
    show["watched_by_source"] = dict(per_source)
    # The catalogue half, per source too, so a month frozen today can still say
    # which service's episode count it was measured against. One entry: a
    # season's total comes from one source (see detail_source).
    detail_from = detail_source(rec)
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

    if watched_lookup is None:
        with span("cls.sync+seasons", n=len(records), fresh=fresh):
            state, details = await asyncio.gather(
                watch_history.sync_and_baseline(settings, user_id, records, force=fresh),
                fetch_season_details(settings, records, fresh=fresh, allow_degrade=allow_degrade),
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
                settings, records, fresh=fresh, allow_degrade=allow_degrade)
    asked = tuple(str(source) for source in sources_read or ())

    shows = []
    matched = 0
    unavailable = 0
    for rec, detail in zip(records, details):
        key = live_key(rec)
        if key in watched_lookup:
            matched += 1
        if isinstance(detail, Exception):
            # allow_degrade path only (else the gather would have raised).
            unavailable += 1
            # No live count for this title: fall back to the number its stored
            # record last settled on, filed under the source that can answer for
            # it so the row still renders one number rather than none.
            fallback = {detail_source(rec) or "": int(rec.get("watched") or 0)}
            show = _merge_unavailable(rec, watched_lookup.get(key) or fallback, asked)
        else:
            show = _merge_available(rec, detail, watched_lookup.get(key) or {}, asked)
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

    if unavailable:
        logger.warning(
            "compute_live_shows: %d/%d title(s) rate-limited/unreachable this pass; "
            "rendered from last-known totals and flagged unavailable.",
            unavailable, len(records),
        )
    _log_watched_coverage(records, watched_lookup, matched)
    return shows
