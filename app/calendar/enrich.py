"""Simkl calendar enrichment: the background drain that fills in the fields
Simkl's calendar CDN files never carry (genres, network, country,
certification, runtime, status, overview), and the write-time application of
what it found.

WHY THIS EXISTS. app/providers/simkl/calendar.py's Records arrive with every one
of those fields at its default and `enriched=False` — the calendar CDN files
simply do not carry them at all, verified against the live files. Doing the
lookup inline, on the fill or the read path, does not scale: a busy window can
reference several hundred distinct titles and Simkl's rate ceiling is 10
GET/second, so a viewer loading a month would wait on it. Instead:

  FILL    `apply_stored_enrichment` gives each Simkl record whatever
          `simkl_titles` ALREADY knows, and the record is stored carrying it.
          One batched query, no network call. A title nobody has looked up is
          stored `enriched=False`, which is what lets app/calendar/filter.py
          exempt it rather than judge it on values nobody has fetched yet.
  DRAIN   the heartbeat calls `drain()`, which asks app/calendar/entries.py for
          every Simkl id the stored calendar names, subtracts the ones
          `simkl_titles` already answers for (or a failure still inside its
          backoff), fetches a bounded batch of what is left, and writes the
          answer to BOTH `simkl_titles` and the calendar row it belongs to. A
          FILL also asks for a drain the moment it stores new records, through
          `run_drain`/`schedule_drain` below.
  READ    does nothing. The fields are on the row.

THERE IS NO READ-TIME OVERLAY ANY MORE, and its removal is the point of the
storage change rather than a side effect. It existed because a stored window was
one compressed blob: a refill replaced it wholesale with a fresh, unenriched
payload, so anything written into it was erased and the only way a viewer saw
enrichment was to reapply it on every single read. Rows removed what that rested
on — a refill rewrites the airings but is forbidden to demote an enriched title
(entries._UPSERT_TITLE), and the drain updates the row directly — so a stored
value is never older than the last drain pass rather than as old as the window,
and `enriched` records what the row CONTAINS instead of what a reader would have
to look up to find out.

THE DRAIN'S WORK IS DERIVED FROM THE STORED CALENDAR, NOT FROM AN IN-MEMORY
QUEUE A READ HAPPENED TO POPULATE. An earlier version fed the drain from a
`_pending` dict that the read overlay filled and a full queue silently dropped an
id from — measured on a live instance, `simkl_titles` grew 100 -> 260 rows over
several minutes while two specific titles (which had aired, rendered, and been
read many times) were never attempted even once, because the queue was full on
every read that offered them. Deriving the owed set fresh from storage every tick
cannot drop an id that way: a span's Simkl ids are knowable the moment FILL
stores them, whether or not any viewer has read them, and the difference between
"what the calendar names" and "what `simkl_titles` answers" is complete and
idempotent. It also survives a restart for free.

A TITLE Simkl DOES NOT KNOW STILL GETS A ROW, WITH AN EMPTY PAYLOAD. That is what
stops the same id being re-attempted on every drain tick after the first failed
attempt — a row with a real payload is what `drain` reads as "already attempted
and answered"; `failed_at`/`fail_count` decide whether an empty row is worth
attempting again yet.

NEITHER THIS MODULE NOR THE READ PATH MAKES A NETWORK CALL FOR ENRICHMENT.
`apply_stored_enrichment` reads one table and fetches nothing, which is what lets
a public share page — which reaches the calendar with allow_fetch=False — store
and serve without ever spending the instance's Simkl budget.

"ALREADY ANSWERED" ALSO MEANS "UNDER THE CURRENT EXTRACTION". A row a narrower
extraction wrote — every simkl_titles row that existed before ids/type/
anime_type/trailers/etc. were added to what app/providers/simkl/titles.py's
`_extract` keeps — carries no `extract_version` key at all, and `drain` reads
that the same way it reads a version number that does not match: owed, not done,
exactly like a title never attempted. Without this an already-answered row from
the old shape would sit there for its full 30-day retention before the wider
extraction ever touched it. Backoff still governs FAILURES (a stored empty
payload); a stale-shaped SUCCESS is re-fetched on the next drain tick regardless
of backoff, because nothing about it failed. `apply_stored_enrichment` is NOT
gated on the version — it applies whatever fields an old row already carries, so
a title enriched under the old shape stays enriched (just missing the newer
fields) while it waits its turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import zlib
from typing import Any

from . import cache as calendar_cache
from . import entries
from .. import db
from .. import perftrace
from ..providers.base import Media, Record, Source, SourceUnavailable
from ..providers.simkl import titles as simkl_titles
from ..providers.simkl import transport as simkl_transport
from ..providers.trakt import detail as trakt_detail
from ..providers.trakt import releases as trakt_releases

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
# AND PRODUCTION CORRECTED THE PARAGRAPH ABOVE, so read it with this attached.
# "Declared parallel-safe" turned out not to mean "no rate applies": the first
# fresh deployment to run this batch against a full calendar and an empty
# simkl_titles was answered 412 — an instance-wide refusal — within the first
# burst, taking Simkl's calendar enrichment, the detail modals, and signing in
# with Simkl down for fifteen minutes at a time. The benchmark that produced 300
# was run on a SETTLED instance, where the drain trickles and never approaches
# its own batch size; nothing about it was wrong, it simply could not observe the
# case it was sizing for. The batch stays 300 because the bound that matters is
# work per tick; what changed is that the requests are now PACED as they leave —
# see CATALOG_MIN_INTERVAL in app/providers/simkl/transport.py, which is where a
# rate belongs, since it is the instance's budget rather than this caller's.
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
# happen. A swept row is simply "never attempted" again to the drain,
# so ordinary traffic re-queues and re-fetches it — no separate un-sweep path
# is needed.
# How many Trakt films one tick may look up. Smaller than the Simkl batch beside
# it because these go out ONE AT A TIME — see drain_releases for why — but not as
# small as caution alone would make it, because the arithmetic is knowable.
#
# MEASURED, 2026-08-11, twelve sequential live calls through this app's own
# transport with the cache read skipped: 57ms fastest, 64ms median, 455ms
# slowest, 127ms mean — about eight calls a second. So a full batch of fifty
# costs roughly SIX SECONDS of one of the four outbound slots, inside a
# sixty-second heartbeat, and the backlog one instance actually had (86 films
# across every window it holds) clears in two ticks rather than nine.
#
# AGAINST WHAT BUDGET: Trakt documents 500 GET requests per five minutes. Fifty
# a tick on a sixty-second heartbeat is 250 across that window, so a batch that
# stayed full would be HALF the budget — worth knowing, and still not the
# binding constraint, because a full tick is a backlog and a backlog is
# temporary: the set owed is what the calendar NAMES and not what it holds. Once
# one is gone this is a handful of films a day and most ticks fetch nothing.
#
# WHY NOT LARGER STILL: the ceiling is not the rate limit, it is that this shares
# a connection pool with work somebody is waiting on — a tracker refresh, a
# calendar window. A pass holds one slot of four for as long as it runs, so the
# number worth minimising is SECONDS PER PASS rather than calls per day, and six
# is comfortably inside the tick that triggered it.
RELEASE_DRAIN_BATCH_SIZE = 50

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


# HOW BIG A STORED FAILURE IS, so SQL can recognise one without decompressing
# every row. `_upsert_failure` writes the compressed empty object and nothing
# else, so this length IS the marker — derived from the function rather than
# written as a number, because a change to the compression would otherwise make
# the constant quietly wrong and the query quietly stop matching anything.
_EMPTY_PAYLOAD_MAX_BYTES = len(_compress({}))


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
    """Record what a lookup found, in BOTH places, and they are not duplicates.

    `simkl_titles` keeps the RAW ANSWER — the whole payload, under the extraction
    version that produced it, with the failure backoff beside it. It is what
    makes a re-fetch decidable: a row written by a narrower extraction is owed
    again, and only the payload can say which extraction wrote it.

    `calendar_titles` takes the PROJECTION of that answer onto the stored title,
    which is what the read path serves. Writing it here is what retired the
    read-time overlay: the fields are on the row a viewer's month already reads,
    so nothing has to be joined back in per request, and `enriched` becomes a
    statement about what the row CONTAINS rather than about what a reader would
    have to look up to find out.

    The second write is a projection of the first and can be rebuilt from it, so
    this is one fact stored once and materialized once, not two truths to keep
    in step.
    """
    blob = _compress(fields)
    await db.execute(
        "INSERT INTO simkl_titles (simkl_id, media, payload, fetched_at, failed_at, fail_count) "
        "VALUES (?, ?, ?, ?, NULL, 0) "
        "ON CONFLICT(simkl_id, media) DO UPDATE SET "
        "payload = excluded.payload, fetched_at = excluded.fetched_at, "
        "failed_at = NULL, fail_count = 0",
        (simkl_id, media, blob, now),
    )
    await entries.apply_enrichment(str(Source.SIMKL), simkl_id, media, fields, now)


async def _upsert_failure(simkl_id: int, media: str, now: int) -> int:
    """Record an attempt that found nothing usable, and answer how many times
    this title has now failed in a row.

    The payload written on the FIRST attempt is the empty answer (there is
    nothing else to store yet); a later failure of a title that once succeeded
    deliberately leaves the old payload in place — a transient failure must not
    erase data this app already has a good answer for, it only says "this attempt
    did not confirm it".

    `fetched_at` IS DELIBERATELY NOT TOUCHED ON CONFLICT, and _GIVE_UP_AFTER
    depends on that: it is what lets a row the drain has given up on still age
    out of the table on the clock it was first written on, and so be asked about
    again from scratch rather than never again.
    """
    blob = _compress({})
    row = await db.fetch_one(
        "INSERT INTO simkl_titles (simkl_id, media, payload, fetched_at, failed_at, fail_count) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(simkl_id, media) DO UPDATE SET "
        "failed_at = excluded.failed_at, fail_count = simkl_titles.fail_count + 1 "
        "RETURNING fail_count",
        (simkl_id, media, blob, now, now),
    )
    return int(row["fail_count"]) if row else 1


# CONSECUTIVE FAILURES BEFORE THE DRAIN STOPS ASKING ABOUT A TITLE. Simkl's
# calendar files name titles Simkl's own API cannot answer for — measured on a
# real instance, six fan films and shorts sat at fail_count 27, each having been
# asked about once a day for the better part of a month.
#
# FIVE BECAUSE THE BACKOFF CAPS AT SIX. _backoff_elapsed doubles from an hour and
# reaches _BACKOFF_MAX_SECONDS at the sixth failure, so five is the point where
# waiting longer stops buying anything: roughly thirty-one hours of attempts,
# which comfortably outlasts an outage but not a title that does not exist.
#
# NOT app/media/artwork.py's MAX_FAIL_COUNT, WHICH IS THREE, and the difference
# is deliberate rather than drift. A poster is retried when somebody asks for it,
# so three attempts can span months; this is retried on a doubling clock whether
# anyone is looking or not, so the same number would give up in seven hours.
#
# WHAT MAKES THIS A PAUSE RATHER THAN A HOLE — and it is the assumption the whole
# ceiling rests on: `sweep` deletes a simkl_titles row 30 days after its
# `fetched_at`, and `_upsert_failure` deliberately does NOT touch `fetched_at` on
# conflict. So a given-up row still ages out on the clock it was first written
# on, and the next drain tick that finds the title still named by a stored window
# sees no row at all and asks again from scratch. If a later change starts
# refreshing `fetched_at` on failure, this stops being a pause and becomes
# permanent — that is the one edit that would break it.
_GIVE_UP_AFTER = 5


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

    WHY THIS IS SEPARATE FROM `apply_stored_enrichment`, WHICH READS THE SAME
    TABLE. A group key is derived from the ids the calendar FILE carries, and it
    is derived BEFORE the records are stored — so an id that arrives with the
    rest of the answer arrives strictly after the key that would have used it. So an id enrichment learns
    can never reach the key by itself, and re-reading a stored window never
    re-keys it. Measured on a live instance: 835 enrichment rows carrying a tmdb
    id on 747 of them made no difference at all to how many entries merged,
    because none of those ids was in play when the keys were derived. If the ids
    are to inform matching, the fill has to go and ask for them, which is what
    this function is.

    IT APPLIES ONLY THE IDS, AND THAT RESTRAINT IS STILL THE POINT — but the
    reason has changed and the old one must not be left standing. It used to be
    that every other field had to stay a read-time overlay; they are stored now.
    What is left is a question of ORDER: an id is the one thing needed BEFORE the
    match runs, and this is the only moment early enough to supply it. The rest
    of the answer is applied by `apply_stored_enrichment` on the way to storage,
    which is late enough not to need saying twice.

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


async def enrich_now(settings, records: list[Record], *, now: int) -> list[Record]:
    """Fetch and apply enrichment for these records IMMEDIATELY, rather than
    leaving them to the drain.

    THE DRAIN IS RIGHT FOR A FILL AND WRONG FOR A SEARCH. A month's fill names
    hundreds of titles at once against a ten-requests-a-second ceiling, so
    nothing on that path may look a title up inline. A search that has just been
    asked about ONE title is the opposite case: the reader is waiting, it is one
    lookup, and handing them a card with no genres and no certification — which
    every filter then declines to act on — is worse than the moment it costs.

    IT IS THE DRAIN'S OWN MACHINERY, not a second copy. `_fetch_one` asks Simkl
    and writes the answer into `simkl_titles`; `apply_stored_enrichment` reads
    that back onto the records. So a title learned this way is learned for the
    WHOLE INSTANCE — every other window naming it is enriched too, and the drain
    finds one less thing owed rather than the same work queued twice.

    ONLY SIMKL, because only Simkl's calendar rows arrive incomplete. Trakt's
    calendar carries every field it will ever have, so a Trakt record is already
    finished and asking again would spend a request to learn nothing. A film's
    release types are a different lookup on a different schedule and stay the
    release drain's.

    A FAILURE COSTS THE FIELDS AND NOT THE ROW. The record is returned either
    way, `enriched` still False, which is exactly the state a fill leaves and
    which the read path already draws — see filter.py on why an unenriched
    record is shown rather than filtered out.
    """
    owed = {(int(r.ids["simkl"]), str(r.media)) for r in records
            if r.source is Source.SIMKL and not r.enriched and r.ids.get("simkl")}
    for simkl_id, media in owed:
        try:
            await _fetch_one(settings, simkl_id, media, now)
        # Deliberately broad: see this function's own last paragraph. Whatever
        # went wrong, the caller still has a drawable record.
        except Exception:
            logger.warning("immediate enrichment failed for simkl id %s (%s)",
                           simkl_id, media, exc_info=True)
    return await apply_stored_enrichment(records)


async def apply_stored_enrichment(records: list[Record]) -> list[Record]:
    """Give the Simkl records a fill is about to STORE whatever `simkl_titles`
    already knows about them. Mutates in place and returns `records`.

    THIS IS THE READ-TIME OVERLAY, MOVED TO WRITE TIME, which is the whole point
    of storing records instead of a blob. The old objection was exact and applied
    to the old shape: a value baked into a stored WINDOW would be frozen for that
    window's whole TTL, because a refill replaced the blob wholesale with a
    fresh, unenriched payload. So the only way a viewer ever saw enrichment was
    to reapply it on every single read.

    Rows removed what that rested on. A refill rewrites the airings but is
    forbidden to demote an enriched title (entries._UPSERT_TITLE), and the drain
    writes the row directly, so a stored value is never older than the last drain
    pass rather than as old as the window.

    IT RUNS AT STORAGE RATHER THAN AT FETCH so that every path which stores
    records gets it — the fetch is only one of them, and a caller that assembles
    records itself would otherwise write rows that read as unenriched while the
    answer sat in the next table along. `overlay_match_ids` stays separate and
    stays ids-only: it has to run before the group keys are derived, which is a
    different job at a different moment.
    """
    candidates = _simkl_candidates(records)
    if not candidates:
        return records
    rows = await _read_rows(candidates.keys())
    for key, group in candidates.items():
        fields = (rows.get(key) or {}).get("fields")
        if not fields:
            continue  # never looked up, or a stored failure: leave it unenriched
        for record in group:
            _apply(record, fields)
    return records


def _apply(record: Record, fields: dict[str, Any]) -> None:
    """Put what a lookup found onto one record.

    IT READS THE PAYLOAD THROUGH `entries.enrichment_values` AND NOT ITSELF. The
    other writer — the drain updating a stored row directly — goes through the
    same function, and when each had its own reading they drifted: one looked for
    `ratings` where the payload says `rating`, and 225 titles on a live instance
    lost their score depending only on which path reached them. What is left here
    is the RECORD-shaped half of the write; what a field means lives in one
    place.
    """
    value = entries.enrichment_values(fields)
    record.genres = list(value["genres"])
    record.network = value["network"]
    record.country = value["country"]
    record.certification = value["certification"]
    record.runtime = value["runtime"]
    record.status = value["status"]
    record.overview = value["overview"]
    # THE THREE THE CALENDAR FILES NEVER CARRY AND THE CARD ALREADY DRAWS. Simkl's
    # calendar CDN entries have no language, no year and no rating at all, so a
    # Simkl card showed none of them — the fields were there and nothing filled
    # them in. Each stays at its own default when the payload has no answer.
    record.language = value["language"]
    record.year = value["year"]
    record.rating = value["rating"]
    record.imdb_rating = value["imdb_rating"]
    # Missing on a row written by the older, narrower extraction — reads as ""
    # exactly like an unenriched record, which is the honest answer until the
    # drain re-fetches it under the wider shape (app/calendar/filter.py's
    # prune_disguised_films is the only reader).
    record.anime_type = value["anime_type"]
    # {country: [release type]} for a film, empty for everything else. An empty
    # map means "this record cannot answer" rather than "released nowhere" —
    # see keep_release in app/calendar/filter.py.
    record.release_types_by_country = dict(value["release_types_by_country"])
    _merge_ids(record, value["ids"])
    record.enriched = True


# THERE IS NO `overlay_records` ANY MORE, AND THE REASON IS WORTH KEEPING.
#
# It read `simkl_titles` on every calendar read and painted genres, network,
# country and certification onto that read's Simkl records, because the stored
# window COULD NOT HOLD THEM: a fill replaced the whole compressed blob with a
# fresh, unenriched payload, so anything written into it was erased on the next
# refill. The docstring on `overlay_match_ids` still states that objection, and
# it was accurate about the design it described -- a value baked into a window
# would be frozen for the window's whole TTL, and `enriched` would be a lie about
# what the window contained.
#
# BOTH HALVES ARE ANSWERED BY THE DRAIN BECOMING A WRITER OF THE ROW. It updates
# `calendar_titles` when it learns something, so a stored value is never older
# than the last drain pass rather than as old as the window; and a refill is
# forbidden to demote an enriched field (entries._UPSERT_TITLE), so the fields
# survive the thing that used to erase them. `enriched` now records what the row
# CONTAINS instead of what a reader would have to look up to find out, which
# makes it more accurate, not less.
#
# WHAT REPLACED IT IS NOT ONLY THE WRITE. A title whose answer was stored before
# its current row existed is answered in `simkl_titles` and blank on the row --
# the state every one of an instance's stored answers is in the moment the
# calendar moves to rows. `drain` gives those rows their answer without fetching
# anything; see the rebuild branch there.


# ---------------------------------------------------------------------------
# the heartbeat drain
# ---------------------------------------------------------------------------

def _media_values() -> tuple[str, str]:
    return (str(Media.SHOW), str(Media.MOVIE))


async def _forget_unusable(rows: list[tuple[int, str]]) -> None:
    """Record that a given-up title has no answer, replacing one nothing can use.

    THE ROW IS NOT DELETED, because the failure count and the timestamp on it are
    what stop this being asked again and what let it age back in later. Only the
    unusable payload goes, which turns the row into exactly the shape a title
    that never answered has — see `_upsert_failure`.
    """
    empty = _compress({})
    await db.executemany(
        "UPDATE simkl_titles SET payload = ? WHERE simkl_id = ? AND media = ?",
        [(empty, int(simkl_id), str(media)) for simkl_id, media in rows])
    logger.info("Simkl enrichment: %d given-up title(s) held an answer from an "
                "older extraction that nothing can use; recorded as unanswered "
                "so they stop being counted as outstanding.", len(rows))


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
    return await entries.stored_titles(str(Source.SIMKL), "simkl", _media_values())


async def _fetch_one(settings, simkl_id: int, media: str, now: int) -> bool:
    fields = await simkl_titles.fetch_title(settings, simkl_id, Media(media))
    if fields is None:
        # AN INSTANCE-WIDE REFUSAL IS NOT A FACT ABOUT THIS TITLE, and recording
        # it as one is how a single 412 poisoned an entire table. `fetch_title`
        # answers None for every failure it meets — that is deliberate, since a
        # missing title and an unreachable service are indistinguishable from a
        # status code there — but the breaker knows which of those just happened,
        # and a blocked call never reached Simkl to have an opinion about this id.
        # Written down because the shape was observed: a fresh deployment ended
        # up with 372 of its 388 rows marked failed, every one of them a title
        # Simkl would have answered for, each carrying a backoff it had not
        # earned.
        if simkl_transport.blocked_seconds_remaining() > 0:
            return False
        failures = await _upsert_failure(simkl_id, media, now)
        # THE COUNT, WHILE IT IS STILL COUNTING. A drain that reports "fetched 0
        # of 10" every tick says nothing about whether it is getting anywhere,
        # and the ceiling that ends it is invisible until it is reached — which
        # is how ten titles came to be asked about several hundred times each
        # without anybody being able to see it happening from the log.
        if failures < _GIVE_UP_AFTER:
            logger.info("Simkl enrichment: no answer for simkl id %s (%s); "
                        "%d more attempt%s before giving up on it.",
                        simkl_id, media, _GIVE_UP_AFTER - failures,
                        "" if _GIVE_UP_AFTER - failures == 1 else "s")
        if failures == _GIVE_UP_AFTER:
            # ONCE, AT THE TRANSITION, rather than on every tick that skips it:
            # a title being given up on is an event worth reading, and a line
            # repeated hourly for a month is not. INFO because the page stops
            # counting this title from here on, and the log is then the only
            # place it is visible at all.
            logger.info("Simkl enrichment: giving up on simkl id %s (%s) after %d "
                        "failed lookups; it will be asked about again when its "
                        "stored row ages out.", simkl_id, media, failures)
        return False
    await _upsert_success(simkl_id, media, fields, now)
    return True


@perftrace.job("title enrichment")
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

    AND IT ASKS WHETHER IT MAY CALL AT ALL FIRST, exactly as `drain_releases`
    does below. Simkl's calendar needs no credential, so an instance can have a
    month full of Simkl titles — and therefore a full queue here — while holding
    no client id at all. Without this gate that instance fires a whole batch of
    lookups carrying an empty id, Simkl answers 412 to every one of them, and
    the transport's breaker then refuses EVERY Simkl call for the next 900
    seconds: the drain, the detail modals, and signing in or linking a Simkl
    account, none of which had anything wrong with them. A missing credential
    must degrade to "this cannot be enriched", never to a quarter-hour outage of
    a service the instance is otherwise configured for.
    """
    if not settings.simkl_catalogue_configured:
        return 0
    # AND IT ASKS THE BREAKER BEFORE IT DERIVES ANY WORK. Every call in the pass
    # would be refused locally anyway; asking once here means a blocked instance
    # spends no database scan per heartbeat either, and — the part that was
    # actually costing something — cannot write a batch of failure rows against
    # titles that were never asked about. See `_fetch_one` for the other half.
    if simkl_transport.blocked_seconds_remaining() > 0:
        return 0
    ts = db.now() if now is None else now
    owed = await _owed_titles()
    if not owed:
        return 0
    rows = await _read_rows(owed.keys())
    # WHICH STORED TITLES HAVE AN ANSWER BUT HAVE NOT BEEN GIVEN IT. A calendar
    # row starts unenriched and only a write marks it otherwise, so a title whose
    # lookup landed a moment after its row was written — or whose answer was
    # stored by a version of this app that did not yet project onto the calendar
    # — is answered in `simkl_titles` and blank on the row a viewer reads.
    #
    # THE SKIP BELOW IS WHY THIS CANNOT BE LEFT TO SORT ITSELF OUT. A title the
    # per-title table already answers is never fetched again, so nothing would
    # ever revisit it; the row would stay blank for as long as it existed. It was
    # briefly argued that applying enrichment at FILL made this unreachable, and
    # that was wrong: the fill covers rows it CREATES, not rows that already
    # exist and are waiting. Five films on one real August calendar sat unenriched
    # with complete stored answers beside them.
    unprojected = await entries.unenriched(str(Source.SIMKL), "simkl")
    rebuild: list[tuple[int, str, dict]] = []
    stale: list[tuple[int, str]] = []
    batch: list[tuple[int, str, str]] = []
    for (simkl_id, media), title in owed.items():
        if len(batch) >= DRAIN_BATCH_SIZE:
            break
        row = rows.get((simkl_id, media))
        if row is not None:
            fields = row["fields"]
            current = bool(fields) and fields.get("extract_version") == simkl_titles.EXTRACT_VERSION
            if current:
                # Already answered under the current extraction: NOTHING TO
                # FETCH, but possibly something to write. Bounded like the fetch
                # batch, so a large backlog is many small ticks rather than one
                # long one.
                if ((simkl_id, media) in unprojected
                        and len(rebuild) < DRAIN_BATCH_SIZE):
                    rebuild.append((simkl_id, media, fields))
                continue
            # A SUCCESS ROW WITH NO fields IS A STORED FAILURE (see
            # _upsert_failure); anything else with fields but the WRONG (or
            # no) extract_version is a row the OLDER, narrower extraction
            # wrote. That is not "answered" any more than an unattempted id
            # is — see the module docstring and titles.EXTRACT_VERSION — so
            # it is owed a re-fetch regardless of backoff, which exists to
            # slow down repeated FAILURES, not to protect a stale success
            # from being refreshed.
            if row["fail_count"] >= _GIVE_UP_AFTER and fields:
                # GIVEN UP ON, AND HOLDING AN ANSWER NOBODY CAN USE. A row whose
                # extraction is the wrong version is already treated as
                # unanswered everywhere else — that is why it was queued at all —
                # but the readout counts a non-empty payload as an answer owed to
                # the calendar row, and the rebuild pass will never hand this one
                # over. So the two disagreed and the box could not reach zero.
                #
                # SAYING SO OUTRIGHT IS THE HONEST RECORD: the lookup is finished
                # with, and what is stored cannot answer for the title, so the
                # row is marked as having no answer. It still ages out and is
                # asked about again from scratch, exactly as any other given-up
                # title is.
                stale.append((simkl_id, media))
                continue
            if row["fail_count"] >= _GIVE_UP_AFTER:
                # Asked about enough times to conclude Simkl has no answer. Not
                # forever: the row ages out of `simkl_titles` and the title is
                # asked about again from scratch — see _GIVE_UP_AFTER.
                #
                # WHATEVER THE ROW HOLDS, AND THAT IS THE CORRECTION. This used
                # to give up only on a row with NO stored answer, so a title
                # holding a STALE one — written by an older extraction and
                # therefore owed a re-fetch — was exempt from the ceiling and
                # asked about on every single pass. Measured on a live instance:
                # ten titles at 171 to 759 failures each, because Simkl answers
                # `[]` for those ids now and no number of attempts will change
                # that. The failures are what the ceiling is counting; what the
                # row happens to hold does not make them less conclusive.
                continue
            if not fields and not _backoff_elapsed(row["fail_count"], row["failed_at"], ts):
                continue  # failed recently; not worth asking again yet
        batch.append((simkl_id, media, title))
    if stale:
        await _forget_unusable(stale)
    if rebuild:
        # BEFORE THE FETCH AND WITHOUT ONE. These are answers this instance has
        # already paid for; handing them to the rows that lack them is a local
        # write, and doing it first means a pass with nothing to fetch still
        # makes progress.
        written = await entries.apply_enrichment_many(str(Source.SIMKL), rebuild, ts)
        logger.info("Simkl enrichment: gave %d stored answer(s) to calendar rows "
                    "that did not have them; no lookup was needed.", written)
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
# the other service's half: where a film is RELEASED, according to Trakt
# ---------------------------------------------------------------------------
# WHY IT IS IN THIS MODULE AND NOT ITS OWN. This file is the calendar's
# enrichment layer — "what does a catalogue know about a record that its
# calendar payload did not carry" — and that question now has two services to
# ask. The alternative was a second module with its own copy of the same four
# shapes (a batched read, a success/failure upsert pair, a backoff, a bounded
# drain), which is how two halves of one idea come to disagree about how long a
# failure backs off for.
#
# WHAT IS DELIBERATELY NOT SHARED: the STORES. simkl_titles is keyed on a Simkl
# id and holds a Simkl payload; trakt_releases is keyed on a Trakt id and holds
# one field. A film both services list has a row in each, and neither table has
# to know the other exists — see migration 27 for why that beat widening one
# table to hold either service's answer.
#
# AND THE OVERLAY STAYS PER SOURCE. A Trakt record is filled in from Trakt's
# answer and a Simkl record from Simkl's; neither service's catalogue is ever
# allowed to speak for the other's record, which would make a card's provenance
# a lie. It is the same rule `_simkl_candidates` follows, applied to the other
# side.


