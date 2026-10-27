"""What each service says about a title and its episodes: the store's rows.

ONE ROW PER (SERVICE, TITLE) AND PER (SERVICE, EPISODE), every descriptive field
and its own expiry. These began as the calendar's own tables and were renamed in
place (migration 47) when they became every feature's: the tracker, the rankings
board and the details panel describe the same titles the calendar does, and a
second copy of a title's facts per feature is how one title came to be described
five ways.

THE FACTS ARE SPLIT BY HOW LONG THEY STAY TRUE, which is why a title and its
episodes are separate rows. Measured across 4,330 (source, title) pairs with more
than one airing: runtime disagrees on 0.1% of them, certification 0.2%, genres
0.4%, network 0.8%, language and country 0.0%. Those are TITLE facts, and storing
a copy on every airing is what let one show's Tuesday and Thursday disagree.
`episode_title` disagrees on 95.5%, the opposite finding.

NOTHING HERE IS PER-VIEWER. Every service's rows are stored whole; which one a
viewer sees is decided at READ against their own preferences
(app/calendar/resolve.py), so one viewer's choice never reaches another's view.

A ROW IS NOT A TITLE. Which title a row belongs to is its `title_key`, decided by
app/metadata/identity.py's one resolution and moved by it when the title is
re-keyed; nothing here derives a key of its own.
"""
from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import db, providers
from ..media import artwork
from ..providers.base import Record, Source
from . import holders, identity

logger = logging.getLogger(__name__)

# The tables, named once. The calendar's own reads join its airings to these and
# name them through here rather than spelling them again.
TITLES = "title_metadata"
SEASONS = "season_metadata"
EPISODES = "episode_metadata"
# The index over folded titles, which the calendar's search names in INDEXED BY.
FOLD_INDEX = "ix_title_metadata_fold"

# THE ORDER SOURCES ARE DECLARED IN, as a rank — the order a fill visits them and
# therefore the order everything downstream was built against. NOT alphabetical:
# sorting by name would put Simkl ahead of Trakt and quietly reverse which
# service's spelling of an id is kept first.
SOURCE_ORDER = {str(s): i for i, s in enumerate(Source)}

# Which id namespace each source is addressed by when a lookup answers for it.
ID_NAMESPACE = {"simkl": "simkl", "trakt": "trakt", "tmdb": "tmdb"}


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
# the clock a ceiling reads
# ---------------------------------------------------------------------------

# THE WORDS A ROW HOLDS: what a service said about a title, as opposed to the
# keys the row is filed by (its coordinates, `title_key`, `ids_json` — an id is
# what a title IS — and `detail_url`, built from the service's own id) and the
# clocks it is kept by (`NOT_WORDS`). tests/metadata/test_retention.py fails on a
# column of these tables that neither names, because a word nobody listed is a
# word `ceiling_clock` cannot see left behind.
WORDS = {
    TITLES: ("title", "year", "network", "country", "language", "certification", "status",
             "overview", "poster", "runtime", "genres_json", "ratings_json", "anime_type",
             "release_types_json", "keywords_json", "first_aired", "homepage",
             "season_count", "cast_json", "videos_json", "logo", "collection_json",
             "next_episode_json"),
    SEASONS: ("name", "overview", "episode_count", "premiere", "air_dates_json", "poster",
              "cast_json", "videos_json"),
    EPISODES: ("title", "overview", "still", "first_aired", "episode_type", "runtime",
               "rating", "votes"),
}
NOT_WORDS = {
    TITLES: ("source", "media", "source_id", "title_key", "title_fold", "ids_json",
             "detail_url", "enriched", "fetched_at", "stale_after", "failed_at", "fail_count"),
    SEASONS: ("source", "media", "source_id", "season", "fetched_at", "stale_after",
              "failed_at", "fail_count"),
    EPISODES: ("source", "media", "source_id", "season", "number", "fetched_at",
               "stale_after"),
}


def ceiling_sources() -> tuple[str, ...]:
    """Every registered service whose terms bound how long what it supplies may
    be kept (`Capabilities.retain_seconds`), by name."""
    return tuple(str(source) for source, provider in providers.registered().items()
                 if provider.capabilities.retain_seconds is not None)


def _held(table: str, column: str) -> str:
    """SQL: the stored row's `column` holds a word — anything but nothing, an
    empty string, an empty list or an empty map."""
    return (f"({table}.{column} IS NOT NULL "
            f"AND {table}.{column} NOT IN ('', '[]', '{{}}'))")


