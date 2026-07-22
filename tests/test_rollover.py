"""Unit tests for the distrakt lazy month rollover, per user.

Covers ensure_month's orchestration (freeze prior -> carry forward minus
completed/abandoned -> premieres minus not-watching -> in-progress history) and
the staleness helpers, with every Trakt call mocked out. The main-calendar
not-watching overlay is NOT mocked: real rows go into calendar_not_watching
through app/calendar_state, since "the roster is built from this user's own
calendar decisions" is exactly the wiring worth proving.

Also covers the per-user guarantees the shared-document model could not make:
two users' rosters are fully independent in both directions, and the prior-month
freeze fires per user independently on first access on/after the 1st.

No network. Each test runs against a throwaway SQLite file.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_rollover -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TMP_DATA = tempfile.mkdtemp(prefix="distrakt-rollover-test-")
os.environ["TRAKT_DATA_DIR"] = _TMP_DATA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import calendar_state, db, distrakt  # noqa: E402

TMP = Path(_TMP_DATA)
SETTINGS = SimpleNamespace(configured=True, network_emojis={}, default_network_emoji=":tv:")


def _detail(total, watched_hint=None, cadence="Mon", premiere="7/6", finale=None,
            started=True, finished=False):
    return {
        "season": 1, "total": total, "cadence": cadence, "premiere": premiere,
        "finale": finale, "started_airing": started, "finished_airing": finished,
        "air_dates": [],
    }


# Per-(trakt_id) season fixtures used by both the freeze pass and history step.
_DETAILS = {
    101: _detail(8, cadence="Mon", started=True, finished=False),           # keepup
    102: _detail(8, started=True, finished=True, finale="7/28"),            # completed (8/8)
    103: _detail(6, started=True),                                          # abandoned (flag wins)
    401: _detail(10, cadence="Tue", started=True, finished=False),          # history in-progress
}

# Watched-episode counts (the live `x`).
_WATCHED = {(101, 1): 2, (102, 1): 8, (103, 1): 0, (401, 1): 3}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    return dict(_DETAILS[int(trakt_id)], season=int(season))


async def _fake_sync_and_baseline(settings, user_id, roster_trakt_ids, force=False, today=None):
    """Stand-in for the watch-history cache: build a state whose watched_map()
    yields _WATCHED (filtered to the requested roster ids)."""
    ids = {int(t) for t in roster_trakt_ids}
    shows: dict = {}
    for (tid, season), cnt in _WATCHED.items():
        if tid in ids:
            shows.setdefault(str(tid), {})[str(season)] = list(range(cnt))
    return {"shows": shows, "movies": {}, "last_synced": "2026-01-01", "beacons": None}


def _cal_item(tid, season, title, network="Net"):
    return {"trakt_id": tid, "trakt_slug": f"slug-{tid}", "title": title,
            "season": season, "network": network}


async def _make_user(username: str) -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


class RolloverTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh database + one distrakt user per test."""
    _counter = 0

    async def asyncSetUp(self):
        RolloverTestCase._counter += 1
        db.set_db_path(TMP / f"rollover-{RolloverTestCase._counter}.db")
        await db.migrate()
        self.user_id = await _make_user("tracker")

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _keys(self, doc):
        return {(s["trakt_id"], s["season"]) for s in doc["shows"]}

    async def _mark_not_watching(self, user_id, year, month, item_id,
                                 endpoint="shows/new"):
        """A real main-calendar "not watching" mark, the same write the calendar
        page makes."""
        await calendar_state.set_not_watching(user_id, endpoint, year, month, item_id, True)


