"""A title's identity: one row per title whoever describes it, the keys it used to
have, and the one rule that decides which title a set of ids names.

WHAT A KEY IS. Every feature files a title under the shared identity waterfall —
app/providers/base.py's `resolve_key`, the first of tmdb, tvdb, imdb, mal a title
is known in — stringified as "{media}:{id space}:{id}". A title no shared id
names (an anime only one service lists) is filed under that service's own name
for it, "{service}:{id}", which can never merge with anything: a visible
duplicate is safer than a wrong merge.

WHY A KEY CAN CHANGE. Ids arrive piecemeal. A listing that names a show only by
its tvdb id is filed under tvdb; when any service later states that show's tmdb
id — a lookup by shared id, another service's listing, an enrichment answer — the
waterfall names it differently. Measured on the development copy before this was
built: 1,256 stored titles already carried ids that keyed them differently from
the key they were filed under, every one a Simkl row whose enrichment had learned
them in place. Left alone, one title answers to two keys and every feature that
compares keys sees two titles.

SO A KEY MOVES ONCE, EVERYWHERE, IN ONE TRANSACTION. `settle` re-keys the title,
records the old key as an alias, and app/metadata/holders.py repoints every row
holding the old key in the same transaction — calendar airings, tracker rows,
board rows, marks, histories. A late arrival still naming the old key is
resolved through the alias.

A KEY ONLY EVER MOVES UP THE WATERFALL, and two titles keyed in the SAME id space
never merge. Measured on the same copy: fifteen pairs of different TMDB titles
share a tvdb, imdb or mal id between them. Those are two titles, whatever the
lower id says — so a lower id joins a title only when exactly one title ranked
above it holds that id, and a tmdb-keyed title is never absorbed into another.

THE SERVICE ROWS STAY WHAT EACH SERVICE SAID (app/metadata/rows.py); this row is
what is KNOWN — the union of every id any service or lookup has stated, first
writer winning a disagreement. Readers that key from a service's own ids complete
them from here first (`complete`), which is what lets a listing that names only a
tvdb id join the card its tmdb title draws.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from .. import db, logging_setup, perftrace
from ..providers.base import MATCH_SOURCES, ItemKey, parse_item_key, resolve_key
from . import holders, tables
from . import ids as id_rules

logger = logging.getLogger(__name__)

# The lower id spaces a title can be FOUND by, each with its own index (migration
# 47). tmdb has none: a title holding a tmdb id is keyed on it, so its key is that
# lookup.
_INDEXED = frozenset({"tvdb", "imdb", "mal"})

# Below every shared id space: a title only one service names.
_UNKEYED_RANK = len(MATCH_SOURCES)


class Identity(NamedTuple):
    """One title as the store knows it: its key, its media and every id known."""
    key: str
    media: str
    ids: dict

    @property
    def triple(self) -> ItemKey | None:
        """The key as the triple a tracker or board row is filed by, or None for a
        title only one service names — which neither feature can hold."""
        try:
            return parse_item_key(self.key)
        except ValueError:
            return None


def per_source_key(source: Any, source_id: Any) -> str:
    """The key of a title no shared id names: the service's own name for it.

    THE SAME SPELLING the calendar's grouping (cache.group_base) and a card's
    `Item.mark_key` fall back to, because all three name the same thing: the one
    listing such a title has. tests/metadata/test_identity.py holds the three to
    it."""
    return f"{source}:{source_id}"


def key_of(media: Any, ids: Mapping[str, Any]) -> str | None:
    """The waterfall's key for `ids`, flat, or None when no shared id names them."""
    found = resolve_key(str(media), ids)
    return None if found is None else str(found)


def rank(key: str) -> int:
    """Where a key's id space sits in the waterfall — 0 for tmdb — with a title
    only one service names below them all. A key only ever moves to a LOWER
    number."""
    try:
        return MATCH_SOURCES.index(parse_item_key(key).match_source)
    except ValueError:
        return _UNKEYED_RANK


def tmdb_of(key: str) -> int | None:
    """The tmdb id a key is filed by, or None for a key in another id space. A
    title with a tmdb id is always keyed by it — a key only moves up the
    waterfall, and tmdb is its top — so None means the title has none."""
    own = _own_id(key)
    return int(own[1]) if own is not None and own[0] == "tmdb" else None


