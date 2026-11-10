"""What each service says about a title and its episodes: the store's groups.

ONE ROW PER TITLE, PER SEASON AND PER EPISODE (app/metadata/tables.py), and in
each, ONE GROUP OF COLUMNS PER SERVICE (app/metadata/groups.py) holding
everything that service said and its own clocks. A service writes only its own
group. The tracker, the rankings board and the details panel describe the same
titles the calendar does, and a second copy of a title's facts per feature is
how one title came to be described five ways.

THE FACTS ARE SPLIT BY HOW LONG THEY STAY TRUE, which is why a title and its
episodes are separate rows. Measured across 4,330 (source, title) pairs with more
than one airing: runtime disagrees on 0.1% of them, certification 0.2%, genres
0.4%, network 0.8%, language and country 0.0%. Those are TITLE facts, and storing
a copy on every airing is what let one show's Tuesday and Thursday disagree.
`episode_title` disagrees on 95.5%, the opposite finding.

NOTHING HERE IS PER-VIEWER. Every service's group is stored whole; which one a
viewer sees is decided at READ against their own preferences
(app/calendar/resolve.py), so one viewer's choice never reaches another's view.

ONE GROUP PER SERVICE PER TITLE, AND IT IS ONE OF THE SERVICE'S ENTRIES. A
service can hold two of its own ids for one title — Simkl files a later season
of an anime as a title of its own, and now and then maps two shows to one id —
and `service_ids` keeps every one of them, so a listing, a refresh or a play
still finds the title by whichever id the service gives. The title's group is
the PRIMARY entry's (`is_primary`): the first to arrive, kept for as long as it
has a group, and where two arrive at once the one with the most listings
(`_entry_rank`) — the rule the store's conversion chose by (app/db.py's
MIGRATION_49). Another entry's words are not kept: its title words would stand
in for the title's own, and its seasons and episodes are numbered its own way.

A ROW IS NOT A TITLE'S IDENTITY. Which title a set of ids is is decided by
app/metadata/identity.py's one resolution and moved by it when the title is
re-keyed; nothing here derives a key of its own.
"""
from __future__ import annotations

import json
import logging
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from .. import db, providers
from ..media import artwork
from ..providers.base import Record, Source
from . import groups, holders, identity, merge, tables
from . import ids as id_rules

logger = logging.getLogger(__name__)

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


def own_id(source: str, source_id: Any, ids: Mapping[str, Any] | str | None) -> str:
    """A service's own id for a title, in ONE spelling: the id its ids state for
    that service — a number as plain decimal text — or, where they state none,
    the id the listing is filed under.

    WHY NOT THE FILED ID: Trakt files a listing under the title's slug and
    renames slugs — one film was filed as 'hot-spot-2026' and again as
    'tttrrr66-2026', one show as '199020' and 'the-traitors' — while its number
    never moves, so two spellings of one entry are one entry. The store's
    conversion (app/db.py's `_m49_own_id`) read every row by the same rule, and
    tests/metadata/test_conversion_agreement.py holds the two to it."""
    if isinstance(ids, str):
        ids = groups.loads(ids, {})
    own = (ids or {}).get(ID_NAMESPACE.get(str(source), str(source)))
    if isinstance(own, bool) or own in (None, ""):
        return str(source_id)
    if isinstance(own, (int, float)) or (isinstance(own, str) and own.strip().isdigit()):
        return str(int(own))
    return str(own)


def id_order(own: str) -> tuple:
    """Lowest first, a number by its value — the last tie-break between two of
    a service's entries."""
    return (len(own), own) if own.isdigit() else (float("inf"), own)


# ---------------------------------------------------------------------------
# a group's SQL, spelled once
# ---------------------------------------------------------------------------

def json_rows(width: int, *, keyed_at: int | None = None, keys: str = ":keys") -> str:
    """The SELECT a set-based write reads its rows from: column `i` of each
    `:rows` element in turn, with the `keys` element at the row's own position
    put in at `keyed_at`.

    EVERY WRITE OF A BATCH IN ONE STATEMENT PER TABLE. Each row travels as a JSON
    array spelled before the caller's transaction; the title key each is filed
    under is known only inside it and travels as a second array the rows index
    by position. ORDER BY keeps the rows in the batch's order, and an upsert over
    a SELECT applies them one at a time in that order — two writes of one group
    are the same two writes they were as two statements. `WHERE true` is
    SQLite's remedy for an upsert on a SELECT, whose ON would otherwise read as a
    join's."""
    columns = [f"json_extract(r.value, '$[{i}]')" for i in range(width)]
    if keyed_at is not None:
        columns.insert(keyed_at, f"json_extract({keys}, '$[' || r.key || ']')")
    return (f"SELECT {', '.join(columns)} FROM json_each(:rows) r "
            "WHERE true ORDER BY r.key")


def clear_sql(table: str, source: str, *, keep: Iterable[str] = ()) -> str:
    """`UPDATE table SET` every column of `source`'s group back to what it holds
    where the service has said nothing — the group gone, the row kept — less the
    columns named in `keep`."""
    kept = set(keep)
    return (f"UPDATE {table} SET " + ", ".join(
        f"{source}_{name} = {groups.literal(groups.EMPTY[name])}"
        for name in groups.every(table, source) if name not in kept))


# ---------------------------------------------------------------------------
# the clock a ceiling reads
# ---------------------------------------------------------------------------

def ceiling_sources() -> tuple[str, ...]:
    """Every registered service whose terms bound how long what it supplies may
    be kept (`Capabilities.retain_seconds`), by name."""
    return tuple(str(source) for source, provider in providers.registered().items()
                 if provider.capabilities.retain_seconds is not None)


def _held(column: str) -> str:
    """Template SQL: the stored group's `column` holds a word — anything but
    nothing, an empty string, an empty list or an empty map."""
    return f"(held.{column} IS NOT NULL AND held.{column} NOT IN ('', '[]', '{{}}'))"


def ceiling_clock(table: str, source: str, rewrites: Mapping[str, str | None], *,
                  new: str = "given.fetched_at") -> str:
    """The template SQL a write of `source`'s group onto `table` sets
    `fetched_at` to — `new`, the moment the service answered, or the group's
    older clock where the write leaves an older word in place.

    A CEILING IS ABOUT EVERY WORD, AND A GROUP HAS ONE CLOCK. A service with a
    ceiling (TMDB: nothing it supplied may be kept past six months) is read by
    `sweep_past_ceilings` from `fetched_at`, so for its groups that clock must
    be the age of the OLDEST word the group holds. Writers rewrite parts of a
    group — a calendar listing its name and picture, a season list a season's
    name and count, a description everything it states — and a partial write
    that renewed the clock would keep the words it left behind past the
    ceiling: a season's cast described once and re-listed every few days would
    never age. So a write renews the clock only when it rewrites every word the
    group holds, and otherwise keeps the older one. A group two kinds of answer
    wrote goes at its oldest word's ceiling and the next view of it builds it
    again (app/metadata/refresh.py) — a few requests every six months, against
    a word kept for ever.

    `rewrites` names each word the write rewrites, with the template condition
    under which it does NOT (None: always) — a listing that keeps the stored
    picture when it states none, say. Every word not named is left. An episode
    description at `fetched_at` 0 is a listing's stub no answer has reached,
    which no clock describes; the answer's clock is its first. A service with no
    ceiling is always given `new`: for it the clock says when it last answered,
    which is what its own readers read.
    """
    if str(source) not in ceiling_sources():
        return new
    left = [_held(column) if column not in rewrites
            else f"(({rewrites[column]}) AND {_held(column)})"
            for column in groups.WORDS[table] if groups.has(table, source, column)
            and (column not in rewrites or rewrites[column] is not None)]
    if not left:
        return new
    return (f"CASE WHEN held.fetched_at > 0 AND ({' OR '.join(left)}) "
            f"THEN MIN(held.fetched_at, {new}) ELSE {new} END")


def kept_clock(source: str, table: str, row: Mapping[str, Any] | None,
               rewrites: Iterable[str], now: int) -> int:
    """`ceiling_clock` for a write made from Python over a group it has read
    (app/metadata/descriptions.py's `plan`): `now`, or the group's older clock
    where the write leaves a word of a ceiling-bound service in place."""
    if row is None or str(source) not in ceiling_sources():
        return now
    stored = int(row["fetched_at"] or 0)
    rewritten = set(rewrites)
    if stored > 0 and any(row[column] not in (None, "", "[]", "{}")
                          for column in groups.WORDS[table]
                          if column not in rewritten and column in row):
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
# keywords and show type: two words, and whether they are an answer
# ---------------------------------------------------------------------------

