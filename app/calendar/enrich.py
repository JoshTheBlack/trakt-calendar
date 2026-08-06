"""Simkl calendar enrichment: the background drain that fills in the fields
Simkl's calendar CDN files never carry (genres, network, country,
certification, runtime, status, overview), and the read-time overlay that
applies what it found.

WHY THIS EXISTS. app/providers/simkl/calendar.py's Records arrive with every
one of those fields at its default and `enriched=False` — the calendar CDN
files simply do not carry them at all, verified against the live files. Doing
the lookup inline, on the fill or the read path, does not scale: a busy
window can reference several hundred distinct titles and Simkl's rate ceiling
is 10 GET/second, so a viewer loading a month would wait on it. Instead:

  FILL    stores the window exactly as normalized, unenriched — see
          app/providers/simkl/calendar.py. Nothing here runs at fill time.
  READ    `overlay_records` reads `simkl_titles` — ONE BATCHED QUERY, NO
          NETWORK CALL — and fills in whatever it already knows for the Simkl
          records this read resolved to. A title with no row yet, or one whose
          last attempt failed and is out of its backoff, is queued for the
          NEXT DRAIN. This is deliberately per-read, in-memory, and lossy
          across a restart: the queue exists to feed the drain, not to
          promise an eventual fetch, and the next viewer who reads a window
          holding that title re-queues it for free.
  DRAIN   the heartbeat calls `drain()`, which pops a bounded batch off that
          queue, fetches each through app/providers/simkl/titles.py, and
          UPSERTs the answer (or the fact that it failed) into `simkl_titles`.
          Never reads the calendar cache itself — it only ever sees ids that a
          real read already surfaced, which is what keeps a heartbeat tick
          cheap regardless of how many calendar windows the instance holds.

A TITLE Simkl DOES NOT KNOW STILL GETS A ROW, WITH AN EMPTY PAYLOAD. That is
what stops the same id being queued again on every single read after the
first failed attempt — presence of a row is the whole signal `overlay_records`
uses to decide "already attempted"; `failed_at`/`fail_count` are what decide
whether it is worth attempting again yet.

THIS MODULE NEVER MAKES A NETWORK CALL FROM overlay_records. That function is
called from app/calendar/cache.py's assemble_range, which a public share page
reaches with allow_fetch=False — the same rule that page holds everywhere else
in the calendar package applies here too, and is why enrichment can only ever
be discovered as pending, never performed, on a read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import zlib
from typing import Any

from .. import db
from ..providers.base import Media, Record, Source
from ..providers.simkl import titles as simkl_titles

logger = logging.getLogger(__name__)

# Bounded per tick so a heartbeat that finds a large backlog (a fresh install,
# or a long-stopped instance whose enrichment table emptied through the
# retention sweep below) never turns one minute of maintenance into a burst
# against Simkl's rate ceiling. At this size a full backlog drains within a
# few minutes of ordinary traffic rather than in one tick.
DRAIN_BATCH_SIZE = 20

# Catalog metadata barely moves, so a stale row is not urgent — but a row that
# is never revisited would eventually be inaccurate for a title Simkl reissues
# (new network, new certification, a status flip). Retention forces a
# recheck, the same TTL detail.py's episode lists use and for the same reason:
# long enough that this table does not become the thing generating most of
# the instance's Simkl traffic, short enough that "wrong forever" cannot
# happen. A swept row is simply "never attempted" again to `overlay_records`,
# so ordinary traffic re-queues and re-fetches it — no separate un-sweep path
# is needed.
RETENTION_SECONDS = 30 * 24 * 60 * 60

# How long a failed id is left alone before it is worth asking about again,
# scaled by how many times running it has failed — the same shape
# app/media/artwork.py's fail_count already uses, capped so a chronically
# unanswerable id is retried at most once a day rather than never again.
_BACKOFF_BASE_SECONDS = 60 * 60
_BACKOFF_MAX_SECONDS = 24 * 60 * 60

# The in-memory queue `overlay_records` feeds and `drain` consumes. A dict
# rather than a set or a list: insertion order is preserved (first surfaced,
# first fetched) and a title read by several viewers between two drains is
# only ever queued once. MODULE STATE, NOT A TABLE — it resets on restart,
# which costs nothing worse than one extra read before an id is re-queued,
# the same trade calendar/cache.py's own `_last_prewarm_at` marker makes.
_PENDING_MAX = 500
_pending: dict[tuple[int, str], None] = {}


def _enqueue(simkl_id: int, media: str) -> None:
    if len(_pending) >= _PENDING_MAX:
        # Not an error: the queue is a hint, not a promise, and a full queue
        # simply means this tick's drain has plenty to do already. The title
        # is re-offered by the very next read that resolves to it.
        return
    _pending[(simkl_id, media)] = None


def _pop_batch(limit: int) -> list[tuple[int, str]]:
    batch = list(_pending.keys())[:limit]
    for key in batch:
        _pending.pop(key, None)
    return batch


def pending_count() -> int:
    """How many titles are queued for the next drain. Read by tests; not load-
    bearing for anything in the app itself."""
    return len(_pending)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------

def _compress(fields: dict) -> bytes:
    return zlib.compress(json.dumps(fields, separators=(",", ":")).encode("utf-8"))


def _decompress(blob: bytes) -> dict:
    try:
        data = json.loads(zlib.decompress(blob).decode("utf-8"))
    except (zlib.error, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def _read_rows(keys) -> dict[tuple[int, str], dict]:
    """The stored `simkl_titles` rows for `keys` (an iterable of (simkl_id,
    media)), keyed the same way. ONE QUERY PER MEDIA KIND present in `keys` —
    at most two, since `media` is the app's own closed 'show'/'movie'
    vocabulary — never one query per id."""
    by_media: dict[str, list[int]] = {}
    for simkl_id, media in keys:
        by_media.setdefault(media, []).append(simkl_id)
    out: dict[tuple[int, str], dict] = {}
    for media, ids in by_media.items():
        placeholders = ",".join("?" for _ in ids)
        rows = await db.fetch_all(
            f"SELECT simkl_id, payload, fetched_at, failed_at, fail_count "
            f"FROM simkl_titles WHERE media = ? AND simkl_id IN ({placeholders})",
            (media, *ids),
        )
        for row in rows:
            out[(int(row["simkl_id"]), media)] = {
                "fields": _decompress(row["payload"]),
                "fetched_at": int(row["fetched_at"]),
                "failed_at": int(row["failed_at"]) if row["failed_at"] is not None else None,
                "fail_count": int(row["fail_count"]),
            }
    return out


async def _upsert_success(simkl_id: int, media: str, fields: dict, now: int) -> None:
    blob = _compress(fields)
    await db.execute(
        "INSERT INTO simkl_titles (simkl_id, media, payload, fetched_at, failed_at, fail_count) "
        "VALUES (?, ?, ?, ?, NULL, 0) "
        "ON CONFLICT(simkl_id, media) DO UPDATE SET "
        "payload = excluded.payload, fetched_at = excluded.fetched_at, "
        "failed_at = NULL, fail_count = 0",
        (simkl_id, media, blob, now),
    )


async def _upsert_failure(simkl_id: int, media: str, now: int) -> None:
    """Record an attempt that found nothing usable. The payload written on the
    FIRST attempt is the empty answer (there is nothing else to store yet); a
    later failure of a title that once succeeded deliberately leaves the old
    payload in place — a transient failure must not erase data this app
    already has a good answer for, it only says "this attempt did not
    confirm it"."""
    blob = _compress({})
    await db.execute(
        "INSERT INTO simkl_titles (simkl_id, media, payload, fetched_at, failed_at, fail_count) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(simkl_id, media) DO UPDATE SET "
        "failed_at = excluded.failed_at, fail_count = simkl_titles.fail_count + 1",
        (simkl_id, media, blob, now, now),
    )


