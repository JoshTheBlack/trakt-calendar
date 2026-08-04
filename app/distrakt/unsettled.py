"""Settling the rows a month that never froze left behind.

ONE JOB — drain `distrakt_unsettled_rows`, the table the roster split (migration
19 in app/db.py) fills with the rows it could not classify. Every row in there is
one the old schema never wrote a premiere date onto, because the old schema only
wrote one when a month FROZE. Deciding what such a row was is a question about the
SHOW — when did this season premiere — and the answer lives at the provider, which
a migration has no way to ask. So the migration keeps the rows exactly as they
were, and this settles them on the first tracker load, where asking is ordinary.

WHY THE MONTH IS THE THING BEING RESCUED. A row's month is recorded nowhere else:
re-fetching a season tells you when it premiered, never which month somebody's
tracker had it on. The first version of the split fed these rows straight to the
viewer's list, which drops the month — and with it any chance of ever working out
that the row had been July's announcement rather than something the viewer was
part-way through. A whole month's first notice went quiet and nothing left in the
database could say what had been in it. Deferring costs one season lookup per held
row, once; guessing cost a month.

WHAT EACH ROW TURNS OUT TO BE, and the one question that decides it: did this
season premiere in the month the row sits on?
  - yes -> that month's premiere record, series or season by the season number.
  - no  -> the viewer's own list, which is where a season somebody is part-way
           through belongs and where it carries no month at all.
The comparison is against the season's FULL premiere date, year included, which is
`air_dates[0]` from the season lookup. Not the "M/D" the record renders — a season
that premiered in July 2025 and was sitting on the viewer's July 2026 list matches
that on the month number alone, and would be announced as new a year late.

AN ANNOUNCEMENT THAT HAS BEGUN AIRING GOES ON THE VIEWER'S LIST TOO, and a held
month is the one place that cannot be left to lifecycle.advance to do. Advance is
what normally copies a premiere across once it starts airing, but it runs only for
the month UNDER WAY — and a held month is usually one that has since ended and is
about to freeze, so nothing would ever run it there.

WITHOUT THAT COPY A BINGE VANISHES. A season that dropped all at once and was
finished inside the same month was never carried onto the next one, so it has no
later row to arrive on the list by. It would be left as a premiere record and
nothing else — and `finish()` moves a season off the viewer's LIST onto the month
its history names, so a season that is not on the list can never be recorded as
completed at all. The month announced it, the viewer watched all of it, and its
Completed section would not mention it. A month announcing something and settling
it in the same month is ordinary, which is why the roster split writes both records
too.

A month still AHEAD is the case this does not apply to: nothing on it has aired, so
there is nothing to get through yet and the announcement stands alone until it
does.

AND THE VERDICTS ARE RECORDED IN THE SAME PASS, before the month can freeze — see
_record_completions. A month rebuilt here has usually already ended, and the pass
that normally settles a finished season runs only for the month under way.

A LOOKUP THAT SAYS NOTHING LEAVES THE ROW ON THE VIEWER'S LIST. A season with no
shared trakt id, or one the provider has forgotten, reports no air dates at all,
and there is no evidence it announced anything. The viewer's list is the answer
that keeps it visible and lets a later load — with better ids, or a provider that
has remembered — move it; filing it as a premiere on no evidence would put a title
in a month's announcement precisely where the least is known about it.

DRAINED A MONTH AT A TIME AND SAFE TO INTERRUPT. Each month's rows are settled and
then deleted, so a run that dies half way leaves the months it had not reached
untouched and repeats at most the one it was in — and repeating is harmless,
because both writes are upserts keyed on the record's identity.
"""
from __future__ import annotations

import logging

from .. import db
from ..providers.base import collect_ids
from ..providers.trakt import TraktError
from . import counts, lifecycle, live, store, watch_history
from .store import ID_COLUMNS, RecordKind

logger = logging.getLogger(__name__)


async def pending_months(user_id: int) -> list[str]:
    """The months this viewer still has held rows on, oldest first.

    Ascending so a season held on two months is written from the later one last:
    both writes are upserts on the same identity, and the later month's row is the
    one whose counts are the most recent.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT month FROM distrakt_unsettled_rows WHERE user_id = ? "
        "ORDER BY month",
        (user_id,),
    )
    return [row["month"] for row in rows]


def _record(row) -> dict:
    """A held row in the record shape the season lookup and the store both read.

    Built here rather than through store.row_to_record, which needs a `kind`
    column: what these rows are is the question, so there is nothing to put in one
    yet.
    """
    return {
        "media": row["media"],
        "match_source": row["match_source"],
        "match_id": row["match_id"],
        "season": int(row["season"]),
        "ids": collect_ids({id_key: row[column]
                            for column, id_key in ID_COLUMNS.items()}),
        "title": row["title"] or "",
        "network": row["network"] or "",
        "added_by": row["added_by"] or "",
    }


async def _settled_seasons(user_id: int) -> set[tuple[str, str, str, int]]:
    """Every season this viewer has already settled on some month.

    The migration left these out of the stash, but a load between two drain
    attempts can complete a season that is still held on a later month — and a
    season settled anywhere is off the list, so it must not be put back on by a
    row older than the verdict.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT media, match_source, match_id, season "
        "FROM distrakt_month_records WHERE user_id = ? AND kind IN (?, ?)",
        (user_id, str(RecordKind.COMPLETED), str(RecordKind.ABANDONED)),
    )
    return {(r["media"], r["match_source"], r["match_id"], int(r["season"]))
            for r in rows}


