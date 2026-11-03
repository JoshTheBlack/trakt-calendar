"""What a service said about one title, KEPT: its details answer written into the
store's groups, and read back as the same answer.

WHY THE ANSWER IS STORED RATHER THAN ASKED FOR EACH TIME. Every surface that
draws a title — a details panel, a tracker row, a board row, an export — used to
ask a service live (through a response cache with an hours-long life) or keep a
copy of its own, and the copies are how one title came to be described five
ways. The store is one row per title, per season and per episode, each holding
a group of columns per service (app/metadata/groups.py); a surface reads them,
and a stale one is re-read behind the reader (app/metadata/refresh.py).

THE ANSWER IS EACH SERVICE'S OWN, UNCHANGED. A service package reads its own
payloads (app/providers/<service>/detail.py's `fetch_details`, whose key set
base.DetailPort fixes and tests/providers/test_protocol_conformance.py holds);
this module only files that answer and gives it back. So what a service means by
a field — Simkl's anime seasons, Trakt's season names, TMDB's days — stays in the
service's package, and a stored answer and a fresh one are the same answer.

WHAT MARKS A GROUP AS DESCRIBED, since a calendar listing writes the same groups
and knows less. A title's group is described once `season_count` is set — a
film's is 0 — which nothing but this module writes (a listing never knows it). A
season's group is described when its `air_dates_json` holds one entry per
episode its `episode_count` names: a season list (`write_season_list`) states
the count and not the dates, so a season that has grown since it was described
reads as not described and is asked again, which is the right answer to a new
episode.

ADDRESSED BY THE SERVICE'S OWN ID. A service's group of a title is the group
whose entry (app/metadata/rows.py's `own_id`, every spelling of it in
`service_ids`) is the id the service is asked by. AN ANSWER ABOUT ANOTHER OF THE
SERVICE'S ENTRIES FOR A TITLE — a second id it holds for the title, whose group
is the first's — is not filed: a group, its seasons and its episodes are one
entry's words, and another entry numbers its own (rows.py says why).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from .. import db
from ..media import artwork
from ..providers import season as season_rules
from ..providers.base import Media, Video, VideoKind, genre_label, status_label
from . import groups, identity, rows, tables
from . import ids as id_rules

logger = logging.getLogger(__name__)

# HOW LONG A DESCRIBED TITLE IS TAKEN AS CURRENT before a surface showing it asks
# for it again (app/metadata/refresh.py). Three days: a title's status, network,
# season count and videos move on that scale — a renewal, a new season announced,
# a trailer released — and a viewer looking at a row every day would otherwise
# read a week-old description of a show in its premiere week. Seasons have their
# own clock, keyed to when their episodes air (`season_stale_after`).
TITLE_REFRESH_SECONDS = 3 * 24 * 60 * 60

# THE ANSWER'S KEY SET, which a stored answer must share with a fresh one —
# base.DetailPort's, held there by the conformance test and here by
# tests/metadata/test_panel.py.
ANSWER_KEYS = frozenset({
    "title", "year", "overview", "season_overview", "status", "network", "country",
    "language", "anime_type", "release_types_by_country", "ids", "poster", "runtime",
    "genres", "genre_slugs", "rating", "imdb_rating", "certification", "videos",
    "first_aired", "homepage", "season", "season_title", "season_count", "seasons",
    "cast", "episodes", "keywords", "show_type",
})


# ---------------------------------------------------------------------------
# finding a service's group
# ---------------------------------------------------------------------------

def _namespace(source: str) -> str:
    return rows.ID_NAMESPACE[str(source)]


def service_rows(conn: db.Connection, source: str, media: str, own_id: Any,
                 ids: Mapping[str, Any] | None = None) -> list[dict]:
    """`source`'s group of the title it knows as `own_id` — a list of at most
    one, empty where it has none.

    BY THE TITLE'S KEY FIRST, which is indexed: `ids` (whatever the caller knows
    of the title) resolve to it, and the service's group under it is read and
    checked for the service's own id. A title the ids do not resolve, or whose
    key holds no group of this entry, is found by the entry's own id
    (`service_ids`) — the path for a caller that knows a service's id and
    nothing else (a queued refresh).

    ONE TITLE IS A BATCH OF ONE (`services_rows`). Each group is a dict of its
    columns under their own names, with `source`, `media`, `source_id` (its own
    id) and `title_key`.
    """
    return services_rows(conn, [(source, media, own_id, ids)])[0]


def services_rows(conn: db.Connection,
                  wanted: Iterable[tuple[str, str, Any, Mapping[str, Any] | None]]
                  ) -> list[list[dict]]:
    """`service_rows` for each (source, media, own id, ids) of `wanted`, in a
    number of reads that does not grow with them — what a refresh pass's
    failures are marked on, read before its write takes the lock."""
    wanted = [(str(source), str(media), own_id, ids) for source, media, own_id, ids in wanted]
    held = _Rows(conn, identity.Planner(conn, now=db.now()))
    held.prefetch([(source, media, own_id, ids) for source, media, own_id, ids in wanted])
    return [held.found(source, media, own_id, ids) for source, media, own_id, ids in wanted]


# EVERY COLUMN OF A TITLE'S GROUP, in the order a batch's reads spell it: the
# words and what files and keeps them (groups.WORDS / NOT_WORDS, which
# tests/metadata/test_retention.py holds to the schema), so a group read here is
# the whole group.
_ROW_COLUMNS = (*groups.NOT_WORDS[tables.TITLES], *groups.WORDS[tables.TITLES])
_ROW_KEYS = ("source", "media", "source_id", "title_key", *_ROW_COLUMNS)


def _row_json(source: str) -> str:
    """A title's group of `source` as one JSON array, in `_ROW_KEYS`' order."""
    return (f"json_array('{source}', t.media, t.{source}_id, t.title_key, "
            + ", ".join(groups.column(tables.TITLES, source, name, "t")
                        for name in _ROW_COLUMNS) + ")")


# EACH READ ANSWERS IN ONE ROW, however many titles it names (identity.py's reads
# say why: the driver gives up the GIL around every row it steps through, and a
# read made inside a write transaction holds the lock for all of it).
#   Every service's group under the keys named, through the key's own index.
def _under_keys_sql() -> str:
    arms = [f"SELECT {_row_json(source)} AS r FROM {tables.TITLES} t "
            f"WHERE t.title_key IN (SELECT value FROM json_each(?)) "
            f"AND {groups.present(tables.TITLES, source, 't')}"
            for source in groups.sources()]
    return f"SELECT json_group_array(json(r)) FROM ({' UNION ALL '.join(arms)})"


def _stating_sql(sources: Iterable[str]) -> str:
    """Every group of each of `sources` whose entry is one of the own ids named
    for it (`:<source>`, a JSON array of text) — through `service_ids`, which
    holds every spelling of an entry, an arm per service. Service names come
    from the registry, never from input."""
    arms = [f"SELECT {_row_json(source)} AS r FROM {tables.SERVICE_IDS} i "
            f"JOIN {tables.TITLES} t ON t.title_key = i.title_key "
            f"WHERE i.source = '{source}' AND i.is_primary = 1 "
            f"AND i.service_id IN (SELECT value FROM json_each(:{source})) "
            f"AND {groups.present(tables.TITLES, source, 't')}"
            for source in sources]
    return f"SELECT json_group_array(json(r)) FROM ({' UNION ALL '.join(arms)})"


class _Rows:
    """The title groups a batch of answers is about: what the store holds, read
    in a few statements, and each answer's writes applied here as the answer is
    planned (`plan`) — so every answer finds exactly the groups, and reads
    exactly the words, it would have found had each answer before it been
    written in a transaction of its own.

    WHAT IS READ UP FRONT (`prefetch`) is `service_rows`' two reads for every
    title at once: the groups under each title's key, and — for a title whose
    key holds none of its entry, or whose ids name no key — every group of each
    entry named. Read again only when an answer needs what was not read: a key
    no answer resolved to at first, or, once a re-key in the batch has moved
    groups between keys, every named entry's group (`found` says why).
    """

    def __init__(self, conn: db.Connection, planner: identity.Planner):
        self._conn = conn
        self._planner = planner
        # (source, media, own id): the group as the batch has left it.
        self._rows: dict[tuple[str, str, str], dict] = {}
        # (source, media, own id): the groups stating it, and what each states.
        self._stating: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
        self._states: dict[tuple[str, str, str], set[str]] = {}
        self._keys_read: set[str] = set()
        self._whole: set[tuple[str, str, str]] = set()
        self._titles: set[tuple[str, str, str]] = set()
        self._after_rekey = False

    # -- reading --------------------------------------------------------------

    def prefetch(self, wanted: Iterable[tuple[str, str, Any, Mapping[str, Any] | None]]) -> None:
        """Read what finding each (source, media, own id, ids) of `wanted` takes:
        their identities (identity.Planner.prefetch), the groups under the keys
        they resolve to, and every named entry's group those keys do not hold —
        a few statements however many there are."""
        wanted = [(str(source), str(media), str(own_id), _with_own(source, own_id, ids))
                  for source, media, own_id, ids in wanted]
        self._titles.update((source, media, own_id) for source, media, own_id, _ids in wanted)
        self._planner.prefetch(identity.Settling(media, ids) for _s, media, _o, ids in wanted)
        keys = [self._planner.resolve(media, ids) for _s, media, _o, ids in wanted]
        self._read_keys(key for key in keys if key is not None)
        self._read_whole([(source, media, own_id)
                          for (source, media, own_id, _ids), key in zip(wanted, keys)
                          if key is None or not self._under(source, media, own_id, key)])

    def _read_keys(self, keys: Iterable[str]) -> None:
        wanted = sorted({key for key in keys if key not in self._keys_read})
        if not wanted:
            return
        (found,) = self._conn.execute(
            _under_keys_sql(), (json.dumps(wanted),) * len(groups.sources())).fetchone()
        for values in json.loads(found):
            self._hold(dict(zip(_ROW_KEYS, values)))
        self._keys_read.update(wanted)

    def _read_whole(self, titles: Iterable[tuple[str, str, str]]) -> None:
        wanted = {title for title in titles if title not in self._whole}
        if not wanted:
            return
        by_source: dict[str, set[str]] = {}
        for source, _media, own_id in wanted:
            by_source.setdefault(source, set()).add(own_id)
        sources = sorted(by_source)
        (found,) = self._conn.execute(_stating_sql(sources), {
            source: json.dumps(sorted(by_source[source])) for source in sources}).fetchone()
        for values in json.loads(found):
            self._hold(dict(zip(_ROW_KEYS, values)))
        self._whole.update(wanted)

    def _hold(self, row: dict) -> None:
        """A group read from the store — unless the batch already holds it, and
        so knows better what it says now."""
        row["source_id"] = str(row["source_id"])
        unique = (str(row["source"]), str(row["media"]), row["source_id"])
        if unique not in self._rows:
            self._rows[unique] = row
            self._index(unique)

    def _index(self, unique: tuple[str, str, str]) -> None:
        """Note which own ids a group states — its entry and the id its id map
        states for the service — anew."""
        source, media, source_id = unique
        stated = _loads(self._rows[unique]["ids_json"]).get(_namespace(source))
        states = {source_id, *(() if stated is None else (str(stated),))}
        for own_id in self._states.get(unique, set()) - states:
            self._stating[(source, media, own_id)].discard(unique)
        for own_id in states:
            self._stating.setdefault((source, media, own_id), set()).add(unique)
        self._states[unique] = states

    def _under(self, source: str, media: str, own_id: str, key: str) -> list[dict]:
        """The groups stating `own_id` filed under `key` now."""
        return [self._rows[unique] for unique in self._stating.get((source, media, own_id), ())
                if self._planner.moved_to(self._rows[unique]["title_key"]) == key]

    def occupant(self, source: str, key: str) -> str | None:
        """The entry `source`'s group under `key` is now — None where it has
        none."""
        self._read_keys([key])
        for (held_source, _media, own), row in self._rows.items():
            if held_source == source and self._planner.moved_to(row["title_key"]) == key:
                return own
        return None

    def found(self, source: str, media: str, own_id: Any,
              ids: Mapping[str, Any] | None) -> list[dict]:
        """`service_rows`' answer as the batch stands: the group of `source`
        under the key `ids` resolve to now that states `own_id`; else the group
        whose entry `own_id` is, wherever it is filed."""
        source, media, own_id = str(source), str(media), str(own_id)
        if self._planner.rekeyed and not self._after_rekey:
            # A RE-KEY MOVES GROUPS BETWEEN KEYS, so the groups read under a key
            # are no longer all the groups under it: a group filed under the key
            # retired is under the new one now. Every named entry's group is
            # read, once, and the keys are then read off the groups themselves.
            self._after_rekey = True
            self._read_whole(self._titles)
        key = self._planner.resolve(media, _with_own(source, own_id, ids))
        if key is not None:
            self._read_keys([key])
            first = self._under(source, media, own_id, key)
            if first:
                return first
        self._read_whole([(source, media, own_id)])
        return sorted((self._rows[unique] for unique in
                       self._stating.get((source, media, own_id), ())),
                      key=lambda row: str(row["source_id"]))

    # -- writing --------------------------------------------------------------

    def changed(self, row: dict) -> None:
        """`row` was rewritten by the answer being planned."""
        self._index((str(row["source"]), str(row["media"]), str(row["source_id"])))

    def create(self, row: dict) -> dict:
        """A group the answer being planned adds."""
        unique = (str(row["source"]), str(row["media"]), str(row["source_id"]))
        self._rows[unique] = {**{name: groups.EMPTY[name] for name in _ROW_COLUMNS}, **row}
        self._index(unique)
        return self._rows[unique]

    def forget(self, row: dict) -> None:
        """`row` is no group any more: its entry moved onto a title another of
        its service's entries holds the group of (merge.entry drops it)."""
        unique = (str(row["source"]), str(row["media"]), str(row["source_id"]))
        self._rows.pop(unique, None)
        for own_id in self._states.pop(unique, set()):
            self._stating.get((unique[0], unique[1], own_id), set()).discard(unique)


