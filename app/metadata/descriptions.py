"""What a service said about one title, KEPT: its details answer written into the
store's rows, and read back as the same answer.

WHY THE ANSWER IS STORED RATHER THAN ASKED FOR EACH TIME. Every surface that
draws a title — a details panel, a tracker row, a board row, an export — used to
ask a service live (through a response cache with an hours-long life) or keep a
copy of its own, and the copies are how one title came to be described five
ways. The store is one row per (service, title), per (service, season) and per
(service, episode); a surface reads them, and a stale one is re-read behind the
reader (app/metadata/refresh.py).

THE ANSWER IS EACH SERVICE'S OWN, UNCHANGED. A service package reads its own
payloads (app/providers/<service>/detail.py's `fetch_details`, whose key set
base.DetailPort fixes and tests/providers/test_protocol_conformance.py holds);
this module only files that answer and gives it back. So what a service means by
a field — Simkl's anime seasons, Trakt's season names, TMDB's days — stays in the
service's package, and a stored answer and a fresh one are the same answer.

WHAT MARKS A ROW AS DESCRIBED, since a calendar listing writes the same rows and
knows less. A title row is described once `season_count` is set — a film's is 0
— which nothing but this module writes (a listing never knows it). A season row
is described when its `air_dates_json` holds one entry per episode its
`episode_count` names: a season list (`write_season_list`) states the count and
not the dates, so a season that has grown since it was described reads as not
described and is asked again, which is the right answer to a new episode.

ADDRESSED BY THE SERVICE'S OWN ID, which is not always a row's `source_id`: a
Trakt listing files its row under the title's slug, a Simkl or TMDB one under
the number. A service's row for a title is the row whose `ids_json` states that
service's own id for it (app/metadata/rows.py's ID_NAMESPACE).
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
from . import identity, rows
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
# finding a service's rows
# ---------------------------------------------------------------------------

def _namespace(source: str) -> str:
    return rows.ID_NAMESPACE[str(source)]


def _states_own_id(row, source: str, own_id: Any) -> bool:
    """Whether a row of `source` is the row for the title `source` knows as
    `own_id`: its id map states it, or (a row a description created) its
    `source_id` is it."""
    stated = _loads(row["ids_json"]).get(_namespace(source))
    return str(stated) == str(own_id) or str(row["source_id"]) == str(own_id)


def service_rows(conn: db.Connection, source: str, media: str, own_id: Any,
                 ids: Mapping[str, Any] | None = None) -> list[dict]:
    """Every row `source` holds for the title it knows as `own_id`, usually one —
    a slug-filed listing and a number-filed description of one title are two.

    BY THE TITLE'S KEY FIRST, which is indexed: `ids` (whatever the caller knows
    of the title) resolve to it, and the service's rows under it are read and
    checked for the service's own id. A title the ids do not resolve, or whose
    key holds no row of this service, is found by the id inside each of the
    service's own rows — a scan of one service's rows, kept for the paths that
    know a service's id and nothing else (a queued refresh).

    ONE TITLE IS A BATCH OF ONE (`services_rows`). Each row is a dict of every
    column, and `rid` its rowid.
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


# EVERY COLUMN OF A TITLE ROW, in the order a batch's reads spell it: rows.WORDS
# and rows.NOT_WORDS name each once (tests/metadata/test_retention.py holds them
# to the schema), so a row read here is the whole row.
_ROW_COLUMNS = (*rows.NOT_WORDS[rows.TITLES], *rows.WORDS[rows.TITLES])
_ROW = "json_array(t.rowid, " + ", ".join(f"t.{column}" for column in _ROW_COLUMNS) + ")"

# EACH READ ANSWERS IN ONE ROW, however many titles it names (identity.py's reads
# say why: the driver gives up the GIL around every row it steps through, and a
# read made inside a write transaction holds the lock for all of it).
#   Every service's rows under the keys named, through the index on title_key.
_UNDER_KEYS = (f"SELECT json_group_array({_ROW}) FROM {rows.TITLES} t "
               "WHERE t.title_key IN (SELECT value FROM json_each(?))")


def _stating_sql(sources: Iterable[str]) -> str:
    """Every row of each of `sources` that states one of the own ids named for
    it (`:<source>`, a JSON array of text) — by its source id, or the id its id
    map states for the service, which no index reaches: a scan of that service's
    rows, an arm per service. Service names come from the registry and their
    namespaces from rows.ID_NAMESPACE, never from input."""
    arms = [f"SELECT {_ROW} AS r FROM {rows.TITLES} t WHERE t.source = '{source}' "
            f"AND (t.source_id IN (SELECT value FROM json_each(:{source})) "
            f"OR CAST(json_extract(t.ids_json, '$.{_namespace(source)}') AS TEXT) "
            f"IN (SELECT value FROM json_each(:{source})))"
            for source in sources]
    return f"SELECT json_group_array(json(r)) FROM ({' UNION ALL '.join(arms)})"