async def _read_release_rows(trakt_ids) -> dict[int, dict]:
    """The stored `trakt_releases` rows for `trakt_ids`, keyed by id. ONE query,
    never one per id — the same batching `_read_rows` does above and for the
    same reason: this runs on every movie-calendar read."""
    ids = [int(i) for i in trakt_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT trakt_id, payload, fetched_at, failed_at, fail_count "
        f"FROM trakt_releases WHERE trakt_id IN ({placeholders})",
        tuple(ids),
    )
    return {
        int(row["trakt_id"]): {
            "fields": _decompress(row["payload"]),
            "fetched_at": int(row["fetched_at"]),
            "failed_at": int(row["failed_at"]) if row["failed_at"] is not None else None,
            "fail_count": int(row["fail_count"]),
        }
        for row in rows
    }


async def _upsert_release_success(trakt_id: int, releases: dict, now: int) -> None:
    """Store one film's release map. AN EMPTY MAP IS A REAL ANSWER — a film Trakt
    knows and has announced no releases for — and is stored as one, which is what
    stops it being asked about again on every tick. It is told apart from a
    stored FAILURE by `failed_at` being NULL, exactly as on the Simkl side."""
    await db.execute(
        "INSERT INTO trakt_releases (trakt_id, payload, fetched_at, failed_at, fail_count) "
        "VALUES (?, ?, ?, NULL, 0) "
        "ON CONFLICT(trakt_id) DO UPDATE SET "
        "payload = excluded.payload, fetched_at = excluded.fetched_at, "
        "failed_at = NULL, fail_count = 0",
        (trakt_id, _compress({"release_types_by_country": dict(releases)}), now),
    )


