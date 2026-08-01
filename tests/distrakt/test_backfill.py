"""Backfilling months the tracker was never running for, from watch history.

The rule under test is the same one the live Completed bucket uses: a season
belongs to the month its LAST episode was watched in, and only if every episode
was watched. Everything expensive is mocked at the Trakt boundary; the sweep,
the bucketing, the plan and the writes are all real.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, distrakt
from app.distrakt import backfill, watch_history
from app.calendar import state as calendar_state
from app.providers.base import ItemKey
from app.config import Settings, save_settings
from app.main import app
from tests.support import ORIGIN, migrated_db, new_db_path

SETTINGS = SimpleNamespace(configured=True, timezone="UTC")


def _ep_event(trakt_id, season, number, watched_at, title="Show", network="Net"):
    return {
        "type": "episode",
        "watched_at": watched_at,
        "show": {"title": title, "network": network,
                 "ids": {"trakt": trakt_id, "slug": f"slug-{trakt_id}", "tmdb": 900 + trakt_id}},
        "episode": {"season": season, "number": number},
    }


def _mv_event(trakt_id, title, year, watched_at):
    return {"type": "movie", "watched_at": watched_at,
            "movie": {"title": title, "year": year,
                      "ids": {"trakt": trakt_id, "tmdb": trakt_id}}}


async def _make_user(username: str) -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


class MonthMathTests(unittest.TestCase):
    def test_month_range_is_inclusive_and_ordered(self):
        self.assertEqual(backfill.month_range("2026-01", "2026-03"),
                         ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(backfill.month_range("2025-12", "2026-01"), ["2025-12", "2026-01"])

    def test_a_backwards_or_unusable_range_covers_no_months(self):
        for start, end in (("2026-06", "2026-01"), ("2026-13", "2026-06"),
                           ("", "2026-06"), ("2026-01", "nonsense")):
            with self.subTest(start=start, end=end):
                self.assertEqual(backfill.month_range(start, end), [])

    def test_a_range_is_bounded(self):
        """One button must not sweep an entire viewing life."""
        self.assertEqual(len(backfill.month_range("2000-01", "2026-12")), backfill.MAX_MONTHS)

    def test_the_default_range_is_this_year_up_to_last_month(self):
        self.assertEqual(backfill.default_range(today=date(2026, 7, 28)),
                         ("2026-01", "2026-06"))

    def test_the_default_range_does_not_shrink_as_months_fill_up(self):
        """It used to stop before the earliest tracked month, which made sense
        when this could only run once. A re-run picks up films across the whole
        range and refills emptied months, so the default has to still reach
        them."""
        self.assertEqual(backfill.default_range(today=date(2026, 12, 1)),
                         ("2026-01", "2026-11"))

    def test_the_default_range_never_reaches_the_current_month(self):
        self.assertEqual(backfill.default_range(today=date(2026, 1, 15)),
                         ("2026-01", "2026-01"))


class BackfillTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("backfill")
        await db.migrate()
        self.user_id = await _make_user("tracker")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _survey(self, events, progress, totals, start="2026-01", end="2026-06"):
        """Run a real survey with Trakt mocked at its three call sites."""
        async def fake_history(settings, start_at=None, limit=100, max_pages=50):
            return events

        async def fake_progress(settings, trakt_id, client=None):
            return progress.get(int(trakt_id), {})

        async def fake_season(settings, trakt_id, season, fresh=False, client=None):
            return totals.get((int(trakt_id), int(season)), {"total": 0})

        with patch("app.providers.trakt.sync.fetch_history", side_effect=fake_history), \
             patch("app.providers.trakt.sync.fetch_show_progress_detail", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=fake_season):
            return await backfill.survey(self.user_id, SETTINGS, start, end,
                                         today=date(2026, 7, 28))


class SurveyTests(BackfillTestCase):
    """What the sweep decides, before anything is written."""

    async def test_a_season_lands_in_the_month_its_last_episode_was_watched(self):
        events = [_ep_event(101, 1, 3, "2026-03-14T20:00:00Z", title="Finished In March")]
        progress = {101: {1: {1: "2026-02-27T00:00:00Z", 2: "2026-03-02T00:00:00Z",
                              3: "2026-03-14T20:00:00Z"}}}
        plan = await self._survey(events, progress, {(101, 1): {"total": 3}})

        self.assertEqual(list(plan["months"]), ["2026-03"])
        row = plan["months"]["2026-03"][0]
        self.assertEqual((row["title"], row["season"], row["bucket"]),
                         ("Finished In March", 1, "completed"))
        # A frozen month's counts are never recomputed, so they have to be right now.
        self.assertEqual((row["watched"], row["total"]), (3, 3))
        self.assertEqual(row["added_by"], distrakt.ADDED_BY_HISTORY)
        # The whole id map the sweep reported travels into the record, so the row
        # it becomes is keyed on the shared id and not on Trakt's own.
        self.assertEqual(row["ids"], {"trakt": 101, "tmdb": 1001, "slug": "slug-101"})

    async def test_an_unfinished_season_belongs_to_no_month(self):
        events = [_ep_event(102, 1, 2, "2026-03-14T20:00:00Z")]
        progress = {102: {1: {1: "2026-03-01T00:00:00Z", 2: "2026-03-14T20:00:00Z"}}}
        plan = await self._survey(events, progress, {(102, 1): {"total": 8}})
        self.assertEqual(plan["months"], {})

    async def test_episodes_watched_before_the_range_still_count_towards_finishing(self):
        """The sweep only sees plays inside the range, but a season whose earlier
        episodes were watched in December and whose last landed in January IS
        finished in January — which is why the completed set comes from progress
        rather than from the sweep."""
        events = [_ep_event(103, 1, 4, "2026-01-06T00:00:00Z")]
        progress = {103: {1: {1: "2025-12-20T00:00:00Z", 2: "2025-12-27T00:00:00Z",
                              3: "2026-01-02T00:00:00Z", 4: "2026-01-06T00:00:00Z"}}}
        plan = await self._survey(events, progress, {(103, 1): {"total": 4}})
        self.assertEqual(list(plan["months"]), ["2026-01"])

    async def test_a_season_finished_outside_the_range_is_not_written(self):
        events = [_ep_event(104, 1, 2, "2026-01-05T00:00:00Z")]
        progress = {104: {1: {1: "2026-01-04T00:00:00Z", 2: "2026-01-05T00:00:00Z"}}}
        plan = await self._survey(events, progress, {(104, 1): {"total": 2}},
                                  start="2026-02", end="2026-06")
        self.assertEqual(plan["months"], {})

    async def test_a_month_that_is_already_tracked_is_left_alone(self):
        """A tracked month holds decisions watch history knows nothing about."""
        await distrakt.add_show(self.user_id, "2026-03", {
            "ids": {"trakt": 999, "tmdb": 999, "slug": "mine"}, "season": 1, "title": "Mine"})
        events = [_ep_event(105, 1, 2, "2026-03-14T00:00:00Z")]
        progress = {105: {1: {1: "2026-03-01T00:00:00Z", 2: "2026-03-14T00:00:00Z"}}}
        plan = await self._survey(events, progress, {(105, 1): {"total": 2}})

        self.assertEqual(plan["skipped"], ["2026-03"])
        self.assertEqual(plan["months"], {})

    async def test_the_summary_carries_no_records(self):
        """The client's job is to say yes, not to hand back what to store."""
        events = [_ep_event(106, 2, 1, "2026-04-02T00:00:00Z", title="Season Two")]
        progress = {106: {2: {1: "2026-04-02T00:00:00Z"}}}
        plan = await self._survey(events, progress, {(106, 2): {"total": 1}})

        summary = backfill.summarize(plan)
        self.assertEqual(summary["months"], [{"month": "2026-04", "count": 1,
                                              "titles": ["Season Two S02"],
                                              "movie_count": 0, "movie_titles": [],
                                              "movie_known": 0}])
        self.assertNotIn("trakt_id", repr(summary))