def ceiling_clock(table: str, rewrites: Mapping[str, str | None], *,
                  new: str = "excluded.fetched_at") -> str:
    """The SQL a write onto `table` sets `fetched_at` to — `new`, the moment the
    service answered, or the row's older clock where the write leaves an older
    word in place.

    A CEILING IS ABOUT EVERY WORD, AND A ROW HAS ONE CLOCK. A service with a
    ceiling (TMDB: nothing it supplied may be kept past six months) is read by
    `sweep_past_ceilings` from `fetched_at`, so for its rows that clock must be
    the age of the OLDEST word the row holds. Writers rewrite parts of a row —
    a calendar listing its name and picture, a season list a season's name and
    count, a description everything it states — and a partial write that
    renewed the clock would keep the words it left behind past the ceiling: a
    season's cast described once and re-listed every few days would never age.
    So a write renews the clock only when it rewrites every word the row holds,
    and otherwise keeps the older one. A row two kinds of answer wrote goes at
    its oldest word's ceiling and the next view of it builds it again
    (app/metadata/refresh.py) — a few requests every six months, against a
    word kept for ever.

    `rewrites` names each word the write rewrites, with the SQL condition under
    which it does NOT (None: always) — a listing that keeps the stored picture
    when it states none, say. Every word not named is left. An episode row at
    `fetched_at` 0 is a listing's stub no answer has reached, which no clock
    describes; the answer's clock is its first. A service with no ceiling is
    always given `new`: for it the clock says when it last answered, which is
    what its own readers read.

    `new` appears twice; an UPDATE passing it as `?` passes the moment twice.
    """
    left = [_held(table, column) if column not in rewrites
            else f"(({rewrites[column]}) AND {_held(table, column)})"
            for column in WORDS[table]
            if column not in rewrites or rewrites[column] is not None]
    names = ", ".join(f"'{name}'" for name in ceiling_sources())
    condition = (f"{table}.source IN ({names}) AND {table}.fetched_at > 0 "
                 f"AND ({' OR '.join(left)})") if names and left else "0"
    return f"CASE WHEN {condition} THEN MIN({table}.fetched_at, {new}) ELSE {new} END"


def kept_clock(source: str, table: str, row: Mapping[str, Any] | None,
               rewrites: Iterable[str], now: int) -> int:
    """`ceiling_clock` for a write made from Python over a row it has read
    (app/metadata/descriptions.py's `write`): `now`, or the row's older clock
    where the write leaves a word of a ceiling-bound service in place."""
    if row is None or str(source) not in ceiling_sources():
        return now
    stored = int(row["fetched_at"] or 0)
    rewritten = set(rewrites)
    if stored > 0 and any(row[column] not in (None, "", "[]", "{}")
                          for column in WORDS[table] if column not in rewritten):
        return min(stored, now)
    return now


def as_year(stored) -> int | str:
    """A stored year as the int it was, or whatever it is if it is not one.

    Not every source states a plain year -- a range or a placeholder comes back
    as itself rather than being coerced into a number it never was.
    """
    text = str(stored or "")
    return int(text) if text.isdigit() else text


# ---------------------------------------------------------------------------
# a service's listing, written
# ---------------------------------------------------------------------------

# Title columns a record can speak to, in the order the upsert states them.
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


# A LISTING MAY NOT DEMOTE WHAT ENRICHMENT LEARNED. The calendar feed and the
# per-title drain write the same row, and the feed knows less: Simkl's CDN files
# carry no genres, network, country or certification at all. An unconditional
# upsert would therefore erase a drained answer every time the month refreshed,
# and the next drain would fetch it again -- a loop that looks like enrichment
# never sticking. So the feed's write keeps the coordinates and the poster, which
# it is authoritative for, and leaves every enriched field alone once `enriched`
# is set.
_UPSERT_TITLE = f"""
INSERT INTO {TITLES}
    (source, media, source_id, title_key, {', '.join(_TITLE_FIELDS)},
     fetched_at, stale_after)
VALUES ({', '.join(['?'] * (4 + len(_TITLE_FIELDS) + 2))})
ON CONFLICT(source, media, source_id) DO UPDATE SET
    title_key  = excluded.title_key,
    title      = excluded.title,
    title_fold = excluded.title_fold,
    detail_url = CASE WHEN excluded.detail_url != '' THEN excluded.detail_url
                      ELSE {TITLES}.detail_url END,
    poster     = CASE WHEN excluded.poster != '' THEN excluded.poster
                      ELSE {TITLES}.poster END,
    ids_json   = excluded.ids_json,
    year       = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.year
                      ELSE excluded.year END,
    network    = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.network
                      ELSE excluded.network END,
    country    = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.country
                      ELSE excluded.country END,
    language   = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.language
                      ELSE excluded.language END,
    certification = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.certification
                         ELSE excluded.certification END,
    status     = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.status
                      ELSE excluded.status END,
    overview   = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.overview
                      ELSE excluded.overview END,
    runtime    = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.runtime
                      ELSE excluded.runtime END,
    genres_json = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.genres_json
                       ELSE excluded.genres_json END,
    ratings_json = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.ratings_json
                        ELSE excluded.ratings_json END,
    anime_type = CASE WHEN {TITLES}.enriched = 1 THEN {TITLES}.anime_type
                      ELSE excluded.anime_type END,
    enriched   = MAX({TITLES}.enriched, excluded.enriched),
"""

