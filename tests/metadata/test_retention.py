"""app/metadata/rows.py's retention: what the sweep keeps and what a service's own
terms take away.

TWO RULES, AND THE ORDER BETWEEN THEM:
  - A DESCRIPTION OUTLIVES THE CALENDAR WHILE A FEATURE ROW HOLDS ITS TITLE. A
    tracker or board row keeps no copy of a title's words, so the store must keep
    every service's row for a held title however long ago a calendar last listed
    it — its seasons and episodes too, which its details panel reads.
  - A SERVICE'S CEILING OVERRIDES THAT. TMDB's terms forbid keeping what it
    answered for more than six months after it was last obtained, held or not;
    a held title is refreshed long before that by being looked at.
"""
from __future__ import annotations

import asyncio

from app import db, providers
from app.metadata import descriptions, rows
from app.providers.base import Media, Record, Source
from app.providers.tmdb import transport as tmdb_transport
from tests.support import DatabaseTestCase

OLD = 1_000
NOW = OLD + 400 * 24 * 60 * 60
CUTOFF = NOW - 180 * 24 * 60 * 60


async def _describe(source: str, source_id: str, key: str, *, fetched_at: int = OLD) -> None:
    """One service's description of a title — its title row, a season and an
    episode — none of them named by any calendar airing."""
    await db.execute(
        "INSERT INTO title_metadata (source, media, source_id, title_key, title, fetched_at) "
        "VALUES (?, 'show', ?, ?, 'A show', ?)", (source, source_id, key, fetched_at))
    await db.execute(
        "INSERT INTO season_metadata (source, media, source_id, season, fetched_at) "
        "VALUES (?, 'show', ?, 1, ?)", (source, source_id, fetched_at))
    await db.execute(
        "INSERT INTO episode_metadata (source, media, source_id, season, number, title, "
        "fetched_at) VALUES (?, 'show', ?, 1, 1, 'Pilot', ?)", (source, source_id, fetched_at))


async def _counts(source_id: str) -> tuple[int, int, int]:
    return tuple([await db.fetch_value(
        f"SELECT COUNT(*) FROM {table} WHERE source_id = ?", (source_id,))
        for table in (rows.TITLES, rows.SEASONS, rows.EPISODES)])


async def _sweep_unlisted() -> dict:
    return await db.transaction(lambda conn: rows.sweep_unlisted(conn, CUTOFF))


class HeldTitlesKeepTheirDescriptionTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user_id = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('v', 1, 1)")).lastrowid
        self.board_id = (await db.execute(
            "INSERT INTO tier_boards (user_id, uid, created_at, updated_at) "
            "VALUES (?, 'b', 1, 1)", (self.user_id,))).lastrowid

    async def test_a_title_nothing_airs_or_holds_is_swept_whole(self):
        await _describe("trakt", "11", "show:tmdb:11")
        await _sweep_unlisted()
        self.assertEqual(await _counts("11"), (0, 0, 0))

    async def test_a_tracker_row_keeps_every_services_description(self):
        await _describe("trakt", "12", "show:tmdb:12")
        await _describe("simkl", "s12", "show:tmdb:12")
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
            "season, kind, created_at) VALUES (?, 'show', 'tmdb', '12', 1, 'keepup', 1)",
            (self.user_id,))
        await _sweep_unlisted()
        self.assertEqual(await _counts("12"), (1, 1, 1))
        self.assertEqual(await _counts("s12"), (1, 1, 1))

    async def test_a_board_row_and_a_deleted_tiers_season_row_keep_theirs(self):
        await _describe("trakt", "13", "show:tmdb:13")
        await _describe("trakt", "14", "show:tvdb:14")
        await db.execute(
            "INSERT INTO tier_items (board_id, media, match_source, match_id, created_at) "
            "VALUES (?, 'show', 'tmdb', '13', 1)", (self.board_id,))
        await db.execute(
            "INSERT INTO tier_category_removals (board_id, category_uid, sort_order, "
            "removed_at, category_json, items_json) VALUES (?, 't', 0, 1, '{}', "
            "'[\"show:tvdb:14:s2\"]')", (self.board_id,))
        await _sweep_unlisted()
        self.assertEqual(await _counts("13"), (1, 1, 1))
        self.assertEqual(await _counts("14"), (1, 1, 1))

    async def test_a_title_held_only_by_a_bookkeeping_table_is_not_kept(self):
        """A not-watching mark is drawn on a card its own airing draws; it holds
        nothing up on its own."""
        await _describe("trakt", "15", "show:tmdb:15")
        await db.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                         "VALUES (?, 'show:tmdb:15', 1)", (self.user_id,))
        await _sweep_unlisted()
        self.assertEqual(await _counts("15"), (0, 0, 0))

    async def test_a_row_touched_inside_the_window_stays_whoever_holds_it(self):
        await _describe("trakt", "16", "show:tmdb:16", fetched_at=NOW)
        await _sweep_unlisted()
        self.assertEqual(await _counts("16"), (1, 1, 1))