async def settle(user_id: int, settings) -> None:
    """Drain this viewer's held rows, if there are any and Trakt can be asked.

    Returns quietly when there is nothing held — which is every call after the
    first successful one, and every call on an instance that never had an unfrozen
    month. That is why the emptiness check is one indexed lookup and comes first:
    this sits in front of every tracker payload for ever, to pay for a repair that
    happens once.

    A PROVIDER FAILURE LEAVES EVERYTHING HELD AND SAYS SO. The rows are not lost by
    waiting, and settling half a month from a lookup that only half worked would
    write premiere records off missing dates — the exact guess this whole path
    exists to avoid. The next load tries again.
    """
    if not getattr(settings, "trakt_configured", False):
        return
    held = await db.fetch_one(
        "SELECT 1 FROM distrakt_unsettled_rows WHERE user_id = ? LIMIT 1", (user_id,))
    if held is None:
        return
    settled = await _settled_seasons(user_id)
    for month in await pending_months(user_id):
        rows = await db.fetch_all(
            "SELECT * FROM distrakt_unsettled_rows WHERE user_id = ? AND month = ? "
            "ORDER BY rowid",
            (user_id, month),
        )
        records = [_record(row) for row in rows]
        try:
            # allow_degrade=False: one failed lookup aborts the month rather than
            # being rendered around. A degraded lookup here is not a gap in what is
            # shown, it is a wrong answer written down for ever.
            details = await live.fetch_season_details(
                settings, records, fresh=False, allow_degrade=False)
        except TraktError as exc:
            logger.warning(
                "Held tracker rows for %s could not be settled yet (Trakt "
                "unreachable): %s. They are unchanged and the next load retries.",
                month, exc)
            return
        premieres, listed = 0, 0
        for record, detail in zip(records, details):
            detail = dict(detail or {})
            fresh = {**record, **detail, "season": record["season"]}
            announced = _premiered_in(detail, month)
            if announced:
                await store.add_month_record(user_id, month, {
                    **fresh, "kind": store.premiere_kind(record["season"])})
                premieres += 1
            key = store.record_key(record)
            if (key.media, key.match_source, key.match_id,
                    record["season"]) in settled:
                continue
            # An announcement the viewer cannot have started yet stands alone; see
            # this module's header for why every other row goes on the list, and
            # what disappeared when an announcement did not.
            if announced and not detail.get("started_airing"):
                continue
            await lifecycle.follow(user_id, fresh)
            listed += 1
        await db.execute(
            "DELETE FROM distrakt_unsettled_rows WHERE user_id = ? AND month = ?",
            (user_id, month))
        logger.info(
            "Settled %d held tracker row(s) for %s: %d were that month's premieres "
            "and %d went onto the viewer's list. The two overlap — a season this "
            "month announced and the viewer has begun is both.",
            len(rows), month, premieres, listed)
    await _record_completions(user_id, settings)


async def _record_completions(user_id: int, settings) -> None:
    """Move every season the rebuilt list has already finished onto the month its
    watch history names.

    WITHOUT THIS THE VERDICTS ARRIVE A LOAD LATE, and a month freezes before they
    do. lifecycle.advance is what normally records a completion, and it runs only
    for the month UNDER WAY — so a month that ended while these rows were held gets
    its verdicts only once somebody happens to open the current month, and until
    then it renders as an announcement with nothing settled under it. The seasons
    are not lost by waiting (a frozen month still accepts the record, so it heals
    itself on that later load) but a viewer opening the month they just restored
    and finding its Completed section empty has no way to know that.

    NO SEASON LOOKUP: the episode totals were written by the drain that just ran,
    and the counts and the finish dates both come out of the watch history, which
    every month payload syncs anyway. The decision itself is lifecycle's, not
    restated here — a second copy of "may an undated finish be recorded" is exactly
    the kind of drift that puts a wrong month on somebody's record.
    """
    listed = await store.user_records(user_id)
    if not listed:
        return
    state = await watch_history.sync_and_baseline(settings, user_id, listed)
    watched = watch_history.watched_map(state)
    completed_on = watch_history.season_completed_map(state)
    settled = 0
    for record in listed:
        # ONE NUMBER, THE PRIMARY SOURCE'S. Deciding whether a season is finished
        # is a verdict that gets written onto a month, and a verdict cannot be
        # two numbers — see app/distrakt/counts.py, which owns which one it is.
        row = {**record, "watched": counts.primary_count(
            watched.get(live.live_key(record)), live.source_order())}
        settled += await lifecycle.finish_if_done(user_id, row, completed_on)
    if settled:
        logger.info(
            "Recorded %d season(s) the viewer had already finished onto the months "
            "their watch history dates them to.", settled)


def _premiered_in(detail: dict, month: str) -> bool:
    """Whether the season the lookup describes premiered in `month`.

    The first known air date, compared as "YYYY-MM" so the year counts. A season
    with no known air dates premiered nowhere this can prove, and says no.
    """
    air_dates = detail.get("air_dates") or []
    return bool(air_dates) and str(air_dates[0])[:7] == month
