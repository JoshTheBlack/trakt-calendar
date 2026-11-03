"""Every column in the schema that holds a title's key, and the one function that
moves them all when a title is re-keyed.

WHY ONE LIST, AND WHY HERE. A re-key (app/metadata/identity.py) changes the key a
title is filed under, and every row that held the old key has to follow it in the
same transaction or the title splits: a calendar airing left on the old key draws
as a second card, a tracker row stops matching its own plays, a board row is
counted as a different title from the one being added. The holders live in four
features, and a re-key cannot ask each of them in turn without importing all of
them; so the tables are named here, once, and tests/metadata/test_holders.py
fails when the schema grows a table this file has not classified — holder or not.

THE RULE WHERE BOTH KEYS ARE ALREADY HELD: THE MORE COMPLETE ROW SURVIVES, under
the new key. The two rows are one viewer's record of one title, written at
different times under two names for it, so neither is a newer truth that should
simply overwrite the other: a tracker row under the old key may be the one with
twelve watched episodes and a frozen month behind it, and the row under the new
key a stub a sync filed yesterday. Which is "more complete" is a question each
table answers in its own terms (`more_complete`, one rule per table, each
tested); the survivor is re-keyed, its EMPTY fields are filled from the other —
never a number, which stands as the survivor has it, and never summed — and the
other is logged in full. A board row that loses goes into its board's removal
history, where its owner can see it and put it back. Measured before the rule was
first written, on the development copy and a production copy: no tracker, board
or watchlist row anywhere would have collided, so this is for the day one does.

WHAT A HOLDER LEARNS ON THE WAY. A row filed under the new key should state the
id it is filed by, so the column for the new key's own id space is filled where a
row has one and it is empty — never overwritten.

AND WHAT A HOLDER KEEPS ALIVE. A feature's row is a reason to keep every service's
description of the title it holds (`held_keys`): the store's retention sweep
(app/metadata/rows.py) clears a service's group only once no listing names its
title AND no feature row holds it, so a tracker or board title never loses the
words it is drawn with to a calendar that stopped listing it.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, NamedTuple

from .. import db
from ..providers.base import ItemKey, parse_item_key
from . import ids as id_rules
from . import merge

logger = logging.getLogger(__name__)


class Holder(NamedTuple):
    """One table holding title keys: the columns that hold them, and what they
    mean there. What `repoint` moves, and what the schema test checks against."""
    table: str
    columns: tuple[str, ...]
    meaning: str


HOLDERS: tuple[Holder, ...] = (
    Holder("titles", ("title_key",),
           "the identity and every service's group — its identity merged by "
           "identity.settle, its groups by merge.titles"),
    Holder("title_aliases", ("old_key", "title_key"),
           "keys a title used to have — kept by identity.settle, never here"),
    Holder("seasons", ("title_key",), "every service's words about a season — merge.titles"),
    Holder("episodes", ("title_key",),
           "every service's listing of an episode and its words about it — merge.titles"),
    Holder("service_ids", ("title_key",),
           "which title each of a service's own ids names — merge.titles"),
    Holder("share_highlights", ("title_key",), "a shared month's featured shows"),
    Holder("title_posters", ("title_key",), "pictures of a title with no tmdb id"),
    Holder("not_watching_shows", ("item_id",), "a viewer's not-watching mark"),
    Holder("calendar_view_state", ("last_show_ids_json",),
           "the titles a viewer's last visit to a month drew, for 'new since'"),
    Holder("distrakt_user_seasons", ("media", "match_source", "match_id"), "a tracker row"),
    Holder("distrakt_month_records", ("media", "match_source", "match_id"),
           "a tracker month record — its identity moves, its frozen title does not"),
    Holder("distrakt_unsettled_rows", ("media", "match_source", "match_id"),
           "a tracker row held back from a month"),
    Holder("distrakt_movie_watches", ("media", "match_source", "match_id"),
           "a film a service says was watched"),
    Holder("distrakt_open_prompts", ("media", "match_source", "match_id"),
           "a question the tracker has not had answered"),
    Holder("distrakt_prompt_dismissals", ("media", "match_source", "match_id"),
           "a question the viewer declined"),
    Holder("distrakt_show_progress", ("media", "match_source", "match_id"),
           "a service's progress for one season"),
    Holder("distrakt_slug_repairs", ("media", "match_source", "match_id"),
           "the log of service names a row was repaired with"),
    Holder("distrakt_months", ("movies_json",), "the films a frozen month recorded"),
    Holder("tier_items", ("media", "match_source", "match_id"), "a rankings board row"),
    Holder("tier_item_removals", ("media", "match_source", "match_id", "item_json"),
           "a board row's removal history"),
    Holder("tier_category_removals", ("items_json",), "the rows a deleted tier held"),
    Holder("watchlist_writes", ("match_source", "match_id"),
           "what the tracker wrote to a service's list for a show"),
)

# Every other table, and why it holds no title key. A table missing from both
# lists fails tests/metadata/test_holders.py — which is the point: a table added
# later is a decision somebody has to make here, not a holder a re-key silently
# leaves behind.
NOT_HOLDERS: dict[str, str] = {
    "metadata_refresh": "keyed by the service's own id for the title",
    "show_posters": "keyed by tmdb id, which a re-key never takes away",
    "season_posters": "keyed by tmdb id, which a re-key never takes away",
    "simkl_titles": "keyed by Simkl's own id",
    "trakt_releases": "keyed by Trakt's own id",
    "distrakt_listed_ids": "keyed by a service's own id",
    "distrakt_watch_state": "its play counts are keyed by a service's own id",
    "calendar_coverage": "which services were asked about a span — no title",
    "calendar_source_files": "a calendar file's validators — no title",
    "api_cache": ("response bodies, and markers lasting a day to a week; a marker "
                  "under a retired key costs at most one repeated lookup"),
    "network_logos": "keyed by network name",
    "users": "accounts",
    "user_prefs": "a viewer's settings",
    "sessions": "sign-ins",
    "linked_identities": "linked accounts",
    "login_attempts": "rate limits",
    "auth_handshakes": "sign-in handshakes",
    "invites": "invitations",
    "invite_redemptions": "invitations",
    "retired_identifiers": "retired names and tokens",
    "app_meta": "instance facts",
    "app_secrets": "credentials",
    "app_settings": "instance settings",
    "schema_version": "the schema's version",
    "share_links": "a viewer's share link — it names no title",
    "distrakt_prefs": "the emoji map, keyed by network",
    "source_prefs": "a viewer's service orders",
    "tier_boards": "a board",
    "tier_categories": "a board's tiers",
    "watchlist_targets": "which services a viewer writes to",
    "sqlite_sequence": "SQLite's own bookkeeping",
}


# ---------------------------------------------------------------------------
# which holders keep a title's description alive
# ---------------------------------------------------------------------------

# A TRIPLE AS THE KEY IT SPELLS — the shared waterfall's flat form, exactly what
# `ItemKey.__str__` writes and `titles.title_key` holds.
_FLAT_TRIPLE = "media || ':' || match_source || ':' || match_id"

# THE HOLDERS WHOSE ROWS DRAW A TITLE, and so keep every service's description of
# it from the retention sweep: one query per table, each answering the keys its
# rows hold. A tracker row, a board row and what either remembers of a title it
# may put back are drawn with the store's words for the title — there is no copy
# of them anywhere else — so the words outlive the calendar's interest in it.
_KEEPS_ALIVE: dict[str, str] = {
    table: f"SELECT DISTINCT {_FLAT_TRIPLE} FROM {table}"
    for table in ("distrakt_user_seasons", "distrakt_month_records",
                  "distrakt_unsettled_rows", "distrakt_movie_watches",
                  "distrakt_open_prompts", "distrakt_prompt_dismissals",
                  "distrakt_show_progress", "tier_items", "tier_item_removals")
}

# Holders whose rows do NOT keep a description alive, each with the reason. With
# `_KEEPS_ALIVE` and the deleted tier's JSON list below this classifies every
# holder, which tests/metadata/test_holders.py checks — a holder added later is a
# decision about retention as well as about re-keying.
KEEPS_NOTHING_ALIVE: dict[str, str] = {
    "titles": "the identity, which the sweep never removes, and the groups it decides about",
    "title_aliases": "the identity's retired keys",
    "seasons": "the descriptions the sweep is deciding about",
    "episodes": "has a rule of its own: a group a service's listing stands beside is kept",
    "service_ids": "an entry's ids, kept and cleared with its group",
    "share_highlights": "a featured show is one its month lists, which keeps its words",
    "title_posters": "pictures, kept by the poster registry's own retention",
    "not_watching_shows": "a mark on a calendar card, which the card's own airing draws",
    "calendar_view_state": "which titles a visit drew, kept only to say what is new",
    "distrakt_slug_repairs": "a log of repairs, which draws no title",
    "distrakt_months": "a frozen month's films carry the title they froze with",
    "watchlist_writes": "what was written to a service's list, which draws no title",
}

# A DELETED TIER'S ROWS ARE KEYS IN A JSON LIST, a board row's key with its season
# suffix where it has one ("show:tmdb:1:s2"), and a restore puts them back.
_TIER_REMOVALS = "tier_category_removals"


def _title_part(board_key: str) -> str:
    """The title a board key names: a title key is three parts (media, id space,
    id) and a board row's season is a fourth (app/ranker/item_keys.py)."""
    return ":".join(str(board_key).split(":")[:3])