class ApplyTests(BackfillTestCase):
    """What gets written, and what refuses to be."""

    async def test_writing_produces_closed_months_the_ranker_can_read(self):
        events = [_ep_event(201, 1, 2, "2026-02-10T00:00:00Z", title="Feb Show"),
                  _ep_event(202, 1, 1, "2026-05-04T00:00:00Z", title="May Show")]
        progress = {201: {1: {1: "2026-02-01T00:00:00Z", 2: "2026-02-10T00:00:00Z"}},
                    202: {1: {1: "2026-05-04T00:00:00Z"}}}
        await self._survey(events, progress,
                           {(201, 1): {"total": 2}, (202, 1): {"total": 1}})

        written = await backfill.apply(self.user_id)
        self.assertEqual(written["months"], ["2026-02", "2026-05"])

        doc = await distrakt.load_month(self.user_id, "2026-02")
        self.assertTrue(doc["closed"])
        self.assertEqual([(s["title"], s["bucket"]) for s in doc["shows"]],
                         [("Feb Show", "completed")])

    async def test_applying_twice_does_not_double_a_month(self):
        events = [_ep_event(203, 1, 1, "2026-02-10T00:00:00Z")]
        progress = {203: {1: {1: "2026-02-10T00:00:00Z"}}}
        await self._survey(events, progress, {(203, 1): {"total": 1}})
        await backfill.apply(self.user_id)

        with self.assertRaises(backfill.BackfillExpired):
            await backfill.apply(self.user_id)
        doc = await distrakt.load_month(self.user_id, "2026-02")
        self.assertEqual(len(doc["shows"]), 1)

    async def test_applying_without_a_survey_refuses(self):
        """The survey is the only thing that can produce records to write."""
        with self.assertRaises(backfill.BackfillExpired):
            await backfill.apply(self.user_id)

    async def test_the_movies_from_the_same_sweep_are_recorded(self):
        """The routine sync only looks back to the start of the current month, so
        for these months movies are recorded nowhere — and the ranker wants them."""
        events = [_ep_event(204, 1, 1, "2026-02-10T00:00:00Z"),
                  _mv_event(50, "A Film", 2026, "2026-02-14T00:00:00Z"),
                  _mv_event(51, "Out Of Range", 2025, "2025-11-01T00:00:00Z")]
        progress = {204: {1: {1: "2026-02-10T00:00:00Z"}}}
        await self._survey(events, progress, {(204, 1): {"total": 1}})
        await backfill.apply(self.user_id)

        state = await watch_history.load_state(self.user_id)
        self.assertEqual(list(state["movies"]), ["movie:tmdb:50"])
        # and the frozen month carries its own **Movies** section, as a month
        # that had been tracked and frozen would
        doc = await distrakt.load_month(self.user_id, "2026-02")
        self.assertEqual([m["title"] for m in doc["movies"]], ["A Film"])

    async def test_films_are_recorded_even_when_no_season_was_finished(self):
        """They are watch history in their own right, and nothing else will ever
        record them for these months — dropping them because no SHOW finished
        that month loses data the ranker asks for by name."""
        events = [_mv_event(60, "Only A Film", 2026, "2026-02-14T00:00:00Z")]
        plan = await self._survey(events, {}, {})
        self.assertEqual(plan["months"], {})

        written = await backfill.apply(self.user_id)
        self.assertEqual(written["movies"], 1)
        state = await watch_history.load_state(self.user_id)
        self.assertEqual(list(state["movies"]), ["movie:tmdb:60"])

    async def test_the_plan_lists_films_month_by_month(self):
        """A count with nothing behind it is not something to confirm."""
        events = [_ep_event(207, 1, 1, "2026-02-10T00:00:00Z", title="Feb Show"),
                  _mv_event(61, "Feb Film", 2026, "2026-02-14T00:00:00Z"),
                  _mv_event(62, "May Film", 2025, "2026-05-02T00:00:00Z")]
        progress = {207: {1: {1: "2026-02-10T00:00:00Z"}}}
        plan = await self._survey(events, progress, {(207, 1): {"total": 1}})

        summary = backfill.summarize(plan)
        by_month = {m["month"]: m for m in summary["months"]}
        self.assertEqual(by_month["2026-02"]["movie_titles"], ["Feb Film (2026)"])
        self.assertEqual(by_month["2026-02"]["titles"], ["Feb Show S01"])
        # A month with films but no finished season still appears, or its films
        # would be written without ever having been shown.
        self.assertEqual(by_month["2026-05"]["count"], 0)
        self.assertEqual(by_month["2026-05"]["movie_titles"], ["May Film (2025)"])

    async def test_films_can_still_be_picked_up_after_the_seasons_are_written(self):
        """THE RE-RUN CASE. Scoping films to the months being WRITTEN made them
        unreachable the moment the seasons had been: the range had no writable
        months left, so a second run reported nothing to do and the films in
        those months could never be collected."""
        events = [_ep_event(210, 1, 1, "2026-02-10T00:00:00Z", title="Feb Show")]
        progress = {210: {1: {1: "2026-02-10T00:00:00Z"}}}
        await self._survey(events, progress, {(210, 1): {"total": 1}})
        await backfill.apply(self.user_id)
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-02"])

        # Same months, now all written. A film in one of them must still be seen.
        events.append(_mv_event(70, "Late Film", 2026, "2026-02-14T00:00:00Z"))
        plan = await self._survey(events, progress, {(210, 1): {"total": 1}})
        self.assertEqual(plan["skipped"], ["2026-02"])
        self.assertEqual([f["title"] for f in plan["movies"]], ["Late Film"])

        written = await backfill.apply(self.user_id)
        self.assertEqual(written["movies"], 1)
        state = await watch_history.load_state(self.user_id)
        self.assertEqual(list(state["movies"]), ["movie:tmdb:70"])

    async def test_a_film_already_recorded_is_not_offered_again(self):
        """A second run offers what is missing, not everything it can see."""
        events = [_mv_event(71, "Seen It", 2026, "2026-02-14T00:00:00Z")]
        await self._survey(events, {}, {})
        await backfill.apply(self.user_id)

        plan = await self._survey(events, {}, {})
        self.assertEqual(plan["movies"], [])

    async def test_a_closed_month_that_gains_films_has_its_own_list_topped_up(self):
        """Otherwise the plan says "February: 1 film" and February goes on
        showing none — its verdicts stay settled, its record of what was
        watched does not."""
        events = [_ep_event(211, 1, 1, "2026-02-10T00:00:00Z")]
        progress = {211: {1: {1: "2026-02-10T00:00:00Z"}}}
        await self._survey(events, progress, {(211, 1): {"total": 1}})
        await backfill.apply(self.user_id)
        before = await distrakt.load_month(self.user_id, "2026-02")
        self.assertEqual(before.get("movies") or [], [])

        events.append(_mv_event(72, "Topped Up", 2026, "2026-02-20T00:00:00Z"))
        await self._survey(events, progress, {(211, 1): {"total": 1}})
        await backfill.apply(self.user_id)

        after = await distrakt.load_month(self.user_id, "2026-02")
        self.assertEqual([m["title"] for m in after["movies"]], ["Topped Up"])
        self.assertTrue(after["closed"])
        # its roster is untouched
        self.assertEqual([s["ids"]["trakt"] for s in after["shows"]], [211])

    async def test_a_month_emptied_by_hand_can_be_built_again(self):
        """Taking the last row off a month leaves the MONTH behind, so judging
        "already tracked" by the month row meant a month you had cleared out
        could never be refilled — and clearing one out is exactly how someone
        asks for it to be built again."""
        events = [_ep_event(212, 1, 1, "2026-01-10T00:00:00Z", title="January Show")]
        progress = {212: {1: {1: "2026-01-10T00:00:00Z"}}}
        await self._survey(events, progress, {(212, 1): {"total": 1}})
        await backfill.apply(self.user_id)

        await distrakt.remove_show(
            self.user_id, "2026-01", ItemKey("show", "tmdb", "1112"), 1)
        emptied = await distrakt.load_month(self.user_id, "2026-01")
        self.assertEqual(emptied["shows"], [])  # the month row itself survives

        plan = await self._survey(events, progress, {(212, 1): {"total": 1}})
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(list(plan["months"]), ["2026-01"])

        await backfill.apply(self.user_id)
        again = await distrakt.load_month(self.user_id, "2026-01")
        self.assertEqual([s["ids"]["trakt"] for s in again["shows"]], [212])
        self.assertTrue(again["closed"])

    async def test_a_month_that_still_holds_something_is_still_protected(self):
        """The other half of the same rule: content is what deserves protecting,
        and a month with any of it left is never rebuilt from a sweep."""
        await distrakt.add_show(self.user_id, "2026-01", {
            "ids": {"trakt": 998, "tmdb": 998, "slug": "kept"}, "season": 1,
            "title": "Kept By Hand"})
        events = [_ep_event(213, 1, 1, "2026-01-10T00:00:00Z")]
        progress = {213: {1: {1: "2026-01-10T00:00:00Z"}}}
        plan = await self._survey(events, progress, {(213, 1): {"total": 1}})

        self.assertEqual(plan["skipped"], ["2026-01"])
        self.assertEqual(plan["months"], {})

    async def test_a_month_created_since_the_survey_is_never_overwritten(self):
        events = [_ep_event(205, 1, 1, "2026-02-10T00:00:00Z")]
        progress = {205: {1: {1: "2026-02-10T00:00:00Z"}}}
        await self._survey(events, progress, {(205, 1): {"total": 1}})

        # Someone (another tab, the tracker itself) creates it in between.
        await distrakt.add_show(self.user_id, "2026-02", {
            "ids": {"trakt": 999, "tmdb": 999, "slug": "first"}, "season": 1,
            "title": "Landed First"})

        written = await backfill.apply(self.user_id)
        self.assertEqual(written["months"], [])
        doc = await distrakt.load_month(self.user_id, "2026-02")
        self.assertEqual([s["title"] for s in doc["shows"]], ["Landed First"])


