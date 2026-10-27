"""The calendar's storage: its airings, its coverage, and the reads that join them
to the title store.

WHAT THIS REPLACES. Until now a fill compressed every source's records for an
(endpoint, seven days) into one blob in `api_cache` and the read path inflated
four or five of them per month. Two questions that mattered could not be asked of
that shape at all: "which titles does the stored calendar name" needed EVERY
window inflated, and "what airs on this day" needed a whole month decompressed to
answer for one day.

WHAT A TITLE IS, IS NOT THE CALENDAR'S. What each service says about a title and
its episodes lives in the title store (app/metadata/rows.py), which every feature
reads; the calendar stores what it LISTS — an airing, on an endpoint, at a time —
and which spans each service was asked about. A fill hands its records to
app/metadata's `store_records`, which files each under its title's key, and the
airings are written under that same key in the same transaction.

NOTHING HERE IS PER-VIEWER, and that is the invariant the whole design serves.
Every source's rows are stored whole; which one a viewer sees, and whether their
filters hide it, is decided at READ against their own preferences.
`app/calendar/resolve.py` owns the read-time decision; this module owns the rows.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from .. import db
from .. import metadata
from ..metadata import identity as title_identity
from ..metadata import rows as title_rows
from ..metadata import typed_search
from ..providers.base import Media, Record, Source, epoch_moment

logger = logging.getLogger(__name__)

# Written onto every airing whose source did not state a coordinate. -1 RATHER
# THAN NULL because SQLite permits NULLs inside a non-INTEGER primary key, so a
# nullable season would let one airing insert twice over. A third of premiere
# entries state no season or no episode number -- overwhelmingly Simkl anime,
# where an absolute episode number is simply how the entry is spelled -- so
# "unstated" is the ordinary case and needs a value the key can compare.
UNSTATED = -1

# The store's tables, as the calendar's own joins name them — spelled where the
# store defines them, never again here.
_TITLES = title_rows.TITLES
_EPISODES = title_rows.EPISODES
_IDENTITY = title_identity.TABLE


def _utc_date(air_ts: float) -> str:
    # Through the shared conversion so this agrees with every other reading of a
    # record's air time, including on a platform whose C library refuses dates
    # before 1970 — see providers/base.epoch_moment.
    return epoch_moment(air_ts).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# writing a fill
# ---------------------------------------------------------------------------

# `from_search` IS THE LAST COLUMN AND THE CALLER STATES IT. A fill writes 0 and
# every airing NO FEED OFFERED writes 1 — a calendar search followed, and a title
# added to somebody's tracker (app/distrakt/arrival.py) — which is what lets the
# fill's delete spare rows it would never put back. The name is narrower than
# the meaning, "not from a feed"; see store_loose_airings and MIGRATION_37. Because
# this is INSERT OR REPLACE on the airing's natural key, a feed row for the same
# airing overwrites a searched one and returns it to 0, so the exemption ends the
# moment the service starts listing the title itself.
_INSERT_AIRING = """
INSERT OR REPLACE INTO calendar_airings
    (source, media, source_id, endpoint, title_key, air_ts, air_date, date_only,
     season, episode_number, episode_label, stored_at, from_search)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _airing_rows(conn: db.Connection, endpoint_key: str, records: list[Record], *,
                 now: int, stale_after: int, from_search: int) -> list[tuple]:
    """The airings for `records`, each filed under its title's key — which the
    title store decides and writes its own rows for, in the caller's
    transaction (metadata.store_records)."""
    keys = metadata.store_records(conn, records, now=now, stale_after=stale_after)
    out = []
    for record, key in zip(records, keys):
        season = record.season if record.season is not None else UNSTATED
        number = record.episode_number if record.episode_number is not None else UNSTATED
        out.append((
            str(record.source), str(record.media), str(record.id), endpoint_key, key,
            float(record.air_ts), _utc_date(record.air_ts), 1 if record.date_only else 0,
            int(season), int(number), str(record.episode_label or ""), now, from_search,
        ))
    return out


