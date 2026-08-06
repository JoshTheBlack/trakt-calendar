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
          records this read resolved to. A record this overlay cannot answer
          for is simply left at `enriched=False`; it does not need to queue
          anything for the drain to eventually find it (see DRAIN below).
  DRAIN   the heartbeat calls `drain()`, which asks
          app/calendar/cache.py's `cached_calendar_groups` for every Simkl id
          ANY currently-stored calendar window names, subtracts the ones
          `simkl_titles` already has a usable answer for (or a failure still
          inside its backoff), fetches a bounded batch of what is left through
          app/providers/simkl/titles.py, and UPSERTs the answer (or the fact
          that it failed) into `simkl_titles`. A FILL also asks for a drain
          the moment it stores new records, through `run_drain`/
          `schedule_drain` below, rather than only ever waiting for the next
          heartbeat tick — see that section for the coalescing latch that
          keeps ten simultaneous fills from running ten passes.

THE DRAIN'S WORK IS DERIVED FROM THE STORED CALENDAR, NOT FROM AN IN-MEMORY
QUEUE A READ HAPPENED TO POPULATE. An earlier version of this module fed the
drain from a `_pending` dict that `overlay_records` filled and a full queue
silently dropped an id from — measured on a live instance, `simkl_titles` grew
100 -> 260 rows over several minutes while two specific titles (which had
aired, rendered, and been read many times) were never attempted even once,
because the queue was full on every read that offered them and nothing
preferred an older offer over a newer one. Deriving the owed set fresh from
`api_cache` every drain tick cannot drop an id that way: a window's Simkl ids
are knowable the moment FILL stores it, whether or not any viewer has read it
yet, and asking "what does the cache currently hold, minus what
`simkl_titles` already answered" is complete and idempotent — an id either
shows up in the difference or it does not, with no queue state to lose it in
between. It also survives a restart for free, which the queue design gave up
on deliberately and which turned out to be the wrong trade.

A TITLE Simkl DOES NOT KNOW STILL GETS A ROW, WITH AN EMPTY PAYLOAD. That is
what stops the same id being re-attempted on every single drain tick after
the first failed attempt — presence of a row with a real payload is what
`overlay_records` reads as "already attempted and answered";
`failed_at`/`fail_count` are what decide whether an empty row is worth
attempting again yet, and the same backoff rule is what keeps a failed id out
of `drain`'s owed set until it has waited long enough.

THIS MODULE NEVER MAKES A NETWORK CALL FROM overlay_records. That function is
called from app/calendar/cache.py's assemble_range, which a public share page
reaches with allow_fetch=False — the same rule that page holds everywhere else
in the calendar package applies here too, and is why enrichment can only ever
be discovered as pending, never performed, on a read.

"ALREADY ANSWERED" ALSO MEANS "UNDER THE CURRENT EXTRACTION". A ROW A NARROWER
extraction wrote — every simkl_titles row that existed before ids/type/
anime_type/trailers/etc. were added to what app/providers/simkl/titles.py's
`_extract` keeps — carries no `extract_version` key at all, and `drain` reads
that the same way it reads a version number that does not match: owed, not
done, exactly like a title never attempted. Without this an already-answered
row from the old shape would sit there for its full 30-day retention window
before the wider extraction ever touched it, which is not what "owed" is
supposed to mean. Backoff still governs FAILURES (a stored empty payload);
a stale-shaped SUCCESS is re-fetched on the very next drain tick regardless of
backoff, because nothing about it failed. `overlay_records` is unaffected —
it applies whatever fields an old row already carries in the meantime, so a
title enriched under the old shape stays enriched (just missing the newer
fields) while it waits its turn to be re-fetched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import zlib
from typing import Any

from . import cache as calendar_cache
from .. import db
from ..providers.base import Media, Record, Source
from ..providers.simkl import titles as simkl_titles

logger = logging.getLogger(__name__)

