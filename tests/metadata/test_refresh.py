"""app/metadata/refresh.py — the title store's refresh queue.

THE PROMISES:
  - A VIEW QUEUES WHAT IS DUE AND NOTHING ELSE: a description past its clock, or
    — where the page draws a title's words — a service that has none yet; the
    stalest first, inside the view's budget; a row whose last refresh failed
    waits out its backoff; asking twice queues once.
  - A VIEW NEVER WAITS FOR IT: `viewed` hands its work to `start` and returns.
  - A DEAD PICTURE JUMPS THE QUEUE: every service that knows the title, and the
    seasons named, at the front.
  - A PASS ASKS AGAIN PAST THE RESPONSE CACHE and files the answer as a first
    answer is filed; a failure marks the row and drops the entry; a service this
    instance cannot ask keeps its rows queued; each service at its own pace.
  - A HELD ROW IS RE-READ BEFORE ITS SERVICE'S CEILING; nobody else's is.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from app import db, providers
from app.metadata import descriptions, refresh
from app.providers.base import SeasonsAnswer, SourceUnavailable
from app.providers.tmdb import transport as tmdb_transport
from tests.metadata.test_panel import _answer
from tests.support import DatabaseTestCase

# In the past, because a drain files what it is answered on the real clock.
T0 = 1_700_000_000
DAY = 24 * 60 * 60
SETTINGS = SimpleNamespace(timezone="UTC")


class _Port:
    """One service's detail port, recording every question it is asked."""

    def __init__(self, source: str, *, fails: bool = False):
        self.source, self.fails = source, fails
        self.asked: list[tuple] = []

    def catalogue_configured(self, settings):
        return True

    async def fetch_details(self, settings, media, source_id, season, *, cache_only=False,
                            fresh=False):
        self.asked.append(("details", str(media), str(source_id), season, fresh))
        if self.fails:
            raise SourceUnavailable("down")
        return _answer(self.source, source_id, season)

    async def fetch_seasons(self, settings, source_id, media, *, cached_only=False,
                            fresh=False):
        self.asked.append(("seasons", str(source_id), fresh))
        return SeasonsAnswer(seasons=[], named_season=None, ids={}, network="")


async def _describe(source: str, own_id: str, *, at: int, season=None, ids=None) -> None:
    answer = _answer(source, own_id, season)
    if ids:
        answer["ids"] = {**answer["ids"], **ids}
    await db.transaction(lambda conn: descriptions.write(
        conn, source, "show", own_id, answer, asked_season=season, tz=ZoneInfo("UTC"),
        now=at))


async def _queue() -> list[tuple]:
    return [tuple(row) for row in await db.fetch_all(
        "SELECT source, media, source_id, season, queued_at, reason FROM metadata_refresh "
        "ORDER BY queued_at, source, source_id, season")]