def _with_own(source: str, own_id: Any, ids: Mapping[str, Any] | None) -> dict:
    """`ids` with the service's own id for the title — what `service_rows`
    resolves the title's key from."""
    return {**dict(ids or {}), _namespace(source): own_id}


def described_row(found: list):
    """The described group among a service's groups for one title, or None."""
    return next((row for row in found if row["season_count"] is not None), None)


def season_described(row) -> bool:
    """Whether a season's group knows the season's SHAPE — one date (or none)
    per episode its count names — rather than only what a season list said of
    it. A tracker row's total and dates read nothing more; a panel's season tab
    also needs the episodes themselves (`read`)."""
    return row is not None and len(_loads_list(row["air_dates_json"])) == (
        row["episode_count"] if row["episode_count"] is not None else -1)


def _title_rows(conn: db.Connection, source: str, media: str, own_id: Any,
                ids: Mapping[str, Any] | None, now: int) -> list:
    """A service's group of a title, with a bare one made where it has none yet —
    a season and a service's words for a title belong to that service's group.
    The bare group is filed under the title's key (identity.settle) and is NOT
    described (`season_count` unset), so the next surface that draws the title
    has it described in full (app/metadata/refresh.py). A title whose group of
    the service is another entry's gets none: [] (the module docstring says
    why)."""
    found = service_rows(conn, source, media, own_id, ids)
    if found:
        return found
    stated = id_rules.canonical(ids or {})
    stated[_namespace(source)] = id_rules.typed(_namespace(source), own_id)
    settled = identity.settle(conn, media, stated,
                              fallback=identity.per_source_key(source, own_id), now=now)
    key = settled.key if settled is not None else identity.per_source_key(source, own_id)
    own = rows.own_id(source, own_id, stated)
    conn.execute(_bare_sql(source), {"rows": json.dumps([[
        str(media), own, json.dumps(stated, sort_keys=True), now, 0, None, 0]]),
        "keys": json.dumps([key]), "now": now})
    rows.file_ids(conn, [[str(source), str(media), str(own_id), own, key]])
    return service_rows(conn, source, media, own_id, ids)


# A BARE GROUP, where the title has none of the service's: the entry, the ids it
# stated and its clocks, the schema's empties for every word. Each row: media,
# own id, ids, fetched_at, stale_after, failed_at, fail_count — the key it is
# filed under beside them.
def _bare_sql(source: str) -> str:
    names = ("id", "ids_json", "fetched_at", "stale_after", "failed_at", "fail_count")
    selected = ["json_extract(:keys, '$[' || r.key || ']')", "json_extract(r.value, '$[0]')",
                ":now", *(f"json_extract(r.value, '$[{at}]')" for at in range(1, 1 + len(names)))]
    return (f"INSERT INTO {tables.TITLES} (title_key, media, created_at, "
            + ", ".join(f"{source}_{name}" for name in names) + ")\n"
            f"SELECT {', '.join(selected)} FROM json_each(:rows) r WHERE true ORDER BY r.key\n"
            "ON CONFLICT(title_key) DO UPDATE SET "
            + ", ".join(f"{source}_{name} = CASE WHEN {tables.TITLES}.{source}_id IS NULL "
                        f"THEN excluded.{source}_{name} ELSE {tables.TITLES}.{source}_{name} END"
                        for name in reversed(names)))


def _season_columns(source: str, names: Iterable[str]) -> list[str]:
    return [name for name in names if groups.has(tables.SEASONS, source, name)]


