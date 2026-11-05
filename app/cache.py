"""Generic TTL blob cache backed by the shared `api_cache` table.

Detail lookups (a show's cast, an episode list) are one Trakt call each, so the
raw JSON is cached and re-served on repeat views. This module used to write one
small file per lookup under data/cache/; the bytes now live in a single SQLite
table shared with the calendar window cache, so the app has one TTL-blob-cache
mechanism rather than two.

get(key, ttl) / set(key, value) are ASYNC and go through app/db.py's helpers like
every other database access in the app. They were briefly synchronous — reading
and zlib-decompressing a ~200 KB blob on the event loop thread — which is exactly
what the "every db call goes off-thread" rule exists to prevent. Both call sites
are inside `async def` already, so awaiting them costs nothing.

Every failure is swallowed: a cache is best-effort and must never take a request
down with it.
"""
from __future__ import annotations

import json
import logging
import shutil
import zlib
from typing import NamedTuple

from . import db
from .config import DATA_DIR

logger = logging.getLogger(__name__)

# The pre-SQLite cache wrote one JSON file per lookup here. Removed once at
# startup after the schema is up to date; kept named so the cleanup, and any
# future archaeology, has something to point at.
LEGACY_CACHE_DIR = DATA_DIR / "cache"

# zlib's default level: a good ratio on the highly-repetitive Trakt JSON without
# the CPU cost of level 9. The calendar window cache uses the same level.
COMPRESS_LEVEL = 6

# Entries past their per-row TTL are held this much longer before being DELETED.
#
# IT IS SIZED BY THE PUBLIC SHARE LINK, not by clock skew, and that is why it is
# ninety days rather than the few hours it used to be. A share page serves
# whatever is cached and never fetches (app/calendar/share_routes.py reads with
# `allow_fetch=False`, so an unauthenticated visitor can never spend the
# instance's rate limit) — which means that once a month's windows are swept, a
# link somebody handed out renders an empty calendar with nothing on the page to
# say why. The row therefore has to outlive the month it describes plus a margin
# for somebody opening the link late, and a calendar month's windows are the
# cheapest rows in the table.
#
# WHAT THIS IS NOT: a reservation. Grace only delays the first pass of `sweep`.
# The second pass evicts least-recently-stored rows whenever the total exceeds
# `Settings.api_cache_max_bytes`, and it does not consult this at all — so under
# real pressure the cap still wins and a share link can still go empty. The
# honest claim is that this makes a shared month survive in practice, not that
# it guarantees it. Measured on a live instance while this was written, against a
# 1 GB cap: 22.9 MB total — 12.9 MB of Simkl CDN bodies over 45 rows, 9.7 MB of
# Trakt responses over 8610 rows, and 0.3 MB of calendar windows over 16 rows. So
# the headroom is very large and holding the windows longer is close to free.
#
# IT DOES NOT MEAN STALE DATA ON A NORMAL READ. Nothing serves an expired row as
# fresh: `get` applies its own cutoff and misses, and
# app/calendar/cache.py's `load_window` compares `cached_at` against the TTL and
# refetches. The only readers a surviving row reaches are the deliberate
# no-fetch ones — `get_stale` and `load_window(allow_fetch=False)` — which is
# exactly the path this is for.
TTL_GRACE_SECONDS = 90 * 24 * 60 * 60

# HOW LONG A CATALOGUE ANSWER IS WORTH KEEPING AT ALL, as opposed to how fresh it
# has to be to serve a request — see `set`, where that distinction is the whole
# point. Six months, which is far past every read-time ttl in the app and past
# the stale reads a public share card makes (it draws whatever is cached and
# never fetches), so nothing live is ever reclaimed. What it reclaims is a key
# nothing asks for any more, which previously stayed for ever.
#
# THE SWEEP ADDS ITS GRACE ON TOP, so a row is actually reclaimed at this plus
# TTL_GRACE_SECONDS — nine months in total. That is not a second policy: the
# grace is one number the sweep applies to every TTL it honours, and stating this
# one net of it would mean the constant and the behaviour disagreed.
RETAIN_SECONDS = 180 * 24 * 60 * 60