async def _upsert_release_failure(trakt_id: int, now: int) -> None:
    """Record an attempt that found nothing usable, leaving any previous answer
    in place — a transient failure says "this attempt did not confirm it", not
    "forget what you knew"."""
    await db.execute(
        "INSERT INTO trakt_releases (trakt_id, payload, fetched_at, failed_at, fail_count) "
        "VALUES (?, ?, ?, ?, 1) "
        "ON CONFLICT(trakt_id) DO UPDATE SET "
        "failed_at = excluded.failed_at, fail_count = trakt_releases.fail_count + 1",
        (trakt_id, _compress({}), now, now),
    )


def _trakt_film_candidates(records: list[Record]) -> dict[int, list[Record]]:
    """The records `trakt_releases` could have something to say about, indexed by
    trakt id: a TRAKT record, for a FILM, carrying a usable numeric id.

    MOVIES ONLY, unlike the Simkl overlay beside it, because a release schedule
    is a thing only films have — Trakt's endpoint is /movies/{id}/releases and
    there is no episode equivalent. Asking it about a show would be a call that
    could only ever 404.
    """
    candidates: dict[int, list[Record]] = {}
    for record in records:
        if record.source != Source.TRAKT or record.media != Media.MOVIE:
            continue
        try:
            trakt_id = int((record.ids or {}).get("trakt"))
        except (TypeError, ValueError):
            continue
        candidates.setdefault(trakt_id, []).append(record)
    return candidates


