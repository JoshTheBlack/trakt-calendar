"""The title store's refresh queue: service rows a surface found stale, re-read
behind the reader.

STALE WHILE IT REVALIDATES. A surface draws what the store holds at once — a
details panel, a tracker row, a board row, a calendar card — and a row it found
past its clock is QUEUED here (`queue_viewed`) rather than asked for while the
reader waits. The queue is a table (metadata_refresh), so a restart does not
forget what was owed, and it is keyed on the row, so asking twice queues once.
`drain` re-reads what is queued, a bounded batch per service per pass, on the
same latch every other background pass of the title store runs on
(app/calendar/enrich.py's run_drain), at each service's own pace.

WHAT "VIEWED" MEANS IS EACH SURFACE'S CALL, and every one of them is a viewer's
own page: opening a details panel (`PANEL_BUDGET`), a tracker's or a board's rows
drawn (`ROWS_BUDGET`), a calendar month drawn (`MONTH_BUDGET`). A PUBLIC READ —
a share page, its preview card — NEVER QUEUES: a stranger's visit spends nothing
of the owner's, and the owner's own views keep what the link shows current.

A REFRESH IS THE SERVICE ASKED AGAIN, PAST ITS RESPONSE CACHE (`fresh`), and its
answer filed exactly as a first answer is (app/metadata/descriptions.py): every
field, its ids through the one resolution — so a service that now names the
title differently re-keys it — and its clock renewed. For TMDB a 304 is an
answer: the row is re-obtained and its six-month ceiling restarts.

A DEAD PICTURE JUMPS THE QUEUE (`queue_title`, reason `REASON_DEAD_PICTURE`): a
picture a service recorded no longer loads, which says the title itself may have
moved there — a replaced poster, a re-pointed id — so every service that knows
the title is asked again, ahead of everything waiting.

A SPARSE LISTING IS COMPLETED HERE TOO (`queue_listed`, reason `REASON_LISTED`):
a service whose calendar listings arrive with no network, status or
certification, and whose own description is one request away with the
instance's credential (Capabilities.describes_sparse_listings — TMDB's
discover listings), has each such row described in full, on the heartbeat, at
the service's own pace. What a viewer asked for stays ahead of it.

A CALENDAR TITLE IS DESCRIBED BY EVERY SERVICE WHOSE DESCRIPTION JOINS ITS
CARDS (`queue_cards`, reason `REASON_CARD`): a service declaring
Capabilities.describes_every_card (TMDB) is asked about each title on the
calendars the heartbeat keeps current that it has an id for and no row of,
within the same minute the sparse listings share (`_background_room`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Coroutine, Iterable, Mapping
from datetime import date
from typing import Any, NamedTuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .. import db, providers
from ..providers import detail_source
from ..providers.base import Media, SourceUnavailable
from . import descriptions, episodes, groups, holders, identity, rows, tables

logger = logging.getLogger(__name__)

# metadata_refresh.season for a title's own row (migration 47 says why not NULL).
TITLE = -1

REASON_VIEWED = "viewed"
REASON_DEAD_PICTURE = "dead picture"
REASON_CEILING = "ceiling"
REASON_LISTED = "listed"
REASON_CARD = "card"

# HOW MANY ROWS ONE VIEW MAY QUEUE. A panel is one title: every service that
# knows it, its row and the season on screen. A page of rows is a tracker month
# (about sixty seasons) or a board page; a calendar month names hundreds of
# titles. The stalest go first, and a view that finds more queues the rest next
# time — these bound a burst, not the work.
PANEL_BUDGET = 12
ROWS_BUDGET = 80
MONTH_BUDGET = 120

# HOW MANY QUEUED ROWS MAY BE RE-READ IN ANY MINUTE, PER SERVICE, however many
# passes ask — and every page view starts one (`viewed`), so a budget per PASS
# was a budget per reload. A refresh shares its service's client with every page
# a viewer is waiting on, so this is what it may take of it:
#   TMDB: six hundred. A refresh is two or three requests, and TMDB's sparse
#   calendar listings arrive by the thousand (two months of Series and Movie
#   Premieres, ~2,300 on the development copy). MEASURED against live TMDB,
#   draining that backlog at 600 a minute: 600 titles a pass in 7–31 s, 41–172
#   requests a second in the bursts, NO 429 — and a page's own TMDB request
#   waited at most 0.5 s meanwhile with six in flight (2.3 s with ten). Should
#   TMDB ever answer 429, the transport holds requests for its Retry-After and
#   the pass leaves the rest queued (`drain`), so this is a pace, not a bet.
#   TRAKT's catalogue reads share the application's bucket — 150 up front, then
#   one a second (providers/trakt/transport.py's APP_GET_PACER) — and a refresh
#   is up to five. MEASURED on a copy of the development database: at twenty a
#   pass, a few tracker reloads spent the bucket and the viewer's own sync read
#   waited 1.3 s behind the queue. Four a minute is twenty reads, a third of the
#   pace, and leaves the bucket to the pages.
#   SIMKL's per-title reads are its cached-by-id family, paced by its own client
#   (providers/simkl/transport.py).
PER_MINUTE = {"tmdb": 600, "trakt": 4, "simkl": 40}

# What each service has been asked in the last minute by this process, as the
# monotonic moments each refresh started (`_allowance`).
_asked: dict[str, deque[float]] = {}


def _allowance(source: str, at: float) -> int:
    """How many more of `source`'s rows may be re-read now (`PER_MINUTE`)."""
    started = _asked.setdefault(source, deque())
    while started and at - started[0] >= 60:
        started.popleft()
    return max(0, PER_MINUTE[source] - len(started))

