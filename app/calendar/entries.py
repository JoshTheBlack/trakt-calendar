"""The calendar's storage: airings, per-source title facts, and coverage.

WHAT THIS REPLACES. Until now a fill compressed every source's records for an
(endpoint, seven days) into one blob in `api_cache` and the read path inflated
four or five of them per month. Two questions that mattered could not be asked of
that shape at all: "which titles does the stored calendar name" needed EVERY
window inflated, which is why the enrichment drain spent hundreds of milliseconds
of event-loop time before making a request; and "what airs on this day" needed a
whole month decompressed to answer for one day.

THE STORED FACTS ARE SPLIT BY HOW LONG THEY STAY TRUE, which is the whole reason
this is three tables rather than one. Measured across 4,330 (source, title) pairs
with more than one airing: runtime disagrees on 0.1% of them, certification 0.2%,
genres 0.4%, network 0.8%, language and country 0.0%. Those are TITLE facts, and
storing a copy on every airing is what let one show's Tuesday and Thursday
disagree -- not because a source said anything different, but because the two
airings were fetched a week apart. `episode_title` disagrees on 95.5%, which is
the opposite finding and why episodes get their own level.

NOTHING HERE IS PER-VIEWER, and that is the invariant the whole design serves.
Every source's rows are stored whole; which one a viewer sees, and whether their
filters hide it, is decided at READ against their own preferences. A per-viewer
value reaching storage would poison what a second viewer sees from the same rows.
`app/calendar/resolve.py` owns the read-time decision; this module owns the rows.
"""
from __future__ import annotations

import json
import logging
import unicodedata
from datetime import date, datetime, timezone

from .. import db
from ..providers.base import Media, Record, Source, epoch_moment

logger = logging.getLogger(__name__)

# Written onto every airing whose source did not state a coordinate. -1 RATHER
# THAN NULL because SQLite permits NULLs inside a non-INTEGER primary key, so a
# nullable season would let one airing insert twice over. A third of premiere
# entries state no season or no episode number -- overwhelmingly Simkl anime,
# where an absolute episode number is simply how the entry is spelled -- so
# "unstated" is the ordinary case and needs a value the key can compare.
UNSTATED = -1


def _as_year(stored) -> int | str:
    """A stored year as the int it was, or whatever it is if it is not one.

    Not every source states a plain year -- a range or a placeholder comes back
    as itself rather than being coerced into a number it never was.
    """
    text = str(stored or "")
    return int(text) if text.isdigit() else text


def _utc_date(air_ts: float) -> str:
    # Through the shared conversion so this agrees with every other reading of a
    # record's air time, including on a platform whose C library refuses dates
    # before 1970 — see providers/base.epoch_moment.
    return epoch_moment(air_ts).strftime("%Y-%m-%d")