async def overlay_releases(records: list[Record]) -> list[Record]:
    """Fill in what `trakt_releases` already knows about the Trakt films in
    `records`, mutating them in place.

    NO NETWORK CALL HAPPENS HERE, EVER — the same promise `apply_stored_enrichment`
    makes. A film with no stored answer is left with an empty map, which
    app/calendar/filter.py reads as "this record cannot answer" and keeps, so a
    film waiting on its first lookup is never dropped by a filter.
    """
    candidates = _trakt_film_candidates(records)
    if not candidates:
        return records
    rows = await _read_release_rows(candidates.keys())
    for trakt_id, group in candidates.items():
        row = rows.get(trakt_id)
        if row is None or not row["fields"]:
            continue
        releases = row["fields"].get("release_types_by_country")
        if not isinstance(releases, dict):
            continue
        for record in group:
            record.release_types_by_country = dict(releases)
    return records


async def _owed_films() -> dict[int, str]:
    """Every Trakt FILM any currently-stored calendar window names, mapped to its
    title. The same "what does the cache currently name" question `_owed_titles`
    asks, on the other service's records, and it leaves the same filtering —
    already answered, still backing off — to the drain.

    IT IS A MUCH SMALLER SET THAN THE SIMKL SIDE, which is what makes this
    affordable: measured on a live instance, one August held 1693 film groups of
    which about 25 came from Trakt. This costs tens of calls a month, not
    thousands.
    """
    films = await entries.stored_titles(str(Source.TRAKT), "trakt", (str(Media.MOVIE),))
    return {trakt_id: title for (trakt_id, _media), title in films.items()}