# How many of a service's refreshes run side by side. TMDB's six are about twelve
# of its client's twenty requests in flight (providers/tmdb/transport.py), which
# leaves the rest of the pool to the pages: MEASURED in the drain above, a page's
# TMDB request waited at most 0.5 s at six against 2.3 s at ten, for no loss of
# pace. Trakt's client gates every call
# behind one semaphore anyway (app/calendar/enrich.py's drain_releases says why
# firing a batch at it only queues on that gate), so it goes one at a time.
# Both tables name every registered service — tests/metadata/test_refresh.py
# fails on one that is missing, because a service with no pace of its own must
# not inherit somebody else's.
IN_FLIGHT = {"tmdb": 6, "trakt": 1, "simkl": 4}

# A ROW THAT FAILED TO REFRESH WAITS before a view may queue it again, doubling
# from an hour to a day — the backoff app/calendar/enrich.py keeps for its own
# lookups — so a service that is down is not asked once per page load.
_BACKOFF_BASE_SECONDS = 60 * 60
_BACKOFF_MAX_SECONDS = 24 * 60 * 60


class Wanted(NamedTuple):
    """One title a surface drew: what it is, every id known for it, and the
    season on screen (None for the title alone, or a film)."""
    media: str
    ids: Mapping[str, Any]
    season: int | None = None


class Entry(NamedTuple):
    """One queued row: the service, the title by the id that service is ASKED by
    (its own, which for a Trakt row is not the row's slug-shaped source_id), and
    the season (`TITLE` for the title's own row)."""
    source: str
    media: str
    own_id: str
    season: int


def _backed_off(row, now: int) -> bool:
    """Whether a row's last refresh failed recently enough to leave it alone."""
    if row is None or not row["fail_count"] or row["failed_at"] is None:
        return False
    wait = min(_BACKOFF_BASE_SECONDS * (2 ** (int(row["fail_count"]) - 1)),
               _BACKOFF_MAX_SECONDS)
    return now - int(row["failed_at"]) < wait


def _askable(settings) -> list[tuple[str, Any]]:
    """(source, detail port) for every service this instance can ask now, in
    declared order — a row nobody can refresh is not queued."""
    return [(str(source), port) for source, port in detail_source.ports()
            if port.catalogue_configured(settings)]


def _due(conn: db.Connection, settings, wanted: Iterable[Wanted], *, now: int,
         missing: bool) -> list[tuple[int, Entry]]:
    """(how stale, entry) for every service row of `wanted` that is due — past
    its clock, or not described at all where `missing` allows it. A row that
    does not exist is the stalest there is (0).

    BY THE TITLE'S KEY, READ ONCE FOR THE PAGE: each title the page drew is
    resolved to its key, and every service's rows under those keys are read in
    one indexed query, its seasons in another — 40 ms for a calendar month of
    1,500 titles on the development copy, where asking each service about each
    title in turn took half a second. A service the page's ids do not name is
    asked by the id its own row states, so a title drawn from one service's ids
    still has its other descriptions kept current.
    """
    asked: dict[str, tuple[str, dict, set[int]]] = {}
    for item in wanted:
        media = str(item.media)
        key = identity.resolve(conn, media, item.ids)
        if key is None:
            continue
        _media, ids, seasons = asked.setdefault(key, (media, {}, set()))
        ids.update({name: value for name, value in (item.ids or {}).items()
                    if value not in (None, "")})
        if item.season is not None and media == str(Media.SHOW):
            seasons.add(int(item.season))
    if not asked:
        return []
    held: dict[tuple[str, str], list] = {}
    keys = json.dumps(list(asked))
    for row in conn.execute(
            rows.title_groups_sql(("ids_json", "season_count", "stale_after", "failed_at",
                                   "fail_count"),
                                  where="t.title_key IN (SELECT value FROM json_each(?))"),
            (keys,) * len(groups.sources())):
        held.setdefault((str(row["title_key"]), str(row["source"])), []).append(row)
    out: list[tuple[int, Entry]] = []
    # (described row, service, its own id, the seasons the page drew)
    seasons_of: list[tuple[Any, str, str, set[int]]] = []
    for source, _port in _askable(settings):
        namespace = rows.ID_NAMESPACE[source]
        for key, (media, ids, seasons) in asked.items():
            found = held.get((key, source), [])
            row = descriptions.described_row(found)
            own_id = str(ids.get(namespace) or "").strip() or next(
                (str(stated) for stated in (_stated_id(r, namespace) for r in found) if stated),
                "")
            if not own_id:
                continue
            if row is None:
                if missing and not any(_backed_off(r, now) for r in found):
                    out.append((0, Entry(source, media, own_id, TITLE)))
                continue
            if int(row["stale_after"] or 0) <= now and not _backed_off(row, now):
                out.append((int(row["stale_after"] or 0), Entry(source, media, own_id, TITLE)))
            if seasons:
                seasons_of.append((row, source, own_id, seasons))
    stored: dict[tuple[str, str, int], Any] = {}
    by_source: dict[str, list[str]] = {}
    for row, source, _own_id, _seasons in seasons_of:
        by_source.setdefault(source, []).append(str(row["title_key"]))
    for source, title_keys in by_source.items():
        for season in conn.execute(
                f"SELECT title_key, season, "
                + groups.select(tables.SEASONS, source, ("episode_count", "air_dates_json",
                                                         "stale_after", "failed_at",
                                                         "fail_count"))
                + f" FROM {tables.SEASONS} WHERE title_key IN (SELECT value FROM json_each(?)) "
                f"AND {groups.present(tables.SEASONS, source)}",
                (json.dumps(sorted(set(title_keys))),)):
            stored[(source, str(season["title_key"]), int(season["season"]))] = season
    for row, source, own_id, seasons in seasons_of:
        media = str(row["media"])
        for season in sorted(seasons):
            of_season = stored.get((source, str(row["title_key"]), season))
            if not descriptions.season_described(of_season):
                if missing and not _backed_off(of_season, now):
                    out.append((0, Entry(source, media, own_id, season)))
            elif int(of_season["stale_after"] or 0) <= now and not _backed_off(of_season, now):
                out.append((int(of_season["stale_after"] or 0),
                            Entry(source, media, own_id, season)))
    return out


