"""app/metadata/rows.py's retention: what the sweep keeps and what a service's own
terms take away.

TWO RULES, AND THE ORDER BETWEEN THEM:
  - A DESCRIPTION OUTLIVES THE CALENDAR WHILE A FEATURE ROW HOLDS ITS TITLE. A
    tracker or board row keeps no copy of a title's words, so the store must keep
    every service's group for a held title however long ago a calendar last
    listed it — its seasons and episodes too, which its details panel reads.
  - A SERVICE'S CEILING OVERRIDES THAT. TMDB's terms forbid keeping what it
    answered for more than six months after it was last obtained, held or not;
    a held title is refreshed long before that by being looked at. The ceiling
    CLEARS the service's group and leaves the row: the title, and the other
    services' words on it, are not TMDB's.
"""
from __future__ import annotations

import asyncio

from app import db, providers
from app.metadata import descriptions, groups, rows, tables
from app.providers.base import Media, Record, Source
from app.providers.tmdb import transport as tmdb_transport
from tests.support import (DatabaseTestCase, episode_groups, put_episode, put_season,
                           put_title, season_groups, title_groups)

OLD = 1_000
NOW = OLD + 400 * 24 * 60 * 60
CUTOFF = NOW - 180 * 24 * 60 * 60


async def _describe(source: str, source_id: str, key: str, *, fetched_at: int = OLD) -> None:
    """One service's description of a title — its title group, a season and an
    episode — none of them listed on any calendar."""
    def _write(conn):
        put_title(conn, source, "show", source_id, key, title="A show", fetched_at=fetched_at)
        put_season(conn, source, key, 1, fetched_at=fetched_at)
        put_episode(conn, source, key, 1, 1, title="Pilot", fetched_at=fetched_at)

    await db.transaction(_write)