def facts_values(keywords: list[str] | None, show_type: str | None) -> dict:
    """The three columns `keywords` and `show_type` are stored in — ONE SPELLING
    for the two writers, a description (app/metadata/descriptions.py) and a
    listing built from one (`write_listings`).

    AN ANSWER ONLY WHEN BOTH ARE STATED (`facts_known`). None is a service that
    does not state them, or a listing nobody has described (Record.keywords), and
    stored as the empty defaults it would read back the same as "TMDB lists no
    keywords for this title" — which an include filter judges, where a title
    nobody has described is shown unjudged. TMDB's description always carries
    both; an answer carrying one is not one this app asked for.
    """
    known = keywords is not None and show_type is not None
    return {"keywords_json": json.dumps([str(k) for k in keywords or ()]),
            "show_type": str(show_type or ""),
            "facts_known": 1 if known else 0}


def facts_of(row: Mapping[str, Any]) -> tuple[list[str] | None, str | None]:
    """(keywords, show_type) as a group states them — both None unless a
    description stated them (`facts_values`)."""
    if not row["facts_known"]:
        return None, None
    try:
        keywords = json.loads(row["keywords_json"] or "[]")
    except ValueError:
        keywords = []
    return ([str(k) for k in keywords] if isinstance(keywords, list) else [],
            str(row["show_type"] or ""))


# ---------------------------------------------------------------------------
# a service's listing of its titles, written
# ---------------------------------------------------------------------------

# Title columns a listing can speak to, in the order a payload spells them — of
# which a service's write names those its group has.
_TITLE_FIELDS = ("title", "title_fold", "ids_json", "detail_url", "year", "network",
                 "country", "language", "certification", "status", "overview",
                 "poster", "runtime", "genres_json", "ratings_json", "anime_type",
                 "release_types_json", "enriched", "keywords_json", "show_type", "facts_known")


def _title_values(record: Record) -> dict[str, Any]:
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
    # THE IDS IN THE STORE'S ONE SPELLING (ids.stored), which a description
    # files too. A listing filed them as the service stated them — Trakt's and
    # Simkl's bare `slug`, Simkl's `traktslug` / `tvdbslug` and the rest — so
    # every description took them off and the next fill put them back: a refill
    # that changed nothing was a write of the group, and the description logged
    # a change that was not one.
    return {
        "title": str(record.title or ""), "title_fold": fold_title(record.title),
        "ids_json": id_rules.stored(record.ids),
        "detail_url": str(record.detail_url or ""),
        "year": str(record.year or ""), "network": str(record.network or ""),
        "country": str(record.country or ""), "language": str(record.language or ""),
        "certification": str(record.certification or ""), "status": str(record.status or ""),
        "overview": str(record.overview or ""), "poster": str(record.poster or ""),
        "runtime": record.runtime,
        "genres_json": json.dumps([str(g) for g in (record.genres or [])]),
        "ratings_json": json.dumps(ratings), "anime_type": str(record.anime_type or ""),
        "release_types_json": json.dumps(record.release_types_by_country or {}),
        "enriched": 1 if record.enriched else 0,
        **facts_values(record.keywords, record.show_type),
    }


def _listed_fields(source: str) -> tuple[str, ...]:
    return tuple(name for name in _TITLE_FIELDS if groups.has(tables.TITLES, source, name))


# A LISTING MAY NOT DEMOTE WHAT ENRICHMENT LEARNED. The calendar feed and the
# per-title drain write the same group, and the feed knows less: Simkl's CDN files
# carry no genres, network, country or certification at all. An unconditional
# write would therefore erase a drained answer every time the month refreshed,
# and the next drain would fetch it again -- a loop that looks like enrichment
# never sticking. So the feed's write keeps the coordinates and the poster, which
# it is authoritative for, and leaves every enriched field alone once `enriched`
# is set.
_ENRICHED_KEPT = ("year", "network", "country", "language", "certification", "status",
                  "overview", "runtime", "genres_json", "ratings_json", "anime_type")
_LISTING_WRITES: dict[str, str] = {
    "title": "given.title", "title_fold": "given.title_fold", "ids_json": "given.ids_json",
    "detail_url": "CASE WHEN given.detail_url != '' THEN given.detail_url ELSE held.detail_url END",
    "poster": "CASE WHEN given.poster != '' THEN given.poster ELSE held.poster END",
    **{name: f"CASE WHEN held.enriched = 1 THEN held.{name} ELSE given.{name} END"
       for name in _ENRICHED_KEPT},
    "enriched": "MAX(held.enriched, given.enriched)",
    # KEYWORDS AND SHOW TYPE ONLY FROM A LISTING THAT STATES THEM — TMDB's season
    # premieres are built from the title's own answer and do; a discover entry
    # says nothing and leaves what a description learned (facts_values).
    "keywords_json": "CASE WHEN given.facts_known = 1 THEN given.keywords_json "
                     "ELSE held.keywords_json END",
    "show_type": "CASE WHEN given.facts_known = 1 THEN given.show_type ELSE held.show_type END",
    "facts_known": "MAX(held.facts_known, given.facts_known)",
    # `release_types_json` HAS ONE WRITER ONCE A GROUP EXISTS: `_update_enriched_sql`
    # sets it and `enriched = 1` in the same breath, so "this group has release
    # types" implies "this group is enriched" by construction rather than by
    # observation, and a re-fill can never touch it. Trakt's half of the question
    # never writes here at all — its own table, applied as a read-time overlay
    # (calendar/enrich.overlay_releases). A listing creating the group is the
    # group's first word, so it is written then.
    "release_types_json": "CASE WHEN held.id IS NULL THEN given.release_types_json "
                          "ELSE held.release_types_json END",
    # A listing's clock for when the group falls due is the one it was created
    # with; only an answer moves it.
    "stale_after": "CASE WHEN held.id IS NULL THEN given.stale_after ELSE held.stale_after END",
}

# What a listing rewrites of a title group: its name always, its picture when it
# states one, the enriched fields until a lookup has answered for them, and the
# keywords and show type when it states them.
_LISTING_REWRITES: dict[str, str | None] = {
    "title": None, "poster": "given.poster = ''",
    **{name: "held.enriched = 1" for name in _ENRICHED_KEPT},
    **{name: "given.facts_known = 0" for name in ("keywords_json", "show_type")}}

# A WRITE OF ONE ENTRY'S WORDS LANDS ONLY ON A GROUP THAT IS THAT ENTRY'S — or on
# a title with no group of the service yet, which it then becomes. The template
# every listing column's write is wrapped in.
_MINE = "(held.id IS NULL OR held.id = given.id)"


def _listing_sets(source: str) -> list[tuple[str, str]]:
    """(column, template) for every column a listing's write sets on `source`'s
    title group — THE ONE STATEMENT OF THE LISTING'S WRITE RULES, spelled into
    the upsert (`_upsert_title_sql`) and into the question asked before the lock
    of whether writing would change anything (`changing`), so the two cannot
    come to disagree."""
    sets = [("id", "COALESCE(held.id, given.id)")]
    for name in (*_listed_fields(source), "stale_after"):
        sets.append((name, f"CASE WHEN {_MINE} THEN {_LISTING_WRITES[name]} ELSE held.{name} END"))
    clock = ceiling_clock(tables.TITLES, source, _LISTING_REWRITES)
    sets.append(("fetched_at", f"CASE WHEN {_MINE} THEN {clock} ELSE held.fetched_at END"))
    return sets


def _listing_columns(source: str) -> tuple[str, ...]:
    """The columns a listing's write inserts, in `_listing_given`'s order."""
    return ("title_key", "media", "created_at",
            *(f"{source}_{name}" for name in ("id", *_listed_fields(source), "fetched_at",
                                             "stale_after")))


def _listing_given(source: str) -> str:
    """The rows a listing's write reads, each column named as the table names
    it (`_listing_columns`): the key the listing is filed under, its media, the
    moment a title nobody registered yet is created at, then `source`'s group as
    `listing_rows` spells it — and `at`, the row's position in `:rows`."""
    names = ("id", *_listed_fields(source), "fetched_at", "stale_after")
    return ("SELECT r.key AS at, json_extract(:keys, '$[' || r.key || ']') AS title_key, "
            "json_extract(r.value, '$[0]') AS media, :now AS created_at, "
            + ", ".join(f"json_extract(r.value, '$[{i}]') AS {source}_{name}"
                        for i, name in enumerate(names, start=1))
            + " FROM json_each(:rows) r")