def _stated_id(row, namespace: str) -> str:
    """The id a stored row says its service knows the title by, or ""."""
    try:
        ids = json.loads(row["ids_json"] or "{}")
    except (TypeError, ValueError):
        return ""
    return str(ids.get(namespace) or "").strip() if isinstance(ids, dict) else ""


# QUEUE `:entries` — the first `:limit` of them not already waiting, in order — in
# ONE statement. A write transaction holds the database's one write lock for as
# long as its statements take, and while the event loop is busy each statement
# can wait a GIL switch interval; a queue written a row at a time held it longer
# the more it queued (a calendar month's view, a minute's allowance of a sparse
# backlog).
_QUEUE = """
INSERT INTO metadata_refresh (source, media, source_id, season, queued_at, reason)
SELECT json_extract(j.value, '$[0]'), json_extract(j.value, '$[1]'),
       json_extract(j.value, '$[2]'), json_extract(j.value, '$[3]'), :now, :reason
FROM json_each(:entries) j
WHERE NOT EXISTS (SELECT 1 FROM metadata_refresh q
                  WHERE q.source = json_extract(j.value, '$[0]')
                    AND q.media = json_extract(j.value, '$[1]')
                    AND q.source_id = json_extract(j.value, '$[2]')
                    AND q.season = json_extract(j.value, '$[3]'))
ORDER BY j.key LIMIT :limit
ON CONFLICT(source, media, source_id, season) DO NOTHING
"""


def _queue(conn: db.Connection, entries: Iterable["Entry"], *, now: int, reason: str,
           limit: int) -> int:
    """Queue the first `limit` of `entries` not already waiting, inside the
    caller's transaction. Returns how many were queued. Each entry once: the
    statement judges every row against the queue as it stood before it ran."""
    entries = list(dict.fromkeys(entries))
    if not entries or limit <= 0:
        return 0
    return conn.execute(_QUEUE, {"entries": json.dumps([list(entry) for entry in entries]),
                                 "now": now, "reason": reason, "limit": limit}).rowcount


async def queue_viewed(settings, wanted: Iterable[Wanted], *, budget: int,
                       missing: bool = True, now: int | None = None) -> int:
    """Queue the due rows of the titles a viewer's page just drew — the stalest
    first, at most `budget` of them. Returns how many were queued.

    `missing` says whether a service the title's ids name and the store holds no
    description from is due too: true for a panel, a tracker row and a board row,
    which draw the description; false for a calendar month, whose cards draw
    each service's own listing and are kept current by the calendar's fill, so
    only a description somebody already asked for is refreshed by being seen.
    """
    wanted = list(wanted)
    if not wanted or budget <= 0:
        return 0
    ts = db.now() if now is None else now
    # READ, THEN WRITE: deciding what is due reads every title the page drew,
    # and doing that inside the write would hold the database's one write lock
    # for the length of it.
    due = await db.run(lambda conn: _due(conn, settings, wanted, now=ts, missing=missing))
    entries = list(dict.fromkeys(entry for _stale, entry in sorted(due, key=lambda p: p[0])))
    if not entries:
        return 0

    queued = await db.transaction(
        lambda conn: _queue(conn, entries, now=ts, reason=REASON_VIEWED, limit=budget))
    if queued:
        logger.debug("title store: queued %d stale row(s) for refresh", queued)
    return queued


def viewed(settings, wanted: Iterable[Wanted], *, budget: int, missing: bool = True) -> None:
    """A page has drawn these titles: queue their due rows (`queue_viewed`) and
    start a pass over them — BEHIND THE PAGE, as a task of its own that the
    caller never waits for. The page has already drawn what the store holds;
    nothing it shows depends on this, so neither it nor the next request on the
    connection pays for the read that decides what is due (about fifty
    milliseconds for a calendar month of 1,500 titles on the development copy).
    A view whose queueing fails costs nothing but one more view before it is
    queued."""
    wanted = list(wanted)
    if wanted and budget > 0:
        start(_queue_then_run(settings, wanted, budget, missing))


async def _queue_then_run(settings, wanted: list[Wanted], budget: int, missing: bool) -> None:
    try:
        queued = await queue_viewed(settings, wanted, budget=budget, missing=missing)
    except db.DatabaseError:
        logger.warning("title store: could not queue a refresh for a viewed page",
                       exc_info=True)
        return
    if queued:
        # At once rather than at the next heartbeat, on the same latch and
        # budgets, so a title the page drew with no words has them by the next
        # load.
        await run(settings)


# THE HEALS UNDER WAY, by entry: which title each queued row of a heal belongs
# to, and when it was queued, so the drain can say when a title's heal is
# finished — its rows may span passes, a service's minute being spent. In
# process, as the heal is.
_heals: dict["Entry", tuple[str, float]] = {}

# HOW LONG A HEAL'S BOOKKEEPING IS KEPT FOR ROWS THE DRAIN HAS NOT REACHED. A
# heal's rows sit at the front of the queue and are re-read within minutes; one
# still queued hours later belongs to a service this instance can no longer ask
# (its credential removed), and stays queued in the table until it can. The
# "heal finished" line is all this map is for, and the process runs for weeks
# between restarts, so an entry that old is dropped rather than held for them.
_HEAL_BOOKKEEPING_SECONDS = 6 * 60 * 60