def _own_id(key: str) -> tuple[str, Any] | None:
    """(id space, typed id) a key is filed by, or None for a per-service key."""
    try:
        parsed = parse_item_key(key)
    except ValueError:
        return None
    return parsed.match_source, id_rules.typed(parsed.match_source, parsed.match_id)


# ---------------------------------------------------------------------------
# what a settle reads and writes, inside a caller's transaction
# ---------------------------------------------------------------------------

class Settling(NamedTuple):
    """One title for `settle_many` — what `settle` takes, as a value."""
    media: str
    ids: Mapping[str, Any] | None
    filed_under: str | None = None
    fallback: str | None = None


class _Title(NamedTuple):
    """One `titles` row as a batch of settles holds it."""
    media: str
    ids: dict
    created_at: int


# EVERY READ BELOW ANSWERS IN ONE ROW, however many keys it names: the keys travel
# as a JSON array and the rows come back as one. The driver lets go of the GIL
# around every row it steps through and must win it back before the next — up to
# a switch interval (5 ms) each time while the event loop is busy — and a settle
# runs inside a write transaction, holding the database's one write lock for all
# of it. MEASURED on a copy of a real instance: a fill's store issued ~3,300
# statements and held the lock 5.3 s under one month read, and the next writer
# gave up at the busy timeout.
_ALIASES_SQL = (f"SELECT json_group_array(json_array(old_key, title_key)) FROM {tables.ALIASES} "
                "WHERE old_key IN (SELECT value FROM json_each(?))")
_ROWS_SQL = (f"SELECT json_group_array(json_array(title_key, media, ids_json, created_at)) "
             f"FROM {tables.TITLES} WHERE title_key IN (SELECT value FROM json_each(?))")
# One arm per indexed id space, each spelled in its index's own expression: the
# planner reaches an expression index only through the expression it was built
# on, so the space cannot be a parameter. The payload drives the join (CROSS
# JOIN fixes the order), a seek per [media, id] asked about.
_HOLDERS_SQL = ("SELECT json_group_array(json_array(media, name, id, title_key)) FROM ("
                + " UNION ALL ".join(
                    f"SELECT t.media AS media, '{name}' AS name, "
                    f"json_extract(t.ids_json, '$.{name}') AS id, t.title_key AS title_key "
                    f"FROM json_each(:{name}) w CROSS JOIN {tables.TITLES} t "
                    f"ON t.media = json_extract(w.value, '$[0]') "
                    f"AND json_extract(t.ids_json, '$.{name}') = json_extract(w.value, '$[1]')"
                    for name in sorted(_INDEXED))
                + ")")
# Every title a batch decided, in one statement. `WHERE true` is SQLite's own
# remedy for an upsert on a SELECT, whose ON would otherwise read as a join's.
_WRITE_SQL = (f"INSERT INTO {tables.TITLES} (title_key, media, ids_json, created_at) "
              "SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
              "json_extract(value, '$[2]'), json_extract(value, '$[3]') "
              "FROM json_each(?) WHERE true "
              "ON CONFLICT(title_key) DO UPDATE SET ids_json = excluded.ids_json, "
              "created_at = MIN(created_at, excluded.created_at)")