# EVERY KEY THE HOLDERS ABOVE HOLD, AND EVERY BOARD KEY A DELETED TIER LISTS, IN ONE
# ROW. The retention sweep asks this every minute, and the driver lets go of the
# GIL around every row it steps through and must win it back — up to a switch
# interval each time while the event loop is busy — so a row per key (1,679 on the
# development copy) was a switch per key; one row is one.
_HELD_SQL = (
    "SELECT json_array(json((SELECT json_group_array(key) FROM ("
    + " UNION ".join(f"SELECT {_FLAT_TRIPLE} AS key FROM {table}" for table in _KEEPS_ALIVE)
    + ") WHERE key IS NOT NULL)), "
    f"json((SELECT json_group_array(j.value) FROM {_TIER_REMOVALS} r, json_each(r.items_json) j "
    "WHERE json_valid(r.items_json) AND json_type(r.items_json) = 'array' "
    "AND j.type = 'text')))")


def held_keys(conn: db.Connection) -> set[str]:
    """Every title key a feature row holds that keeps its description alive —
    see `_KEEPS_ALIVE` — read in one row (`_HELD_SQL`). Read on the caller's
    connection, so a sweep and the rows it judges by are one moment."""
    (found,) = conn.execute(_HELD_SQL).fetchone()
    held, removed = json.loads(found)
    return {str(key) for key in held if key} | {_title_part(value) for value in removed}


