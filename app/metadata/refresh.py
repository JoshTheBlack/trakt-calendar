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
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Coroutine, Iterable, Mapping
from typing import Any, NamedTuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .. import db, providers
from ..providers import detail_source
from ..providers.base import Media, SourceUnavailable
from . import descriptions, holders, identity, rows

logger = logging.getLogger(__name__)

# metadata_refresh.season for a title's own row (migration 47 says why not NULL).
TITLE = -1

REASON_VIEWED = "viewed"
REASON_DEAD_PICTURE = "dead picture"
REASON_CEILING = "ceiling"

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
#   TMDB answers about forty requests a second and its client keeps twenty in
#   flight (providers/tmdb/transport.py); a refresh is two or three of them.
#   TRAKT's reads share one bucket — 150 up front, then three a second
#   (providers/trakt/transport.py's GET_PACER) — and a refresh is up to five.
#   MEASURED on a copy of the development database: at twenty a pass, a few
#   tracker reloads spent the bucket and the viewer's own sync read waited 1.3 s
#   behind the queue. Four a minute is twenty reads, a ninth of the pace, and
#   leaves the bucket to the pages.
#   SIMKL's per-title reads are its cached-by-id family, paced by its own client
#   (providers/simkl/transport.py).
PER_MINUTE = {"tmdb": 40, "trakt": 4, "simkl": 40}

# What each service has been asked in the last minute by this process, as the
# monotonic moments each refresh started (`_allowance`).
_asked: dict[str, deque[float]] = {}


def _allowance(source: str, at: float) -> int:
    """How many more of `source`'s rows may be re-read now (`PER_MINUTE`)."""
    started = _asked.setdefault(source, deque())
    while started and at - started[0] >= 60:
        started.popleft()
    return max(0, PER_MINUTE[source] - len(started))

# How many of a service's refreshes run side by side. TMDB's is its client's own
# ceiling (providers/tmdb/transport.py keeps twenty in flight, measured clear of
# any 429), so a pass may fill it and anything asked meanwhile waits its turn in
# the same pool rather than being refused. Trakt's client gates every call
# behind one semaphore anyway (app/calendar/enrich.py's drain_releases says why
# firing a batch at it only queues on that gate), so it goes one at a time.
# Both tables name every registered service — tests/metadata/test_refresh.py
# fails on one that is missing, because a service with no pace of its own must
# not inherit somebody else's.
IN_FLIGHT = {"tmdb": 20, "trakt": 1, "simkl": 4}

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
    keys = list(asked)
    for start in range(0, len(keys), _CHUNK):
        chunk = keys[start:start + _CHUNK]
        for row in conn.execute(
                f"SELECT rowid AS rid, source, media, source_id, title_key, ids_json, "
                f"season_count, stale_after, failed_at, fail_count FROM {rows.TITLES} "
                f"WHERE title_key IN ({', '.join('?' * len(chunk))})", chunk):
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
    stored: dict[tuple[str, str, str, int], Any] = {}
    by_title: dict[tuple[str, str], list[str]] = {}
    for row, source, _own_id, _seasons in seasons_of:
        by_title.setdefault((source, str(row["media"])), []).append(str(row["source_id"]))
    for (source, media), source_ids in by_title.items():
        for start in range(0, len(source_ids), _CHUNK):
            chunk = source_ids[start:start + _CHUNK]
            for season in conn.execute(
                    f"SELECT source_id, season, episode_count, air_dates_json, stale_after, "
                    f"failed_at, fail_count FROM {rows.SEASONS} WHERE source = ? "
                    f"AND media = ? AND source_id IN ({', '.join('?' * len(chunk))})",
                    (source, media, *chunk)):
                stored[(source, media, str(season["source_id"]), int(season["season"]))] = season
    for row, source, own_id, seasons in seasons_of:
        media = str(row["media"])
        for season in sorted(seasons):
            of_season = stored.get((source, media, str(row["source_id"]), season))
            if not descriptions.season_described(of_season):
                if missing and not _backed_off(of_season, now):
                    out.append((0, Entry(source, media, own_id, season)))
            elif int(of_season["stale_after"] or 0) <= now and not _backed_off(of_season, now):
                out.append((int(of_season["stale_after"] or 0),
                            Entry(source, media, own_id, season)))
    return out


# Keys or ids per IN list, under SQLite's bound-parameter ceiling.
_CHUNK = 400


def _stated_id(row, namespace: str) -> str:
    """The id a stored row says its service knows the title by, or ""."""
    try:
        ids = json.loads(row["ids_json"] or "{}")
    except (TypeError, ValueError):
        return ""
    return str(ids.get(namespace) or "").strip() if isinstance(ids, dict) else ""


_QUEUE = ("INSERT INTO metadata_refresh (source, media, source_id, season, queued_at, reason) "
          "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source, media, source_id, season) DO NOTHING")


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

    def _work(conn) -> int:
        queued = 0
        for entry in entries:
            if queued >= budget:
                break
            queued += conn.execute(_QUEUE, (*entry, ts, REASON_VIEWED)).rowcount
        return queued

    queued = await db.transaction(_work)
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

    def _work(conn) -> None:
        for entry in entries:
            conn.execute(
                "INSERT INTO metadata_refresh (source, media, source_id, season, queued_at, "
                "reason) VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(source, media, source_id, season) "
                "DO UPDATE SET queued_at = 0, reason = excluded.reason", (*entry, reason))

    await db.transaction(_work)
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