class _Known:
    """What the identity tables hold for the keys a batch of settles names — read
    up front in three statements (`prefetch`), kept current as each settle
    decides, and written afterwards in a fixed number (`SettlePlan.apply`).

    WHAT THE BATCH HAS DECIDED OUTRANKS WHAT IS STORED, because nothing it decides
    is written while it decides: a title one listing registers is found by the next
    listing's lower id (`holders_of`), and a key one re-key retired is followed by
    every settle after it — exactly what settling each title in its own turn read
    back from the tables.

    A KEY NOBODY ASKED ABOUT UP FRONT IS STILL ANSWERED, read when first wanted, at
    one statement each. The ids a stored title already holds can name a key no
    listing stated; that is rare. What keeps a fill's statement count fixed is
    that every key a listing states, everything those keys reach and every title
    holding one of their lower ids are read together.
    """

    def __init__(self, conn: db.Connection):
        self._conn = conn
        self._aliases: dict[str, str | None] = {}
        self._rows: dict[str, _Title | None] = {}
        self._holders: dict[tuple[str, str, Any], set[str]] = {}
        self._unwritten: dict[str, _Title] = {}
        # Keys a re-key in this batch retired, and where their holders went.
        self._moved: dict[str, str] = {}
        # The re-keys decided, in order, for `SettlePlan.apply` to write.
        self._retired: list[tuple[str, str]] = []

    # -- reading --------------------------------------------------------------

    def prefetch(self, titles: Iterable[Settling]) -> None:
        """Read, together, everything settling `titles` will ask: every key they
        state or are filed under, what each answers to, every title holding one
        of their lower ids, and the rows of all of those."""
        keys: set[str] = set()
        probes: set[tuple[str, str, Any]] = set()
        for title in titles:
            media = str(title.media)
            incoming = id_rules.canonical(title.ids)
            keys.update(key for key in (title.filed_under, title.fallback) if key)
            for name in MATCH_SOURCES:
                value = incoming.get(name)
                if value not in (None, "", 0):
                    keys.add(f"{media}:{name}:{value}")
                    probes.add((media, name, value))
        self._read_aliases(keys)
        reached = keys | {alias for key in keys if (alias := self._aliases.get(key))}
        probes.update(probe for key in reached if (probe := _probe(key)) is not None)
        self._read_holders(probes)
        held = {key for probe in probes for key in self._holders.get(probe, ())}
        self._read_rows(reached | held)

    def follow(self, key: str) -> str:
        """The key a title answers to now: `key` itself, or what it was re-keyed
        to. ONE HOP IS ALWAYS ENOUGH — a re-key repoints every alias that named
        the key it retires (`absorb`), so an alias never names another alias.

        A key with a title row is no alias — a re-key deletes the row of the key
        it retires — so a row already held answers without a read."""
        if key not in self._aliases and self._rows.get(key) is not None:
            return key
        self._read_aliases([key])
        return self._aliases[key] or key

    def registered(self, key: str) -> str | None:
        """What a title registered under `key` answers to now — `key` itself, or
        what it was re-keyed to — or None when no title was ever registered under
        it."""
        self._read_aliases([key])
        alias = self._aliases[key]
        if alias:
            return alias
        return key if self.row(key) is not None else None

    def row(self, key: str) -> _Title | None:
        self._read_rows([key])
        return self._rows[key]

    def holders_of(self, media: str, name: str, value: Any) -> set[str]:
        """Every title holding `name` = `value` among its ids — stored, or
        decided by this batch and not yet written."""
        if name not in _INDEXED:
            return set()
        probe = (media, name, value)
        self._read_holders([probe])
        found = set(self._holders[probe])
        found.update(key for key, title in self._unwritten.items()
                     if title.media == media and title.ids.get(name) == value)
        return found

    def moved_to(self, key: str | None) -> str | None:
        """`key`, or where this batch's re-keys moved its holders — what a row
        filed under it before the batch holds now."""
        while key in self._moved:
            key = self._moved[key]
        return key

    def _read_aliases(self, keys: Iterable[str]) -> None:
        wanted = sorted({key for key in keys if key and key not in self._aliases})
        if not wanted:
            return
        (found,) = self._conn.execute(_ALIASES_SQL, (json.dumps(wanted),)).fetchone()
        pairs = {str(old): str(new) for old, new in json.loads(found)}
        for key in wanted:
            # An alias naming a key this batch retired names where it went: the
            # re-key is written by `SettlePlan.apply`, after every read.
            self._aliases[key] = self.moved_to(pairs.get(key))

    def _read_rows(self, keys: Iterable[str]) -> None:
        wanted = sorted({key for key in keys if key and key not in self._rows})
        if not wanted:
            return
        (found,) = self._conn.execute(_ROWS_SQL, (json.dumps(wanted),)).fetchone()
        for key, media, ids_json, created_at in json.loads(found):
            self._rows[str(key)] = _Title(str(media), _ids_of(ids_json), int(created_at))
        for key in wanted:
            self._rows.setdefault(key, None)

    def _read_holders(self, probes: Iterable[tuple[str, str, Any]]) -> None:
        wanted = [probe for probe in set(probes)
                  if probe[1] in _INDEXED and probe not in self._holders]
        if not wanted:
            return
        (found,) = self._conn.execute(_HOLDERS_SQL, {
            name: json.dumps([[media, value] for media, space, value in wanted if space == name])
            for name in _INDEXED}).fetchone()
        for probe in wanted:
            self._holders[probe] = set()
        for media, name, value, key in json.loads(found):
            if str(key) not in self._moved:  # its row goes with the re-key that retired it
                self._holders.setdefault((media, name, value), set()).add(str(key))

    # -- writing --------------------------------------------------------------

    def write(self, key: str, media: str, ids: dict, created_at: int) -> None:
        """Decide a title's row — `_WRITE_SQL`'s upsert, applied here first and
        written by `SettlePlan.apply`."""
        held = self._rows.get(key)
        title = (_Title(held.media, dict(ids), min(held.created_at, created_at)) if held
                 else _Title(media, dict(ids), created_at))
        self._rows[key] = self._unwritten[key] = title

    def absorb(self, old: str, new: str) -> None:
        """Decide to retire `old` in favour of `new` (`_retire` writes it, when
        the plan is applied): every row holding it moves to `new`, every alias
        naming it names `new`, and `old` becomes one — all of which every later
        settle in the batch reads here first."""
        self._retired.append((old, new))
        for key, alias in self._aliases.items():
            if alias == old:
                self._aliases[key] = new
        self._aliases[new] = None
        self._aliases[old] = new
        self._rows[old] = None
        self._unwritten.pop(old, None)
        for held in self._holders.values():
            held.discard(old)
        for key, target in self._moved.items():
            if target == old:
                self._moved[key] = new
        self._moved[old] = new