def keeps_alive_tables() -> frozenset[str]:
    """The holders `held_keys` reads, for the classification test."""
    return frozenset({*_KEEPS_ALIVE, _TIER_REMOVALS})


class _Triple(NamedTuple):
    """A table filed by (media, match_source, match_id): what else makes two of
    its rows the same row — the columns a row under the new key must share for
    the old one to be a duplicate of it — and where it keeps ids of its own."""
    table: str
    same_row: tuple[str, ...]
    # Id spaces with a column of their own here, filled for the new key's space.
    id_columns: tuple[str, ...] = ()
    # Whether the table keeps an id map the new key's own id is added to.
    ids_json: bool = False


_TRACKER_IDS = ("tmdb", "tvdb", "imdb", "mal")
_TRIPLES = (
    _Triple("distrakt_user_seasons", ("user_id", "season"), _TRACKER_IDS),
    _Triple("distrakt_month_records", ("user_id", "month", "kind", "season"), _TRACKER_IDS),
    _Triple("distrakt_unsettled_rows", ("user_id", "month", "season"), _TRACKER_IDS),
    _Triple("distrakt_movie_watches", ("user_id", "source")),
    _Triple("distrakt_open_prompts", ("user_id", "season"), ids_json=True),
    _Triple("distrakt_prompt_dismissals", ("user_id", "season")),
    _Triple("distrakt_show_progress", ("user_id", "season", "source")),
    _Triple("distrakt_slug_repairs", ()),
    # A whole-show row's season is NULL, and `IS` is what compares two NULLs as
    # equal — the board's own unique index says IFNULL(season, -1) for that reason.
    _Triple("tier_items", ("board_id", "season"), ("tmdb",), ids_json=True),
)