class _Rows:
    """The title rows a batch of answers is about: what the store holds, read in
    a few statements, and each answer's writes applied here as the answer is
    planned (`plan`) — so every answer finds exactly the rows, and reads exactly
    the words, it would have found had each answer before it been written in a
    transaction of its own.

    WHAT IS READ UP FRONT (`prefetch`) is `service_rows`' two reads for every
    title at once: the rows under each title's key, and — for a title whose key
    holds none, or whose ids name no key — every row stating its own id. Read
    again only when an answer needs what was not read: a key no answer resolved
    to at first, or, once a re-key in the batch has moved rows between keys,
    every row stating each title's own id (`found` says why).
    """

    def __init__(self, conn: db.Connection, planner: identity.Planner):
        self._conn = conn
        self._planner = planner
        # (source, media, source id): the row as the batch has left it.
        self._rows: dict[tuple[str, str, str], dict] = {}
        # (source, media, own id): the rows stating it, and what each row states.
        self._stating: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
        self._states: dict[tuple[str, str, str], set[str]] = {}
        self._keys_read: set[str] = set()
        self._whole: set[tuple[str, str, str]] = set()
        self._titles: set[tuple[str, str, str]] = set()
        self._after_rekey = False

    # -- reading --------------------------------------------------------------

    def prefetch(self, wanted: Iterable[tuple[str, str, Any, Mapping[str, Any] | None]]) -> None:
        """Read what finding each (source, media, own id, ids) of `wanted` takes:
        their identities (identity.Planner.prefetch), the rows under the keys
        they resolve to, and every row stating the own id of each title those
        keys do not find — six statements however many there are."""
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
        (found,) = self._conn.execute(_UNDER_KEYS, (json.dumps(wanted),)).fetchone()
        for values in json.loads(found):
            self._hold(dict(zip(("rid", *_ROW_COLUMNS), values)))
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
            self._hold(dict(zip(("rid", *_ROW_COLUMNS), values)))
        self._whole.update(wanted)

    def _hold(self, row: dict) -> None:
        """A row read from the store — unless the batch already holds it, and so
        knows better what it says now."""
        unique = (str(row["source"]), str(row["media"]), str(row["source_id"]))
        if unique not in self._rows:
            self._rows[unique] = row
            self._index(unique)

    def _index(self, unique: tuple[str, str, str]) -> None:
        """Note which own ids a row states (`_states_own_id`'s rule), anew."""
        source, media, source_id = unique
        stated = _loads(self._rows[unique]["ids_json"]).get(_namespace(source))
        states = {source_id, *(() if stated is None else (str(stated),))}
        for own_id in self._states.get(unique, set()) - states:
            self._stating[(source, media, own_id)].discard(unique)
        for own_id in states:
            self._stating.setdefault((source, media, own_id), set()).add(unique)
        self._states[unique] = states

    def _under(self, source: str, media: str, own_id: str, key: str) -> list[dict]:
        """The rows stating `own_id` filed under `key` now."""
        return [self._rows[unique] for unique in self._stating.get((source, media, own_id), ())
                if self._planner.moved_to(self._rows[unique]["title_key"]) == key]

    def found(self, source: str, media: str, own_id: Any,
              ids: Mapping[str, Any] | None) -> list[dict]:
        """`service_rows`' answer as the batch stands: the rows of `source` under
        the key `ids` resolve to now that state `own_id`, in the order its query
        reads them (the key's index, by rowid); else every row stating it (the
        service's own index, by source id)."""
        source, media, own_id = str(source), str(media), str(own_id)
        if self._planner.rekeyed and not self._after_rekey:
            # A RE-KEY MOVES ROWS BETWEEN KEYS, so the rows read under a key are
            # no longer all the rows under it: a row filed under the key retired
            # is under the new one now. Every row stating each title's own id is
            # read, once, and the keys are then read off the rows themselves.
            self._after_rekey = True
            self._read_whole(self._titles)
        key = self._planner.resolve(media, _with_own(source, own_id, ids))
        if key is not None:
            self._read_keys([key])
            first = self._under(source, media, own_id, key)
            if first:
                return sorted(first, key=_rowid_order)
        self._read_whole([(source, media, own_id)])
        return sorted((self._rows[unique] for unique in
                       self._stating.get((source, media, own_id), ())),
                      key=lambda row: str(row["source_id"]))

    # -- writing --------------------------------------------------------------

    def changed(self, row: dict) -> None:
        """`row` was rewritten by the answer being planned."""
        self._index((str(row["source"]), str(row["media"]), str(row["source_id"])))

    def create(self, row: dict) -> dict:
        """A row the answer being planned adds — after every stored row in any
        order a later answer reads, as the rowid SQLite gives it would be."""
        unique = (str(row["source"]), str(row["media"]), str(row["source_id"]))
        self._rows[unique] = {**dict.fromkeys(_ROW_COLUMNS), **row, "rid": None}
        self._index(unique)
        return self._rows[unique]


def _with_own(source: str, own_id: Any, ids: Mapping[str, Any] | None) -> dict:
    """`ids` with the service's own id for the title — what `service_rows`
    resolves the title's key from."""
    return {**dict(ids or {}), _namespace(source): own_id}


def _rowid_order(row: dict) -> tuple[bool, int]:
    return row["rid"] is None, int(row["rid"] or 0)


def described_row(found: list):
    """The described row among a service's rows for one title, or None."""
    return next((row for row in found if row["season_count"] is not None), None)