class _RefreshTestCase(DatabaseTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ports = {"tmdb": _Port("tmdb"), "trakt": _Port("trakt")}
        patch = mock.patch.object(refresh, "_askable",
                                  lambda settings: list(self.ports.items()))
        patch.start()
        self.addCleanup(patch.stop)

    async def view(self, *wanted, at: int, budget: int = 50, missing: bool = True) -> int:
        return await refresh.queue_viewed(SETTINGS, list(wanted), budget=budget,
                                          missing=missing, now=at)


class EveryServiceHasAPaceTests(DatabaseTestCase):
    def test_every_registered_service_has_a_pass_budget_and_a_gate(self):
        """A service with no pace of its own must not inherit somebody else's."""
        for source in providers.registered():
            with self.subTest(source=source):
                self.assertGreater(refresh.PER_MINUTE[str(source)], 0)
                self.assertGreater(refresh.IN_FLIGHT[str(source)], 0)


class WhatAViewQueuesTests(_RefreshTestCase):

    async def test_a_description_past_its_clock_is_queued(self):
        await _describe("tmdb", "5", at=T0)
        self.assertEqual(await self.view(refresh.Wanted("show", {"tmdb": 5}),
                                         at=T0 + 4 * DAY), 1)
        self.assertEqual(await _queue(),
                         [("tmdb", "show", "5", refresh.TITLE, T0 + 4 * DAY, "viewed")])

    async def test_a_description_inside_its_clock_is_not(self):
        await _describe("tmdb", "5", at=T0)
        self.assertEqual(await self.view(refresh.Wanted("show", {"tmdb": 5}), at=T0 + DAY), 0)

    async def test_a_service_with_no_description_is_due_only_where_the_page_draws_one(self):
        await _describe("tmdb", "5", at=T0)
        wanted = refresh.Wanted("show", {"tmdb": 5, "trakt": 9})
        self.assertEqual(await self.view(wanted, at=T0 + DAY, missing=False), 0)
        self.assertEqual(await self.view(wanted, at=T0 + DAY, missing=True), 1)
        self.assertEqual([row[:4] for row in await _queue()],
                         [("trakt", "show", "9", refresh.TITLE)])

    async def test_a_service_the_page_did_not_name_is_asked_by_the_id_its_row_states(self):
        await _describe("tmdb", "5", at=T0 + 10 * DAY)
        await _describe("trakt", "9", at=T0, ids={"tmdb": 5})
        await self.view(refresh.Wanted("show", {"tmdb": 5}), at=T0 + 10 * DAY)
        self.assertEqual([row[:4] for row in await _queue()],
                         [("trakt", "show", "9", refresh.TITLE)])

    async def test_the_season_on_screen_is_due_by_its_own_clock(self):
        await _describe("tmdb", "5", at=T0)
        await _describe("tmdb", "5", at=T0, season=1)
        await self.view(refresh.Wanted("show", {"tmdb": 5}, 1),
                        refresh.Wanted("show", {"tmdb": 5}, 2), at=T0 + 400 * DAY)
        self.assertEqual([row[:4] for row in await _queue()],
                         [("tmdb", "show", "5", refresh.TITLE), ("tmdb", "show", "5", 1),
                          ("tmdb", "show", "5", 2)])

    async def test_the_stalest_go_first_inside_the_budget(self):
        for own_id, at in (("1", T0 + 2 * DAY), ("2", T0), ("3", T0 + DAY)):
            await _describe("tmdb", own_id, at=at)
        await self.view(*(refresh.Wanted("show", {"tmdb": n}) for n in (1, 2, 3)),
                        at=T0 + 30 * DAY, budget=2)
        self.assertEqual(sorted(row[2] for row in await _queue()), ["2", "3"])

    async def test_a_row_whose_refresh_failed_waits_out_its_backoff(self):
        await _describe("tmdb", "5", at=T0)
        now = T0 + 10 * DAY
        await db.execute("UPDATE title_metadata SET fail_count = 1, failed_at = ?", (now - 60,))
        self.assertEqual(await self.view(refresh.Wanted("show", {"tmdb": 5}), at=now), 0)
        self.assertEqual(await self.view(refresh.Wanted("show", {"tmdb": 5}),
                                         at=now + 2 * 60 * 60), 1)

    async def test_asking_twice_queues_once(self):
        await _describe("tmdb", "5", at=T0)
        for _ in range(2):
            await self.view(refresh.Wanted("show", {"tmdb": 5}), at=T0 + 10 * DAY)
        self.assertEqual(len(await _queue()), 1)


class AViewNeverWaitsTests(_RefreshTestCase):

    async def test_viewed_hands_its_work_to_start_and_returns(self):
        await _describe("tmdb", "5", at=T0)
        started = []
        ran = []

        async def _run(settings):
            ran.append(True)
            return 0

        with mock.patch.object(refresh, "start", started.append), \
                mock.patch.object(refresh, "run", _run), \
                mock.patch.object(db, "now", return_value=T0 + 10 * DAY):
            self.assertIsNone(refresh.viewed(SETTINGS, [refresh.Wanted("show", {"tmdb": 5})],
                                             budget=5))
            self.assertEqual(await _queue(), [], "nothing is queued before the work runs")
            self.assertEqual(len(started), 1)
            await started[0]
        self.assertEqual(len(await _queue()), 1)
        self.assertEqual(ran, [True])

    def test_nothing_drawn_starts_nothing(self):
        with mock.patch.object(refresh, "start") as start:
            refresh.viewed(SETTINGS, [], budget=5)
        start.assert_not_called()


class ADeadPictureJumpsTheQueueTests(_RefreshTestCase):

    async def test_every_service_and_season_goes_to_the_front(self):
        await _describe("tmdb", "5", at=T0)
        await self.view(refresh.Wanted("show", {"tmdb": 5}), at=T0 + 10 * DAY)
        entries = await refresh.queue_title(SETTINGS, "show", {"tmdb": 5, "trakt": 9}, [2],
                                             reason=refresh.REASON_DEAD_PICTURE)
        self.assertEqual(len(entries), 4)
        self.assertEqual(sorted(await _queue()), sorted([
            ("tmdb", "show", "5", refresh.TITLE, 0, "dead picture"),
            ("tmdb", "show", "5", 2, 0, "dead picture"),
            ("trakt", "show", "9", refresh.TITLE, 0, "dead picture"),
            ("trakt", "show", "9", 2, 0, "dead picture")]))

    async def test_a_film_has_no_seasons_to_queue(self):
        entries = await refresh.queue_title(SETTINGS, "movie", {"tmdb": 5}, [1],
                                            reason=refresh.REASON_DEAD_PICTURE)
        self.assertEqual([entry.season for entry in entries], [refresh.TITLE])

    async def test_a_heal_nobody_can_finish_is_forgotten_on_its_clock(self):
        """A heal's rows for a service this instance can no longer ask stay queued
        in the table, and the drain never pops their bookkeeping. The process runs
        for weeks, so the next heal drops what is older than the bookkeeping clock
        rather than holding it for all of them."""
        clock = [5000.0]
        with mock.patch.object(refresh.time, "monotonic", lambda: clock[0]):
            await refresh.queue_title(SETTINGS, "show", {"tmdb": 5}, [2],
                                      reason=refresh.REASON_DEAD_PICTURE, title_key="show:tmdb:5")
            self.assertEqual(len(refresh._heals), 2)
            clock[0] += refresh._HEAL_BOOKKEEPING_SECONDS
            await refresh.queue_title(SETTINGS, "movie", {"tmdb": 6},
                                      reason=refresh.REASON_DEAD_PICTURE, title_key="movie:tmdb:6")
        self.assertEqual([key for key, _since in refresh._heals.values()], ["movie:tmdb:6"])


class TheDrainTests(_RefreshTestCase):

    async def test_a_pass_asks_past_the_cache_and_files_the_answer(self):
        await _describe("tmdb", "5", at=T0)
        await refresh.queue_title(SETTINGS, "show", {"tmdb": 5}, reason="viewed")
        now = T0 + 10 * DAY
        self.assertEqual(await refresh.drain(SETTINGS, now=now), 1)
        self.assertEqual(self.ports["tmdb"].asked,
                         [("details", "show", "5", None, True), ("seasons", "5", True)])
        self.assertEqual(await _queue(), [])
        stale_after = await db.fetch_value(
            "SELECT stale_after FROM title_metadata WHERE source = 'tmdb'")
        self.assertGreater(stale_after, now)

    async def test_a_failure_marks_the_row_and_drops_the_entry(self):
        await _describe("tmdb", "5", at=T0)
        self.ports["tmdb"] = _Port("tmdb", fails=True)
        await refresh.queue_title(SETTINGS, "show", {"tmdb": 5}, reason="viewed")
        self.assertEqual(await refresh.drain(SETTINGS, now=T0 + DAY), 0)
        self.assertEqual(await _queue(), [])
        row = await db.fetch_one("SELECT fail_count, failed_at FROM title_metadata")
        self.assertEqual((row["fail_count"], row["failed_at"]), (1, T0 + DAY))

    async def test_a_service_that_says_slow_down_keeps_its_rows_queued_unmarked(self):
        """A 429 is not the title's failure: no backoff is recorded (five would
        give a sparse listing up), the entry stays queued, and the rest of that
        service's batch waits for the next pass rather than adding to the storm."""
        for n in (1, 2, 3):
            await _describe("tmdb", str(n), at=T0)
            await refresh.queue_title(SETTINGS, "show", {"tmdb": n}, reason="viewed")
        asked = []

        async def _slow_down(settings, media, source_id, season, *, cache_only=False,
                             fresh=False):
            asked.append(source_id)
            raise SourceUnavailable("TMDB asked us to slow down", 429)
        self.ports["tmdb"].fetch_details = _slow_down
        with mock.patch.dict(refresh.IN_FLIGHT, {"tmdb": 1}):
            self.assertEqual(await refresh.drain(SETTINGS, now=T0 + DAY), 0)
        self.assertEqual(len(asked), 1, "the batch went on asking after the 429")
        self.assertEqual(len(await _queue()), 3)
        self.assertEqual(await db.fetch_value(
            "SELECT MAX(fail_count) FROM title_metadata WHERE source = 'tmdb'"), 0)

    async def test_a_service_this_instance_cannot_ask_keeps_its_rows_queued(self):
        await refresh.queue_title(SETTINGS, "show", {"tmdb": 5}, reason="viewed")
        del self.ports["tmdb"]
        self.assertEqual(await refresh.drain(SETTINGS, now=T0), 0)
        self.assertEqual(len(await _queue()), 1)

    async def test_each_service_takes_only_its_minutes_allowance(self):
        """However many passes ask: a page view starts one, so a budget per pass
        would be a budget per reload."""
        for n in (1, 2, 3, 4):
            await refresh.queue_title(SETTINGS, "movie", {"tmdb": n}, reason="viewed")
        with mock.patch.dict(refresh.PER_MINUTE, {"tmdb": 2}):
            self.assertEqual(await refresh.drain(SETTINGS, now=T0), 2)
            self.assertEqual(await refresh.drain(SETTINGS, now=T0), 0)
            self.assertEqual(len(await _queue()), 2)
            # A minute on, the next two.
            with mock.patch.object(refresh.time, "monotonic",
                                   return_value=refresh.time.monotonic() + 61):
                self.assertEqual(await refresh.drain(SETTINGS, now=T0), 2)
        self.assertEqual(await _queue(), [])


async def _listed(tmdb_id: int, *, at: int) -> None:
    """A TMDB discover listing of a show, stored as a calendar fill stores it —
    sparse, `enriched=False` (providers/tmdb/normalize.py's `to_record`)."""
    from app import metadata
    from app.providers.base import Media
    from app.providers.tmdb import normalize

    record = normalize.to_record({"id": tmdb_id, "name": f"Show {tmdb_id}",
                                  "first_air_date": "2026-10-27"}, Media.SHOW, 1, "2026-10-27")
    listed = metadata.listings([record], now=at, stale_after=at + DAY)
    await db.transaction(lambda conn: metadata.store_records(conn, listed))


class ASparseListingIsDescribedTests(_RefreshTestCase):
    """A service whose calendar listings arrive with no network, status or
    certification, and whose own description is one request away
    (Capabilities.describes_sparse_listings — TMDB's), has each such row queued
    for a full description on the heartbeat, at the service's own pace."""

    async def test_each_sparse_listing_is_queued_newest_first_within_the_minute(self):
        for tmdb_id, at in ((1, T0), (2, T0 + 20), (3, T0 + 10)):
            await _listed(tmdb_id, at=at)
        with mock.patch.dict(refresh.PER_MINUTE, {"tmdb": 2}):
            self.assertEqual(await refresh.queue_listed(SETTINGS, now=T0 + 30), 2)
            self.assertEqual(await refresh.queue_listed(SETTINGS, now=T0 + 31), 0,
                             "more were queued than a minute's allowance")
        self.assertEqual([row[:4] + row[5:] for row in await _queue()],
                         [("tmdb", "show", "2", refresh.TITLE, "listed"),
                          ("tmdb", "show", "3", refresh.TITLE, "listed")])

    async def test_a_service_whose_listings_arrive_complete_is_never_queued(self):
        await _describe("trakt", "9", at=T0)
        await db.execute("UPDATE title_metadata SET enriched = 0")
        self.assertEqual(await refresh.queue_listed(SETTINGS, now=T0 + 30), 0)

    async def test_a_described_listing_is_owed_nothing(self):
        await _listed(5, at=T0)
        self.assertEqual(await refresh.listings_owed(), 1)
        await refresh.queue_listed(SETTINGS, now=T0 + 30)
        self.assertEqual(await refresh.drain(SETTINGS, now=T0 + 40), 1)
        self.assertEqual(await refresh.listings_owed(), 0)
        self.assertEqual(await refresh.queue_listed(SETTINGS, now=T0 + 50), 0)

    async def test_one_the_service_says_nothing_about_backs_off_and_is_given_up_on(self):
        """An answer that says nothing is a refresh that did not happen: marked as
        one, so the listing waits out the backoff rather than being asked about
        every minute, and stops being owed at the ceiling."""
        await _listed(5, at=T0)

        async def _nothing(settings, media, source_id, season, *, cache_only=False, fresh=False):
            return {}
        self.ports["tmdb"].fetch_details = _nothing
        now = T0 + 30
        for attempt in range(refresh.LISTED_GIVE_UP_AFTER):
            self.assertEqual(await refresh.queue_listed(SETTINGS, now=now), 1, attempt)
            await refresh.drain(SETTINGS, now=now)
            self.assertEqual(await refresh.queue_listed(SETTINGS, now=now + 60), 0,
                             "a listing that said nothing was asked about again at once")
            now += 2 * DAY
        self.assertEqual(await refresh.listings_owed(), 0)
        self.assertEqual(await refresh.queue_listed(SETTINGS, now=now), 0)

    async def test_the_calendars_unenriched_count_includes_them(self):
        from app.calendar import enrich as calendar_enrich

        await _listed(5, at=T0)
        titles, _seasons = await calendar_enrich.backlog()
        self.assertEqual(titles, 1)

    def test_every_service_states_how_its_listings_are_completed(self):
        """A per-service fact, declared by each package: TMDB's sparse listings
        are described; Trakt's arrive complete; Simkl's are completed by its own
        drain."""
        self.assertEqual(set(refresh._listing_sources()), {"tmdb"})


class HeldRowsNearTheCeilingTests(_RefreshTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        user = (await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('v', 1, 1)")).lastrowid
        await db.execute(
            "INSERT INTO distrakt_user_seasons (user_id, media, match_source, match_id, "
            "season, kind, created_at) VALUES (?, 'show', 'tmdb', '21', 1, 'keepup', 1)",
            (user,))
        self.now = T0 + 400 * DAY
        self.near = self.now - tmdb_transport.MAX_CACHE_SECONDS + DAY

    async def _title(self, source: str, own_id: int, fetched_at: int) -> None:
        await db.execute(
            "INSERT INTO title_metadata (source, media, source_id, title_key, ids_json, "
            "fetched_at) VALUES (?, 'show', ?, ?, ?, ?)",
            (source, str(own_id), f"show:tmdb:{own_id}", json.dumps({source: own_id}),
             fetched_at))

    async def test_a_held_row_near_its_ceiling_is_queued(self):
        await self._title("tmdb", 21, self.near)
        self.assertEqual(await refresh.queue_ceiling(SETTINGS, now=self.now), 1)
        self.assertEqual(await _queue(),
                         [("tmdb", "show", "21", refresh.TITLE, self.now, "ceiling")])

    async def test_a_row_nobody_holds_is_left_to_its_ceiling(self):
        await self._title("tmdb", 22, self.near)
        self.assertEqual(await refresh.queue_ceiling(SETTINGS, now=self.now), 0)

    async def test_a_row_far_from_its_ceiling_waits(self):
        await self._title("tmdb", 21, self.now - DAY)
        self.assertEqual(await refresh.queue_ceiling(SETTINGS, now=self.now), 0)

    async def test_a_service_with_no_ceiling_is_never_queued_for_one(self):
        await db.execute(
            "INSERT INTO title_metadata (source, media, source_id, title_key, ids_json, "
            "fetched_at) VALUES ('trakt', 'show', 'slug', 'show:tmdb:21', '{\"trakt\": 9}', ?)",
            (self.near,))
        self.assertEqual(await refresh.queue_ceiling(SETTINGS, now=self.now), 0)