def _retire(conn: db.Connection, old: str, new: str, *, now: int) -> None:
    """Write one re-key: every row holding `old` repointed to `new`, every alias
    naming it naming `new`, and `old` itself an alias. A re-key is rare, and the
    holders it moves live in every feature's tables."""
    counts = holders.repoint(conn, old, new, now=now)
    conn.execute(f"UPDATE {tables.ALIASES} SET title_key = ? WHERE title_key = ?", (new, old))
    conn.execute(f"DELETE FROM {tables.ALIASES} WHERE old_key = ?", (new,))
    conn.execute(
        f"INSERT INTO {tables.ALIASES} (old_key, title_key, aliased_at) VALUES (?, ?, ?) "
        "ON CONFLICT(old_key) DO UPDATE SET title_key = excluded.title_key, "
        "aliased_at = excluded.aliased_at", (old, new, now))
    conn.execute(f"DELETE FROM {tables.TITLES} WHERE title_key = ?", (old,))
    touched = {table: n for table, n in counts.items() if n}
    logger.info("title store: re-keyed %s -> %s (%s)", old, new,
                ", ".join(f"{table} {n}" for table, n in sorted(touched.items())) or "no rows")


class SettlePlan(NamedTuple):
    """What a batch of settles decided (`plan_settles`), not yet written: each
    title's identity, the re-keys in the order they were decided, and the rows
    to write. `apply` writes it, inside the caller's transaction — on the
    connection it was planned on, which `db.transaction(prepare=...)` guarantees
    is unchanged since, or plans again."""
    identities: list[Identity | None]
    retired: list[tuple[str, str]]
    written: dict[str, _Title]
    now: int

    @property
    def empty(self) -> bool:
        """Whether `apply` would write nothing: no re-key, no title's row."""
        return not (self.retired or self.written)

    def apply(self, conn: db.Connection, *, laps: perftrace.Laps | None = None) -> None:
        """Written: each re-key's repointing — which merges the two titles' rows
        (app/metadata/merge.py) — then every title's row. `laps`, when given, is
        told each half ("merges", then "settles")."""
        for old, new in self.retired:
            _retire(conn, old, new, now=self.now)
        if laps is not None:
            laps.lap("merges")
        if self.written:
            conn.execute(_WRITE_SQL, (json.dumps([
                [key, title.media, json.dumps(title.ids, sort_keys=True), title.created_at]
                for key, title in self.written.items()]),))
        if laps is not None:
            laps.lap("settles")


def _probe(key: str) -> tuple[str, str, Any] | None:
    """(media, id space, typed id) a key is filed by — what `holders_of` is asked
    about it — or None for a per-service key."""
    try:
        parsed = parse_item_key(key)
    except ValueError:
        return None
    return (parsed.media, parsed.match_source,
            id_rules.typed(parsed.match_source, parsed.match_id))


