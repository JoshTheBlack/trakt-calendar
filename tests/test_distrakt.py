"""Unit tests for the distrakt data layer (BUILD_PLAN Chat 2).

Covers the correctness-critical parts: season cadence + premiere/finale
detection (binge vs weekly vs unknown-date tail) and the pure JSON store
round-trip. No network — _derive_season is pure, and the store is pointed at a
throwaway data dir via TRAKT_DATA_DIR (set BEFORE importing app modules).

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

# Isolate the store on disk before app.config binds DATA_DIR at import time.
_TMP_DATA = tempfile.mkdtemp(prefix="distrakt-test-")
os.environ["TRAKT_DATA_DIR"] = _TMP_DATA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import distrakt  # noqa: E402
from app.trakt import _derive_season  # noqa: E402

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 8, 1, tzinfo=UTC)  # fixed "today" so started/finished is stable


def _ep(number: int, iso_date: str | None):
    """A minimal Trakt season episode. iso_date=None => air date unknown."""
    return {"number": number, "first_aired": f"{iso_date}T18:00:00.000Z" if iso_date else None}


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
        self.assertEqual(res["total"], 4)          # y counts undated eps too (§3)
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


class StoreTests(unittest.TestCase):
    def setUp(self):
        # Fresh distrakt dir per test.
        import shutil
        if distrakt.DISTRAKT_DIR.exists():
            shutil.rmtree(distrakt.DISTRAKT_DIR)

    def test_add_show_creates_and_reads_back(self):
        self.assertIsNone(distrakt.load_month("2026-07"))
        distrakt.add_show("2026-07", {
            "trakt_id": 12345, "slug": "the-westies", "title": "The Westies",
            "season": 1, "network": "MGM+", "watched": 0, "total": 8,
            "cadence": "Sun", "premiere": "7/12", "finale": "8/23", "bucket": "returning",
        })
        doc = distrakt.load_month("2026-07")
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

    def test_add_show_upserts_by_id_and_season(self):
        distrakt.add_show("2026-07", {"trakt_id": 1, "season": 2, "title": "X", "watched": 3, "total": 10})
        distrakt.add_show("2026-07", {"trakt_id": 1, "season": 2, "watched": 7, "total": 12})
        doc = distrakt.load_month("2026-07")
        self.assertEqual(len(doc["shows"]), 1)          # updated, not duplicated
        self.assertEqual(doc["shows"][0]["watched"], 7)
        self.assertEqual(doc["shows"][0]["total"], 12)
        self.assertEqual(doc["shows"][0]["title"], "X")  # untouched key preserved

    def test_different_season_is_a_new_record(self):
        distrakt.add_show("2026-07", {"trakt_id": 1, "season": 1, "title": "X"})
        distrakt.add_show("2026-07", {"trakt_id": 1, "season": 2, "title": "X"})
        self.assertEqual(len(distrakt.load_month("2026-07")["shows"]), 2)

    def test_set_abandoned_toggle(self):
        distrakt.add_show("2026-07", {"trakt_id": 9, "season": 1, "title": "Bel-Air"})
        rec = distrakt.set_abandoned("2026-07", 9, 1, True, abandoned_form="`Bel-Air S01 (0/6)`")
        self.assertIsNotNone(rec)
        self.assertTrue(rec["abandoned"])
        self.assertEqual(rec["abandoned_form"], "`Bel-Air S01 (0/6)`")
        # Persisted
        self.assertTrue(distrakt.load_month("2026-07")["shows"][0]["abandoned"])
        # Un-abandon clears the frozen form
        rec2 = distrakt.set_abandoned("2026-07", 9, 1, False)
        self.assertFalse(rec2["abandoned"])
        self.assertIsNone(rec2["abandoned_form"])

    def test_set_abandoned_missing_returns_none(self):
        self.assertIsNone(distrakt.set_abandoned("2026-07", 404, 1, True))          # no month
        distrakt.add_show("2026-07", {"trakt_id": 1, "season": 1})
        self.assertIsNone(distrakt.set_abandoned("2026-07", 999, 1, True))          # no such show

    def test_list_months_sorted(self):
        distrakt.add_show("2026-08", {"trakt_id": 1, "season": 1})
        distrakt.add_show("2026-07", {"trakt_id": 1, "season": 1})
        distrakt.add_show("2026-12", {"trakt_id": 1, "season": 1})
        self.assertEqual(distrakt.list_months(), ["2026-07", "2026-08", "2026-12"])

    def test_invalid_month_rejected(self):
        with self.assertRaises(ValueError):
            distrakt.load_month("2026-13")
        with self.assertRaises(ValueError):
            distrakt.add_show("July", {"trakt_id": 1, "season": 1})


if __name__ == "__main__":
    unittest.main()
