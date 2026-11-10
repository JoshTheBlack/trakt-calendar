"""TMDB's Season Premieres, DERIVED — and derived BEHIND the request, never on it.

TMDB PUBLISHES NO SEASON-PREMIERES CALENDAR, so the answer for a span of days is
worked out from what it does publish. Measured on ten real weeks (2026-09-28 to
2026-11-30):
  1. every show with an episode airing in the span — discover/tv by `air_date`:
     1,750 to 2,466 shows a week, 88 to 124 pages;
  2. less every show with one in each of the four weeks before (the same
     listing, for each of those weeks): what is left had nothing on air for a
     month and is back — 171 to 525 candidates a week;
  3. each candidate's own `/tv/{id}`, whose `seasons[].air_date` says whether a
     season premieres in the span (normalize.season_premieres): 145 to 436
     premieres a week, with no candidate TMDB could not answer for. Most are
     season ones — series premieres, which a season-premieres calendar lists as
     every service's does.
A cold week is therefore a few hundred requests, TENS OF SECONDS, which is why
it never runs on a request: `window` answers from a finished derivation or
raises base.SourcePending at once, having started one (`work`). The calendar
stores what the other services said meanwhile and fills TMDB's share when this
finishes (app/calendar/cache.py).

WHAT IS KEPT, in the shared response cache (api_cache), each under TMDB's
six-month ceiling (transport.CACHE_RETAIN_SECONDS): each week's AIRED SET — the
ids from step 1 — which the four weeks after it reuse as their "before", so a
steady instance pays for one new week's listing per week rather than five; and
each span's DERIVED PREMIERES, the (show, season, day) list step 3 confirmed. The
title answers step 3 read are the ones the details panel reads
(detail.title_payload), so a premiere's record, its panel and its row in the
title store are built from one cached object.

STALE IS SERVED, AND RENEWED BEHIND IT. A derivation older than its clock
(`_TTL`) still answers — the premieres of a week change little from one day to
the next — and a fresh one is worked out behind it, which the calendar's next
fill of that week reads. Only a span with NO derivation is pending.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

from ... import after_response, cache, db, perftrace
from ...config import Settings
from ..base import Media, Record, SourcePending
from . import detail, discover, normalize, transport

logger = logging.getLogger(__name__)

# "BACK FROM A BREAK" MEANS NOTHING ON AIR FOR FOUR WEEKS. Shorter, and a show
# that skips a week or two mid-season reads as a premiere; longer, and a season
# that follows its predecessor closely is missed. Measured at four: of 150
# sampled candidates, every one TMDB's own season list confirmed or refuted, with
# no premiere found missing.
WEEKS_BEFORE = 4
_WEEK = 7

# HOW MANY OF THIS DERIVATION'S REQUESTS ARE IN FLIGHT AT ONCE: half of what the
# client allows (transport.py keeps twenty), so the pages viewers are waiting on
# — a details panel, a search — always find room beside it. Measured: a cold
# week at twelve in flight took 24 seconds.
IN_FLIGHT = 10

# HOW LONG A WEEK'S ANSWERS ARE CURRENT. A span still running, or less than a
# week behind, is renewed on TMDB's own response clock (transport.py says why six
# hours); one further back is history TMDB rarely edits, and renewing it a week
# at a time keeps its few readers from paying for a few hundred requests each.
_PAST_TTL_SECONDS = 7 * 24 * 60 * 60

_AIRED = "tmdb:aired"
_PREMIERES = "tmdb:season-premieres"


class Span(NamedTuple):
    """A run of days, both ends inclusive — what one derivation answers for."""
    first: date
    last: date

    @classmethod
    def of(cls, start: date, days: int) -> "Span":
        return cls(start, start + timedelta(days=max(int(days), 1) - 1))

    def weeks_back(self, weeks: int) -> "Span":
        """The week-long span `weeks` weeks before this one began."""
        first = self.first - timedelta(days=_WEEK * weeks)
        return Span(first, first + timedelta(days=_WEEK - 1))

    def __str__(self) -> str:
        return f"{self.first.isoformat()}..{self.last.isoformat()}"


def _key(kind: str, span: Span) -> str:
    return f"{kind}:{span.first.isoformat()}:{span.last.isoformat()}"


def _ttl(span: Span, now: int) -> int:
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()
    return (_PAST_TTL_SECONDS if span.last < today - timedelta(days=_WEEK)
            else transport.DEFAULT_TTL_SECONDS)


# ---------------------------------------------------------------------------
# what a fill reads
# ---------------------------------------------------------------------------

async def window(settings: Settings, start: date, days: int) -> list[Record]:
    """TMDB's season premieres in [start, start + days), as Records — from the
    span's finished derivation, which a stale one still is. Raises
    base.SourcePending, having started the work, for a span nothing has been
    derived for yet; never waits for it.

    A PREMIERE WHOSE TITLE ANSWER IS NO LONGER STORED (it aged out, or the
    instance's TMDB language changed, which is part of every address) is not
    looked up here — that would put a few hundred requests back on the fill. The
    span is worked out again behind the answer, which stores them, and the
    premieres that could be built are the answer meanwhile; with none at all the
    span is pending again.
    """
    span = Span.of(start, days)
    held = await cache.get_stale(_key(_PREMIERES, span))
    found = held.get("premieres") if isinstance(held, dict) else None
    if not isinstance(found, list):
        raise SourcePending(f"TMDB's season premieres for {span} are still being worked out.",
                            work(settings, span))
    if db.now() - int(held.get("at") or 0) > _ttl(span, db.now()):
        work(settings, span)
    records, missing = await _records(settings, found)
    if missing:
        ready = work(settings, span)
        if not records:
            raise SourcePending(f"TMDB's season premieres for {span} are being worked out "
                                "again.", ready)
    return records


async def _records(settings: Settings, found: list) -> tuple[list[Record], int]:
    """(the records of a derivation's premieres, how many had no stored answer).
    Read from the stored title answers only (`cache_only`)."""
    async def _one(row) -> Record | None:
        tmdb_id, season, day = row
        payload = await detail.title_payload(settings, tmdb_id, Media.SHOW, cache_only=True)
        return normalize.to_record(payload, Media.SHOW, int(season), str(day)) if payload else None

    built = await asyncio.gather(*(_one(row) for row in found
                                   if isinstance(row, list) and len(row) == 3))
    records = [record for record in built if record is not None]
    return records, len(built) - len(records)


# ---------------------------------------------------------------------------
# the work, one span at a time
# ---------------------------------------------------------------------------

class _Job(NamedTuple):
    settings: Settings
    done: asyncio.Future


# THE SPANS WAITING TO BE WORKED OUT, in the order they were asked for, the one
# being worked out first. An entry leaves the moment its work ends, so this
# holds no more than the spans asked for and not yet answered — a month's five
# or six, or the scheduled refresh's two months.
_waiting: dict[Span, _Job] = {}
_worker: asyncio.Task | None = None
# LOOP-KEYED, like the client pool: a future belongs to the loop that made it,
# and the suite runs every test on a loop of its own.
_loop = None


def pending() -> int:
    """How many spans are waiting to be worked out, or being worked out now."""
    return len(_waiting) if _loop is _running_loop() else 0


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def work(settings: Settings, span: Span) -> asyncio.Future:
    """Have `span` worked out — joining it if it is already waiting — and return
    the future that finishes when it is (and raises when it failed). Starts the
    worker that works the spans out one at a time — once the request that asked
    has answered (after_response), so its few hundred requests a span do not
    begin beside that page — and returns at once."""
    global _worker, _loop
    loop = asyncio.get_running_loop()
    if _loop is not loop:
        _waiting.clear()
        _worker, _loop = None, loop
    job = _waiting.get(span)
    if job is None:
        job = _Job(settings, loop.create_future())
        # A failure nobody is waiting for — a stale span renewed behind its
        # answer — is logged where it happens, and retrieved here so asyncio does
        # not report it a second time as never retrieved.
        job.done.add_done_callback(lambda done: done.cancelled() or done.exception())
        _waiting[span] = job
        logger.info("TMDB season premieres: the days %s are queued to be worked out "
                    "(%d waiting)", span, len(_waiting))
    after_response.later(_start_worker)
    return job.done


def _start_worker() -> None:
    global _worker
    if _worker is None or _worker.done():
        _worker = asyncio.get_running_loop().create_task(_work_through())


async def _work_through() -> None:
    """Work out every waiting span, oldest first, one at a time — one span is a
    few hundred requests and they share the client with every page.

    DETACHED FROM THE REQUEST THAT STARTED IT (perftrace.detach): a task copies
    the context it was created in, so its requests would otherwise be timed as
    the page's, long after the page was answered."""
    perftrace.detach()
    with perftrace.activity("TMDB season premieres"):
        await _each_waiting()


async def _each_waiting() -> None:
    while _waiting:
        span, job = next(iter(_waiting.items()))
        try:
            found = await derive(job.settings, span)
        except asyncio.CancelledError:
            job.done.cancel()
            raise
        except Exception as exc:  # delivered to whoever waits; the worker carries on
            if isinstance(exc, transport.TmdbError):
                logger.warning("TMDB season premieres: the days %s could not be worked out "
                               "(%s); the calendar asks again on its next retry", span, exc)
            else:
                logger.exception("TMDB season premieres: working out the days %s failed", span)
            if not job.done.done():
                job.done.set_exception(exc)
        else:
            if not job.done.done():
                job.done.set_result(len(found))
        finally:
            _waiting.pop(span, None)


async def derive(settings: Settings, span: Span) -> list[tuple[int, int, str]]:
    """Work `span` out now, store what was found, and return it: (tmdb id,
    season, premiere day) for every season premiering in it. Raises
    transport.TmdbError when TMDB could not be asked for something it needed —
    a derivation from a partial listing would drop premieres and call it whole.
    """
    started = time.perf_counter()
    gate = asyncio.Semaphore(IN_FLIGHT)
    logger.info("TMDB season premieres: working out the days %s", span)
    this, *before = await asyncio.gather(
        aired(settings, span, gate),
        *(aired(settings, span.weeks_back(weeks), gate) for weeks in range(1, WEEKS_BEFORE + 1)))
    earlier = set().union(*before)
    candidates = sorted(this - earlier)

    async def _confirm(tmdb_id: int) -> list[tuple[int, int, str]]:
        async with gate:
            payload = await detail.title_payload(settings, tmdb_id, Media.SHOW)
        return [(tmdb_id, season, day)
                for season, day in normalize.season_premieres(payload or {}, span.first,
                                                               span.last)]

    found = sorted(premiere for confirmed in await asyncio.gather(
        *(_confirm(tmdb_id) for tmdb_id in candidates)) for premiere in confirmed)
    await cache.set(_key(_PREMIERES, span),
                    {"at": db.now(), "premieres": [list(premiere) for premiere in found]},
                    retain_seconds=transport.CACHE_RETAIN_SECONDS)
    logger.info("TMDB season premieres: the days %s — %d shows on air, %d in the %d weeks "
                "before, %d back from a break, %d premieres; %.1f s", span, len(this),
                len(earlier), WEEKS_BEFORE, len(candidates), len(found),
                time.perf_counter() - started)
    return found


async def aired(settings: Settings, span: Span, gate: asyncio.Semaphore) -> set[int]:
    """Every show with an episode airing in `span`, by tmdb id — from the stored
    set while it is current (`_ttl`), otherwise from TMDB's listing, which is
    then stored for the spans after it to reuse."""
    key = _key(_AIRED, span)
    held = await cache.get(key, _ttl(span, db.now()))
    if isinstance(held, dict) and isinstance(held.get("ids"), list):
        return {int(tmdb_id) for tmdb_id in held["ids"]}
    listed = await discover.entries(settings, "tv", "air_date", span.first, span.last, gate=gate)
    ids = sorted({int(entry["id"]) for entry in listed if isinstance(entry.get("id"), int)})
    await cache.set(key, {"at": db.now(), "ids": ids},
                    retain_seconds=transport.CACHE_RETAIN_SECONDS)
    return set(ids)