async def store_span(endpoint_key: str, span_start: date, span_end: date,
                     records: list[Record], *, sources, asked, unchanged=(),
                     now: int, stale_after: int) -> None:
    """Replace what is stored for `endpoint_key` over [span_start, span_end).

    DELETE THEN INSERT, PER SOURCE THAT ANSWERED WITH DATA, and it has to be: a
    title a source has STOPPED listing leaves no row for an upsert to find, so an
    insert-only refresh would go on drawing it for ever. Only sources that
    ANSWERED are cleared -- wiping a source's airings because it was unreachable
    would turn a refused request into a month that lost half its entries.

    `unchanged` IS A THIRD ANSWER AND NOT A KIND OF SILENCE. A source whose files
    all replied 304 has told us something definite -- the rows already stored are
    still correct -- so its coverage advances exactly as an answer does, and its
    airings are LEFT ALONE rather than deleted.

    One transaction, so a reader never sees the gap between the delete and the
    insert, and so a title re-keyed by what this fill states moves with the
    airings that stated it.
    """
    first, last = span_start.isoformat(), span_end.isoformat()
    answered = [str(s) for s in sources]

    # TRIMMED HERE AS WELL AS AT THE FETCH, because an airing is FILED BY ITS OWN
    # DATE. A source may treat the `days` bound as a floor rather than a ceiling
    # -- Trakt has returned entries two months past a seven-day window -- and a
    # row landing in a span with no coverage row would never be read, and the
    # next fill of its own span would delete it unseen. Dropping it loudly is the
    # honest failure; the span it belongs to fetches it properly when asked.
    inside = [r for r in records if first <= _utc_date(r.air_ts) < last]
    if len(inside) != len(records):
        logger.debug("%d %s entr(ies) fell outside the span starting %s; trimmed.",
                     len(records) - len(inside), endpoint_key, first)

    def _work(conn):
        for name in answered:
            conn.execute(
                # `from_search = 0` SPARES WHAT NO FEED EVER LISTED. This delete
                # exists so a title a source has STOPPED listing stops being
                # drawn, and a searched row was never in that source's answer to
                # begin with. MIGRATION_37 has the measured case.
                "DELETE FROM calendar_airings WHERE from_search = 0 "
                "  AND source = ? AND endpoint = ? "
                "AND air_date >= ? AND air_date < ?",
                (name, endpoint_key, first, last))
        conn.executemany(_INSERT_AIRING, _airing_rows(
            conn, endpoint_key, inside, now=now, stale_after=stale_after, from_search=0))
        replied = {*answered, *(str(s) for s in unchanged)}
        conn.executemany(
            "INSERT INTO calendar_coverage "
            "(endpoint, span_start, source, asked, answered, stored_at, stale_after) "
            "VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(endpoint, span_start, source) DO UPDATE SET "
            "asked = 1, answered = excluded.answered, "
            "stored_at = excluded.stored_at, stale_after = excluded.stale_after",
            [(endpoint_key, first, str(name), 1 if str(name) in replied else 0,
              now, stale_after) for name in {*asked, *replied}])

    await db.transaction(_work)


async def store_loose_airings(endpoint_key: str, records, *, now: int,
                              stale_after: int) -> int:
    """Store airings that no CALENDAR FEED offered, filling a gap in one.

    WHY A SOURCE CAN BE MISSING ITS OWN TITLE. A service answers two different
    datasets about one show, and they disagree. Trakt's show record dates Half
    Man's first season to 2026-04-28T20:00Z; Trakt's premieres CALENDAR for that
    week does not list it, and its all-episodes calendar carries episode two and
    no episode one. The title is real, the date is the service's own, and the
    calendar this app is built from simply has a hole in it.

    SO THE SEARCH REPAIRS IT. A catalogue lookup already had to describe the
    title and date its seasons to draw a result row at all; writing that as an
    airing costs nothing more and turns "go to the month and hope" into a card
    that is actually there.

    WRITTEN AS THE FEED WOULD HAVE WRITTEN IT, deliberately. Same source, same
    ids, same natural key, same title store — so a row here resolves and filters
    and renders like every other, and a later fill of its span REPLACES it rather
    than duplicating it. The records come back with their ids completed from the
    title store (metadata.store_records), so a caller drawing them keys them as
    the calendar's read will.

    NO COVERAGE ROW IS WRITTEN, and that is the important restraint. Coverage
    records which sources were ASKED about a window and which ANSWERED — this
    asked nobody about a window, so claiming coverage would tell the fill path a
    span had been fetched when it has not.

    Returns how many airings were written.
    """
    records = list(records or ())
    if not records:
        return 0

    def _write(conn):
        conn.executemany(_INSERT_AIRING, _airing_rows(
            conn, endpoint_key, records, now=now, stale_after=stale_after, from_search=1))

    await db.transaction(_write)
    logger.info("calendar search filled %d airing(s) on %s that no feed listed.",
                len(records), endpoint_key)
    return len(records)


# ---------------------------------------------------------------------------
# reading a span back
# ---------------------------------------------------------------------------