def repoint(conn: db.Connection, old: str, new: str, *, now: int) -> dict[str, int]:
    """Move every row holding `old` to `new`, inside the caller's transaction.
    Returns {table: rows moved}. The identity tables are identity.settle's.

    A key with no shared id (a title one service names) can only be held by the
    tables that file such titles — the store's own and a viewer's marks — so the
    tracker's and the board's are skipped for it rather than asked."""
    counts = {
        # THE STORE'S OWN ROWS MERGE, each service's group decided on its own
        # (app/metadata/merge.py); every other holder is repointed.
        **merge.titles(conn, old, new, now=now),
        "share_highlights": _scoped_column(conn, "share_highlights", "title_key",
                                           ("user_id", "year", "month"), old, new),
        "title_posters": _scoped_column(conn, "title_posters", "title_key",
                                        ("media", "source"), old, new),
        "not_watching_shows": _scoped_column(conn, "not_watching_shows", "item_id",
                                             ("user_id",), old, new),
        "calendar_view_state": _json_list(conn, "calendar_view_state",
                                          "last_show_ids_json", old, new),
        "tier_category_removals": _json_list(conn, "tier_category_removals",
                                             "items_json", old, new),
        "distrakt_months": _months_films(conn, old, new),
    }
    old_key, new_key = _triple(old), _triple(new)
    if old_key is None or new_key is None:
        return counts
    for spec in _TRIPLES:
        counts[spec.table] = _move_triple(conn, spec, old_key, new_key, now=now)
    counts["tier_item_removals"] = _removal_history(conn, old, new, old_key, new_key)
    if old_key.media == "show":
        counts["watchlist_writes"] = _watchlist_writes(conn, old_key, new_key)
    return counts


def _triple(key: str) -> ItemKey | None:
    try:
        return parse_item_key(key)
    except ValueError:
        return None


def _rekeyed(value: Any, old: str, new: str) -> Any:
    """A key a feature holds, moved: exactly `old`, or `old` with that feature's
    own suffix after it (a board's season row, "show:tmdb:1:s2"), keeping the
    suffix. Anything else comes back untouched."""
    if not isinstance(value, str):
        return value
    if value == old:
        return new
    if value.startswith(old + ":"):
        return new + value[len(old):]
    return value


def _column(conn: db.Connection, table: str, column: str, old: str, new: str) -> int:
    return conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                        (new, old)).rowcount


def _same(table: str, columns: tuple[str, ...]) -> str:
    """`n` is the same row as `table`'s on every one of `columns` — `IS`, so two
    NULLs count as the same."""
    return " AND ".join(f"n.{c} IS {table}.{c}" for c in columns)


def _scoped_column(conn: db.Connection, table: str, column: str, scope: tuple[str, ...],
                   old: str, new: str) -> int:
    """One key column unique within `scope`: a row the new key already has in
    the same scope wins, and the old one goes — the two meant the same thing."""
    conn.execute(
        f"DELETE FROM {table} WHERE {column} = ? AND EXISTS ("
        f"SELECT 1 FROM {table} n WHERE n.{column} = ? AND {_same(table, scope)})",
        (old, new))
    return _column(conn, table, column, old, new)


def _like_prefix(old: str) -> str:
    """A LIKE pattern for `old` followed by a feature's own suffix, with LIKE's
    wildcards in the key itself neutralised."""
    for char in ("\\", "%", "_"):
        old = old.replace(char, "\\" + char)
    return old + ":%"


def _json_list(conn: db.Connection, table: str, column: str, old: str, new: str) -> int:
    """A JSON list of keys: each naming `old` names `new`, and a key the list
    then holds twice is held once, where it first appeared."""
    rows = conn.execute(
        f"SELECT rowid, {column} FROM {table} WHERE json_valid({column}) AND EXISTS ("
        f"SELECT 1 FROM json_each({table}.{column}) j "
        "WHERE j.value = ? OR j.value LIKE ? ESCAPE '\\')",
        (old, _like_prefix(old))).fetchall()
    for rowid, document in rows:
        moved = list(dict.fromkeys(_rekeyed(value, old, new) for value in json.loads(document)))
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                     (json.dumps(moved), rowid))
    return len(rows)


