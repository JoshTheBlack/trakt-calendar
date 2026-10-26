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

THE RULE WHERE BOTH KEYS ARE ALREADY HELD: THE NEW KEY'S ROW WINS. Measured before
this was written, on the development copy and on a production copy: no tracker,
board or watchlist row anywhere would have collided. When one does, the row under
the old key is dropped and logged in full, and a board row goes into that board's
removal history, where its owner can see it and put it back.

WHAT A HOLDER LEARNS ON THE WAY. A row filed under the new key should state the
id it is filed by, so the column for the new key's own id space is filled where a
row has one and it is empty — never overwritten.
"""
from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple

from .. import db
from ..providers.base import ItemKey, parse_item_key
from . import ids as id_rules

logger = logging.getLogger(__name__)


class Holder(NamedTuple):
    """One table holding title keys: the columns that hold them, and what they
    mean there. What `repoint` moves, and what the schema test checks against."""
    table: str
    columns: tuple[str, ...]
    meaning: str


HOLDERS: tuple[Holder, ...] = (
    Holder("titles", ("title_key",),
           "the identity itself — merged into the new key by identity.settle"),
    Holder("title_aliases", ("old_key", "title_key"),
           "keys a title used to have — kept by identity.settle, never here"),
    Holder("title_metadata", ("title_key",), "each service's description of the title"),
    Holder("calendar_airings", ("title_key",), "every listing of the title on a calendar"),
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
    "episode_metadata": "keyed by the service's own id for the title",
    "season_metadata": "keyed by the service's own id for the title",
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
    tables that file such titles — the calendar's and a viewer's marks — so the
    tracker's and the board's are skipped for it rather than asked."""
    counts = {
        "title_metadata": _column(conn, "title_metadata", "title_key", old, new),
        "calendar_airings": _column(conn, "calendar_airings", "title_key", old, new),
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


def _move_triple(conn: db.Connection, spec: _Triple, old: ItemKey, new: ItemKey, *,
                 now: int) -> int:
    old_params = (old.media, old.match_source, old.match_id)
    if spec.same_row:
        clash = (f"{_TRIPLE_WHERE} AND EXISTS (SELECT 1 FROM {spec.table} n "
                 "WHERE n.media = ? AND n.match_source = ? AND n.match_id = ? "
                 f"AND {_same(spec.table, spec.same_row)})")
        clash_params = (*old_params, new.media, new.match_source, new.match_id)
        dropped = conn.execute(f"SELECT * FROM {spec.table} WHERE {clash}",
                               clash_params).fetchall()
        for row in dropped:
            _dropped(conn, spec.table, row, new, now=now)
        if dropped:
            conn.execute(f"DELETE FROM {spec.table} WHERE {clash}", clash_params)
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


def _dropped(conn: db.Connection, table: str, row, new: ItemKey, *, now: int) -> None:
    """A row the new key already holds the same of. Logged in full — the log is
    the only record of it left — and a board row is kept in its board's removal
    history, through the board's own writer, so its owner can put it back."""
    logger.warning("title store: dropped a %s row the re-keyed title %s already held: %s",
                   table, new, json.dumps(dict(row), default=str, sort_keys=True))
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
