"""app/metadata/holders.py — every column holding a title's key, moved together.

TWO PROMISES, and the first is what keeps the second true:
  - EVERY TABLE IN THE SCHEMA IS CLASSIFIED: a holder, with the columns it holds
    keys in, or a non-holder, with the reason. A table added later fails here
    until somebody decides which it is, and a classified table that grows a
    column named like a key fails until that column is accounted for — so a
    re-key cannot silently leave a new holder behind.
  - A RE-KEY MOVES A ROW IN EVERY HOLDER, in the transaction it happens in; where
    a holder already has the new key's row the new one wins and the old one is
    dropped (a board's into its removal history); and a key a feature extends
    with its own suffix keeps the suffix.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unittest

from app import db
from app.metadata import holders, identity
from app.providers.base import ItemKey, Media
from tests.support import DatabaseTestCase, migrated_db

# Column names that hold, or are shaped to hold, a title's key. A column matching
# this on a table either holder list names must be accounted for below.
_KEYISH = re.compile(r"(title_key|match_source|match_id|item_id|old_key|_key$|_keys?_json$|"
                     r"^items?_json$|show_ids|movies_json)")

# Columns that match the pattern and hold no title key, each with the reason.
_NOT_A_TITLE_KEY = {
    ("api_cache", "cache_key"): "a response's address — keys may appear inside it as text, "
                                "and a marker under a retired key only costs a lookup",
    ("app_meta", "key"): "an instance fact's name",
    ("tier_item_removals", "item_json"): "listed as a holder column",
}


def _schema() -> dict[str, list[str]]:
    path = migrated_db("holders-schema")
    db.close_thread_connection()
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')] for t in tables}
    finally:
        conn.close()


class EveryTableIsClassifiedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = _schema()
        cls.held = {h.table: h for h in holders.HOLDERS}

    def test_every_table_is_a_holder_or_says_why_it_is_not(self):
        unclassified = sorted(t for t in self.schema
                              if t not in self.held and t not in holders.NOT_HOLDERS)
        self.assertEqual(unclassified, [], (
            "these tables are in neither app/metadata/holders.py list. Decide: does "
            "a row here hold a title's key (then a re-key must move it) or not?"))

    def test_no_table_is_both(self):
        self.assertEqual(sorted(set(self.held) & set(holders.NOT_HOLDERS)), [])

    def test_the_lists_name_only_tables_that_exist(self):
        self.assertEqual(sorted(t for t in (*self.held, *holders.NOT_HOLDERS)
                                if t not in self.schema), [])

    def test_a_holders_columns_exist(self):
        for holder in holders.HOLDERS:
            with self.subTest(table=holder.table):
                self.assertTrue(set(holder.columns) <= set(self.schema[holder.table]))

    def test_the_shape_test_recognises_every_holder_column_it_guards(self):
        """A pattern too narrow to see the columns already known would pass the
        next test while checking nothing."""
        unseen = sorted(f"{h.table}.{c}" for h in holders.HOLDERS for c in h.columns
                        if c != "media" and not _KEYISH.search(c))
        self.assertEqual(unseen, [])

    def test_every_column_shaped_like_a_key_is_accounted_for(self):
        unlisted = []
        for table, columns in self.schema.items():
            declared = set(self.held[table].columns) if table in self.held else set()
            for column in columns:
                if (_KEYISH.search(column) and column not in declared
                        and (table, column) not in _NOT_A_TITLE_KEY):
                    unlisted.append(f"{table}.{column}")
        self.assertEqual(sorted(unlisted), [], (
            "these columns look like they hold a title's key and no holder lists "
            "them — add them to app/metadata/holders.py (and to repoint) or say "
            "here why they do not"))


OLD = ItemKey("show", "tvdb", "4321")
NEW = ItemKey("show", "tmdb", "900")


def _hold(conn, user_id: int, board_id: int, key: ItemKey, *, title: str = "Old") -> None:
    """One row under `key` in every holder table a re-key moves. The tables keyed
    by a month rather than by the title take a month of the key's own, so two
    keys' rows sit side by side."""
    flat, triple = str(key), (key.media, key.match_source, key.match_id)
    month = 6 if key == OLD else 5
    conn.execute("INSERT INTO title_metadata (source, media, source_id, title_key, title, "
                 "fetched_at) VALUES ('simkl', 'show', ?, ?, ?, 1)", (key.match_id, flat, title))
    conn.execute("INSERT INTO calendar_airings (source, media, source_id, endpoint, title_key, "
                 "air_ts, air_date, stored_at) VALUES ('simkl', 'show', ?, 'shows', ?, "
                 "1783540800, '2026-07-08', 1)", (key.match_id, flat))
    conn.execute("INSERT INTO title_posters (media, title_key, source, url, first_seen_at, "
                 "last_seen_at) VALUES ('show', ?, 'simkl', 'https://p.test/x.jpg', 1, 1)",
                 (flat,))
    conn.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) VALUES (?, ?, 1)",
                 (user_id, flat))
    conn.execute("INSERT INTO calendar_view_state (user_id, endpoint, year, month, "
                 "last_show_ids_json, updated_at) VALUES (?, 'shows', 2026, ?, ?, 1)",
                 (user_id, month, json.dumps(["show:tmdb:1", flat])))
    conn.execute("INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
                 "season, kind, title, created_at) VALUES (?, ?, ?, ?, 1, 'keepup', ?, 1)",
                 (user_id, *triple, title))
    conn.execute("INSERT INTO distrakt_month_records (user_id, month, kind, media, match_source, "
                 "match_id, season, title, created_at) VALUES (?, '2026-07', 'completed', ?, ?, "
                 "?, 1, ?, 1)", (user_id, *triple, title))
    conn.execute("INSERT INTO distrakt_unsettled_rows (user_id, month, media, match_source, "
                 "match_id, season, created_at) VALUES (?, '2026-07', ?, ?, ?, 1, 1)",
                 (user_id, *triple))
    conn.execute("INSERT INTO distrakt_movie_watches (user_id, media, match_source, match_id, "
                 "source) VALUES (?, ?, ?, ?, 'trakt')", (user_id, *triple))
    conn.execute("INSERT INTO distrakt_open_prompts (user_id, media, match_source, match_id, "
                 "season, number, ids_json, created_at) VALUES (?, ?, ?, ?, 1, 2, '{}', 1)",
                 (user_id, *triple))
    conn.execute("INSERT INTO distrakt_prompt_dismissals (user_id, media, match_source, "
                 "match_id, season, created_at) VALUES (?, ?, ?, ?, 1, 1)", (user_id, *triple))
    conn.execute("INSERT INTO distrakt_show_progress (user_id, media, match_source, match_id, "
                 "season, source) VALUES (?, ?, ?, ?, 1, 'simkl')", (user_id, *triple))
    conn.execute("INSERT INTO distrakt_slug_repairs (user_id, media, match_source, match_id, "
                 "column_name, value, evidence, repaired_at) VALUES (?, ?, ?, ?, 'simkl_slug', "
                 "'x', 'detail_lookup', 1)", (user_id, *triple))
    conn.execute("INSERT INTO distrakt_months (user_id, month, movies_json, created_at) "
                 "VALUES (?, ?, ?, 1)",
                 (user_id, f"2026-0{month}", json.dumps([{"key": flat, "title": title}])))
    conn.execute("INSERT INTO tier_items (board_id, media, match_source, match_id, ids_json, "
                 "title, created_at) VALUES (?, ?, ?, ?, ?, ?, 1)",
                 (board_id, *triple, json.dumps({key.match_source: int(key.match_id)}), title))
    conn.execute("INSERT INTO tier_item_removals (board_id, media, match_source, match_id, "
                 "season, removed_at, item_json) VALUES (?, ?, ?, ?, 2, 1, ?)",
                 (board_id, *triple, json.dumps({"key": f"{flat}:s2", "title": title})))
    conn.execute("INSERT INTO tier_category_removals (board_id, category_uid, sort_order, "
                 "removed_at, category_json, items_json) VALUES (?, 'tier-a', 0, 1, '{}', ?)",
                 (board_id, json.dumps([f"{flat}:s2", flat])))
    conn.execute("INSERT INTO watchlist_writes (user_id, source, match_source, match_id, "
                 "status_before, state, updated_at) VALUES (?, 'simkl', ?, ?, 'none', "
                 "'written', 1)", (user_id, key.match_source, key.match_id))