class RolloverTests(RolloverTestCase):
    async def test_first_month_init_from_scratch(self):
        """No prior month: seed premieres (new/returning) + in-progress history."""
        async def fake_calendar(endpoint, settings, year, month):
            if endpoint.key == "shows/new":
                return [_cal_item(201, 1, "New One")]
            return [_cal_item(201, 1, "New One"), _cal_item(301, 2, "Returner")]

        async def fake_progress(settings, since_days=60):
            return [{"trakt_id": 401, "season": 1, "watched": 3,
                     "slug": "slug-401", "title": "Backlog", "network": "Net"}]

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail):
            doc = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS)

        self.assertFalse(doc["closed"])
        self.assertEqual(await self._keys(doc), {(201, 1), (301, 2), (401, 1)})
        self.assertIsNotNone(doc["totals_refreshed_at"])

    async def test_not_watching_premiere_excluded(self):
        """A premiere toggled not-watching BEFORE commit is simply excluded."""
        async def fake_calendar(endpoint, settings, year, month):
            return [_cal_item(201, 1, "New One"), _cal_item(202, 1, "Skip Me")]

        async def fake_progress(settings, since_days=60):
            return []

        # not-watching stores the calendar card id = slug (preferred) or trakt id.
        await self._mark_not_watching(self.user_id, 2026, 8, "slug-202")

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail):
            doc = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS)

        self.assertEqual(await self._keys(doc), {(201, 1)})

    async def test_rollover_freezes_prior_and_drops_completed_abandoned(self):
        """July -> August: freeze July, carry active only, add August premieres."""
        # Seed an OPEN July with an active, a completed, and an abandoned show.
        for tid, title in ((101, "Active"), (102, "Done"), (103, "Gone")):
            await distrakt.add_show(self.user_id, "2026-07", {
                "trakt_id": tid, "season": 1, "title": title,
                "network": "Net", "slug": f"slug-{tid}"})
        await distrakt.set_abandoned(self.user_id, "2026-07", 103, 1, True,
                                     abandoned_form="`Gone S01 (0/6)`")

        async def fake_calendar(endpoint, settings, year, month):
            return [_cal_item(201, 1, "Aug New")]

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            # today >= Aug 1 -> August has begun, so July freezes on this access.
            aug = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=date(2026, 8, 1))

        # July is now frozen with buckets persisted.
        july = await distrakt.load_month(self.user_id, "2026-07")
        self.assertTrue(july["closed"])
        self.assertIsNotNone(july["totals_refreshed_at"])
        by_id = {s["trakt_id"]: s for s in july["shows"]}
        self.assertEqual(by_id[101]["bucket"], "keepup")
        self.assertEqual(by_id[102]["bucket"], "completed")
        self.assertEqual(by_id[103]["bucket"], "abandoned")
        self.assertIn("started_airing", by_id[101])  # frozen snapshot renders offline
        self.assertTrue(by_id[101]["started_airing"])

        # August: dropped 102 (completed) + 103 (abandoned); kept 101; added 201.
        self.assertEqual(await self._keys(aug), {(101, 1), (201, 1)})
        carried = next(s for s in aug["shows"] if s["trakt_id"] == 101)
        self.assertFalse(carried["abandoned"])
        self.assertIsNone(carried["abandoned_form"])

    async def test_frozen_month_returns_untouched(self):
        """An already-closed month is returned as-is with no Trakt calls."""
        await distrakt.add_show(self.user_id, "2026-05",
                                {"trakt_id": 900, "season": 1, "title": "X", "slug": "slug-900"})
        doc = await distrakt.load_month(self.user_id, "2026-05")
        doc["closed"] = True
        await distrakt.save_month(self.user_id, doc)
        with patch("app.trakt.fetch_calendar", new=AsyncMock()) as cal, \
             patch("app.trakt.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(self.user_id, 2026, 5, SETTINGS)
        self.assertTrue(out["closed"])
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_unconfigured_month_not_persisted(self):
        out = await distrakt.ensure_month(self.user_id, 2026, 9, SimpleNamespace(configured=False))
        self.assertEqual(out["shows"], [])
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-09"))  # nothing written

    async def test_backward_nav_does_not_backfill(self):
        """Navigating to a never-tracked PAST month must NOT create it."""
        await distrakt.add_show(self.user_id, "2026-08",
                                {"trakt_id": 700, "season": 1, "title": "Seed", "slug": "slug-700"})
        # July is earlier than the only tracked month (Aug) -> blocked.
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))
        with patch("app.trakt.fetch_calendar", new=AsyncMock()) as cal, \
             patch("app.trakt.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(self.user_id, 2026, 7, SETTINGS)
        self.assertEqual(out["shows"], [])
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-07"))  # not written
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_preview_does_not_freeze_prior(self):
        """Accessing August BEFORE Aug 1 must NOT freeze July (still current)."""
        await distrakt.add_show(self.user_id, "2026-07", {
            "trakt_id": 101, "season": 1, "title": "Active",
            "network": "Net", "slug": "slug-101"})

        async def fake_calendar(endpoint, settings, year, month):
            return []  # no August premieres in this scenario

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline):
            aug = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS,
                                              today=date(2026, 7, 20))

        july = await distrakt.load_month(self.user_id, "2026-07")
        self.assertFalse(july["closed"])                      # stays open during preview
        self.assertEqual(await self._keys(aug), {(101, 1)})   # still carried forward live

    async def test_import_premieres_merges_skipping_existing_and_not_watching(self):
        await distrakt.add_show(self.user_id, "2026-08",
                                {"trakt_id": 201, "season": 1, "title": "Already", "slug": "slug-201"})

        async def fake_calendar(endpoint, settings, year, month):
            return [_cal_item(201, 1, "Already"), _cal_item(202, 1, "Fresh"), _cal_item(203, 1, "Skip")]

        await self._mark_not_watching(self.user_id, 2026, 8, "slug-203")

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar):
            await distrakt.import_premieres(self.user_id, "2026-08", SETTINGS)

        doc = await distrakt.load_month(self.user_id, "2026-08")
        keys = {(s["trakt_id"], s["season"]) for s in doc["shows"]}
        self.assertEqual(keys, {(201, 1), (202, 1)})  # 201 not duplicated, 203 excluded

    async def test_remove_show(self):
        await distrakt.add_show(self.user_id, "2026-06",
                                {"trakt_id": 55, "season": 1, "title": "Oops", "slug": "slug-55"})
        await distrakt.add_show(self.user_id, "2026-06",
                                {"trakt_id": 66, "season": 2, "title": "Keep", "slug": "slug-66"})
        self.assertTrue(await distrakt.remove_show(self.user_id, "2026-06", 55, 1))
        self.assertFalse(await distrakt.remove_show(self.user_id, "2026-06", 55, 1))  # already gone
        doc = await distrakt.load_month(self.user_id, "2026-06")
        self.assertEqual({(s["trakt_id"], s["season"]) for s in doc["shows"]}, {(66, 2)})


