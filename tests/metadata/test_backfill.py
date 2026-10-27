"""app/metadata/backfill.py — registering, once, every title already stored.

WHAT IS PINNED: every title a service's rows, a tracker row or a board row names
gets an identity; a stored row whose own ids have outgrown its key is re-keyed,
and its airings and marks move with it (the 1,256 titles measured on the
development copy before this existed); a tvdb-only listing joins the tmdb title
another service's row states that tvdb id for; and the pass runs once.
"""
from __future__ import annotations

import json

from app import db
from app.metadata import backfill
from tests.support import DatabaseTestCase


async def _title_row(source, source_id, key, ids, title="A Show"):
    await db.execute(
        "INSERT INTO title_metadata (source, media, source_id, title_key, title, ids_json, "
        "fetched_at) VALUES (?, 'show', ?, ?, ?, ?, 1)",
        (source, source_id, key, title, json.dumps(ids)))
    await db.execute(
        "INSERT INTO calendar_airings (source, media, source_id, endpoint, title_key, air_ts, "
        "air_date, season, episode_number, stored_at) VALUES (?, 'show', ?, 'shows', ?, "
        "1783540800, '2026-07-08', 1, 1, 1)", (source, source_id, key))


class RegisteringWhatIsStoredTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db.now()
        self.user_id = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('v', ?, ?)",
            (now, now))).lastrowid

    async def test_a_row_whose_ids_outgrew_its_key_is_re_keyed_with_what_holds_it(self):
        """Enrichment taught a Simkl row its tvdb id after it was filed under
        Simkl's own id; nothing moved the key."""
        await _title_row("simkl", "55", "simkl:55", {"simkl": 55, "tvdb": 4321})
        await db.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                         "VALUES (?, 'simkl:55', 1)", (self.user_id,))
        self.assertEqual(await backfill.register_everything(), 1)
        for sql in ("SELECT title_key FROM title_metadata",
                    "SELECT title_key FROM calendar_airings",
                    "SELECT item_id FROM not_watching_shows"):
            self.assertEqual(await db.fetch_value(sql), "show:tvdb:4321", sql)
        self.assertEqual(await db.fetch_value(
            "SELECT title_key FROM title_aliases WHERE old_key = 'simkl:55'"), "show:tvdb:4321")

    async def test_a_tvdb_listing_joins_the_title_another_service_states_tmdb_for(self):
        """The cross-service case: two cards for one show until now."""
        await _title_row("simkl", "55", "show:tvdb:4321", {"simkl": 55, "tvdb": 4321})
        await _title_row("trakt", "the-show", "show:tmdb:900",
                         {"trakt": 7, "tmdb": 900, "tvdb": 4321})
        await backfill.register_everything()
        self.assertEqual(
            {r["title_key"] for r in await db.fetch_all("SELECT title_key FROM calendar_airings")},
            {"show:tmdb:900"})
        ids = json.loads(await db.fetch_value(
            "SELECT ids_json FROM titles WHERE title_key = 'show:tmdb:900'"))
        self.assertEqual((ids["simkl"], ids["trakt"], ids["tvdb"]), (55, 7, 4321))

    async def test_tracker_and_board_titles_are_registered_too(self):
        now = db.now()
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, season, "
            "kind, tmdb, trakt_id, created_at) VALUES (?, 'show', 'tmdb', '900', 1, 'keepup', "
            "900, 7, ?)", (self.user_id, now))
        board = (await db.execute("INSERT INTO tier_boards (user_id, uid, created_at, "
                                  "updated_at) VALUES (?, 'b', ?, ?)",
                                  (self.user_id, now, now))).lastrowid
        await db.execute("INSERT INTO tier_items (board_id, media, match_source, match_id, "
                         "ids_json, created_at) VALUES (?, 'movie', 'tmdb', '550', "
                         "'{\"tmdb\": 550, \"imdb\": \"tt0137523\"}', ?)", (board, now))
        await backfill.register_everything()
        known = {r["title_key"]: json.loads(r["ids_json"]) for r in
                 await db.fetch_all("SELECT title_key, ids_json FROM titles")}
        self.assertEqual(known["show:tmdb:900"], {"tmdb": 900, "trakt": 7})
        self.assertEqual(known["movie:tmdb:550"], {"tmdb": 550, "imdb": "tt0137523"})

    async def test_it_runs_once(self):
        await _title_row("simkl", "55", "simkl:55", {"simkl": 55, "tvdb": 4321})
        self.assertEqual(await backfill.run_once(), 1)
        self.assertEqual(await backfill.run_once(), 0)
        self.assertEqual(await db.get_meta(backfill.MARKER), "1")

    async def test_settling_everything_twice_changes_nothing_the_second_time(self):
        await _title_row("simkl", "55", "simkl:55", {"simkl": 55, "tvdb": 4321})
        await backfill.register_everything()
        before = [tuple(r) for r in await db.fetch_all(
            "SELECT title_key, ids_json FROM titles ORDER BY title_key")]
        await backfill.register_everything()
        after = [tuple(r) for r in await db.fetch_all(
            "SELECT title_key, ids_json FROM titles ORDER BY title_key")]
        self.assertEqual(before, after)