def _season_upsert_sql(source: str, writes: Mapping[str, str],
                       rewrites: Mapping[str, str | None]) -> str:
    """An upsert of `source`'s group of each season a batch names: its title key
    and season, then `writes`' columns its group has, then `fetched_at` — each
    written by its rule, the clock by `rows.ceiling_clock`."""
    names = _season_columns(source, writes)
    sets = [f"{source}_{name} = {groups.spelled(writes[name], tables.SEASONS, source)}"
            for name in names]
    sets.append(f"{source}_fetched_at = " + groups.spelled(
        rows.ceiling_clock(tables.SEASONS, source,
                           {name: rule for name, rule in rewrites.items() if name in names}),
        tables.SEASONS, source))
    columns = ("title_key", "season", *(f"{source}_{name}" for name in names),
               f"{source}_fetched_at")
    return (f"INSERT INTO {tables.SEASONS} ({', '.join(columns)})\n"
            f"{rows.json_rows(len(columns))}\n"
            "ON CONFLICT(title_key, season) DO UPDATE SET " + ", ".join(sets))


def _season_row(source: str, writes: Mapping[str, str], key: str, season: int,
                values: Mapping[str, Any], fetched_at: int) -> list:
    """One season as `_season_upsert_sql(source, writes, ...)` reads it."""
    return [key, int(season), *(values[name] for name in _season_columns(source, writes)),
            fetched_at]


# A SEASON'S SHAPE (`write_season_summary`): how many episodes and their days.
_SHAPE_WRITES = {
    "episode_count": "given.episode_count",
    "premiere": "CASE WHEN given.premiere != '' THEN given.premiere ELSE held.premiere END",
    "air_dates_json": "given.air_dates_json", "stale_after": "given.stale_after",
    "failed_at": "NULL", "fail_count": "0",
}
_SHAPE_REWRITES: dict[str, str | None] = {
    "episode_count": None, "premiere": "given.premiere = ''", "air_dates_json": None}


# What a season summary says that its shape must give back.
_SUMMARY_FIELDS = ("total", "cadence", "premiere", "finale", "started_airing", "finished_airing")


def summary_shape(summary: Mapping[str, Any]) -> list[str | None]:
    """A season summary's shape as the store keeps it: the known days, then an
    empty entry for every episode not yet dated (see `write_season_summary`)."""
    total = int(summary.get("total") or 0)
    known = sorted(str(day)[:10] for day in summary.get("air_dates") or [] if day)
    return [*known, *([None] * max(0, total - len(known)))]


def keeps_the_answer(summary: Mapping[str, Any], today: date) -> bool:
    """Whether the shape a summary would be kept as gives the SAME summary back
    against `today` — the store is a copy of the service's answer and must never
    change it. A summary whose flags its own days do not explain (a service
    deriving them some other way) is answered and not kept."""
    try:
        days = [date.fromisoformat(day) if day else None for day in summary_shape(summary)]
    except ValueError:
        return False
    again = season_rules.derive_season(days, today)
    return all(again.get(field) == summary.get(field) for field in _SUMMARY_FIELDS)


def write_season_summary(conn: db.Connection, source: str, media: str, own_id: Any,
                         summary: Mapping[str, Any], *, ids: Mapping[str, Any] | None,
                         now: int) -> None:
    """File the SHAPE of one season — a service's season summary
    (app/providers/season.py's: how many episodes, the days they air) — onto its
    season's group, inside the caller's transaction. Its name, description and
    cast are left as they were: a summary does not state them, and a panel's own
    description of the season is what does.

    THE SUMMARY STATES ITS KNOWN DAYS AND ITS TOTAL, and that is enough to keep
    the season as `derive_season` reads it: the known days, then an empty entry
    for every episode not yet dated. The rule is about how many there are and
    which days are known, never about which episode an undated day belongs to,
    so the stored shape gives back the summary it came from.
    """
    shape = summary_shape(summary)
    known = [day for day in shape if day]
    days = [date.fromisoformat(day) if day else None for day in shape]
    values = {"episode_count": len(shape), "premiere": known[0] if known else "",
              "air_dates_json": json.dumps(shape), "stale_after": season_stale_after(days, now),
              "failed_at": None, "fail_count": 0}
    written = [_season_row(source, _SHAPE_WRITES, row["title_key"], int(summary["season"]),
                           values, now)
               for row in _title_rows(conn, source, media, own_id, ids, now)]
    if written:
        conn.execute(_season_upsert_sql(source, _SHAPE_WRITES, _SHAPE_REWRITES),
                     {"rows": json.dumps(written)})


# The words a feature's record may seed, and how to tell the store already has
# one: a service's group stating it. Constants, never input.
_SEEDED_WORDS = {"title": "{} != ''", "network": "{} != ''",
                 "year": "{} != ''", "runtime": "{} IS NOT NULL"}


def _stated_anywhere(word: str) -> str:
    """SQL over a title's row: some service's group states `word`."""
    tests = [_SEEDED_WORDS[word].format(f"{source}_{word}") for source in groups.sources()
             if groups.has(tables.TITLES, source, word)]
    return "(" + " OR ".join(tests or ["0"]) + ")"


def seed_words(conn: db.Connection, media: str, ids: Mapping[str, Any] | None, *,
               title: str = "", network: str = "", year: Any = None,
               runtime: int | None = None, season: int | None = None,
               season_name: str = "", episode_count: int | None = None,
               now: int) -> str | None:
    """Give a title the store has NO words for the words a feature's record
    carries, inside the caller's transaction. Returns the service they were
    filed under, or None when nothing was written.

    FOR A RECORD THAT ARRIVES CARRYING WORDS AND NO SERVICE TO ANSWER FOR THEM: a
    season a month settled and the viewer has just taken back onto their list, a
    held row from an old migration's stash, a board row added from a search, a
    restored backup. Each was drawn with these words; a tracker's or a board's
    row keeps none of its own, so without this it would draw none until its
    title is described.

    A SEED, NOT AN ANSWER. Filed under the first service in the declared order
    the record's ids name, on a group left undescribed (`season_count` unset), so
    the first surface that draws the title has that service describe it
    (app/metadata/refresh.py) and its own words replace these. A WORD ANY SERVICE
    ALREADY STATES FOR THE TITLE IS NOT SEEDED, each word on its own: the store's
    words are always better than a record's copy of them.

    A RECORD OF ONE SEASON may carry that season's own name and episode total (a
    board row that is one season); they seed that season's group the same way —
    only where no service has a group for the season — and, holding no dates, it
    reads as not described, so the season is described when it is drawn. A
    service whose season group states no name is given the total alone.
    """
    words = {"title": str(title or ""), "network": str(network or ""),
             "year": str(year) if year not in (None, "") else "",
             "runtime": runtime if isinstance(runtime, int) else None}
    has_season = season is not None and bool(season_name or episode_count)
    source, own = next(((source, (ids or {}).get(rows.ID_NAMESPACE[source]))
                        for source in rows.SOURCE_ORDER
                        if (ids or {}).get(rows.ID_NAMESPACE[source]) not in (None, "")),
                       (None, None))
    if source is None:
        return None
    key = identity.resolve(conn, media, ids)
    if key is not None:
        held = conn.execute(
            "SELECT " + ", ".join(_stated_anywhere(word) for word in _SEEDED_WORDS)
            + f" FROM {tables.TITLES} WHERE title_key = ?", (key,)).fetchone()
        if held is not None:
            words = {name: (None if name == "runtime" else "") if stated else words[name]
                     for (name, stated) in zip(_SEEDED_WORDS, held)}
        described = " OR ".join(groups.present(tables.SEASONS, name)
                                for name in groups.sources())
        if has_season and conn.execute(
                f"SELECT 1 FROM {tables.SEASONS} WHERE title_key = ? AND season = ? "
                f"AND ({described}) LIMIT 1", (key, int(season))).fetchone():
            has_season = False
    seeded = {name: value for name, value in words.items()
              if value not in (None, "") and groups.has(tables.TITLES, source, name)}
    if not seeded and not has_season:
        return None
    for row in _title_rows(conn, source, media, own, ids, now):
        if seeded:
            sets = [f"{source}_{name} = ?" for name in seeded]
            params = list(seeded.values())
            if "title" in seeded:
                sets.append(f"{source}_title_fold = ?")
                params.append(rows.fold_title(seeded["title"]))
            conn.execute(f"UPDATE {tables.TITLES} SET {', '.join(sets)} "
                         f"WHERE title_key = ? AND {source}_id = ?",
                         (*params, row["title_key"], row["source_id"]))
        if has_season:
            conn.execute(_season_upsert_sql(source, _LISTED_SEASON_WRITES,
                                            _LISTED_SEASON_REWRITES),
                         {"rows": json.dumps([_season_row(
                             source, _LISTED_SEASON_WRITES, row["title_key"], int(season),
                             {"name": str(season_name or ""),
                              "episode_count": int(episode_count or 0),
                              "premiere": "", "poster": ""}, now)])})
    return source