async def _fetch_one_release(settings, trakt_id: int, now: int) -> bool:
    releases = await trakt_releases.fetch_releases(settings, trakt_id)
    if releases is None:
        await _upsert_release_failure(trakt_id, now)
        return False
    await _upsert_release_success(trakt_id, releases, now)
    return True


@perftrace.job("film releases")
async def drain_releases(settings, *, now: int | None = None) -> int:
    """One heartbeat's worth of Trakt release lookups, bounded exactly as the
    Simkl drain above is bounded and for the same reason.

    SEQUENTIALLY, WHICH IS THE ONE REAL DIFFERENCE FROM THE SIMKL DRAIN. That one
    fans its batch out concurrently because its pool allows parallel requests;
    Trakt's transport gates every call behind one semaphore sized under its
    connection pool (app/providers/trakt/transport.py's _SEND_CONCURRENCY), so
    firing a batch at it would queue on that gate rather than go faster, while
    making a 429 storm harder to read. The batch is small and nobody is waiting
    on it.
    """
    if not settings.trakt_catalogue_configured:
        return 0
    ts = db.now() if now is None else now
    owed = await _owed_films()
    if not owed:
        return 0
    rows = await _read_release_rows(owed.keys())
    batch: list[tuple[int, str]] = []
    for trakt_id, title in owed.items():
        if len(batch) >= RELEASE_DRAIN_BATCH_SIZE:
            break
        row = rows.get(trakt_id)
        if row is not None:
            if row["failed_at"] is None:
                continue  # answered, even if the answer was "no releases announced"
            if not _backoff_elapsed(row["fail_count"], row["failed_at"], ts):
                continue  # failed recently; not worth asking again yet
        batch.append((trakt_id, title))
    if not batch:
        return 0
    fetched = 0
    for trakt_id, title in batch:
        try:
            if await _fetch_one_release(settings, trakt_id, ts):
                fetched += 1
                logger.debug("Trakt release drain: read %r (trakt id %s).", title, trakt_id)
        except Exception:
            # One film's lookup failing must not end the pass — the next tick
            # asks again, and the row it wrote records the attempt.
            logger.debug("Trakt release lookup failed for %s", trakt_id, exc_info=True)
    logger.info("Trakt release drain: read %d of %d queued film(s).", fetched, len(batch))
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