def _upsert_title_sql(source: str) -> str:
    columns = ", ".join(_listing_columns(source))
    return (f"INSERT INTO {tables.TITLES} ({columns})\n"
            f"SELECT {columns} FROM ({_listing_given(source)}) WHERE true ORDER BY at"
            "\nON CONFLICT(title_key) DO UPDATE SET " + ",\n    ".join(
                f"{source}_{name} = {groups.spelled(template, tables.TITLES, source)}"
                for name, template in _listing_sets(source)))


# A SPELLING OF AN ENTRY, AND WHETHER IT IS THE TITLE'S GROUP — every spelling a
# listing filed it under and the one spelling `own_id` reads, decided after the
# groups are written so a group this write created is the primary it now is.
def _primary_test(alias: str) -> str:
    return ("CASE json_extract(j.value, '$[0]') "
            + " ".join(f"WHEN '{source}' THEN {alias}.{source}_id" for source in groups.sources())
            + " END = json_extract(j.value, '$[3]')")


_UPSERT_SERVICE_IDS = f"""
INSERT INTO {tables.SERVICE_IDS} (source, media, service_id, title_key, is_primary)
SELECT json_extract(j.value, '$[0]'), json_extract(j.value, '$[1]'), json_extract(j.value, '$[2]'),
       json_extract(j.value, '$[4]'),
       COALESCE((SELECT {_primary_test('t')} FROM {tables.TITLES} t
                 WHERE t.title_key = json_extract(j.value, '$[4]')), 0)
FROM json_each(:ids) j WHERE true ORDER BY j.key
ON CONFLICT(source, media, service_id) DO UPDATE SET
    title_key = excluded.title_key, is_primary = excluded.is_primary
"""


def _spelled_ids(entries: Iterable[list]) -> list[list]:
    """Each [source, media, spelling, own id, title key]'s spellings, as
    `_UPSERT_SERVICE_IDS` reads them: the spelling it arrived under and the one
    `own_id` reads, each once — the last entry naming it deciding its title."""
    spelled: dict[tuple[str, str, str], list] = {}
    for source, media, spelling, own, key in entries:
        for name in dict.fromkeys((str(spelling), str(own))):
            spelled[(str(source), str(media), name)] = [str(source), str(media), name,
                                                        str(own), str(key)]
    return list(spelled.values())


def file_ids(conn: db.Connection, entries: Iterable[list]) -> None:
    """Each [source, media, spelling, own id, title key]'s spellings in
    `service_ids` — the spelling it arrived under and the one `own_id` reads —
    in one statement, after the groups it names are written."""
    spelled = _spelled_ids(entries)
    if spelled:
        conn.execute(_UPSERT_SERVICE_IDS, {"ids": json.dumps(spelled)})


# Where each spelling is filed now, and whether it is its title's group — one row.
_IDS_HELD_SQL = f"""
SELECT json_group_array(json_array(s.source, s.media, s.service_id, s.title_key, s.is_primary))
FROM json_each(?) j CROSS JOIN {tables.SERVICE_IDS} s
  ON s.source = json_extract(j.value, '$[0]') AND s.media = json_extract(j.value, '$[1]')
 AND s.service_id = json_extract(j.value, '$[2]')
"""


class ListingRows(NamedTuple):
    """A fill's listings as the store writes them, one entry per listing —
    everything but the title key each is filed under, which only the caller's
    transaction knows. Built BEFORE that transaction (`listing_rows`), so the
    write lock is held for a few statements and not for the Python that spells
    several hundred rows."""
    sources: list[str]
    # The service's own id for each listing's title (`own_id`), and the id the
    # listing was filed under.
    owns: list[str]
    spellings: list[str]
    # Each listing's title group as `_upsert_title_sql` reads it, JSON.
    titles: list[str]
    # How a listing's entry ranks against another of the same service for one
    # title (`_entry_rank`): enriched, then how many words it states.
    ranks: list[tuple[int, int]]
    # (media, source, address) of each listing's picture, None for none.
    pictures: list[tuple[str, str, str] | None]

    def only(self, positions: Iterable[int]) -> "ListingRows":
        """These rows narrowed to the listings at `positions`, in that order."""
        positions = list(positions)
        return ListingRows(*([column[i] for i in positions] for column in self))

    def with_ids(self, changes: Mapping[int, Mapping[str, Any]]) -> "ListingRows":
        """These rows with the listing at each position in `changes` stating
        those ids instead of its own — every other word as it was spelled."""
        if not changes:
            return self
        titles = list(self.titles)
        for at, ids in changes.items():
            spelled = json.loads(titles[at])
            # Each row: media, the own id, then the group's listed fields.
            spelled[2 + _listed_fields(self.sources[at]).index("ids_json")] = id_rules.stored(ids)
            titles[at] = json.dumps(spelled)
        return self._replace(titles=titles)


def listing_rows(records: Iterable[Record], *, now: int, stale_after: int) -> ListingRows:
    """What `write_listings` writes for each of `records`, spelled — pure, and
    for the caller to call before its transaction."""
    out = ListingRows([], [], [], [], [], [])
    for record in records:
        source = str(record.source)
        values = _title_values(record)
        own = own_id(source, record.id, record.ids)
        out.sources.append(source)
        out.owns.append(own)
        out.spellings.append(str(record.id))
        out.titles.append(json.dumps([str(record.media), own,
                                      *(values[name] for name in _listed_fields(source)),
                                      now, stale_after]))
        out.ranks.append((values["enriched"],
                          sum(1 for name in groups.WORDS[tables.TITLES]
                              if values.get(name) not in (None, "", "[]", "{}"))))
        out.pictures.append((str(record.media), source, str(record.poster))
                            if record.poster else None)
    return out


def _entry_rank(counts: Mapping[tuple[str, str, str], int], key: str, source: str, own: str,
                rank: tuple[int, int]) -> tuple:
    """Which of a service's entries for one title becomes its group when more
    than one arrives together: THE ONE WITH THE MOST LISTINGS — for a per-season
    entry, the season airing now, whose words the calendar draws — then the more
    complete (a description answered, then more words), then the lowest id. The
    store's conversion chose by the same rule."""
    return (-counts[(key, source, own)], -rank[0], -rank[1], id_order(own))


def ordered(rows: ListingRows, keys: list[str]) -> list[int]:
    """The positions of `rows` in the order their title groups are written: for
    each (title, service), the entry `_entry_rank` puts first goes first, so
    that the group a write creates is that entry's — the order otherwise kept."""
    counts: dict[tuple[str, str, str], int] = {}
    best: dict[tuple[str, str, str], tuple[int, int]] = {}
    for source, own, key, rank in zip(rows.sources, rows.owns, keys, rows.ranks):
        counts[(key, source, own)] = counts.get((key, source, own), 0) + 1
        best[(key, source, own)] = max(best.get((key, source, own), rank), rank)
    leader: dict[tuple[str, str], str] = {}
    for (key, source, own), rank in best.items():
        held = leader.get((key, source))
        if held is None or (_entry_rank(counts, key, source, own, rank)
                            < _entry_rank(counts, key, source, held, best[(key, source, held)])):
            leader[(key, source)] = own
    return sorted(range(len(keys)), key=lambda at: (
        leader[(keys[at], rows.sources[at])] != rows.owns[at]))


def primaries(conn: db.Connection, rows: ListingRows, keys: list[str]
              ) -> dict[tuple[str, str], str]:
    """{(title key, service): the own id of the entry that title's group is once
    `write_listings` has written `rows` under `keys`} — the group already there,
    else the entry `ordered` puts first. Read, not written: what a caller filing
    each listing's episode row needs to know before the write lock is taken."""
    wanted = sorted(set(keys))
    if not wanted:
        return {}
    names = groups.sources()
    (found,) = conn.execute(
        "SELECT json_group_array(json_array(t.title_key, "
        + ", ".join(f"t.{source}_id" for source in names) + f")) FROM {tables.TITLES} t "
        "WHERE t.title_key IN (SELECT value FROM json_each(?))", (json.dumps(wanted),)).fetchone()
    out: dict[tuple[str, str], str] = {}
    for key, *ids in json.loads(found):
        for source, held in zip(names, ids):
            if held is not None:
                out[(str(key), source)] = str(held)
    for at in ordered(rows, keys):
        out.setdefault((keys[at], rows.sources[at]), rows.owns[at])
    return out