class PerUserIsolationTests(RolloverTestCase):
    """The guarantees the shared-document model could not make."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.other_id = await _make_user("other")

    async def test_two_users_rosters_are_independent_in_both_directions(self):
        await distrakt.add_show(self.user_id, "2026-08",
                                {"trakt_id": 111, "season": 1, "title": "Mine", "slug": "slug-111"})
        await distrakt.add_show(self.other_id, "2026-08",
                                {"trakt_id": 222, "season": 1, "title": "Theirs", "slug": "slug-222"})

        mine = await distrakt.load_month(self.user_id, "2026-08")
        theirs = await distrakt.load_month(self.other_id, "2026-08")
        self.assertEqual({s["trakt_id"] for s in mine["shows"]}, {111})
        self.assertEqual({s["trakt_id"] for s in theirs["shows"]}, {222})

        # A mutation on one side is invisible on the other, both ways round.
        await distrakt.set_abandoned(self.user_id, "2026-08", 111, 1, True, abandoned_form="`f`")
        self.assertFalse((await distrakt.load_month(self.other_id, "2026-08"))["shows"][0]["abandoned"])
        self.assertIsNone(await distrakt.set_abandoned(self.other_id, "2026-08", 111, 1, True))

        # Deleting the other user's only show leaves this user's roster intact.
        self.assertTrue(await distrakt.remove_show(self.other_id, "2026-08", 222, 1))
        self.assertFalse(await distrakt.remove_show(self.other_id, "2026-08", 111, 1))
        self.assertEqual(len((await distrakt.load_month(self.user_id, "2026-08"))["shows"]), 1)

    async def test_month_lists_and_backfill_gates_are_per_user(self):
        await distrakt.add_show(self.user_id, "2026-08", {"trakt_id": 1, "season": 1})
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-08"])
        self.assertEqual(await distrakt.list_months(self.other_id), [])
        # A month that is backward/blocked for one user is a clean first seed for
        # the other, whose store is still empty.
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))
        self.assertFalse(await distrakt.is_backfill_blocked(self.other_id, "2026-07"))

    async def test_not_watching_overlay_is_per_user(self):
        """One user's calendar not-watching mark must not drop a premiere from
        anyone else's roster."""
        async def fake_calendar(endpoint, settings, year, month):
            return [_cal_item(201, 1, "Shared Premiere")]

        async def fake_progress(settings, since_days=60):
            return []

        await self._mark_not_watching(self.user_id, 2026, 8, "slug-201")

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail):
            mine = await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS)
            theirs = await distrakt.ensure_month(self.other_id, 2026, 8, SETTINGS)

        self.assertEqual(await self._keys(mine), set())          # excluded for me
        self.assertEqual(await self._keys(theirs), {(201, 1)})   # still there for them

    async def test_freeze_prior_month_fires_per_user_independently(self):
        """Reaching the 1st freezes only the accessing user's prior month; another
        user's July stays open until THEY first access August."""
        for uid in (self.user_id, self.other_id):
            await distrakt.add_show(uid, "2026-07", {
                "trakt_id": 101, "season": 1, "title": "Active",
                "network": "Net", "slug": "slug-101"})

        async def fake_calendar(endpoint, settings, year, month):
            return []

        async def fake_progress(settings, since_days=60):
            return []

        patches = (
            patch("app.trakt.fetch_calendar", side_effect=fake_calendar),
            patch("app.trakt.fetch_watched_progress", side_effect=fake_progress),
            patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail),
            patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            await distrakt.ensure_month(self.user_id, 2026, 8, SETTINGS, today=date(2026, 8, 1))

            self.assertTrue((await distrakt.load_month(self.user_id, "2026-07"))["closed"])
            self.assertFalse((await distrakt.load_month(self.other_id, "2026-07"))["closed"])

            # The other user's own first access on/after the 1st freezes theirs.
            await distrakt.ensure_month(self.other_id, 2026, 8, SETTINGS, today=date(2026, 8, 1))
            self.assertTrue((await distrakt.load_month(self.other_id, "2026-07"))["closed"])