def _ids_of(text) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def follow(conn: db.Connection, key: str) -> str:
    """The key a title answers to now: `key` itself, or what it was re-keyed to."""
    return _Known(conn).follow(key)


def _sole_higher_holder(known: _Known, media: str, key: str) -> str | None:
    """The one title ranked ABOVE `key` that holds `key`'s own id, or None when
    no title does or more than one does.

    A {tvdb} listing and a {tmdb, tvdb} title of one show are the same title, and
    this is how the first finds the second without a lookup. More than one is two
    different titles claiming one lower id — measured fifteen times on the
    development copy — and choosing between them would be a guess."""
    own = _own_id(key)
    if own is None:
        return None
    above = {holder for holder in known.holders_of(media, *own) if rank(holder) < rank(key)}
    return next(iter(above)) if len(above) == 1 else None


def _claimed_elsewhere(known: _Known, media: str, name: str, value: Any,
                       *, besides: set[str], below: int) -> bool:
    """Whether a title other than those in `besides`, ranked above `below`,
    holds this id — which makes a merge by it ambiguous."""
    return any(rank(holder) < below
               for holder in known.holders_of(media, name, value) if holder not in besides)


# ---------------------------------------------------------------------------
# the one resolution
# ---------------------------------------------------------------------------

def settle(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None, *,
           filed_under: str | None = None, fallback: str | None = None,
           now: int) -> Identity | None:
    """The title `ids` names, registered, with every id handed in added to what is
    known and any title it turns out to be merged into it. Inside the caller's
    transaction, so a re-key and the write that caused it land together.

    `filed_under` is a key a row ALREADY HOLDS for this title — a service's
    stored row, a tracker or board row being restored. Its id is a stated fact
    about the title, so it is added to `ids` unless `ids` contradict the title it
    names; and if this resolution lands elsewhere, everything holding it is
    repointed. `fallback` is the per-service key a listing with no shared id is
    filed under (`per_source_key`).

    Returns None only for ids that name nothing and arrived with no key at all.

    ONE TITLE IS A BATCH OF ONE (`settle_many`), so there is one resolution
    whichever way a title arrives.
    """
    return settle_many(conn, [Settling(str(media), ids, filed_under, fallback)], now=now)[0]


def settle_many(conn: db.Connection, titles: Iterable[Settling], *,
                now: int) -> list[Identity | None]:
    """`settle` for each of `titles`, in order, inside the caller's transaction —
    each decided as if the ones before it had been settled and written in turn,
    in a number of statements that does not grow with them (`_Known`): three
    reads, one write, and a re-key's own repointing where a title is re-keyed.

    EVERY ANSWER IS THE KEY ITS TITLE ANSWERS TO ONCE THE BATCH IS DONE. A later
    title can re-key one an earlier title registered — a listing naming a show
    by its tvdb id, then another naming its tmdb id too — and an answer naming the
    retired key would file the caller's row under a key no identity holds.
    """
    plan = plan_settles(conn, titles, now=now)
    plan.apply(conn)
    return plan.identities


def plan_settles(conn: db.Connection, titles: Iterable[Settling], *, now: int) -> SettlePlan:
    """`settle_many`'s decisions, made and NOT written — reads only, so a caller
    can make them before taking the write lock (db.transaction's `prepare`) and
    write them inside it (`SettlePlan.apply`)."""
    titles = list(titles)
    planner = Planner(conn, now=now)
    planner.prefetch(titles)
    for title in titles:
        planner.settle(title)
    return planner.plan()


