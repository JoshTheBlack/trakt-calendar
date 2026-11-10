"""The title store's writers run a fixed number of statements, however much they
write — a description's episodes, a pass's enrichment answers, the rows a view
queues.

A write transaction holds the database's one write lock for as long as its
statements take, and while the event loop is busy each statement can wait a GIL
switch interval to go on. Each test writes a small batch and one ten times its
size and requires the same count.
"""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from app import db
from app.calendar import entries
from app.metadata import descriptions, refresh
from app.metadata import rows as title_rows
from app.providers.base import Media, Record, SeasonsAnswer, Source, SourceUnavailable
from tests.metadata.test_panel import _answer
from tests.support import DatabaseTestCase, episode_groups, listings, season_groups, statements_per_transaction, title_groups

DAY = 1783540800.0  # 2026-07-08 20:00Z
WEEK = (date(2026, 7, 6), date(2026, 7, 13))


class ADescriptionsStatementsDoNotGrowWithItsSeasonTests(DatabaseTestCase):

    async def _describe(self, size: int, own_id: int) -> list[int]:
        answer = {**_answer("tmdb", str(own_id), 1), "episodes": [
            {"number": n, "title": f"Episode {n}", "first_aired": f"2026-07-{n:02d}",
             "rating": None, "overview": "", "episode_type": "", "runtime": None}
            for n in range(1, size + 1)]}
        seasons = [{"season": n, "episode_count": 10, "name": f"Season {n}"}
                   for n in range(1, size + 1)]
        with statements_per_transaction() as counted:
            await descriptions.store("tmdb", "show", str(own_id), answer, asked_season=1,
                                     tz=ZoneInfo("UTC"), seasons=seasons)
        return counted

    async def test_thirty_episodes_and_seasons_cost_what_three_do(self):
        self.assertEqual(await self._describe(3, 31), await self._describe(30, 32))
        self.assertEqual(sum(g["title_key"].endswith(":32")
                             for g in await episode_groups()), 30)
        self.assertEqual(sum(g["title_key"].endswith(":32")
                             for g in await season_groups()), 30)


class APassIsCutIntoTurnsTests(DatabaseTestCase):
    """descriptions.turns: in the order given, at most TURN_ANSWERS answers a
    turn and as many as keep its rows under TURN_ROWS."""

    @staticmethod
    def _answer(episodes: int) -> descriptions.Answer:
        return descriptions.Answer("tmdb", "show", "1", {"episodes": [{}] * episodes}, 1)

    def test_answers_and_rows_both_bound_a_turn(self):
        with (mock.patch.object(descriptions, "TURN_ANSWERS", 3),
              mock.patch.object(descriptions, "TURN_ROWS", 20)):
            sizes = [len(turn) for turn in descriptions.turns(
                [self._answer(1) for _ in range(7)])]
            self.assertEqual(sizes, [3, 3, 1])
            heavy = descriptions.turns([self._answer(8), self._answer(8), self._answer(30),
                                        self._answer(1)])
        self.assertEqual([[len(a.answer["episodes"]) for a in turn] for turn in heavy],
                         [[8, 8], [30], [1]])

    def test_nothing_is_no_turn(self):
        self.assertEqual(descriptions.turns([]), [])