def _months_films(conn: db.Connection, old: str, new: str) -> int:
    """A frozen month's film snapshot names each film by key. The key moves; the
    title it froze with does not — a frozen month is a record, not a copy."""
    rows = conn.execute(
        "SELECT rowid, movies_json FROM distrakt_months WHERE json_valid(movies_json) "
        "AND EXISTS (SELECT 1 FROM json_each(distrakt_months.movies_json) j "
        "WHERE json_extract(j.value, '$.key') = ?)", (old,)).fetchall()
    for rowid, document in rows:
        films, seen = [], set()
        for film in json.loads(document):
            if isinstance(film, dict) and film.get("key") == old:
                film = {**film, "key": new}
            key = film.get("key") if isinstance(film, dict) else None
            if key is not None and key in seen:
                continue
            seen.add(key)
            films.append(film)
        conn.execute("UPDATE distrakt_months SET movies_json = ? WHERE rowid = ?",
                     (json.dumps(films), rowid))
    return len(rows)


_TRIPLE_WHERE = "media = ? AND match_source = ? AND match_id = ?"


# ---------------------------------------------------------------------------
# where both keys hold the same row: the more complete one survives
# ---------------------------------------------------------------------------

def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _month_record_rank(row: Mapping[str, Any]) -> tuple:
    """A MONTH RECORD: one that RECORDED a per-service breakdown beats one that did
    not — a breakdown is written when a month freezes, and a frozen record is the
    only account of that month there will ever be — then more watched, then one
    that knows its episode total, then the one added first."""
    recorded = row["watched_by_source"] is not None or row["total_by_source"] is not None
    return (recorded, _int(row["watched"]), _int(row["total"]) > 0, -_int(row["created_at"]))


def _tracker_row_rank(row: Mapping[str, Any]) -> tuple:
    """A TRACKER ROW: more watched, then one that knows its episode total, then the
    one added first — the earlier filing is the one the viewer has been living
    with, and its watermarks were set against it."""
    return (_int(row["watched"]), _int(row["total"]) > 0, -_int(row["created_at"]))


def _first_added_rank(row: Mapping[str, Any]) -> tuple:
    """A row held back from a month says nothing but that it is held: the one
    filed first."""
    return (-_int(row["created_at"]),)


def _board_row_rank(row: Mapping[str, Any]) -> tuple:
    """A BOARD ROW: one placed in a tier beats one in the pool — the placement is
    the work a board is for — then one the viewer rated, then the one added
    first."""
    return (row["category_id"] is not None, row["user_rating"] is not None,
            -_int(row["created_at"]), -_int(row["id"]))


def _newer_play_rank(row: Mapping[str, Any]) -> tuple:
    """A SERVICE'S ANSWER about a play: the newer one, by when the play was — the
    rule the watch state already applies to two rows for one film."""
    return (str(row["watched_at"] or ""),)


def _newer_question_rank(row: Mapping[str, Any]) -> tuple:
    """An OPEN QUESTION about a play: the newer play's, then the newer asking."""
    return (str(row["watched_at"] or ""), _int(row["created_at"]))


def _newer_answer_rank(row: Mapping[str, Any]) -> tuple:
    """A DECLINED QUESTION: the newer refusal, which is the viewer's latest word."""
    return (_int(row["created_at"]),)


def _no_clock_rank(_row: Mapping[str, Any]) -> tuple:
    """A SERVICE'S PROGRESS FOR A SEASON keeps no clock — the table is rewritten
    whole on every sync (app/distrakt/watch_history.py's `_save`) — so neither row
    is newer, and on a tie the new key's row stands. The next sync rewrites it from
    the service either way."""
    return ()