def _forget_old_heals(moment: float) -> None:
    for entry in [entry for entry, (_key, since) in _heals.items()
                  if moment - since >= _HEAL_BOOKKEEPING_SECONDS]:
        del _heals[entry]


async def queue_title(settings, media: str, ids: Mapping[str, Any], seasons: Iterable[int] = (),
                      *, reason: str, title_key: str = "") -> list[Entry]:
    """Queue EVERY service row of one title — its own and the seasons named —
    whatever their clocks say, at the FRONT of the queue (queued_at 0), moving
    any already waiting there. What a confirmed dead picture asks for, naming
    the title (`title_key`) so the drain can report the heal finished; returns
    what was queued."""
    entries = [Entry(source, str(media), own_id, season)
               for source, _port in _askable(settings)
               if (own_id := str(ids.get(rows.ID_NAMESPACE[source]) or "").strip())
               for season in (TITLE, *(() if str(media) == str(Media.MOVIE) else seasons))]
    if not entries:
        return []

    await db.execute(
        "INSERT INTO metadata_refresh (source, media, source_id, season, queued_at, reason) "
        "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
        "json_extract(value, '$[2]'), json_extract(value, '$[3]'), 0, ? "
        "FROM json_each(?) WHERE true ON CONFLICT(source, media, source_id, season) "
        "DO UPDATE SET queued_at = 0, reason = excluded.reason",
        (reason, json.dumps([list(entry) for entry in entries])))
    if title_key:
        moment = time.monotonic()
        _forget_old_heals(moment)
        _heals.update(dict.fromkeys(entries, (str(title_key), moment)))
    return entries


# A HELD TITLE OUTLIVES ITS CEILING BY BEING RE-READ BEFORE IT. A service whose
# terms bound how long its words may be kept (rows.sweep_past_ceilings) would
# otherwise take a tracker's or a board's words away from a row nobody happened
# to open for half a year — and a backup made then would carry no name for it.
# So the heartbeat queues every held title's rows of such a service that will
# reach the ceiling within `CEILING_MARGIN_SECONDS`, `CEILING_BUDGET` a pass,
# BEHIND what viewers asked for. A title nobody holds is left to its ceiling:
# keeping it would be keeping it for nobody.
CEILING_MARGIN_SECONDS = 14 * 24 * 60 * 60
CEILING_BUDGET = 40

def _described_season(source: str) -> str:
    """SQL over a season row aliased `s`: `source`'s group holds a DESCRIPTION
    — a season list alone is re-read with its title's group, which renews a
    group holding nothing else (rows.ceiling_clock)."""
    def column(name: str) -> str:
        return groups.column(tables.SEASONS, source, name, "s")
    return (f"({column('overview')} != '' OR {column('cast_json')} NOT IN ('', '[]') "
            f"OR {column('air_dates_json')} NOT IN ('', '[]'))")


async def queue_ceiling(settings, *, now: int | None = None) -> int:
    """Queue the held rows of every service with a ceiling that are within
    `CEILING_MARGIN_SECONDS` of it — the oldest first, at most `CEILING_BUDGET`
    — with reason `REASON_CEILING`. Returns how many were queued."""
    ts = db.now() if now is None else now
    askable = {source for source, _port in _askable(settings)}
    bounded = {str(source): provider.capabilities.retain_seconds
               for source, provider in providers.registered().items()
               if provider.capabilities.retain_seconds is not None and str(source) in askable}
    if not bounded:
        return 0

    # READ, THEN WRITE, as `queue_viewed` does: which held rows near a ceiling is
    # a read of every feature's holders and every bounded service's rows, and
    # made inside the write it held the database's one write lock for all of it.
    def _due(conn) -> list[Entry]:
        held = holders.held_keys(conn)
        due: list[tuple[int, Entry]] = []
        for source, retain in bounded.items():
            namespace = rows.ID_NAMESPACE[source]
            threshold = ts - int(retain) + CEILING_MARGIN_SECONDS
            own = f"CAST(json_extract(t.{source}_ids_json, '$.{namespace}') AS TEXT) AS own_id"
            described = _described_season(source)
            for row in conn.execute(
                    f"SELECT t.title_key, t.media, {own}, t.{source}_fetched_at AS fetched_at, "
                    f"NULL AS season FROM {tables.TITLES} t WHERE t.{source}_fetched_at > 0 "
                    f"AND t.{source}_fetched_at <= ? "
                    "UNION ALL "
                    f"SELECT t.title_key, t.media, {own}, s.{source}_fetched_at, s.season "
                    f"FROM {tables.SEASONS} s JOIN {tables.TITLES} t ON t.title_key = s.title_key "
                    f"WHERE t.{source}_id IS NOT NULL AND s.season > 0 AND {described} "
                    f"AND s.{source}_fetched_at > 0 AND s.{source}_fetched_at <= ?",
                    (threshold, threshold)):
                if str(row["title_key"]) not in held:
                    continue
                own_id = str(row["own_id"] or "").strip()
                if own_id:
                    season = TITLE if row["season"] is None else int(row["season"])
                    due.append((int(row["fetched_at"]),
                                Entry(source, str(row["media"]), own_id, season)))
        return [entry for _clock, entry in sorted(due, key=lambda pair: pair[0])]

    due = await db.run(_due)
    queued = await db.transaction(
        lambda conn: _queue(conn, due, now=ts, reason=REASON_CEILING, limit=CEILING_BUDGET))
    if queued:
        logger.info("title store: queued %d held row(s) nearing their service's ceiling",
                    queued)
    return queued