def write_title_facts(conn: db.Connection, source: str, media: str, own_id: Any, *,
                      ids: Mapping[str, Any] | None, title: str = "", network: str = "",
                      now: int) -> None:
    """File what a service called a title and who it says carried it
    (base.DetailPort.fetch_title_facts) onto that service's group of it, inside
    the caller's transaction. A word the service did not state is left as the
    group has it. Its clock is left alone: two words re-read are not the whole
    description re-read."""
    title, network = str(title or ""), str(network or "")
    if not (title or network):
        return
    for row in _title_rows(conn, source, media, own_id, ids, now):
        sets, params = [], []
        if title:
            sets += [f"{source}_title = ?", f"{source}_title_fold = ?"]
            params += [title, rows.fold_title(title)]
        if network:
            sets.append(f"{source}_network = ?")
            params.append(network)
        conn.execute(f"UPDATE {tables.TITLES} SET {', '.join(sets)} "
                     f"WHERE title_key = ? AND {source}_id = ?",
                     (*params, row["title_key"], row["source_id"]))


# ---------------------------------------------------------------------------
# writing an answer
# ---------------------------------------------------------------------------

def answered(answer: Mapping[str, Any], source: str) -> bool:
    """Whether a service's answer says anything about a title — its own id for
    it, or a name, a description or episodes. A service asked about an id it
    does not know answers the full key set, empty, and storing that would mark a
    title described by a service that has never heard of it."""
    return bool((answer.get("ids") or {}).get(_namespace(source)) or answer.get("title")
                or answer.get("overview") or answer.get("episodes"))


def _ratings(answer: Mapping[str, Any], source: str) -> dict:
    """{service: score} — the service's own under its own name and IMDb's under
    IMDb's, exactly as a listing files them (rows._title_values)."""
    out = {}
    if answer.get("rating") is not None:
        out[str(source)] = answer["rating"]
    if answer.get("imdb_rating") is not None:
        out["imdb"] = answer["imdb_rating"]
    return out


def _video_record(video: Video) -> dict:
    return {"site": video.site, "video_id": video.video_id, "url": video.url,
            "title": video.title, "kind": str(video.kind), "official": video.official,
            "published": video.published, "sources": list(video.sources)}


def _video_from(record: Mapping[str, Any]) -> Video | None:
    try:
        return Video(site=str(record["site"]), video_id=str(record.get("video_id") or ""),
                     url=str(record["url"]), title=str(record.get("title") or ""),
                     kind=VideoKind.read(record.get("kind")),
                     official=record.get("official"),
                     published=record.get("published"),
                     sources=tuple(str(s) for s in record.get("sources") or ()))
    except (KeyError, TypeError):
        return None


def local_day(first_aired: Any, tz: ZoneInfo) -> date | None:
    """The calendar day an episode airs on in `tz`: a bare day as itself (a
    service that dates without timing means that day everywhere), an instant
    converted. None when it has no date or one this cannot read."""
    text = str(first_aired or "").strip()
    if not text:
        return None
    try:
        if "T" not in text:
            return date.fromisoformat(text[:10])
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def season_stale_after(days: list[date | None], now: int) -> int:
    """When a described season falls due again: on its last episode's clock
    (rows.episode_stale_after — a season still airing or about to is re-read
    daily, a finished one monthly, an old one quarterly), and daily while any of
    its episodes is undated or it lists none, because that is a season whose
    shape is still being announced."""
    if not days or any(day is None for day in days):
        return rows.episode_stale_after(None, now)
    last = max(day for day in days if day is not None)
    noon = datetime(last.year, last.month, last.day, 12, tzinfo=timezone.utc).timestamp()
    return rows.episode_stale_after(noon, now)


# The words a description states for every title group it writes, in one spelling
# (`_title_values`), and with them what filing an answer sets on the group — the
# columns `_described_sql` writes of those a service's group has.
_DESCRIBED = ("ids_json", "year", "network", "country", "language", "certification",
              "status", "overview", "runtime", "genres_json", "ratings_json", "anime_type",
              "release_types_json", "first_aired", "homepage", "season_count", "videos_json",
              "keywords_json", "show_type", "facts_known")
_WRITTEN = (*_DESCRIBED, "title", "title_fold", "poster", "cast_json", "enriched",
            "fetched_at", "stale_after", "failed_at", "fail_count")


def _title_values(answer: Mapping[str, Any], source: str, ids: dict) -> dict:
    year = answer.get("year")
    runtime = answer.get("runtime")
    return {
        **rows.facts_values(answer.get("keywords"), answer.get("show_type")),
        "ids_json": json.dumps(ids, sort_keys=True),
        "year": str(year) if isinstance(year, int) else str(year or ""),
        "network": str(answer.get("network") or ""),
        "country": str(answer.get("country") or ""),
        "language": str(answer.get("language") or ""),
        "certification": str(answer.get("certification") or ""),
        # STORED AS THE LOWER-CASE WORD a listing stores, so a card filtering on
        # status and a panel drawing it read one vocabulary.
        "status": str(answer.get("status") or "").strip().lower(),
        "overview": str(answer.get("overview") or ""),
        "runtime": runtime if isinstance(runtime, int) else None,
        "genres_json": json.dumps([str(g) for g in answer.get("genre_slugs") or []]),
        "ratings_json": json.dumps(_ratings(answer, source)),
        "anime_type": str(answer.get("anime_type") or ""),
        "release_types_json": json.dumps(dict(answer.get("release_types_by_country") or {})),
        "first_aired": str(answer.get("first_aired") or ""),
        "homepage": str(answer.get("homepage") or ""),
        "season_count": int(answer.get("season_count") or 0),
        "videos_json": json.dumps([_video_record(v) for v in answer.get("videos") or []
                                   if isinstance(v, Video)]),
    }


class Written(NamedTuple):
    """What filing one answer changed — what a refresh logs, so a heal can be
    followed: the title's key before and after (a re-key, or this service's group
    moving to another title), its picture before and after, and its ids."""
    key: str
    previous_key: str | None
    poster: str
    previous_poster: str
    ids: dict
    previous_ids: dict

    @property
    def rekeyed(self) -> bool:
        return self.previous_key is not None and self.previous_key != self.key

    @property
    def ids_changed(self) -> dict:
        """{id space: (before, after)} for every id this answer stated differently."""
        names = {*self.ids, *self.previous_ids}
        return {name: (self.previous_ids.get(name), self.ids.get(name)) for name in sorted(names)
                if str(self.previous_ids.get(name) or "") != str(self.ids.get(name) or "")}


class Answer(NamedTuple):
    """One service's details answer to file (`write_many`): the service, the
    title by the id that service is asked by, the answer, the season it was asked
    about (None for the title alone), and a season list read beside it."""
    source: str
    media: str
    own_id: Any
    answer: Mapping[str, Any]
    asked_season: int | None = None
    seasons: list[Mapping[str, Any]] | None = None


def write(conn: db.Connection, source: str, media: str, own_id: Any,
          answer: Mapping[str, Any], *, asked_season: int | None, tz: ZoneInfo,
          now: int) -> Written | None:
    """File one service's details answer, inside the caller's transaction:
    the title's group, and — for an answer about a season — that season's group
    and its episodes'. Returns what changed (`Written`), or None for an answer
    that says nothing (`answered`) or is about another of the service's entries
    for the title, which writes nothing.

    THE IDS GO THROUGH THE ONE RESOLUTION (identity.settle), so a service that
    now names a title differently — a re-pointed TMDB id, a newly learned one —
    re-keys it or moves this service's entry to the title it names. THIS IS THE
    ONE WRITER THAT MOVES AN ENTRY: a listing's copy of the ids never does,
    because one service's listings disagree with each other where its own
    answer about the title does not (app/metadata/__init__.py's `plan_records`).
    The ids written are what the service said this time.

    A NAME OR A PICTURE THE ANSWER DOES NOT STATE IS NOT ERASED: a listing may
    have given the group one the per-title answer leaves out.

    ONE ANSWER IS A BATCH OF ONE (`write_many`).
    """
    (filed,) = write_many(conn, [Answer(str(source), str(media), own_id, answer, asked_season)],
                          tz=tz, now=now)
    if isinstance(filed, Exception):
        raise filed
    return filed


def write_many(conn: db.Connection, answers: Iterable[Answer], *, tz: ZoneInfo,
               now: int) -> list[Written | None | Exception]:
    """`write` for each of `answers`, inside the caller's transaction, and a
    season list beside each that carries one — in a number of statements that
    does not grow with them (`apply`). One entry per answer, in order: what
    filing it changed, None for an answer that says nothing, or — as
    asyncio.gather's `return_exceptions` hands one back — the exception reading
    that answer raised, which costs the batch nothing else."""
    return apply(conn, plan(conn, answers, tz=tz, now=now))