def _entries(rows: ListingRows, keys: list[str]) -> list[int]:
    """The listings a title group is written from: ONE PER ENTRY — (title key,
    service, own id) — its last in write order (`ordered`), the entries in the
    order they first appear there. An entry's listings in one answer state the
    same words, the show's carried on each of its airings, so its last is all of
    them; one write per entry is what lets each be judged against the stored row
    alone (groups.changing)."""
    last: dict[tuple[str, str, str], int] = {}
    for at in ordered(rows, keys):
        last[(keys[at], rows.sources[at], rows.owns[at])] = at
    return list(last.values())


class ListingWrites(NamedTuple):
    """What `write_listings` writes of a batch of listings, decided before the
    lock (`plan_listing_writes`): only what would change the stored rows."""
    # {service: (each entry's title group as `_upsert_title_sql` reads it, the
    # key each is filed under)}.
    titles: dict[str, tuple[list[str], list[str]]]
    # [source, media, spelling, own id, title key] for each spelling whose
    # filing — its title, or whether it is the title's group — would change.
    ids: list[list]
    # (media, None, title key, source, address) for each picture to file.
    sightings: list[tuple]

    @property
    def empty(self) -> bool:
        return not (self.titles or self.ids or self.sightings)


def plan_listing_writes(conn: db.Connection, rows: ListingRows, keys: list[str],
                        primaries: Mapping[tuple[str, str], str], *, now: int,
                        whole: Collection[str] = ()) -> ListingWrites:
    """What writing `rows` under `keys` would change — read and decided, NOT
    written: a statement per service for the title groups (groups.changing, over
    `_listing_sets`), one for the entries' spellings, one for the pictures.

    A REFILL THAT CHANGED NOTHING WRITES NONE OF IT. A service's clock is not a
    change except where a ceiling reads it (`ceiling_sources`): the coverage row
    the refill writes says when the service last confirmed the window, and a
    listing's group is not rewritten to say so again — while TMDB's group is
    re-stamped whenever a re-read restates every word it holds (`ceiling_clock`),
    because its terms are about when each word was obtained.

    `primaries` is `primaries`' answer for these rows; `whole` names title keys a
    merge in the same write reshapes, whose rows are written whatever this read
    of them as they were before says."""
    whole = frozenset(whole)
    by_source: dict[str, list[int]] = {}
    for at in _entries(rows, keys):
        by_source.setdefault(rows.sources[at], []).append(at)
    titles: dict[str, tuple[list[str], list[str]]] = {}
    for source, positions in by_source.items():
        counted = [(name, template) for name, template in _listing_sets(source)
                   if name != "fetched_at" or source in ceiling_sources()]
        moving = groups.changing(
            conn, tables.TITLES, ("title_key",), _listing_given(source), counted, source,
            {"rows": "[" + ",".join(rows.titles[at] for at in positions) + "]",
             "keys": json.dumps([keys[at] for at in positions]), "now": now})
        kept = [at for i, at in enumerate(positions) if i in moving or keys[at] in whole]
        if kept:
            titles[source] = ([rows.titles[at] for at in kept], [keys[at] for at in kept])
    spelled = _spelled_ids([source, json.loads(title)[0], spelling, own, key]
                           for source, own, spelling, key, title in zip(
                               rows.sources, rows.owns, rows.spellings, keys, rows.titles))
    held: dict[tuple[str, str, str], tuple[str, int]] = {}
    if spelled:
        (found,) = conn.execute(_IDS_HELD_SQL, (json.dumps(spelled),)).fetchone()
        held = {(source, media, str(name)): (str(key), int(primary))
                for source, media, name, key, primary in json.loads(found)}
    ids = [entry for entry in spelled
           if entry[4] in whole or held.get(tuple(entry[:3])) != (
               entry[4], int(primaries.get((entry[4], entry[0])) == entry[3]))]
    # A TITLE WITH NO TMDB ID HAS ITS LISTING'S PICTURE FILED UNDER ITS KEY, as
    # the store knows it only here (app/media/artwork.py's `title_posters`). One
    # with a tmdb id is filed by the calendar's fill, which records every
    # picture a fetch brought (app/calendar/cache.py's `_poster_sighting`).
    sightings = list(dict.fromkeys(
        (media, None, key, source, url)
        for picture, key in zip(rows.pictures, keys)
        if picture is not None and identity.tmdb_of(key) is None
        for media, source, url in (picture,)))
    return ListingWrites(titles, ids, artwork.unseen(conn, sightings, now))


def write_listings(conn: db.Connection, writes: ListingWrites, *, now: int) -> None:
    """Services' listings of their titles as `plan_listing_writes` decided them,
    inside the caller's transaction — on the connection it was planned on, which
    db.transaction's `prepare` guarantees holds what it did then: each entry's
    title group under its key where that entry is the title's group, its
    spellings in `service_ids`, each picture — a statement per service and
    table, however many listings there are, and none where nothing changes."""
    for source, (payload, filed) in writes.titles.items():
        conn.execute(_upsert_title_sql(source), {
            "rows": "[" + ",".join(payload) + "]", "keys": json.dumps(filed), "now": now})
    if writes.ids:
        conn.execute(_UPSERT_SERVICE_IDS, {"ids": json.dumps(writes.ids)})
    artwork.file_sightings(conn, writes.sightings, now)


# Where each named spelling of a service's entry is filed now — one read for a
# whole fill.
_FILED_SQL = f"""
SELECT json_group_array(json_array(s.source, s.media, s.service_id, s.title_key))
FROM json_each(?) j CROSS JOIN {tables.SERVICE_IDS} s
  ON s.source = json_extract(j.value, '$[0]') AND s.media = json_extract(j.value, '$[1]')
 AND s.service_id = json_extract(j.value, '$[2]')
"""


def filed_keys(conn: db.Connection, refs: Iterable[tuple[str, str, str]]
               ) -> dict[tuple[str, str, str], str]:
    """{(source, media, own id or spelling): the key that service's entry is
    filed under now}, for each of `refs` the store knows. One row back, however
    many are asked about (identity.py's reads say why)."""
    refs = [list(ref) for ref in refs]
    if not refs:
        return {}
    (found,) = conn.execute(_FILED_SQL, (json.dumps(refs),)).fetchone()
    return {(str(source), str(media), str(service_id)): str(key)
            for source, media, service_id, key in json.loads(found)}


class Move(NamedTuple):
    """One service's entry for a title found under another title: the service's
    own answer about the title says it is this one now. A listing never makes
    one (app/metadata/__init__.py's `plan_records` says why)."""
    source: str
    media: str
    own: str
    old: str
    new: str


def move_entries(conn: db.Connection, moves: Iterable[Move], *, now: int) -> None:
    """Each entry in `moves` taken from the title it was filed under to the one
    it names now, inside the caller's transaction — its group, its seasons and
    episodes and its listings with it (app/metadata/merge.py's `entry`).

    A SERVICE RE-POINTS ITS SHARED IDS AND NEVER ITS OWN — Simkl moved The Great
    British Bake Off between TMDB's two titles — and what that service said about
    the title is about the title its own id names. Made only by a service's own
    answer about the title, so once per re-point; each its own few statements,
    like a re-key."""
    for move in dict.fromkeys(moves):
        if move.old != move.new:
            merge.entry(conn, move.source, move.media, move.own, move.old, move.new, now=now)


# ---------------------------------------------------------------------------
# what a per-title lookup learned
# ---------------------------------------------------------------------------

# The columns an enrichment answer may write. Deliberately NOT every column: a
# lookup has nothing to say about the title's key, the coordinates or when it
# was listed, and listing what it may touch is what stops a future field being
# written by accident from a payload that happens to carry that name.
_ENRICHED_FIELDS = ("network", "country", "language", "certification", "status",
                    "overview", "runtime", "year")
_ENRICHED_COLUMNS = (*_ENRICHED_FIELDS, "genres_json", "ratings_json", "anime_type",
                     "release_types_json")