# THREE LEVELS AND THE IDENTITY, THREE LEFT JOINS, ONE QUERY. The title and
# episode joins are LEFT because a missing row at either level is ordinary rather
# than exceptional: an episode row exists only for a stated coordinate, and Simkl
# publishes no per-episode facts at all. The identity join is LEFT because a title
# registered before the store knew its identity reads exactly as it always did.
_READ_SQL = f"""
SELECT a.source, a.media, a.source_id, a.title_key, a.air_ts, a.date_only,
       a.season, a.episode_number, a.episode_label,
       t.title, t.ids_json, t.detail_url, t.year, t.network, t.country,
       t.language, t.certification, t.status, t.overview, t.poster, t.runtime,
       t.genres_json, t.ratings_json, t.anime_type, t.release_types_json,
       t.enriched,
       e.title AS episode_title, e.runtime AS episode_runtime,
       e.rating AS episode_rating,
       i.ids_json AS known_ids
FROM calendar_airings a
LEFT JOIN {_TITLES} t
       ON t.source = a.source AND t.media = a.media AND t.source_id = a.source_id
LEFT JOIN {_EPISODES} e
       ON e.source = a.source AND e.media = a.media AND e.source_id = a.source_id
      AND e.season = a.season AND e.number = a.episode_number
LEFT JOIN {_IDENTITY} i ON i.title_key = a.title_key
WHERE a.endpoint = ? AND a.air_date >= ? AND a.air_date < ?
ORDER BY a.air_ts, a.title_key
"""


def _record_from_row(row) -> Record:
    """One stored airing, back as the Record the read path already knows.

    ITS IDS ARE THE SERVICE'S OWN WITH THE TITLE'S SHARED IDS BENEATH THEM. The
    read groups listings by the ids they carry (cache.group_base), so a listing
    whose service knows a show only by its tvdb id would never join the listing
    of the same show its tmdb id names — two cards for one episode. The title
    store knows both, and adding what it knows under the service's own statement
    (identity.with_shared) makes every listing of one title key the same way.

    A MISSING TITLE ROW IS NOT AN ERROR. The LEFT JOIN can miss while a fill is
    in flight, or if retention swept the title before its airings; what comes
    back then is an airing with a blank title, which renders as a thin card
    rather than vanishing.
    """
    ratings = json.loads(row["ratings_json"] or "{}") if row["ratings_json"] else {}
    source = str(row["source"])
    season = row["season"]
    number = row["episode_number"]
    ids = json.loads(row["ids_json"] or "{}") if row["ids_json"] else {}
    if row["known_ids"]:
        ids = title_identity.with_shared(ids, json.loads(row["known_ids"]))
    return Record(
        source=Source(source), media=Media(str(row["media"])), id=str(row["source_id"]),
        ids=ids,
        detail_url=str(row["detail_url"] or ""), title=str(row["title"] or ""),
        air_ts=float(row["air_ts"]), date_only=bool(row["date_only"]),
        # BACK TO A NUMBER WHERE IT IS ONE. `Record.year` is `int | str`, the
        # column is TEXT, and a caller comparing or sorting on it would see the
        # type change in storage; every source hands it over as an int.
        year=title_rows.as_year(row["year"]), network=row["network"] or "",
        country=row["country"] or "", language=row["language"] or "",
        # THE EPISODE'S OWN RUNTIME WHERE ONE IS KNOWN, the title's otherwise.
        # A show's episodes really do differ -- a double-length finale is not the
        # show's nominal runtime. Simkl publishes no per-episode runtime at all,
        # so falling back is the ordinary path and not a failure.
        runtime=(row["episode_runtime"] if row["episode_runtime"] is not None
                 else row["runtime"]),
        status=row["status"] or "",
        # BACK OUT OF THE MAP UNDER THIS SOURCE'S OWN NAME. `Record.rating` is
        # one number shown under one service's mark, so a row that holds three
        # services' scores still hands this source only its own.
        rating=ratings.get(source),
        # IMDb IS IN THE SAME MAP AND IS NOT THIS SOURCE'S OWN SCORE, which is
        # exactly why the map is keyed by who said it rather than being one
        # number. It is read out by name for every source.
        imdb_rating=ratings.get("imdb"),
        genres=json.loads(row["genres_json"] or "[]") if row["genres_json"] else [],
        certification=row["certification"] or "", overview=row["overview"] or "",
        poster=row["poster"] or "",
        episode_label=row["episode_label"] or None,
        episode_title=row["episode_title"] or "",
        season=None if season == UNSTATED else season,
        episode_number=None if number == UNSTATED else number,
        anime_type=row["anime_type"] or "",
        release_types_by_country=(
            json.loads(row["release_types_json"] or "{}")
            if row["release_types_json"] else {}),
        enriched=bool(row["enriched"]),
    )