class ManualCompletedRouteTests(unittest.TestCase):
    """The escape hatch: recording a finished season into a past month by hand,
    for what watch history cannot know — a season watched somewhere that never
    reached Trakt, or a play logged against the wrong date."""
    def setUp(self):
        migrated_db("backfill-route")
        save_settings(Settings(trakt_client_id="id", trakt_access_token="tok"))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(_make_user("tracker"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=7,
            access_token="user-token")))
        session = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _add(self, year, month, season=1, total=6):
        async def fake_season(settings, trakt_id, season_no, fresh=False, client=None):
            return {"total": total, "cadence": "Mon", "premiere": "3/1", "finale": "4/5",
                    "started_airing": True, "finished_airing": True}

        with patch("app.distrakt.routes.fetch_season_detail", side_effect=fake_season), \
             patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/add-completed", json={
                "year": year, "month": month, "season": season,
                "ids": {"trakt": 321, "tmdb": 4321, "slug": "elsewhere"},
                "title": "Watched Elsewhere", "network": "Net"})

    def test_it_records_the_season_as_finished_and_closes_the_month(self):
        resp = self._add(2026, 3)
        self.assertEqual(resp.status_code, 200, resp.text)
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        self.assertTrue(doc["closed"])
        row = doc["shows"][0]
        self.assertEqual((row["bucket"], row["watched"], row["total"]), ("completed", 6, 6))
        self.assertEqual(row["added_by"], distrakt.ADDED_BY_MANUAL)

    def test_the_episode_total_comes_from_trakt_not_the_caller(self):
        """A frozen month's counts are never recomputed, so a wrong total is
        wrong forever and reaches the ranker import as a wrong episode count."""
        async def fake_season(settings, trakt_id, season_no, fresh=False, client=None):
            return {"total": 10}

        with patch("app.distrakt.routes.fetch_season_detail", side_effect=fake_season), \
             patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            self.client.post("/api/distrakt/add-completed", json={
                "year": 2026, "month": 3, "season": 1,
                "ids": {"trakt": 321, "tmdb": 4321},
                "title": "T", "watched": 999, "total": 999})
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        self.assertEqual((doc["shows"][0]["watched"], doc["shows"][0]["total"]), (10, 10))

    def test_it_works_on_a_month_that_was_never_tracked_at_all(self):
        """The no-backfill guard stops month-NAV inventing history; a person
        saying "January had this in it" is the case it never meant to cover."""
        resp = self._add(2026, 1)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("2026-01", asyncio.run(distrakt.list_months(self.user_id)))

    def test_the_current_month_is_refused(self):
        today = date.today()
        resp = self._add(today.year, today.month)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("past month", resp.json()["error"])

    def test_a_season_trakt_has_no_episodes_for_is_refused(self):
        """Recording 0/0 as finished would import as a show with no episodes."""
        resp = self._add(2026, 3, total=0)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(asyncio.run(distrakt.load_month(self.user_id, "2026-03")), None)