# A season row that holds a DESCRIPTION — a season list alone is re-read with
# its title's row, which renews a row holding nothing else (rows.ceiling_clock).
_DESCRIBED_SEASON = ("(s.overview != '' OR s.cast_json NOT IN ('', '[]') "
                     "OR s.air_dates_json NOT IN ('', '[]'))")


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

    def _work(conn) -> int:
        held = holders.held_keys(conn)
        due: list[tuple[int, Entry]] = []
        for source, retain in bounded.items():
            namespace = rows.ID_NAMESPACE[source]
            threshold = ts - int(retain) + CEILING_MARGIN_SECONDS
            own = f"CAST(json_extract(t.ids_json, '$.{namespace}') AS TEXT) AS own_id"
            for row in conn.execute(
                    f"SELECT t.title_key, t.media, {own}, t.fetched_at, NULL AS season "
                    f"FROM {rows.TITLES} t WHERE t.source = ? AND t.fetched_at > 0 "
                    "AND t.fetched_at <= ? "
                    "UNION ALL "
                    f"SELECT t.title_key, t.media, {own}, s.fetched_at, s.season "
                    f"FROM {rows.SEASONS} s JOIN {rows.TITLES} t ON t.source = s.source "
                    "AND t.media = s.media AND t.source_id = s.source_id "
                    f"WHERE s.source = ? AND s.season > 0 AND {_DESCRIBED_SEASON} "
                    "AND s.fetched_at > 0 AND s.fetched_at <= ?",
                    (source, threshold, source, threshold)):
                if str(row["title_key"]) not in held:
                    continue
                own_id = str(row["own_id"] or "").strip()
                if own_id:
                    season = TITLE if row["season"] is None else int(row["season"])
                    due.append((int(row["fetched_at"]),
                                Entry(source, str(row["media"]), own_id, season)))
        queued = 0
        for _clock, entry in sorted(due, key=lambda pair: pair[0]):
            if queued >= CEILING_BUDGET:
                break
            queued += conn.execute(_QUEUE, (*entry, ts, REASON_CEILING)).rowcount
        return queued

    queued = await db.transaction(_work)
    if queued:
        logger.info("title store: queued %d held row(s) nearing their service's ceiling",
                    queued)
    return queued


# ---------------------------------------------------------------------------
# the drain
# ---------------------------------------------------------------------------

async def _refresh(settings, port, entry: Entry, *, tz: ZoneInfo) -> descriptions.Written | None:
    """Ask one service again about one queued row and file the answer. A show's
    title row brings its season list with it, which is what a board's season
    rows and the season picker read."""
    season = None if entry.season == TITLE else entry.season
    answer = await port.fetch_details(settings, Media(entry.media), entry.own_id, season,
                                      fresh=True)
    seasons = None
    if season is None and entry.media == str(Media.SHOW):
        seasons = (await port.fetch_seasons(settings, entry.own_id, Media.SHOW,
                                            fresh=True)).seasons
    return await descriptions.store(entry.source, entry.media, entry.own_id, answer,
                                    asked_season=season, tz=tz, seasons=seasons)


async def _record_failure(entry: Entry, now: int) -> None:
    """A refresh that could not be made: the rows it was for say so, so a view
    leaves them alone for a while (`_backed_off`)."""
    def _work(conn) -> None:
        for row in descriptions.service_rows(conn, entry.source, entry.media, entry.own_id):
            if entry.season == TITLE:
                conn.execute(f"UPDATE {rows.TITLES} SET failed_at = ?, fail_count = "
                             "fail_count + 1 WHERE rowid = ?", (now, row["rid"]))
            else:
                conn.execute(f"UPDATE {rows.SEASONS} SET failed_at = ?, fail_count = "
                             "fail_count + 1 WHERE source = ? AND media = ? AND source_id = ? "
                             "AND season = ?", (now, entry.source, entry.media,
                                                row["source_id"], entry.season))

    await db.transaction(_work)


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
    each service's rows as its minute allows (`PER_MINUTE`), each asked again and
    filed. Returns how many rows were refreshed.

    A SERVICE THIS INSTANCE CANNOT ASK leaves its rows queued — a credential
    filled in later finds them waiting. One that fails leaves its rows marked
    (`_record_failure`) and drops the entry; the next view asks again once the
    backoff has passed.
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
    tz = ZoneInfo(settings.timezone)
    refreshed = 0

    # What this pass did for each heal's title: (entry, what became of it).
    healed: dict[str, list[tuple[Entry, str]]] = {}

    async def _one(source: str, entry: Entry, reason: str, gate: asyncio.Semaphore) -> bool:
        async with gate:
            try:
                written = await _refresh(settings, askable[source], entry, tz=tz)
            except SourceUnavailable as exc:
                logger.log(logging.INFO if reason == REASON_DEAD_PICTURE else logging.DEBUG,
                           "title store: %s could not refresh %s %s (%s): %s",
                           source, entry.media, entry.own_id, reason, exc)
                await _record_failure(entry, ts)
                if entry in _heals:
                    healed.setdefault(_heals.pop(entry)[0], []).append((entry, "failed"))
                return False
            finally:
                await db.execute(
                    "DELETE FROM metadata_refresh WHERE source = ? AND media = ? "
                    "AND source_id = ? AND season = ?", entry)
            _log_refreshed(entry, reason, written)
            if entry in _heals:
                healed.setdefault(_heals.pop(entry)[0], []).append((
                    entry, shortened(written.poster) if written is not None else "nothing"))
            return True

    for source, batch in batches.items():
        gate = asyncio.Semaphore(IN_FLIGHT[source])
        results = await asyncio.gather(*(_one(source, entry, reason, gate)
                                         for entry, reason in batch),
                                       return_exceptions=True)
        for (entry, _reason), result in zip(batch, results):
            if isinstance(result, BaseException):
                logger.warning("title store: refreshing %s %s %s failed", entry.source,
                               entry.media, entry.own_id, exc_info=result)
            elif result:
                refreshed += 1
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