class Planner:
    """`plan_settles` one title at a time, with `resolve` answered between them as
    the tables would answer it had each title so far been settled and written.

    FOR A WRITER WHOSE NEXT TITLE DEPENDS ON THE LAST ONE'S SETTLE: which rows a
    service holds for a title is read by the key its ids resolve to, and an earlier
    title in the same batch can register a title or re-key one that changes that
    answer (app/metadata/descriptions.py's `plan`). Reads only, like
    `plan_settles`; `plan` is what `SettlePlan.apply` writes."""

    def __init__(self, conn: db.Connection, *, now: int):
        self._known = _Known(conn)
        self._now = now
        self._settled: list[tuple[Settling, str | None]] = []

    def prefetch(self, titles: Iterable[Settling]) -> None:
        """Read, together, what settling or resolving `titles` will ask — as many
        times as a caller learns of more; what is already known is not read
        again (`_Known.prefetch`)."""
        self._known.prefetch(list(titles))

    def resolve(self, media: Any, ids: Mapping[str, Any] | None) -> str | None:
        """`resolve`, against what the tables hold and every title this batch has
        settled so far."""
        return _resolve(self._known, media, ids)

    def moved_to(self, key: str | None) -> str | None:
        """Where the re-keys settled so far moved a key's holders: `key` itself
        unless one retired it."""
        return self._known.moved_to(key)

    @property
    def rekeyed(self) -> bool:
        """Whether any title settled so far re-keyed one."""
        return bool(self._known._retired)

    def settle(self, title: Settling) -> str | None:
        """Decide one title, after every one before it: the key it settles under
        as the tables would answer now, or None. The n-th title settled is the
        n-th of `plan`'s identities, which names the key it ends under."""
        target = _settle(self._known, title, now=self._now)
        self._settled.append((title, target))
        return target

    def named_apart(self, title: Settling) -> str | None:
        """The title `title`'s ids name, when it is NOT the title it is filed
        under nor one that title would be re-keyed into — ids naming another
        title of the same id space or a lower one — else None. Answered against
        every title settled so far, and writing nothing.

        A RE-KEY IS NOT APART: a title filed under its tvdb id whose ids now
        state a tmdb id moves up the waterfall to it, everything it holds with
        it. Two titles of one id space are two titles (the module docstring), so
        ids naming one while filed under the other say something about which
        title the statement is about."""
        filed = self._known.moved_to(title.filed_under)
        if not filed:
            return None
        current = self._known.follow(filed)
        # The ids as a settle reads them: the filing's own id adopted wherever
        # they do not contradict it (`_settle`'s first step).
        incoming = id_rules.canonical(title.ids)
        _adopt_filing_id(self._known, incoming, filed)
        landing = _resolve(self._known, title.media, incoming)
        if landing is None or landing == current or rank(landing) < rank(current):
            return None
        return landing

    def agreeing(self, title: Settling) -> dict:
        """`title`'s ids less every shared id the title it is filed under does
        not hold as stated: what a statement that named another title still says
        about the one it is filed under — its service's own ids, and the shared
        ids the two agree on. It teaches that title nothing."""
        filed = self._known.moved_to(title.filed_under)
        held = self._known.row(self._known.follow(filed)) if filed else None
        known = held.ids if held is not None else {}
        return {name: value for name, value in (title.ids or {}).items()
                if name not in MATCH_SOURCES
                or known.get(name) == id_rules.typed(name, value)}

    def plan(self) -> SettlePlan:
        """Everything settled, each identity the key its title answers to once
        the batch is done (`settle_many` says why)."""
        identities: list[Identity | None] = []
        for title, target in self._settled:
            if target is None:
                identities.append(None)
                continue
            key = self._known.follow(target)
            row = self._known.row(key)
            identities.append(Identity(key, str(title.media),
                                       dict(row.ids) if row is not None else {}))
        return SettlePlan(identities, list(self._known._retired), dict(self._known._unwritten),
                          self._now)