async def read_span(endpoint_key: str, span_start: date, span_end: date
                    ) -> tuple[list[Record], tuple[str, ...], tuple[str, ...], int | None]:
    """Every stored airing for `endpoint_key` in [span_start, span_end), with who
    was asked, who answered, and when it was stored.

    The coverage lists come from `calendar_coverage` and NEVER from which sources
    happen to have airings here: a source that legitimately lists nothing in a
    span would otherwise be indistinguishable from one that was never asked,
    which is the difference between "partial" and "nothing to report".
    """
    first, last = span_start.isoformat(), span_end.isoformat()
    found = await db.fetch_all(_READ_SQL, (endpoint_key, first, last))
    answered, asked, stored_at = await span_coverage(endpoint_key, span_start)
    return _in_fetch_order(found), answered, asked, stored_at


async def span_coverage(endpoint_key: str, span_start: date
                        ) -> tuple[tuple[str, ...], tuple[str, ...], int | None]:
    """(who answered, who was asked, when it was stored) for the span beginning
    `span_start` — `read_span`'s coverage half, with no airing read at all.

    FOR THE READ PATH, WHICH NO LONGER GROUPS A SPAN ON ITS OWN. Whether a span
    is fresh, partial or missing is a question about coverage; its airings are
    read once for the whole range a reader asked about (`read_span_whole`), so
    reading them here as well would read every row twice.

    AND FOR THE SCHEDULE (cache.refresh_months), whose clock this is: stored
    rather than remembered, so a restart does not reset it. The EARLIEST source's
    store rather than the latest: a span is only as fresh as its stalest source,
    so one source refilled an hour ago must not make the whole span look current
    while another has not been asked in a week.
    """
    cover = await db.fetch_all(
        "SELECT source, asked, answered, stored_at FROM calendar_coverage "
        "WHERE endpoint = ? AND span_start = ?", (endpoint_key, span_start.isoformat()))
    asked = tuple(str(r["source"]) for r in cover if r["asked"])
    answered = tuple(str(r["source"]) for r in cover if r["answered"])
    stored_at = min((int(r["stored_at"]) for r in cover), default=None)
    return answered, asked, stored_at


# EVERY OTHER STORED LISTING OF THE EPISODES A RANGE HOLDS, at any date. One
# episode is ONE card whichever services list it, and whether two services'
# listings are the same card is decided by comparing their DATES — so a listing
# just outside the range, or weeks outside it within the same month, can decide
# where a card inside it is drawn. This reads exactly the listings that can
# matter, which is every listing of each episode the range names
# (app/calendar/cache.py's `join_listings` is the rule that compares them).
#
# THE EPISODE IS THE COORDINATE: (title_key, season, episode_number) for a show,
# the title alone for a film. A show listing with no full coordinate is not an
# episode this can name, and its own day is all that ever joins it to one
# (cache.match_keys), so it never needs a listing from elsewhere. The title_key is
# the title store's, so two services' listings of one title share it once the
# store knows they are one title — a re-key moves both.
#
# BY TITLE, THEN BY COORDINATE, AND THE ORDER IS THE COST. The only index that
# reaches an airing by what it IS rather than when is `ix_calendar_airings_key`,
# on the title alone. Joining each of a month's five thousand coordinates to it
# visits every row of its title once PER COORDINATE — MEASURED on a copy of a
# real instance, a plan that does that took 39 seconds for one `shows` month.
# Materialising the distinct titles first visits each title's rows once and
# tests the coordinate on the row in hand: 33ms for that month, 79ms for the
# busiest one held, 16ms for a day — beside 206-384ms for the range's own rows.
_HELD_EPISODES = f"""
WITH held AS MATERIALIZED (
    SELECT DISTINCT title_key, season, episode_number FROM calendar_airings
    WHERE endpoint = :endpoint AND air_date >= :first AND air_date < :last
      AND (media = 'movie' OR (season != {UNSTATED} AND episode_number != {UNSTATED}))),
held_titles AS MATERIALIZED (SELECT DISTINCT title_key FROM held)
"""
_OTHER_LISTINGS_SQL = _HELD_EPISODES + _READ_SQL.replace(
    "FROM calendar_airings a",
    "FROM held_titles k CROSS JOIN calendar_airings a ON a.title_key = k.title_key",
).replace(
    "WHERE a.endpoint = ? AND a.air_date >= ? AND a.air_date < ?",
    """WHERE +a.endpoint = :endpoint AND (a.air_date < :first OR a.air_date >= :last)
  AND (a.title_key, a.season, a.episode_number) IN
      (SELECT title_key, season, episode_number FROM held)""",
)