# How many REQUESTS one tick may spend on season lookups — not how many seasons
# it may deal with; see EPISODE_DRAIN_LOCAL_LIMIT below, which is the other half
# of that sentence. Seasons rather than episodes because that is the shape of the
# call: Trakt answers a whole season's episode list in one request. Small because
# each of those is bigger than a title lookup and nobody is waiting on it. The
# Simkl batch beside this one is 300; the difference is that this goes through
# Trakt's shared connection pool and its 500-GET-per-5-minutes budget, and a
# calendar month names far fewer distinct SEASONS than it does titles.
EPISODE_DRAIN_BATCH_SIZE = 25

# How many seasons one tick may WORK THROUGH, as opposed to spend requests on.
#
# THE TWO WERE ONE NUMBER AND THAT WAS THE BUG. The cap exists to pace this
# instance against Trakt's rate limit, but it was applied to every owed season
# whether or not that season cost a request — and the response cache answers a
# great many of them for nothing. Measured on a real instance with 397 seasons
# owed, 362 of them (91%) were already in the response cache: a queue that needed
# 35 requests was being paced as though it needed 397, and took a quarter of an
# hour to do a few seconds of outbound work.
#
# SIZED ON MEASURED LOCAL COST, not guessed. A cache-answered season is one
# `cache.get` — a worker-thread round trip plus a zlib decompress — measured at
# 0.33ms each over 400 real cached season responses, so this bound is worth about
# 130ms of a heartbeat tick and none of it on the event loop. It exists at all
# because "free" is not "unbounded": an instance that has just stored a year of
# calendar could otherwise hand one tick tens of thousands of rows.
EPISODE_DRAIN_LOCAL_LIMIT = 400