def _settle(known: _Known, title: Settling, *, now: int) -> str | None:
    """One title's resolution against what `known` holds, decided there — the
    key it settles under, or None. The steps, each with its reason:
      1. The waterfall over the ids picks a key, through any alias.
      2. A key in a lower id space joins the ONE title above it holding that id
         (`_sole_higher_holder`).
      3. Every title keyed by a LOWER id these ids state is the same title — the
         ids say so — and is merged in, unless a second title above it also
         claims that id.
      4. The merged ids may name a higher key still; if so, that is where the
         title goes.
    """
    media = str(title.media)
    incoming = id_rules.canonical(title.ids)
    # What a row filed under this key before the batch holds NOW: a re-key earlier
    # in the batch moved it, as it moved the row.
    filed_under = known.moved_to(title.filed_under)
    fallback = title.fallback
    _adopt_filing_id(known, incoming, filed_under)
    stated = key_of(media, incoming)
    followed = None
    if stated is not None:
        followed = known.follow(stated)
        target = _sole_higher_holder(known, media, followed) or followed
    elif filed_under:
        target = known.follow(filed_under)
    elif fallback:
        target = known.follow(fallback)
    else:
        return None

    moved: list[str] = []       # keys whose holders move to the target
    absorbed: list[str] = []    # titles whose rows merge into the target's
    if filed_under and filed_under != target:
        # A ROW HOLDS THIS KEY, so it moves whether or not the title was ever
        # registered under it: a row restored from a backup, or written before
        # its title learned a higher id.
        current = known.follow(filed_under)
        if current != target and rank(current) > rank(target):
            moved.append(filed_under)
            if known.row(current) is not None:
                absorbed.append(current)
        elif current == target:
            moved.append(filed_under)   # a row still naming a retired key
    if fallback and fallback not in (target, filed_under):
        # The per-service key moves only when a title was REGISTERED under it —
        # a listing that once named no shared id. Every other listing has one and
        # nothing holds it, and treating it as held would write an alias and walk
        # every holder for every title a fill names.
        current = known.registered(fallback)
        if current is not None and current != target and current not in absorbed:
            moved.append(current)
            absorbed.append(current)
    if followed is not None and followed not in (target, *absorbed) \
            and known.row(followed) is not None:
        absorbed.append(followed)
    for name in MATCH_SOURCES:
        value = incoming.get(name)
        if value in (None, "", 0) or f"{media}:{name}:{value}" in (target, stated):
            continue
        lower = known.registered(f"{media}:{name}:{value}")
        if lower is None or lower == target or lower in absorbed or rank(lower) <= rank(target):
            continue
        if _claimed_elsewhere(known, media, name, value, besides={target, lower},
                              below=rank(lower)):
            logger.info("title store: %s=%s is claimed by more than one title; "
                        "%s is left apart from %s", name, value, lower, target)
            continue
        absorbed.append(lower)

    row = known.row(target)
    merged = dict(row.ids) if row is not None else {}
    _merge_into(merged, incoming, target)
    for key in absorbed:
        found = known.row(key)
        if found is not None:
            _merge_into(merged, found.ids, target)

    # The merged ids may name a higher title than the one they were filed under.
    higher = key_of(media, merged)
    if higher is not None and higher != target:
        higher = known.follow(higher)
        if higher != target and rank(higher) < rank(target):
            above = known.row(higher)
            if above is not None:
                merged = {**merged, **above.ids}
            absorbed.append(target)
            moved.append(target)
            target = higher
            row = above

    retired = [key for key in dict.fromkeys((*absorbed, *moved)) if key != target]
    if row is not None and not retired and row.ids == merged:
        # NOTHING NEW: the commonest case by far — a refill naming a title it
        # named yesterday — and a write per listing would be a write per airing.
        return target
    created = min([found.created_at for found in (known.row(key) for key in (target, *absorbed))
                   if found is not None] or [now])
    known.write(target, media, merged, created)
    for key in retired:
        known.absorb(key, target)
    return target


def _adopt_filing_id(known: _Known, incoming: dict, filed_under: str | None) -> None:
    """Add the id a row is filed under to `incoming`, unless `incoming` states a
    shared id that title's known ids contradict — then the row is being told
    something new about itself, and the old filing is not evidence for it."""
    if not filed_under:
        return
    own = _own_id(filed_under)
    if own is None or str(incoming.get(own[0])) == str(own[1]):
        return  # nothing to adopt: the ids already state it
    row = known.row(known.follow(filed_under))
    held_ids = row.ids if row is not None else {own[0]: own[1]}
    for name in MATCH_SOURCES:
        stated, held = incoming.get(name), held_ids.get(name)
        if stated not in (None, "") and held not in (None, "") and str(stated) != str(held):
            return
    incoming.setdefault(own[0], own[1])


def _merge_into(merged: dict, other: Mapping[str, Any], key: str) -> None:
    """Add `other`'s ids to `merged`, the ids already there winning — the first
    writer is the answer every time, which is what keeps it stable. A shared id
    the two disagree about is logged: it is the one disagreement that can mean a
    service has the wrong title."""
    for name, value in other.items():
        if value in (None, ""):
            continue
        held = merged.get(name)
        if held in (None, ""):
            merged[name] = value
        elif name in MATCH_SOURCES and str(held) != str(value):
            logging_setup.once(logger, logging.INFO,
                               "title store: %s holds %s=%s; another statement said %s",
                               key, name, held, value)