async def read_span_whole(endpoint_key: str, span_start: date, span_end: date
                          ) -> list[Record]:
    """Every stored airing for `endpoint_key` in [span_start, span_end), and every
    other stored listing, at any date, of each episode those airings name — the
    rows a reader of that range needs to say which cards it draws, and where.

    No coverage is read: whether each span inside the range was filled is the
    caller's question (`span_coverage`), asked once per span before this.
    """
    first, last = span_start.isoformat(), span_end.isoformat()
    inside = await db.fetch_all(_READ_SQL, (endpoint_key, first, last))
    outside = await db.fetch_all(_OTHER_LISTINGS_SQL,
                                 {"endpoint": endpoint_key, "first": first, "last": last})
    return _in_fetch_order([*inside, *outside])


def _in_fetch_order(found) -> list[Record]:
    """Stored rows as Records, ordered as a fill would have produced them.

    SQL has already ordered by air time; this settles the tie between two sources
    describing the same instant, which SQL cannot because the order is the
    registry's rather than any column's. A stable sort, so the query's own
    ordering survives inside each source.
    """
    records = [_record_from_row(r) for r in found]
    records.sort(key=lambda r: (r.air_ts, title_rows.SOURCE_ORDER.get(str(r.source), 99)))
    return records


# ---------------------------------------------------------------------------
# the calendar's own search
# ---------------------------------------------------------------------------
# WHICH GROUPS A QUERY MATCHES, on one endpoint. It answers `title_key` — the
# cross-source identity the read path groups by — rather than airings, because a
# group is what has to be loaded WHOLE: two services describing one title may
# spell it differently, so matching on the title alone can find one source's
# record and miss the other's, and a group missing a source resolves to a
# different card than the calendar draws.
#
# TWO SHAPES OF ONE QUESTION, because each is fast exactly where the other is
# slow; `groups_matching` says which it asks when. Both answer the same set
# whenever fewer than the limit match, which is what lets it choose on cost.
#
# THE MATCHES FIRST (`_SEARCH_SQL`): the folded titles are scanned in the title
# store's fold index, which holds nothing else, and only the rows that matched
# are looked up on this endpoint through the airings' own primary key. `CROSS
# JOIN` is what fixes that order — SQLite never reorders one — and it has to be
# fixed: left to itself the planner starts from the endpoint's airings, every one
# of them, and checks each title as it goes. MEASURED on a copy of a real
# instance (28,005 airings on `shows`, 14,761 on `shows/premieres`, 21,247
# titles): a query naming a title took 38ms on premieres, 57ms on `shows` and
# 112ms across every calendar that way, even when nothing matched; matches
# first, 2ms per calendar.
# `INDEXED BY` RATHER THAN A `rowid IN (...)` SUBQUERY, because this one stops at
# the limit and the subquery cannot: it collects EVERY matching rowid before the
# first airing is read, 3-10ms for two letters that match thousands of titles,
# while a scan of the index itself hands matches over as it finds them.
# A RAW STRING, because the escape character this states IS a backslash.
_SEARCH_SQL = rf"""
SELECT DISTINCT a.title_key
FROM {_TITLES} t INDEXED BY {title_rows.FOLD_INDEX}
CROSS JOIN calendar_airings a
  ON a.source = t.source AND a.media = t.media AND a.source_id = t.source_id
 AND a.endpoint = ?
WHERE t.title_fold LIKE ? ESCAPE '\'
LIMIT ?
"""

# THE ENDPOINT FIRST (`_SEARCH_BY_AIRING_SQL`), in air-date order: the shape this
# search has always had, and still the right one twice over. On a SMALL calendar
# it costs next to nothing whatever is typed, because the whole calendar is a
# few hundred airings — under a millisecond measured on `shows/new` (438) and
# `shows/finales` (249) — while matches-first has to walk every title that
# matches anywhere, 16-24ms for two letters there. And for a query BROAD enough
# to match more groups than the limit takes, it is the one that decides WHICH
# groups a search loads — the earliest-airing ones, as it always has.
_SEARCH_BY_AIRING_SQL = rf"""
SELECT DISTINCT a.title_key
FROM calendar_airings a
JOIN {_TITLES} t
  ON t.source = a.source AND t.media = a.media AND t.source_id = a.source_id
WHERE a.endpoint = ? AND t.title_fold LIKE ? ESCAPE '\'
LIMIT ?
"""