# A SPARSE LISTING NOBODY CAN DESCRIBE IS GIVEN UP ON after this many refreshes
# that failed or said nothing — five, over about fifteen hours of the doubling
# backoff, the ceiling the Simkl drain keeps for its own lookups
# (app/calendar/enrich.py's `_GIVE_UP_AFTER`). A title the service lists but will
# not describe would otherwise be asked about daily for as long as it is listed,
# and hold the stats box's count up for all of it. Its row still draws what the
# listing said.
LISTED_GIVE_UP_AFTER = 5


def _listing_sources() -> tuple[str, ...]:
    """Every registered service whose sparse listings are completed by describing
    them (Capabilities.describes_sparse_listings), by name."""
    return tuple(str(source) for source, provider in providers.registered().items()
                 if provider.capabilities.describes_sparse_listings)


# WHAT A SERVICE'S SPARSE LISTINGS OWE, said once for the queue and the count it
# is read back as, because the two answering differently is what makes a backlog
# readout untrustworthy. Parameter: the give-up ceiling.
def _owed_listings(source: str) -> str:
    return (f"FROM {tables.TITLES} t WHERE t.{source}_id IS NOT NULL "
            f"AND t.{source}_enriched = 0 AND t.{source}_fail_count < ?")


async def queue_listed(settings, *, now: int | None = None) -> int:
    """Queue each sparse listing — a title row its service's calendar wrote
    unenriched (Capabilities.describes_sparse_listings) — for a full description,
    newest listings first, with reason `REASON_LISTED`, keeping no more of them
    waiting per service than its minute's allowance (`PER_MINUTE`): the drain
    takes no more than that a minute anyway, and whatever a viewer queues is not
    held up behind a backlog queued all at once. A row still inside its backoff,
    or given up on (`LISTED_GIVE_UP_AFTER`), is left. Returns how many were
    queued."""
    ts = db.now() if now is None else now
    askable = {source for source, _port in _askable(settings)}
    sources = [source for source in _listing_sources() if source in askable]
    if not sources:
        return 0

    # READ, THEN WRITE: the owed rows are a backlog of thousands, and reading them
    # inside the write held its lock for every row read. A minute's allowance of
    # them is all one pass can queue, so no more are kept.
    def _owed(conn) -> dict[str, list[Entry]]:
        owed: dict[str, list[Entry]] = {}
        for source in sources:
            own = (f"CAST(json_extract(t.{source}_ids_json, '$.{rows.ID_NAMESPACE[source]}') "
                   "AS TEXT)")
            found = owed.setdefault(source, [])
            for row in conn.execute(
                    f"SELECT t.media, {own} AS own_id, t.{source}_fail_count AS fail_count, "
                    f"t.{source}_failed_at AS failed_at "
                    f"{_owed_listings(source)} AND NOT EXISTS (SELECT 1 FROM metadata_refresh q "
                    f"WHERE q.source = '{source}' AND q.media = t.media AND q.source_id = {own} "
                    f"AND q.season = {TITLE}) ORDER BY t.{source}_fetched_at DESC",
                    (LISTED_GIVE_UP_AFTER,)):
                if len(found) >= PER_MINUTE[source]:
                    break
                if row["own_id"] and not _backed_off(row, ts):
                    found.append(Entry(source, str(row["media"]), str(row["own_id"]), TITLE))
        return owed

    owed = await db.run(_owed)

    def _work(conn) -> int:
        return sum(_queue(conn, entries, now=ts, reason=REASON_LISTED,
                          limit=_background_room(conn, source))
                   for source, entries in owed.items())

    queued = await db.transaction(_work)
    if queued:
        logger.debug("title store: queued %d sparse listing(s) to be described", queued)
    return queued


# WHAT THE HEARTBEAT QUEUES ON ITS OWN, rather than for a viewer: sparse listings
# (`queue_listed`) and the calendar's titles a service has no description of yet
# (`queue_cards`). TOGETHER they keep no more than a service's minute
# (`PER_MINUTE`) waiting, so whatever a viewer queues is never more than a
# minute's drain behind them; a minute each would put it two minutes behind. The
# sparse listings, queued first each tick, are the ones a card draws with no
# words at all.
_BACKGROUND_REASONS = (REASON_LISTED, REASON_CARD)


def _background_room(conn: db.Connection, source: str) -> int:
    """How many more of `source`'s rows the heartbeat may queue now."""
    waiting = conn.execute(
        "SELECT COUNT(*) FROM metadata_refresh WHERE source = ? AND reason IN "
        f"({', '.join('?' * len(_BACKGROUND_REASONS))})",
        (source, *_BACKGROUND_REASONS)).fetchone()[0]
    return PER_MINUTE[source] - int(waiting)


def _card_sources() -> tuple[str, ...]:
    """Every registered service whose description joins every calendar card of a
    title it knows (Capabilities.describes_every_card), by name."""
    return tuple(str(source) for source, provider in providers.registered().items()
                 if provider.capabilities.describes_every_card)