# What a listing rewrites of a title row: its name always, its picture when it
# states one, and the enriched fields until a lookup has answered for them.
_LISTING_REWRITES: dict[str, str | None] = {
    "title": None, "poster": "excluded.poster = ''",
    **{name: f"{TITLES}.enriched = 1"
       for name in ("year", "network", "country", "language", "certification", "status",
                    "overview", "runtime", "genres_json", "ratings_json", "anime_type")}}


def _upsert_title_sql() -> str:
    return _UPSERT_TITLE + f"    fetched_at = {ceiling_clock(TITLES, _LISTING_REWRITES)}\n"
# `release_types_json` IS ABSENT FROM THE UPDATE ABOVE ON PURPOSE, and it is the
# only enrichment-owned column that is, so it reads like an oversight and is
# worth stating. A column left out of DO UPDATE SET keeps whatever it holds, so
# the effect is the strongest possible guard: a re-fill can never touch it.
#
# THAT IS SAFE BECAUSE IT HAS EXACTLY ONE WRITER. `_UPDATE_ENRICHED` below is the
# only statement that sets it, and it sets `enriched = 1` in the same breath, so
# "this row has release types" implies "this row is enriched" by construction
# rather than by observation. Trakt's half of the same question never writes here
# at all — it keeps its own table and applies its answer as a read-time overlay
# (calendar/enrich.overlay_releases), which is why there is no second writer.
#
# WHAT WOULD CHANGE THAT: a provider filling `release_types_by_country` on a
# CALENDAR record, which `Record` already permits and none does today. Then the
# first fill would store it, every later one would silently ignore it, and this
# column would need the same `CASE WHEN enriched = 1` its neighbours carry.

# THE LISTING'S HALF OF AN EPISODE ROW. A calendar entry names the episode's
# title and nothing else about it -- no overview, no runtime, no rating, no still
# -- so a listing writes a STUB and the per-episode drain fills in the rest later.
#
# IT NEVER REPLACES A TITLE WITH AN EMPTY ONE, which is the only demotion a
# listing can cause on its own. It CAN still replace a real title with the
# placeholder Trakt sometimes serves ("Episode 1"): telling those apart by shape
# would be guessing, and the drain reading the service's own episode list is the
# authority that settles it.
_UPSERT_EPISODE_STUB = f"""
INSERT INTO {EPISODES}
    (source, media, source_id, season, number, title, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, 0)
ON CONFLICT(source, media, source_id, season, number) DO UPDATE SET
    -- ONCE A LOOKUP HAS ANSWERED, IT OWNS THE TITLE. Both writers reach this row:
    -- a listing, carrying whatever the calendar feed spelled, and the episode
    -- drain, carrying a whole season read from the service's own episode list.
    -- The second knows more, so letting the feed overwrite it on the next refill
    -- would trade a real answer for a thinner one, once a day, for ever. Before a
    -- lookup has answered (`fetched_at = 0`) the feed's title is the only one
    -- there is and is kept.
    title = CASE WHEN {EPISODES}.fetched_at > 0 THEN {EPISODES}.title
                 WHEN excluded.title != '' THEN excluded.title
                 ELSE {EPISODES}.title END
"""
# `fetched_at` IS NOT TOUCHED ON CONFLICT, which is the same rule as `enriched`
# one level up: A LISTING MAY NOT DEMOTE WHAT A LOOKUP LEARNED. The stub's 0 means
# "still owed a lookup", so writing it over an answered row would push it back to
# owed on every refill — observed before the guard: the episode drain reported 25
# seasons filled every single minute, for ever, because each refresh handed back
# the work the last one had just finished.