async def _counts(source: str, key: str) -> tuple[int, int, int]:
    """How many of the service's title, season and episode groups the title
    still has."""
    return (sum(g["title_key"] == key for g in await title_groups(source)),
            sum(g["title_key"] == key for g in await season_groups(source)),
            sum(g["title_key"] == key for g in await episode_groups(source)))


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

    async def test_a_title_nothing_lists_or_holds_is_swept_whole(self):
        await _describe("trakt", "11", "show:tmdb:11")
        await _sweep_unlisted()
        self.assertEqual(await _counts("trakt", "show:tmdb:11"), (0, 0, 0))
        self.assertEqual(await db.fetch_value(
            f"SELECT COUNT(*) FROM {tables.SERVICE_IDS} WHERE title_key = 'show:tmdb:11'"), 0)

    async def test_a_tracker_row_keeps_every_services_description(self):
        await _describe("trakt", "12", "show:tmdb:12")
        await _describe("simkl", "512", "show:tmdb:12")
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
            "season, kind, created_at) VALUES (?, 'show', 'tmdb', '12', 1, 'keepup', 1)",
            (self.user_id,))
        await _sweep_unlisted()
        self.assertEqual(await _counts("trakt", "show:tmdb:12"), (1, 1, 1))
        self.assertEqual(await _counts("simkl", "show:tmdb:12"), (1, 1, 1))

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
        self.assertEqual(await _counts("trakt", "show:tmdb:13"), (1, 1, 1))
        self.assertEqual(await _counts("trakt", "show:tvdb:14"), (1, 1, 1))

    async def test_a_title_held_only_by_a_bookkeeping_table_is_not_kept(self):
        """A not-watching mark is drawn on a card its own listing draws; it holds
        nothing up on its own."""
        await _describe("trakt", "15", "show:tmdb:15")
        await db.execute("INSERT INTO not_watching_shows (user_id, item_id, created_at) "
                         "VALUES (?, 'show:tmdb:15', 1)", (self.user_id,))
        await _sweep_unlisted()
        self.assertEqual(await _counts("trakt", "show:tmdb:15"), (0, 0, 0))

    async def test_a_group_touched_inside_the_window_stays_whoever_holds_it(self):
        await _describe("trakt", "16", "show:tmdb:16", fetched_at=NOW)
        await _sweep_unlisted()
        self.assertEqual(await _counts("trakt", "show:tmdb:16"), (1, 1, 1))

    async def test_a_title_keeps_its_identity_when_every_group_goes(self):
        """The row is the title's identity — its key and every id known — and a
        retention sweep only ever clears what a service said."""
        await _describe("trakt", "17", "show:tmdb:17")
        await _sweep_unlisted()
        self.assertEqual(await db.fetch_value(
            f"SELECT COUNT(*) FROM {tables.TITLES} WHERE title_key = 'show:tmdb:17'"), 1)


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

    async def test_tmdbs_groups_go_at_its_ceiling_even_while_held(self):
        await _describe("tmdb", "21", "show:tmdb:21", fetched_at=NOW - tmdb_transport.MAX_CACHE_SECONDS)
        removed = await self._sweep()
        self.assertEqual(await _counts("tmdb", "show:tmdb:21"), (0, 0, 0))
        self.assertEqual(removed["tmdb"], 3)

    async def test_the_ceiling_clears_tmdbs_group_and_leaves_the_others(self):
        await _describe("tmdb", "21", "show:tmdb:21", fetched_at=NOW - tmdb_transport.MAX_CACHE_SECONDS)
        await _describe("trakt", "21", "show:tmdb:21", fetched_at=OLD)
        await self._sweep()
        self.assertEqual(await _counts("trakt", "show:tmdb:21"), (1, 1, 1))
        self.assertEqual(await db.fetch_value(
            f"SELECT trakt_title FROM {tables.TITLES} WHERE title_key = 'show:tmdb:21'"),
            "A show")

    async def test_a_tmdb_group_re_obtained_inside_the_ceiling_stays(self):
        await _describe("tmdb", "21", "show:tmdb:21",
                        fetched_at=NOW - tmdb_transport.MAX_CACHE_SECONDS + 1)
        await self._sweep()
        self.assertEqual(await _counts("tmdb", "show:tmdb:21"), (1, 1, 1))

    async def test_a_listing_stub_lives_as_long_as_its_title_group(self):
        await _describe("tmdb", "22", "show:tmdb:22", fetched_at=NOW)
        await db.execute(f"UPDATE {tables.EPISODES} SET tmdb_fetched_at = 0 "
                         "WHERE title_key = 'show:tmdb:22'")
        await self._sweep()
        self.assertEqual(await _counts("tmdb", "show:tmdb:22"), (1, 1, 1))
        await db.execute(f"UPDATE {tables.TITLES} SET tmdb_fetched_at = ? "
                         "WHERE title_key = 'show:tmdb:22'", (OLD,))
        await self._sweep()
        self.assertEqual(await _counts("tmdb", "show:tmdb:22"), (0, 1, 0))

    async def test_a_service_with_no_ceiling_is_never_swept_by_one(self):
        await _describe("trakt", "21", "show:tmdb:21", fetched_at=OLD)
        removed = await self._sweep()
        self.assertEqual(await _counts("trakt", "show:tmdb:21"), (1, 1, 1))
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


async def _write_listing(record: Record, key: str, *, now: int) -> None:
    """One listing's title group written under `key`, as a fill writes it."""
    spelled = rows.listing_rows([record], now=now, stale_after=now)
    await db.transaction(lambda conn: rows.write_listings(conn, spelled, [key], now=now))