# What `_hold` fills, which must be every holder a re-key moves — the identity's
# own two tables are settle's and are checked by its tests.
_FIXTURE_TABLES = {
    "title_metadata", "calendar_airings", "title_posters", "not_watching_shows",
    "calendar_view_state", "distrakt_user_seasons", "distrakt_month_records",
    "distrakt_unsettled_rows", "distrakt_movie_watches", "distrakt_open_prompts",
    "distrakt_prompt_dismissals", "distrakt_show_progress", "distrakt_slug_repairs",
    "distrakt_months", "tier_items", "tier_item_removals", "tier_category_removals",
    "watchlist_writes",
}


def _count(conn, table: str, key: ItemKey) -> int:
    """How many rows of `table` hold `key`."""
    flat = str(key)
    if table in ("title_metadata", "calendar_airings", "title_posters"):
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE title_key = ?", (flat,)).fetchone()[0]
    if table == "not_watching_shows":
        return conn.execute("SELECT COUNT(*) FROM not_watching_shows WHERE item_id = ?",
                            (flat,)).fetchone()[0]
    if table == "calendar_view_state":
        return conn.execute("SELECT COUNT(DISTINCT calendar_view_state.rowid) FROM "
                            "calendar_view_state, json_each(last_show_ids_json) j "
                            "WHERE j.value = ?", (flat,)).fetchone()[0]
    if table == "distrakt_months":
        return conn.execute("SELECT COUNT(DISTINCT distrakt_months.rowid) FROM distrakt_months, "
                            "json_each(movies_json) j WHERE json_extract(j.value, '$.key') = ?",
                            (flat,)).fetchone()[0]
    if table == "tier_category_removals":
        return conn.execute("SELECT COUNT(DISTINCT tier_category_removals.id) FROM "
                            "tier_category_removals, json_each(items_json) j "
                            "WHERE j.value = ? OR j.value = ?", (flat, f"{flat}:s2")).fetchone()[0]
    if table == "watchlist_writes":
        return conn.execute("SELECT COUNT(*) FROM watchlist_writes WHERE match_source = ? "
                            "AND match_id = ?", (key.match_source, key.match_id)).fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE media = ? AND match_source = ? "
                        "AND match_id = ?", (key.media, key.match_source, key.match_id)).fetchone()[0]


class ARekeyMovesEveryHolderTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db.now()

        def _setup(conn):
            user = conn.execute("INSERT INTO users (username, created_at, updated_at) "
                                "VALUES ('viewer', ?, ?)", (now, now)).lastrowid
            board = conn.execute("INSERT INTO tier_boards (user_id, uid, created_at, "
                                 "updated_at) VALUES (?, 'b1', ?, ?)", (user, now, now)).lastrowid
            return user, board

        self.user_id, self.board_id = await db.transaction(_setup)

    async def rekey(self):
        """Register the title under the old key, then teach it its tmdb id."""
        await db.transaction(lambda conn: identity.settle(
            conn, Media.SHOW, {"tvdb": 4321}, filed_under=str(OLD), now=10))
        await db.transaction(lambda conn: identity.settle(
            conn, Media.SHOW, {"tvdb": 4321, "tmdb": 900}, filed_under=str(OLD), now=20))

    def test_the_fixture_fills_every_holder_a_rekey_moves(self):
        moved = {h.table for h in holders.HOLDERS} - {"titles", "title_aliases"}
        self.assertEqual(_FIXTURE_TABLES, moved)

    async def test_every_holder_follows_the_title_to_its_new_key(self):
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await self.rekey()
        for table in sorted(_FIXTURE_TABLES):
            with self.subTest(table=table):
                old = await db.run(lambda conn, t=table: _count(conn, t, OLD))
                new = await db.run(lambda conn, t=table: _count(conn, t, NEW))
                self.assertEqual((old, new), (0, 1))

    async def test_a_suffix_a_feature_put_on_the_key_is_kept(self):
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await self.rekey()
        record = json.loads(await db.fetch_value("SELECT item_json FROM tier_item_removals"))
        self.assertEqual(record["key"], f"{NEW}:s2")
        self.assertEqual(json.loads(await db.fetch_value(
            "SELECT items_json FROM tier_category_removals")), [f"{NEW}:s2", str(NEW)])

    async def test_a_moved_row_learns_the_id_it_is_now_filed_by(self):
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await self.rekey()
        self.assertEqual(await db.fetch_value("SELECT tmdb FROM distrakt_user_seasons"), 900)
        row = await db.fetch_one("SELECT tmdb, ids_json FROM tier_items")
        self.assertEqual((row["tmdb"], json.loads(row["ids_json"])),
                         (900, {"tvdb": 4321, "tmdb": 900}))

    async def test_where_both_keys_are_held_the_new_keys_row_wins(self):
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, NEW,
                                                title="New"))
        with self.assertLogs("app.metadata.holders", "WARNING"):
            await self.rekey()
        self.assertEqual(await db.fetch_value(
            "SELECT title FROM distrakt_user_seasons WHERE match_source = 'tmdb'"), "New")
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM distrakt_user_seasons"), 1)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM not_watching_shows"), 1)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM watchlist_writes"), 1)

    async def test_a_dropped_board_row_goes_to_its_boards_removal_history(self):
        """So its owner can see it and put it back."""
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, NEW,
                                                title="New"))
        with self.assertLogs("app.metadata.holders", "WARNING"):
            await self.rekey()
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM tier_items"), 1)
        history = await db.fetch_all(
            "SELECT match_source, season, item_json FROM tier_item_removals "
            "WHERE season IS NULL")
        self.assertEqual([(r["match_source"], json.loads(r["item_json"])["title"])
                          for r in history], [("tmdb", "Old")])

    async def test_a_title_no_shared_id_names_moves_only_where_such_titles_are_held(self):
        await db.execute("INSERT INTO calendar_airings (source, media, source_id, endpoint, "
                         "title_key, air_ts, air_date, stored_at) VALUES ('simkl', 'show', "
                         "'55', 'shows', 'simkl:55', 1783540800, '2026-07-08', 1)")
        await db.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                         "VALUES (?, 'simkl:55', 1)", (self.user_id,))
        await db.transaction(lambda conn: identity.settle(
            conn, Media.SHOW, {"simkl": 55}, fallback="simkl:55", now=10))
        await db.transaction(lambda conn: identity.settle(
            conn, Media.SHOW, {"simkl": 55, "tvdb": 4321}, filed_under="simkl:55", now=20))
        self.assertEqual(await db.fetch_value("SELECT title_key FROM calendar_airings"),
                         "show:tvdb:4321")
        self.assertEqual(await db.fetch_value("SELECT item_id FROM not_watching_shows"),
                         "show:tvdb:4321")