def _backoff_elapsed(fail_count: int, failed_at: int | None, now: int) -> bool:
    if failed_at is None or fail_count <= 0:
        return True
    wait = min(_BACKOFF_BASE_SECONDS * (2 ** (fail_count - 1)), _BACKOFF_MAX_SECONDS)
    return (now - failed_at) >= wait


# ---------------------------------------------------------------------------
# read-time overlay
# ---------------------------------------------------------------------------

def _apply(record: Record, fields: dict[str, Any]) -> None:
    record.genres = list(fields.get("genres") or [])
    record.network = str(fields.get("network") or "")
    record.country = str(fields.get("country") or "")
    record.certification = str(fields.get("certification") or "")
    record.runtime = fields.get("runtime")
    record.status = str(fields.get("status") or "")
    record.overview = str(fields.get("overview") or "")
    upgrades = fields.get("ids") or {}
    if upgrades:
        # First-writer-wins over an id the calendar file already supplied,
        # matching app/calendar/cache.py's group_records — enrichment only
        # ADDS a namespace (tvdb, mal, anidb) the calendar file never carries
        # at all, it never overrides one the fill already had.
        merged = dict(record.ids)
        for namespace, value in upgrades.items():
            merged.setdefault(namespace, value)
        record.ids = merged
    record.enriched = True


