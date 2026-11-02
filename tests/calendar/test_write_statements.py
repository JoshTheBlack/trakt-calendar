"""The calendar's writers run a fixed number of statements, however much they write.

A write transaction holds the database's one write lock for as long as its
statements take, and while the event loop is busy each statement can wait a GIL
switch interval to go on — so a fill that issued a statement or eight per record
held the lock for seconds under one month read, and the next writer gave up at
the busy timeout. Each test stores a small batch and one ten times its size and
requires the same count.
"""
from __future__ import annotations

from datetime import date

from app import db
from app.calendar import entries
from app.providers.base import Media, Record, Source
from tests.support import DatabaseTestCase, statements_per_transaction
from tests.support import listings as stored_listings

DAY = 1783540800.0  # 2026-07-08 20:00Z
WEEK = (date(2026, 7, 6), date(2026, 7, 13))


def listings(count: int, *, offset: int = 0) -> list[Record]:
    """`count` listings of distinct titles, every shape a fill carries: Trakt's
    with a tmdb id, Simkl's naming only a tvdb id, a second airing of a title, a
    picture, and a listing with no episode coordinate."""
    out = []
    for n in range(offset, offset + count):
        if n % 2:
            out.append(Record(source=Source.SIMKL, media=Media.SHOW, id=str(5000 + n),
                              ids={"simkl": 5000 + n, "tvdb": 9000 + n}, detail_url="",
                              title=f"Simkl {n}", air_ts=DAY + n, poster="https://simkl.in/p.jpg",
                              season=None if n % 7 == 0 else 1, episode_number=n % 9 + 1,
                              enriched=False))
        else:
            out.append(Record(source=Source.TRAKT, media=Media.SHOW, id=str(100 + n),
                              ids={"trakt": 100 + n, "tmdb": 700 + n, "tvdb": 9000 + n + 1},
                              detail_url="", title=f"Trakt {n}", air_ts=DAY + n,
                              season=2, episode_number=1, episode_title="Pilot"))
    out.append(Record(**{**out[0].__dict__, "air_ts": DAY + 86400, "episode_number": 2}))
    return out


async def store(records: list[Record]) -> None:
    now = db.now()
    await entries.store_span("shows", *WEEK, records, sources=["simkl", "trakt"],
                             asked=["simkl", "trakt"], now=now, stale_after=now + 86400)


class AFillsStatementsDoNotGrowWithItsRecordsTests(DatabaseTestCase):

    async def test_four_hundred_listings_cost_what_forty_do(self):
        with statements_per_transaction() as few:
            await store(listings(40))
        with statements_per_transaction() as many:
            await store(listings(400, offset=1000))
        self.assertEqual(few, many)
        self.assertLessEqual(many[0], 16)
        self.assertEqual(len(await stored_listings("shows")), 400 + 1)

    async def test_a_refill_of_titles_already_held_costs_the_same_whatever_its_size(self):
        """The commonest store — a refill naming titles it named yesterday — and
        one fewer statement than a fill bringing new titles: nothing new is
        known about any of them, so the identity writes nothing."""
        await store(listings(400))
        with statements_per_transaction() as few:
            await store(listings(40))
        with statements_per_transaction() as many:
            await store(listings(400))
        self.assertEqual(few, many)

    async def test_a_searched_airing_writes_as_few_for_fifty_as_for_five(self):
        now = db.now()
        with statements_per_transaction() as few:
            await entries.store_loose_airings("shows", listings(5), now=now,
                                              stale_after=now + 86400)
        with statements_per_transaction() as many:
            await entries.store_loose_airings("shows", listings(50, offset=1000), now=now,
                                              stale_after=now + 86400)
        self.assertEqual(few, many)


class TheEpisodeDrainsStatementsDoNotGrowWithTheSeasonTests(DatabaseTestCase):

    async def _season(self, episodes: int, trakt_id: int) -> list[int]:
        records = [Record(source=Source.TRAKT, media=Media.SHOW, id=str(trakt_id),
                          ids={"trakt": trakt_id, "tmdb": trakt_id}, detail_url="",
                          title="A Show", air_ts=DAY + n, season=1, episode_number=n)
                   for n in range(1, episodes + 1)]
        await store(records)
        answers = [{"number": n, "title": f"Episode {n}"} for n in range(1, episodes + 1)]
        with statements_per_transaction() as counted:
            written = await entries.store_episodes("trakt", trakt_id, "show", 1, answers,
                                                   db.now())
        self.assertEqual(written, episodes)
        return counted

    async def test_thirty_episodes_cost_what_three_do(self):
        self.assertEqual(await self._season(3, 11), await self._season(30, 12))