# RAISED FROM 20 TO 300, MEASURED 2026-08-06 AGAINST THE LIVE ENDPOINT, NOT
# ESTIMATED. Median latency to GET /tv/{id} is ~20ms because the endpoint is
# Cloudflare-cached (146 of 150 sampled requests answered cache HIT); at 20
# on a 60-second heartbeat the drain was spending ~0.4 SECONDS of every tick
# doing work and sitting idle the other 99.3% — 432 real titles measured
# ~22 minutes to clear at that pace and ~18 seconds even with no parallelism
# at all. 300 sits inside the measured 200-500 range Simkl's own limits
# support (see the next paragraph) and, with CATALOG_POOL's existing
# concurrency of 6 — left untouched; run-to-run variance at 20ms swamps
# whatever effect tuning it would have — clears an ordinarily-browsed
# month's worth of titles inside a single tick.
#
# THE CAVEAT THIS NUMBER DEPENDS ON, KEPT BESIDE IT SO IT TRAVELS WITH IT:
# Simkl's published rate limits allow PARALLEL / high-rate requests only on
# its Cloudflare-cached-by-id family — the trending and calendar data files,
# GET /movies/{id}, GET /tv/{id}, GET /anime/{id}, GET /tv/episodes/{id} and
# GET /anime/episodes/{id}. This drain calls /tv/{id} and NOTHING ELSE (see
# app/providers/simkl/titles.py), so it qualifies. EVERYTHING ELSE Simkl
# exposes stays capped at 10 GET/second and 1 POST/second — the tracker's
# /sync/ reads and every POST run SEQUENTIALLY on SYNC_POOL
# (app/providers/simkl/transport.py), which admits one request at a time on
# purpose. A large number here is not licence to raise a batch, a pool's
# concurrency, or anything else on one of those other paths — measured, the
# sequential benchmark for THIS endpoint alone already ran at 23.5 req/s,
# above the general 10 GET/s ceiling, and that is only legitimate because the
# path is declared cached and parallel-safe; the same rate on an
# uncached path would breach Simkl's limit without anyone touching a
# constant.
#
# TWO THINGS MAKE THE REAL WORLD SLOWER THAN THIS BENCHMARK: 20ms is the
# cache-HIT case, and a title nobody has fetched before — everything on a
# fresh instance's first drain — is likelier to MISS; and each title here
# also costs a database write, which the benchmark did not measure at all.
#
# Bounded per tick regardless of size so a heartbeat that finds a large
# backlog (a fresh install, or a long-stopped instance whose enrichment table
# emptied through the retention sweep below) never turns one minute of
# maintenance into an unbounded burst.
DRAIN_BATCH_SIZE = 300

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