# WHICH ROW IS MORE COMPLETE, per table — one rule each, and every table a re-key
# can find holding one row twice has one (tests/metadata/test_holders.py). A
# larger rank is more complete; a tie keeps the new key's row.
_RANKS: dict[str, Callable[[Mapping[str, Any]], tuple]] = {
    "distrakt_user_seasons": _tracker_row_rank,
    "distrakt_month_records": _month_record_rank,
    "distrakt_unsettled_rows": _first_added_rank,
    "distrakt_movie_watches": _newer_play_rank,
    "distrakt_open_prompts": _newer_question_rank,
    "distrakt_prompt_dismissals": _newer_answer_rank,
    "distrakt_show_progress": _no_clock_rank,
    "tier_items": _board_row_rank,
}


def more_complete(table: str, candidate: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """Whether `candidate` is the more complete of two rows `table` holds for one
    title under two keys — strictly: a tie is not a win."""
    rank = _RANKS[table]
    return rank(candidate) > rank(other)


def _empty(value: Any) -> bool:
    """A field with nothing in it. A NUMBER IS NEVER EMPTY — a zero is a count the
    row states, and filling it from the other row would invent one."""
    return value is None or (isinstance(value, str) and value.strip() in ("", "{}", "[]"))


def _loads_map(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def gaps_filled(survivor: Mapping[str, Any], other: Mapping[str, Any],
                fixed: tuple[str, ...]) -> dict[str, Any]:
    """{column: value} that fills `survivor`'s empty fields from `other`. `fixed`
    are the columns that say WHICH row it is (its identity, its scope, its id),
    which are never copied. An id map is merged, the survivor's ids winning."""
    out: dict[str, Any] = {}
    for column in survivor.keys():
        if column in fixed or column == "rid":
            continue
        mine, theirs = survivor[column], other[column]
        if column == "ids_json":
            merged = {**_loads_map(theirs), **_loads_map(mine)}
            if merged != _loads_map(mine):
                out[column] = json.dumps(merged, sort_keys=True)
        elif _empty(mine) and not _empty(theirs):
            out[column] = theirs
    return out


def _settle_clash(conn: db.Connection, spec: _Triple, old_row, new_row, new: ItemKey, *,
                  now: int) -> None:
    """One row both keys hold: the more complete survives (the old one is then
    moved by the caller's re-key like any other), its gaps are filled from the
    other, and the other goes — logged in full, and a board row into its board's
    removal history."""
    if more_complete(spec.table, old_row, new_row):
        survivor, loser = old_row, new_row
    else:
        survivor, loser = new_row, old_row
    fixed = ("id", "media", "match_source", "match_id", *spec.same_row)
    fill = gaps_filled(survivor, loser, fixed)
    _dropped(conn, spec.table, loser, new, kept_old=survivor is old_row, now=now)
    conn.execute(f"DELETE FROM {spec.table} WHERE rowid = ?", (loser["rid"],))
    if fill:
        conn.execute(
            f"UPDATE {spec.table} SET {', '.join(f'{column} = ?' for column in fill)} "
            "WHERE rowid = ?", (*fill.values(), survivor["rid"]))


def _move_triple(conn: db.Connection, spec: _Triple, old: ItemKey, new: ItemKey, *,
                 now: int) -> int:
    old_params = (old.media, old.match_source, old.match_id)
    if spec.same_row:
        twin_where = " AND ".join(f"{column} IS ?" for column in spec.same_row)
        for old_row in conn.execute(
                f"SELECT rowid AS rid, * FROM {spec.table} WHERE {_TRIPLE_WHERE}",
                old_params).fetchall():
            new_row = conn.execute(
                f"SELECT rowid AS rid, * FROM {spec.table} WHERE {_TRIPLE_WHERE} "
                f"AND {twin_where}",
                (new.media, new.match_source, new.match_id,
                 *(old_row[column] for column in spec.same_row))).fetchone()
            if new_row is not None:
                _settle_clash(conn, spec, old_row, new_row, new, now=now)
    own = id_rules.typed(new.match_source, new.match_id)
    sets = ["match_source = ?", "match_id = ?"]
    params: list = [new.match_source, new.match_id]
    if new.match_source in spec.id_columns:
        column = new.match_source
        sets.append(f"{column} = CASE WHEN {column} IS NULL OR {column} = '' "
                    f"THEN ? ELSE {column} END")
        params.append(own)
    if spec.ids_json:
        # Patched UNDER what the row holds: an id it already states is kept.
        sets.append("ids_json = json_patch(?, ids_json)")
        params.append(json.dumps({new.match_source: own}))
    return conn.execute(f"UPDATE {spec.table} SET {', '.join(sets)} WHERE {_TRIPLE_WHERE}",
                        (*params, *old_params)).rowcount


def _dropped(conn: db.Connection, table: str, row, new: ItemKey, *, kept_old: bool,
             now: int) -> None:
    """The less complete of two rows one title held under two keys. Logged in full
    — the log is the only record of it left — and a board row is kept in its
    board's removal history, through the board's own writer, so its owner can put
    it back. `kept_old` says which survived, so the line says which way it went."""
    record = {name: row[name] for name in row.keys() if name != "rid"}
    logger.warning("title store: re-keying %s found a %s row under both keys; kept the "
                   "%s key's as the more complete, dropped: %s",
                   new, table, "old" if kept_old else "new",
                   json.dumps(record, default=str, sort_keys=True))
    if table == "tier_items":
        # Deferred: the board imports this package, and its removal history is its
        # own to write — what a restore reads back is the board's projection of
        # a row, which only the board knows.
        from ..ranker import core as ranker_core
        ranker_core.remember_removal(conn, int(row["board_id"]), int(row["id"]), now)


def _removal_history(conn: db.Connection, old: str, new: str, old_key: ItemKey,
                     new_key: ItemKey) -> int:
    """A board's removal history: one entry per board row, so where both keys hold
    one for the same board and season, the NEWER removal is kept — the board's own
    rule for a title removed twice — and its stored record names the new key."""
    same = "n.board_id IS tier_item_removals.board_id AND n.season IS tier_item_removals.season"
    for older, newer in ((old_key, new_key), (new_key, old_key)):
        # `>=` one way and `>` the other, so a tie keeps the new key's entry.
        compare = ">=" if older is old_key else ">"
        conn.execute(
            f"DELETE FROM tier_item_removals WHERE {_TRIPLE_WHERE} AND EXISTS ("
            f"SELECT 1 FROM tier_item_removals n WHERE {same} "
            "AND n.media = ? AND n.match_source = ? AND n.match_id = ? "
            f"AND n.removed_at {compare} tier_item_removals.removed_at)",
            (older.media, older.match_source, older.match_id,
             newer.media, newer.match_source, newer.match_id))
    rows = conn.execute(f"SELECT id, item_json FROM tier_item_removals WHERE {_TRIPLE_WHERE}",
                        (old_key.media, old_key.match_source, old_key.match_id)).fetchall()
    for row_id, document in rows:
        try:
            record = json.loads(document or "{}")
        except ValueError:
            record = {}
        if isinstance(record, dict) and "key" in record:
            record["key"] = _rekeyed(record["key"], old, new)
        conn.execute(
            "UPDATE tier_item_removals SET match_source = ?, match_id = ?, item_json = ? "
            "WHERE id = ?",
            (new_key.match_source, new_key.match_id, json.dumps(record, sort_keys=True), row_id))
    return len(rows)


def _watchlist_writes(conn: db.Connection, old: ItemKey, new: ItemKey) -> int:
    """What the tracker wrote to a service's list, per show. Keyed on the show
    alone (a service lists a show once), so there is no media column; the new
    key's row wins, for the reason every other holder's does."""
    where = "match_source = ? AND match_id = ?"
    clash = (f"{where} AND EXISTS (SELECT 1 FROM watchlist_writes n WHERE "
             f"{_same('watchlist_writes', ('user_id', 'source'))} "
             "AND n.match_source = ? AND n.match_id = ?)")
    params = (old.match_source, old.match_id, new.match_source, new.match_id)
    for row in conn.execute(f"SELECT * FROM watchlist_writes WHERE {clash}", params).fetchall():
        logger.warning("title store: dropped a watchlist_writes row the re-keyed title %s "
                       "already held: %s", new, json.dumps(dict(row), sort_keys=True))
    conn.execute(f"DELETE FROM watchlist_writes WHERE {clash}", params)
    return conn.execute(
        f"UPDATE watchlist_writes SET match_source = ?, match_id = ? WHERE {where}",
        (new.match_source, new.match_id, old.match_source, old.match_id)).rowcount