@perftrace.job("episode lookup")
async def drain_episodes(settings, *, now: int | None = None) -> int:
    """One heartbeat's worth of per-episode lookups — level 2 of the stored
    calendar.

    WHAT THIS FILLS IN THAT NOTHING ELSE CAN. A calendar feed names an episode
    and, at best, titles it. The facts that genuinely vary per episode — the
    overview, the runtime, the rating — have never been stored by this app at
    all: the modal shows a season's worth of them today by stamping the SHOW's
    runtime and rating onto every one, which is wrong on any show with a
    double-length finale and wrong about every episode's rating.

    ONE REQUEST PER SEASON, NOT PER EPISODE, which is the whole reason this is
    affordable — see providers/trakt/detail.py's fetch_season_episodes, and note
    the `extended=full` it depends on: `extended=episodes` alone returns episode
    objects with no `first_aired` at all.

    SEQUENTIALLY, and for the same reason drain_releases is: Trakt's transport
    gates every call behind one semaphore sized under its connection pool, so
    firing a batch would queue on that gate rather than go faster while making a
    429 storm harder to read.

    IT DOES NOT STOP WHEN EVERY SEASON HAS BEEN ANSWERED ONCE. A season falls due
    again on a clock keyed to when its episodes AIRED — see
    entries.episode_stale_after — because the titles and overviews this fills in
    are routinely corrected in the days after broadcast, not only published
    before it. Never-answered seasons still take the batch first.

    THE PACING IS ON REQUESTS, NOT ON SEASONS, and those are very different
    quantities: the response cache answers a large share of any queue for
    nothing. Each season is asked for free first (`only_if_cached`), and only a
    miss is charged against EPISODE_DRAIN_BATCH_SIZE — so a tick clears
    everything already held and spends its budget on what actually needs the
    network. The free ask is not free of ALL cost, which is what
    EPISODE_DRAIN_LOCAL_LIMIT bounds.
    """
    if not settings.trakt_catalogue_configured:
        return 0
    ts = db.now() if now is None else now
    owed = await entries.owed_episodes(str(Source.TRAKT), "trakt",
                                       EPISODE_DRAIN_LOCAL_LIMIT, ts)
    if not owed:
        return 0
    written = 0
    requests_spent = 0
    from_cache = 0
    for trakt_id, media, season in owed:
        if media != str(Media.SHOW):
            continue
        try:
            # FREE FIRST. None here means "not without a request" — distinct
            # from [], which is a season Trakt genuinely has nothing for and is
            # a real answer worth storing.
            episodes = await trakt_detail.fetch_season_episodes(
                settings, trakt_id, season, only_if_cached=True)
            if episodes is None:
                if requests_spent >= EPISODE_DRAIN_BATCH_SIZE:
                    # Budget gone. Not `break`: the seasons after this one may
                    # still be answerable for free, and stopping here would put
                    # the old behaviour back for everything behind the first
                    # cache miss in the queue.
                    continue
                requests_spent += 1
                episodes = await trakt_detail.fetch_season_episodes(
                    settings, trakt_id, season)
            else:
                from_cache += 1
        except SourceUnavailable as exc:
            # One season's failure is not the batch's: the rest are independent
            # lookups and this one is owed again next tick.
            logger.debug("episode lookup failed for trakt id %s season %s: %s",
                         trakt_id, season, exc)
            continue
        written += await entries.store_episodes(
            str(Source.TRAKT), trakt_id, media, season, episodes, ts)
    if written:
        # THE SPLIT IS THE POINT OF THE LINE NOW. "Filled 121 across 25" said
        # nothing about what it cost, and the cost is the thing being paced:
        # a tick that answered two hundred seasons out of the cache and spent
        # three requests is a healthy tick, and it used to be indistinguishable
        # from one that spent two hundred. `len(owed)` would be wrong here — it
        # is the LOCAL limit's worth of candidates, most of which may not have
        # been shows at all.
        logger.info("calendar episode drain: filled %d episode(s) across %d season(s) "
                    "— %d from cache, %d request(s) spent.",
                    written, from_cache + requests_spent, from_cache, requests_spent)
    return written