def _encode(value) -> bytes:
    return zlib.compress(json.dumps(value, separators=(",", ":")).encode("utf-8"), COMPRESS_LEVEL)


def _decode(blob) -> object:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


async def get(key: str, ttl_seconds: int):
    """Return the cached value for `key` if it was stored within `ttl_seconds`,
    else None. ttl<=0 disables caching (an explicit "always miss").

    The decompress happens on the worker thread alongside the read rather than
    back on the event loop — the payloads are hundreds of kilobytes, so the
    unpacking is the more expensive half of this call.
    """
    if ttl_seconds <= 0:
        return None
    cutoff = db.now() - ttl_seconds

    def _work(conn: db.Connection):
        row = conn.execute(
            "SELECT payload, cached_at FROM api_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None or int(row["cached_at"]) < cutoff:
            return None
        return _decode(row["payload"])

    try:
        return await db.run(_work)
    except (db.DatabaseError, zlib.error, ValueError):
        # A locked or corrupt database, or a payload that no longer decodes,
        # must read as a miss rather than failing the request behind it.
        return None


async def get_stale(key: str):
    """Return the cached value for `key` regardless of age, or None if there's
    no row at all. For cache_only callers (the public share page): they can
    never fall through to a live fetch, so a merely-expired row is still far
    better than blank fields — unlike `get`, this never applies a TTL cutoff.
    """

    def _work(conn: db.Connection):
        row = conn.execute(
            "SELECT payload FROM api_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return _decode(row["payload"])

    try:
        return await db.run(_work)
    except (db.DatabaseError, zlib.error, ValueError):
        return None


async def fresh_keys(keys, ttl_seconds: int) -> frozenset[str]:
    """Which of `keys` hold a value stored within `ttl_seconds` — `get`'s
    question for many keys in one read, without decoding any payload.

    FOR A CALLER CHOOSING WORK BY WHAT IT REMEMBERS DOING: a background pass
    weighing a few thousand cards a minute against "asked this week" markers
    would otherwise make a few thousand reads to learn which few are due. A
    locked database reads as nothing remembered, like `get`'s miss — the caller
    then asks again what it asked before, which costs a request and loses
    nothing."""
    wanted = list(dict.fromkeys(str(key) for key in keys))
    if not wanted or ttl_seconds <= 0:
        return frozenset()
    cutoff = db.now() - ttl_seconds

    def _work(conn: db.Connection) -> frozenset[str]:
        # A list, not a set: this module's own `set` is the cache write.
        fresh: list[str] = []
        for at in range(0, len(wanted), 500):
            chunk = wanted[at:at + 500]
            fresh.extend(row[0] for row in conn.execute(
                f"SELECT cache_key FROM api_cache WHERE cached_at >= ? "
                f"AND cache_key IN ({','.join('?' * len(chunk))})", (cutoff, *chunk)))
        return frozenset(fresh)

    try:
        return await db.run(_work)
    except db.DatabaseError:
        return frozenset()


async def set(key: str, value, retain_seconds: int | None = RETAIN_SECONDS) -> None:
    """Store `value` for `key`, to be reclaimed once nothing has re-fetched it for
    `retain_seconds`. Best-effort — a write failure is swallowed.

    THE ROW'S TTL IS NOT ITS FRESHNESS. How stale an answer may be before a
    caller re-fetches is decided by the ttl passed to `get`, per read, and is
    measured in minutes or hours; this one decides when a row is worth keeping AT
    ALL, and is measured in months. The two never met before because rows were
    written with no TTL at all, which put them entirely beyond the age sweep.

    WHAT THAT COST: a key nothing asks for any more was kept for ever. Change the
    parameters a call sends — a search that starts asking for more results, a
    lookup that gains a field — and the old address is orphaned, unreachable and
    unreclaimed, because the only eviction was a size cap this instance never
    reaches. There are such rows in the field already, from a search-by-id path
    that no longer exists.

    A ROW STILL IN USE NEVER AGES OUT, which is what makes this safe: every
    refresh re-inserts and bumps `cached_at`, so the clock only ever runs on rows
    nothing has asked for. What expires is precisely the orphans, plus titles
    genuinely untouched for half a year — and those simply get fetched again.

    IN TURN, LIKE EVERY WRITE (db.transaction): a write made around the writer
    queue commits while a calendar store is deciding its rows in its turn, and
    the store must then decide them again under the lock. MEASURED on a harness
    copy while a month's refills and the background lookups ran together: six
    stores re-decided, holding the lock 0.17–0.70 s each to do it. The payload
    is compressed before the lock is taken (`prepare`).
    """
    def _write(conn: db.Connection, blob: bytes) -> None:
        conn.execute(
            "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, byte_size) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "payload = excluded.payload, cached_at = excluded.cached_at, "
            "ttl_seconds = excluded.ttl_seconds, byte_size = excluded.byte_size",
            (key, blob, db.now(), retain_seconds, len(blob)),
        )

    try:
        await db.transaction(_write, prepare=lambda conn: _encode(value))
    except (db.DatabaseError, TypeError, ValueError):
        pass


async def delete(key: str) -> None:
    """Remove `key` now. Best-effort, like `set`.

    FOR A VALUE THAT MUST NOT OUTLIVE THE MOMENT IT DESCRIBES. Everything else in
    this table is a copy of somebody else's answer, and leaving it for the sweep
    costs nothing but bytes. A short-lived handshake is different: once it has
    ended its row describes nothing, and the sweep's grace period would keep it
    for months. In turn, like `set`.
    """
    try:
        await db.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
    except db.DatabaseError:
        pass


# ---------------------------------------------------------------------------
# conditional-GET validators for the files a source publishes
# ---------------------------------------------------------------------------
#
# IN THE KERNEL, NOT IN THE CALENDAR, and the reason is a layering one. A
# provider has to READ a validator before it sends the request and WRITE one
# after — so wherever these live, `providers/` calls them. Putting them in
# app/calendar/ made a provider import a feature package, which is exactly
# backwards and is what tests/kernel/test_layering.py refused. Asking "has this
# file changed since I last read it" is a caching question and this module is
# where the app's other caching answers live.
#
# THE ROW HOLDS NO BODY, which is the whole design. Its predecessor kept the
# decoded file beside its ETag -- 15.15 MB of JSON on the machine this was
# written for -- because a 304 carries no body and the WINDOW that needed it
# could be evicted separately. Once a fill stores rows, a 304 needs no body at
# all: "unchanged" means the rows already derived from this file are still
# correct. What is left is about a kilobyte of validators.



async def source_file_validator(url: str) -> tuple[str, str]:
    """(etag, last_modified) for one source file, or two empty strings.

    THE ROW HOLDS NO BODY, WHICH IS THE POINT. Its predecessor kept the whole
    decoded file beside its ETag -- 15.15 MB of JSON on the machine this was
    written for, 2.57 MB compressed -- because a 304 carries no body and the
    WINDOW that needed it might have been evicted separately. Under rows a 304
    needs no body at all: "unchanged" means the airings already derived from this
    file are still correct, so the answer is to do nothing. What is left is about
    a kilobyte of validators.
    """
    row = await db.fetch_one(
        "SELECT etag, last_modified FROM calendar_source_files WHERE url = ?", (url,))
    if row is None:
        return "", ""
    return str(row["etag"] or ""), str(row["last_modified"] or "")


async def record_source_file(url: str, source: str, month: str, *, etag: str,
                             last_modified: str, entries: int, now: int,
                             changed: bool) -> None:
    """Note that a source file was read, and whether its content moved.

    `fetched_at` AND `changed_at` ARE DIFFERENT QUESTIONS. The first advances on
    every look, including a 304; the second only when a 200 actually brought
    something new. One number cannot answer both "is this due another look" and
    "how stale can the rows derived from it be", and conflating them is how a
    file that is checked hourly and changes monthly comes to look perpetually
    fresh or perpetually stale depending on which meaning won.
    """
    await db.execute(
        "INSERT INTO calendar_source_files "
        "(url, source, month, etag, last_modified, fetched_at, changed_at, entries) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET "
        "etag = excluded.etag, last_modified = excluded.last_modified, "
        "fetched_at = excluded.fetched_at, entries = excluded.entries, "
        "changed_at = CASE WHEN ? THEN excluded.changed_at "
        "                  ELSE calendar_source_files.changed_at END",
        (url, source, month, etag, last_modified, now, now if changed else 0,
         entries, 1 if changed else 0))


# HOW MUCH ONE TURN OF THE SWEEP DELETES. A deleted row's payload pages are
# walked to be freed, so a turn's cost follows the bytes it removes as well as the
# rows; both are capped, and a single row larger than the byte cap is a turn of
# its own. MEASURED on a copy of dev (88,295 rows, 204 MB): see `sweep`.
SWEEP_TURN_ROWS = 500
SWEEP_TURN_BYTES = 4 * 1024 * 1024

# A row past its own ttl and the grace — the first pass. Rows written with no ttl
# never match: they leave only by the size cap.
_DUE = "ttl_seconds IS NOT NULL AND cached_at + ttl_seconds + :grace <= :now"


class _Turn(NamedTuple):
    """One turn of a sweep: the JSON array of [rowid, cached_at] it deletes, and
    how many rows and bytes that is."""
    victims: str
    rows: int
    bytes: int


def _plan_sweep(conn: db.Connection, now: int, max_bytes: int | None) -> list[_Turn]:
    """Every row a sweep at `now` deletes, oldest first and cut into turns —
    read and decided and NOT written.

    THE FIRST READ ANSWERS IN ONE ROW, and almost always with nothing: whether
    any row is past its ttl, and the table's total against the cap. Only when
    something is due are the rows themselves read: every row past its ttl, then,
    of the rest, the least-recently stored until the total fits — the running
    total a window over the order the eviction walks, so a row goes when the rows
    before it have not yet freed enough.

    THE TURNS ARE SPELLED HERE, on the worker thread, so what crosses back to the
    event loop is one string per turn rather than a Python object per row: halving
    a copy of dev's table plans 40,538 rows."""
    params = {"grace": TTL_GRACE_SECONDS, "now": now}
    (total, expired, expired_bytes), = conn.execute(
        f"SELECT COALESCE(SUM(byte_size), 0), COALESCE(SUM({_DUE}), 0), "
        f"COALESCE(SUM(CASE WHEN {_DUE} THEN byte_size END), 0) FROM api_cache",
        params).fetchall()
    excess = (total - expired_bytes - max_bytes
              if max_bytes is not None and max_bytes >= 0 else 0)
    if not expired and excess <= 0:
        return []
    reads = []
    if expired:
        reads.append((f"SELECT rowid, cached_at, byte_size FROM api_cache WHERE {_DUE} "
                      f"ORDER BY cached_at, cache_key", params))
    if excess > 0:
        reads.append((
            f"SELECT rowid, cached_at, byte_size FROM ("
            f"  SELECT rowid, cached_at, byte_size, cache_key, SUM(byte_size) OVER ("
            f"    ORDER BY cached_at ASC, cache_key ASC "
            f"    ROWS UNBOUNDED PRECEDING) - byte_size AS freed_before "
            f"  FROM api_cache WHERE NOT ({_DUE})) "
            f"WHERE freed_before < :excess ORDER BY cached_at, cache_key",
            {**params, "excess": excess}))
    turns: list[_Turn] = []
    victims: list[str] = []
    size = 0
    for sql, bound in reads:
        cursor = conn.cursor()
        cursor.row_factory = None
        for rowid, cached_at, byte_size in cursor.execute(sql, bound):
            # A turn is full at its row cap, or when this row would take it past
            # its byte cap — and a row larger than the cap is a turn of its own.
            if victims and (len(victims) >= SWEEP_TURN_ROWS
                            or size + byte_size > SWEEP_TURN_BYTES):
                turns.append(_Turn(f"[{','.join(victims)}]", len(victims), size))
                victims, size = [], 0
            victims.append(f"[{rowid},{cached_at}]")
            size += byte_size
    if victims:
        turns.append(_Turn(f"[{','.join(victims)}]", len(victims), size))
    return turns


# One turn's delete: the planned rows that are still what the plan saw. A row
# stored again since (`set` keeps the rowid and moves `cached_at`) is recent now,
# and stays.
_DELETE_TURN = (
    "DELETE FROM api_cache WHERE rowid IN ("
    "  SELECT a.rowid FROM json_each(?) v CROSS JOIN api_cache a"
    "  WHERE a.rowid = json_extract(v.value, '$[0]')"
    "    AND a.cached_at = json_extract(v.value, '$[1]'))")


async def sweep(now: int | None = None, max_bytes: int | None = None) -> int:
    """Evict from api_cache and return how many rows were deleted.

    Two passes: first every entry past its per-row TTL plus the grace period
    (rows written with no TTL are exempt — they only leave via the size cap);
    then, if the summed byte_size still exceeds max_bytes, the least-recently
    stored entries until it fits again.

    "Least-recently stored" is oldest cached_at, which is the recency signal we
    have: an entry is re-inserted, bumping cached_at, every time it is refreshed
    from Trakt, so cached_at tracks the last time it was actually (re)fetched.
    There is deliberately no separate last-access column — maintaining one would
    mean a write on every cache read.

    DECIDED BEFORE THE LOCK, NOTHING DUE TAKES NO LOCK, AND WHAT IS DUE GOES IN
    BOUNDED TURNS. The heartbeat runs this every minute and nearly every minute
    nothing is due, yet it used to scan the whole table twice inside its write
    transaction — a column after the payload is read by walking the payload's
    pages — and delete everything due in that same hold. MEASURED on a copy of dev
    (88,295 rows, 204 MB, nothing due): the lock held 0.56–0.68 s a minute alone
    and 0.91–1.02 s beside a month read, 3.54 s on dev at a start; halving the
    table, one 2.05 s hold; 25,575 rows past their ttl, 2.15 s — and every other
    writer waited the whole of it. Now the plan is a read on a worker thread
    (`_plan_sweep`), which waits for no writer and holds nobody up; and each turn
    of at most SWEEP_TURN_ROWS rows / SWEEP_TURN_BYTES bytes is a transaction of
    its own, one statement, taking its place in the writer queue behind whatever
    arrived meanwhile. MEASURED on the same copy: nothing due takes no lock at
    all; halving the table is 85 turns of a median 13 ms and a p95 of 0.1 s — a
    turn whose commit crosses SQLite's automatic WAL checkpoint pays for the
    checkpoint, as any writer's does (0.07–0.14 s, and 0.3 s for the first one
    on a freshly copied file) — and a one-row write beside it waits a median
    2–10 ms.

    A TURN DELETES ONLY WHAT THE PLAN SAW: a row stored again since the plan was
    made is recent now and stays (`_DELETE_TURN`). Rows written after the plan
    are not counted against the cap until the next sweep, a minute later — the
    cap is a budget the sweep restores, never a wall a write is refused at.
    """
    ts = db.now() if now is None else now
    turns = await db.run(lambda conn: _plan_sweep(conn, ts, max_bytes))
    if not turns:
        return 0
    deleted = 0
    for turn in turns:
        def _sweep_turn(conn: db.Connection, victims: str = turn.victims) -> int:
            return conn.execute(_DELETE_TURN, (victims,)).rowcount

        deleted += await db.transaction(_sweep_turn)
    logger.info("response cache: removed %d row(s) of %d planned (%.1f MB) in %d turn(s).",
                deleted, sum(turn.rows for turn in turns),
                sum(turn.bytes for turn in turns) / 1e6, len(turns))
    return deleted


def discard_legacy_dir() -> None:
    """Remove the old data/cache/*.json directory once its bytes live in
    api_cache. Called at startup after the schema is current; idempotent (the
    directory is normally already gone on later runs)."""
    if not LEGACY_CACHE_DIR.exists():
        return
    try:
        shutil.rmtree(LEGACY_CACHE_DIR)
        logger.info("Removed the legacy file cache at %s (now in api_cache)", LEGACY_CACHE_DIR)
    except OSError:
        logger.warning("Could not remove the legacy cache directory %s", LEGACY_CACHE_DIR)