class _Spelled(NamedTuple):
    """What filing one answer writes that the store's groups do not decide."""
    ids: dict
    values: dict
    title: str
    poster: str
    # The title's own cast, None for an answer about a season, which states that
    # season's.
    cast: str | None
    # The season the answer is about: its group's values, and each of its
    # episodes' (season, number, values).
    season: tuple[int, dict, list[tuple[int, dict]]] | None
    # Each season the list beside it names: (season, values).
    listed: list[tuple[int, dict]]


def _spell(item: Answer, *, tz: ZoneInfo, now: int) -> _Spelled:
    answer, source = item.answer, item.source
    ids = id_rules.canonical(answer.get("ids") or {})
    ids[_namespace(source)] = id_rules.typed(_namespace(source), item.own_id)
    # THE SEASON THE SERVICE ANSWERED, which a service may decide for a caller
    # that named none: Simkl answers an anime's only season (its fetch_details).
    season = answer.get("season")
    return _Spelled(
        ids=ids, values=_title_values(answer, source, ids),
        title=str(answer.get("title") or ""), poster=str(answer.get("poster") or ""),
        cast=(json.dumps(list(answer.get("cast") or [])) if item.asked_season is None
              else None),
        season=(_season_tails(int(season), answer, tz=tz, now=now)
                if item.media == str(Media.SHOW) and season not in (None, "") else None),
        # A SEASON LIST READ BESIDE THE ANSWER, else the one the answer states
        # itself (DetailPort's `seasons`): a description keeps its list, so a
        # stored answer offers the seasons a fresh one does.
        listed=_listed_tails(item.seasons if item.seasons is not None
                             else list(answer.get("seasons") or []), now))


class _Filed(NamedTuple):
    """`Written`, but for the key the title ends under, which is known once the
    batch's identities are (`apply`)."""
    settled: int
    fallback: str
    previous_key: str | None
    poster: str
    previous_poster: str
    ids: dict
    previous_ids: dict


class Planned(NamedTuple):
    """A batch of answers as `apply` writes it, decided by `plan`. Each write
    names the settle that decides its title's key and the key used where that
    names none."""
    settles: identity.SettlePlan
    # {service: [(settle, fallback, group values by name)]}: every title group
    # the batch writes, once each, as its last answer left it.
    titles: dict[str, list[tuple[int, str, dict]]]
    # Entries an answer names under another title than they are filed under:
    # (service, media, own id, the key they are under, settle, fallback).
    moves: list[tuple[str, str, str, str, int, str]]
    # [service, media, spelling, own id, settle, fallback] for each group created.
    created: list[tuple[str, str, str, str, int, str]]
    # (settle, fallback, media, service, address) for each picture stated.
    sightings: list[tuple[int, str, str, str, str]]
    # {service: [(settle, fallback, season, values)]}
    seasons: dict[str, list[tuple[int, str, int, dict]]]
    # {service: [(settle, fallback, season, number, values)]}
    episodes: dict[str, list[tuple[int, str, int, int, dict]]]
    listed: dict[str, list[tuple[int, str, int, dict]]]
    filed: list[_Filed | None | Exception]
    now: int


def plan(conn: db.Connection, answers: Iterable[Answer], *, tz: ZoneInfo,
         now: int) -> Planned:
    """Decide what filing `answers` writes — reads only, for db.transaction's
    `prepare`, so the write lock is held for `apply`'s statements and not for
    the reading and deciding.

    EACH ANSWER EXACTLY AS IF THE ONES BEFORE IT HAD BEEN WRITTEN, each in its own
    transaction: its group is found (`service_rows`' rule) among the groups as
    the batch has left them, its title settled after every settle before it
    (identity.Planner), and the group's clock kept from the group as the answer
    finds it (rows.kept_clock) — so a show's title and one of its seasons,
    answered in one pass, are the same two writes they were as two transactions.

    THE ANSWERS CARRYING A SEASON LIST COME LAST. A season's group is written by
    two statements — a description of the season and a list naming it — and
    `apply` runs every description before every list. Taken in that order, that
    is the order one transaction per answer would have written them in: the order
    the answers arrive in is the service's, and any order is one they could
    have arrived in.

    AN ANSWER THIS CANNOT READ is that answer's failure (`write_many`), found
    before it touches anything another answer reads.
    """
    answers = [item._replace(source=str(item.source), media=str(item.media))
               for item in answers]
    filed: list[_Filed | None | Exception] = [None] * len(answers)
    spelled: dict[int, _Spelled] = {}
    for at, item in enumerate(answers):
        if not answered(item.answer, item.source):
            continue
        try:
            spelled[at] = _spell(item, tz=tz, now=now)
        # Reading one answer is pure and touches nothing: what it raises is that
        # answer's, and the rest of the batch is filed without it.
        except Exception as exc:
            filed[at] = exc

    planner = identity.Planner(conn, now=now)
    held = _Rows(conn, planner)
    held.prefetch([(item.source, item.media, item.own_id, spelled[at].ids)
                   for at, item in enumerate(answers) if at in spelled]
                  + [(item.source, item.media, item.own_id, None)
                     for at, item in enumerate(answers) if at in spelled and item.seasons])
    # And what each settle will ask about the groups found, read together.
    planner.prefetch(identity.Settling(
        item.media, spelled[at].ids,
        filed_under=next((str(row["title_key"]) for row in held.found(
            item.source, item.media, item.own_id, spelled[at].ids)), None),
        fallback=identity.per_source_key(item.source, item.own_id))
        for at, item in enumerate(answers) if at in spelled)

    touched: dict[tuple[str, str, str], tuple[int, str, dict]] = {}
    moves: list[tuple[str, str, str, str, int, str]] = []
    created: list[tuple[str, str, str, str, int, str]] = []
    sightings: list[tuple[int, str, str, str, str]] = []
    seasons: dict[str, list] = {}
    episodes: dict[str, list] = {}
    listed: dict[str, list] = {}
    settled = 0
    for at in sorted(spelled, key=lambda at: bool(spelled[at].listed)):
        item, words = answers[at], spelled[at]
        found = held.found(item.source, item.media, item.own_id, words.ids)
        before = dict(found[0]) if found else None
        # The key the group holds as this answer finds it — before its own
        # settle, which may re-key it.
        previous_key = planner.moved_to(str(before["title_key"])) if before else None
        fallback = identity.per_source_key(item.source, item.own_id)
        key = planner.settle(identity.Settling(
            item.media, words.ids, filed_under=str(before["title_key"]) if before else None,
            fallback=fallback)) or fallback
        own = str(before["source_id"]) if before else rows.own_id(item.source, item.own_id,
                                                                  words.ids)
        # A SERVICE RE-POINTED ITS SHARED IDS, NOT ITS OWN: this group goes
        # where its own id now says, its seasons and episodes with it — the one
        # writer that moves an entry (`write` says why).
        moved = before is not None and previous_key != key
        if moved:
            moves.append((item.source, item.media, own, previous_key, settled, fallback))
        # ANOTHER OF THE SERVICE'S ENTRIES IS THIS TITLE'S GROUP: the answer is
        # about a second id the service holds for the title, and its words are
        # not kept (the module docstring says why). Its settle stands — the ids
        # it taught are the title's — and an entry it moved there arrives as
        # that second id, its group dropped (merge.entry).
        occupant = held.occupant(item.source, key)
        if occupant is not None and occupant != own:
            if moved:
                held.forget(found[0])
            settled += 1
            continue
        if moved:
            before["title_key"] = key
        changes = {**words.values, "title_key": key, "enriched": 1,
                   "stale_after": now + TITLE_REFRESH_SECONDS, "failed_at": None,
                   "fail_count": 0}
        if words.title:
            changes.update(title=words.title, title_fold=rows.fold_title(words.title))
        if words.poster:
            changes["poster"] = words.poster
        if words.cast is not None:
            changes["cast_json"] = words.cast
        # What this answer rewrote, for the clock (rows.kept_clock): a name or a
        # picture it did not state, and the title's cast from an answer about a
        # season, are older words left in place.
        rewritten = set(changes)
        if before is None:
            row = held.create({"source": item.source, "media": item.media, "source_id": own,
                               "title": "", "title_fold": "", "poster": "", "cast_json": "[]",
                               **changes, "fetched_at": now})
            created.append((item.source, item.media, str(item.own_id), own, settled, fallback))
        else:
            row = found[0]
            row.update(changes, fetched_at=rows.kept_clock(item.source, tables.TITLES, row,
                                                           rewritten, now))
            held.changed(row)
        touched[(item.source, item.media, own)] = (settled, fallback, row)
        # THE PICTURE IS A SIGHTING TOO, filed in the poster registry as the store
        # writes it — under the title's tmdb id, else its key — so a service's new
        # address reaches every page that draws a picture the moment it is
        # learned, and a refresh that brings the same dead address leaves it
        # capped (app/media/artwork.py's `file_sighting`).
        if words.poster:
            sightings.append((settled, fallback, item.media, item.source, words.poster))
        if words.season is not None:
            number, season_values, episode_tails = words.season
            seasons.setdefault(item.source, []).append(
                (settled, fallback, number, season_values))
            episodes.setdefault(item.source, []).extend(
                (settled, fallback, number, episode, values) for episode, values in episode_tails)
        if words.listed:
            listed.setdefault(item.source, []).extend(
                (settled, fallback, season, values) for season, values in words.listed)
        filed[at] = _Filed(
            settled, fallback,
            previous_key=previous_key,
            poster=words.poster or (str(before["poster"] or "") if before else ""),
            previous_poster=str(before["poster"] or "") if before else "",
            ids=words.ids, previous_ids=_loads(before["ids_json"]) if before else {})
        settled += 1

    by_source: dict[str, list[tuple[int, str, dict]]] = {}
    for (source, _media, _own), written in touched.items():
        by_source.setdefault(source, []).append(written)
    return Planned(
        settles=planner.plan(), titles=by_source, moves=moves, created=created,
        sightings=sightings, seasons=seasons, episodes=episodes, listed=listed, filed=filed,
        now=now)


