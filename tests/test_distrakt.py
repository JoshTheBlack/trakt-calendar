"""Unit tests for the distrakt data layer.

Covers the correctness-critical parts: season cadence + premiere/finale
detection (binge vs weekly vs unknown-date tail) and the per-user store round
trip against distrakt_months + distrakt_shows. No network — _derive_season is
pure, and the store runs against a throwaway SQLite file per test.

Run from the repo root:
    ./.venv/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Isolate the data dir on disk before app.config binds DATA_DIR at import time.
_TMP_DATA = tempfile.mkdtemp(prefix="distrakt-test-")
os.environ["TRAKT_DATA_DIR"] = _TMP_DATA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, distrakt  # noqa: E402
from app.trakt import _derive_season  # noqa: E402

TMP = Path(_TMP_DATA)
UTC = ZoneInfo("UTC")
NOW = datetime(2026, 8, 1, tzinfo=UTC)  # fixed "today" so started/finished is stable


def _ep(number: int, iso_date: str | None):
    """A minimal Trakt season episode. iso_date=None => air date unknown."""
    return {"number": number, "first_aired": f"{iso_date}T18:00:00.000Z" if iso_date else None}


async def make_user(username: str) -> int:
    """A distrakt-approved account to hang tracker rows off. The rows are keyed by
    user_id with a real foreign key, so the row has to exist."""
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


class DistraktTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh database + one distrakt user per test."""
    _counter = 0

    async def asyncSetUp(self):
        DistraktTestCase._counter += 1
        db.set_db_path(TMP / f"distrakt-{DistraktTestCase._counter}.db")
        await db.migrate()
        self.user_id = await make_user("tracker")

    async def asyncTearDown(self):
        db.close_thread_connection()


class DeriveSeasonTests(unittest.TestCase):
    def test_binge_all_same_date(self):
        eps = [_ep(n, "2026-07-10") for n in range(1, 9)]  # 8 eps, one drop date
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 8)
        self.assertEqual(res["cadence"], "b")
        self.assertEqual(res["premiere"], "7/10")
        self.assertEqual(res["finale"], "7/10")
        self.assertTrue(res["started_airing"])
        self.assertTrue(res["finished_airing"])

    def test_weekly_fully_scheduled(self):
        # 2026-07-05 is a Sunday; +7d steps keep the same weekday.
        eps = [_ep(1, "2026-07-05"), _ep(2, "2026-07-12"),
               _ep(3, "2026-07-19"), _ep(4, "2026-07-26")]
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 4)
        self.assertEqual(res["cadence"], "Sun")
        self.assertEqual(res["premiere"], "7/5")
        self.assertEqual(res["finale"], "7/26")
        self.assertTrue(res["started_airing"])
        self.assertTrue(res["finished_airing"])
        self.assertNotEqual(res["cadence"], "b")

    def test_weekly_unknown_tail(self):
        # Two aired, two announced-but-unscheduled -> finale unknown ("?/?").
        eps = [_ep(1, "2026-07-05"), _ep(2, "2026-07-12"), _ep(3, None), _ep(4, None)]
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 4)          # y counts undated eps too
        self.assertEqual(res["cadence"], "Sun")    # still weekly, from known dates
        self.assertEqual(res["premiere"], "7/5")
        self.assertIsNone(res["finale"])           # renderer shows "?/?"
        self.assertTrue(res["started_airing"])
        self.assertFalse(res["finished_airing"])   # no known finale -> not finished

    def test_no_dates_known(self):
        eps = [_ep(1, None), _ep(2, None)]
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["total"], 2)
        self.assertIsNone(res["cadence"])
        self.assertIsNone(res["premiere"])
        self.assertIsNone(res["finale"])
        self.assertFalse(res["started_airing"])

    def test_not_yet_started_future_premiere(self):
        eps = [_ep(1, "2026-09-06"), _ep(2, "2026-09-13")]  # after NOW
        res = _derive_season(eps, UTC, now=NOW)
        self.assertEqual(res["premiere"], "9/6")
        self.assertFalse(res["started_airing"])
        self.assertFalse(res["finished_airing"])

    def test_empty_season(self):
        res = _derive_season([], UTC, now=NOW)
        self.assertEqual(res["total"], 0)
        self.assertIsNone(res["cadence"])
        self.assertIsNone(res["premiere"])