class SeedingWhatHeldRowsCarriedTests(DatabaseTestCase):
    """Every word a tracker or board row carries a copy of reaches the store where
    no service states it — once, after registration, and only while the copies
    exist — so no row draws a blank once its readers read the store."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        now = db.now()
        self.user_id = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('v', ?, ?)",
            (now, now))).lastrowid
        self.board_id = (await db.execute(
            "INSERT INTO tier_boards (user_id, uid, created_at, updated_at) VALUES (?, 'b', 1, 1)",
            (self.user_id,))).lastrowid
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, season, "
            "kind, title, network, tmdb, created_at) "
            "VALUES (?, 'show', 'tmdb', '11', 1, 'keepup', 'Tracked', 'HBO', 11, 1)",
            (self.user_id,))
        await db.execute(
            "INSERT INTO tier_items (board_id, media, match_source, match_id, season, tmdb, "
            "ids_json, title, network, year, runtime, season_name, episode_count, created_at) "
            "VALUES (?, 'show', 'tmdb', '12', 3, 12, '{}', 'Ranked', 'FX', '2021', 50, "
            "'The Return', 8, 1)", (self.board_id,))
        await db.set_meta(backfill.MARKER, "1")

    async def words(self, key: str) -> tuple:
        row = await db.fetch_one(
            "SELECT title, network, year, runtime FROM title_metadata WHERE title_key = ?", (key,))
        return tuple(row) if row is not None else None

    async def test_both_rows_give_their_words_and_a_season_its_own(self):
        self.assertEqual(await backfill.seed_words_once(), 2)
        self.assertEqual(await self.words("show:tmdb:11"), ("Tracked", "HBO", "", None))
        self.assertEqual(await self.words("show:tmdb:12"), ("Ranked", "FX", "2021", 50))
        season = await db.fetch_one("SELECT name, episode_count FROM season_metadata "
                                    "WHERE source_id = '12' AND season = 3")
        self.assertEqual(tuple(season), ("The Return", 8))
        self.assertEqual(await backfill.seed_words_once(), 0, "once")

    async def test_a_word_a_service_states_is_kept(self):
        await db.execute(
            "INSERT INTO title_metadata (source, media, source_id, title_key, title, ids_json, "
            "fetched_at) VALUES ('trakt', 'show', 'x', 'show:tmdb:11', 'The Service''s', "
            "'{\"trakt\": 5, \"tmdb\": 11}', 1)")
        await backfill.seed_words_once()
        # Word by word: the name a service states stands, and the network nobody
        # stated is seeded.
        rows = {r["source"]: (r["title"], r["network"]) for r in await db.fetch_all(
            "SELECT source, title, network FROM title_metadata WHERE title_key = 'show:tmdb:11'")}
        self.assertEqual(rows, {"trakt": ("The Service's", ""), "tmdb": ("", "HBO")})

    async def test_it_waits_for_registration(self):
        await db.set_meta(backfill.MARKER, "0")
        self.assertEqual(await backfill.seed_words_once(), 0)
        self.assertIsNone(await self.words("show:tmdb:11"))

    async def test_a_table_whose_copies_are_gone_gives_nothing(self):
        for column in ("title", "network"):
            await db.execute(f"ALTER TABLE distrakt_user_seasons DROP COLUMN {column}")
        self.assertEqual(await backfill.seed_held_words(), 1)
        self.assertIsNone(await self.words("show:tmdb:11"))