async def overlay_records(records: list[Record], *, now: int | None = None) -> list[Record]:
    """Fill in whatever `simkl_titles` already knows about the Simkl records in
    `records`, mutating them in place, and queue anything unanswered for the
    next drain. Returns `records` for convenience at the call site.

    NO NETWORK CALL HAPPENS HERE, EVER — see the module docstring. A record
    this overlay cannot answer for is simply left at `enriched=False`, which
    is what lets app/calendar/filter.py exempt it rather than judge it on
    values it has not been able to look up yet.
    """
    candidates: dict[tuple[int, str], list[Record]] = {}
    for record in records:
        if record.source != Source.SIMKL:
            continue
        raw_id = (record.ids or {}).get("simkl")
        try:
            simkl_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        candidates.setdefault((simkl_id, str(record.media)), []).append(record)
    if not candidates:
        return records

    ts = db.now() if now is None else now
    rows = await _read_rows(candidates.keys())
    for key, group in candidates.items():
        row = rows.get(key)
        if row is None:
            _enqueue(*key)
            continue
        fields = row["fields"]
        if not fields:
            # A stored failure: nothing to apply, and worth retrying only once
            # its backoff has elapsed.
            if _backoff_elapsed(row["fail_count"], row["failed_at"], ts):
                _enqueue(*key)
            continue
        for record in group:
            _apply(record, fields)
    return records


# ---------------------------------------------------------------------------
# the heartbeat drain
# ---------------------------------------------------------------------------

async def _fetch_one(settings, simkl_id: int, media: str, now: int) -> bool:
    fields = await simkl_titles.fetch_title(settings, simkl_id, Media(media))
    if fields is None:
        await _upsert_failure(simkl_id, media, now)
        return False
    await _upsert_success(simkl_id, media, fields, now)
    return True


async def drain(settings, *, now: int | None = None) -> int:
    """One heartbeat's worth of enrichment: fetch detail for a bounded batch of
    titles a recent read found unenriched, and store what came back. Returns
    how many titles were newly enriched (0 when the queue was empty or
    everything in the batch failed).

    RUNS THROUGH CATALOG_POOL, WHICH ALLOWS PARALLEL REQUESTS — see
    app/providers/simkl/transport.py — so the batch is fetched concurrently
    rather than one title at a time.
    """
    batch = _pop_batch(DRAIN_BATCH_SIZE)
    if not batch:
        return 0
    ts = db.now() if now is None else now
    results = await asyncio.gather(
        *(_fetch_one(settings, simkl_id, media, ts) for simkl_id, media in batch),
        return_exceptions=True,
    )
    fetched = sum(1 for r in results if r is True)
    # INFO, NOT DEBUG, and the same reasoning as calendar/cache.py's own
    # pre-warm line: this spends the instance's Simkl budget with no viewer
    # present, and an operator should be able to see that it ran.
    logger.info("Simkl enrichment drain: fetched %d of %d queued title(s).", fetched, len(batch))
    return fetched


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

async def sweep(now: int | None = None) -> int:
    """Delete `simkl_titles` rows past the retention window, on the same
    heartbeat that sweeps api_cache. A swept row is not lost data so much as a
    forced recheck: the very next read that resolves to that title finds no
    row, queues it, and the next drain re-fetches it — see overlay_records."""
    ts = db.now() if now is None else now
    cutoff = ts - RETENTION_SECONDS
    result = await db.execute("DELETE FROM simkl_titles WHERE fetched_at <= ?", (cutoff,))
    return result.rowcount