class ServiceCeilingTests(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        user = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('v', 1, 1)")).lastrowid
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
            "season, kind, created_at) VALUES (?, 'show', 'tmdb', '21', 1, 'keepup', 1)",
            (user,))

    async def _sweep(self) -> dict:
        return await db.transaction(lambda conn: rows.sweep_past_ceilings(conn, NOW))

    async def test_tmdbs_rows_go_at_its_ceiling_even_while_held(self):
        await _describe("tmdb", "21", "show:tmdb:21", fetched_at=NOW - tmdb_transport.MAX_CACHE_SECONDS)
        removed = await self._sweep()
        self.assertEqual(await _counts("21"), (0, 0, 0))
        self.assertEqual(removed["tmdb"], 3)

    async def test_a_tmdb_row_re_obtained_inside_the_ceiling_stays(self):
        await _describe("tmdb", "21", "show:tmdb:21",
                        fetched_at=NOW - tmdb_transport.MAX_CACHE_SECONDS + 1)
        await self._sweep()
        self.assertEqual(await _counts("21"), (1, 1, 1))

    async def test_a_listing_stub_lives_as_long_as_its_title_row(self):
        await _describe("tmdb", "22", "show:tmdb:22", fetched_at=NOW)
        await db.execute("UPDATE episode_metadata SET fetched_at = 0 WHERE source_id = '22'")
        await self._sweep()
        self.assertEqual(await _counts("22"), (1, 1, 1))
        await db.execute("UPDATE title_metadata SET fetched_at = ? WHERE source_id = '22'",
                         (OLD,))
        await self._sweep()
        self.assertEqual(await _counts("22"), (0, 1, 0))

    async def test_a_service_with_no_ceiling_is_never_swept_by_one(self):
        await _describe("trakt", "21", "show:tmdb:21", fetched_at=OLD)
        removed = await self._sweep()
        self.assertEqual(await _counts("21"), (1, 1, 1))
        self.assertNotIn("trakt", removed)


class EveryServiceStatesItsCeilingTests(DatabaseTestCase):
    def test_tmdbs_ceiling_is_the_one_its_client_declares(self):
        self.assertEqual(providers.get(Source.TMDB).capabilities.retain_seconds,
                         tmdb_transport.MAX_CACHE_SECONDS)

    def test_every_registered_source_states_one_or_none(self):
        for source, provider in providers.registered().items():
            with self.subTest(source=source):
                value = provider.capabilities.retain_seconds
                self.assertTrue(value is None or (isinstance(value, int) and value > 0))


def _tmdb_record(title: str = "A show") -> Record:
    return Record(source=Source.TMDB, media=Media.SHOW, id="31", ids={"tmdb": 31},
                  detail_url="", title=title, air_ts=float(NOW), season=1, episode_number=1,
                  episode_label="S01E01", episode_title="Pilot")