def season_described(row) -> bool:
    """Whether a season row knows the season's SHAPE — one date (or none) per
    episode its count names — rather than only what a season list said of it.
    A tracker row's total and dates read nothing more; a panel's season tab also
    needs the episodes themselves (`read`)."""
    return row is not None and len(_loads_list(row["air_dates_json"])) == (
        row["episode_count"] if row["episode_count"] is not None else -1)


def _title_rows(conn: db.Connection, source: str, media: str, own_id: Any,
                ids: Mapping[str, Any] | None, now: int) -> list:
    """A service's rows for a title, with a bare one made where it has none yet —
    a season and a service's words for a title belong to that service's title
    row. The bare row is filed under the title's key (identity.settle) and is
    NOT described (`season_count` unset), so the next surface that draws the
    title has it described in full (app/metadata/refresh.py)."""
    found = service_rows(conn, source, media, own_id, ids)
    if found:
        return found
    stated = id_rules.canonical(ids or {})
    stated[_namespace(source)] = id_rules.typed(_namespace(source), own_id)
    settled = identity.settle(conn, media, stated,
                              fallback=identity.per_source_key(source, own_id), now=now)
    key = settled.key if settled is not None else identity.per_source_key(source, own_id)
    conn.execute(
        f"INSERT INTO {rows.TITLES} (source, media, source_id, title_key, ids_json, "
        "fetched_at, stale_after) VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(source, media, source_id) DO NOTHING",
        (str(source), str(media), str(own_id), key, json.dumps(stated, sort_keys=True), now))
    return service_rows(conn, source, media, own_id, ids)


_UPSERT_SEASON_SHAPE = f"""
INSERT INTO {rows.SEASONS}
    (source, media, source_id, season, episode_count, premiere, air_dates_json,
     fetched_at, stale_after)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, media, source_id, season) DO UPDATE SET
    episode_count = excluded.episode_count,
    premiere = CASE WHEN excluded.premiere != '' THEN excluded.premiere
                    ELSE {rows.SEASONS}.premiere END,
    air_dates_json = excluded.air_dates_json,
    stale_after = excluded.stale_after, failed_at = NULL, fail_count = 0,
"""
_SHAPE_REWRITES: dict[str, str | None] = {
    "episode_count": None, "premiere": "excluded.premiere = ''", "air_dates_json": None}


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
    season row, inside the caller's transaction. Its name, description and cast
    are left as they were: a summary does not state them, and a panel's own
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
    total = len(shape)
    for row in _title_rows(conn, source, media, own_id, ids, now):
        conn.execute(_UPSERT_SEASON_SHAPE + "    fetched_at = "
                     + rows.ceiling_clock(rows.SEASONS, _SHAPE_REWRITES), (
            str(source), str(media), str(row["source_id"]), int(summary["season"]), total,
            known[0] if known else "", json.dumps(shape), now, season_stale_after(days, now)))


# The words a feature's record may seed, and how to tell the store already has
# one: a title row stating it. Constants, never input.
_SEEDED_WORDS = {"title": "title != ''", "network": "network != ''",
                 "year": "year != ''", "runtime": "runtime IS NOT NULL"}


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
    the record's ids name, on a row left undescribed (`season_count` unset), so
    the first surface that draws the title has that service describe it
    (app/metadata/refresh.py) and its own words replace these. A WORD ANY SERVICE
    ALREADY STATES FOR THE TITLE IS NOT SEEDED, each word on its own: the store's
    words are always better than a record's copy of them.

    A RECORD OF ONE SEASON may carry that season's own name and episode total (a
    board row that is one season); they seed that season's row the same way —
    only where no service has a row for the season — and, holding no dates, the
    row reads as not described, so the season is described when it is drawn.
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
            "SELECT " + ", ".join(f"MAX({test})" for test in _SEEDED_WORDS.values())
            + f" FROM {rows.TITLES} WHERE title_key = ?", (key,)).fetchone()
        words = {name: (None if name == "runtime" else "") if stated else words[name]
                 for (name, stated) in zip(_SEEDED_WORDS, held)}
        if has_season and conn.execute(
                f"SELECT 1 FROM {rows.TITLES} t JOIN {rows.SEASONS} s ON s.source = t.source "
                "AND s.media = t.media AND s.source_id = t.source_id "
                "WHERE t.title_key = ? AND s.season = ? LIMIT 1", (key, int(season))).fetchone():
            has_season = False
    seeded = {name: value for name, value in words.items() if value not in (None, "")}
    if not seeded and not has_season:
        return None
    for row in _title_rows(conn, source, media, own, ids, now):
        if seeded:
            sets = [f"{name} = ?" for name in seeded]
            params = list(seeded.values())
            if "title" in seeded:
                sets.append("title_fold = ?")
                params.append(rows.fold_title(seeded["title"]))
            conn.execute(f"UPDATE {rows.TITLES} SET {', '.join(sets)} WHERE rowid = ?",
                         (*params, row["rid"]))
        if has_season:
            conn.execute(_upsert_listed_season_sql(), {"rows": json.dumps([[
                source, str(media), str(row["source_id"]), int(season), str(season_name or ""),
                int(episode_count or 0), "", "", now, 0]])})
    return source