class CorrectingAFrozenMonthTests(unittest.TestCase):
    """Taking a row off a month that has already closed.

    The sweep cannot be perfect: watch one episode of a season you finished
    years ago and its last-watched date moves to this month, so it reads as
    finished now. That row does not belong on the month, and a frozen month with
    no way to correct it would be a permanent wrong answer feeding the ranker.
    """
    def setUp(self):
        migrated_db("backfill-frozen")
        save_settings(Settings(trakt_client_id="id", trakt_access_token="tok"))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(_make_user("tracker"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=7,
            access_token="user-token")))
        session = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session)
        self._freeze_march_with(added_by=distrakt.ADDED_BY_HISTORY)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _freeze_march_with(self, added_by):
        asyncio.run(distrakt.add_show(self.user_id, "2026-03", {
            "ids": {"trakt": 601, "tmdb": 601, "slug": "slug-601"},
            "season": 1, "title": "Rewatched One Episode",
            "network": "Net", "media": "show", "watched": 10, "total": 10,
            "bucket": "completed", "added_by": added_by,
        }))
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        doc["closed"] = True
        asyncio.run(distrakt.save_month(self.user_id, doc))

    def _remove(self):
        with patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/remove", json={
                "year": 2026, "month": 3, "key": "show:tmdb:601", "season": 1})

    def test_a_row_can_be_taken_off_a_closed_month(self):
        resp = self._remove()
        self.assertEqual(resp.status_code, 200, resp.text)
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        self.assertEqual(doc["shows"], [])

    def test_the_month_stays_closed(self):
        """Correcting what a month records must not reopen it to bucketing."""
        self._remove()
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        self.assertTrue(doc["closed"])

    def test_it_never_touches_the_calendar(self):
        """A season finished years ago is not something to start hiding from
        today's calendar — even for a row the calendar itself put there."""
        for added_by in (distrakt.ADDED_BY_HISTORY, distrakt.ADDED_BY_CALENDAR, ""):
            with self.subTest(added_by=added_by):
                asyncio.run(db.execute("DELETE FROM distrakt_months WHERE user_id = ?",
                                       (self.user_id,)))
                asyncio.run(db.execute("DELETE FROM distrakt_shows WHERE user_id = ?",
                                       (self.user_id,)))
                self._freeze_march_with(added_by=added_by)
                resp = self._remove()
                self.assertFalse(resp.json()["hidden_on_calendar"])
                self.assertEqual(
                    asyncio.run(calendar_state.not_watching_ids(self.user_id)), set())