async def backlog(*, now: int | None = None) -> tuple[int, int]:
    """(titles awaiting enrichment, seasons awaiting an episode lookup).

    WHAT THIS IS FOR: a drain reports only what it just DID, so "filled 121
    episodes across 25 seasons" reads identically whether there are 26 seasons
    left or twenty thousand. Without a number that counts DOWN there is no way
    to tell a drain working through a backlog from one looping on work it has
    already finished — and this calendar has shipped that second bug twice, both
    times found by a person watching the log and guessing.

    INSTANCE-WIDE, NOT THE VIEWER'S MONTH, because settling is instance-wide. A
    count scoped to what is on screen would reach zero while the drain still had
    hours to run: the reassuring answer rather than the true one.

    IT LIVES HERE RATHER THAN IN entries.py BECAUSE THIS MODULE IS WHERE "which
    source each drain covers" is decided. Level 2 is Trakt-only — it needs a
    per-season episode list, which is the one thing Simkl's public files do not
    carry — and stating that fact in the storage layer as well would be a second
    place to change when it stops being true.

    A TITLE THE DRAIN HAS GIVEN UP ON IS NOT COUNTED, which is the whole reason
    the title half of this is a join rather than entries.py's plain COUNT. Six
    fan films Simkl's API cannot answer for would otherwise hold the number at
    six for ever, and a readout that never reaches zero cannot say "settled" —
    which is the one thing it was added to say. Giving up is logged where it
    happens, so the titles are not lost, only uncounted.
    """
    ts = db.now() if now is None else now
    titles = await db.fetch_value(
        "SELECT COUNT(*) FROM calendar_titles t "
        "LEFT JOIN simkl_titles s "
        "  ON s.simkl_id = json_extract(t.ids_json, '$.simkl') AND s.media = t.media "
        "WHERE t.enriched = 0 "
        # An empty payload is a stored failure (see _upsert_failure); a row WITH
        # a payload is an answer this title has not been given yet, and no number
        # of later failures makes that stop being owed.
        "  AND NOT (s.fail_count >= ? AND (s.payload IS NULL OR length(s.payload) <= ?))",
        (_GIVE_UP_AFTER, _EMPTY_PAYLOAD_MAX_BYTES))
    return (int(titles or 0),
            await entries.owed_season_count(str(Source.TRAKT), "trakt", ts))


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
        # THE OTHER SERVICE'S PASS RIDES THE SAME LATCH rather than carrying one
        # of its own. Both derive their work from the stored calendar cache and
        # both are bounded per pass, so what the latch protects — one drain at a
        # time, however many callers ask — is exactly as true of the pair as of
        # either. A second latch would also let a fill start a Trakt pass while a
        # Simkl one was running, which is the concurrency this exists to refuse.
        #
        # AFTER, NOT BEFORE: the Simkl half is what a fill is usually waiting on
        # (a card with no genres is visible; a film's release map is only read by
        # a filter), so it goes first and this follows on the same tick.
        # ITS COUNT IS NOT ADDED TO THE RETURN, which is the Simkl drain's own
        # number and is what the latch's callers and its tests are written
        # against. This one reports itself in its own log line.
        try:
            await drain_releases(settings)
        except Exception:
            # Never let the other service's pass fail this one's answer — the
            # heartbeat asks again in a minute and nothing is waiting on it.
            logger.error("Trakt release drain failed.", exc_info=True)
        try:
            # LEVEL 2, LAST, because it is the least urgent of the three: a card
            # with no genres looks broken, a film with no release map is filtered
            # wrongly, and an episode with no overview simply shows less in a
            # modal nobody has opened yet.
            await drain_episodes(settings)
        except Exception:
            logger.error("Calendar episode drain failed.", exc_info=True)
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
    a drain from a read would put that promise in the same
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