def write_listing(conn: db.Connection, record: Record, title_key: str, *, now: int,
                  stale_after: int) -> None:
    """One service's listing of a title, written: its title row under `title_key`,
    and the stub of the episode it names. Inside the caller's transaction.

    ONLY A STATED COORDINATE CAN KEY AN EPISODE. An entry that names no season or
    number is not identifying an episode this store can file anything under, and
    inventing a key for it would collide two different episodes of one show.
    Measured over 45,827 stored records: 7,387 state no coordinate and NOT ONE of
    them carries an episode title, so nothing is lost by having nowhere to put one.
    """
    conn.execute(_upsert_title_sql(), (str(record.source), str(record.media),
                                       str(record.id), title_key, *_title_values(record),
                                       now, stale_after))
    # A TITLE WITH NO TMDB ID HAS ITS LISTING'S PICTURE FILED UNDER ITS KEY, as
    # the store knows it only here (app/media/artwork.py's `title_posters`). One
    # with a tmdb id is filed by the calendar's fill, which records every
    # picture a fetch brought (app/calendar/cache.py's `_poster_sighting`).
    if record.poster and identity.tmdb_of(title_key) is None:
        artwork.file_sighting(conn, str(record.media), None, title_key, str(record.source),
                              str(record.poster), now)
    if record.season is not None and record.episode_number is not None:
        conn.execute(_UPSERT_EPISODE_STUB, (
            str(record.source), str(record.media), str(record.id), int(record.season),
            int(record.episode_number), str(record.episode_title or "")))


def filed_key(conn: db.Connection, record: Record) -> str | None:
    """The key a service's row for this record's title is filed under now, or None
    when the service has no row for it yet."""
    row = conn.execute(
        f"SELECT title_key FROM {TITLES} WHERE source = ? AND media = ? AND source_id = ?",
        (str(record.source), str(record.media), str(record.id))).fetchone()
    return None if row is None else str(row[0])


# ---------------------------------------------------------------------------
# what a per-title lookup learned
# ---------------------------------------------------------------------------

# The columns an enrichment answer may write. Deliberately NOT every column: a
# lookup has nothing to say about `title_key`, the coordinates or `stored_at`,
# and listing what it may touch is what stops a future field being written by
# accident from a payload that happens to carry that name.
_ENRICHED_FIELDS = ("network", "country", "language", "certification", "status",
                    "overview", "runtime", "year")

# `ids_json = json_patch(<learned>, ids_json)` MERGES AND LETS THE EXISTING WIN,
# which is the direction that matters. A lookup knows id spaces the calendar file
# never carries -- tvdb, mal, anidb -- and adding them is most of why the payload
# is worth keeping; but the calendar file's OWN tmdb is the one the listing
# matched on, and a lookup overwriting it would re-identify a title under cover of
# enriching it. Patching the learned map UNDER the stored one adds what is new and
# keeps every value already there.
def _update_enriched_sql() -> str:
    """The enrichment UPDATE — `ceiling_clock` with the moment passed as `?`,
    twice (`_enrichment_params`)."""
    rewrites = dict.fromkeys(
        (*_ENRICHED_FIELDS, "genres_json", "ratings_json", "anime_type", "release_types_json"))
    return (
        f"UPDATE {TITLES} SET {', '.join(f'{n} = ?' for n in _ENRICHED_FIELDS)}, "
        "genres_json = ?, ratings_json = ?, anime_type = ?, release_types_json = ?, "
        "ids_json = json_patch(?, ids_json), "
        f"enriched = 1, fetched_at = {ceiling_clock(TITLES, rewrites, new='?')}, "
        "failed_at = NULL, fail_count = 0 "
        "WHERE source = ? AND media = ? AND json_extract(ids_json, '$.' || ?) = ?")


