"""app/metadata/holders.py — every column holding a title's key, moved together.

TWO PROMISES, and the first is what keeps the second true:
  - EVERY TABLE IN THE SCHEMA IS CLASSIFIED: a holder, with the columns it holds
    keys in, or a non-holder, with the reason. A table added later fails here
    until somebody decides which it is, and a classified table that grows a
    column named like a key fails until that column is accounted for — so a
    re-key cannot silently leave a new holder behind.
  - A RE-KEY MOVES A ROW IN EVERY HOLDER, in the transaction it happens in; where
    a holder already has the new key's row the MORE COMPLETE of the two survives
    under the new key with its gaps filled from the other, which is dropped (a
    board's into its removal history); and a key a feature extends with its own
    suffix keeps the suffix.
And every holder says whether its rows keep a title's description alive against
the store's retention sweep.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unittest

from app import db
from app.metadata import holders, identity, tables
from app.providers.base import ItemKey, Media
from tests.support import (DatabaseTestCase, listings, migrated_db, put_listing, put_season,
                           put_title)

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
        """A retired table is read by nothing and says why it is kept in
        app/metadata/tables.py, which is its classification."""
        unclassified = sorted(t for t in self.schema
                              if t not in self.held and t not in holders.NOT_HOLDERS
                              and t not in tables.RETIRED)
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

    def test_every_holder_says_whether_it_keeps_a_description_alive(self):
        keeps, not_kept = holders.keeps_alive_tables(), set(holders.KEEPS_NOTHING_ALIVE)
        self.assertEqual(sorted(set(self.held) - keeps - not_kept), [], (
            "these holders are in neither retention list in app/metadata/holders.py. "
            "Decide: does a row here draw its title, so the title's description must "
            "outlive the calendar's interest in it?"))
        self.assertEqual(sorted(keeps & not_kept), [])
        self.assertEqual(sorted((keeps | not_kept) - set(self.held)), [])

    def test_every_table_a_rekey_can_find_twice_has_a_completeness_rule(self):
        clashing = {spec.table for spec in holders._TRIPLES if spec.same_row}
        self.assertEqual(sorted(clashing - set(holders._RANKS)), [])

    def test_every_column_shaped_like_a_key_is_accounted_for(self):
        unlisted = []
        for table, columns in self.schema.items():
            if table in tables.RETIRED:
                continue
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
    put_title(conn, "simkl", "show", key.match_id, flat, title=title, fetched_at=1)
    put_season(conn, "simkl", flat, 1, name=title)
    put_listing(conn, "simkl", "show", key.match_id, "shows", flat, 1783540800,
                season=1, number=1, stored_at=1)
    conn.execute("INSERT INTO share_highlights (user_id, year, month, title_key, position, "
                 "created_at) VALUES (?, 2026, ?, ?, 0, 1)", (user_id, month, flat))
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
    "seasons", "episodes", "service_ids", "share_highlights", "title_posters",
    "not_watching_shows",
    "calendar_view_state", "distrakt_user_seasons", "distrakt_month_records",
    "distrakt_unsettled_rows", "distrakt_movie_watches", "distrakt_open_prompts",
    "distrakt_prompt_dismissals", "distrakt_show_progress", "distrakt_slug_repairs",
    "distrakt_months", "tier_items", "tier_item_removals", "tier_category_removals",
    "watchlist_writes",
}


def _count(conn, table: str, key: ItemKey) -> int:
    """How many rows of `table` hold `key`."""
    flat = str(key)
    if table in ("seasons", "episodes", "service_ids", "share_highlights", "title_posters"):
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

    async def test_where_both_keys_hold_an_equal_row_the_new_keys_row_stands(self):
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

    async def test_the_more_complete_tracker_row_survives_under_the_new_key(self):
        """The row under the OLD key has watched more: it is the viewer's record,
        and the new key's row was a stub a later filing made. It survives, is
        re-keyed, keeps its own numbers and takes what it lacked from the stub."""
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, NEW,
                                                title="New"))
        await db.execute("UPDATE distrakt_user_seasons SET watched = 7, total = 10, "
                         "network = '' WHERE match_source = 'tvdb'")
        await db.execute("UPDATE distrakt_user_seasons SET watched = 2, total = 0, "
                         "network = 'HBO', history_from = '2026-01-01' "
                         "WHERE match_source = 'tmdb'")
        with self.assertLogs("app.metadata.holders", "WARNING") as logged:
            await self.rekey()
        rows = await db.fetch_all("SELECT * FROM distrakt_user_seasons")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["match_source"], row["match_id"]), ("tmdb", "900"))
        self.assertEqual((row["title"], row["watched"], row["total"]), ("Old", 7, 10))
        self.assertEqual((row["network"], row["history_from"]), ("HBO", "2026-01-01"))
        self.assertTrue(any("kept the old key's" in line and '"watched": 2' in line
                            for line in logged.output))

    async def _two_board_rows(self) -> tuple[int, int]:
        """A board row under each key — told apart by the viewer's rating, which
        is the row's own (its words are the title store's) — as (old id, new id)."""
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, OLD))
        await db.transaction(lambda conn: _hold(conn, self.user_id, self.board_id, NEW,
                                                title="New"))
        await db.execute("UPDATE tier_items SET user_rating = CASE match_source "
                         "WHEN 'tvdb' THEN 3 ELSE 9 END")
        old = await db.fetch_value("SELECT id FROM tier_items WHERE match_source = 'tvdb'")
        new = await db.fetch_value("SELECT id FROM tier_items WHERE match_source = 'tmdb'")
        return old, new

    async def _history_ratings(self) -> list:
        return [json.loads(r["item_json"])["user_rating"] for r in await db.fetch_all(
            "SELECT item_json FROM tier_item_removals WHERE season IS NULL")]

    async def test_a_tiered_board_row_survives_and_the_other_goes_to_its_history(self):
        """So its owner can see the dropped one and put it back."""
        _old, new = await self._two_board_rows()
        category = (await db.execute(
            "INSERT INTO tier_categories (board_id, uid, label, sort_order, created_at) "
            "VALUES (?, 'tier-s', 'S', 0, 1)", (self.board_id,))).lastrowid
        await db.execute("UPDATE tier_items SET category_id = ? WHERE match_source = 'tmdb'",
                         (category,))
        with self.assertLogs("app.metadata.holders", "WARNING"):
            await self.rekey()
        row = await db.fetch_one("SELECT id, category_id FROM tier_items")
        self.assertEqual((row["id"], row["category_id"]), (new, category))
        self.assertEqual(await self._history_ratings(), [3])

    async def test_between_two_untiered_board_rows_the_earlier_survives(self):
        old, _new = await self._two_board_rows()
        with self.assertLogs("app.metadata.holders", "WARNING"):
            await self.rekey()
        self.assertEqual(await db.fetch_value("SELECT id FROM tier_items"), old)
        self.assertEqual(await self._history_ratings(), [9])

    async def test_a_title_no_shared_id_names_moves_only_where_such_titles_are_held(self):
        await db.transaction(lambda conn: put_listing(
            conn, "simkl", "show", "55", "shows", "simkl:55", 1783540800,
            season=1, number=1, stored_at=1))
        await db.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                         "VALUES (?, 'simkl:55', 1)", (self.user_id,))
        await db.transaction(lambda conn: identity.settle(
            conn, Media.SHOW, {"simkl": 55}, fallback="simkl:55", now=10))
        await db.transaction(lambda conn: identity.settle(
            conn, Media.SHOW, {"simkl": 55, "tvdb": 4321}, filed_under="simkl:55", now=20))
        self.assertEqual([listed["title_key"] for listed in await listings()],
                         ["show:tvdb:4321"])
        self.assertEqual(await db.fetch_value("SELECT item_id FROM not_watching_shows"),
                         "show:tvdb:4321")




def _row(**values) -> dict:
    return {"rid": 1, **values}


class MoreCompleteTests(unittest.TestCase):
    """One rule per table, each a pure comparison of two rows."""

    def test_a_month_record_that_recorded_a_breakdown_beats_one_that_did_not(self):
        frozen = _row(watched_by_source='{"trakt": 3}', total_by_source=None, watched=3,
                      total=10, created_at=5)
        open_ = _row(watched_by_source=None, total_by_source=None, watched=9, total=10,
                     created_at=1)
        self.assertTrue(holders.more_complete("distrakt_month_records", frozen, open_))
        self.assertFalse(holders.more_complete("distrakt_month_records", open_, frozen))

    def test_a_month_record_then_ranks_watched_then_a_known_total_then_the_earlier(self):
        base = dict(watched_by_source=None, total_by_source=None)
        self.assertTrue(holders.more_complete(
            "distrakt_month_records", _row(**base, watched=4, total=0, created_at=9),
            _row(**base, watched=3, total=10, created_at=1)))
        self.assertTrue(holders.more_complete(
            "distrakt_month_records", _row(**base, watched=3, total=10, created_at=9),
            _row(**base, watched=3, total=0, created_at=1)))
        self.assertTrue(holders.more_complete(
            "distrakt_month_records", _row(**base, watched=3, total=10, created_at=1),
            _row(**base, watched=3, total=10, created_at=9)))

    def test_a_tracker_row_ranks_watched_then_a_known_total_then_the_earlier(self):
        rank = lambda a, b: holders.more_complete("distrakt_user_seasons", a, b)  # noqa: E731
        self.assertTrue(rank(_row(watched=5, total=0, created_at=9),
                             _row(watched=4, total=8, created_at=1)))
        self.assertTrue(rank(_row(watched=4, total=8, created_at=9),
                             _row(watched=4, total=0, created_at=1)))
        self.assertTrue(rank(_row(watched=4, total=8, created_at=1),
                             _row(watched=4, total=8, created_at=9)))

    def test_a_board_row_ranks_a_tier_then_a_rating_then_the_earlier(self):
        rank = lambda a, b: holders.more_complete("tier_items", a, b)  # noqa: E731
        self.assertTrue(rank(_row(category_id=3, user_rating=None, created_at=9, id=9),
                             _row(category_id=None, user_rating=10, created_at=1, id=1)))
        self.assertTrue(rank(_row(category_id=None, user_rating=7, created_at=9, id=9),
                             _row(category_id=None, user_rating=None, created_at=1, id=1)))
        self.assertTrue(rank(_row(category_id=None, user_rating=None, created_at=1, id=1),
                             _row(category_id=None, user_rating=None, created_at=9, id=9)))

    def test_a_held_back_row_keeps_the_first_filed(self):
        self.assertTrue(holders.more_complete("distrakt_unsettled_rows",
                                              _row(created_at=1), _row(created_at=2)))

    def test_questions_answers_and_plays_keep_the_newer(self):
        self.assertTrue(holders.more_complete(
            "distrakt_open_prompts", _row(watched_at="2026-05-02", created_at=1),
            _row(watched_at="2026-05-01", created_at=9)))
        self.assertTrue(holders.more_complete(
            "distrakt_prompt_dismissals", _row(created_at=9), _row(created_at=1)))
        self.assertTrue(holders.more_complete(
            "distrakt_movie_watches", _row(watched_at="2026-05-02"),
            _row(watched_at="2026-05-01")))

    def test_progress_has_no_clock_so_neither_row_is_more_complete(self):
        self.assertFalse(holders.more_complete("distrakt_show_progress", _row(), _row()))

    def test_a_tie_is_not_a_win(self):
        same = _row(watched=1, total=1, created_at=1)
        self.assertFalse(holders.more_complete("distrakt_user_seasons", same, dict(same)))


class GapsFilledTests(unittest.TestCase):
    def test_only_empty_fields_are_filled_and_never_a_number(self):
        survivor = _row(title="", network="HBO", watched=0, total=0, cadence=None,
                        missing_sources_json="[]")
        other = _row(title="Show", network="AMC", watched=9, total=10, cadence="Mon",
                     missing_sources_json='["simkl"]')
        self.assertEqual(holders.gaps_filled(survivor, other, ()),
                         {"title": "Show", "cadence": "Mon",
                          "missing_sources_json": '["simkl"]'})

    def test_an_id_map_is_merged_with_the_survivors_ids_winning(self):
        filled = holders.gaps_filled(_row(ids_json='{"tmdb": 900, "tvdb": 1}'),
                                     _row(ids_json='{"tvdb": 2, "imdb": "tt9"}'), ())
        self.assertEqual(json.loads(filled["ids_json"]),
                         {"tmdb": 900, "tvdb": 1, "imdb": "tt9"})

    def test_the_columns_that_say_which_row_it_is_are_never_copied(self):
        self.assertEqual(holders.gaps_filled(_row(season=None, board_id=None),
                                             _row(season=2, board_id=4),
                                             ("season", "board_id")), {})
