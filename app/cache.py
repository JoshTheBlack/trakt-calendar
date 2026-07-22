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

# Entries past their per-row TTL are held this much longer before eviction, so a
# brief clock skew — or a window that just lapsed and is about to be re-read —
# isn't discarded the instant it expires.
TTL_GRACE_SECONDS = 6 * 60 * 60


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


async def set(key: str, value) -> None:
    """Store `value` for `key`. Written with no per-row TTL: freshness for these
    detail lookups is decided by the ttl passed to get(), so the row is aged out
    by the size cap rather than the TTL sweep. Best-effort — a write failure is
    swallowed."""
    def _work(conn: db.Connection) -> None:
        blob = _encode(value)
        conn.execute(
            "INSERT INTO api_cache (cache_key, payload, cached_at, ttl_seconds, byte_size) "
            "VALUES (?, ?, ?, NULL, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "payload = excluded.payload, cached_at = excluded.cached_at, "
            "ttl_seconds = excluded.ttl_seconds, byte_size = excluded.byte_size",
            (key, blob, db.now(), len(blob)),
        )

    try:
        await db.run(_work)
    except (db.DatabaseError, TypeError, ValueError):
        pass


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
    """
    ts = db.now() if now is None else now

    def _work(conn: db.Connection) -> int:
        deleted = conn.execute(
            "DELETE FROM api_cache WHERE ttl_seconds IS NOT NULL "
            "AND (cached_at + ttl_seconds + ?) <= ?",
            (TTL_GRACE_SECONDS, ts),
        ).rowcount
        if max_bytes is not None and max_bytes >= 0:
            total = conn.execute("SELECT COALESCE(SUM(byte_size), 0) FROM api_cache").fetchone()[0]
            if total > max_bytes:
                to_free = total - max_bytes
                freed = 0
                victims: list[str] = []
                # Walk oldest-first, collecting keys until enough bytes are freed,
                # then delete in one pass — the cursor is fully read before any
                # DELETE runs.
                for row in conn.execute(
                    "SELECT cache_key, byte_size FROM api_cache "
                    "ORDER BY cached_at ASC, cache_key ASC"
                ).fetchall():
                    if freed >= to_free:
                        break
                    victims.append(row["cache_key"])
                    freed += int(row["byte_size"])
                if victims:
                    conn.executemany(
                        "DELETE FROM api_cache WHERE cache_key = ?", [(k,) for k in victims]
                    )
                    deleted += len(victims)
        return deleted

    return await db.transaction(_work)


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