def enrichment_values(fields: dict) -> dict:
    """ONE READING OF WHAT A PER-TITLE LOOKUP'S PAYLOAD MEANS.

    THERE ARE TWO PLACES A LOOKUP'S ANSWER IS WRITTEN, and before this there were
    two readings of it. calendar/enrich.py sets the fields on a RECORD, on the way
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
            now, now, source, media, ID_NAMESPACE.get(source, source), service_id)


def _enrich_and_settle(conn: db.Connection, params: tuple, *, now: int) -> None:
    """One answer written onto its rows, and THE IDS IT LEARNED TAUGHT TO THE
    TITLE, in the same transaction.

    THIS IS WHERE A KEY USED TO GO STALE. A lookup adds ids the listing never
    carried — a Simkl title with only an anidb id learns its tvdb, a tvdb-only one
    its tmdb — and the row used to keep the key it was filed under while its ids
    named another. Measured on the development copy: 1,256 stored titles in that
    state, each one a card that could not join another service's listing of the
    same show. Settling the row's ids against its key re-keys the title at the
    moment it learns enough to, everywhere it is held.
    """
    conn.execute(_update_enriched_sql(), params)
    source, media, namespace, service_id = params[-4:]
    rows = conn.execute(
        f"SELECT title_key, ids_json, source_id FROM {TITLES} "
        "WHERE source = ? AND media = ? AND json_extract(ids_json, '$.' || ?) = ?",
        (source, media, namespace, service_id)).fetchall()
    for row in rows:
        identity.settle(conn, media, json.loads(row["ids_json"] or "{}"),
                        filed_under=str(row["title_key"]),
                        fallback=identity.per_source_key(source, row["source_id"]), now=now)


async def apply_enrichment(source: str, service_id: int, media: str, fields: dict,
                           now: int) -> None:
    """Write what a per-title lookup learned onto the stored title, and mark it
    answered.

    THE DRAIN IS A WRITER OF THE ROW, WHICH IS THE WHOLE POINT. Enrichment used
    to be a side table overlaid at read; the objection to storing it was that a
    value baked in at fill would be frozen for the window's whole TTL and would
    make `enriched` a lie about what was stored. Maintained here, `enriched`
    records what the row ACTUALLY CONTAINS, and the value is never older than the
    last drain pass rather than as old as the window.

    ADDRESSED BY THE SERVICE'S NUMERIC ID rather than by `source_id`, via ids_json
    — a Simkl row is filed under its slug.
    """
    params = _enrichment_params(source, service_id, media, fields, now)
    await db.transaction(lambda conn: _enrich_and_settle(conn, params, now=now))


async def apply_enrichment_many(source: str, items, now: int) -> int:
    """The same write for many titles, in ONE transaction.

    FOR THE REBUILD, NOT THE FETCH: titles whose answers are already stored and
    whose rows have not been given them. That happens in bulk, and a worker-thread
    hop per title (measured at ~2ms) would turn a few hundred local writes into
    most of a second on the event loop.
    """
    rows = [_enrichment_params(source, service_id, media, fields, now)
            for service_id, media, fields in items]
    if not rows:
        return 0

    def _work(conn) -> int:
        for params in rows:
            _enrich_and_settle(conn, params, now=now)
        return len(rows)

    return await db.transaction(_work)


async def unenriched(source: str, id_namespace: str,
                     media: tuple[str, ...] | None = None) -> dict[tuple[int, str], str]:
    """{(service id, media): title} for stored titles still carrying no answer —
    the rows a service's enrichment drain owes, read through the index on
    `enriched` rather than out of every row the service has.

    THIS IS THE DRAIN'S WHOLE WORKLIST, and it used to be every title the service
    had a row for, each one's stored answer read back and decompressed every tick
    only to find most of them already answered (25,123 Simkl titles on the
    development copy, ~0.7 s of worker time a minute). A row that IS answered is
    kept current by being viewed (app/metadata/refresh.py), not by being re-read
    on a clock.

    IT ALSO NAMES THE ROWS A REBUILD IS FOR: ones where the ANSWER EXISTS in the
    per-title table and this row was never given it — a lookup that lands a
    moment after the row is written, or any row whose listing and whose drain
    crossed. Without this the drain cannot tell "nobody has looked this up" from
    "the answer is here and this row has not been handed it".
    """
    sql = (f"SELECT json_extract(ids_json, '$.' || ?) AS service_id, media, title "
           f"FROM {TITLES} WHERE enriched = 0 AND source = ?")
    params: list = [id_namespace, source]
    if media:
        sql += f" AND media IN ({', '.join('?' * len(media))})"
        params.extend(media)
    sql += " ORDER BY fetched_at DESC"
    out: dict[tuple[int, str], str] = {}
    for row in await db.fetch_all(sql, tuple(params)):
        try:
            key = (int(row["service_id"]), str(row["media"]))
        except (TypeError, ValueError):
            continue  # a title this service never named in its own id space
        out.setdefault(key, str(row["title"] or ""))
    return out


async def stored_titles(source: str, id_namespace: str, media: tuple[str, ...] | None = None,
                        ) -> dict[tuple[int, str], str]:
    """{(service id, media): title} for every title `source` has a row for.

    A PURE "WHAT DOES THE STORE NAME" QUESTION, with exactly one reason to change.
    Deciding which of these are still worth FETCHING belongs to the drain: a title
    already answered, one inside its failure backoff, and one whose stored answer
    came from a narrower extraction than the current one are three different
    exclusions, and only the drain can see the last of them.

    KEYED ON THE SERVICE'S OWN NUMERIC ID, WHICH IS NOT `source_id`. A Simkl
    record's `id` is its SLUG where it has one, and the lookup this feeds is
    addressed by number — so the number is read out of `ids_json`.
    """
    sql = (f"SELECT json_extract(ids_json, '$.' || ?) AS service_id, media, title "
           f"FROM {TITLES} WHERE source = ?")
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


async def source_ids_for(source: str, media: str, service_id: int) -> list[str]:
    """Every `source_id` a service's rows carry for one of its own numeric ids —
    usually one, but a Simkl row filed under its slug and one under its number
    are the same title."""
    rows = await db.fetch_all(
        f"SELECT source_id FROM {TITLES} "
        "WHERE source = ? AND media = ? AND json_extract(ids_json, '$.' || ?) = ?",
        (source, media, ID_NAMESPACE.get(source, source), service_id))
    return [str(r["source_id"]) for r in rows]


# ---------------------------------------------------------------------------
# what a service says about ONE EPISODE
# ---------------------------------------------------------------------------

# HOW LONG AN ANSWERED EPISODE MAY GO UNREFETCHED, by how long ago it AIRED.
# Keyed on the episode's own air date rather than on when it was looked up,
# because that is what the accuracy of the answer actually tracks: episode facts
# are CORRECTED AFTER AIR at least as often as they are published before it. A
# mystery-box show ships "Episode 7" as the title and a placeholder overview,
# and the real ones arrive once people have watched — so the week AFTER an
# episode airs is when a re-read is worth most.
#
# These are NOT the calendar's span tiers (calendar/cache.py) and must not be
# merged with them. Those describe how often a source REGENERATES A FILE; these
# describe how long a fact stays wrong after somebody fixes it.
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


_UPSERT_EPISODE_ANSWER = f"""
INSERT INTO {EPISODES}
    (source, media, source_id, season, number, title, overview, still, first_aired,
     episode_type, runtime, rating, votes, fetched_at, stale_after)
VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, media, source_id, season, number) DO UPDATE SET
    -- THE LOOKUP WINS EXCEPT WHERE IT HAS NOTHING TO SAY. A listing already stored
    -- an episode title from the calendar feed, and a season the service could not
    -- name must not blank it.
    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE {EPISODES}.title END,
    overview = excluded.overview, first_aired = excluded.first_aired,
    episode_type = excluded.episode_type, runtime = excluded.runtime,
    rating = excluded.rating, votes = excluded.votes, stale_after = excluded.stale_after,
"""

_EPISODE_ANSWER_REWRITES: dict[str, str | None] = {
    "title": "excluded.title = ''", "overview": None, "first_aired": None,
    "episode_type": None, "runtime": None, "rating": None, "votes": None}


def write_episode_answer(conn: db.Connection, source: str, media: str, source_id: str,
                         season: int, number: int, fields: dict, *, now: int,
                         stale_after: int) -> None:
    """What a service's own episode list says about one episode, written over the
    listing's stub. Inside the caller's transaction."""
    conn.execute(_UPSERT_EPISODE_ANSWER + "    fetched_at = "
                 + ceiling_clock(EPISODES, _EPISODE_ANSWER_REWRITES), (
        source, media, source_id, int(season), int(number),
        str(fields.get("title") or ""), str(fields.get("overview") or ""),
        str(fields.get("first_aired") or ""), str(fields.get("episode_type") or ""),
        fields.get("runtime"), fields.get("rating"), fields.get("votes"),
        now, stale_after))


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def _delete_rowids(conn: db.Connection, table: str, rowids: list[int]) -> int:
    """Delete rows by rowid, in chunks under SQLite's bound-parameter ceiling."""
    removed = 0
    for start in range(0, len(rowids), 400):
        chunk = rowids[start:start + 400]
        removed += conn.execute(
            f"DELETE FROM {table} WHERE rowid IN ({', '.join('?' * len(chunk))})",
            chunk).rowcount
    return removed


# An airing naming a service's title, and one naming a service's episode, as SQL
# over a row aliased `r`. Constants, never input.
_AIRED_TITLE = ("EXISTS (SELECT 1 FROM calendar_airings a WHERE a.source = r.source "
                "AND a.media = r.media AND a.source_id = r.source_id)")
_AIRED_EPISODE = ("EXISTS (SELECT 1 FROM calendar_airings a WHERE a.source = r.source "
                  "AND a.media = r.media AND a.source_id = r.source_id "
                  "AND a.season = r.season AND a.episode_number = r.number)")