# The same read the span path uses, addressed by group instead of by date range.
# One placeholder per key, which is why the caller's limit matters.
# `+a.endpoint` KEEPS THE ENDPOINT OFF THE INDEX, which is SQLite's own spelling
# for "do not use an index for this term": the planner otherwise walks the
# endpoint's whole month index and tests each airing's key, when the keys are a
# handful and `ix_calendar_airings_key` goes straight to their rows. Measured on
# the same copy, forty groups: 6.1ms -> 0.8ms on premieres, 17ms -> 9.6ms on
# `shows`, the same rows either way.
_BY_KEY_SQL = _READ_SQL.replace(
    "WHERE a.endpoint = ? AND a.air_date >= ? AND a.air_date < ?",
    "WHERE +a.endpoint = ? AND a.title_key IN (__KEYS__)")


# HOW MANY AIRINGS MAKE A CALENDAR BIG ENOUGH TO SEARCH MATCHES-FIRST. Reading
# every airing of the endpoint costs about 2ms a thousand on the measured copy
# (0.5ms for 438, 15ms for 7,770, 40ms for 14,761), and the matches-first read
# costs about 2ms for a query naming a title — so a thousand is where the two
# meet. Counting up to it is a walk of at most this many index entries.
_SMALL_CALENDAR = 1000

_CALENDAR_SIZE_SQL = """
SELECT COUNT(*) FROM (SELECT 1 FROM calendar_airings WHERE endpoint = ? LIMIT ?)
"""


async def groups_matching(endpoint_key: str, query: str, limit: int) -> list[str]:
    """The `title_key`s on `endpoint_key` whose title matches `query`.

    MATCHED ON `title_fold`, the same folding a stored title was written with —
    case-folded and accent-stripped, so "pokemon" finds "Pokemon" (the title
    store's `like_needle`). The index on that column is what makes this a lookup
    rather than a scan.

    WHICH READ IS ASKED IS A COST DECISION AND NEVER CHANGES THE ANSWER. A small
    calendar is read endpoint-first, whatever was typed. A big one is read
    matches-first, and fewer than `limit` back is the whole answer — the same
    set the endpoint-first read would have found. `limit` or more means the
    query matched more groups than a search loads, and which of them is the
    endpoint-first read's to say, so it is asked again that way. The keys come
    back in no particular order: `read_groups` sorts what it loads.
    """
    needle = typed_search.like_needle(query)
    size = await db.fetch_value(_CALENDAR_SIZE_SQL, (endpoint_key, _SMALL_CALENDAR), 0)
    if size >= _SMALL_CALENDAR:
        found = await db.fetch_all(_SEARCH_SQL, (endpoint_key, needle, limit))
        if len(found) < limit:
            return [str(r["title_key"]) for r in found]
    found = await db.fetch_all(_SEARCH_BY_AIRING_SQL, (endpoint_key, needle, limit))
    return [str(r["title_key"]) for r in found]


async def read_groups(endpoint_key: str, title_keys) -> list[Record]:
    """Every stored airing of `title_keys` on one endpoint, whole.

    WHOLE IS THE POINT — every source's record for each group, so the read path
    resolves the same card a month read would. Loading by group rather than by
    month is what makes a search cheap: one real `shows` month held 12,880
    airings, and the titles a query matches are a handful of them.
    """
    keys = [str(k) for k in title_keys]
    if not keys:
        return []
    sql = _BY_KEY_SQL.replace("__KEYS__", ", ".join("?" * len(keys)))
    found = await db.fetch_all(sql, (endpoint_key, *keys))
    return _in_fetch_order(found)


async def sources_airing(endpoint_key: str, title_key: str, season: int | None) -> set[str]:
    """The sources with a stored airing of `title_key` (that season, or with no
    season for a film) on `endpoint_key` — which services' calendars already
    draw it there. A keyed read on the airings' own index."""
    if season is None:
        found = await db.fetch_all(
            "SELECT DISTINCT source FROM calendar_airings "
            "WHERE endpoint = ? AND title_key = ?", (endpoint_key, str(title_key)))
    else:
        found = await db.fetch_all(
            "SELECT DISTINCT source FROM calendar_airings "
            "WHERE endpoint = ? AND title_key = ? AND season = ?",
            (endpoint_key, str(title_key), int(season)))
    return {str(row["source"]) for row in found}


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

# HOW LONG A STORED AIRING IS KEPT AT ALL, as opposed to how fresh it has to be
# to serve a request -- two clocks, deliberately different fields, because "this
# is stale, refresh it when somebody touches it" and "delete this" are different
# instructions and one number cannot mean both.
#
# SIX MONTHS, FLAT, EVERY SOURCE. TMDB's terms forbid caching anything obtained
# from them for longer than six months, so the ceiling exists whether or not this
# design wants one; applying it to everything rather than to TMDB rows alone
# costs almost nothing and removes a per-source branch nobody would remember to
# keep correct.
#
# AND THE TIERS MAKE IT A NON-EVENT: anything with traffic refreshes far inside
# six months, so the only rows this deletes are ones nobody has touched in half a
# year, which refetch on the next visit.
#
# ONE HONEST REGRESSION, RECORDED RATHER THAN DISCOVERED. The blob this replaces
# was swept at app/cache.py's RETAIN_SECONDS plus TTL_GRACE_SECONDS -- nine
# months in total, deliberately, because a public share link never refetches and
# must go on rendering whatever it has. A share link to a month between six and
# nine months old renders today and will render EMPTY under this. Narrow, real,
# and a decision rather than an oversight.
RETAIN_SECONDS = 180 * 24 * 60 * 60