def write_title_facts(conn: db.Connection, source: str, media: str, own_id: Any, *,
                      ids: Mapping[str, Any] | None, title: str = "", network: str = "",
                      now: int) -> None:
    """File what a service called a title and who it says carried it
    (base.DetailPort.fetch_title_facts) onto that service's row of it, inside
    the caller's transaction. A word the service did not state is left as the
    row has it. Its clock is left alone: two words re-read are not the whole
    description re-read."""
    title, network = str(title or ""), str(network or "")
    if not (title or network):
        return
    for row in _title_rows(conn, source, media, own_id, ids, now):
        sets, params = [], []
        if title:
            sets += ["title = ?", "title_fold = ?"]
            params += [title, rows.fold_title(title)]
        if network:
            sets.append("network = ?")
            params.append(network)
        conn.execute(f"UPDATE {rows.TITLES} SET {', '.join(sets)} WHERE rowid = ?",
                     (*params, row["rid"]))


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


# The words a description states for every title row it writes, in one spelling
# (`_title_values`), and with them what filing an answer sets on the row — the
# columns `_UPSERT_DESCRIBED` writes, `title_key` beside them.
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
    followed: the title's key before and after (a re-key, or this service's row
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
    the title's row, and — for an answer about a season — that season's row and
    its episodes' rows. Returns what changed (`Written`), or None for an answer
    that says nothing (`answered`), which writes nothing.

    THE IDS GO THROUGH THE ONE RESOLUTION (identity.settle), so a service that
    now names a title differently — a re-pointed TMDB id, a newly learned one —
    re-keys it or moves this service's row to the title it names, exactly as a
    listing does. The ids written are what the service said this time.

    A NAME OR A PICTURE THE ANSWER DOES NOT STATE IS NOT ERASED: a listing may
    have given the row one the per-title answer leaves out.

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
    """What filing one answer writes that the store's rows do not decide."""
    ids: dict
    values: dict
    title: str
    poster: str
    # The title's own cast, None for an answer about a season, which states that
    # season's.
    cast: str | None
    # The season the answer is about: its row's columns after the title's, and
    # each of its episodes'.
    season: tuple[list, list[list]] | None
    # Each season the list beside it names, its columns after the title's.
    listed: list[list]


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
    """A batch of answers as `apply` writes it, decided by `plan`."""
    settles: identity.SettlePlan
    # Every title row the batch writes, once each, as its last answer left it —
    # spelled for `_UPSERT_DESCRIBED` without the key it is filed under, which
    # `keyed` names: (the settle that decides it, the key if that names none).
    titles: str
    keyed: list[tuple[int, str]]
    # (settle, its fallback key, media, service, address) for each picture stated.
    sightings: list[tuple[int, str, str, str, str]]
    seasons: str
    episodes: str
    listed: str
    filed: list[_Filed | None | Exception]
    now: int