def _cards_owed_sql(source: str) -> str:
    """The titles a calendar listing dated in [:lo, :hi) names that `source` has
    an id for and no group of, nearest :today first, at most :limit — the titles
    each on its own row, (media, own id), none already queued.

    THE LISTINGS ARE NARROWED TO THE SPAN FIRST AND THEN TO TITLES, and only then
    is each title checked: MEASURED on the development copy, 11,290 titles from
    37,942 listings in two months, 130 ms — checking each listing in turn, or
    grouping every listing stored, took 360-480 ms. Each service's air-time index
    is what reaches a span's listings; the service and its namespace are the
    registry's, never input."""
    own = f"CAST(json_extract(i.ids_json, '$.{rows.ID_NAMESPACE[source]}') AS TEXT)"
    listed = " UNION ALL ".join(
        f"SELECT title_key, media, {name}_air_ts AS at FROM {tables.EPISODES} "
        f"WHERE {name}_calendars != 0 AND {name}_air_ts >= :lo AND {name}_air_ts < :hi "
        f"UNION ALL SELECT e.title_key, e.media, json_extract(r.value, '$.at') "
        f"FROM {tables.EPISODES} e, json_each(e.{name}_repeats_json) r "
        f"WHERE e.{name}_repeats_json != '[]' AND json_extract(r.value, '$.at') >= :lo "
        f"AND json_extract(r.value, '$.at') < :hi"
        for name in groups.sources() if groups.has(tables.EPISODES, name, "air_ts"))
    return (
        f"SELECT d.media, {own} AS own_id FROM ("
        "  SELECT l.title_key, l.media, "
        "         MIN(ABS(julianday(date(l.at, 'unixepoch')) - julianday(:today))) AS distance "
        f"  FROM ({listed}) l GROUP BY l.title_key) d "
        f"JOIN {tables.TITLES} i ON i.title_key = d.title_key "
        f"WHERE {own} IS NOT NULL AND {groups.absent(tables.TITLES, source, 'i')} "
        "  AND NOT EXISTS (SELECT 1 FROM metadata_refresh q "
        f"                  WHERE q.source = '{source}' AND q.media = d.media "
        f"                    AND q.source_id = {own} AND q.season = {TITLE}) "
        "ORDER BY d.distance LIMIT :limit")


async def queue_cards(settings, first: date, last: date, *, today: date,
                      now: int | None = None) -> int:
    """Queue a description of each title a calendar airing dated in [first, last)
    names, from every service whose description joins every card of a title it
    knows (Capabilities.describes_every_card — TMDB's), where the title has that
    service's id and the store no row of it — nearest `today` first, with reason
    `REASON_CARD`, inside what the heartbeat may keep waiting
    (`_background_room`). Returns how many were queued.

    WHY: such a service's row is what joins a card as its title-only record
    (app/calendar/cache.py's `with_title_only`) — the certification, language,
    keywords and show type a card nobody else describes is filtered by — and a
    row exists only where something asked: its own listings, a panel opened, a
    tracker or a board row. MEASURED on the development copy, 7,157 titles on
    this and next month's calendars had a TMDB id and no TMDB row, 5,828 of them
    films. At TMDB's allowance of six hundred titles a minute (two or three
    requests each) that backlog clears in about twelve minutes after a deploy,
    and then each new title as it arrives.

    A TITLE THE SERVICE WILL NOT DESCRIBE is not asked forever: its failure leaves
    a bare row (descriptions.plan_failures), which this no longer queues, and
    which `queue_listed` retries on the backoff until LISTED_GIVE_UP_AFTER."""
    ts = db.now() if now is None else now
    askable = {source for source, _port in _askable(settings)}
    sources = [source for source in _card_sources() if source in askable]
    if not sources:
        return 0

    # READ, THEN WRITE, as `queue_listed` does: the titles owed are read from every
    # airing in two months, and a minute's allowance of them is all a tick queues.
    def _owed(conn) -> dict[str, list[Entry]]:
        return {source: [Entry(source, str(row["media"]), str(row["own_id"]), TITLE)
                         for row in conn.execute(_cards_owed_sql(source), {
                             "lo": episodes.epoch(first), "hi": episodes.epoch(last),
                             "today": today.isoformat(), "limit": PER_MINUTE[source]})]
                for source in sources}

    owed = await db.run(_owed)

    def _work(conn) -> int:
        return sum(_queue(conn, entries, now=ts, reason=REASON_CARD,
                          limit=_background_room(conn, source))
                   for source, entries in owed.items())

    queued = await db.transaction(_work)
    if queued:
        logger.info("title store: queued %d calendar title(s) for a description", queued)
    return queued


async def listings_owed() -> int:
    """How many sparse listings are still owed a description (`queue_listed`'s
    worklist, whatever its allowance), every service's together — the calendar's
    "unenriched" count adds it to the Simkl drain's own."""
    sources = _listing_sources()
    if not sources:
        return 0
    total = 0
    for source in sources:
        total += int(await db.fetch_value(f"SELECT COUNT(*) {_owed_listings(source)}",
                                          (LISTED_GIVE_UP_AFTER,)) or 0)
    return total


# ---------------------------------------------------------------------------
# the drain
# ---------------------------------------------------------------------------

async def _look_up(settings, port, entry: Entry) -> descriptions.Answer:
    """Ask one service again about one queued row, writing nothing. A show's
    title row brings its season list with it, which is what a board's season
    rows and the season picker read."""
    season = None if entry.season == TITLE else entry.season
    answer = await port.fetch_details(settings, Media(entry.media), entry.own_id, season,
                                      fresh=True)
    seasons = None
    if season is None and entry.media == str(Media.SHOW):
        seasons = (await port.fetch_seasons(settings, entry.own_id, Media.SHOW,
                                            fresh=True)).seasons
    return descriptions.Answer(entry.source, entry.media, entry.own_id, answer, season, seasons)


# What a service's "slow down" arrives as: an HTTP 429 on the app-wide
# SourceUnavailable contract (base.py), which every transport raises it as.
RATE_LIMITED = 429