class FilmsAreVisibleTests(unittest.TestCase):
    """Films reaching the month PAYLOAD, which is what puts them on the page.

    They were being swept, recorded, counted, snapshotted onto the month and
    imported into Rankings while appearing nowhere on the tracker except buried
    inside the POST 2 copy text — which reads exactly like them not being there.
    """
    def setUp(self):
        migrated_db("backfill-visible")
        save_settings(Settings(trakt_client_id="id", trakt_access_token="tok"))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(_make_user("tracker"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=7,
            access_token="user-token")))
        session = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _freeze_january_with_a_film(self):
        asyncio.run(distrakt.add_show(self.user_id, "2026-01", {
            "ids": {"trakt": 801, "tmdb": 801, "slug": "jan"}, "season": 1,
            "title": "Jan Show", "bucket": "completed", "watched": 4, "total": 4}))
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-01"))
        doc["closed"] = True
        doc["movies"] = [{"title": "The Sixth Sense", "year": 1999,
                          "watched_at": "2026-01-10T00:00:00Z"}]
        asyncio.run(distrakt.save_month(self.user_id, doc))

    def test_a_frozen_month_hands_its_films_to_the_page(self):
        self._freeze_january_with_a_film()
        body = self.client.get("/api/distrakt/month?year=2026&month=1").json()
        self.assertEqual([m["title"] for m in body["movies"]], ["The Sixth Sense"])

    def test_a_month_with_nothing_still_answers_with_an_empty_list(self):
        """The page reads this key unconditionally; a missing one is a crash.

        An empty month has no stored snapshot, so the route builds it LIVE, and
        that path reaches Trakt four separate ways: the month's premieres, recent
        watch progress, the last-activities gate, and the history sweep behind
        it. Every one of them needs patching. Unpatched they went to the real
        Trakt, which 401s, and the transport swallows a 401 — so the empty list
        this asserts on arrived for entirely the wrong reason, and only on a
        machine with a network.
        """
        async def no_premieres(*args, **kwargs):
            return [], None

        async def no_events(settings, start_at=None, limit=100, max_pages=50):
            return []

        with patch("app.calendar.cache.read_month", side_effect=no_premieres), \
             patch("app.providers.trakt.sync.fetch_watched_progress", return_value=[]), \
             patch("app.providers.trakt.sync.fetch_last_activities", return_value={}), \
             patch("app.providers.trakt.sync.fetch_history", side_effect=no_events), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}):
            body = self.client.get("/api/distrakt/month?year=2020&month=5").json()
        self.assertEqual(body["movies"], [])