class StoreTests(DistraktTestCase):
    async def test_add_show_creates_and_reads_back(self):
        self.assertIsNone(await distrakt.load_month(self.user_id, "2026-07"))
        await distrakt.add_show(self.user_id, "2026-07", {
            "trakt_id": 12345, "slug": "the-westies", "title": "The Westies",
            "season": 1, "network": "MGM+", "watched": 0, "total": 8,
            "cadence": "Sun", "premiere": "7/12", "finale": "8/23", "bucket": "returning",
        })
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertIsNotNone(doc)
        self.assertEqual(doc["month"], "2026-07")
        self.assertFalse(doc["closed"])
        self.assertEqual(len(doc["shows"]), 1)
        rec = doc["shows"][0]
        self.assertEqual(rec["trakt_id"], 12345)
        self.assertEqual(rec["season"], 1)
        self.assertEqual(rec["total"], 8)
        self.assertEqual(rec["cadence"], "Sun")
        self.assertFalse(rec["abandoned"])
        self.assertIsNone(rec["abandoned_form"])

    async def test_add_show_upserts_by_id_and_season(self):
        await distrakt.add_show(self.user_id, "2026-07",
                                {"trakt_id": 1, "season": 2, "title": "X", "watched": 3, "total": 10})
        await distrakt.add_show(self.user_id, "2026-07",
                                {"trakt_id": 1, "season": 2, "watched": 7, "total": 12})
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual(len(doc["shows"]), 1)          # updated, not duplicated
        self.assertEqual(doc["shows"][0]["watched"], 7)
        self.assertEqual(doc["shows"][0]["total"], 12)
        self.assertEqual(doc["shows"][0]["title"], "X")  # untouched column preserved

    async def test_different_season_is_a_new_record(self):
        await distrakt.add_show(self.user_id, "2026-07", {"trakt_id": 1, "season": 1, "title": "X"})
        await distrakt.add_show(self.user_id, "2026-07", {"trakt_id": 1, "season": 2, "title": "X"})
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertEqual(len(doc["shows"]), 2)

    async def test_set_abandoned_toggle(self):
        await distrakt.add_show(self.user_id, "2026-07", {"trakt_id": 9, "season": 1, "title": "Bel-Air"})
        rec = await distrakt.set_abandoned(self.user_id, "2026-07", 9, 1, True,
                                           abandoned_form="`Bel-Air S01 (0/6)`")
        self.assertIsNotNone(rec)
        self.assertTrue(rec["abandoned"])
        self.assertEqual(rec["abandoned_form"], "`Bel-Air S01 (0/6)`")
        # Persisted
        doc = await distrakt.load_month(self.user_id, "2026-07")
        self.assertTrue(doc["shows"][0]["abandoned"])
        # Un-abandon clears the frozen form
        rec2 = await distrakt.set_abandoned(self.user_id, "2026-07", 9, 1, False)
        self.assertFalse(rec2["abandoned"])
        self.assertIsNone(rec2["abandoned_form"])

    async def test_set_abandoned_missing_returns_none(self):
        self.assertIsNone(await distrakt.set_abandoned(self.user_id, "2026-07", 404, 1, True))  # no month
        await distrakt.add_show(self.user_id, "2026-07", {"trakt_id": 1, "season": 1})
        self.assertIsNone(await distrakt.set_abandoned(self.user_id, "2026-07", 999, 1, True))  # no such show

    async def test_list_months_sorted(self):
        await distrakt.add_show(self.user_id, "2026-08", {"trakt_id": 1, "season": 1})
        await distrakt.add_show(self.user_id, "2026-07", {"trakt_id": 1, "season": 1})
        await distrakt.add_show(self.user_id, "2026-12", {"trakt_id": 1, "season": 1})
        self.assertEqual(await distrakt.list_months(self.user_id), ["2026-07", "2026-08", "2026-12"])

    async def test_invalid_month_rejected(self):
        with self.assertRaises(ValueError):
            await distrakt.load_month(self.user_id, "2026-13")
        with self.assertRaises(ValueError):
            await distrakt.add_show(self.user_id, "July", {"trakt_id": 1, "season": 1})

    async def test_remove_show(self):
        await distrakt.add_show(self.user_id, "2026-06", {"trakt_id": 55, "season": 1, "title": "Oops"})
        await distrakt.add_show(self.user_id, "2026-06", {"trakt_id": 66, "season": 2, "title": "Keep"})
        self.assertTrue(await distrakt.remove_show(self.user_id, "2026-06", 55, 1))
        self.assertFalse(await distrakt.remove_show(self.user_id, "2026-06", 55, 1))  # already gone
        doc = await distrakt.load_month(self.user_id, "2026-06")
        self.assertEqual({(s["trakt_id"], s["season"]) for s in doc["shows"]}, {(66, 2)})

    async def test_frozen_snapshot_columns_survive_a_round_trip(self):
        """A frozen month renders offline from these columns alone, so they have to
        come back exactly as written — including the airing flags and the
        month-level movies snapshot."""
        doc = distrakt.new_month_doc("2026-05")
        doc["shows"] = [{
            "trakt_id": 7, "tmdb": 42, "slug": "s", "media": "show", "title": "T",
            "season": 3, "network": "N", "abandoned": False, "abandoned_form": None,
            "watched": 5, "total": 8, "cadence": "Tue", "premiere": "5/1",
            "finale": "5/29", "bucket": "keepup",
            "started_airing": True, "finished_airing": False,
        }]
        doc["closed"] = True
        doc["totals_refreshed_at"] = db.now()
        doc["movies"] = [{"title": "A Film", "year": 2026, "watched_at": "2026-05-04T00:00:00Z"}]
        await distrakt.save_month(self.user_id, doc)

        back = await distrakt.load_month(self.user_id, "2026-05")
        self.assertTrue(back["closed"])
        self.assertEqual(back["movies"], doc["movies"])
        rec = back["shows"][0]
        self.assertTrue(rec["started_airing"])
        self.assertFalse(rec["finished_airing"])
        self.assertEqual(rec["bucket"], "keepup")
        self.assertEqual(rec["tmdb"], 42)
        # frozen_shows reads them straight back with no Trakt call.
        self.assertTrue(distrakt.frozen_shows(back)[0]["started_airing"])


if __name__ == "__main__":
    unittest.main()