class AnEnrichmentPassesStatementsDoNotGrowWithItsAnswersTests(DatabaseTestCase):

    async def _listed(self, ids: range) -> None:
        now = db.now()
        await entries.store_span("shows", *WEEK, [
            Record(source=Source.SIMKL, media=Media.SHOW, id=f"slug-{n}", ids={"simkl": n},
                   detail_url="", title=f"Anime {n}", air_ts=DAY, enriched=False)
            for n in ids], sources=["simkl"], asked=["simkl"], now=now, stale_after=now + 86400)

    async def _answer(self, ids: range) -> list[int]:
        with statements_per_transaction() as counted:
            await title_rows.apply_enrichment_many(
                "simkl", [(n, "show", {"network": "Tokyo MX"}) for n in ids], db.now())
        return counted

    async def test_thirty_answers_cost_what_three_do(self):
        await self._listed(range(1, 34))
        self.assertEqual(await self._answer(range(1, 4)), await self._answer(range(4, 34)))
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM titles WHERE simkl_enriched = 1 "
            "AND simkl_network = 'Tokyo MX'"),
            33)

    async def test_a_pass_is_written_in_bounded_turns(self):
        await self._listed(range(1, 11))
        with mock.patch.object(title_rows, "ENRICHMENT_TURN", 4):
            counted = await self._answer(range(1, 11))
            one = await self._answer(range(1, 2))
        self.assertEqual(len(counted), 3)
        self.assertEqual(set(counted), set(one))
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM titles WHERE simkl_enriched = 1"), 10)

    async def test_an_answer_still_teaches_its_title_the_ids_it_learned(self):
        """Its rows found by one read before the write, the answer is written by
        rowid — and the title is re-keyed by what it learned, in the same write."""
        await self._listed(range(1, 2))
        await title_rows.apply_enrichment_many(
            "simkl", [(1, "show", {"ids": {"simkl": 1, "tvdb": 4321}})], db.now())
        self.assertEqual([listed["title_key"] for listed in await listings()],
                         ["show:tvdb:4321"])

    async def test_every_turn_teaches_its_titles_the_ids_they_learned(self):
        """The settle is planned before each turn's lock from the ids the turn's
        update leaves — every title of every turn re-keyed, the ids it already
        held kept beside the ones it learned."""
        await self._listed(range(1, 6))
        with mock.patch.object(title_rows, "ENRICHMENT_TURN", 2):
            await title_rows.apply_enrichment_many(
                "simkl", [(n, "show", {"ids": {"simkl": n, "tvdb": 4320 + n}})
                          for n in range(1, 6)], db.now())
        self.assertEqual(sorted(listed["title_key"] for listed in await listings()),
                         [f"show:tvdb:{4320 + n}" for n in range(1, 6)])
        held = json.loads(await db.fetch_value(
            "SELECT simkl_ids_json FROM titles WHERE title_key = 'show:tvdb:4323'"))
        self.assertEqual(held, {"simkl": 3, "tvdb": 4323})

    async def test_a_row_whose_id_moved_after_it_was_found_is_left_alone(self):
        await self._listed(range(1, 2))
        real = db.fetch_value

        async def found_then_moved(sql, params=(), default=None):
            answer = await real(sql, params, default)
            # The group became another of Simkl's entries in between.
            await db.execute("UPDATE titles SET simkl_id = '99', "
                             "simkl_ids_json = '{\"simkl\": 99}'")
            return answer

        title_rows.db.fetch_value = found_then_moved
        try:
            await title_rows.apply_enrichment_many(
                "simkl", [(1, "show", {"network": "Tokyo MX"})], db.now())
        finally:
            title_rows.db.fetch_value = real
        self.assertEqual(await db.fetch_value("SELECT simkl_enriched FROM titles"), 0)


class _Port:
    """TMDB's detail port as a refresh pass meets it: every third title cannot be
    asked, and every show answers its season list and the season named."""

    def catalogue_configured(self, settings):
        return True

    async def fetch_details(self, settings, media, source_id, season, *, cache_only=False,
                            fresh=False):
        if int(source_id) % 3 == 0:
            raise SourceUnavailable("down")
        return {**_answer("tmdb", str(source_id), season),
                "episodes": [] if season is None else [
                    {"number": n, "title": f"E{n}", "first_aired": f"2026-07-0{n}",
                     "rating": None, "overview": "", "episode_type": "", "runtime": None}
                    for n in (1, 2)]}

    async def fetch_seasons(self, settings, source_id, media, *, cached_only=False, fresh=False):
        return SeasonsAnswer(seasons=[{"season": 1, "episode_count": 2},
                                      {"season": 2, "episode_count": 6}],
                             named_season=None, ids={}, network="")


