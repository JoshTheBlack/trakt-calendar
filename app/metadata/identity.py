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

from .. import db
from ..providers.base import MATCH_SOURCES, ItemKey, parse_item_key, resolve_key
from . import holders
from . import ids as id_rules

logger = logging.getLogger(__name__)

# The identity tables. Named once, for the statements below and for the calendar's
# read, which merges a title's known ids into each listing it draws.
TABLE = "titles"
ALIASES = "title_aliases"

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
# reading, inside a caller's transaction
# ---------------------------------------------------------------------------

def follow(conn: db.Connection, key: str) -> str:
    """The key a title answers to now: `key` itself, or what it was re-keyed to.

    ONE HOP IS ALWAYS ENOUGH. A re-key repoints every alias that named the key it
    retires (`_absorb`), so an alias never names another alias."""
    row = conn.execute(f"SELECT title_key FROM {ALIASES} WHERE old_key = ?", (key,)).fetchone()
    return str(row[0]) if row else key


def _registered(conn: db.Connection, key: str) -> str | None:
    """The key a title registered under `key` answers to now — `key` itself, or
    what it was re-keyed to — or None when no title was ever registered under it.
    One query for what `follow` and `_row` would ask in turn, because a fill asks
    it of every lower id every listing states."""
    row = conn.execute(
        f"SELECT title_key FROM {ALIASES} WHERE old_key = ? "
        f"UNION ALL SELECT title_key FROM {TABLE} WHERE title_key = ? LIMIT 1",
        (key, key)).fetchone()
    return None if row is None else str(row[0])


def _row(conn: db.Connection, key: str):
    return conn.execute(
        f"SELECT title_key, media, ids_json, created_at FROM {TABLE} WHERE title_key = ?",
        (key,)).fetchone()