# A PASS'S ENTRIES LEAVE THE QUEUE IN ONE STATEMENT, driven through the queue's
# own key (CROSS JOIN fixes that order): correlated the other way round, the
# planner walked every queued row and the pass's entries inside each.
_DEQUEUE = """
DELETE FROM metadata_refresh WHERE rowid IN (
    SELECT q.rowid FROM json_each(?) j CROSS JOIN metadata_refresh q
    ON q.source = json_extract(j.value, '$[0]') AND q.media = json_extract(j.value, '$[1]')
   AND q.source_id = json_extract(j.value, '$[2]') AND q.season = json_extract(j.value, '$[3]'))
"""


async def _settle_entries(done: list[Entry], failed: list[Entry], now: int) -> None:
    """Take a pass's entries off the queue, and record each that failed on the
    rows it was for (descriptions.plan_failures), so a view leaves them alone for
    a while (`_backed_off`) — one turn of the writer queue for the whole pass, its
    rows found before the lock is taken."""
    if not done:
        return

    def _work(conn, marked: descriptions.Failures) -> None:
        conn.execute(_DEQUEUE, (json.dumps([list(entry) for entry in done]),))
        descriptions.mark_failures(conn, marked)

    await db.transaction(_work, prepare=lambda conn: descriptions.plan_failures(
        conn, [(entry.source, entry.media, entry.own_id,
                None if entry.season == TITLE else entry.season) for entry in failed],
        now=now))


def shortened(address: str) -> str:
    """A picture's address as a log line names it: its host and its last path
    segment — enough to tell two apart, and never a whole signed URL."""
    parts = urlsplit(str(address or ""))
    if not parts.netloc:
        return "(none)"
    return f"{parts.netloc}/…/{parts.path.rstrip('/').rsplit('/', 1)[-1]}"


def _log_refreshed(entry: Entry, reason: str, written: descriptions.Written | None) -> None:
    """One line per refreshed row. INFO for a heal, so one can be followed end to
    end by the title's key; DEBUG for the routine traffic of being viewed."""
    level = logging.INFO if reason == REASON_DEAD_PICTURE else logging.DEBUG
    what = "title" if entry.season == TITLE else f"season {entry.season}"
    if written is None:
        logger.log(level, "title store: refreshed %s %s %s (%s): the service says nothing "
                   "about it", entry.source, entry.media, entry.own_id, what)
        return
    logger.log(
        level, "title store: refreshed %s's %s row of %s (%s): picture %s -> %s; ids %s; %s",
        entry.source, what, written.key, reason, shortened(written.previous_poster),
        shortened(written.poster),
        written.ids_changed or "unchanged",
        f"re-keyed from {written.previous_key}" if written.rekeyed else "not re-keyed")