def fold_title(title: str) -> str:
    """A title reduced to what a search should match it on.

    Case-folded and accent-stripped, so "Pokemon" finds "Pokémon" -- the single
    most common way a title typed from memory misses the one stored. Stored in a
    column rather than computed in the query because the query has to fold its
    needle with the SAME rule, and one implementation is the only way to be sure
    it does.
    """
    decomposed = unicodedata.normalize("NFKD", str(title or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


# ---------------------------------------------------------------------------
# writing a fill
# ---------------------------------------------------------------------------

# Level-1 columns a record can speak to, in the order the upsert states them.
_TITLE_FIELDS = ("title", "title_fold", "ids_json", "detail_url", "year", "network",
                 "country", "language", "certification", "status", "overview",
                 "poster", "runtime", "genres_json", "ratings_json", "anime_type",
                 "release_types_json", "enriched")


def _title_values(record: Record) -> tuple:
    ratings = {}
    if record.imdb_rating is not None:
        # A THIRD PARTY'S SCORE, UNDER ITS OWN NAME. It is not this source's
        # answer and must not be filed as one — see Record.imdb_rating.
        ratings["imdb"] = record.imdb_rating
    if record.rating is not None:
        # UNDER THE SOURCE'S OWN NAME, never a bare number. The card draws two
        # services' ratings side by side and never averages them; a map keyed by
        # who said it is what keeps that honest when a third service arrives.
        ratings[str(record.source)] = record.rating
    return (
        str(record.title or ""),
        fold_title(record.title),
        json.dumps(record.ids or {}),
        str(record.detail_url or ""),
        str(record.year or ""),
        str(record.network or ""),
        str(record.country or ""),
        str(record.language or ""),
        str(record.certification or ""),
        str(record.status or ""),
        str(record.overview or ""),
        str(record.poster or ""),
        record.runtime,
        json.dumps([str(g) for g in (record.genres or [])]),
        json.dumps(ratings),
        str(record.anime_type or ""),
        json.dumps(record.release_types_by_country or {}),
        1 if record.enriched else 0,
    )


# A FILL MAY NOT DEMOTE WHAT ENRICHMENT LEARNED. The calendar feed and the
# per-title drain write the same row, and the feed knows less: Simkl's CDN files
# carry no genres, network, country or certification at all. An unconditional
# upsert would therefore erase a drained answer every time the month refreshed,
# and the next drain would fetch it again -- a loop that looks like enrichment
# never sticking. So the feed's write keeps the coordinates and the poster, which
# it is authoritative for, and leaves every enriched field alone once `enriched`
# is set.
_UPSERT_TITLE = f"""
INSERT INTO calendar_titles
    (source, media, source_id, title_key, {', '.join(_TITLE_FIELDS)},
     fetched_at, stale_after)
VALUES ({', '.join(['?'] * (4 + len(_TITLE_FIELDS) + 2))})
ON CONFLICT(source, media, source_id) DO UPDATE SET
    title_key  = excluded.title_key,
    title      = excluded.title,
    title_fold = excluded.title_fold,
    detail_url = CASE WHEN excluded.detail_url != '' THEN excluded.detail_url
                      ELSE calendar_titles.detail_url END,
    poster     = CASE WHEN excluded.poster != '' THEN excluded.poster
                      ELSE calendar_titles.poster END,
    ids_json   = excluded.ids_json,
    year       = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.year
                      ELSE excluded.year END,
    network    = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.network
                      ELSE excluded.network END,
    country    = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.country
                      ELSE excluded.country END,
    language   = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.language
                      ELSE excluded.language END,
    certification = CASE WHEN calendar_titles.enriched = 1
                         THEN calendar_titles.certification
                         ELSE excluded.certification END,
    status     = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.status
                      ELSE excluded.status END,
    overview   = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.overview
                      ELSE excluded.overview END,
    runtime    = CASE WHEN calendar_titles.enriched = 1 THEN calendar_titles.runtime
                      ELSE excluded.runtime END,
    genres_json = CASE WHEN calendar_titles.enriched = 1
                       THEN calendar_titles.genres_json
                       ELSE excluded.genres_json END,
    ratings_json = CASE WHEN calendar_titles.enriched = 1
                        THEN calendar_titles.ratings_json
                        ELSE excluded.ratings_json END,
    anime_type = CASE WHEN calendar_titles.enriched = 1
                      THEN calendar_titles.anime_type ELSE excluded.anime_type END,
    enriched   = MAX(calendar_titles.enriched, excluded.enriched),
    fetched_at = excluded.fetched_at
"""

# `from_search` IS THE LAST COLUMN AND THE CALLER STATES IT. A fill writes 0 and
# the calendar search writes 1, which is what lets the fill's delete spare rows
# it would never put back — see store_loose_airings and MIGRATION_37. Because
# this is INSERT OR REPLACE on the airing's natural key, a feed row for the same
# airing overwrites a searched one and returns it to 0, so the exemption ends the
# moment the service starts listing the title itself.
_INSERT_AIRING = """
INSERT OR REPLACE INTO calendar_airings
    (source, media, source_id, endpoint, title_key, air_ts, air_date, date_only,
     season, episode_number, episode_label, stored_at, from_search)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# THE FEED'S HALF OF A LEVEL-2 ROW. A calendar entry names the episode's title
# and nothing else about it -- no overview, no runtime, no rating, no still --
# so the fill writes a STUB and the per-episode drain fills in the rest later.
#
# IT NEVER REPLACES A TITLE WITH AN EMPTY ONE, which is the only demotion the
# fill can cause on its own. It CAN still replace a real title with the
# placeholder Trakt sometimes serves ("Episode 1"): telling those apart by shape
# would be guessing, and the level-2 drain reading
# /shows/{id}/seasons?extended=full,episodes is the authority that settles it.
# What matters is that there is now ONE row to settle rather than one per airing.
_UPSERT_EPISODE = """
INSERT INTO calendar_episodes
    (source, media, source_id, season, number, title, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, media, source_id, season, number) DO UPDATE SET
    -- ONCE A LOOKUP HAS ANSWERED, IT OWNS THE TITLE. Both writers reach this
    -- statement: the fill, carrying whatever the calendar feed spelled, and the
    -- episode drain, carrying a whole season read from the source's own episode
    -- list. The second knows more -- it brings the overview, the runtime and the
    -- rating with it -- so letting the feed overwrite it on the next refill
    -- would trade a real answer for a thinner one, once a day, for ever. Before
    -- a lookup has answered (`fetched_at = 0`) the feed's title is the only one
    -- there is and is kept.
    title = CASE WHEN calendar_episodes.fetched_at > 0 THEN calendar_episodes.title
                 WHEN excluded.title != '' THEN excluded.title
                 ELSE calendar_episodes.title END,
    -- MAX, NOT ASSIGNMENT, AND IT IS THE SAME RULE AS `enriched` ONE LEVEL UP: A
    -- FILL MAY NOT DEMOTE WHAT A LOOKUP LEARNED. The fill writes 0 here to mean
    -- "a stub, still owed a lookup", so assigning it would push an ANSWERED
    -- episode back to owed on every refill -- and the current month refills
    -- daily. Observed before the guard: the episode drain reported 25 seasons
    -- filled every single minute, for ever, because each refresh handed back the
    -- work the last one had just finished.
    fetched_at = MAX(calendar_episodes.fetched_at, excluded.fetched_at)
"""


def title_key_of(record: Record) -> str:
    """The cross-source identity for a record, deferring to the one matcher.

    Imported from cache rather than reimplemented: `group_base` is the waterfall
    in providers/base.py's resolve_key, and a second spelling of it here would be
    a second answer to "are these the same title" that could drift from the one
    the read path groups by.
    """
    from . import cache  # deferred: cache imports this module for storage

    return cache.group_base(record)


async def store_span(endpoint_key: str, span_start: date, span_end: date,
                     records: list[Record], *, sources, asked, unchanged=(),
                     now: int, stale_after: int) -> None:
    """Replace what is stored for `endpoint_key` over [span_start, span_end).

    DELETE THEN INSERT, PER SOURCE THAT ANSWERED WITH DATA, and it has to be: a
    title a source has STOPPED listing leaves no row for an upsert to find, so an
    insert-only refresh would go on drawing it for ever. The window model got
    this free by replacing a whole blob; rows have to say it out loud. Only
    sources that ANSWERED are cleared -- wiping a source's airings because it was
    unreachable would turn a refused request into a month that lost half its
    entries.

    `unchanged` IS A THIRD ANSWER AND NOT A KIND OF SILENCE. A source whose files
    all replied 304 has told us something definite -- the rows already stored are
    still correct -- so its coverage advances exactly as an answer does, and its
    airings are LEFT ALONE rather than deleted. Treating it as answered would
    wipe every row it holds (it returned none); treating it as failed would mark
    the span partial and refetch it for ever.

    One transaction, so a reader never sees the gap between the delete and the
    insert.
    """
    first, last = span_start.isoformat(), span_end.isoformat()
    answered = [str(s) for s in sources]
    rows_by_source: dict[str, list] = {name: [] for name in answered}
    titles: list[tuple] = []
    episodes: list[tuple] = []

    # TRIMMED HERE AS WELL AS AT THE FETCH, because an airing is now FILED BY ITS
    # OWN DATE. A source may treat the `days` bound as a floor rather than a
    # ceiling -- Trakt has returned entries two months past a seven-day window --
    # and under the old blob a stray record was simply stored under the window
    # key and filtered at read. A row cannot be: it would land in a span that has
    # no coverage row, so nothing would ever read it and the next fill of its own
    # span would delete it unseen. Dropping it loudly is the honest failure; the
    # span it belongs to fetches it properly when something asks for that span.
    inside = [r for r in records if first <= _utc_date(r.air_ts) < last]
    if len(inside) != len(records):
        logger.debug("%d %s entr(ies) fell outside the span starting %s; trimmed.",
                     len(records) - len(inside), endpoint_key, first)
    records = inside

    for record in records:
        name = str(record.source)
        key = title_key_of(record)
        season = record.season if record.season is not None else UNSTATED
        number = record.episode_number if record.episode_number is not None else UNSTATED
        rows_by_source.setdefault(name, []).append((
            name, str(record.media), str(record.id), endpoint_key, key,
            float(record.air_ts), _utc_date(record.air_ts),
            1 if record.date_only else 0,
            int(season), int(number), str(record.episode_label or ""), now, 0,
        ))
        titles.append((name, str(record.media), str(record.id), key,
                       *_title_values(record), now, stale_after))
        # ONLY A STATED COORDINATE CAN KEY AN EPISODE. An entry that names no
        # season or number is not identifying an episode this app can file
        # anything under, and inventing a key for it would collide two different
        # episodes of one show. Measured over 45,827 stored records: 7,387 state
        # no coordinate and NOT ONE of them carries an episode title, so nothing
        # is lost by having nowhere to put one.
        if season != UNSTATED and number != UNSTATED:
            # `fetched_at = 0` MARKS A STUB. The feed gives a title and nothing
            # else, so this row is present and keyed but still owed everything a
            # per-episode lookup would add — see owed_episodes, which reads a
            # zero here as "never looked up" rather than as an old timestamp.
            episodes.append((name, str(record.media), str(record.id),
                             int(season), int(number),
                             str(record.episode_title or ""), 0))

    def _work(conn):
        for name in answered:
            conn.execute(
                # `from_search = 0` SPARES WHAT NO FEED EVER LISTED. This
                # delete exists so a title a source has STOPPED listing stops
                # being drawn, and a searched row was never in that source's
                # answer to begin with — it is in the delete's path and not in
                # the insert's, so without this it lasted only until the window
                # refetched. MIGRATION_37 has the measured case.
                "DELETE FROM calendar_airings WHERE from_search = 0 "
                "  AND source = ? AND endpoint = ? "
                "AND air_date >= ? AND air_date < ?",
                (name, endpoint_key, first, last))
        for batch in rows_by_source.values():
            conn.executemany(_INSERT_AIRING, batch)
        for row in titles:
            conn.execute(_UPSERT_TITLE, row)
        for row in episodes:
            conn.execute(_UPSERT_EPISODE, row)
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


# ---------------------------------------------------------------------------
# reading a span back
# ---------------------------------------------------------------------------

# The level-1 fields an airing's Record is rebuilt from. Named once because the
# SELECT and the Record construction below must agree, and a mismatch would be a
# field silently reading as its default.
# THREE LEVELS, TWO LEFT JOINS, ONE QUERY. Both joins are LEFT because a missing
# row at either level is ordinary rather than exceptional: an episode row exists
# only for a stated coordinate, and Simkl publishes no per-episode facts at all.
_READ_SQL = """
SELECT a.source, a.media, a.source_id, a.title_key, a.air_ts, a.date_only,
       a.season, a.episode_number, a.episode_label,
       t.title, t.ids_json, t.detail_url, t.year, t.network, t.country,
       t.language, t.certification, t.status, t.overview, t.poster, t.runtime,
       t.genres_json, t.ratings_json, t.anime_type, t.release_types_json,
       t.enriched,
       e.title AS episode_title, e.runtime AS episode_runtime,
       e.rating AS episode_rating
FROM calendar_airings a
LEFT JOIN calendar_titles t
       ON t.source = a.source AND t.media = a.media AND t.source_id = a.source_id
LEFT JOIN calendar_episodes e
       ON e.source = a.source AND e.media = a.media AND e.source_id = a.source_id
      AND e.season = a.season AND e.number = a.episode_number
WHERE a.endpoint = ? AND a.air_date >= ? AND a.air_date < ?
ORDER BY a.air_ts, a.title_key
"""

# THE ORDER SOURCES ARE DECLARED IN, which is the order a fill visited them and
# therefore the order everything downstream was built against. It is NOT
# alphabetical, and the difference is visible: `group_records` keeps the FIRST
# writer's spelling of each id, and two records sharing an air time render in
# whatever order they arrive. Sorting by source name instead would put Simkl
# ahead of Trakt and quietly reverse both.
_SOURCE_ORDER = {str(s): i for i, s in enumerate(Source)}


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
    that is actually there. It is also the only way this app can show a premiere
    both services' calendars have missed.

    WRITTEN AS THE FEED WOULD HAVE WRITTEN IT, deliberately. Same source, same
    ids, same natural key, same shaping as `store_span` — so a row here is not a
    special kind of row anybody has to know about downstream, it resolves and
    filters and renders like every other, and a later fill of its span REPLACES
    it rather than duplicating it. The feed stays authoritative: when the source
    is asked about that window again its answer wins outright, and if the hole is
    still there the next search fills it again.

    NO COVERAGE ROW IS WRITTEN, and that is the important restraint. Coverage
    records which sources were ASKED about a window and which ANSWERED — this
    asked nobody about a window, so claiming coverage would tell the fill path a
    span had been fetched when it has not, and a month would go permanently
    half-empty. These rows sit inside whatever coverage the span already has.

    Returns how many airings were written.
    """
    rows = []
    titles = []
    for record in records or ():
        name = str(record.source)
        key = title_key_of(record)
        season = record.season if record.season is not None else UNSTATED
        number = record.episode_number if record.episode_number is not None else UNSTATED
        rows.append((
            name, str(record.media), str(record.id), endpoint_key, key,
            float(record.air_ts), _utc_date(record.air_ts),
            1 if record.date_only else 0,
            int(season), int(number), str(record.episode_label or ""), now, 1,
        ))
        titles.append((name, str(record.media), str(record.id), key,
                       *_title_values(record), now, stale_after))
    if not rows:
        return 0

    def _write(conn):
        for row in titles:
            conn.execute(_UPSERT_TITLE, row)
        conn.executemany(_INSERT_AIRING, rows)

    await db.transaction(_write)
    logger.info("calendar search filled %d airing(s) on %s that no feed listed.",
                len(rows), endpoint_key)
    return len(rows)


def _record_from_row(row) -> Record:
    """One stored airing, back as the Record the read path already knows.

    A MISSING TITLE ROW IS NOT AN ERROR. The LEFT JOIN can miss while a fill is
    in flight, or if retention swept the title before its airings; what comes
    back then is an airing with a blank title, which renders as a thin card
    rather than vanishing. Dropping it instead would make a half-written fill
    look like a month that lost entries.
    """
    ratings = json.loads(row["ratings_json"] or "{}") if row["ratings_json"] else {}
    source = str(row["source"])
    season = row["season"]
    number = row["episode_number"]
    return Record(
        source=Source(source), media=Media(str(row["media"])), id=str(row["source_id"]),
        ids=json.loads(row["ids_json"] or "{}") if row["ids_json"] else {},
        detail_url=str(row["detail_url"] or ""), title=str(row["title"] or ""),
        air_ts=float(row["air_ts"]), date_only=bool(row["date_only"]),
        # BACK TO A NUMBER WHERE IT IS ONE. `Record.year` is `int | str`, the
        # column is TEXT, and a card that renders "2026" and one that renders
        # 2026 are indistinguishable -- but a caller comparing or sorting on it
        # is not, and every source hands it over as an int. Restoring the type on
        # the way out keeps storage from being a place where it silently changes.
        year=_as_year(row["year"]), network=row["network"] or "",
        country=row["country"] or "", language=row["language"] or "",
        # THE EPISODE'S OWN RUNTIME WHERE ONE IS KNOWN, the title's otherwise.
        # A show's episodes really do differ -- a double-length finale is not the
        # show's nominal runtime -- and stamping the series figure onto every
        # airing is what the level-2 split exists to stop. Simkl publishes no
        # per-episode runtime at all, so falling back is the ordinary path and
        # not a failure.
        runtime=(row["episode_runtime"] if row["episode_runtime"] is not None
                 else row["runtime"]),
        status=row["status"] or "",
        # BACK OUT OF THE MAP UNDER THIS SOURCE'S OWN NAME. `Record.rating` is
        # one number shown under one service's mark, so a row that holds three
        # services' scores still hands this source only its own.
        rating=ratings.get(source),
        # IMDb IS IN THE SAME MAP AND IS NOT THIS SOURCE'S OWN SCORE, which is
        # exactly why the map is keyed by who said it rather than being one
        # number. It is read out by name for every source, because it is a fact
        # about the TITLE that arrived through whichever source could report it.
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
    rows = await db.fetch_all(_READ_SQL, (endpoint_key, first, last))
    cover = await db.fetch_all(
        "SELECT source, asked, answered, stored_at FROM calendar_coverage "
        "WHERE endpoint = ? AND span_start = ?", (endpoint_key, first))
    asked = tuple(str(r["source"]) for r in cover if r["asked"])
    answered = tuple(str(r["source"]) for r in cover if r["answered"])
    stored_at = min((int(r["stored_at"]) for r in cover), default=None)
    return _in_fetch_order(rows), answered, asked, stored_at


def _in_fetch_order(rows) -> list[Record]:
    """Stored rows as Records, ordered as a fill would have produced them.

    SQL has already ordered by air time; this settles the tie between two sources
    describing the same instant, which SQL cannot because the order is the
    registry's rather than any column's. A stable sort, so the query's own
    ordering survives inside each source.
    """
    records = [_record_from_row(r) for r in rows]
    records.sort(key=lambda r: (r.air_ts, _SOURCE_ORDER.get(str(r.source), 99)))
    return records


# ---------------------------------------------------------------------------
# the questions the blob could not answer
# ---------------------------------------------------------------------------

_ALL_SQL = _READ_SQL.replace(
    "WHERE a.endpoint = ? AND a.air_date >= ? AND a.air_date < ?", "")


# WHERE A TITLE THIS INSTANCE HOLDS ACTUALLY AIRS, by folded title. It answers
# COORDINATES ONLY -- endpoint, air time, and enough to name the title -- and
# never a card, because whether a viewer's own calendar would DRAW that airing
# is a question about their filters and their sources, which this table has no
# opinion about. app/calendar/search.py asks the real read path that second
# question; this one exists so it can ask about a handful of months instead of
# every month on the instance.
# A RAW STRING, because the escape character this states IS a backslash. In
# an ordinary literal the backslash pairs with the quote after it and the
# clause collapses to an empty ESCAPE, which SQLite refuses at prepare time.
# WHICH GROUPS A QUERY MATCHES, on one endpoint. It answers `title_key` — the
# cross-source identity the read path groups by — rather than airings, because a
# group is what has to be loaded WHOLE: two services describing one title may
# spell it differently, so matching on the title alone can find one source's
# record and miss the other's, and a group missing a source resolves to a
# different card than the calendar draws.
_SEARCH_SQL = r"""
SELECT DISTINCT a.title_key
FROM calendar_airings a
JOIN calendar_titles t
  ON t.source = a.source AND t.media = a.media AND t.source_id = a.source_id
WHERE a.endpoint = ? AND t.title_fold LIKE ? ESCAPE '\'
LIMIT ?
"""

# The same three-level read the span path uses, addressed by group instead of by
# date range. One placeholder per key, which is why the caller's limit matters.
_BY_KEY_SQL = _READ_SQL.replace(
    "WHERE a.endpoint = ? AND a.air_date >= ? AND a.air_date < ?",
    "WHERE a.endpoint = ? AND a.title_key IN (__KEYS__)")


def like_needle(query: str) -> str:
    """`query` folded and wrapped for a LIKE, with the wildcards it may contain
    itself neutralised.

    A SEARCH BOX IS UNTRUSTED INPUT AND `%` IS A CHARACTER IN TITLES. Left
    unescaped, a query of `%` matches the whole table and one of `100%` silently
    matches far more than it should; both are ordinary things to type rather
    than attacks — this instance holds `100% Footy` and `The 1% Club (US)`. The
    backslash is escaped first so it cannot smuggle the escape character itself.
    """
    folded = fold_title(query)
    for char in ("\\", "%", "_"):
        folded = folded.replace(char, "\\" + char)
    return f"%{folded}%"


async def groups_matching(endpoint_key: str, query: str, limit: int) -> list[str]:
    """The `title_key`s on `endpoint_key` whose title matches `query`.

    MATCHED ON `title_fold`, the same folding a stored title was written with —
    case-folded and accent-stripped, so "pokemon" finds "Pokemon". The index on
    that column is what makes this a lookup rather than a scan.
    """
    rows = await db.fetch_all(
        _SEARCH_SQL, (endpoint_key, like_needle(query), limit))
    return [str(r["title_key"]) for r in rows]


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
    rows = await db.fetch_all(sql, (endpoint_key, *keys))
    return _in_fetch_order(rows)


async def slugs_for(title_keys) -> dict[str, dict[str, str]]:
    """`{title key: {"<source>_slug": name}}` for the titles named, and only
    those.

    A KEYED READ WHERE THERE USED TO BE A WALK, and the difference is the whole
    reason this exists. The tracker fills in the names it is missing from what
    the calendar already knows, and it did that by materialising EVERY stored
    airing and grouping them in memory to build an index of the lot. Measured on
    a live instance: 3.8 seconds to learn two names, 2.7 of them blocking the
    event loop, on every add and every remove — because a roster edit changes
    what is owed, which is half of the signature that guards the walk.

    `title_key` IS ALREADY THE ANSWER TO "WHICH TITLE IS THIS". It is
    `resolve_key` stringified, written onto every row at store time and indexed,
    which is exactly the identity the tracker keys its own rows by — so the
    question "what does the calendar call these few titles" is an indexed lookup
    on a handful of keys rather than a reduction over the whole table.

    READ PER SOURCE, NOT OFF A MERGE. A title both services list has two names
    and a merged id map can only carry one; each row here is one service's own
    record, so `simkl_slug` comes from Simkl's row and `trakt_slug` from Trakt's,
    with nothing to disambiguate.

    THE NAMESPACED SPELLING IS PREFERRED where a row has one, falling back to the
    bare `slug` that older rows carry — both mean the same thing, and taking the
    explicit one first keeps this from depending on the per-source reading
    staying correct for ever.
    """
    keys = [str(k) for k in dict.fromkeys(title_keys or ()) if k]
    if not keys:
        return {}
    # Chunked because SQLite has a bound-parameter ceiling (999 by default) and
    # the caller's list is however many names an account happens to owe.
    out: dict[str, dict[str, str]] = {}
    for start in range(0, len(keys), 400):
        chunk = keys[start:start + 400]
        rows = await db.fetch_all(
            "SELECT title_key, source, ids_json FROM calendar_titles "
            f"WHERE title_key IN ({', '.join('?' * len(chunk))})", tuple(chunk))
        for row in rows:
            ids = json.loads(row["ids_json"] or "{}") if row["ids_json"] else {}
            source = str(row["source"])
            value = ids.get(f"{source}_slug") or ids.get("slug")
            if value in (None, ""):
                continue
            # First writer wins, so two rows naming one title give a stable
            # answer rather than one that depends on the order they came back.
            out.setdefault(str(row["title_key"]), {}).setdefault(
                f"{source}_slug", str(value))
    return out


async def unenriched(source: str, id_namespace: str) -> set[tuple[int, str]]:
    """{(service id, media)} for stored titles still carrying no answer.

    IT NAMES THE ROWS A REBUILD IS FOR: ones where the ANSWER EXISTS in the
    per-title table and this row was never given it. That gap is ordinary, not
    exotic — a lookup that lands a moment after the row is written, an answer
    stored by a version of this app that did not yet project onto the calendar,
    or any row whose fill and whose drain crossed. Without this the drain cannot
    tell "nobody has looked this up" from "the answer is here and this row has
    not been handed it", and the second reads as the first being done.
    """
    rows = await db.fetch_all(
        "SELECT json_extract(ids_json, '$.' || ?) AS service_id, media "
        "FROM calendar_titles WHERE source = ? AND enriched = 0",
        (id_namespace, source))
    out: set[tuple[int, str]] = set()
    for row in rows:
        try:
            out.add((int(row["service_id"]), str(row["media"])))
        except (TypeError, ValueError):
            continue
    return out


async def apply_enrichment_many(source: str, items, now: int) -> int:
    """The same write for many titles, in ONE transaction.

    FOR THE REBUILD, NOT THE FETCH. A drain pass fetches a bounded batch and
    could write those one at a time; what needs this is the other case — titles
    whose answers are already stored and whose rows have not been given them.
    That happens in bulk, and a worker-thread hop per title (measured at ~2ms)
    would turn a few hundred local writes into most of a second on the event
    loop.
    """
    rows = [_enrichment_params(source, service_id, media, fields, now)
            for service_id, media, fields in items]
    if not rows:
        return 0

    def _work(conn) -> int:
        for params in rows:
            conn.execute(_UPDATE_ENRICHED, params)
        return len(rows)

    return await db.transaction(_work)


async def stored_titles(source: str, id_namespace: str, media: tuple[str, ...] | None = None,
                        ) -> dict[tuple[int, str], str]:
    """{(service id, media): title} for every title `source` names in the stored
    calendar.

    A PURE "WHAT DOES THE CALENDAR CURRENTLY NAME" QUESTION, with exactly one
    reason to change -- which is the division its predecessor drew and this keeps.
    Deciding which of these are still worth FETCHING belongs to the drain: a
    title already answered, one inside its failure backoff, and one whose stored
    answer came from a narrower extraction than the current one are three
    different exclusions, and only the drain can see the last of them. Filtering
    on `enriched` here would quietly retire EXTRACT_VERSION, because a row
    re-owed by a widened extraction is already marked answered.

    THIS IS THE FUNCTION THE WHOLE MODEL PAYS FOR. Its predecessor inflated every
    stored window on the instance and walked the groups -- measured at ~358ms
    against 101 windows, and called TWICE per drain pass, so an enrichment tick
    spent most of a second of event-loop time before it made a request. It is now
    an indexed read.

    KEYED ON THE SERVICE'S OWN NUMERIC ID, WHICH IS NOT `source_id`. A Simkl
    record's `id` is its SLUG where it has one (providers/simkl/calendar.py's
    `_record_id` prefers it), and the lookup this feeds is addressed by number.
    The number is in `ids_json`, so it is read from there rather than assumed of
    a column that usually holds something else.
    """
    sql = ("SELECT json_extract(ids_json, '$.' || ?) AS service_id, media, title "
           "FROM calendar_titles WHERE source = ?")
    params: list = [id_namespace, source]
    if media:
        sql += f" AND media IN ({', '.join('?' * len(media))})"
        params.extend(media)
    sql += " ORDER BY fetched_at DESC"
    owed: dict[tuple[int, str], str] = {}
    for row in await db.fetch_all(sql, tuple(params)):
        try:
            service_id = int(row["service_id"])
        except (TypeError, ValueError):
            continue  # a title this service never named in its own id space
        owed.setdefault((service_id, str(row["media"])), str(row["title"] or ""))
    return owed


# The columns an enrichment answer may write. Deliberately NOT every column: a
# lookup has nothing to say about `title_key`, the coordinates or `stored_at`,
# and listing what it may touch is what stops a future field being written by
# accident from a payload that happens to carry that name.
_ENRICHED_FIELDS = ("network", "country", "language", "certification", "status",
                    "overview", "runtime", "year")


async def apply_enrichment(source: str, service_id: int, media: str, fields: dict,
                           now: int) -> None:
    """Write what a per-title lookup learned onto the stored title, and mark it
    answered.

    THE DRAIN IS A WRITER OF THE ROW, WHICH IS THE WHOLE POINT. Enrichment used
    to be a side table overlaid at read; the objection to storing it was that a
    value baked in at fill would be frozen for the window's whole TTL and would
    make `enriched` a lie about what was stored. Both were true of a flag written
    once at fill and never revisited. Maintained here, `enriched` records what
    the row ACTUALLY CONTAINS, and the value is never older than the last drain
    pass rather than as old as the window.

    ADDRESSED BY THE SERVICE'S NUMERIC ID rather than by `source_id`, matching
    `stored_titles` -- and via ids_json for the same reason, since a Simkl row is
    filed under its slug.
    """
    await db.execute(_UPDATE_ENRICHED,
                     _enrichment_params(source, service_id, media, fields, now))


# `ids_json = json_patch(<learned>, ids_json)` MERGES AND LETS THE EXISTING WIN,
# which is the direction that matters. A lookup knows id spaces the calendar file
# never carries -- tvdb, mal, anidb -- and adding them is most of why the payload
# is worth keeping; but the calendar file's OWN tmdb is the one the fill matched
# on, and a lookup overwriting it would re-identify a title under cover of
# enriching it. Patching the learned map UNDER the stored one adds what is new and
# keeps every value already there.
_UPDATE_ENRICHED = (
    f"UPDATE calendar_titles SET {', '.join(f'{n} = ?' for n in _ENRICHED_FIELDS)}, "
    "genres_json = ?, ratings_json = ?, anime_type = ?, release_types_json = ?, "
    "ids_json = json_patch(?, ids_json), "
    "enriched = 1, fetched_at = ?, failed_at = NULL, fail_count = 0 "
    "WHERE source = ? AND media = ? AND json_extract(ids_json, '$.' || ?) = ?")


def enrichment_values(fields: dict) -> dict:
    """ONE READING OF WHAT A PER-TITLE LOOKUP'S PAYLOAD MEANS.

    THERE ARE TWO PLACES A LOOKUP'S ANSWER IS WRITTEN, and before this there were
    two readings of it. `enrich._apply` sets the fields on a RECORD, on the way
    into a fill; `_enrichment_params` below sets them on a stored ROW, when the
    drain answers for a title whose row already exists. Both had their own list
    of keys, and they drifted exactly as two copies of one fact do: this one read
    `ratings` where the payload says `rating`, so every title enriched through
    the row path lost its score while every title enriched through the fill path
    kept it. Measured on a live instance before the fix: 1,505 titles with a
    rating stored, 225 with it silently dropped, decided by nothing but which
    path happened to reach them first.

    So the payload is read HERE and nowhere else, and both writers take what this
    returns. A field added to a lookup is added once.
    """
    year = fields.get("year")
    rating = fields.get("rating")
    imdb = fields.get("imdb_rating")
    releases = fields.get("release_types_by_country")
    return {
        "genres": [str(g) for g in (fields.get("genres") or [])],
        "network": str(fields.get("network") or ""),
        "country": str(fields.get("country") or ""),
        "language": str(fields.get("language") or ""),
        "certification": str(fields.get("certification") or ""),
        "status": str(fields.get("status") or ""),
        "overview": str(fields.get("overview") or ""),
        "runtime": fields.get("runtime"),
        # Left at "" rather than guessed when the payload has no year — a row the
        # narrower extraction wrote has no key for it, and "nothing to say" is
        # not the same as a number.
        "year": year if isinstance(year, int) else "",
        "rating": float(rating) if isinstance(rating, (int, float)) else None,
        "imdb_rating": (float(imdb) if isinstance(imdb, (int, float)) else None),
        "anime_type": str(fields.get("anime_type") or ""),
        "release_types_by_country": dict(releases) if isinstance(releases, dict) else {},
        "ids": {k: v for k, v in (fields.get("ids") or {}).items()
                if v not in (None, "")},
    }


def _enrichment_params(source: str, service_id: int, media: str, fields: dict,
                       now: int) -> tuple:
    value = enrichment_values(fields)
    # UNDER THE SOURCE'S OWN NAME, exactly as `_title_values` does it for a
    # record — one number shown under one service's mark, never a bare figure.
    ratings = {} if value["rating"] is None else {source: value["rating"]}
    if value["imdb_rating"] is not None:
        ratings["imdb"] = value["imdb_rating"]
    return (*(value[name] for name in _ENRICHED_FIELDS),
            json.dumps(value["genres"]),
            json.dumps(ratings),
            value["anime_type"],
            json.dumps(value["release_types_by_country"]),
            json.dumps(value["ids"]),
            now, source, media, _ID_NAMESPACE.get(source, source), service_id)


# Which id namespace each source is addressed by when a lookup answers for it.
# Named here beside the two functions that use it so the pair cannot disagree.
_ID_NAMESPACE = {"simkl": "simkl", "trakt": "trakt", "tmdb": "tmdb"}


# HOW LONG A STORED AIRING IS KEPT AT ALL, as opposed to how fresh it has to be
# to serve a request -- two clocks, deliberately different fields, because "this
# is stale, refresh it when somebody touches it" and "delete this" are different
# instructions and one number cannot mean both.
#
# SIX MONTHS, FLAT, EVERY SOURCE. TMDB's terms forbid caching anything obtained
# from them for longer than six months, so the ceiling exists whether or not this
# design wants one; applying it to everything rather than to TMDB rows alone
# costs almost nothing and removes a per-source branch nobody would remember to
# keep correct. Simkl imposes no ceiling and Trakt's caching guide states none.
#
# AND THE TIERS MAKE IT A NON-EVENT: anything with traffic refreshes far inside
# six months, so the only rows this deletes are ones nobody has touched in half a
# year, which refetch on the next visit. Ageing out and refetching IS how a
# change is eventually noticed.
#
# ONE HONEST REGRESSION, RECORDED RATHER THAN DISCOVERED. The blob this replaces
# was swept at app/cache.py's RETAIN_SECONDS plus TTL_GRACE_SECONDS -- nine
# months in total, deliberately, because a public share link never refetches and
# must go on rendering whatever it has. A share link to a month between six and
# nine months old renders today and will render EMPTY under this. Narrow, real,
# and a decision rather than an oversight.
RETAIN_SECONDS = 180 * 24 * 60 * 60


async def sweep(now: int | None = None) -> int:
    """Delete calendar rows nothing has touched inside the retention window.

    ORDER IS THE WHOLE OF THE CARE HERE. Airings go first; a title or an episode
    is removed only once it is old AND NOTHING STILL AIRS IT. Deleting a title on
    age alone would strand every airing still pointing at it — the read's LEFT
    JOIN would hand back a card with no name, which is worse than either keeping
    the row or dropping the airing with it.

    THE EPISODE CHECK IS PER COORDINATE, NOT PER TITLE, and that is deliberate: a
    series in its fifth season stops carrying rows for its first, while a title
    still airing keeps everything that still airs. Matching on the title alone
    would have been one clause shorter and would have grown without bound.
    """
    cutoff = (db.now() if now is None else now) - RETAIN_SECONDS

    def _work(conn) -> int:
        removed = conn.execute(
            "DELETE FROM calendar_airings WHERE stored_at <= ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM calendar_coverage WHERE stored_at <= ?", (cutoff,))
        conn.execute(
            "DELETE FROM calendar_titles WHERE fetched_at <= ? AND NOT EXISTS ("
            "  SELECT 1 FROM calendar_airings a"
            "  WHERE a.source = calendar_titles.source"
            "    AND a.media = calendar_titles.media"
            "    AND a.source_id = calendar_titles.source_id)", (cutoff,))
        conn.execute(
            "DELETE FROM calendar_episodes WHERE fetched_at <= ? AND NOT EXISTS ("
            "  SELECT 1 FROM calendar_airings a"
            "  WHERE a.source = calendar_episodes.source"
            "    AND a.media = calendar_episodes.media"
            "    AND a.source_id = calendar_episodes.source_id"
            "    AND a.season = calendar_episodes.season"
            "    AND a.episode_number = calendar_episodes.number)", (cutoff,))
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
# level 2 — what a source says about ONE EPISODE
# ---------------------------------------------------------------------------

# HOW LONG AN ANSWERED EPISODE MAY GO UNREFETCHED, by how long ago it AIRED.
# Keyed on the episode's own air date rather than on when it was looked up,
# because that is what the accuracy of the answer actually tracks: episode facts
# are CORRECTED AFTER AIR at least as often as they are published before it. A
# mystery-box show ships "Episode 7" as the title and a placeholder overview,
# and the real ones arrive once people have watched — so the week AFTER an
# episode airs is when a re-read is worth most, which a "refetch old rows less
# often" rule keyed on fetch time gets exactly backwards.
#
# These are NOT the span tiers in cache.py and must not be merged with them.
# Those describe how often a source REGENERATES A FILE; these describe how long
# a fact stays wrong after somebody fixes it. Same shape, different reason to
# change.
#
# (days since this episode aired, seconds its row may go unrefetched)
_EPISODE_TIERS = (
    (-36500, 24 * 60 * 60),   # still upcoming: a day, and details firm up late
    (0, 24 * 60 * 60),        # aired within the week: a day, the correction window
    (7, 30 * 24 * 60 * 60),   # settled: a month
    (183, 90 * 24 * 60 * 60),  # older than retention sweeps anyway: a quarter
)


def episode_stale_after(air_ts: float | None, now: int) -> int:
    """When a just-answered episode row falls due again.

    An airing with no stated time is treated as upcoming — the fast tier. It is
    the cheap direction to be wrong in: the alternative parks a row nobody can
    date on the slow tier for a month, and an undated row is far more likely to
    be an imminent episode the feed has not pinned down than a settled one.
    """
    try:
        aired_days = (now - float(air_ts)) / 86400.0
    except (TypeError, ValueError):
        aired_days = 0.0
    ttl = _EPISODE_TIERS[0][1]
    for days, seconds in _EPISODE_TIERS:
        if aired_days >= days:
            ttl = seconds
    return now + ttl


# WHICH SEASONS THE LEVEL-2 DRAIN STILL OWES WORK ON. Written once and shared by
# the drain's batch and by the count the page shows, because those two answering
# differently is the exact failure that makes a backlog readout untrustworthy:
# a number that never reaches zero while the drain insists it is finished tells
# a reader nothing except that one of the two is lying.
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
    "JOIN calendar_titles t ON t.source = a.source AND t.media = a.media "
    "                      AND t.source_id = a.source_id "
    "LEFT JOIN calendar_episodes e ON e.source = a.source AND e.media = a.media "
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
    Asking per episode would be one request per airing, which is the cost this
    level was designed to avoid paying.

    A ROW WRITTEN BY THE FILL IS NOT AN ANSWER. The feed gives an episode title
    and nothing else, so `fetched_at = 0` marks a stub: present, keyed, and still
    owed everything a lookup would add. Reading "has a row" as "has been looked
    up" would leave every episode with a title and no runtime for ever.

    AN AIRING WITH NO STATED EPISODE NUMBER IS NOT OWED, and leaving it in was a
    live defect: `store_episodes` cannot key a row for a coordinate the source
    never gave, so such an airing came back owed on EVERY pass, and the drain
    re-read those seasons once a minute for ever. The symptom was a heartbeat
    that logged a screenful of season lookups while nobody was browsing.

    NEVER-LOOKED-UP SEASONS GO FIRST, and only then the ones falling due. A
    season that has never been answered shows placeholder facts to somebody
    RIGHT NOW; a due one shows facts that were true when they were fetched. With
    a fixed batch size the two compete for the same tick, and serving the
    re-reads first would let a large calendar starve its own first pass.
    """
    rows = await db.fetch_all(
        _OWED_SEASONS + " ORDER BY answered_at ASC, newest DESC LIMIT ?",
        (id_namespace, source, now, limit))
    out: list[tuple[int, str, int]] = []
    for row in rows:
        try:
            out.append((int(row["service_id"]), str(row["media"]), int(row["season"])))
        except (TypeError, ValueError):
            continue  # a title this service never named in its own id space
    return out


async def store_episodes(source: str, service_id: int, media: str, season: int,
                         episodes: list[dict], now: int) -> int:
    """Write one season's episode facts, addressed by the service's own id.

    A SEASON WITH NO EPISODES STILL COUNTS AS ANSWERED, which is why the stub
    rows are stamped even when the list is empty: without it a season Trakt has
    nothing for is owed again on every pass for ever, which is the same
    starvation the enrichment drain's failure rows exist to prevent.

    WHEN EACH ROW FALLS DUE AGAIN IS DECIDED PER EPISODE, from that episode's own
    latest AIRING rather than from the season's or from the lookup's clock — a
    season part-aired sits on both sides of the correction window at once, and
    one date for the whole season would put the aired half on the slow tier or
    the unaired half on the fast one.
    """
    rows = await db.fetch_all(
        "SELECT source_id FROM calendar_titles "
        "WHERE source = ? AND media = ? AND json_extract(ids_json, '$.' || ?) = ?",
        (source, media, _ID_NAMESPACE.get(source, source), service_id))
    source_ids = [str(r["source_id"]) for r in rows]
    if not source_ids:
        return 0
    by_number = {int(e["number"]): e for e in episodes if e.get("number") is not None}

    def _work(conn) -> int:
        written = 0
        for source_id in source_ids:
            # Every airing of this season, so a stub the fill wrote is answered
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
                due = episode_stale_after(air_ts, now)
                fields = by_number.get(int(number)) or {}
                conn.execute(
                    "INSERT INTO calendar_episodes "
                    "(source, media, source_id, season, number, title, overview, "
                    " still, first_aired, episode_type, runtime, rating, votes, "
                    " fetched_at, stale_after) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(source, media, source_id, season, number) DO UPDATE SET "
                    # THE LOOKUP WINS EXCEPT WHERE IT HAS NOTHING TO SAY. The fill
                    # already stored an episode title from the calendar feed, and
                    # a season Trakt could not name must not blank it.
                    "  title = CASE WHEN excluded.title != '' THEN excluded.title "
                    "               ELSE calendar_episodes.title END, "
                    "  overview = excluded.overview, first_aired = excluded.first_aired, "
                    "  episode_type = excluded.episode_type, runtime = excluded.runtime, "
                    "  rating = excluded.rating, votes = excluded.votes, "
                    "  fetched_at = excluded.fetched_at, "
                    "  stale_after = excluded.stale_after",
                    (source, media, source_id, season, int(number),
                     str(fields.get("title") or ""), str(fields.get("overview") or ""),
                     str(fields.get("first_aired") or ""),
                     str(fields.get("episode_type") or ""),
                     fields.get("runtime"), fields.get("rating"), fields.get("votes"),
                     now, due))
                written += 1
        return written

    return await db.transaction(_work)


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
    rows = await db.fetch_all(
        "SELECT DISTINCT source FROM calendar_airings "
        "WHERE endpoint = ? AND air_date >= ? AND air_date < ?",
        (endpoint_key, span_start.isoformat(), span_end.isoformat()))
    return {str(r["source"]) for r in rows}


async def span_stored_at(endpoint_key: str, span_start: date) -> int | None:
    """When this span was last stored, or None if it never has been.

    THE SCHEDULE'S CLOCK, and it is stored rather than remembered so a restart
    does not reset it. `min` across the sources rather than `max`: a span is only
    as fresh as its stalest source, so one source refilled an hour ago must not
    make the whole span look current while another has not been asked in a week.
    """
    row = await db.fetch_one(
        "SELECT MIN(stored_at) AS at FROM calendar_coverage "
        "WHERE endpoint = ? AND span_start = ?",
        (endpoint_key, span_start.isoformat()))
    return None if row is None or row["at"] is None else int(row["at"])
