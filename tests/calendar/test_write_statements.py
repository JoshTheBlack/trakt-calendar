"""The calendar's writers run a fixed number of statements, however much they write.

A write transaction holds the database's one write lock for as long as its
statements take, and while the event loop is busy each statement can wait a GIL
switch interval to go on — so a fill that issued a statement or eight per record
held the lock for seconds under one month read, and the next writer gave up at
the busy timeout. Each test stores a small batch and one ten times its size and
requires the same count.
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import date
from unittest import mock

from app import db, metadata
from app.calendar import entries
from app.metadata import episodes
from app.providers.base import Media, Record, Source
from tests.support import (DatabaseTestCase, statements_executed, statements_per_transaction,
                           statements_run)
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


async def store(records: list[Record], *, now: int | None = None) -> None:
    now = db.now() if now is None else now
    await entries.store_span("shows", *WEEK, records, sources=["simkl", "trakt"],
                             asked=["simkl", "trakt"], now=now, stale_after=now + 86400)


class ARefillWritesOnlyWhatChangedTests(DatabaseTestCase):
    """The commonest store is a refill naming what it named an hour ago: it
    writes its coverage row — the service confirming the window — and none of
    the title or episode rows it would only have written back as they were."""

    async def test_a_refill_that_changed_nothing_writes_its_coverage_and_nothing_else(self):
        """Decided on a plain read, it takes no transaction: its coverage row is
        one statement in a turn of its own."""
        await store(listings(40))
        with statements_run() as run, statements_executed() as executed:
            await store(listings(40))
        self.assertEqual(run, [])
        self.assertEqual([sql.split()[2] for sql in executed], ["calendar_coverage"])

    async def test_a_viewers_write_does_not_wait_while_an_unchanged_refill_decides(self):
        """Every write waits its turn behind the one ahead of it, so a store that
        decided its rows in its turn held a filter save or a mark behind that
        deciding — seconds, for a month's week — even when it then wrote only its
        coverage row. An unchanged refill decides before taking a turn."""
        await store(listings(40))
        deciding, release = threading.Event(), threading.Event()
        real = metadata.plan_records

        def slow(conn, listed):
            deciding.set()
            release.wait(5)
            return real(conn, listed)

        with mock.patch.object(metadata, "plan_records", slow):
            refill = asyncio.ensure_future(store(listings(40)))
            self.assertTrue(await asyncio.to_thread(deciding.wait, 5))
            try:
                await asyncio.wait_for(
                    db.execute("UPDATE users SET timezone = timezone WHERE id = 0"), 2)
            finally:
                release.set()
                await refill
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM calendar_coverage WHERE endpoint = 'shows'"), 2)

    async def test_an_unchanged_refill_decides_its_rows_once_before_the_lock(self):
        """"Nothing changed" is `Book.spell`'s answer None, which the write once
        took for "not spelled yet" and spelled every row again under the lock —
        sending nothing either time, so the statements above could not see it."""
        await store(listings(40))
        spell = episodes.Book.spell
        spelled: list[bool] = []

        def counted(book):
            answer = spell(book)
            spelled.append(answer is None)
            return answer

        with mock.patch.object(episodes.Book, "spell", counted):
            await store(listings(40))
        self.assertEqual(spelled, [True])

    async def test_a_refill_writes_the_rows_that_changed_and_no_others(self):
        await store(listings(40))
        changed = listings(40)
        changed[2].title = "Renamed"
        changed[1].air_ts += 3600
        with statements_run() as run:
            await store(changed)
        (statements,) = run
        written = " ".join(statements)
        self.assertIn("Renamed", written)
        self.assertEqual(await db.fetch_value(
            "SELECT trakt_title FROM titles WHERE trakt_id = '102'"), "Renamed")
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM episodes WHERE simkl_air_ts = ?", (changed[1].air_ts,)), 1)
        # The titles and episodes that came back as they were are in no
        # statement's payload.
        self.assertNotIn("Trakt 4", written)
        self.assertNotIn("Simkl 3", written)


class AChangingWindowDecidesBeforeItsTurnTests(DatabaseTestCase):
    """A window whose answer changes its rows decides them on a plain read, and
    its turn uses that decision when what it was decided from reads the same
    there (db.transaction's `decided`) — so the deciding is not a turn every
    other write waits behind — and decides again when a write landed in
    between, ending in the rows the two writes make one after the other."""

    async def _changed(self) -> list[Record]:
        await store(listings(40))
        changed = listings(40)
        changed[2].title = "Renamed"
        changed[1].air_ts += 3600
        return changed

    async def test_its_turn_uses_the_decision_made_before_it(self):
        changed = await self._changed()
        real, planned = metadata.plan_records, []

        def counted(conn, listed):
            planned.append(conn.in_transaction)
            return real(conn, listed)

        with mock.patch.object(metadata, "plan_records", counted):
            await store(changed)
        self.assertEqual(planned, [False], "decided once, outside the writer queue")
        self.assertEqual(await db.fetch_value(
            "SELECT trakt_title FROM titles WHERE trakt_id = '102'"), "Renamed")

    async def _trakt_store_with_a_write_between(self, sql: str, params=()) -> int:
        """Trakt's changed answer stored, a write landing between its decision
        and its turn; how many times Trakt's store decided."""
        changed = await self._changed()
        real_decide, planned = db.decide, []
        real = metadata.plan_records

        def counted(conn, listed):
            planned.append(listed.records[0].source)
            return real(conn, listed)

        async def then_a_write(prepare):
            decided = await real_decide(prepare)
            if len(planned) == 1:
                await db.execute(sql, params)
            return decided

        with (mock.patch.object(metadata, "plan_records", counted),
              mock.patch.object(db, "decide", then_a_write)):
            trakt = [record for record in changed if str(record.source) == "trakt"]
            now = db.now()
            await entries.store_span("shows", *WEEK, trakt, sources=["trakt"], asked=["trakt"],
                                     now=now, stale_after=now + 86400)
        self.assertEqual(await db.fetch_value(
            "SELECT trakt_title FROM titles WHERE trakt_id = '102'"), "Renamed")
        return len(planned)

    async def test_a_write_in_between_it_did_not_read_leaves_its_decision_standing(self):
        """Simkl's own name for a title is no input to Trakt's decision."""
        self.assertEqual(await self._trakt_store_with_a_write_between(
            "UPDATE titles SET simkl_title = 'Renamed by Simkl' WHERE simkl_id = '5001'"), 1)
        self.assertEqual(await db.fetch_value(
            "SELECT simkl_title FROM titles WHERE simkl_id = '5001'"), "Renamed by Simkl")

    async def test_a_write_in_between_to_what_it_read_decides_it_again(self):
        """Simkl's airing of an episode in the window moved under it: the
        window's episode rows decide its fold."""
        moved_to = listings(40)[1].air_ts + 1800
        self.assertEqual(await self._trakt_store_with_a_write_between(
            "UPDATE episodes SET simkl_air_ts = ? WHERE simkl_air_ts = ?",
            (moved_to, listings(40)[1].air_ts)), 2)
        self.assertEqual(await db.fetch_value(
            "SELECT COUNT(*) FROM episodes WHERE simkl_air_ts = ?", (moved_to,)), 1)

    async def test_stores_decide_one_at_a_time(self):
        """Side by side, each store's decision was made stale by the other's
        write; one at a time, each decides from what the one before wrote."""
        await store(listings(40))
        real, deciding, most = metadata.plan_records, [0], [0]

        def counted(conn, listed):
            deciding[0] += 1
            most[0] = max(most[0], deciding[0])
            try:
                time.sleep(0.05)
                return real(conn, listed)
            finally:
                deciding[0] -= 1

        changed = listings(40)
        changed[2].title = "Renamed"
        week_after = [Record(**{**record.__dict__, "air_ts": record.air_ts + 7 * 86400})
                      for record in listings(40)]
        with mock.patch.object(metadata, "plan_records", counted):
            now = db.now()
            await asyncio.gather(
                store(changed, now=now),
                entries.store_span("shows", date(2026, 7, 13), date(2026, 7, 20), week_after,
                                   sources=["simkl", "trakt"], asked=["simkl", "trakt"],
                                   now=now, stale_after=now + 86400))
        self.assertEqual(most[0], 1)


class TheRetentionSweepsStatementsDoNotGrowWithWhatItSweepsTests(DatabaseTestCase):
    """The heartbeat sweeps every minute, and nearly every minute nothing is
    due: it must then take no lock at all, and when something is due its lock is
    held for a fixed number of statements, however much goes."""

    async def test_a_sweep_with_nothing_due_takes_no_lock(self):
        await store(listings(40))
        with statements_per_transaction() as counted:
            self.assertEqual(await entries.sweep(), 0)
        self.assertEqual(counted, [])

    async def test_a_hundred_listings_going_cost_what_ten_do(self):
        old = db.now() - entries.RETAIN_SECONDS - 10
        await store(listings(10), now=old)
        with statements_per_transaction() as few:
            self.assertEqual(await entries.sweep(), 11)
        await store(listings(100, offset=1000), now=old)
        with statements_per_transaction() as many:
            self.assertEqual(await entries.sweep(), 101)
        self.assertEqual(few, many)
        self.assertEqual(await stored_listings("shows"), [])


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


class ATmdbGroupItsDescriptionFilledTests(DatabaseTestCase):
    """TMDB's description fills words its discover listing never states — the
    logo, the next episode, a season's own videos — and the listing's refill
    must still write nothing: a TMDB group's clock is its OLDEST word's
    (rows.ceiling_clock), and a listing leaving the description's words in place
    neither re-stamps the group nor reads as a change."""

    WEEK = (date(2026, 11, 2), date(2026, 11, 9))

    def listing(self) -> list[Record]:
        return [Record(source=Source.TMDB, media=Media.SHOW, id="4242", ids={"tmdb": 4242},
                       detail_url="", title="Listed", air_ts=1793836800.0, date_only=True,
                       season=2, episode_number=1, episode_label="S02E01", enriched=False)]

    async def store(self) -> None:
        now = db.now()
        await entries.store_span("shows/premieres", *self.WEEK, self.listing(),
                                 sources=["tmdb"], asked=["tmdb"], now=now,
                                 stale_after=now + 86400)

    async def test_an_unchanged_refill_writes_its_coverage_and_nothing_else(self):
        from zoneinfo import ZoneInfo
        from app.metadata import descriptions
        from tests.metadata.test_panel import _answer, _video
        await self.store()
        await descriptions.store("tmdb", "show", "4242", {
            **_answer("tmdb", "4242", 2, title="Listed",
                      logo="https://image.tmdb.org/t/p/w500/logo.png",
                      next_episode={"season": 2, "number": 2, "title": "", "episode_type": "",
                                    "first_aired": "2026-11-09"}),
            "season_videos": [_video("seasonvideo", "tmdb")]},
            asked_season=2, tz=ZoneInfo("UTC"))
        with statements_run() as run, statements_executed() as executed:
            await self.store()
        self.assertEqual(run, [])
        self.assertEqual([sql.split()[2] for sql in executed], ["calendar_coverage"])