class OneClockForEveryWordTests(DatabaseTestCase):
    """A ceiling reads ONE clock per row, so for a service with a ceiling that
    clock is the age of the oldest word the row holds: a write renews it only
    when it rewrites every word there (rows.ceiling_clock)."""

    def test_every_column_is_a_word_or_says_why_not(self):
        for table in (rows.TITLES, rows.SEASONS, rows.EPISODES):
            with self.subTest(table=table):
                columns = {row[1] for row in asyncio.run(
                    db.fetch_all(f"PRAGMA table_info({table})"))}
                words, others = set(rows.WORDS[table]), set(rows.NOT_WORDS[table])
                self.assertFalse(words & others)
                self.assertEqual(columns, words | others)

    async def _season(self, source: str) -> int:
        return await db.fetch_value(
            "SELECT fetched_at FROM season_metadata WHERE source = ? AND season = 1", (source,))

    async def _list(self, source: str, now: int) -> None:
        await db.transaction(lambda conn: descriptions.write_season_list(
            conn, source, "show", "31", [{"season": 1, "episode_count": 8, "name": "One"}],
            now=now, ids={source: 31}))

    async def test_a_season_list_over_a_described_season_keeps_the_descriptions_clock(self):
        await self._list("tmdb", OLD)
        await db.execute("UPDATE season_metadata SET overview = 'What happens.'")
        await self._list("tmdb", NOW)
        self.assertEqual(await self._season("tmdb"), OLD)

    async def test_a_season_list_over_a_season_only_listed_renews_it(self):
        await self._list("tmdb", OLD)
        await self._list("tmdb", NOW)
        self.assertEqual(await self._season("tmdb"), NOW)

    async def test_a_service_with_no_ceiling_is_given_the_moment_it_answered(self):
        await self._list("trakt", OLD)
        await db.execute("UPDATE season_metadata SET overview = 'What happens.'")
        await self._list("trakt", NOW)
        self.assertEqual(await self._season("trakt"), NOW)

    async def test_a_calendar_listing_over_a_described_title_keeps_its_clock(self):
        await db.transaction(lambda conn: rows.write_listing(
            conn, _tmdb_record(), "show:tmdb:31", now=OLD, stale_after=OLD))
        await db.execute("UPDATE title_metadata SET season_count = 2, cast_json = '[\"x\"]'")
        await db.transaction(lambda conn: rows.write_listing(
            conn, _tmdb_record("Renamed"), "show:tmdb:31", now=NOW, stale_after=NOW))
        row = await db.fetch_one("SELECT title, fetched_at FROM title_metadata")
        self.assertEqual((row["title"], row["fetched_at"]), ("Renamed", OLD))

    async def test_a_listing_alone_renews_what_it_wrote(self):
        await db.transaction(lambda conn: rows.write_listing(
            conn, _tmdb_record(), "show:tmdb:31", now=OLD, stale_after=OLD))
        await db.transaction(lambda conn: rows.write_listing(
            conn, _tmdb_record(), "show:tmdb:31", now=NOW, stale_after=NOW))
        self.assertEqual(await db.fetch_value("SELECT fetched_at FROM title_metadata"), NOW)

    def test_a_description_that_leaves_a_word_keeps_the_older_clock(self):
        row = {"fetched_at": OLD, **dict.fromkeys(rows.WORDS[rows.TITLES], ""),
               "cast_json": '["x"]'}
        everything_but_cast = set(rows.WORDS[rows.TITLES]) - {"cast_json"}
        self.assertEqual(rows.kept_clock("tmdb", rows.TITLES, row, everything_but_cast, NOW), OLD)
        self.assertEqual(rows.kept_clock("tmdb", rows.TITLES, row, rows.WORDS[rows.TITLES], NOW),
                         NOW)
        self.assertEqual(rows.kept_clock("trakt", rows.TITLES, row, everything_but_cast, NOW),
                         NOW)

    def test_a_stub_no_answer_reached_takes_the_answers_clock(self):
        row = {"fetched_at": 0, **dict.fromkeys(rows.WORDS[rows.EPISODES], ""), "title": "Pilot"}
        self.assertEqual(rows.kept_clock("tmdb", rows.EPISODES, row, (), NOW), NOW)


class TheStoreFilesThePicturesItLearnsTests(DatabaseTestCase):
    """A picture a service states reaches the poster registry as the store writes
    it (artwork.file_sighting): a listing of a title with no tmdb id under its
    key, and a description under the title's tmdb id, else its key."""

    async def test_a_listing_of_a_title_with_no_tmdb_id_is_filed_under_its_key(self):
        record = Record(source=Source.SIMKL, media=Media.SHOW, id="77", ids={"simkl": 77},
                        detail_url="", title="Anime", air_ts=float(NOW),
                        poster="https://simkl.in/p.jpg")
        await db.transaction(lambda conn: rows.write_listing(
            conn, record, "simkl:77", now=NOW, stale_after=NOW))
        self.assertEqual([tuple(r) for r in await db.fetch_all(
            "SELECT media, title_key, source, url FROM title_posters")],
            [("show", "simkl:77", "simkl", "https://simkl.in/p.jpg")])

    async def test_a_description_is_filed_under_the_titles_tmdb_id(self):
        from zoneinfo import ZoneInfo
        from tests.metadata.test_panel import _answer
        answer = _answer("trakt", "9", None, poster="https://media.trakt.tv/new.jpg")
        answer["ids"] = {"trakt": 9, "tmdb": 31}
        await db.transaction(lambda conn: descriptions.write(
            conn, "trakt", "show", "9", answer, asked_season=None, tz=ZoneInfo("UTC"),
            now=NOW))
        self.assertEqual([tuple(r) for r in await db.fetch_all(
            "SELECT media, tmdb, source, url FROM show_posters")],
            [("show", 31, "trakt", "https://media.trakt.tv/new.jpg")])