# ---------------------------------------------------------------------------
# reading without writing
# ---------------------------------------------------------------------------

def resolve(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None) -> str | None:
    """The key `ids` would settle under, decided and NOT written: the waterfall's
    key through any alias, joined to the one title above it holding its id. None
    when the ids name no shared id at all."""
    return _resolve(_Known(conn), media, ids)


def _resolve(known: _Known, media: Any, ids: Mapping[str, Any] | None) -> str | None:
    stated = key_of(str(media), id_rules.canonical(ids))
    if stated is None:
        return None
    target = known.follow(stated)
    return _sole_higher_holder(known, str(media), target) or target


def complete(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None) -> dict:
    """`ids` with the SHARED ids of the title they name added beneath them — the
    service's own ids winning — so that keying the result gives the title's key.

    FOR EVERY READER THAT KEYS FROM ONE SERVICE'S IDS: a calendar listing, a
    history event, a search result about to be drawn. A service that knows a show
    only by its tvdb id still names the tmdb title once its ids are completed,
    and nothing downstream has to know aliases exist. Only the shared ids are
    added: a service's own id is that service's to state.
    """
    known = _Known(conn)
    key = _resolve(known, media, ids)
    row = None if key is None else known.row(key)
    return with_shared(ids, row.ids if row is not None else {})


def with_shared(own: Mapping[str, Any] | None, known: Mapping[str, Any]) -> dict:
    """`own` with the SHARED ids in `known` beneath it — `complete`'s rule, for a
    caller that already holds the title's known ids."""
    out = dict(own or {})
    for name in MATCH_SOURCES:
        if out.get(name) in (None, "") and known.get(name) not in (None, ""):
            out[name] = known[name]
    return out


async def every_id(media: Any, ids: Mapping[str, Any] | None, *, key: str = "") -> dict:
    """`ids` with EVERY id the store knows for the title they name beneath them —
    each service's own id as well as the shared ones — `ids` winning. `key`, a
    title key the caller holds for it (followed through any re-key), names the
    title where `ids` alone may not: a card's panel request carries each
    service's own id and no tvdb or imdb.

    NOT `complete`, WHICH ADDS ONLY THE SHARED IDS, and the difference is what
    each is for. `complete` keys a listing: a service's own id is that service's
    to state, so a reader keying one service's row must not be handed another's.
    This is for a reader that only ADDRESSES a service — an outbound button needs
    the service's own id to name its page — and a title whose Trakt id a lookup by
    shared id learned has a Trakt page whether or not any surface drew Trakt's
    description of it. MEASURED on the development copy: 83 of October's
    TMDB-only Season Premieres held a Simkl id here that no panel offered.
    """
    def _work(conn):
        reader = _Known(conn)
        found = reader.follow(str(key)) if key else _resolve(reader, media, ids)
        row = None if found is None else reader.row(found)
        return dict(row.ids) if row is not None else {}

    stored = await db.run(_work)
    return {**stored, **{name: value for name, value in (ids or {}).items()
                         if value not in (None, "")}}


async def completed(pairs: Iterable[tuple[Any, Mapping[str, Any] | None]]) -> list[dict]:
    """`complete` for many (media, ids) at once, in one read."""
    pairs = list(pairs)
    if not pairs:
        return []
    return await db.run(lambda conn: [complete(conn, media, ids) for media, ids in pairs])


async def canonical_keys(keys: Iterable[str]) -> dict[str, str]:
    """{key: the key it answers to now}, for keys a caller already holds."""
    wanted = [str(k) for k in dict.fromkeys(keys) if k]
    if not wanted:
        return {}
    def _work(conn):
        reader = _Known(conn)
        reader._read_aliases(wanted)  # one read for every key, not one each
        return {key: reader.follow(key) for key in wanted}

    return await db.run(_work)


async def known(title_key: str) -> Identity | None:
    """The title a key names now — followed through any re-key — or None when the
    store has no such title."""
    def _work(conn):
        reader = _Known(conn)
        key = reader.follow(str(title_key))
        row = reader.row(key)
        return None if row is None else Identity(key, row.media, dict(row.ids))

    return await db.run(_work)