async def sweep(now: int | None = None) -> int:
    """Delete calendar rows nothing has touched inside the retention window, and
    then the title store's rows no airing names any more.

    ORDER IS THE WHOLE OF THE CARE HERE. Airings go first; a title or an episode
    row is removed only once it is old AND NOTHING STILL AIRS IT
    (title_rows.sweep_unlisted). Deleting a title on age alone would strand every
    airing still pointing at it — the read's LEFT JOIN would hand back a card with
    no name, which is worse than either keeping the row or dropping the airing
    with it. One transaction, so that order cannot be seen half-done.
    """
    ts = db.now() if now is None else now
    cutoff = ts - RETAIN_SECONDS

    def _work(conn) -> int:
        removed = conn.execute(
            "DELETE FROM calendar_airings WHERE stored_at <= ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM calendar_coverage WHERE stored_at <= ?", (cutoff,))
        title_rows.sweep_unlisted(conn, cutoff)
        # And whatever a service's own terms say may not be kept, held or not —
        # after the age sweep, which it overrides (TMDB's six months).
        title_rows.sweep_past_ceilings(conn, ts)
        # The validators outlive nothing. A file whose rows are gone has nothing
        # for a 304 to confirm, so keeping its ETag would answer "unchanged" for
        # a month this app no longer holds anything about.
        conn.execute("DELETE FROM calendar_source_files WHERE fetched_at <= ?", (cutoff,))
        return removed

    removed = await db.transaction(_work)
    if removed:
        logger.info("calendar retention: removed %d airing(s) not stored inside "
                    "the last %d days.", removed, RETAIN_SECONDS // 86400)
    return removed


# ---------------------------------------------------------------------------
# which episodes the calendar holds that nobody has looked up
# ---------------------------------------------------------------------------

# WHICH SEASONS THE EPISODE DRAIN STILL OWES WORK ON. Written once and shared by
# the drain's batch and by the count the page shows, because those two answering
# differently is the exact failure that makes a backlog readout untrustworthy.
#
# Parameters, in order: the id namespace to read out of ids_json, the source, and
# the clock a due date is compared against.
_OWED_SEASONS = (
    "SELECT json_extract(t.ids_json, '$.' || ?) AS service_id, "
    "       a.media, a.season, "
    # NULL and 0 both mean "never answered", and both must sort ahead of every
    # real timestamp — hence the coalesce rather than MIN alone.
    "       MIN(COALESCE(e.fetched_at, 0)) AS answered_at, "
    "       MAX(a.stored_at) AS newest "
    "FROM calendar_airings a "
    f"JOIN {_TITLES} t ON t.source = a.source AND t.media = a.media "
    "                      AND t.source_id = a.source_id "
    f"LEFT JOIN {_EPISODES} e ON e.source = a.source AND e.media = a.media "
    "                             AND e.source_id = a.source_id "
    "                             AND e.season = a.season "
    "                             AND e.number = a.episode_number "
    "WHERE a.source = ? AND a.season >= 0 AND a.episode_number >= 0 "
    "  AND (e.fetched_at IS NULL OR e.fetched_at = 0 OR e.stale_after <= ?) "
    "GROUP BY service_id, a.media, a.season"
)


async def owed_season_count(source: str, id_namespace: str, now: int) -> int:
    """How many seasons `owed_episodes` would hand out if nothing capped it.

    The same query the batch comes from, so the number a reader watches count
    down cannot disagree with the work actually being done.
    """
    return int(await db.fetch_value(
        f"SELECT COUNT(*) FROM ({_OWED_SEASONS})",
        (id_namespace, source, now)) or 0)


async def owed_episodes(source: str, id_namespace: str, limit: int,
                        now: int) -> list[tuple[int, str, int]]:
    """[(service id, media, season)] for seasons on the calendar whose episodes
    nobody has looked up yet, or whose answers have fallen due again.

    A SEASON AT A TIME, NOT AN EPISODE AT A TIME, because that is the shape of
    the call that answers it — one request returns a season's whole episode list.

    A ROW WRITTEN BY A LISTING IS NOT AN ANSWER. The feed gives an episode title
    and nothing else, so `fetched_at = 0` marks a stub: present, keyed, and still
    owed everything a lookup would add.

    AN AIRING WITH NO STATED EPISODE NUMBER IS NOT OWED, and leaving it in was a
    live defect: `store_episodes` cannot key a row for a coordinate the source
    never gave, so such an airing came back owed on EVERY pass, and the drain
    re-read those seasons once a minute for ever.

    NEVER-LOOKED-UP SEASONS GO FIRST, and only then the ones falling due. A
    season that has never been answered shows placeholder facts to somebody
    RIGHT NOW; a due one shows facts that were true when they were fetched.
    """
    found = await db.fetch_all(
        _OWED_SEASONS + " ORDER BY answered_at ASC, newest DESC LIMIT ?",
        (id_namespace, source, now, limit))
    out: list[tuple[int, str, int]] = []
    for row in found:
        try:
            out.append((int(row["service_id"]), str(row["media"]), int(row["season"])))
        except (TypeError, ValueError):
            continue  # a title this service never named in its own id space
    return out


async def store_episodes(source: str, service_id: int, media: str, season: int,
                         episodes: list[dict], now: int) -> int:
    """Write one season's episode facts — every episode of it the calendar lists
    — addressed by the service's own id.

    A SEASON WITH NO EPISODES STILL COUNTS AS ANSWERED, which is why the stub
    rows are stamped even when the list is empty: without it a season Trakt has
    nothing for is owed again on every pass for ever.

    WHEN EACH ROW FALLS DUE AGAIN IS DECIDED PER EPISODE, from that episode's own
    latest AIRING rather than from the season's or from the lookup's clock — a
    season part-aired sits on both sides of the correction window at once.
    """
    source_ids = await title_rows.source_ids_for(source, media, service_id)
    if not source_ids:
        return 0
    by_number = {int(e["number"]): e for e in episodes if e.get("number") is not None}

    def _work(conn) -> int:
        written = 0
        for source_id in source_ids:
            # Every airing of this season, so a stub a listing wrote is answered
            # even when the lookup did not mention that episode.
            owed = conn.execute(
                "SELECT episode_number, MAX(air_ts) FROM calendar_airings "
                "WHERE source = ? AND media = ? AND source_id = ? AND season = ? "
                "GROUP BY episode_number",
                (source, media, source_id, season)).fetchall()
            for (number, air_ts) in owed:
                if number is None or number < 0:
                    continue
                # The LAST airing, not the first: a re-airing is another chance
                # for somebody to have corrected the record, so the correction
                # window opens again behind it.
                title_rows.write_episode_answer(
                    conn, source, media, source_id, season, int(number),
                    by_number.get(int(number)) or {}, now=now,
                    stale_after=title_rows.episode_stale_after(air_ts, now))
                written += 1
        return written

    return await db.transaction(_work)


# ---------------------------------------------------------------------------
# the questions the read path asks about storage itself
# ---------------------------------------------------------------------------

async def signature() -> str:
    """A cheap token that changes whenever the stored calendar might name a title
    it did not name before.

    THE CONTRACT IS WHAT MATTERS, not the shape: the tracker's name resolution
    caches an index against this and must rebuild when the calendar has moved.
    COUNT plus the newest stored_at is enough -- an airing added, replaced or
    swept moves one or the other.
    """
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(MAX(stored_at), 0) AS newest "
        "FROM calendar_airings")
    return f"{int(row['n'])}:{int(row['newest'])}" if row else "0:0"


async def sources_with_rows(endpoint_key: str, span_start: date, span_end: date
                           ) -> set[str]:
    """Which sources actually HOLD airings for this span.

    NOT THE COVERAGE TABLE, AND THE DIFFERENCE IS A BUG THAT SHIPPED. Coverage
    records who replied; this records who left something behind. They came apart
    when a source answered "unchanged" for a span it had never stored anything
    for — coverage said answered, the airings table was empty, and because the
    gate that decides whether "unchanged" is acceptable read COVERAGE, the state
    justified itself for ever.

    THE FAILURE DIRECTION IS CHOSEN. A source that has genuinely nothing to say
    about a span looks the same as one that never stored anything, so it will
    re-read a body it did not need — one 200 instead of one 304, occasionally.
    The other way round loses a whole service off a calendar silently, which is
    what happened.
    """
    found = await db.fetch_all(
        "SELECT DISTINCT source FROM calendar_airings "
        "WHERE endpoint = ? AND air_date >= ? AND air_date < ?",
        (endpoint_key, span_start.isoformat(), span_end.isoformat()))
    return {str(r["source"]) for r in found}
