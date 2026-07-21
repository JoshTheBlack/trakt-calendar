"""Unit tests for the distrakt lazy month rollover (BUILD_PLAN Chat 5).

Covers ensure_month's orchestration (freeze prior -> carry forward minus
completed/abandoned -> premieres minus not-watching -> in-progress history) and
the §3 staleness helpers, with every Trakt call + the main-calendar not-watching
store mocked out. No network. The store is pointed at a throwaway data dir via
TRAKT_DATA_DIR (set BEFORE importing app modules).

Run from the repo root:
    ./.venv/Scripts/python.exe -m unittest tests.test_rollover -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_TMP_DATA = tempfile.mkdtemp(prefix="distrakt-rollover-test-")
os.environ["TRAKT_DATA_DIR"] = _TMP_DATA

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app import distrakt  # noqa: E402

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
    401: _detail(10, cadence="Tue", started=True, finished=False),         # history in-progress
}

# Watched-episode counts (the live `x`).
_WATCHED = {(101, 1): 2, (102, 1): 8, (103, 1): 0, (401, 1): 3}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    return dict(_DETAILS[int(trakt_id)], season=int(season))


async def _fake_sync_and_baseline(settings, roster_trakt_ids, force=False, today=None):
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


class RolloverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Fresh store per test.
        for f in distrakt.DISTRAKT_DIR.glob("*.json") if distrakt.DISTRAKT_DIR.exists() else []:
            f.unlink()

    def _keys(self, doc):
        return {(s["trakt_id"], s["season"]) for s in doc["shows"]}

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
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.state.load_state", return_value={"notWatching": []}):
            doc = await distrakt.ensure_month(2026, 8, SETTINGS)

        self.assertFalse(doc["closed"])
        self.assertEqual(self._keys(doc), {(201, 1), (301, 2), (401, 1)})
        self.assertIsNotNone(doc["totals_refreshed_at"])

    async def test_not_watching_premiere_excluded(self):
        """A premiere toggled not-watching BEFORE commit is simply excluded (§5)."""
        async def fake_calendar(endpoint, settings, year, month):
            if endpoint.key == "shows/new":
                return [_cal_item(201, 1, "New One"), _cal_item(202, 1, "Skip Me")]
            return [_cal_item(201, 1, "New One"), _cal_item(202, 1, "Skip Me")]

        async def fake_progress(settings, since_days=60):
            return []

        # not-watching stores the calendar card id = slug (preferred) or trakt id.
        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.state.load_state", return_value={"notWatching": ["slug-202"]}):
            doc = await distrakt.ensure_month(2026, 8, SETTINGS)

        self.assertEqual(self._keys(doc), {(201, 1)})

    async def test_rollover_freezes_prior_and_drops_completed_abandoned(self):
        """July -> August: freeze July, carry active only, add August premieres."""
        # Seed an OPEN July with an active, a completed, and an abandoned show.
        distrakt.add_show("2026-07", {"trakt_id": 101, "season": 1, "title": "Active", "network": "Net", "slug": "slug-101"})
        distrakt.add_show("2026-07", {"trakt_id": 102, "season": 1, "title": "Done", "network": "Net", "slug": "slug-102"})
        distrakt.add_show("2026-07", {"trakt_id": 103, "season": 1, "title": "Gone", "network": "Net", "slug": "slug-103"})
        distrakt.set_abandoned("2026-07", 103, 1, True, abandoned_form="`Gone S01 (0/6)`")

        async def fake_calendar(endpoint, settings, year, month):
            if endpoint.key == "shows/new":
                return [_cal_item(201, 1, "Aug New")]
            return [_cal_item(201, 1, "Aug New")]

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline), \
             patch("app.state.load_state", return_value={"notWatching": []}):
            # today >= Aug 1 -> August has begun, so July freezes on this access.
            aug = await distrakt.ensure_month(2026, 8, SETTINGS, today=date(2026, 8, 1))

        # July is now frozen with buckets persisted.
        july = distrakt.load_month("2026-07")
        self.assertTrue(july["closed"])
        self.assertIsNotNone(july["totals_refreshed_at"])
        by_id = {s["trakt_id"]: s for s in july["shows"]}
        self.assertEqual(by_id[101]["bucket"], "keepup")
        self.assertEqual(by_id[102]["bucket"], "completed")
        self.assertEqual(by_id[103]["bucket"], "abandoned")
        self.assertIn("started_airing", by_id[101])  # frozen snapshot renders offline

        # August: dropped 102 (completed) + 103 (abandoned); kept 101; added 201.
        self.assertEqual(self._keys(aug), {(101, 1), (201, 1)})
        carried = next(s for s in aug["shows"] if s["trakt_id"] == 101)
        self.assertFalse(carried["abandoned"])
        self.assertIsNone(carried["abandoned_form"])

    async def test_frozen_month_returns_untouched(self):
        """An already-closed month is returned as-is with no Trakt calls."""
        distrakt.add_show("2026-05", {"trakt_id": 900, "season": 1, "title": "X", "slug": "slug-900"})
        doc = distrakt.load_month("2026-05")
        doc["closed"] = True
        distrakt.save_month(doc)
        with patch("app.trakt.fetch_calendar", new=AsyncMock()) as cal, \
             patch("app.trakt.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(2026, 5, SETTINGS)
        self.assertTrue(out["closed"])
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_unconfigured_month_not_persisted(self):
        out = await distrakt.ensure_month(2026, 9, SimpleNamespace(configured=False))
        self.assertEqual(out["shows"], [])
        self.assertIsNone(distrakt.load_month("2026-09"))  # nothing written to disk

    async def test_backward_nav_does_not_backfill(self):
        """Navigating to a never-tracked PAST month must NOT create it (§6)."""
        distrakt.add_show("2026-08", {"trakt_id": 700, "season": 1, "title": "Seed", "slug": "slug-700"})
        # July is earlier than the only tracked month (Aug) -> blocked.
        self.assertTrue(distrakt.is_backfill_blocked("2026-07"))
        with patch("app.trakt.fetch_calendar", new=AsyncMock()) as cal, \
             patch("app.trakt.fetch_watched_progress", new=AsyncMock()) as prog:
            out = await distrakt.ensure_month(2026, 7, SETTINGS)
        self.assertEqual(out["shows"], [])
        self.assertIsNone(distrakt.load_month("2026-07"))  # not written
        cal.assert_not_called()
        prog.assert_not_called()

    async def test_preview_does_not_freeze_prior(self):
        """Accessing August BEFORE Aug 1 must NOT freeze July (still current)."""
        distrakt.add_show("2026-07", {"trakt_id": 101, "season": 1, "title": "Active", "network": "Net", "slug": "slug-101"})

        async def fake_calendar(endpoint, settings, year, month):
            return []  # no August premieres in this scenario

        async def fake_progress(settings, since_days=60):
            return []

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.trakt.fetch_watched_progress", side_effect=fake_progress), \
             patch("app.trakt.fetch_season_detail", side_effect=_fake_season_detail), \
             patch("app.watch_history.sync_and_baseline", side_effect=_fake_sync_and_baseline), \
             patch("app.state.load_state", return_value={"notWatching": []}):
            aug = await distrakt.ensure_month(2026, 8, SETTINGS, today=date(2026, 7, 20))

        july = distrakt.load_month("2026-07")
        self.assertFalse(july["closed"])          # July stays open during preview
        self.assertEqual(self._keys(aug), {(101, 1)})  # still carried forward live

    async def test_import_premieres_merges_skipping_existing_and_not_watching(self):
        distrakt.add_show("2026-08", {"trakt_id": 201, "season": 1, "title": "Already", "slug": "slug-201"})

        async def fake_calendar(endpoint, settings, year, month):
            return [_cal_item(201, 1, "Already"), _cal_item(202, 1, "Fresh"), _cal_item(203, 1, "Skip")]

        with patch("app.trakt.fetch_calendar", side_effect=fake_calendar), \
             patch("app.state.load_state", return_value={"notWatching": ["slug-203"]}):
            await distrakt.import_premieres("2026-08", SETTINGS)

        keys = {(s["trakt_id"], s["season"]) for s in distrakt.load_month("2026-08")["shows"]}
        self.assertEqual(keys, {(201, 1), (202, 1)})  # 201 not duplicated, 203 excluded

    def test_remove_show(self):
        distrakt.add_show("2026-06", {"trakt_id": 55, "season": 1, "title": "Oops", "slug": "slug-55"})
        distrakt.add_show("2026-06", {"trakt_id": 66, "season": 2, "title": "Keep", "slug": "slug-66"})
        self.assertTrue(distrakt.remove_show("2026-06", 55, 1))
        self.assertFalse(distrakt.remove_show("2026-06", 55, 1))  # already gone
        self.assertEqual(
            {(s["trakt_id"], s["season"]) for s in distrakt.load_month("2026-06")["shows"]},
            {(66, 2)},
        )


class CanInitializeTests(unittest.TestCase):
    def setUp(self):
        for f in distrakt.DISTRAKT_DIR.glob("*.json") if distrakt.DISTRAKT_DIR.exists() else []:
            f.unlink()

    def test_empty_store_seeds_anything(self):
        self.assertTrue(distrakt.can_initialize("2026-07"))
        self.assertFalse(distrakt.is_backfill_blocked("2026-07"))

    def test_forward_allowed_backward_blocked(self):
        distrakt.save_month(distrakt.new_month_doc("2026-08"))
        self.assertTrue(distrakt.can_initialize("2026-09"))   # forward
        self.assertTrue(distrakt.can_initialize("2027-01"))   # forward (year wrap)
        self.assertFalse(distrakt.can_initialize("2026-07"))  # backward
        self.assertFalse(distrakt.can_initialize("2026-08"))  # already latest
        self.assertTrue(distrakt.is_backfill_blocked("2026-07"))
        self.assertFalse(distrakt.is_backfill_blocked("2026-09"))


class MonthCommittedTests(unittest.TestCase):
    def test_committed_boundary(self):
        self.assertFalse(distrakt.month_committed("2026-08", date(2026, 7, 31)))
        self.assertTrue(distrakt.month_committed("2026-08", date(2026, 8, 1)))
        self.assertTrue(distrakt.month_committed("2026-08", date(2026, 9, 15)))
        self.assertFalse(distrakt.month_committed("2027-01", date(2026, 12, 31)))
        self.assertTrue(distrakt.month_committed("2027-01", date(2027, 1, 1)))


class StalenessTests(unittest.TestCase):
    def test_missing_timestamp_is_stale(self):
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": None}))
        self.assertTrue(distrakt.is_stale({}))

    def test_recent_is_fresh(self):
        now = datetime.now(timezone.utc).isoformat()
        self.assertFalse(distrakt.is_stale({"totals_refreshed_at": now}))

    def test_old_is_stale(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": old}))

    def test_naive_timestamp_tolerated(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).replace(tzinfo=None).isoformat()
        self.assertTrue(distrakt.is_stale({"totals_refreshed_at": old}))


if __name__ == "__main__":
    unittest.main()