def sweep_unlisted(conn: db.Connection, cutoff: int) -> dict[str, int]:
    """Delete title, season and episode rows untouched since `cutoff` that NO
    CALENDAR AIRING STILL NAMES AND NO FEATURE ROW HOLDS, inside the caller's
    transaction — after the calendar has swept its own airings, which is the order
    that keeps a row while anything still draws it. Returns {table: rows removed}.

    A HELD TITLE KEEPS EVERY SERVICE'S DESCRIPTION, whatever the calendar thinks
    of it. A tracker row, a board row and what either may put back are drawn with
    the store's words for the title and keep no copy of their own, so a show that
    stopped airing in March cannot lose its name in September because no calendar
    lists it any more (holders.held_keys says which rows hold).

    THE EPISODE CHECK IS PER COORDINATE FOR A TITLE NOBODY HOLDS: a series in its
    fifth season stops carrying rows for its first, while a title still airing
    keeps everything that still airs. A held title keeps all of its episodes and
    seasons — they are what its details panel and its tracker row read.
    """
    held = holders.held_keys(conn)

    def unheld(rows) -> list[int]:
        return [int(row[0]) for row in rows if row[1] not in held]

    removed = {TITLES: _delete_rowids(conn, TITLES, unheld(conn.execute(
        f"SELECT r.rowid, r.title_key FROM {TITLES} r "
        f"WHERE r.fetched_at <= ? AND NOT {_AIRED_TITLE}", (cutoff,))))}
    # A SEASON OR AN EPISODE BELONGS TO ITS SERVICE'S TITLE ROW, which says whose
    # title it is; one whose title row has already gone is held by nothing.
    removed[SEASONS] = _delete_rowids(conn, SEASONS, unheld(conn.execute(
        f"SELECT r.rowid, t.title_key FROM {SEASONS} r LEFT JOIN {TITLES} t "
        "ON t.source = r.source AND t.media = r.media AND t.source_id = r.source_id "
        f"WHERE r.fetched_at <= ? AND NOT {_AIRED_TITLE}", (cutoff,))))
    removed[EPISODES] = _delete_rowids(conn, EPISODES, unheld(conn.execute(
        f"SELECT r.rowid, t.title_key FROM {EPISODES} r LEFT JOIN {TITLES} t "
        "ON t.source = r.source AND t.media = r.media AND t.source_id = r.source_id "
        f"WHERE r.fetched_at <= ? AND NOT {_AIRED_EPISODE}", (cutoff,))))
    return removed