async def drain(settings, *, now: int | None = None) -> int:
    """One pass over the queue: what is waiting, the front first, as many of
    each service's rows as its minute allows (`PER_MINUTE`), each asked again —
    and every answer filed together once the pass has asked them all. Returns
    how many rows were refreshed.

    A SERVICE THIS INSTANCE CANNOT ASK leaves its rows queued — a credential
    filled in later finds them waiting. One that fails leaves its rows marked
    (`_settle_entries`) and drops the entry; the next view asks again once the
    backoff has passed.

    ASKED FIRST, THEN WRITTEN ONCE. Each answer used to be filed as it arrived and
    its entry dropped after it — two or three write transactions a title, each a
    turn in the one queue every write in the process waits in (app/db.py's
    `_writer_queue`), a viewer's own included. MEASURED on a copy of the
    development database, draining a backlog of TMDB titles at its six hundred a
    minute: 3,700 turns in 65 s of passes, 57 a second, and a filter save went
    from 0.36 s between passes to 0.52 s during one. Now a pass's answers are one
    transaction of a fixed number of statements (descriptions.store_many) and its
    dequeues and failures another, so they reach the store together once the
    pass has asked them all — and a pass a restart interrupts has written
    nothing: its rows are still queued, and the next pass asks them again.
    """
    ts = db.now() if now is None else now
    askable = dict(_askable(settings))
    if not askable:
        return 0
    queued = await db.fetch_all(
        "SELECT source, media, source_id, season, reason FROM metadata_refresh "
        "WHERE queued_at <= ? ORDER BY queued_at, rowid", (ts,))
    batches: dict[str, list[tuple[Entry, str]]] = {}
    moment = time.monotonic()
    allowed = {source: _allowance(source, moment) for source in askable}
    for row in queued:
        source = str(row["source"])
        if source not in askable:
            continue
        batch = batches.setdefault(source, [])
        if len(batch) < allowed[source]:
            batch.append((Entry(source, str(row["media"]), str(row["source_id"]),
                                int(row["season"])), str(row["reason"] or "")))
    batches = {source: batch for source, batch in batches.items() if batch}
    if not batches:
        return 0
    for source, batch in batches.items():
        _asked[source].extend([moment] * len(batch))

    # What became of each entry the pass asked about, in the order the answers
    # came back: an answer to file, a refresh that could not be made, or a fault.
    # An entry in none of them was left queued.
    answered: list[tuple[Entry, str, descriptions.Answer]] = []
    failed: list[tuple[Entry, str]] = []
    faulted: list[tuple[Entry, Exception]] = []
    # The services that told this pass to slow down (`_one`).
    slowed: set[str] = set()

    async def _one(source: str, entry: Entry, reason: str, gate: asyncio.Semaphore) -> None:
        async with gate:
            if source in slowed:
                return   # left queued, for the next pass
            try:
                found = await _look_up(settings, askable[source], entry)
            except SourceUnavailable as exc:
                if exc.status == RATE_LIMITED:
                    # THE SERVICE SAID SLOW DOWN, which is not this title's
                    # failure: no backoff is recorded against it (five would give
                    # a sparse listing up for good), its entry stays queued, and
                    # the rest of this service's batch waits for the next pass.
                    # The transport has already held the request for the service's
                    # Retry-After where it could (providers/tmdb/transport.py).
                    if source not in slowed:
                        logger.info("title store: %s asked us to slow down; its queued "
                                    "rows wait for the next pass", source)
                    slowed.add(source)
                    return
                logger.log(logging.INFO if reason == REASON_DEAD_PICTURE else logging.DEBUG,
                           "title store: %s could not refresh %s %s (%s): %s",
                           source, entry.media, entry.own_id, reason, exc)
                failed.append((entry, reason))
                return
            # Not a service's answer at all — a fault here. The entry goes, as every
            # answered one does, so it is not retried every pass; the pass logs it.
            except Exception as exc:
                faulted.append((entry, exc))
                return
            answered.append((entry, reason, found))

    for source, batch in batches.items():
        gate = asyncio.Semaphore(IN_FLIGHT[source])
        await asyncio.gather(*(_one(source, entry, reason, gate) for entry, reason in batch))

    filed: list[descriptions.Written | None | Exception] = []
    unfiled: list[Entry] = []
    if answered:
        try:
            filed = await descriptions.store_many([found for _e, _r, found in answered],
                                                  tz=ZoneInfo(settings.timezone))
        # THE PASS'S WRITE FAILED AS A WHOLE, and each answer in it with it: their
        # entries go, as a fault's does, so a broken write is not retried every
        # pass, and a view queues each again while it is still due.
        except Exception:
            logger.warning("title store: a refresh pass could not file its %d answer(s)",
                           len(answered), exc_info=True)
            unfiled = [entry for entry, _reason, _found in answered]
            answered = []

    # What this pass did for each heal's title: (entry, what became of it).
    healed: dict[str, list[tuple[Entry, str]]] = {}
    refreshed = 0
    said_nothing: list[Entry] = []
    for (entry, reason, _found), written in zip(answered, filed):
        if isinstance(written, Exception):
            faulted.append((entry, written))
            continue
        if written is None:
            # AN ANSWER THAT SAYS NOTHING IS A REFRESH THAT DID NOT HAPPEN, and is
            # marked as one, so the backoff holds it back: a title the service no
            # longer knows would otherwise be asked about again on every view — and
            # a sparse listing on every heartbeat (`queue_listed`), whose give-up
            # reads this count.
            said_nothing.append(entry)
        _log_refreshed(entry, reason, written)
        if entry in _heals:
            healed.setdefault(_heals.pop(entry)[0], []).append((
                entry, shortened(written.poster) if written is not None else "nothing"))
        refreshed += 1
    for entry, _reason in failed:
        if entry in _heals:
            healed.setdefault(_heals.pop(entry)[0], []).append((entry, "failed"))
    for entry, exc in faulted:
        logger.warning("title store: refreshing %s %s %s failed", entry.source,
                       entry.media, entry.own_id, exc_info=exc)
    await _settle_entries(
        [entry for entry, _r, _f in answered] + [entry for entry, _r in failed]
        + [entry for entry, _exc in faulted] + unfiled,
        [entry for entry, _r in failed] + said_nothing, ts)

    if refreshed:
        logger.info("title store: refreshed %d queued row(s) — %s", refreshed,
                    ", ".join(f"{source} {len(batch)}" for source, batch in batches.items()))
    for key, done in healed.items():
        waiting = sum(1 for other, _since in _heals.values() if other == key)
        logger.info(
            "title store: heal of %s %s — %s", key,
            f"continues, {waiting} row(s) still queued" if waiting else "finished",
            "; ".join(f"{entry.source} "
                      + ("title" if entry.season == TITLE else f"season {entry.season}")
                      + f": picture {outcome}" for entry, outcome in done))
    return refreshed


# ONE PASS AT A TIME, HOWEVER MANY ASK. The heartbeat's drain pass asks every
# minute (app/calendar/enrich.py's run_drain) and a confirmed dead picture asks
# the moment it is confirmed (`schedule`), so the two can meet; a second pass
# arriving while one runs is remembered and run after it, never beside it —
# the latch app/calendar/enrich.py keeps for its own drain, for the reasons it
# gives there.
_active = False
_rerun = False
_tasks: set[asyncio.Task] = set()


async def run(settings) -> int:
    """One latched `drain`, or this call folded into the one running."""
    global _active, _rerun
    if _active:
        _rerun = True
        return 0
    _active, _rerun = True, False
    try:
        refreshed = await drain(settings)
        while _rerun:
            _rerun = False
            refreshed += await drain(settings)
        return refreshed
    finally:
        _active = False


def _forget(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("title store: a refresh started behind a page failed",
                     exc_info=task.exception())


def start(work: Coroutine[Any, Any, Any]) -> None:
    """Run `work` as a task of its own, never awaited by the caller — kept
    referenced until it finishes, so the event loop cannot collect it halfway.
    With no running loop to put it on, it is dropped. The one way this module
    starts anything behind a caller, so a test replaces it once
    (tests/conftest.py's `no_background_title_refresh`)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        work.close()
        return
    task = asyncio.create_task(work)
    _tasks.add(task)
    task.add_done_callback(_forget)


def schedule(settings) -> None:
    """Start a pass now rather than at the next heartbeat — what a heal asks
    for, so the title's rows are re-read within seconds of the picture being
    confirmed dead."""
    start(run(settings))


async def backlog(*, now: int | None = None) -> int:
    """How many rows are waiting for a refresh now."""
    ts = db.now() if now is None else now
    return int(await db.fetch_value(
        "SELECT COUNT(*) FROM metadata_refresh WHERE queued_at <= ?", (ts,)) or 0)