class CanInitializeTests(RolloverTestCase):
    async def test_empty_store_seeds_anything(self):
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2026-07"))
        self.assertFalse(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))

    async def test_forward_allowed_backward_blocked(self):
        await distrakt.save_month(self.user_id, distrakt.new_month_doc("2026-08"))
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2026-09"))   # forward
        self.assertTrue(await distrakt.can_initialize(self.user_id, "2027-01"))   # forward (year wrap)
        self.assertFalse(await distrakt.can_initialize(self.user_id, "2026-07"))  # backward
        self.assertFalse(await distrakt.can_initialize(self.user_id, "2026-08"))  # already latest
        self.assertTrue(await distrakt.is_backfill_blocked(self.user_id, "2026-07"))
        self.assertFalse(await distrakt.is_backfill_blocked(self.user_id, "2026-09"))


class MonthCommittedTests(unittest.TestCase):
    def test_committed_boundary(self):
        self.assertFalse(distrakt.month_committed("2026-08", date(2026, 7, 31)))
        self.assertTrue(distrakt.month_committed("2026-08", date(2026, 8, 1)))
        self.assertTrue(distrakt.month_committed("2026-08", date(2026, 9, 15)))
        self.assertFalse(distrakt.month_committed("2027-01", date(2026, 12, 31)))
        self.assertTrue(distrakt.month_committed("2027-01", date(2027, 1, 1)))


class StalenessTests(unittest.TestCase):
    """totals_refreshed_at is whole UTC seconds, the representation every
    timestamp column in this schema uses."""

    def test_missing_timestamp_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": None}))
        self.assertTrue(distrakt.is_stale({}))
        self.assertTrue(distrakt.is_stale(None))

    def test_recent_is_fresh(self):
        self.assertFalse(distrakt.is_stale({"totals_refreshed_at": db.now()}))

    def test_old_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": db.now() - 25 * 3600}))

    def test_unparseable_timestamp_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": "not-a-number"}))


class StampRefreshedTests(RolloverTestCase):
    async def test_stamp_refreshed_clears_staleness_for_that_user_only(self):
        other = await _make_user("other")
        for uid in (self.user_id, other):
            doc = distrakt.new_month_doc("2026-07")
            doc["totals_refreshed_at"] = db.now() - 30 * 3600
            await distrakt.save_month(uid, doc)

        await distrakt.stamp_refreshed(self.user_id, "2026-07")

        self.assertFalse(distrakt.is_stale(await distrakt.load_month(self.user_id, "2026-07")))
        self.assertTrue(distrakt.is_stale(await distrakt.load_month(other, "2026-07")))


if __name__ == "__main__":
    unittest.main()