def apply(conn: db.Connection, planned: Planned) -> list[Written | None | Exception]:
    """Write what `plan` decided, inside the caller's transaction — on the
    connection it was planned on, which db.transaction's `prepare` guarantees
    holds what it did then. A FIXED NUMBER OF STATEMENTS however many answers:
    the identities (identity.SettlePlan, and a re-key's own repointing where one
    re-keys a title — and a group moving between titles, which is as rare), then
    for each service the title groups, the seasons, their episodes and the season
    lists, one statement each, and the ids and pictures. A write transaction
    holds the database's one write lock for as long as its statements take, and
    while the event loop is busy each can wait a GIL switch interval.

    EVERY GROUP IS FILED UNDER THE KEY ITS TITLE ANSWERS TO ONCE THE BATCH IS
    DONE (identity.settle_many says why), and its picture with it — the order a
    calendar fill writes in (app/metadata/__init__.py's `write_records`)."""
    planned.settles.apply(conn)
    keys = [found.key if found is not None else None for found in planned.settles.identities]

    def final(settled: int, fallback: str) -> str:
        return keys[settled] or fallback

    rows.move_entries(conn, [rows.Move(source, media, own, identity.follow(conn, old),
                                       final(settled, fallback))
                             for source, media, own, old, settled, fallback in planned.moves],
                      now=planned.now)
    for source, written in planned.titles.items():
        names = [name for name in _WRITTEN if groups.has(tables.TITLES, source, name)]
        conn.execute(_described_sql(source, names), {
            "rows": json.dumps([[row["media"], row["source_id"],
                                 *(row[name] for name in names)] for _s, _f, row in written]),
            "keys": json.dumps([final(settled, fallback) for settled, fallback, _r in written]),
            "now": planned.now})
    rows.file_ids(conn, [[source, media, spelling, own, final(settled, fallback)]
                     for source, media, spelling, own, settled, fallback in planned.created])
    artwork.file_sightings(conn, [
        (media, identity.tmdb_of(key), key, source, url)
        for settled, fallback, media, source, url in planned.sightings
        for key in (final(settled, fallback),)], planned.now)
    for source, written in planned.seasons.items():
        conn.execute(_season_upsert_sql(source, _SEASON_WRITES, _SEASON_REWRITES), {
            "rows": json.dumps([_season_row(source, _SEASON_WRITES, final(settled, fallback),
                                            season, values, planned.now)
                                for settled, fallback, season, values in written])})
    for source, written in planned.episodes.items():
        names = rows.episode_written(source, _EPISODE_WRITES)
        conn.execute(rows.episode_upsert_sql(source, _EPISODE_WRITES, _EPISODE_REWRITES), {
            "rows": json.dumps([[final(settled, fallback), f"S{season}E{number}",
                                 str(Media.SHOW), season, number,
                                 *(values[name] for name in names), planned.now]
                                for settled, fallback, season, number, values in written])})
    for source, written in planned.listed.items():
        conn.execute(_season_upsert_sql(source, _LISTED_SEASON_WRITES, _LISTED_SEASON_REWRITES), {
            "rows": json.dumps([_season_row(source, _LISTED_SEASON_WRITES,
                                            final(settled, fallback), season, values, planned.now)
                                for settled, fallback, season, values in written])})
    return [Written(final(found.settled, found.fallback), found.previous_key, found.poster,
                    found.previous_poster, found.ids, found.previous_ids)
            if isinstance(found, _Filed) else found
            for found in planned.filed]


def _described_sql(source: str, names: list[str]) -> str:
    """EVERY TITLE GROUP A BATCH DESCRIBES IN ONE STATEMENT, each once, as its
    last answer left it (`_Rows`): a group the store holds is rewritten from what
    was read of it and what each answer changed — what a statement per answer did
    one answer at a time — and a group it does not hold is added, the schema's
    empties filling what a description does not state. Each row: media, the
    entry's own id, then `names`; the key it is filed under beside it."""
    columns = ("id", *names)
    selected = ["json_extract(:keys, '$[' || r.key || ']')", "json_extract(r.value, '$[0]')",
                ":now", *(f"json_extract(r.value, '$[{at}]')" for at in range(1, 1 + len(columns)))]
    return (f"INSERT INTO {tables.TITLES} (title_key, media, created_at, "
            + ", ".join(f"{source}_{name}" for name in columns) + ")\n"
            f"SELECT {', '.join(selected)} FROM json_each(:rows) r WHERE true ORDER BY r.key\n"
            "ON CONFLICT(title_key) DO UPDATE SET "
            + ", ".join(f"{source}_{name} = excluded.{source}_{name}" for name in columns))


# A SEASON'S DESCRIPTION, every season of a batch in one statement per service,
# applied in the batch's order.
_SEASON_WRITES = {name: f"given.{name}" for name in (
    "name", "overview", "episode_count", "premiere", "air_dates_json", "cast_json",
    "stale_after")} | {"failed_at": "NULL", "fail_count": "0"}
_SEASON_REWRITES: dict[str, str | None] = dict.fromkeys(
    ("name", "overview", "episode_count", "premiere", "air_dates_json", "cast_json"))


# AN EPISODE'S DESCRIPTION, over whatever group a listing or the episode drain
# left: the answer wins where it states something, and a column it does not
# carry — the drain's vote count — is left as it was.
#
# EVERY EPISODE OF A BATCH IN ONE STATEMENT PER SERVICE (rows.json_rows), as a
# listing's are: a write transaction holds the database's one write lock for as
# long as its statements take, and while the event loop is busy each one can
# wait a GIL switch interval — so a statement per episode held it longer the
# longer the season.
_EPISODE_WRITES = {
    "title": "CASE WHEN given.title != '' THEN given.title ELSE held.title END",
    "overview": "given.overview", "first_aired": "given.first_aired",
    "episode_type": "CASE WHEN given.episode_type != '' THEN given.episode_type "
                    "ELSE held.episode_type END",
    "runtime": "COALESCE(given.runtime, held.runtime)",
    "rating": "given.rating", "stale_after": "given.stale_after",
}
_EPISODE_REWRITES: dict[str, str | None] = {
    "title": "given.title = ''", "overview": None, "first_aired": None,
    "episode_type": "given.episode_type = ''", "runtime": "given.runtime IS NULL",
    "rating": None}


def _season_tails(season: int, answer: Mapping[str, Any], *, tz: ZoneInfo,
                  now: int) -> tuple[int, dict, list[tuple[int, dict]]]:
    """The season an answer describes, as its group's values, and each of its
    episodes'."""
    episodes = sorted((e for e in answer.get("episodes") or [] if e.get("number") is not None),
                      key=lambda e: int(e["number"]))
    days = [local_day(e.get("first_aired"), tz) for e in episodes]
    dated = [e for e in episodes if local_day(e.get("first_aired"), tz) is not None]
    premiere = min(dated, key=lambda e: local_day(e.get("first_aired"), tz),
                   default=None)
    described = {
        "name": str(answer.get("season_title") or ""),
        "overview": str(answer.get("season_overview") or ""),
        "episode_count": len(episodes),
        "premiere": str(premiere.get("first_aired") or "") if premiere else "",
        "air_dates_json": json.dumps([day.isoformat() if day else None for day in days]),
        "cast_json": json.dumps(list(answer.get("cast") or [])),
        "stale_after": season_stale_after(days, now), "failed_at": None, "fail_count": 0}
    each = []
    for episode, day in zip(episodes, days):
        noon = (datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()
                if day else None)
        each.append((int(episode["number"]), {
            "title": str(episode.get("title") or ""),
            "overview": str(episode.get("overview") or ""),
            "first_aired": str(episode.get("first_aired") or ""),
            "episode_type": str(episode.get("episode_type") or ""),
            "runtime": episode.get("runtime"), "rating": episode.get("rating"),
            "stale_after": rows.episode_stale_after(noon, now)}))
    return season, described, each