class OneClockForEveryWordTests(DatabaseTestCase):
    """A ceiling reads ONE clock per group, so for a service with a ceiling that
    clock is the age of the oldest word the group holds: a write renews it only
    when it rewrites every word there (rows.ceiling_clock)."""

    def test_every_column_is_a_word_or_says_why_not(self):
        """Every column of the three tables is a column of the row's identity,
        or one of a service's group that the words or the not-words name — a
        word nobody listed is a word `ceiling_clock` cannot see left behind."""
        for table in (tables.TITLES, tables.SEASONS, tables.EPISODES):
            with self.subTest(table=table):
                columns = {row[1] for row in asyncio.run(
                    db.fetch_all(f"PRAGMA table_info({table})"))}
                words, others = set(groups.WORDS[table]), set(groups.NOT_WORDS[table])
                self.assertFalse(words & others)
                named = set(groups.IDENTITY[table]) | {
                    f"{source}_{name}" for source in groups.sources()
                    for name in words | others}
                self.assertEqual(columns - named, set())
                for source in groups.sources():
                    self.assertEqual(groups.names(table, source) - words - others, set(),
                                     f"{source} declares a column neither list names")

    async def _season(self, source: str) -> int:
        return await db.fetch_value(
            f"SELECT {source}_fetched_at FROM {tables.SEASONS} WHERE season = 1")

    async def _list(self, source: str, now: int) -> None:
        await db.transaction(lambda conn: descriptions.write_season_list(
            conn, source, "show", "31", [{"season": 1, "episode_count": 8, "name": "One"}],
            now=now, ids={source: 31}))

    async def test_a_season_list_over_a_described_season_keeps_the_descriptions_clock(self):
        await self._list("tmdb", OLD)
        await db.execute(f"UPDATE {tables.SEASONS} SET tmdb_overview = 'What happens.'")
        await self._list("tmdb", NOW)
        self.assertEqual(await self._season("tmdb"), OLD)

    async def test_a_season_list_over_a_season_only_listed_renews_it(self):
        await self._list("tmdb", OLD)
        await self._list("tmdb", NOW)
        self.assertEqual(await self._season("tmdb"), NOW)

    async def test_a_service_with_no_ceiling_is_given_the_moment_it_answered(self):
        await self._list("trakt", OLD)
        await db.execute(f"UPDATE {tables.SEASONS} SET trakt_overview = 'What happens.'")
        await self._list("trakt", NOW)
        self.assertEqual(await self._season("trakt"), NOW)

    async def test_a_calendar_listing_over_a_described_title_keeps_its_clock(self):
        await _write_listing(_tmdb_record(), "show:tmdb:31", now=OLD)
        await db.execute(f"UPDATE {tables.TITLES} SET tmdb_season_count = 2, "
                         "tmdb_cast_json = '[\"x\"]'")
        await _write_listing(_tmdb_record("Renamed"), "show:tmdb:31", now=NOW)
        row = await db.fetch_one(f"SELECT tmdb_title, tmdb_fetched_at FROM {tables.TITLES}")
        self.assertEqual((row["tmdb_title"], row["tmdb_fetched_at"]), ("Renamed", OLD))

    async def test_a_listing_alone_renews_what_it_wrote(self):
        await _write_listing(_tmdb_record(), "show:tmdb:31", now=OLD)
        await _write_listing(_tmdb_record(), "show:tmdb:31", now=NOW)
        self.assertEqual(await db.fetch_value(f"SELECT tmdb_fetched_at FROM {tables.TITLES}"),
                         NOW)

    def test_a_description_that_leaves_a_word_keeps_the_older_clock(self):
        words = groups.WORDS[tables.TITLES]
        row = {"fetched_at": OLD, **dict.fromkeys(words, ""), "cast_json": '["x"]'}
        everything_but_cast = set(words) - {"cast_json"}
        self.assertEqual(rows.kept_clock("tmdb", tables.TITLES, row, everything_but_cast, NOW),
                         OLD)
        self.assertEqual(rows.kept_clock("tmdb", tables.TITLES, row, words, NOW), NOW)
        self.assertEqual(rows.kept_clock("trakt", tables.TITLES, row, everything_but_cast, NOW),
                         NOW)

    def test_a_stub_no_answer_reached_takes_the_answers_clock(self):
        row = {"fetched_at": 0, **dict.fromkeys(groups.WORDS[tables.EPISODES], ""),
               "title": "Pilot"}
        self.assertEqual(rows.kept_clock("tmdb", tables.EPISODES, row, (), NOW), NOW)


class TheStoreFilesThePicturesItLearnsTests(DatabaseTestCase):
    """A picture a service states reaches the poster registry as the store writes
    it (artwork.file_sighting): a listing of a title with no tmdb id under its
    key, and a description under the title's tmdb id, else its key."""

    async def test_a_listing_of_a_title_with_no_tmdb_id_is_filed_under_its_key(self):
        record = Record(source=Source.SIMKL, media=Media.SHOW, id="77", ids={"simkl": 77},
                        detail_url="", title="Anime", air_ts=float(NOW),
                        poster="https://simkl.in/p.jpg")
        await _write_listing(record, "simkl:77", now=NOW)
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