class AddingAFilmByHandTests(unittest.TestCase):
    """Recording a film the sweep could not know about."""
    def setUp(self):
        migrated_db("backfill-addfilm")
        save_settings(Settings(trakt_client_id="id", trakt_access_token="tok"))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(_make_user("tracker"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=7,
            access_token="user-token")))
        session = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _add(self, watched_on, trakt_id=900, title="Hand Added"):
        with patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/add-movie", json={
                "ids": {"trakt": trakt_id, "tmdb": trakt_id}, "title": title, "year": 2011,
                "watched_on": watched_on, "year_view": 2026, "month_view": 7})

    def test_it_is_recorded_on_the_day_given(self):
        resp = self._add("2026-03-09")
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = asyncio.run(db.fetch_all(
            "SELECT title, watched_at FROM distrakt_movie_watches WHERE user_id = ?",
            (self.user_id,)))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["watched_at"].startswith("2026-03-09"))

    def test_the_month_it_lands_in_follows_the_date_not_the_page(self):
        """Added while looking at July, watched in March: it is March's."""
        self._add("2026-03-09")
        rows = asyncio.run(db.fetch_all(
            "SELECT substr(watched_at, 1, 7) m FROM distrakt_movie_watches WHERE user_id = ?",
            (self.user_id,)))
        self.assertEqual(rows[0]["m"], "2026-03")

    def test_a_closed_month_is_told_about_it(self):
        """A frozen month renders films from its own snapshot, so a film added
        to one has to be written into it or it will never appear."""
        asyncio.run(distrakt.add_show(self.user_id, "2026-03", {
            "ids": {"trakt": 802, "tmdb": 802, "slug": "mar"}, "season": 1,
            "title": "Mar Show"}))
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        doc["closed"] = True
        asyncio.run(distrakt.save_month(self.user_id, doc))

        self._add("2026-03-09", title="Into A Frozen Month")
        after = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        self.assertEqual([m["title"] for m in after["movies"]], ["Into A Frozen Month"])
        self.assertTrue(after["closed"])

    def test_a_day_is_required_and_cannot_be_in_the_future(self):
        """Stamping today's date silently would file it under a month the user
        is not even looking at."""
        for day in ("", "not-a-date", "2099-01-01"):
            with self.subTest(day=day):
                self.assertEqual(self._add(day).status_code, 400)
        self.assertEqual(asyncio.run(db.fetch_all(
            "SELECT 1 FROM distrakt_movie_watches WHERE user_id = ?", (self.user_id,))), [])