# ---------------------------------------------------------------------------
# a refresh that could not be made
# ---------------------------------------------------------------------------

class Failures(NamedTuple):
    """What marking a batch of failed refreshes writes (`plan_failures`)."""
    settles: identity.SettlePlan
    # {service: [title key, ...]} and {service: [[title key, season], ...]}, each
    # named once per failure that marks it.
    titles: dict[str, list[str]]
    seasons: dict[str, list[list]]
    # {service: [[media, own id, ids, fetched_at, stale_after, failed_at,
    # fail_count]]} — the bare groups that carry a failure for a title with none
    # (`plan_failures`), the key each is filed under in `keyed`.
    bare: dict[str, list[list]]
    keyed: dict[str, list[tuple[int, str]]]
    # [service, media, own id, own id, settle] for each bare group's ids.
    created: list[tuple[str, str, str, str, int, str]]
    now: int


def plan_failures(conn: db.Connection,
                  failed: Iterable[tuple[str, str, Any, int | None]], *, now: int) -> Failures:
    """Decide how each (source, media, own id, season — None for the title)
    refresh that could not be made is recorded — reads only, for db.transaction's
    `prepare`.

    ON THE GROUPS IT WAS FOR (`service_rows`): the title's own for the title,
    the season's for a season, so a view leaves them alone for a while
    (app/metadata/refresh.py's backoff), each failure counted.

    AND WHERE THE SERVICE HAS NO GROUP FOR THE TITLE, A BARE ONE CARRIES IT —
    filed under the title's key, described by nothing, as `_title_rows` makes
    one — when the service's own id names the title on its own: TMDB's, a shared
    id the waterfall keys by. Without it a title the service does not know was
    asked about again every time anything queued it, since a failure recorded
    nowhere is no backoff at all; with it, it is a group owed a description and
    nothing else, and the backoff and give-up every owed group has apply
    (refresh.queue_listed). A service whose own id names no title (Trakt's slug,
    Simkl's number) would have its group filed under a key of its own, apart
    from the title, so it is left unmarked as before.
    """
    failed = [(str(source), str(media), str(own_id), season)
              for source, media, own_id, season in failed]
    planner = identity.Planner(conn, now=now)
    held = _Rows(conn, planner)
    held.prefetch([(source, media, own_id, None) for source, media, own_id, _s in failed])
    titles: dict[str, list[str]] = {}
    seasons: dict[str, list[list]] = {}
    bare: dict[tuple[str, str, str], list] = {}
    keyed: dict[str, list[tuple[int, str]]] = {}
    created: list[tuple[str, str, str, str, int, str]] = []
    settled = 0
    for source, media, own_id, season in failed:
        found = held.found(source, media, own_id, None)
        if season is not None:
            seasons.setdefault(source, []).extend(
                [str(row["title_key"]), int(season)] for row in found)
        elif found:
            titles.setdefault(source, []).extend(str(row["title_key"]) for row in found)
        elif (source, media, own_id) in bare:
            bare[(source, media, own_id)][-1] += 1
        elif planner.resolve(media, _with_own(source, own_id, None)) is not None:
            stated = {_namespace(source): id_rules.typed(_namespace(source), own_id)}
            fallback = identity.per_source_key(source, own_id)
            planner.settle(identity.Settling(media, stated, fallback=fallback))
            keyed.setdefault(source, []).append((settled, fallback))
            own = rows.own_id(source, own_id, stated)
            created.append((source, media, own_id, own, settled, fallback))
            settled += 1
            bare[(source, media, own_id)] = [media, own, json.dumps(stated, sort_keys=True),
                                             now, 0, now, 1]
    by_source: dict[str, list[list]] = {}
    for (source, _media, _own), values in bare.items():
        by_source.setdefault(source, []).append(values)
    return Failures(planner.plan(), titles, seasons, by_source, keyed, created, now)


def mark_failures(conn: db.Connection, planned: Failures) -> None:
    """Write what `plan_failures` decided, inside the caller's transaction — a
    statement per service and kind, each failure counted once per group it
    marks, as a transaction each counted them."""
    planned.settles.apply(conn)
    keys = [found.key if found is not None else None for found in planned.settles.identities]
    for source, marked in planned.titles.items():
        conn.execute(
            f"UPDATE {tables.TITLES} SET {source}_failed_at = :now, "
            f"{source}_fail_count = {source}_fail_count + f.times "
            "FROM (SELECT value AS title_key, COUNT(*) AS times FROM json_each(:rows) "
            "GROUP BY value) f "
            f"WHERE {tables.TITLES}.title_key = f.title_key AND {tables.TITLES}.{source}_id IS NOT NULL",
            {"now": planned.now, "rows": json.dumps(marked)})
    for source, marked in planned.seasons.items():
        conn.execute(
            f"UPDATE {tables.SEASONS} SET {source}_failed_at = :now, "
            f"{source}_fail_count = {source}_fail_count + f.times "
            "FROM (SELECT json_extract(value, '$[0]') AS title_key, "
            "json_extract(value, '$[1]') AS season, COUNT(*) AS times "
            "FROM json_each(:rows) GROUP BY 1, 2) f "
            f"WHERE {tables.SEASONS}.title_key = f.title_key "
            f"AND {tables.SEASONS}.season = f.season "
            f"AND {tables.SEASONS}.{source}_fetched_at IS NOT NULL",
            {"now": planned.now, "rows": json.dumps(marked)})
    for source, values in planned.bare.items():
        conn.execute(_bare_sql(source), {
            "rows": json.dumps(values), "now": planned.now,
            "keys": json.dumps([keys[at] or fallback for at, fallback in planned.keyed[source]])})
    rows.file_ids(conn, [[source, media, spelling, own, keys[at] or fallback]
                     for source, media, spelling, own, at, fallback in planned.created])


# Every season a list names in one statement per service, the episodes' reason.
_LISTED_SEASON_WRITES = {
    "name": "CASE WHEN given.name != '' THEN given.name ELSE held.name END",
    "episode_count": "given.episode_count",
    "premiere": "CASE WHEN given.premiere != '' THEN given.premiere ELSE held.premiere END",
    "poster": "CASE WHEN given.poster != '' THEN given.poster ELSE held.poster END",
}
_LISTED_SEASON_REWRITES: dict[str, str | None] = {
    "name": "given.name = ''", "episode_count": None,
    "premiere": "given.premiere = ''", "poster": "given.poster = ''"}


def write_season_list(conn: db.Connection, source: str, media: str, own_id: Any,
                      seasons: list[Mapping[str, Any]], *, now: int,
                      ids: Mapping[str, Any] | None = None) -> int:
    """File a service's season list for a title (base.SeasonsAnswer's `seasons`):
    each season's name, episode count, premiere and poster, on that service's
    groups of the title's seasons — a bare title group made where it has none
    (`_title_rows`). Returns how many seasons were filed.

    THE LIST'S COUNT REPLACES THE DESCRIPTION'S, and that is how a season that
    grew is noticed (`season_described`). Its clock (`stale_after`) is left
    alone: a list is not a description of the season."""
    tails = _listed_tails(seasons, now)
    written = [_season_row(source, _LISTED_SEASON_WRITES, row["title_key"], season, values, now)
               for row in _title_rows(conn, str(source), str(media), own_id, ids, now)
               for season, values in tails]
    if written:
        conn.execute(_season_upsert_sql(source, _LISTED_SEASON_WRITES, _LISTED_SEASON_REWRITES),
                     {"rows": json.dumps(written)})
    return len(written)


def _listed_tails(seasons: list[Mapping[str, Any]], now: int) -> list[tuple[int, dict]]:
    """Each season a list names: (season, its values)."""
    return [(int(entry["season"]), {
        "name": str(entry.get("name") or ""),
        "episode_count": int(entry.get("episode_count") or 0),
        "premiere": str(entry.get("first_aired") or ""),
        "poster": str(entry.get("poster") or "")})
        for entry in seasons if entry.get("season") is not None]


# What a season's group holds, read back under the column's own name.
_SEASON_NAMES = ("name", "overview", "episode_count", "premiere", "air_dates_json", "poster",
                 "cast_json", "videos_json", "fetched_at", "stale_after", "failed_at",
                 "fail_count")