class ARefreshPassesStatementsDoNotGrowWithItsAnswersTests(DatabaseTestCase):
    """A pass asks every row it takes, then files its answers in bounded turns
    (descriptions.turns), each a transaction of a fixed number of statements, and
    takes its entries off the queue — marking the rows of those that failed — in
    one more (refresh.drain)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for patch in (mock.patch.object(refresh, "_askable", lambda settings: [("tmdb", _Port())]),
                      mock.patch.object(refresh, "_asked", {})):
            patch.start()
            self.addCleanup(patch.stop)

    async def _pass(self, ids: range, *, turn: int = 1_000) -> list[int]:
        for n in ids:
            # Described already, so a failure has rows to mark.
            await descriptions.store("tmdb", "show", str(n), _answer("tmdb", str(n), 1),
                                     asked_season=1, tz=ZoneInfo("UTC"))
        await db.execute(
            "INSERT INTO metadata_refresh (source, media, source_id, season, queued_at, reason) "
            "SELECT 'tmdb', 'show', json_extract(value, '$[0]'), json_extract(value, '$[1]'), "
            "1, 'viewed' FROM json_each(?)",
            (json.dumps([[str(n), season] for n in ids for season in (refresh.TITLE, 1)]),))
        with (mock.patch.object(descriptions, "TURN_ANSWERS", turn),
              mock.patch.object(descriptions, "TURN_ROWS", 1_000_000),
              statements_per_transaction() as counted):
            refreshed = await refresh.drain(SimpleNamespace(timezone="UTC"), now=2)
        self.assertEqual(refreshed, 2 * sum(1 for n in ids if n % 3))
        return counted

    async def _settled(self) -> None:
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM metadata_refresh"), 0)
        self.assertEqual(sum(g["fail_count"] == 1 for g in await title_groups()), 11)
        self.assertEqual(sum(g["season"] == 1 and g["fail_count"] == 1
                             for g in await season_groups()), 11)
        self.assertEqual(sum(g["season"] == 2 for g in await season_groups()), 22)

    async def test_a_turn_of_thirty_costs_what_a_turn_of_three_does(self):
        three, thirty = await self._pass(range(1, 4)), await self._pass(range(4, 34))
        self.assertEqual(len(three), 2, "one write for the answers, one for the entries")
        self.assertEqual(three, thirty)
        await self._settled()

    async def test_a_pass_is_filed_in_bounded_turns(self):
        """Forty answers, four to a turn: ten turns, each no more statements than
        a turn holding every kind of answer, then the entries."""
        one_turn = await self._pass(range(34, 37))
        counted = await self._pass(range(1, 31), turn=4)
        self.assertEqual(len(counted), 40 // 4 + 1)
        self.assertTrue(all(count <= one_turn[0] for count in counted[:-1]), counted)
        self.assertEqual(counted[-1], one_turn[-1])
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM metadata_refresh"), 0)
        self.assertEqual(sum(g["season"] == 2 for g in await season_groups()), 20 + 2)


class APassThatCannotFileAnAnswerTests(DatabaseTestCase):
    """Filed together, one answer's fault must cost the pass nothing else, and a
    write that fails whole must not leave its entries to be asked every minute."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        port = _Port()
        real = port.fetch_details

        async def one_broken(settings, media, source_id, season, *, cache_only=False,
                             fresh=False):
            answer = await real(settings, media, source_id, season, fresh=fresh)
            if str(source_id) == "2":
                answer["episodes"] = [{"number": "two"}]
            return answer

        port.fetch_details = one_broken
        for patch in (mock.patch.object(refresh, "_askable", lambda settings: [("tmdb", port)]),
                      mock.patch.object(refresh, "_asked", {})):
            patch.start()
            self.addCleanup(patch.stop)
        await db.execute(
            "INSERT INTO metadata_refresh (source, media, source_id, season, queued_at, reason) "
            "VALUES ('tmdb', 'show', '1', 1, 1, 'viewed'), ('tmdb', 'show', '2', 1, 1, 'viewed'), "
            "('tmdb', 'show', '4', 1, 1, 'viewed')")

    async def test_an_answer_that_cannot_be_read_drops_only_its_entry(self):
        with self.assertLogs(refresh.logger, "WARNING"):
            self.assertEqual(await refresh.drain(SimpleNamespace(timezone="UTC"), now=2), 2)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM metadata_refresh"), 0)
        self.assertEqual(sorted(g["source_id"] for g in await title_groups()), ["1", "4"])

    async def test_a_write_that_fails_whole_drops_the_passs_entries(self):
        async def locked(answers, *, tz):
            raise db.DatabaseError("database is locked")

        with mock.patch.object(descriptions, "store_many", locked), \
                self.assertLogs(refresh.logger, "WARNING"):
            self.assertEqual(await refresh.drain(SimpleNamespace(timezone="UTC"), now=2), 0)
        self.assertEqual(await db.fetch_value("SELECT COUNT(*) FROM metadata_refresh"), 0)
        self.assertEqual(await title_groups(), [])


class AQueuesStatementsDoNotGrowWithWhatItQueuesTests(DatabaseTestCase):

    def _entries(self, ids: range) -> list[refresh.Entry]:
        return [refresh.Entry("tmdb", "show", str(n), refresh.TITLE) for n in ids]

    async def test_thirty_rows_queue_in_what_three_do(self):
        with statements_per_transaction() as few:
            await db.transaction(lambda conn: refresh._queue(
                conn, self._entries(range(1, 4)), now=1, reason="viewed", limit=100))
        with statements_per_transaction() as many:
            await db.transaction(lambda conn: refresh._queue(
                conn, self._entries(range(4, 34)), now=1, reason="viewed", limit=100))
        self.assertEqual(few, many)

    async def test_only_new_rows_count_against_the_limit_in_the_order_given(self):
        await db.transaction(lambda conn: refresh._queue(
            conn, self._entries(range(2, 3)), now=1, reason="viewed", limit=10))
        queued = await db.transaction(lambda conn: refresh._queue(
            conn, self._entries(range(1, 6)), now=2, reason="viewed", limit=2))
        self.assertEqual(queued, 2)
        self.assertEqual([r["source_id"] for r in await db.fetch_all(
            "SELECT source_id FROM metadata_refresh ORDER BY source_id")], ["1", "2", "3"])