class RemovingAFilmTests(unittest.TestCase):
    """Forgetting a film Trakt reported wrongly.

    Unlike a show there is no roster row to take off — the watch record IS the
    film — so this is the only way to correct one.
    """
    def setUp(self):
        migrated_db("backfill-rmfilm")
        save_settings(Settings(trakt_client_id="id", trakt_access_token="tok"))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(_make_user("tracker"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=7,
            access_token="user-token")))
        session = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session)
        asyncio.run(watch_history.record_movie_watches(self.user_id, [
            {"ids": {"trakt": 91, "tmdb": 91}, "title": "Never Watched This", "year": 2011,
             "watched_at": "2026-03-09T12:00:00Z"},
            {"ids": {"trakt": 92, "tmdb": 92}, "title": "Did Watch This", "year": 2012,
             "watched_at": "2026-03-11T12:00:00Z"},
        ]))

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _remove(self, tmdb=91):
        with patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/remove-movie", json={
                "key": f"movie:tmdb:{tmdb}", "year": 2026, "month": 7})

    def test_it_forgets_only_that_film(self):
        self.assertEqual(self._remove().status_code, 200)
        state = asyncio.run(watch_history.load_state(self.user_id))
        self.assertEqual([m["title"] for m in state["movies"].values()], ["Did Watch This"])

    def test_a_closed_month_carrying_it_is_re_snapshotted(self):
        """A frozen month renders films from its own copy, so removing one has
        to rebuild that copy or the film stays on the page for good."""
        asyncio.run(distrakt.add_show(self.user_id, "2026-03", {
            "ids": {"trakt": 803, "tmdb": 803, "slug": "mar"}, "season": 1,
            "title": "Mar Show"}))
        doc = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        doc["closed"] = True
        doc["movies"] = [{"key": "movie:tmdb:91", "title": "Never Watched This",
                          "year": 2011, "watched_at": "2026-03-09T12:00:00Z"}]
        asyncio.run(distrakt.save_month(self.user_id, doc))

        self._remove()
        after = asyncio.run(distrakt.load_month(self.user_id, "2026-03"))
        self.assertEqual([m["title"] for m in after["movies"]], ["Did Watch This"])
        self.assertTrue(after["closed"])
        self.assertEqual([s["ids"]["trakt"] for s in after["shows"]], [803])

    def test_a_film_that_was_never_there_is_a_404(self):
        self.assertEqual(self._remove(tmdb=404).status_code, 404)

    def test_the_removal_survives_an_ordinary_sync(self):
        """The routine sweep only reaches back to where it last synced, so a film
        forgotten from an earlier month is not quietly handed back on next load.
        (An explicit backfill over that range WILL offer it again — Trakt still
        reports it — but only in a plan that has to be confirmed.)"""
        self._remove()

        async def no_events(settings, start_at=None, limit=100, max_pages=50):
            return []

        with patch("app.providers.trakt.sync.fetch_last_activities", return_value={}), \
             patch("app.providers.trakt.sync.fetch_history", side_effect=no_events), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}):
            asyncio.run(watch_history.sync(SETTINGS, self.user_id, today=date(2026, 7, 28)))

        state = asyncio.run(watch_history.load_state(self.user_id))
        self.assertNotIn("movie:tmdb:91", state["movies"])