def sweep_past_ceilings(conn: db.Connection, now: int) -> dict[str, int]:
    """Delete every row a service's own terms say may not be kept any longer —
    whoever holds or airs it — inside the caller's transaction. Returns
    {source: rows removed}.

    A SERVICE DECLARES ITS CEILING (`Capabilities.retain_seconds`, read through
    the registry), so this names no service: TMDB's terms forbid keeping anything
    obtained from it for longer than six months, and a second service with a rule
    of its own is a declaration in its package, not an edit here.

    THE CLOCK IS `fetched_at`, WHEN THE SERVICE LAST ANSWERED FOR THE ROW. Every
    answer re-obtains it — a refresh, a 304 confirming nothing changed, a listing
    naming it — and a row a viewer keeps looking at is refreshed long before this
    (app/metadata/refresh.py), so what goes here is what nobody has looked at in
    half a year. AN EPISODE STUB a listing wrote has never been answered for
    (`fetched_at` 0) and lives as long as the title row it came with.
    """
    removed: dict[str, int] = {}
    for source, provider in providers.registered().items():
        retain = provider.capabilities.retain_seconds
        if retain is None:
            continue
        cutoff = now - int(retain)
        name = str(source)
        count = 0
        for table in (TITLES, SEASONS):
            count += conn.execute(f"DELETE FROM {table} WHERE source = ? AND fetched_at <= ?",
                                  (name, cutoff)).rowcount
        count += conn.execute(
            f"DELETE FROM {EPISODES} WHERE source = ? AND ("
            "  (fetched_at > 0 AND fetched_at <= ?)"
            f"  OR (fetched_at = 0 AND NOT EXISTS (SELECT 1 FROM {TITLES} t "
            f"      WHERE t.source = {EPISODES}.source AND t.media = {EPISODES}.media "
            f"        AND t.source_id = {EPISODES}.source_id)))", (name, cutoff)).rowcount
        if count:
            logger.info("title store: removed %d %s row(s) not re-obtained inside the "
                        "%d days that service's terms allow", count, name, retain // 86400)
        removed[name] = count
    return removed


# ---------------------------------------------------------------------------
# what other features read of a title
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StoredTitle:
    """One service's stored record of a title: that service's own id map for it,
    its own name for the title, and the network it states. Empty network means
    that service has not said — a Simkl row waits for enrichment to fill it."""
    ids: dict
    network: str
    title: str = ""


async def known_about(title_keys) -> dict[str, dict[str, StoredTitle]]:
    """`{title key: {source: StoredTitle}}` for the titles named, and only those.

    WHAT THE STORE ALREADY KNOWS ABOUT A TITLE, PER SERVICE — for the tracker,
    which files rows under the same key and arrives at many of them knowing less
    than this does: a history-built row has no network, and a row filed off one
    service's calendar has only that service's ids. One indexed query, no call.

    PER SOURCE, NEVER MERGED. A title both services list has two names, two ids
    and possibly two networks (Simkl says who aired it, Trakt who carries it); a
    merged map can carry one of each, and each row here is one service's own
    record, so whose answer is whose needs no disambiguating.
    """
    keys = [str(k) for k in dict.fromkeys(title_keys or ()) if k]
    out: dict[str, dict[str, StoredTitle]] = {}
    # Chunked because SQLite has a bound-parameter ceiling (999 by default).
    for start in range(0, len(keys), 400):
        chunk = keys[start:start + 400]
        rows = await db.fetch_all(
            f"SELECT title_key, source, ids_json, network, title FROM {TITLES} "
            f"WHERE title_key IN ({', '.join('?' * len(chunk))}) "
            "ORDER BY title_key, source, network = '', source_id", tuple(chunk))
        for row in rows:
            ids = json.loads(row["ids_json"]) if row["ids_json"] else {}
            # ONE ROW PER (title, source), CHOSEN BY THE ORDER ABOVE: a service
            # can hold several rows for one title (Simkl lists an anime's seasons
            # as separate titles), and the one stating a network is preferred,
            # then the lowest id, so the answer never depends on scan order.
            out.setdefault(str(row["title_key"]), {}).setdefault(
                str(row["source"]),
                StoredTitle(ids=dict(ids), network=str(row["network"] or ""),
                            title=str(row["title"] or "")))
    return out


# The fields a surface can be showing one service's answer for while another
# service is the one describing the title — so the only three a request has any
# business naming a service for. A closed set, checked before anything reaches a
# query: what comes in is a query parameter.
SHOWN_FIELDS = ("title", "poster", "overview")

# Every registered source's name, which is what makes a source name a CLOSED set
# rather than a string a caller chose.
_SOURCE_NAMES = frozenset(str(source) for source in Source)


async def stored_values(title_key: str, by_source: dict[str, str]) -> dict[str, str]:
    """`{field: the value that service stored}` for each of SHOWN_FIELDS whose
    entry in `by_source` names a source with a row for `title_key`.

    WHAT IT IS FOR. A calendar card shows the name, the picture and the
    description this viewer's own field order picked, and the details panel has to
    open on those rather than on whatever the describing service says. A card
    ships one word per field, and this reads the rest back out of the row it was
    drawn from.

    A NAME THAT IS NOT A REGISTERED SOURCE NEVER REACHES THE DATABASE, and a
    field this cannot answer for is simply absent. The address is something a
    reader can edit, and the answer is rendered into a page served from the
    owner's origin, so a value that did not come out of the owner's own store has
    no business being in it.
    """
    wanted = {field: name for field in SHOWN_FIELDS
              if (name := str(by_source.get(field) or "")) in _SOURCE_NAMES}
    if not title_key or not wanted:
        return {}
    names = sorted(set(wanted.values()))
    rows = await db.fetch_all(
        f"SELECT source, title, poster, overview FROM {TITLES} "
        f"WHERE title_key = ? AND source IN ({', '.join('?' * len(names))})",
        (str(title_key), *names))
    stored = {str(row["source"]): row for row in rows}
    out = {}
    for field_name, name in wanted.items():
        row = stored.get(name)
        if row is not None and row[field_name]:
            out[field_name] = str(row[field_name])
    return out


async def posters_for(title_keys, media) -> dict[str, dict[str, str]]:
    """{title_key: {source: poster address}} for every stored title among
    `title_keys` that some service's record gives a picture — the ORIGIN address
    the calendar's own cards draw, never proxied here. One query however many
    keys: the tracker draws a picture on each of its rows, and a title the store
    already holds costs no request at all."""
    keys = sorted({str(key) for key in title_keys if key})
    if not keys:
        return {}
    rows = await db.fetch_all(
        f"SELECT title_key, source, poster FROM {TITLES} "
        f"WHERE media = ? AND title_key IN ({', '.join('?' * len(keys))}) AND poster != ''",
        (str(media), *keys))
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        out.setdefault(str(row["title_key"]), {}).setdefault(str(row["source"]), str(row["poster"]))
    return out


async def id_maps(title_key: str, media: str | None, own: tuple[str, str] | None
                  ) -> list[tuple[str, dict]]:
    """[(media, the row's own ids)] for every service row filed under `title_key`,
    or stating the id `own` names — every spelling the title's rows have ever
    answered to, for a reader that has to recognise any of them."""
    if own is None:
        rows = await db.fetch_all(
            f"SELECT media, ids_json FROM {TITLES} WHERE title_key = ?", (str(title_key),))
    else:
        rows = await db.fetch_all(
            f"SELECT media, ids_json FROM {TITLES} WHERE title_key = ? "
            "OR (media = ? AND CAST(json_extract(ids_json, '$.' || ?) AS TEXT) = ?)",
            (str(title_key), media, own[0], own[1]))
    out = []
    for row in rows:
        try:
            ids = json.loads(row["ids_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(ids, dict):
            out.append((str(row["media"]), ids))
    return out


async def titles_for(title_key: str) -> list:
    """Every service row filed under `title_key`: (source, media, title, ids)."""
    return await db.fetch_all(
        f"SELECT source, media, title, ids_json FROM {TITLES} WHERE title_key = ?",
        (str(title_key),))