def _simkl_candidates(records: list[Record]) -> dict[tuple[int, str], list[Record]]:
    """The records in `records` that `simkl_titles` could have something to say
    about, indexed by the (simkl_id, media) key that table is keyed on.

    Shared by the two overlays below so "which records is this table about" has
    one answer: a Simkl record carrying a usable numeric `simkl` id, and nothing
    else. A record from another source, or one whose id will not parse, is not a
    row this table could ever hold.
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
    return candidates


def _merge_ids(record: Record, upgrades: dict[str, Any]) -> None:
    """Add id namespaces this record does not already carry, and NEVER override
    one it does.

    First-writer-wins over an id the calendar file already supplied, matching
    app/calendar/cache.py's `group_records` — the calendar file is the thing that
    was actually published for this airing, and enrichment describes the TITLE.
    Where they disagree the calendar file is the more specific statement.
    """
    if not upgrades:
        return
    merged = dict(record.ids)
    for namespace, value in upgrades.items():
        merged.setdefault(namespace, value)
    record.ids = merged


async def overlay_match_ids(records: list[Record]) -> list[Record]:
    """Merge stored enrichment's `ids` into the Simkl records a FILL is about to
    group — and nothing else about them. Mutates in place and returns `records`.

    WHY A FILL CONSULTS THIS TABLE AT ALL, WHEN `overlay_records` BELOW ALREADY
    APPLIES IT AT READ. A group key is derived at FILL, from the ids the calendar
    FILE carries; the read-time overlay is exactly that, an overlay, and arrives
    strictly after the key that would have used it. So an id enrichment learns
    can never reach the key by itself, and re-reading a stored window never
    re-keys it. Measured on a live instance: 835 enrichment rows carrying a tmdb
    id on 747 of them made no difference at all to how many entries merged,
    because none of those ids was in play when the keys were derived. If the ids
    are to inform matching, the fill has to go and ask for them, which is what
    this function is.

    IT APPLIES ONLY THE IDS, AND THAT RESTRAINT IS THE POINT. Every other field
    enrichment holds — genres, network, country, certification, runtime, status,
    overview — stays a READ-time overlay, because baking those into a stored
    window would freeze one moment's enrichment into a row served for the whole
    TTL and would make `enriched` a lie about what the window contains. An id is
    the one thing a fill genuinely needs before it can do its own job.

    WHAT IT CANNOT DO IS HELP A TITLE NOBODY HAS ENRICHED YET. The first fill of
    an unfamiliar month finds no rows, keys on the calendar files alone, and only
    picks up the bridge when that window is next refilled — the same self-healing
    shape the rest of this module already has, one TTL wide.

    AND THE CEILING IS LOW, WHICH IS WORTH WRITING DOWN SO NOBODY MEASURES THIS
    AND CONCLUDES IT IS BROKEN. Of the Simkl-only groups on the author's live
    cache that structurally cannot merge — the ones keyed by Simkl's own id —
    enrichment held an external id for 6 of 98. The other 92 are titles Trakt
    simply does not have, which is a catalogue difference no matching can close.
    Single-digit gains are the honest expectation.
    """
    candidates = _simkl_candidates(records)
    if not candidates:
        return records
    rows = await _read_rows(candidates.keys())
    for key, group in candidates.items():
        row = rows.get(key)
        if row is None:
            continue
        upgrades = (row["fields"] or {}).get("ids") or {}
        for record in group:
            _merge_ids(record, upgrades)
    return records


def _apply(record: Record, fields: dict[str, Any]) -> None:
    record.genres = list(fields.get("genres") or [])
    record.network = str(fields.get("network") or "")
    record.country = str(fields.get("country") or "")
    record.certification = str(fields.get("certification") or "")
    record.runtime = fields.get("runtime")
    record.status = str(fields.get("status") or "")
    record.overview = str(fields.get("overview") or "")
    # Missing on a row written by the older, narrower extraction — reads as ""
    # exactly like an unenriched record, which is the honest answer until the
    # drain re-fetches it under the wider shape (see EXTRACT_VERSION below and
    # app/calendar/filter.py's prune_disguised_films, the only reader of this).
    record.anime_type = str(fields.get("anime_type") or "")
    _merge_ids(record, fields.get("ids") or {})
    record.enriched = True


async def overlay_records(records: list[Record]) -> list[Record]:
    """Fill in whatever `simkl_titles` already knows about the Simkl records in
    `records`, mutating them in place. Returns `records` for convenience at
    the call site.

    NO NETWORK CALL HAPPENS HERE, EVER — see the module docstring. A record
    this overlay cannot answer for is simply left at `enriched=False`, which
    is what lets app/calendar/filter.py exempt it rather than judge it on
    values it has not been able to look up yet. It does not need to queue
    anything for `drain` to find later, either: `drain` derives its own work
    straight from the stored calendar cache (see `_owed_titles` below), so a
    record read here and a record nobody has ever read are equally visible to
    the next drain tick.
    """
    candidates = _simkl_candidates(records)
    if not candidates:
        return records

    rows = await _read_rows(candidates.keys())
    for key, group in candidates.items():
        row = rows.get(key)
        if row is None:
            continue
        fields = row["fields"]
        if not fields:
            # A stored failure: nothing to apply. Whether it is worth
            # attempting again yet is `drain`'s question, not a read's.
            continue
        for record in group:
            _apply(record, fields)
    return records


# ---------------------------------------------------------------------------
# the heartbeat drain
# ---------------------------------------------------------------------------

def _media_values() -> tuple[str, str]:
    return (str(Media.SHOW), str(Media.MOVIE))


async def _owed_titles() -> dict[tuple[int, str], str]:
    """Every (simkl_id, media) any currently-stored calendar window names,
    mapped to that title's display name — the full set of Simkl titles this
    instance's calendar could show, independent of whether a viewer has ever
    read the window naming them. See the module docstring for why this
    replaces an in-memory queue fed by reads.

    Filtering OWED down to what is actually worth fetching (excluding a title
    already answered, and one still inside its backoff) is `drain`'s job, not
    this function's — it stays a pure "what does the cache currently name"
    question so it has exactly one reason to change.
    """
    groups = await calendar_cache.cached_calendar_groups()
    media_values = _media_values()
    owed: dict[tuple[int, str], str] = {}
    for group in groups:
        record = (group.get("by_source") or {}).get(str(Source.SIMKL))
        if not isinstance(record, dict):
            continue
        media = record.get("media")
        if media not in media_values:
            continue
        raw_id = (record.get("ids") or {}).get("simkl")
        try:
            simkl_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        key = (simkl_id, media)
        if key not in owed:
            owed[key] = str(record.get("title") or "")
    return owed


async def _fetch_one(settings, simkl_id: int, media: str, now: int) -> bool:
    fields = await simkl_titles.fetch_title(settings, simkl_id, Media(media))
    if fields is None:
        await _upsert_failure(simkl_id, media, now)
        return False
    await _upsert_success(simkl_id, media, fields, now)
    return True


async def drain(settings, *, now: int | None = None) -> int:
    """One heartbeat's worth of enrichment: derive what the stored calendar
    still owes (see `_owed_titles`), fetch detail for a bounded batch of what
    is left after already-answered and still-backed-off titles are excluded,
    and store what came back. Returns how many titles were newly enriched (0
    when nothing was owed or everything in the batch failed).

    THE BOUND IS ON WORK PER TICK, NOT ON HOW MUCH IS DERIVED. `_owed_titles`
    scans every stored calendar window every tick — measured cheap (see its
    docstring) — precisely so the set it hands back is always complete; only
    the fetch below is capped, which is what keeps a large backlog from
    turning one heartbeat into a burst against Simkl's rate ceiling. A full
    backlog drains within a few minutes of ordinary heartbeat ticks rather
    than in one.

    RUNS THROUGH CATALOG_POOL, WHICH ALLOWS PARALLEL REQUESTS — see
    app/providers/simkl/transport.py — so the batch is fetched concurrently
    rather than one title at a time.
    """
    ts = db.now() if now is None else now
    owed = await _owed_titles()
    if not owed:
        return 0
    rows = await _read_rows(owed.keys())
    batch: list[tuple[int, str, str]] = []
    for (simkl_id, media), title in owed.items():
        if len(batch) >= DRAIN_BATCH_SIZE:
            break
        row = rows.get((simkl_id, media))
        if row is not None:
            fields = row["fields"]
            current = bool(fields) and fields.get("extract_version") == simkl_titles.EXTRACT_VERSION
            if current:
                continue  # already answered, under the current extraction
            # A SUCCESS ROW WITH NO fields IS A STORED FAILURE (see
            # _upsert_failure); anything else with fields but the WRONG (or
            # no) extract_version is a row the OLDER, narrower extraction
            # wrote. That is not "answered" any more than an unattempted id
            # is — see the module docstring and titles.EXTRACT_VERSION — so
            # it is owed a re-fetch regardless of backoff, which exists to
            # slow down repeated FAILURES, not to protect a stale success
            # from being refreshed.
            if not fields and not _backoff_elapsed(row["fail_count"], row["failed_at"], ts):
                continue  # failed recently; not worth asking again yet
        batch.append((simkl_id, media, title))
    if not batch:
        return 0
    results = await asyncio.gather(
        *(_fetch_one(settings, simkl_id, media, ts) for simkl_id, media, _title in batch),
        return_exceptions=True,
    )
    fetched = 0
    for (simkl_id, media, title), result in zip(batch, results):
        if result is True:
            fetched += 1
            # DEBUG, not INFO: the summary line below is the operator-facing
            # one (the same reasoning calendar/cache.py's pre-warm line
            # carries); this is the per-title detail worth having in a debug
            # log, named rather than left as a bare id, per the author's own
            # request — an id alone is unreadable in a log.
            logger.debug("Simkl enrichment drain: enriched %r (simkl id %s, %s).",
                         title, simkl_id, media)
    # INFO, NOT DEBUG, and the same reasoning as calendar/cache.py's own
    # pre-warm line: this spends the instance's Simkl budget with no viewer
    # present, and an operator should be able to see that it ran.
    logger.info("Simkl enrichment drain: fetched %d of %d queued title(s).", fetched, len(batch))
    return fetched


# ---------------------------------------------------------------------------
# the coalescing latch — a fill can ask for a pass sooner than the heartbeat
# ---------------------------------------------------------------------------
#
# THE FIRST VIEW OF AN UNFAMILIAR MONTH USED TO WAIT ON THE HEARTBEAT: a fill
# stores unenriched records and nothing looked at them again until the next
# 60-second tick, so a card could sit without genres, network, or overview —
# and unjudged by any filter that needs those fields — for up to a minute.
# `schedule_drain` below is what a fill calls the moment it stores new
# records, so the drain that would have run on the next tick anyway typically
# runs within about a second instead.
#
# WHY A LATCH OF DEPTH ONE, NOT A QUEUE, IS THE RIGHT SHAPE HERE — and this
# has to be argued from what `drain` costs, not merely asserted. `drain` is
# IDEMPOTENT AND DERIVES ITS ENTIRE WORKLIST FRESH FROM THE STORED CALENDAR
# every time it runs (`_owed_titles`, above): it does not consume anything a
# caller handed it. Queueing N requests behind a running pass would therefore
# not do N times the work — it would do the SAME work once and then spend N-1
# more full scans of the stored calendar re-deriving "nothing is owed any
# more". That scan is measured cheap per call (`_owed_titles`'s own
# docstring), but it is also the one thing here that grows with how many
# windows an instance has accumulated, so a queue's redundancy is exactly the
# part that gets worse over the life of an instance while a depth-one latch's
# does not. At most one pass runs, at most one more is remembered to run
# after it — ten viewers opening ten unfamiliar months produce at most two
# passes, not ten.
#
# WHY THE SECOND PASS IS A RERUN FLAG RATHER THAN DROPPING THE REQUEST. A
# fill landing WHILE a pass is running names titles that pass has already
# read `_owed_titles` past — the running pass derived its worklist once, at
# the start, and has no way to notice a window stored a moment later.
# Dropping a request that arrives mid-pass would send those titles back to
# waiting for the next heartbeat tick, which is the exact delay this latch
# exists to remove. Remembering "go around once more" guarantees the very
# next pass's fresh scan sees them, without ever running two passes at once.
_drain_active = False
_drain_rerun_requested = False

# Strong references to scheduled drain tasks, held for the task's lifetime.
# A bare `asyncio.create_task(...)` whose result nobody keeps can be garbage
# collected mid-flight — nothing else in this module holds the coroutine, so
# without this set a fill-triggered drain could simply vanish before it ran.
# Each task discards itself here via its own done callback once it finishes,
# so this never accumulates: the latch above already guarantees at most one
# entry, since a second `schedule_drain` call while one task is live folds
# into the flag rather than creating another task.
_drain_tasks: set[asyncio.Task] = set()


async def run_drain(settings) -> int:
    """Run one latched pass of `drain`, or fold this call into the pass
    already running — see the module note above this function for why a
    latch of depth one, rather than a queue, is what `drain`'s own cost
    argues for.

    THE SAME LATCH SERVES EVERY CALLER, THE HEARTBEAT INCLUDED. app/main.py's
    heartbeat tick calls this directly (awaited, so a tick still does not
    return until its own pass — or the rerun it triggered — has finished);
    `schedule_drain` below calls it from a background task for a fill. A
    heartbeat tick landing while a fill-triggered pass is running therefore
    coalesces exactly like a second fill would, rather than starting a
    concurrent pass of its own — one running drain at a time, regardless of
    who asked for it.

    THE LATCH RELEASES UNDER EXCEPTION, NOT ONLY ON SUCCESS: `finally` clears
    `_drain_active` whether `drain` returned normally or raised, so a batch
    that fails cannot wedge every later caller into "rerun requested, never
    actually run" — the very next call starts a fresh pass rather than
    finding the latch permanently held. The rerun flag is also cleared the
    moment this call ACQUIRES the latch, not only after it is consumed below:
    a flag left set by a pass that raised before ever checking it must not
    cause an unrequested extra pass the next time the latch is free.

    Returns how many titles the pass(es) this call actually ran fetched — 0
    when this call only set the rerun flag and ran nothing itself.
    """
    global _drain_active, _drain_rerun_requested
    if _drain_active:
        _drain_rerun_requested = True
        return 0
    _drain_active = True
    _drain_rerun_requested = False
    try:
        fetched = await drain(settings)
        while _drain_rerun_requested:
            _drain_rerun_requested = False
            fetched = await drain(settings)
        return fetched
    finally:
        _drain_active = False


def _forget_drain_task(task: asyncio.Task) -> None:
    """The done callback every task `schedule_drain` creates carries: drop
    the strong reference now that the task no longer needs one, and LOG
    rather than swallow an exception that escaped `run_drain` — the whole
    reason this path exists is to do better than waiting for the heartbeat,
    and a drain that dies quietly here, with nothing watching, would be
    worse than that: not silent-and-safe, silent-and-stuck, since nothing
    else would notice until the next tick's own `run_drain` call happened to
    log the same failure again."""
    _drain_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Fill-triggered enrichment drain failed.", exc_info=exc)


def schedule_drain(settings) -> None:
    """Ask the drain to run soon rather than waiting for the next heartbeat
    tick. Call this when a window FILL stores new records (see
    app/calendar/cache.py's `load_window`) — never from a read: the read path
    already promises no outbound call (see the module docstring), and firing
    a drain from `overlay_records` would put that promise in the same
    function it is meant to hold for.

    FIRE AND FORGET, ON PURPOSE, AND NEVER AWAITED BY THE CALLER. The caller
    is a request that just finished fetching and storing a calendar window;
    it must not wait on enrichment to answer that request, so this schedules
    the work as a background task and returns immediately. Coalescing is
    `run_drain`'s job, not this function's — calling this while a pass is
    already running is exactly as safe as calling it while nothing is
    running, because `run_drain` folds a busy call into the rerun flag rather
    than ever executing two passes at once.

    DEGRADES TO A NO-OP RATHER THAN RAISING wherever there is no running
    event loop to schedule onto — a script, a synchronous test path, or the
    app already tearing down at shutdown. The heartbeat drain is unchanged
    and still runs every tick regardless of whether this ever fires, so the
    only cost of a skipped schedule here is the one tick this call was meant
    to save.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    task = asyncio.create_task(run_drain(settings))
    _drain_tasks.add(task)
    task.add_done_callback(_forget_drain_task)


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

async def sweep(now: int | None = None) -> int:
    """Delete `simkl_titles` rows past the retention window, on the same
    heartbeat that sweeps api_cache. A swept row is not lost data so much as a
    forced recheck: the very next drain tick that finds the title still named
    by a stored window sees no row and re-fetches it — see `_owed_titles`."""
    ts = db.now() if now is None else now
    cutoff = ts - RETENTION_SECONDS
    result = await db.execute("DELETE FROM simkl_titles WHERE fetched_at <= ?", (cutoff,))
    return result.rowcount