def _ids(row) -> dict:
    try:
        value = json.loads(row["ids_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _holders_of(conn: db.Connection, media: str, name: str, value: Any) -> list[str]:
    """Every title holding `name` = `value` among its ids, through that space's
    index. The query's expression is the index's own, which is what lets the
    planner use it."""
    if name not in _INDEXED:
        return []
    rows = conn.execute(
        f"SELECT title_key FROM {TABLE} "
        f"WHERE media = ? AND json_extract(ids_json, '$.{name}') = ?", (media, value))
    return [str(row[0]) for row in rows]


def _sole_higher_holder(conn: db.Connection, media: str, key: str) -> str | None:
    """The one title ranked ABOVE `key` that holds `key`'s own id, or None when
    no title does or more than one does.

    A {tvdb} listing and a {tmdb, tvdb} title of one show are the same title, and
    this is how the first finds the second without a lookup. More than one is two
    different titles claiming one lower id — measured fifteen times on the
    development copy — and choosing between them would be a guess."""
    own = _own_id(key)
    if own is None:
        return None
    above = {holder for holder in _holders_of(conn, media, *own) if rank(holder) < rank(key)}
    return next(iter(above)) if len(above) == 1 else None


def _claimed_elsewhere(conn: db.Connection, media: str, name: str, value: Any,
                       *, besides: set[str], below: int) -> bool:
    """Whether a title other than those in `besides`, ranked above `below`,
    holds this id — which makes a merge by it ambiguous."""
    return any(rank(holder) < below
               for holder in _holders_of(conn, media, name, value) if holder not in besides)


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

    THE STEPS, each with its reason:
      1. The waterfall over the ids picks a key, through any alias.
      2. A key in a lower id space joins the ONE title above it holding that id
         (`_sole_higher_holder`).
      3. Every title keyed by a LOWER id these ids state is the same title — the
         ids say so — and is merged in, unless a second title above it also
         claims that id.
      4. The merged ids may name a higher key still; if so, that is where the
         title goes.
    """
    media = str(media)
    incoming = id_rules.canonical(ids)
    _adopt_filing_id(conn, incoming, filed_under)
    stated = key_of(media, incoming)
    followed = None
    if stated is not None:
        followed = follow(conn, stated)
        target = _sole_higher_holder(conn, media, followed) or followed
    elif filed_under:
        target = follow(conn, filed_under)
    elif fallback:
        target = follow(conn, fallback)
    else:
        return None

    moved: list[str] = []       # keys whose holders move to the target
    absorbed: list[str] = []    # titles whose rows merge into the target's
    if filed_under and filed_under != target:
        # A ROW HOLDS THIS KEY, so it moves whether or not the title was ever
        # registered under it: a row restored from a backup, or written before
        # its title learned a higher id.
        current = follow(conn, filed_under)
        if current != target and rank(current) > rank(target):
            moved.append(filed_under)
            if _row(conn, current) is not None:
                absorbed.append(current)
        elif current == target:
            moved.append(filed_under)   # a row still naming a retired key
    if fallback and fallback not in (target, filed_under):
        # The per-service key moves only when a title was REGISTERED under it —
        # a listing that once named no shared id. Every other listing has one and
        # nothing holds it, and treating it as held would write an alias and walk
        # every holder for every title a fill names.
        current = _registered(conn, fallback)
        if current is not None and current != target and current not in absorbed:
            moved.append(current)
            absorbed.append(current)
    if followed is not None and followed not in (target, *absorbed) \
            and _row(conn, followed) is not None:
        absorbed.append(followed)
    for name in MATCH_SOURCES:
        value = incoming.get(name)
        if value in (None, "", 0) or f"{media}:{name}:{value}" in (target, stated):
            continue
        lower = _registered(conn, f"{media}:{name}:{value}")
        if lower is None or lower == target or lower in absorbed or rank(lower) <= rank(target):
            continue
        if _claimed_elsewhere(conn, media, name, value, besides={target, lower},
                              below=rank(lower)):
            logger.info("title store: %s=%s is claimed by more than one title; "
                        "%s is left apart from %s", name, value, lower, target)
            continue
        absorbed.append(lower)

    row = _row(conn, target)
    merged = _ids(row) if row is not None else {}
    _merge_into(merged, incoming, target)
    for key in absorbed:
        found = _row(conn, key)
        if found is not None:
            _merge_into(merged, _ids(found), target)

    # The merged ids may name a higher title than the one they were filed under.
    higher = key_of(media, merged)
    if higher is not None and higher != target:
        higher = follow(conn, higher)
        if higher != target and rank(higher) < rank(target):
            above = _row(conn, higher)
            if above is not None:
                merged = {**merged, **_ids(above)}
            absorbed.append(target)
            moved.append(target)
            target = higher
            row = above

    retired = [key for key in dict.fromkeys((*absorbed, *moved)) if key != target]
    if row is not None and not retired and _ids(row) == merged:
        # NOTHING NEW: the commonest case by far — a refill naming a title it
        # named yesterday — and a write per listing would be a write per airing.
        return Identity(target, media, merged)
    created = min([int(r["created_at"]) for r in
                   (_row(conn, key) for key in (target, *absorbed)) if r is not None]
                  or [now])
    conn.execute(
        f"INSERT INTO {TABLE} (title_key, media, ids_json, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(title_key) DO UPDATE SET ids_json = excluded.ids_json, "
        "created_at = MIN(created_at, excluded.created_at)",
        (target, media, json.dumps(merged, sort_keys=True), created))
    for key in retired:
        _absorb(conn, key, target, now=now)
    return Identity(target, media, merged)


def _adopt_filing_id(conn: db.Connection, incoming: dict, filed_under: str | None) -> None:
    """Add the id a row is filed under to `incoming`, unless `incoming` states a
    shared id that title's known ids contradict — then the row is being told
    something new about itself, and the old filing is not evidence for it."""
    if not filed_under:
        return
    own = _own_id(filed_under)
    if own is None or str(incoming.get(own[0])) == str(own[1]):
        return  # nothing to adopt: the ids already state it
    row = _row(conn, follow(conn, filed_under))
    known = _ids(row) if row is not None else {own[0]: own[1]}
    for name in MATCH_SOURCES:
        stated, held = incoming.get(name), known.get(name)
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
            logger.info("title store: %s holds %s=%s; another statement said %s",
                        key, name, held, value)


def _absorb(conn: db.Connection, old: str, new: str, *, now: int) -> None:
    """Retire `old` in favour of `new`: every row holding it is repointed, every
    alias naming it now names `new`, and `old` itself becomes one."""
    counts = holders.repoint(conn, old, new, now=now)
    conn.execute(f"UPDATE {ALIASES} SET title_key = ? WHERE title_key = ?", (new, old))
    conn.execute(f"DELETE FROM {ALIASES} WHERE old_key = ?", (new,))
    conn.execute(
        f"INSERT INTO {ALIASES} (old_key, title_key, aliased_at) VALUES (?, ?, ?) "
        "ON CONFLICT(old_key) DO UPDATE SET title_key = excluded.title_key, "
        "aliased_at = excluded.aliased_at", (old, new, now))
    conn.execute(f"DELETE FROM {TABLE} WHERE title_key = ?", (old,))
    touched = {table: n for table, n in counts.items() if n}
    logger.info("title store: re-keyed %s -> %s (%s)", old, new,
                ", ".join(f"{table} {n}" for table, n in sorted(touched.items())) or "no rows")


# ---------------------------------------------------------------------------
# reading without writing
# ---------------------------------------------------------------------------

def resolve(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None) -> str | None:
    """The key `ids` would settle under, decided and NOT written: the waterfall's
    key through any alias, joined to the one title above it holding its id. None
    when the ids name no shared id at all."""
    stated = key_of(str(media), id_rules.canonical(ids))
    if stated is None:
        return None
    target = follow(conn, stated)
    return _sole_higher_holder(conn, str(media), target) or target


def complete(conn: db.Connection, media: Any, ids: Mapping[str, Any] | None) -> dict:
    """`ids` with the SHARED ids of the title they name added beneath them — the
    service's own ids winning — so that keying the result gives the title's key.

    FOR EVERY READER THAT KEYS FROM ONE SERVICE'S IDS: a calendar listing, a
    history event, a search result about to be drawn. A service that knows a show
    only by its tvdb id still names the tmdb title once its ids are completed,
    and nothing downstream has to know aliases exist. Only the shared ids are
    added: a service's own id is that service's to state.
    """
    key = resolve(conn, media, ids)
    row = None if key is None else _row(conn, key)
    return with_shared(ids, _ids(row) if row is not None else {})


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
        found = follow(conn, str(key)) if key else resolve(conn, media, ids)
        row = None if found is None else _row(conn, found)
        return _ids(row) if row is not None else {}

    known = await db.run(_work)
    return {**known, **{name: value for name, value in (ids or {}).items()
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
    return await db.run(lambda conn: {key: follow(conn, key) for key in wanted})


async def known(title_key: str) -> Identity | None:
    """The title a key names now — followed through any re-key — or None when the
    store has no such title."""
    def _work(conn):
        key = follow(conn, str(title_key))
        row = _row(conn, key)
        return None if row is None else Identity(key, str(row["media"]), _ids(row))

    return await db.run(_work)
