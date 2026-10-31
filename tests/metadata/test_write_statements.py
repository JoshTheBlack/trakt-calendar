"""The title store's writers run a fixed number of statements, however much they
write — a description's episodes, a pass's enrichment answers, the rows a view
queues.

A write transaction holds the database's one write lock for as long as its
statements take, and while the event loop is busy each statement can wait a GIL
switch interval to go on. Each test writes a small batch and one ten times its
size and requires the same count.
"""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

from app import db
from app.calendar import entries
from app.metadata import descriptions, refresh
from app.metadata import rows as title_rows
from app.providers.base import Media, Record, Source
from tests.metadata.test_panel import _answer
from tests.support import DatabaseTestCase, statements_per_transaction

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
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM episode_metadata WHERE source_id = '32'"), 30)
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM season_metadata WHERE source_id = '32'"), 30)


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
            "SELECT COUNT(*) FROM title_metadata WHERE enriched = 1 AND network = 'Tokyo MX'"),
            33)

    async def test_an_answer_still_teaches_its_title_the_ids_it_learned(self):
        """Its rows found by one read before the write, the answer is written by
        rowid — and the title is re-keyed by what it learned, in the same write."""
        await self._listed(range(1, 2))
        await title_rows.apply_enrichment_many(
            "simkl", [(1, "show", {"ids": {"simkl": 1, "tvdb": 4321}})], db.now())
        self.assertEqual(await db.fetch_value("SELECT title_key FROM calendar_airings"),
                         "show:tvdb:4321")

    async def test_a_row_whose_id_moved_after_it_was_found_is_left_alone(self):
        await self._listed(range(1, 2))
        real = db.fetch_value

        async def found_then_moved(sql, params=(), default=None):
            answer = await real(sql, params, default)
            await db.execute("UPDATE title_metadata SET ids_json = '{\"simkl\": 99}'")
            return answer

        title_rows.db.fetch_value = found_then_moved
        try:
            await title_rows.apply_enrichment_many(
                "simkl", [(1, "show", {"network": "Tokyo MX"})], db.now())
        finally:
            title_rows.db.fetch_value = real
        self.assertEqual(await db.fetch_value("SELECT enriched FROM title_metadata"), 0)


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
