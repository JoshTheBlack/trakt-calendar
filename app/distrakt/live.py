"""The live computation: a stored roster record plus what the provider says
about it right now, merged into the flat shape the renderers read.

ONE JOB — turn records into shows. It reads (watch history, season detail) and
writes nothing, which is what lets the month payload, the freeze pass and the
rollover bucketing all call it without any of them knowing about the others.
"""
from __future__ import annotations

import asyncio
import logging

from . import discord_fmt
from ..perftrace import span
from .store import record_key

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


async def fetch_season_details(settings, records: list[dict], *, fresh: bool,
                               allow_degrade: bool) -> list:
    """One season lookup per record, in parallel, in the records' own order.

    `allow_degrade` decides what a single failure does: captured as a result the
    caller renders around, or raised so the caller aborts the whole pass. That is
    the only difference, and it is the caller's policy rather than this
    function's — see compute_live_shows.
    """
    from ..providers.trakt.detail import fetch_season_detail
    from ..providers.trakt.transport import shared_client
    # The app-wide shared client for the whole fan-out (no per-call client).
    client = shared_client()
    return await asyncio.gather(*(
        fetch_season_detail(settings, (rec.get("ids") or {}).get("trakt"),
                            rec["season"], fresh=fresh, client=client)
        for rec in records
    ), return_exceptions=allow_degrade)


def _merge_available(rec: dict, detail: dict, watched: int) -> dict:
    show = {**rec, "key": str(record_key(rec)), "watched": watched, "unavailable": False}
    show.update({field: detail[field] for field in _LIVE_FIELDS})
    return show


def _merge_unavailable(rec: dict, watched: int) -> dict:
    """This one title's totals could not be fetched. Render it from its stored
    record's last-known fields and flag it, so the UI can say "unavailable,
    refresh to retry" rather than presenting a fabricated 0/0 as real."""
    show = {**rec, "key": str(record_key(rec)), "watched": watched, "unavailable": True}
    show.update({
        "total": int(rec.get("total") or 0),
        "cadence": rec.get("cadence"),
        "premiere": rec.get("premiere"),
        "finale": rec.get("finale"),
        "started_airing": bool(rec.get("started_airing")),
        "finished_airing": bool(rec.get("finished_airing")),
    })
    return show


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
                             completed_lookup: dict | None = None) -> list[dict]:
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
    else:
        with span("cls.season_gather", n=len(records), fresh=fresh):
            details = await fetch_season_details(
                settings, records, fresh=fresh, allow_degrade=allow_degrade)

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
            show = _merge_unavailable(rec, watched_lookup.get(key, int(rec.get("watched") or 0)))
        else:
            show = _merge_available(rec, detail, watched_lookup.get(key, 0))
        show["bucket"] = discord_fmt.bucket_of(show, show)
        # WHEN the season was finished, and only for a season that IS finished:
        # on a partly-watched season the same date is just "last time I watched
        # something", which must not read as a completion. "" = not finished, or
        # finished on a date the history cache cannot name.
        show["completed_on"] = (
            (completed_lookup or {}).get(key, "")
            if show["bucket"] == discord_fmt.Bucket.COMPLETED else ""
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