def plan(conn: db.Connection, answers: Iterable[Answer], *, tz: ZoneInfo,
         now: int) -> Planned:
    """Decide what filing `answers` writes — reads only, for db.transaction's
    `prepare`, so the write lock is held for `apply`'s statements and not for
    the reading and deciding.

    EACH ANSWER EXACTLY AS IF THE ONES BEFORE IT HAD BEEN WRITTEN, each in its own
    transaction: its rows are found (`service_rows`' rule) among the rows as the
    batch has left them, its title settled after every settle before it
    (identity.Planner), and each row's clock kept from the row as the answer
    finds it (rows.kept_clock) — so a show's title row and one of its seasons,
    answered in one pass, are the same two writes they were as two transactions.

    THE ANSWERS CARRYING A SEASON LIST COME LAST. A season's row is written by two
    statements — a description of the season (`_UPSERT_SEASON`) and a list naming
    it (`_UPSERT_LISTED_SEASON`) — and `apply` runs every description before
    every list. Taken in that order, that is the order one transaction per answer
    would have written them in: the order the answers arrive in is the service's,
    and any order is one they could have arrived in.

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
    # And what each settle will ask about the rows found, read together.
    planner.prefetch(identity.Settling(
        item.media, spelled[at].ids,
        filed_under=next((str(row["title_key"]) for row in held.found(
            item.source, item.media, item.own_id, spelled[at].ids)), None),
        fallback=identity.per_source_key(item.source, item.own_id))
        for at, item in enumerate(answers) if at in spelled)

    touched: dict[tuple[str, str, str], dict] = {}
    keyed: dict[tuple[str, str, str], tuple[int, str]] = {}
    sightings: list[tuple[int, str, str, str, str]] = []
    seasons: list[list] = []
    episodes: list[list] = []
    listed: list[list] = []
    settled = 0
    for at in sorted(spelled, key=lambda at: bool(spelled[at].listed)):
        item, words = answers[at], spelled[at]
        found = held.found(item.source, item.media, item.own_id, words.ids)
        before = dict(found[0]) if found else None
        # The key the row holds as this answer finds it — before its own settle,
        # which may re-key it.
        previous_key = planner.moved_to(str(before["title_key"])) if before else None
        fallback = identity.per_source_key(item.source, item.own_id)
        key = planner.settle(identity.Settling(
            item.media, words.ids, filed_under=str(before["title_key"]) if before else None,
            fallback=fallback)) or fallback
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
        if not found:
            found = [held.create({"source": item.source, "media": item.media,
                                  "source_id": str(item.own_id), "title": "", "title_fold": "",
                                  "poster": "", "cast_json": "[]", **changes,
                                  "fetched_at": now})]
        else:
            for row in found:
                row.update(changes,
                           fetched_at=rows.kept_clock(item.source, rows.TITLES, row, rewritten,
                                                      now))
                held.changed(row)
        for row in found:
            unique = (str(row["source"]), str(row["media"]), str(row["source_id"]))
            touched[unique] = row
            keyed[unique] = (settled, fallback)
        # THE PICTURE IS A SIGHTING TOO, filed in the poster registry as the store
        # writes it — under the title's tmdb id, else its key — so a service's new
        # address reaches every page that draws a picture the moment it is
        # learned, and a refresh that brings the same dead address leaves it
        # capped (app/media/artwork.py's `file_sighting`).
        if words.poster:
            sightings.append((settled, fallback, item.media, item.source, words.poster))
        if words.season is not None:
            season_tail, episode_tails = words.season
            for row in found:
                own = [item.source, item.media, str(row["source_id"])]
                seasons.append([*own, *season_tail])
                episodes.extend([*own, *tail] for tail in episode_tails)
        if words.listed:
            listed.extend([item.source, item.media, str(row["source_id"]), *tail]
                          for row in held.found(item.source, item.media, item.own_id, None)
                          for tail in words.listed)
        filed[at] = _Filed(
            settled, fallback,
            previous_key=previous_key,
            poster=words.poster or (str(before["poster"] or "") if before else ""),
            previous_poster=str(before["poster"] or "") if before else "",
            ids=words.ids, previous_ids=_loads(before["ids_json"]) if before else {})
        settled += 1

    return Planned(
        settles=planner.plan(),
        titles=json.dumps([[row["source"], row["media"], row["source_id"],
                            *(row[column] for column in _WRITTEN)] for row in touched.values()]),
        keyed=list(keyed.values()), sightings=sightings, seasons=json.dumps(seasons),
        episodes=json.dumps(episodes), listed=json.dumps(listed), filed=filed, now=now)


def apply(conn: db.Connection, planned: Planned) -> list[Written | None | Exception]:
    """Write what `plan` decided, inside the caller's transaction — on the
    connection it was planned on, which db.transaction's `prepare` guarantees
    holds what it did then. A FIXED NUMBER OF STATEMENTS however many answers:
    the identities (identity.SettlePlan, and a re-key's own repointing where one
    re-keys a title), the title rows, the pictures, the seasons, their episodes
    and the season lists, one statement each. A write transaction holds the
    database's one write lock for as long as its statements take, and while the
    event loop is busy each can wait a GIL switch interval.

    EVERY ROW IS FILED UNDER THE KEY ITS TITLE ANSWERS TO ONCE THE BATCH IS DONE
    (identity.settle_many says why), and its picture with it — the order a
    calendar fill writes in (app/metadata/__init__.py's `write_records`)."""
    planned.settles.apply(conn)
    keys = [found.key if found is not None else None for found in planned.settles.identities]

    def final(settled: int, fallback: str) -> str:
        return keys[settled] or fallback

    if planned.keyed:
        conn.execute(_UPSERT_DESCRIBED, {
            "rows": planned.titles,
            "keys": json.dumps([final(settled, fallback) for settled, fallback in planned.keyed])})
    artwork.file_sightings(conn, [
        (media, identity.tmdb_of(key), key, source, url)
        for settled, fallback, media, source, url in planned.sightings
        for key in (final(settled, fallback),)], planned.now)
    for sql, written in ((_upsert_season_sql(), planned.seasons),
                         (_upsert_episode_sql(), planned.episodes),
                         (_upsert_listed_season_sql(), planned.listed)):
        if written != "[]":
            conn.execute(sql, {"rows": written})
    return [Written(final(found.settled, found.fallback), found.previous_key, found.poster,
                    found.previous_poster, found.ids, found.previous_ids)
            if isinstance(found, _Filed) else found
            for found in planned.filed]


# EVERY TITLE ROW A BATCH DESCRIBES IN ONE STATEMENT, each once, as its last
# answer left it (`_Rows`): a row the store holds is rewritten from what was read
# of it and what each answer changed — what a statement per answer did one answer
# at a time — and a row it does not hold is added, the schema's defaults filling
# what a description does not state. The key each is filed under travels beside
# the rows (rows.json_rows' `keyed_at`), known only once the batch's identities
# are written.
_UPSERT_DESCRIBED = f"""
INSERT INTO {rows.TITLES} (source, media, source_id, title_key, {', '.join(_WRITTEN)})
{rows.json_rows(3 + len(_WRITTEN), keyed_at=3)}
ON CONFLICT(source, media, source_id) DO UPDATE SET
    title_key = excluded.title_key,
    {', '.join(f'{column} = excluded.{column}' for column in _WRITTEN)}
"""

# A SEASON'S DESCRIPTION, every season of a batch in one statement, applied in
# the batch's order (rows.json_rows). Each row: source, media, source_id, then
# `_season_tails`' season columns.
_UPSERT_SEASON = f"""
INSERT INTO {rows.SEASONS}
    (source, media, source_id, season, name, overview, episode_count, premiere,
     air_dates_json, cast_json, fetched_at, stale_after, failed_at, fail_count)
{rows.json_rows(14)}
ON CONFLICT(source, media, source_id, season) DO UPDATE SET
    name = excluded.name, overview = excluded.overview,
    episode_count = excluded.episode_count, premiere = excluded.premiere,
    air_dates_json = excluded.air_dates_json, cast_json = excluded.cast_json,
    stale_after = excluded.stale_after, failed_at = NULL, fail_count = 0,
"""
_SEASON_REWRITES: dict[str, str | None] = dict.fromkeys(
    ("name", "overview", "episode_count", "premiere", "air_dates_json", "cast_json"))


def _upsert_season_sql() -> str:
    return _UPSERT_SEASON + "    fetched_at = " + rows.ceiling_clock(rows.SEASONS,
                                                                     _SEASON_REWRITES)


# AN EPISODE'S DESCRIPTION, over whatever row a listing or the episode drain
# left: the answer wins where it states something, and a column it does not
# carry — the drain's vote count — is left as it was.
#
# EVERY EPISODE OF A BATCH IN ONE STATEMENT (rows.json_rows), as a listing's
# are: a write transaction holds the database's one write lock for as long as its
# statements take, and while the event loop is busy each one can wait a GIL switch
# interval — so a statement per episode held it longer the longer the season.
_UPSERT_EPISODE = f"""
INSERT INTO {rows.EPISODES}
    (source, media, source_id, season, number, title, overview, first_aired,
     episode_type, runtime, rating, fetched_at, stale_after)
{rows.json_rows(13)}
ON CONFLICT(source, media, source_id, season, number) DO UPDATE SET
    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE {rows.EPISODES}.title END,
    overview = excluded.overview, first_aired = excluded.first_aired,
    episode_type = CASE WHEN excluded.episode_type != '' THEN excluded.episode_type
                        ELSE {rows.EPISODES}.episode_type END,
    runtime = COALESCE(excluded.runtime, {rows.EPISODES}.runtime),
    rating = excluded.rating, stale_after = excluded.stale_after,
"""
_EPISODE_REWRITES: dict[str, str | None] = {
    "title": "excluded.title = ''", "overview": None, "first_aired": None,
    "episode_type": "excluded.episode_type = ''", "runtime": "excluded.runtime IS NULL",
    "rating": None}


def _upsert_episode_sql() -> str:
    return _UPSERT_EPISODE + "    fetched_at = " + rows.ceiling_clock(rows.EPISODES,
                                                                      _EPISODE_REWRITES)


def _season_tails(season: int, answer: Mapping[str, Any], *, tz: ZoneInfo,
                  now: int) -> tuple[list, list[list]]:
    """The season an answer describes, as `_UPSERT_SEASON` writes it after the
    title's own columns, and each of its episodes as `_UPSERT_EPISODE` does."""
    episodes = sorted((e for e in answer.get("episodes") or [] if e.get("number") is not None),
                      key=lambda e: int(e["number"]))
    days = [local_day(e.get("first_aired"), tz) for e in episodes]
    dated = [e for e in episodes if local_day(e.get("first_aired"), tz) is not None]
    premiere = min(dated, key=lambda e: local_day(e.get("first_aired"), tz),
                   default=None)
    described = [
        season, str(answer.get("season_title") or ""), str(answer.get("season_overview") or ""),
        len(episodes), str(premiere.get("first_aired") or "") if premiere else "",
        json.dumps([day.isoformat() if day else None for day in days]),
        json.dumps(list(answer.get("cast") or [])), now, season_stale_after(days, now), None, 0]
    each = []
    for episode, day in zip(episodes, days):
        noon = (datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp()
                if day else None)
        each.append([
            season, int(episode["number"]), str(episode.get("title") or ""),
            str(episode.get("overview") or ""), str(episode.get("first_aired") or ""),
            str(episode.get("episode_type") or ""), episode.get("runtime"), episode.get("rating"),
            now, rows.episode_stale_after(noon, now)])
    return described, each


# ---------------------------------------------------------------------------
# a refresh that could not be made
# ---------------------------------------------------------------------------

class Failures(NamedTuple):
    """What marking a batch of failed refreshes writes (`plan_failures`)."""
    settles: identity.SettlePlan
    # Title rowids, and season rows (source, media, source id, season), each named
    # once per failure that marks it.
    titles: str
    seasons: str
    # The bare rows that carry a failure for a title with none (`plan_failures`),
    # without the key each is filed under, which `keyed` names.
    bare: str
    keyed: list[tuple[int, str]]
    now: int


def plan_failures(conn: db.Connection,
                  failed: Iterable[tuple[str, str, Any, int | None]], *, now: int) -> Failures:
    """Decide how each (source, media, own id, season — None for the title)
    refresh that could not be made is recorded — reads only, for db.transaction's
    `prepare`.

    ON THE ROWS IT WAS FOR (`service_rows`): the title's own for the title, the
    season's for a season, so a view leaves them alone for a while
    (app/metadata/refresh.py's backoff), each failure counted.

    AND WHERE THE SERVICE HAS NO ROW FOR THE TITLE, A BARE ONE CARRIES IT — filed
    under the title's key, described by nothing, as `_title_rows` makes one —
    when the service's own id names the title on its own: TMDB's, a shared id the
    waterfall keys by. Without it a title the service does not know was asked
    about again every time anything queued it, since a failure recorded nowhere
    is no backoff at all; with it, it is a row owed a description and nothing
    else, and the backoff and give-up every owed row has apply
    (refresh.queue_listed). A service whose own id names no title (Trakt's slug,
    Simkl's number) would have its row filed under a key of its own, apart from
    the title, so it is left unmarked as before.
    """
    failed = [(str(source), str(media), str(own_id), season)
              for source, media, own_id, season in failed]
    planner = identity.Planner(conn, now=now)
    held = _Rows(conn, planner)
    held.prefetch([(source, media, own_id, None) for source, media, own_id, _s in failed])
    titles: list[int] = []
    seasons: list[list] = []
    bare: dict[tuple[str, str, str], list] = {}
    keyed: list[tuple[int, str]] = []
    for source, media, own_id, season in failed:
        found = held.found(source, media, own_id, None)
        if season is not None:
            seasons.extend([source, media, str(row["source_id"]), int(season)] for row in found)
        elif found:
            titles.extend(int(row["rid"]) for row in found)
        elif (source, media, own_id) in bare:
            bare[(source, media, own_id)][-1] += 1
        elif planner.resolve(media, _with_own(source, own_id, None)) is not None:
            stated = {_namespace(source): id_rules.typed(_namespace(source), own_id)}
            fallback = identity.per_source_key(source, own_id)
            planner.settle(identity.Settling(media, stated, fallback=fallback))
            keyed.append((len(keyed), fallback))
            bare[(source, media, own_id)] = [source, media, own_id,
                                             json.dumps(stated, sort_keys=True), now, 0, now, 1]
    return Failures(planner.plan(), json.dumps(titles), json.dumps(seasons),
                    json.dumps(list(bare.values())), keyed, now)


# A BATCH'S FAILURES IN THREE STATEMENTS, each failure counted once per row it
# marks, as a transaction each counted them.
_FAIL_TITLES = f"""
UPDATE {rows.TITLES} SET failed_at = :now, fail_count = fail_count + f.times
FROM (SELECT value AS rid, COUNT(*) AS times FROM json_each(:rows) GROUP BY value) f
WHERE {rows.TITLES}.rowid = f.rid
"""
_FAIL_SEASONS = f"""
UPDATE {rows.SEASONS} SET failed_at = :now, fail_count = fail_count + f.times
FROM (SELECT json_extract(value, '$[0]') AS source, json_extract(value, '$[1]') AS media,
             json_extract(value, '$[2]') AS source_id, json_extract(value, '$[3]') AS season,
             COUNT(*) AS times
      FROM json_each(:rows) GROUP BY 1, 2, 3, 4) f
WHERE {rows.SEASONS}.source = f.source AND {rows.SEASONS}.media = f.media
  AND {rows.SEASONS}.source_id = f.source_id AND {rows.SEASONS}.season = f.season
"""
# Each row: source, media, source_id, ids_json, fetched_at, stale_after, failed_at,
# fail_count — the key filed under travelling beside them (rows.json_rows).
_BARE_FAILED = f"""
INSERT INTO {rows.TITLES} (source, media, source_id, title_key, ids_json, fetched_at,
                           stale_after, failed_at, fail_count)
{rows.json_rows(8, keyed_at=3)}
ON CONFLICT(source, media, source_id) DO NOTHING
"""


def mark_failures(conn: db.Connection, planned: Failures) -> None:
    """Write what `plan_failures` decided, inside the caller's transaction."""
    planned.settles.apply(conn)
    if planned.titles != "[]":
        conn.execute(_FAIL_TITLES, {"now": planned.now, "rows": planned.titles})
    if planned.seasons != "[]":
        conn.execute(_FAIL_SEASONS, {"now": planned.now, "rows": planned.seasons})
    if planned.keyed:
        keys = [found.key if found is not None else None
                for found in planned.settles.identities]
        conn.execute(_BARE_FAILED, {
            "rows": planned.bare,
            "keys": json.dumps([keys[at] or fallback for at, fallback in planned.keyed])})


# Every season a list names in one statement, the episodes' reason. Each row:
# source, media, source_id, season, name, episode_count, premiere, poster,
# fetched_at, and a `stale_after` of 0 — a list is not a description of the season.
_UPSERT_LISTED_SEASON = f"""
INSERT INTO {rows.SEASONS}
    (source, media, source_id, season, name, episode_count, premiere, poster,
     fetched_at, stale_after)
{rows.json_rows(10)}
ON CONFLICT(source, media, source_id, season) DO UPDATE SET
    name = CASE WHEN excluded.name != '' THEN excluded.name ELSE {rows.SEASONS}.name END,
    episode_count = excluded.episode_count,
    premiere = CASE WHEN excluded.premiere != '' THEN excluded.premiere
                    ELSE {rows.SEASONS}.premiere END,
    poster = CASE WHEN excluded.poster != '' THEN excluded.poster ELSE {rows.SEASONS}.poster END,
"""
_LISTED_SEASON_REWRITES: dict[str, str | None] = {
    "name": "excluded.name = ''", "episode_count": None,
    "premiere": "excluded.premiere = ''", "poster": "excluded.poster = ''"}


def _upsert_listed_season_sql() -> str:
    return (_UPSERT_LISTED_SEASON + "    fetched_at = "
            + rows.ceiling_clock(rows.SEASONS, _LISTED_SEASON_REWRITES))


def write_season_list(conn: db.Connection, source: str, media: str, own_id: Any,
                      seasons: list[Mapping[str, Any]], *, now: int,
                      ids: Mapping[str, Any] | None = None) -> int:
    """File a service's season list for a title (base.SeasonsAnswer's `seasons`):
    each season's name, episode count, premiere and poster, on that service's
    row of the title — a bare one made where it has none (`_title_rows`).
    Returns how many seasons were filed.

    THE LIST'S COUNT REPLACES THE DESCRIPTION'S, and that is how a season that
    grew is noticed (`season_described`). Its clock (`stale_after`) is left
    alone: a list is not a description of the season."""
    tails = _listed_tails(seasons, now)
    written = [[str(source), str(media), str(row["source_id"]), *tail]
               for row in _title_rows(conn, str(source), str(media), own_id, ids, now)
               for tail in tails]
    if written:
        conn.execute(_upsert_listed_season_sql(), {"rows": json.dumps(written)})
    return len(written)


def _listed_tails(seasons: list[Mapping[str, Any]], now: int) -> list[list]:
    """Each season a list names, as `_UPSERT_LISTED_SEASON` writes it after the
    title's own columns."""
    return [[int(entry["season"]), str(entry.get("name") or ""),
             int(entry.get("episode_count") or 0), str(entry.get("first_aired") or ""),
             str(entry.get("poster") or ""), now, 0]
            for entry in seasons if entry.get("season") is not None]


# A season a list offers: numbered, and holding at least one episode — the rule
# every service's own picker applies (base.SeasonsAnswer).
_LISTED = "season > 0 AND episode_count > 0"


def listed_seasons(conn: db.Connection, source: str, media: str, own_id: Any, *,
                   ids: Mapping[str, Any] | None = None) -> list[dict]:
    """A service's season list for a title as the store holds it, in
    base.SeasonsAnswer's `seasons` shape — or [] when the store cannot vouch for
    a whole list.

    WHAT VOUCHES FOR IT IS THE SERVICE'S DESCRIPTION OF THE TITLE. Season rows
    come from three writers — a season list, a tracker's summary of one season,
    a panel's description of one season — so the rows that happen to exist are
    not a list: a tracker that summarised season 3 alone would read as a show of
    one season. A described title states how many seasons it has
    (`season_count`), and the rows are this service's list when they number
    exactly that.
    """
    row = described_row(service_rows(conn, str(source), str(media), own_id, ids))
    if row is None:
        return []
    found = [{"season": int(s["season"]), "episode_count": int(s["episode_count"]),
              "first_aired": str(s["premiere"] or ""), "name": str(s["name"] or ""),
              "poster": str(s["poster"] or "")}
             for s in conn.execute(
                 f"SELECT season, name, episode_count, premiere, poster FROM {rows.SEASONS} "
                 f"WHERE source = ? AND media = ? AND source_id = ? AND {_LISTED} "
                 "ORDER BY season", (row["source"], row["media"], row["source_id"]))]
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
    return conn.execute(
        f"SELECT * FROM {rows.SEASONS} WHERE source = ? AND media = ? AND source_id = ? "
        "AND season = ?", (row["source"], row["media"], row["source_id"], season)).fetchone()


def _only_described_season(conn: db.Connection, row) -> int | None:
    """The one season this row has a description of, where it has exactly one —
    what a service answers a title with when nobody named a season."""
    found = [r["season"] for r in conn.execute(
        f"SELECT * FROM {rows.SEASONS} WHERE source = ? AND media = ? AND source_id = ?",
        (row["source"], row["media"], row["source_id"])) if season_described(r)]
    return int(found[0]) if len(found) == 1 else None


def _episodes(conn: db.Connection, row, season: int) -> list[dict]:
    return [{"number": int(e["number"]), "title": str(e["title"] or ""),
             "first_aired": str(e["first_aired"] or ""), "rating": e["rating"],
             "overview": str(e["overview"] or ""), "episode_type": str(e["episode_type"] or ""),
             "runtime": e["runtime"]}
            for e in conn.execute(
                f"SELECT * FROM {rows.EPISODES} WHERE source = ? AND media = ? "
                "AND source_id = ? AND season = ? AND fetched_at > 0 ORDER BY number",
                (row["source"], row["media"], row["source_id"], season))]


def read(conn: db.Connection, source: str, media: str, own_id: Any,
         season: int | None, *, ids: Mapping[str, Any] | None = None) -> dict | None:
    """One service's stored answer about a title — and a season of it, where one
    is named — in base.DetailPort's key set; None when this service's row for it
    is not described, or the season named is not.

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
        # service's rows hold episodes of — the list its own answers stated, kept
        # with them (`write_many`) — and never a season the store cannot vouch for.
        "seasons": [{"season": int(r["season"]), "episode_count": int(r["episode_count"]),
                     "first_aired": str(r["premiere"] or ""), "name": str(r["name"] or ""),
                     "poster": str(r["poster"] or "")}
                    for r in conn.execute(
                        f"SELECT season, episode_count, premiere, name, poster FROM {rows.SEASONS} "
                        f"WHERE source = ? AND media = ? AND source_id = ? AND {_LISTED} "
                        "ORDER BY season", (row["source"], row["media"], row["source_id"]))],
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