def _update_enriched_sql(source: str) -> str:
    """The enrichment UPDATE of `source`'s group — `ceiling_clock` with the
    moment as `:now`.

    `ids_json = json_patch(<learned>, ids_json)` MERGES AND LETS THE EXISTING
    WIN, which is the direction that matters. A lookup knows id spaces the
    calendar file never carries -- tvdb, mal -- and adding them is most of
    why the payload is worth keeping; but the calendar file's OWN tmdb is the one
    the listing matched on, and a lookup overwriting it would re-identify a title
    under cover of enriching it.

    EVERY ANSWER OF A PASS IN ONE STATEMENT, each group it is for named by its
    title's key (`:rows`, one array per answer: the key, `_ENRICHED_COLUMNS`'
    values, the learned ids, and the entry the group must still be). The keys
    come from `service_ids`, read BEFORE the write lock is taken
    (`apply_enrichment_many` says why), so the update re-checks the entry: a
    group that became another entry's in between is left for the next pass
    rather than given another entry's answer."""
    columns = [name for name in _ENRICHED_COLUMNS if groups.has(tables.TITLES, source, name)]
    at = {name: i for i, name in enumerate(_ENRICHED_COLUMNS, start=1)}
    clock = groups.spelled(ceiling_clock(tables.TITLES, source, dict.fromkeys(columns), new=":now"),
                    tables.TITLES, source)
    table = tables.TITLES
    return (
        f"UPDATE {table} SET "
        + ", ".join(f"{source}_{name} = json_extract(j.value, '$[{at[name]}]')"
                    for name in columns)
        + f", {source}_ids_json = {_patched_ids(source, table)}, {source}_enriched = 1, "
        f"{source}_fetched_at = {clock}, {source}_failed_at = NULL, {source}_fail_count = 0 "
        f"FROM json_each(:rows) j WHERE {_answered_by(source, table)}")


# Where in an answer's row (`_enrichment_row`, then the entry) the learned ids
# and the entry it is for sit.
_LEARNED, _STATED = len(_ENRICHED_COLUMNS) + 1, len(_ENRICHED_COLUMNS) + 2


def _patched_ids(source: str, table: str) -> str:
    """SQL: the group's ids once an answer (`j`) is written — what the UPDATE
    sets, and what the settle after it is planned from (`_settlings_sql`), in
    one spelling so the two cannot disagree."""
    return f"json_patch(json_extract(j.value, '$[{_LEARNED}]'), {table}.{source}_ids_json)"


def _answered_by(source: str, table: str) -> str:
    """SQL: the answer (`j`) is for this group — its title's key, and the entry
    the group must still be."""
    return (f"{table}.title_key = json_extract(j.value, '$[0]') "
            f"AND {table}.{source}_id = json_extract(j.value, '$[{_STATED}]')")


def _settlings_sql(source: str) -> str:
    """Every title a pass's answers (`:rows`) name, as the UPDATE leaves it: its
    key, media, this service's ids — patched where an answer is for its group —
    and the entry. One row, read BEFORE the lock (`apply_enrichment_many`)."""
    table = tables.TITLES
    return (f"SELECT json_group_array(json_array({table}.title_key, {table}.media, "
            f"CASE WHEN j.value IS NULL THEN {table}.{source}_ids_json "
            f"ELSE {_patched_ids(source, table)} END, {table}.{source}_id)) "
            f"FROM {table} LEFT JOIN json_each(:rows) j ON {_answered_by(source, table)} "
            f"WHERE {table}.title_key IN (SELECT json_extract(value, '$[0]') "
            f"FROM json_each(:rows))")


# Where each answered entry's group is: its title's key, through `service_ids`
# (the entry's own id is its primary spelling), only where it IS the group.
_ANSWERED_SQL = f"""
SELECT json_group_array(json_array(s.title_key, s.media, s.service_id))
FROM json_each(:ids) j CROSS JOIN {tables.SERVICE_IDS} s
  ON s.source = :source AND s.media = json_extract(j.value, '$[1]')
 AND s.service_id = json_extract(j.value, '$[0]')
WHERE s.is_primary = 1
"""