class BackfillRouteTests(unittest.TestCase):
    """The two-step route pair: the survey writes nothing, and apply writes only
    what the survey worked out."""
    def setUp(self):
        migrated_db("backfill-api")
        save_settings(Settings(trakt_client_id="id", trakt_access_token="tok"))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.user_id = asyncio.run(_make_user("tracker"))
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=7,
            access_token="user-token")))
        session = asyncio.run(auth.create_session(self.user_id))
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _survey(self):
        events = [_ep_event(301, 1, 2, "2026-02-10T00:00:00Z", title="Route Show")]
        progress = {301: {1: {1: "2026-02-01T00:00:00Z", 2: "2026-02-10T00:00:00Z"}}}

        async def fake_history(settings, start_at=None, limit=100, max_pages=50):
            return events

        async def fake_progress(settings, trakt_id, client=None):
            return progress.get(int(trakt_id), {})

        async def fake_season(settings, trakt_id, season, fresh=False, client=None):
            return {"total": 2}

        with patch("app.providers.trakt.sync.fetch_history", side_effect=fake_history), \
             patch("app.providers.trakt.sync.fetch_show_progress_detail", side_effect=fake_progress), \
             patch("app.providers.trakt.detail.fetch_season_detail", side_effect=fake_season):
            return self.client.post("/api/distrakt/backfill/survey",
                                    json={"start": "2026-01", "end": "2026-06"})

    def test_the_survey_reports_without_writing_anything(self):
        resp = self._survey()
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["months"], [{"month": "2026-02", "count": 1,
                                           "titles": ["Route Show S01"],
                                           "movie_count": 0, "movie_titles": [],
                                           "movie_known": 0}])
        self.assertEqual(asyncio.run(distrakt.list_months(self.user_id)), [])

    def test_apply_writes_what_the_survey_found(self):
        self._survey()
        resp = self.client.post("/api/distrakt/backfill/apply", json={})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["months"], ["2026-02"])
        self.assertEqual(asyncio.run(distrakt.list_months(self.user_id)), ["2026-02"])

    def test_apply_without_a_survey_is_refused(self):
        resp = self.client.post("/api/distrakt/backfill/apply", json={})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(asyncio.run(distrakt.list_months(self.user_id)), [])

    def test_a_backwards_range_is_refused(self):
        resp = self.client.post("/api/distrakt/backfill/survey",
                                json={"start": "2026-06", "end": "2026-01"})
        self.assertEqual(resp.status_code, 400)

    def test_the_dialog_is_offered_a_range_that_stops_at_what_is_tracked(self):
        asyncio.run(distrakt.add_show(self.user_id, "2026-07", {
            "ids": {"trakt": 1, "tmdb": 1, "slug": "t"}, "season": 1, "title": "T"}))
        body = self.client.get("/api/distrakt/backfill").json()
        self.assertEqual(body["end"], "2026-06")
        self.assertEqual(body["tracked"], ["2026-07"])


if __name__ == "__main__":
    unittest.main()
