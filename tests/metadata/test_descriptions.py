"""app/metadata/descriptions.py's writer: a batch of answers filed in one
transaction is exactly those answers filed one transaction each.

THE PROMISE THE REFRESH PASS RESTS ON. A pass files every answer it asked for at
once (descriptions.store_many) where it used to file each as it arrived, and the
rows, clocks, facts and identities have to come out the same: a show's title row
and one of its seasons answered in one pass, a title an earlier answer creates
and a later one rewrites, a re-key that moves a later answer's rows under
another key. Each test writes the batch, reads every table it touches and rolls
it back, then writes the same answers one by one — in the order the batch takes
them — and compares.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from app import db
from app.metadata import descriptions
from tests.metadata.test_panel import _answer
from tests.support import DatabaseTestCase, title_group, title_groups

OLD = 1_000_000
NOW = OLD + 30 * 24 * 60 * 60
UTC = ZoneInfo("UTC")

# Every table a description writes or a re-key moves, ordered by its key.
_TABLES = {
    "titles": "title_key",
    "service_ids": "source, media, service_id",
    "seasons": "title_key, season",
    "episodes": "title_key, unit",
    "title_aliases": "old_key",
    "show_posters": "media, tmdb, source",
    "title_posters": "media, title_key, source",
}


def _dump(conn) -> dict:
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in _TABLES.items()}


class _Batch(Exception):
    """What a batch wrote, carried out of the transaction it is rolled back in."""

    def __init__(self, dump: dict, filed: list):
        super().__init__("rolled back")
        self.dump, self.filed = dump, filed


def _episodes(*numbers, day="2026-07-1"):
    return [{"number": n, "title": f"E{n}", "first_aired": f"{day}{n}T02:00:00Z",
             "rating": None, "overview": "", "episode_type": "", "runtime": None}
            for n in numbers]


class ABatchIsItsAnswersOneByOneTests(DatabaseTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # A described TMDB show whose title row holds a cast, and a Trakt show
        # filed under a tvdb key that a TMDB answer in the batch will re-key.
        await self._write(descriptions.Answer(
            "tmdb", "show", "5", _answer("tmdb", "5", None, title="Five", cast=["Lead"])), OLD)
        await self._write(descriptions.Answer(
            "tmdb", "show", "5", {**_answer("tmdb", "5", 1), "episodes": _episodes(1)}, 1), OLD)
        trakt = _answer("trakt", "9", None, title="Nine")
        trakt["ids"] = {"trakt": 9, "tvdb": 77}
        await self._write(descriptions.Answer("trakt", "show", "9", trakt), OLD)

    async def _write(self, item: descriptions.Answer, now: int) -> None:
        """One answer in a transaction of its own, as a refresh used to file it:
        the description, then the season list read beside it."""
        def _work(conn):
            written = descriptions.write(conn, item.source, item.media, item.own_id,
                                         item.answer, asked_season=item.asked_season,
                                         tz=UTC, now=now)
            if written is not None and item.seasons:
                descriptions.write_season_list(conn, item.source, item.media, item.own_id,
                                               item.seasons, now=now)
            return written
        return await db.transaction(_work)

    async def _compare(self, answers: list[descriptions.Answer]) -> list:
        before = await db.run(_dump)

        def _batch(conn):
            filed = descriptions.write_many(conn, answers, tz=UTC, now=NOW)
            raise _Batch(_dump(conn), filed)

        with self.assertRaises(_Batch) as caught:
            await db.transaction(_batch)
        self.assertEqual(await db.run(_dump), before, "the batch was not rolled back")
        # The order the batch takes them in: those carrying a season list last.
        for item in ([a for a in answers if not a.seasons] + [a for a in answers if a.seasons]):
            await self._write(item, NOW)
        after = await db.run(_dump)
        for table in _TABLES:
            with self.subTest(table=table):
                self.assertEqual(caught.exception.dump[table], after[table])
        return caught.exception.filed

    async def test_a_title_and_its_season_answered_in_one_pass(self):
        title = _answer("tmdb", "5", None, title="Five", cast=["Lead", "Second"])
        season = {**_answer("tmdb", "5", 1, title="Five (US)"), "episodes": _episodes(1, 2)}
        filed = await self._compare([
            descriptions.Answer("tmdb", "show", "5", title, None,
                                [{"season": 1, "episode_count": 10, "name": "One"},
                                 {"season": 2, "episode_count": 8}]),
            descriptions.Answer("tmdb", "show", "5", season, 1)])
        self.assertEqual([found.key for found in filed], ["show:tmdb:5", "show:tmdb:5"])

    async def test_an_answer_that_leaves_a_word_keeps_the_rows_older_clock(self):
        """TMDB's ceiling reads one clock per row (rows.kept_clock): an answer
        about a season leaves the title's cast, so the row keeps its clock — and
        the facts a description states are filed as known (rows.facts_values)."""
        season = {**_answer("tmdb", "5", 1), "episodes": _episodes(1, 2)}
        await self._compare([
            descriptions.Answer("tmdb", "show", "5", season, 1),
            descriptions.Answer("tmdb", "show", "12",
                                _answer("tmdb", "12", None, keywords=["heist"],
                                        show_type="Scripted"))])
        row = await title_group("show:tmdb:5", "tmdb")
        self.assertEqual((row["fetched_at"], row["cast_json"]), (OLD, '["Lead"]'))
        self.assertEqual((await title_group("show:tmdb:12", "tmdb"))["facts_known"], 1)

    async def test_a_re_key_moves_a_later_answers_rows(self):
        six = _answer("tmdb", "6", None, title="Six")
        six["ids"] = {"tmdb": 6, "tvdb": 77}
        nine = {**_answer("trakt", "9", 2), "episodes": _episodes(1, 2, 3)}
        nine["ids"] = {"trakt": 9, "tvdb": 77}
        filed = await self._compare([descriptions.Answer("tmdb", "show", "6", six),
                                     descriptions.Answer("trakt", "show", "9", nine, 2)])
        self.assertEqual([found.key for found in filed], ["show:tmdb:6", "show:tmdb:6"])
        self.assertEqual(filed[1].previous_key, "show:tmdb:6",
                         "the trakt row was found under the key the re-key moved it to")

    async def test_a_title_one_answer_creates_and_the_next_rewrites(self):
        filed = await self._compare([
            descriptions.Answer("tmdb", "show", "40",
                                _answer("tmdb", "40", None, poster="https://tmdb.example/40.jpg"),
                                None, [{"season": 1, "episode_count": 3}]),
            descriptions.Answer("tmdb", "show", "40",
                                {**_answer("tmdb", "40", 1), "episodes": _episodes(1, 2, 3)}, 1),
            descriptions.Answer("simkl", "movie", "300",
                                _answer("simkl", "300", None, title="Only Simkl")),
            descriptions.Answer("tmdb", "show", "41", {})])
        self.assertEqual(filed[1].previous_key, None, "the season was filed before the title")
        self.assertIsNone(filed[3], "an answer that says nothing writes nothing")
        self.assertEqual(filed[2].key, "simkl:300")


class AnAnswerThatCannotBeReadCostsTheBatchNothingElseTests(DatabaseTestCase):

    async def test_the_rest_are_filed_and_its_exception_is_handed_back(self):
        broken = {**_answer("tmdb", "8", 1), "episodes": [{"number": "eight"}]}
        filed = await db.transaction(lambda conn: descriptions.write_many(conn, [
            descriptions.Answer("tmdb", "show", "7", _answer("tmdb", "7", None)),
            descriptions.Answer("tmdb", "show", "8", broken, 1),
            descriptions.Answer("tmdb", "show", "9", _answer("tmdb", "9", None))],
            tz=UTC, now=NOW))
        self.assertIsInstance(filed[1], ValueError)
        self.assertEqual([found.key for found in (filed[0], filed[2])],
                         ["show:tmdb:7", "show:tmdb:9"])
        self.assertEqual(sorted(g["source_id"] for g in await title_groups()), ["7", "9"])

    async def test_one_answer_raises_it(self):
        broken = {**_answer("tmdb", "8", 1), "episodes": [{"number": "eight"}]}
        with self.assertRaises(ValueError):
            await descriptions.store("tmdb", "show", "8", broken, asked_season=1, tz=UTC)