def enrichment_values(fields: dict) -> dict:
    """ONE READING OF WHAT A PER-TITLE LOOKUP'S PAYLOAD MEANS.

    THERE ARE TWO PLACES A LOOKUP'S ANSWER IS WRITTEN, and before this there were
    two readings of it. calendar/enrich.py sets the fields on a RECORD, on the way
    into a fill; `_enrichment_row` below sets them on a stored GROUP, when the
    drain answers for a title whose group already exists. Both had their own list
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


def _enrichment_row(source: str, fields: dict) -> list:
    """One answer as `_update_enriched_sql` writes it: `_ENRICHED_COLUMNS`' values,
    then the ids it learned."""
    value = enrichment_values(fields)
    # UNDER THE SOURCE'S OWN NAME, exactly as `_title_values` does it for a
    # record — one number shown under one service's mark, never a bare figure.
    ratings = {} if value["rating"] is None else {source: value["rating"]}
    if value["imdb_rating"] is not None:
        ratings["imdb"] = value["imdb_rating"]
    return [*(value[name] for name in _ENRICHED_FIELDS),
            json.dumps(value["genres"]),
            json.dumps(ratings),
            value["anime_type"],
            json.dumps(value["release_types_by_country"]),
            # Learned ids in the spelling every writer files (`_title_values`
            # says why): merged in raw, they put back what a description took off.
            json.dumps(id_rules.canonical(value["ids"]))]


async def apply_enrichment(source: str, service_id: int, media: str, fields: dict,
                           now: int) -> None:
    """Write what a per-title lookup learned onto the stored title, and mark it
    answered.

    THE DRAIN IS A WRITER OF THE GROUP, WHICH IS THE WHOLE POINT. Enrichment used
    to be a side table overlaid at read; the objection to storing it was that a
    value baked in at fill would be frozen for the window's whole TTL and would
    make `enriched` a lie about what was stored. Maintained here, `enriched`
    records what the group ACTUALLY CONTAINS, and the value is never older than
    the last drain pass rather than as old as the window.

    ADDRESSED BY THE SERVICE'S NUMERIC ID — its entry's own id.
    """
    await apply_enrichment_many(source, [(service_id, media, fields)], now)


# A PASS'S ANSWERS ARE WRITTEN IN BOUNDED TURNS of this many titles: every write
# in the process waits its turn behind the one writing (app/db.py's
# `_writer_queue`), and the Simkl drain's whole pass in one turn held the lock
# 1.30-1.94 s beside a restart's refills (MEASURED, a harness copy in a browser
# pass) — the settle inside it, and its reads, as well as the update.
ENRICHMENT_TURN = 50


async def apply_enrichment_many(source: str, items, now: int) -> int:
    """The same write for many titles, in bounded turns (`ENRICHMENT_TURN`) of a
    fixed number of statements each, however many there are. Returns how many
    answers were handed in.

    FOR THE REBUILD as well as the fetch: titles whose answers are already stored
    and whose groups have not been given them, which happens in bulk.

    WHERE EACH GROUP IS, IS READ BEFORE THE TRANSACTION, by one read: held inside
    the write lock, finding a hundred answers' rows held it 11.7 s with nothing
    else running — MEASURED on the development copy, where the log shows
    rebuilds of up to 45. A group a fill writes between that read and this write
    is not handed its answer here; it is written unenriched, and the next pass's
    rebuild gives it the stored one — which is exactly what it was owed already.

    AND THE IDS AN ANSWER LEARNED ARE TAUGHT TO THE TITLE in the same
    transaction. THIS IS WHERE A KEY USED TO GO STALE. A lookup adds ids the
    listing never carried — a Simkl title with only an anidb id learns its tvdb, a
    tvdb-only one its tmdb — and the group used to keep the key it was filed
    under while its ids named another. Measured on the development copy: 1,256
    stored titles in that state, each one a card that could not join another
    service's listing of the same show. Settling the group's ids against its key
    re-keys the title at the moment it learns enough to, everywhere it is held.
    THE SETTLE IS DECIDED BEFORE THE LOCK (db.transaction's `prepare`): the ids
    each group will hold once the update is written are read by the update's own
    expression (`_settlings_sql`), the settle planned from them
    (identity.plan_settles), and only the update and the settle's writes are made
    under the lock.
    """
    items = list(items)
    # One answer per title: a pass names each once, and should one arrive twice
    # the later is the one written, as it was when each was written in turn.
    answers = {(str(service_id), str(media)): _enrichment_row(source, fields)
               for service_id, media, fields in items}
    if not answers:
        return len(items)
    found = await db.fetch_value(_ANSWERED_SQL, {
        "source": source, "ids": json.dumps([list(pair) for pair in answers])})
    order = {pair: at for at, pair in enumerate(answers)}
    targets = sorted(((str(key), str(media), str(own)) for key, media, own
                      in json.loads(found or "[]") if (str(own), str(media)) in answers),
                     key=lambda target: (order[(target[2], target[1])], target[0]))
    for at in range(0, len(targets), ENRICHMENT_TURN):
        turn = json.dumps([[key, *answers[(own, media)], own]
                           for key, media, own in targets[at:at + ENRICHMENT_TURN]])

        def prepare(conn, turn=turn) -> identity.SettlePlan:
            return _plan_enrichment(conn, source, turn, now)
        # Decided before its turn, and used in it while what it read reads the
        # same (db.transaction's `decided`).
        await db.transaction(
            lambda conn, settles, turn=turn: _write_enrichment(conn, source, turn, settles, now),
            prepare=prepare, decided=await db.decide(prepare))
    return len(items)


def _plan_enrichment(conn: db.Connection, source: str, written: str,
                     now: int) -> identity.SettlePlan:
    """The settle a turn of answers (`written`, `_update_enriched_sql`'s rows)
    makes once its update is written — reads only — each title in its answer's
    order, which is the order a settle decides them in."""
    (rows,) = conn.execute(_settlings_sql(source), {"rows": written}).fetchone()
    # A PATCHED GROUP'S IDS ARRIVE AS AN OBJECT, an unpatched one's as text:
    # json_patch's answer is JSON to SQLite, and json_array nests it as such.
    by_key = {str(key): (media, ids if isinstance(ids, dict) else json.loads(ids or "{}"), own)
              for key, media, ids, own in json.loads(rows or "[]")}
    return identity.plan_settles(conn, [
        identity.Settling(str(media), ids, key, identity.per_source_key(source, own))
        for key in dict.fromkeys(str(row[0]) for row in json.loads(written)) if key in by_key
        for media, ids, own in (by_key[key],) if own is not None], now=now)


def _write_enrichment(conn: db.Connection, source: str, written: str,
                      settles: identity.SettlePlan, now: int) -> None:
    """A turn of answers written, and the settle `_plan_enrichment` decided."""
    conn.execute(_update_enriched_sql(source), {"rows": written, "now": now})
    settles.apply(conn)


async def _service_titles(source: str, id_namespace: str, media: tuple[str, ...] | None, *,
                          owed: bool) -> dict[tuple[int, str], str]:
    """{(service id, media): title} for each title `source` has a group for —
    only those still carrying no answer where `owed`."""
    sql = (f"SELECT json_extract({source}_ids_json, '$.' || ?) AS service_id, media, "
           f"{source}_title AS title FROM {tables.TITLES} WHERE "
           + (f"{source}_enriched = 0 AND {source}_id IS NOT NULL" if owed
              else f"{source}_id IS NOT NULL"))
    params: list = [id_namespace]
    if media:
        sql += f" AND media IN ({', '.join('?' * len(media))})"
        params.extend(media)
    sql += f" ORDER BY {source}_fetched_at DESC"
    out: dict[tuple[int, str], str] = {}
    for row in await db.fetch_all(sql, tuple(params)):
        try:
            key = (int(row["service_id"]), str(row["media"]))
        except (TypeError, ValueError):
            continue  # a title this service never named in its own id space
        out.setdefault(key, str(row["title"] or ""))
    return out


async def unenriched(source: str, id_namespace: str,
                     media: tuple[str, ...] | None = None) -> dict[tuple[int, str], str]:
    """{(service id, media): title} for stored titles still carrying no answer —
    the groups a service's enrichment drain owes, read through the index on
    `enriched` rather than out of every group the service has.

    THIS IS THE DRAIN'S WHOLE WORKLIST, and it used to be every title the service
    had a row for, each one's stored answer read back and decompressed every tick
    only to find most of them already answered (25,123 Simkl titles on the
    development copy, ~0.7 s of worker time a minute). A group that IS answered
    is kept current by being viewed (app/metadata/refresh.py), not by being
    re-read on a clock.

    IT ALSO NAMES THE GROUPS A REBUILD IS FOR: ones where the ANSWER EXISTS in the
    per-title table and this group was never given it — a lookup that lands a
    moment after the group is written, or any group whose listing and whose drain
    crossed. Without this the drain cannot tell "nobody has looked this up" from
    "the answer is here and this group has not been handed it".
    """
    return await _service_titles(source, id_namespace, media, owed=True)


async def stored_titles(source: str, id_namespace: str, media: tuple[str, ...] | None = None,
                        ) -> dict[tuple[int, str], str]:
    """{(service id, media): title} for every title `source` has a group for.

    A PURE "WHAT DOES THE STORE NAME" QUESTION, with exactly one reason to change.
    Deciding which of these are still worth FETCHING belongs to the drain: a title
    already answered, one inside its failure backoff, and one whose stored answer
    came from a narrower extraction than the current one are three different
    exclusions, and only the drain can see the last of them.

    KEYED ON THE SERVICE'S OWN NUMERIC ID, read out of the group's ids.
    """
    return await _service_titles(source, id_namespace, media, owed=False)


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
    """When a just-answered episode falls due again.

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


_EPISODE_ANSWER_WRITES: dict[str, str] = {
    # THE LOOKUP WINS EXCEPT WHERE IT HAS NOTHING TO SAY. A listing already stored
    # an episode title from the calendar feed, and a season the service could not
    # name must not blank it.
    "title": "CASE WHEN given.title != '' THEN given.title ELSE held.title END",
    "overview": "given.overview", "first_aired": "given.first_aired",
    "episode_type": "given.episode_type", "runtime": "given.runtime",
    "rating": "given.rating", "votes": "given.votes", "stale_after": "given.stale_after",
}
_EPISODE_ANSWER_REWRITES: dict[str, str | None] = {
    "title": "given.title = ''", "overview": None, "first_aired": None,
    "episode_type": None, "runtime": None, "rating": None, "votes": None}


def episode_upsert_sql(source: str, writes: Mapping[str, str],
                       rewrites: Mapping[str, str | None]) -> str:
    """An upsert of `source`'s description of each episode a batch names — a
    row's key and coordinate (title key, unit, media, season, number), then
    `writes`' columns its group has, then its `fetched_at`, in that order — each
    stated column written by its rule in `writes`, the clock by
    `ceiling_clock`. One statement however many episodes."""
    names = [name for name in writes if groups.has(tables.EPISODES, source, name)]
    sets = [f"{source}_{name} = {groups.spelled(writes[name], tables.EPISODES, source)}"
            for name in names]
    sets.append(f"{source}_fetched_at = " + groups.spelled(
        ceiling_clock(tables.EPISODES, source, {name: rewrites[name] for name in rewrites
                                                if name in names}),
        tables.EPISODES, source))
    columns = ("title_key", "unit", "media", "season", "number",
               *(f"{source}_{name}" for name in names), f"{source}_fetched_at")
    return (f"INSERT INTO {tables.EPISODES} ({', '.join(columns)})\n{json_rows(len(columns))}\n"
            "ON CONFLICT(title_key, unit) DO UPDATE SET " + ", ".join(sets))


def episode_written(source: str, writes: Mapping[str, str]) -> tuple[str, ...]:
    """The columns `episode_upsert_sql` writes for `source`, in its order."""
    return tuple(name for name in writes if groups.has(tables.EPISODES, source, name))


class EpisodeAnswer(NamedTuple):
    """What a service's own episode list says about one episode of a title, and
    when the answer falls due again (`episode_stale_after`)."""
    title_key: str
    number: int
    fields: dict
    stale_after: int


def write_episode_answers(conn: db.Connection, source: str, media: str, season: int,
                          answers: Iterable[EpisodeAnswer], *, now: int) -> None:
    """What a service's own episode list says about each of one season's episodes,
    written over the listings' stubs — inside the caller's transaction, in one
    statement however many episodes the season has."""
    names = episode_written(source, _EPISODE_ANSWER_WRITES)
    written = []
    for answer in answers:
        fields = {**answer.fields, "stale_after": int(answer.stale_after)}
        values = {"title": str(fields.get("title") or ""),
                  "overview": str(fields.get("overview") or ""),
                  "first_aired": str(fields.get("first_aired") or ""),
                  "episode_type": str(fields.get("episode_type") or ""),
                  "runtime": fields.get("runtime"), "rating": fields.get("rating"),
                  "votes": fields.get("votes"), "stale_after": int(answer.stale_after)}
        written.append([str(answer.title_key), f"S{int(season)}E{int(answer.number)}",
                        str(media), int(season), int(answer.number),
                        *(values[name] for name in names), now])
    if written:
        conn.execute(episode_upsert_sql(source, _EPISODE_ANSWER_WRITES,
                                        _EPISODE_ANSWER_REWRITES),
                     {"rows": json.dumps(written)})


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def _emptied(source: str, alias: str) -> str:
    """SQL: the episode row aliased `alias` is one `:emptied` names for `source`
    — one whose last listing by that service the sweep deciding this takes off."""
    return (f"EXISTS (SELECT 1 FROM json_each(:emptied) x "
            f"WHERE json_extract(x.value, '$[0]') = '{source}' "
            f"AND json_extract(x.value, '$[1]') = {alias}.title_key "
            f"AND json_extract(x.value, '$[2]') = {alias}.unit)")


def _listed_by(source: str, alias: str) -> str:
    """SQL: `source` lists an episode of the title a row aliased `alias` is
    about, on any calendar, once the sweep has taken off what it is taking off
    (`_emptied`)."""
    return (f"EXISTS (SELECT 1 FROM {tables.EPISODES} e WHERE e.title_key = {alias}.title_key "
            f"AND e.{source}_air_ts IS NOT NULL AND NOT {_emptied(source, 'e')})")


def _keys(conn: db.Connection, sql: str, params: Mapping[str, Any]) -> list:
    """`sql`'s one aggregated row — a JSON array of keys — read back. A sweep's
    reads answer in one row: the driver lets go of the GIL around every row it
    steps through and must win it back, up to a switch interval each time while
    the event loop is busy."""
    (found,) = conn.execute(sql, params).fetchone()
    return json.loads(found)


def _clear(conn: db.Connection, table: str, source: str, keys: list, *,
           keep: Iterable[str] = ()) -> int:
    """Clear `source`'s group on each row of `table` named in `keys` — title
    keys, or [title key, season] / [title key, unit] pairs."""
    if not keys:
        return 0
    where = {tables.TITLES: "title_key IN (SELECT value FROM json_each(?))",
             tables.SEASONS: "(title_key, season) IN (SELECT json_extract(value, '$[0]'), "
                             "json_extract(value, '$[1]') FROM json_each(?))",
             tables.EPISODES: "(title_key, unit) IN (SELECT json_extract(value, '$[0]'), "
                              "json_extract(value, '$[1]') FROM json_each(?))"}[table]
    count = conn.execute(f"{clear_sql(table, source, keep=keep)} WHERE {where}",
                         (json.dumps(keys),)).rowcount
    if table == tables.TITLES:
        # THE ENTRY GOES WITH ITS GROUP: nothing the service said about the title
        # is left to file a later listing under, and a later listing of it is a
        # title met afresh.
        conn.execute(f"DELETE FROM {tables.SERVICE_IDS} WHERE source = ? "
                     "AND title_key IN (SELECT value FROM json_each(?))", (source, json.dumps(keys)))
    return count


def drop_empty_rows(conn: db.Connection) -> dict[str, int]:
    """Delete every season and episode row no service has a group on any more —
    inside the caller's transaction. A title's row is its identity and stays."""
    seasons = " AND ".join(groups.absent(tables.SEASONS, source)
                           for source in groups.sources()) or "0"
    episodes = " AND ".join(
        f"{groups.absent(tables.EPISODES, source)} AND "
        f"{groups.absent(tables.EPISODES, source, listed=True)}"
        for source in groups.sources()) or "0"
    return {tables.SEASONS: conn.execute(f"DELETE FROM {tables.SEASONS} WHERE {seasons}").rowcount,
            tables.EPISODES: conn.execute(
                f"DELETE FROM {tables.EPISODES} WHERE {episodes}").rowcount}


# WHAT A SWEEP CLEARS: {(table, service): the rows' keys} — title keys, or
# [title key, season] / [title key, unit] pairs — decided by `plan_unlisted` and
# `plan_past_ceilings` before the sweep's lock is taken, written by `clear`.
Clearing = dict[tuple[str, str], list]


def plan_unlisted(conn: db.Connection, cutoff: int, *,
                  emptied: Iterable[tuple[str, str, str]] = ()) -> Clearing:
    """Every service's group a title, season or episode row holds that has gone
    untouched since `cutoff` and that NO CALENDAR LISTING OF THE SERVICE STILL
    NAMES AND NO FEATURE ROW HOLDS — decided, NOT written, in a read per table
    and service and one for the holders, each answering in one row.

    `emptied` names the episode rows whose last listing by a service the
    calendar's own sweep is taking off in the same transaction — (service, title
    key, unit) — counted as no longer listed by it: the listings go first and a
    group nothing lists any more after them, the order that keeps a group while
    anything still draws it (app/calendar/entries.py's `sweep`).

    A HELD TITLE KEEPS EVERY SERVICE'S DESCRIPTION, whatever the calendar thinks
    of it. A tracker row, a board row and what either may put back are drawn with
    the store's words for the title and keep no copy of their own, so a show that
    stopped airing in March cannot lose its name in September because no calendar
    lists it any more (holders.held_keys says which rows hold).

    THE EPISODE CHECK IS PER EPISODE FOR A TITLE NOBODY HOLDS: a series in its
    fifth season stops carrying words for its first, while a title still airing
    keeps everything that still airs. A held title keeps all of its episodes and
    seasons — they are what its details panel and its tracker row read.
    """
    held = holders.held_keys(conn)
    params = {"cutoff": cutoff, "emptied": json.dumps([list(row) for row in emptied])}
    out: Clearing = {}
    for source in groups.sources():
        def stale(alias: str) -> str:
            clock = f"{alias}.{source}_fetched_at"
            return f"{clock} IS NOT NULL AND {clock} <= :cutoff"

        found = {
            tables.TITLES: [key for key in _keys(conn, (
                f"SELECT json_group_array(t.title_key) FROM {tables.TITLES} t "
                f"WHERE {stale('t')} AND NOT {_listed_by(source, 't')}"), params)
                if key not in held],
            tables.SEASONS: [pair for pair in _keys(conn, (
                f"SELECT json_group_array(json_array(s.title_key, s.season)) "
                f"FROM {tables.SEASONS} s WHERE {stale('s')} AND NOT {_listed_by(source, 's')}"),
                params) if pair[0] not in held],
            # An episode's description goes where this service no longer lists
            # THAT episode — its own listing on the row is what kept it.
            tables.EPISODES: [pair for pair in _keys(conn, (
                f"SELECT json_group_array(json_array(e.title_key, e.unit)) "
                f"FROM {tables.EPISODES} e WHERE {stale('e')} "
                f"AND ({groups.absent(tables.EPISODES, source, 'e', listed=True)} "
                f"OR {_emptied(source, 'e')})"), params) if pair[0] not in held]}
        out.update({(table, source): keys for table, keys in found.items() if keys})
    return out


def plan_past_ceilings(conn: db.Connection, now: int) -> Clearing:
    """Every group a service's own terms say may not be kept any longer —
    whoever holds or lists the title — decided, NOT written, a read per table
    and service each answering in one row. The row stays when it is written
    (`clear`): it is the title's, and the other services' words on it are theirs.

    A SERVICE DECLARES ITS CEILING (`Capabilities.retain_seconds`, read through
    the registry), so this names no service: TMDB's terms forbid keeping anything
    obtained from it for longer than six months, and a second service with a rule
    of its own is a declaration in its package, not an edit here.

    THE CLOCK IS `fetched_at`, WHEN THE SERVICE LAST ANSWERED FOR THE GROUP.
    Every answer re-obtains it — a refresh, a 304 confirming nothing changed, a
    listing naming it — and a group a viewer keeps looking at is refreshed long
    before this (app/metadata/refresh.py), so what goes here is what nobody has
    looked at in half a year. AN EPISODE STUB a listing wrote has never been
    answered for (`fetched_at` 0) and lives as long as the title's group it came
    with.
    """
    out: Clearing = {}
    for source, provider in providers.registered().items():
        retain = provider.capabilities.retain_seconds
        if retain is None:
            continue
        name = str(source)
        params = {"cutoff": now - int(retain)}
        stale = f"{name}_fetched_at IS NOT NULL AND {name}_fetched_at <= :cutoff"
        found = {
            tables.TITLES: _keys(conn, f"SELECT json_group_array(title_key) FROM {tables.TITLES} "
                                       f"WHERE {stale}", params),
            tables.SEASONS: _keys(conn, "SELECT json_group_array(json_array(title_key, season)) "
                                        f"FROM {tables.SEASONS} WHERE {stale}", params)}
        if groups.has(tables.EPISODES, name, "fetched_at"):
            # A stub goes with its title's group — the one there is, or the one
            # this plan clears.
            found[tables.EPISODES] = _keys(conn, (
                "SELECT json_group_array(json_array(e.title_key, e.unit)) "
                f"FROM {tables.EPISODES} e WHERE "
                f"(e.{name}_fetched_at > 0 AND e.{name}_fetched_at <= :cutoff) "
                f"OR (e.{name}_fetched_at = 0 AND (e.title_key IN (SELECT value FROM "
                f"json_each(:titles)) OR NOT EXISTS (SELECT 1 FROM {tables.TITLES} t "
                f"    WHERE t.title_key = e.title_key AND t.{name}_id IS NOT NULL)))"),
                {**params, "titles": json.dumps(found[tables.TITLES])})
        out.update({(table, name): keys for table, keys in found.items() if keys})
    return out


def clear(conn: db.Connection, clearing: Clearing) -> dict[tuple[str, str], int]:
    """Each group `clearing` names cleared, inside the caller's transaction — a
    statement per table and service named (and its entries' ids, for a title's
    group) — the season and episode rows nothing is left on removed after.
    Returns {(table, service): groups cleared}."""
    counts = {(table, source): _clear(conn, table, source, keys,
                                      keep=groups.LISTING if table == tables.EPISODES else ())
              for (table, source), keys in clearing.items()}
    if any(counts.values()):
        drop_empty_rows(conn)
    return counts


def report_ceilings(counts: Mapping[tuple[str, str], int]) -> dict[str, int]:
    """{service: groups cleared} of `clear`'s answer for `plan_past_ceilings`,
    each service's logged — the one record of what its terms took."""
    removed: dict[str, int] = {}
    for (_table, source), count in counts.items():
        removed[source] = removed.get(source, 0) + count
    for source, count in removed.items():
        if count:
            retain = providers.registered()[Source(source)].capabilities.retain_seconds
            logger.info("title store: removed %d %s group(s) not re-obtained inside the "
                        "%d days that service's terms allow", count, source, retain // 86400)
    return removed


def sweep_unlisted(conn: db.Connection, cutoff: int) -> dict[str, int]:
    """`plan_unlisted` written, inside the caller's transaction — after the
    calendar has swept its own listings. Returns {table: groups cleared}."""
    removed = {tables.TITLES: 0, tables.SEASONS: 0, tables.EPISODES: 0}
    for (table, _source), count in clear(conn, plan_unlisted(conn, cutoff)).items():
        removed[table] += count
    return removed


def sweep_past_ceilings(conn: db.Connection, now: int) -> dict[str, int]:
    """`plan_past_ceilings` written, inside the caller's transaction. Returns
    {source: groups cleared}, every ceiling-bound service named."""
    return {**{source: 0 for source in ceiling_sources()},
            **report_ceilings(clear(conn, plan_past_ceilings(conn, now)))}


# ---------------------------------------------------------------------------
# what other features read of a title
# ---------------------------------------------------------------------------

def title_groups_sql(wanted: Iterable[str], *, where: str = "", sources=None) -> str:
    """A query answering ONE ROW PER SERVICE GROUP of the `titles` rows `where`
    names (a condition on the table, aliased `t`): `source`, `title_key`,
    `media`, `source_id` — the group's own id — and each of `wanted` as the
    group states it. For the readers that ask about a title's services one by
    one; a reader asking one field of several services reads the row itself."""
    wanted = list(wanted)
    arms = []
    for source in sources or groups.sources():
        condition = groups.present(tables.TITLES, source, "t") + (f" AND ({where})" if where else "")
        arms.append(f"SELECT '{source}' AS source, t.title_key AS title_key, t.media AS media, "
                    f"t.{source}_id AS source_id"
                    + (", " + groups.select(tables.TITLES, source, wanted, "t") if wanted else "")
                    + f" FROM {tables.TITLES} t WHERE {condition}")
    return " UNION ALL ".join(arms)


@dataclass(frozen=True)
class StoredTitle:
    """One service's stored record of a title: that service's own id map for it,
    its own name for the title, and the network it states. Empty network means
    that service has not said — a Simkl group waits for enrichment to fill it."""
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
    merged map can carry one of each, and each group here is one service's own
    record, so whose answer is whose needs no disambiguating.
    """
    keys = [str(k) for k in dict.fromkeys(title_keys or ()) if k]
    if not keys:
        return {}
    out: dict[str, dict[str, StoredTitle]] = {}
    for row in await db.fetch_all(
            title_groups_sql(("ids_json", "network", "title"),
                             where="t.title_key IN (SELECT value FROM json_each(?))"),
            (json.dumps(keys),) * len(groups.sources())):
        out.setdefault(str(row["title_key"]), {})[str(row["source"])] = StoredTitle(
            ids=groups.loads(row["ids_json"], {}), network=str(row["network"] or ""),
            title=str(row["title"] or ""))
    return {key: dict(sorted(found.items(), key=lambda item: SOURCE_ORDER.get(item[0], 99)))
            for key, found in out.items()}


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
    entry in `by_source` names a source with a group for `title_key`.

    WHAT IT IS FOR. A calendar card shows the name, the picture and the
    description this viewer's own field order picked, and the details panel has to
    open on those rather than on whatever the describing service says. A card
    ships one word per field, and this reads the rest back out of the group it was
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
        title_groups_sql(SHOWN_FIELDS, where="t.title_key = ?", sources=names),
        (str(title_key),) * len(names))
    stored = {str(row["source"]): row for row in rows}
    out = {}
    for field_name, name in wanted.items():
        row = stored.get(name)
        if row is not None and row[field_name]:
            out[field_name] = str(row[field_name])
    return out


async def posters_for(title_keys, media) -> dict[str, dict[str, str]]:
    """{title_key: {source: poster address}} for every stored title among
    `title_keys` that some service's group gives a picture — the ORIGIN address
    the calendar's own cards draw, never proxied here. One query however many
    keys: the tracker draws a picture on each of its rows, and a title the store
    already holds costs no request at all."""
    keys = sorted({str(key) for key in title_keys if key})
    if not keys:
        return {}
    rows = await db.fetch_all(
        title_groups_sql(("poster",), where="t.media = ? AND t.title_key IN "
                                            "(SELECT value FROM json_each(?))"),
        (str(media), json.dumps(keys)) * len(groups.sources()))
    out: dict[str, dict[str, str]] = {}
    for row in sorted(rows, key=lambda r: SOURCE_ORDER.get(str(r["source"]), 99)):
        if row["poster"]:
            out.setdefault(str(row["title_key"]), {}).setdefault(str(row["source"]),
                                                                 str(row["poster"]))
    return out


async def id_maps(title_key: str, media: str | None, own: tuple[str, str] | None
                  ) -> list[tuple[str, dict]]:
    """[(media, the group's own ids)] for every service group of the title
    `title_key` names, or stating the id `own` names — every spelling the
    title's groups have answered to, for a reader that has to recognise any of
    them."""
    if own is None:
        rows = await db.fetch_all(title_groups_sql(("ids_json",), where="t.title_key = ?"),
                                  (str(title_key),) * len(groups.sources()))
    else:
        arms = " UNION ALL ".join(
            f"SELECT t.media AS media, t.{source}_ids_json AS ids_json FROM {tables.TITLES} t "
            f"WHERE t.title_key = ? OR (t.media = ? AND "
            f"CAST(json_extract(t.{source}_ids_json, '$.' || ?) AS TEXT) = ?)"
            for source in groups.sources())
        rows = await db.fetch_all(arms, (str(title_key), media, own[0], own[1])
                                  * len(groups.sources()))
    out = []
    for row in rows:
        ids = groups.loads(row["ids_json"], {})
        if ids:
            out.append((str(row["media"]), ids))
    return out


async def titles_for(title_key: str) -> list:
    """Every service group of the title `title_key` names: (source, media,
    title, ids_json)."""
    return await db.fetch_all(
        title_groups_sql(("title", "ids_json"), where="t.title_key = ?"),
        (str(title_key),) * len(groups.sources()))