def _season_sql(source: str, where: str) -> str:
    return (f"SELECT season, {groups.select(tables.SEASONS, source, _SEASON_NAMES)} "
            f"FROM {tables.SEASONS} WHERE title_key = ? "
            f"AND {groups.present(tables.SEASONS, source)} {where}")


# A season a list offers: numbered, and holding at least one episode — the rule
# every service's own picker applies (base.SeasonsAnswer).
_LISTED = "AND season > 0 AND episode_count > 0"


def _listed_rows(conn: db.Connection, row) -> list[dict]:
    """The seasons of a group's title its service lists, by season."""
    return [{"season": int(s["season"]), "episode_count": int(s["episode_count"]),
             "first_aired": str(s["premiere"] or ""), "name": str(s["name"] or ""),
             "poster": str(s["poster"] or "")}
            for s in conn.execute(
                f"SELECT * FROM ({_season_sql(row['source'], '')}) WHERE 1 {_LISTED} "
                "ORDER BY season", (row["title_key"],))]


def listed_seasons(conn: db.Connection, source: str, media: str, own_id: Any, *,
                   ids: Mapping[str, Any] | None = None) -> list[dict]:
    """A service's season list for a title as the store holds it, in
    base.SeasonsAnswer's `seasons` shape — or [] when the store cannot vouch for
    a whole list.

    WHAT VOUCHES FOR IT IS THE SERVICE'S DESCRIPTION OF THE TITLE. Season groups
    come from three writers — a season list, a tracker's summary of one season,
    a panel's description of one season — so the groups that happen to exist are
    not a list: a tracker that summarised season 3 alone would read as a show of
    one season. A described title states how many seasons it has
    (`season_count`), and the groups are this service's list when they number
    exactly that.
    """
    row = described_row(service_rows(conn, str(source), str(media), own_id, ids))
    if row is None:
        return []
    found = _listed_rows(conn, row)
    return found if len(found) == int(row["season_count"] or 0) else []


# ---------------------------------------------------------------------------
# reading an answer back
# ---------------------------------------------------------------------------

def _loads(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _loads_list(text: Any) -> list:
    try:
        value = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def season_row(conn: db.Connection, row, season: int):
    """The season's group of `row`'s service — None where it has none."""
    return conn.execute(_season_sql(row["source"], "AND season = ?"),
                        (row["title_key"], int(season))).fetchone()


def _only_described_season(conn: db.Connection, row) -> int | None:
    """The one season this group has a description of, where it has exactly one —
    what a service answers a title with when nobody named a season."""
    found = [r["season"] for r in conn.execute(_season_sql(row["source"], ""),
                                               (row["title_key"],)) if season_described(r)]
    return int(found[0]) if len(found) == 1 else None


def _episodes(conn: db.Connection, row, season: int) -> list[dict]:
    source = str(row["source"])
    names = ("number", "title", "first_aired", "rating", "overview", "episode_type", "runtime")
    wanted = [name for name in names if name != "number"]
    return [{"number": int(e["number"]), "title": str(e["title"] or ""),
             "first_aired": str(e["first_aired"] or ""), "rating": e["rating"],
             "overview": str(e["overview"] or ""), "episode_type": str(e["episode_type"] or ""),
             "runtime": e["runtime"]}
            for e in conn.execute(
                f"SELECT number, {groups.select(tables.EPISODES, source, wanted)} "
                f"FROM {tables.EPISODES} WHERE title_key = ? AND season = ? "
                f"AND number IS NOT NULL AND {source}_fetched_at > 0 ORDER BY number",
                (row["title_key"], season))]


def read(conn: db.Connection, source: str, media: str, own_id: Any,
         season: int | None, *, ids: Mapping[str, Any] | None = None) -> dict | None:
    """One service's stored answer about a title — and a season of it, where one
    is named — in base.DetailPort's key set; None when this service's group for
    it is not described, or the season named is not.

    A SEASON'S CAST IS THE SEASON'S WHERE THE SERVICE NAMED ONE and the title's
    otherwise, the rule every service's own answer follows."""
    row = described_row(service_rows(conn, str(source), str(media), own_id, ids))
    if row is None:
        return None
    if season is None and str(media) == str(Media.SHOW):
        season = _only_described_season(conn, row)
        of_season = None if season is None else season_row(conn, row, season)
    elif season is not None:
        of_season = season_row(conn, row, int(season))
        # A SEASON TAB DRAWS THE EPISODES, so a season whose shape is known
        # (a tracker's summary of it) but whose episodes are not is not yet
        # described for a panel.
        if (not season_described(of_season)
                or len(_episodes(conn, row, int(season))) < int(of_season["episode_count"])):
            return None
    else:
        of_season = None
    ratings = _loads(row["ratings_json"])
    title_cast = _loads_list(row["cast_json"])
    season_cast = _loads_list(of_season["cast_json"]) if of_season is not None else []
    slugs = [str(g) for g in _loads_list(row["genres_json"])]
    keywords, show_type = rows.facts_of(row)
    return {
        "keywords": keywords,
        "show_type": show_type,
        "title": str(row["title"] or ""),
        "year": rows.as_year(row["year"]),
        "overview": str(row["overview"] or ""),
        "season_overview": str(of_season["overview"] or "") if of_season is not None else "",
        "status": status_label(row["status"]),
        "network": str(row["network"] or ""),
        "country": str(row["country"] or ""),
        "language": str(row["language"] or ""),
        "anime_type": str(row["anime_type"] or ""),
        "release_types_by_country": _loads(row["release_types_json"]),
        "ids": _loads(row["ids_json"]),
        "poster": str(row["poster"] or ""),
        "runtime": row["runtime"],
        "genres": [genre_label(slug) for slug in slugs],
        "genre_slugs": slugs,
        "rating": ratings.get(str(source)),
        "imdb_rating": ratings.get("imdb"),
        "certification": str(row["certification"] or ""),
        "videos": [video for video in (_video_from(v) for v in _loads_list(row["videos_json"]))
                   if video is not None],
        "first_aired": str(row["first_aired"] or ""),
        "homepage": str(row["homepage"] or ""),
        "season": season,
        "season_title": str(of_season["name"] or "") if of_season is not None else "",
        "season_count": int(row["season_count"] or 0),
        # THE SEASON LIST, in base.SeasonsAnswer's shape: every season this
        # service's groups hold episodes of — the list its own answers stated,
        # kept with them (`write_many`) — and never a season the store cannot
        # vouch for.
        "seasons": _listed_rows(conn, row),
        "cast": season_cast or title_cast,
        "episodes": _episodes(conn, row, int(season)) if of_season is not None else [],
    }


def season_facts(conn: db.Connection, source: str, media: str, own_id: Any, season: int, *,
                 today: date, ids: Mapping[str, Any] | None = None) -> dict | None:
    """A described season reduced to app/providers/season.py's summary — the
    tracker's total, cadence and dates — by the same rule a lookup is reduced
    by, decided against `today` at read; None when the season is not described."""
    row = next(iter(service_rows(conn, str(source), str(media), own_id, ids)), None)
    if row is None:
        return None
    found = season_row(conn, row, int(season))
    if not season_described(found):
        return None
    days = [date.fromisoformat(day) if day else None
            for day in _loads_list(found["air_dates_json"])]
    return {"season": int(season), **season_rules.derive_season(days, today)}


# ---------------------------------------------------------------------------
# the async edges
# ---------------------------------------------------------------------------

async def store(source: str, media: str, own_id: Any, answer: Mapping[str, Any], *,
                asked_season: int | None, tz: ZoneInfo,
                seasons: list[Mapping[str, Any]] | None = None) -> Written | None:
    """`write`, and a season list beside it where one was read, in one
    transaction — a batch of one (`store_many`)."""
    (filed,) = await store_many([Answer(str(source), str(media), own_id, answer, asked_season,
                                        seasons)], tz=tz)
    if isinstance(filed, Exception):
        raise filed
    return filed


async def store_many(answers: Iterable[Answer], *, tz: ZoneInfo
                     ) -> list[Written | None | Exception]:
    """`write_many` in one transaction of its own, every read and decision made
    before it takes the write lock (`plan`, as db.transaction's `prepare`) — a
    refresh pass's answers in one turn of the writer queue, rather than a
    transaction per answer."""
    answers = list(answers)
    if not answers:
        return []
    now = db.now()
    return await db.transaction(apply, prepare=lambda conn: plan(conn, answers, tz=tz, now=now))


async def stored(source: str, media: str, own_id: Any, season: int | None, *,
                 ids: Mapping[str, Any] | None = None) -> dict | None:
    """`read`, on a worker thread."""
    return await db.run(lambda conn: read(conn, source, media, own_id, season, ids=ids))
