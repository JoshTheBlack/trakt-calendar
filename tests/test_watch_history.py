"""Unit tests for the incremental watch-history cache (app/watch_history).

Pure state folders/readers are tested directly; the gated `sync` is tested with
the three Trakt calls mocked. No network. TRAKT_DATA_DIR points at a temp dir
(set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_watch_history -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="distrakt-wh-test-")
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from app import watch_history as wh  # noqa: E402

SETTINGS = SimpleNamespace(configured=True)


def _ep_event(tid, season, number, watched_at="2026-07-10T00:00:00.000Z"):
    return {"type": "episode", "watched_at": watched_at,
            "show": {"ids": {"trakt": tid}}, "episode": {"season": season, "number": number}}


def _mv_event(tid, title, year, watched_at):
    return {"type": "movie", "watched_at": watched_at,
            "movie": {"title": title, "year": year, "ids": {"trakt": tid}}}


class PureStateTests(unittest.TestCase):
    def test_watched_map_counts_len(self):
        state = {"shows": {"101": {"1": [1, 2, 3], "2": [1]}}}
        self.assertEqual(wh.watched_map(state), {(101, 1): 3, (101, 2): 1})

    def test_apply_episode_dedups_and_skips_untracked(self):
        state = {"shows": {"101": {"1": [1, 2]}}}
        wh._apply_episode(state, 101, 1, 2)   # already known -> no change
        wh._apply_episode(state, 101, 1, 3)   # new -> added
        wh._apply_episode(state, 999, 1, 1)   # untracked show -> ignored
        self.assertEqual(state["shows"]["101"]["1"], [1, 2, 3])
        self.assertNotIn("999", state["shows"])

    def test_apply_episode_new_season_on_tracked_show(self):
        state = {"shows": {"101": {"1": [1]}}}
        wh._apply_episode(state, 101, 2, 1)
        self.assertEqual(state["shows"]["101"]["2"], [1])

    def test_apply_movie_keeps_latest_watched_at(self):
        state = {"movies": {}}
        wh._apply_movie(state, 5, "Film", 2025, "2026-07-01T00:00:00Z")
        wh._apply_movie(state, 5, "Film", 2025, "2026-07-09T00:00:00Z")  # later -> wins
        wh._apply_movie(state, 5, "Film", 2025, "2026-06-01T00:00:00Z")  # earlier -> ignored
        self.assertEqual(state["movies"]["5"]["watched_at"], "2026-07-09T00:00:00Z")

    def test_apply_event_dispatch(self):
        state = {"shows": {"101": {"1": []}}, "movies": {}}
        wh._apply_event(state, _ep_event(101, 1, 4))
        wh._apply_event(state, _mv_event(7, "Movie", 2024, "2026-07-15T00:00:00Z"))
        self.assertEqual(state["shows"]["101"]["1"], [4])
        self.assertEqual(state["movies"]["7"]["title"], "Movie")

    def test_movies_in_range(self):
        state = {"movies": {
            "1": {"title": "Jul", "year": 2026, "watched_at": "2026-07-15T00:00:00Z"},
            "2": {"title": "Jun", "year": 2026, "watched_at": "2026-06-30T00:00:00Z"},
            "3": {"title": "Aug", "year": 2026, "watched_at": "2026-08-01T00:00:00Z"},
        }}
        got = {m["title"] for m in wh.movies_in_range(state, "2026-07-01", "2026-07-31")}
        self.assertEqual(got, {"Jul"})

    def test_month_bounds(self):
        self.assertEqual(wh.month_bounds("2026-07"), ("2026-07-01", "2026-07-31"))
        self.assertEqual(wh.month_bounds("2026-02"), ("2026-02-01", "2026-02-28"))

    def test_removed_changed(self):
        old = {"ep_removed": "a", "mv_removed": "b"}
        self.assertFalse(wh._removed_changed(old, {"ep_removed": "a", "mv_removed": "b"}))
        self.assertTrue(wh._removed_changed(old, {"ep_removed": "z", "mv_removed": "b"}))
        self.assertFalse(wh._removed_changed(None, {"ep_removed": "z"}))  # first run, not a removal


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        wh._save(wh._default_state())

    async def test_gate_skips_history_when_unchanged(self):
        la = {"episodes": {"watched_at": "T1", "removed_at": None}, "movies": {"watched_at": "T1", "removed_at": None}}
        # First sync establishes the beacon + last_synced.
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history", return_value=[_ep_event(101, 1, 1)]) as hist, \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            # Seed a baselined show so the episode event is applied.
            st = wh._load(); st["shows"]["101"] = {"1": []}; wh._save(st)
            await wh.sync(SETTINGS, today=date(2026, 7, 20))
            self.assertEqual(hist.call_count, 1)

        # Second sync with the SAME beacon -> no history pull.
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history", return_value=[]) as hist2, \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            await wh.sync(SETTINGS, today=date(2026, 7, 20))
            hist2.assert_not_called()

    async def test_change_applies_history_delta(self):
        wh._save({"shows": {"101": {"1": [1]}}, "movies": {}, "last_synced": "2026-07-01", "beacons": {"ep_watched": "OLD"}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None}, "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history", return_value=[_ep_event(101, 1, 2), _mv_event(9, "M", 2026, "2026-07-05T00:00:00Z")]), \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            state = await wh.sync(SETTINGS, today=date(2026, 7, 20))
        self.assertEqual(state["shows"]["101"]["1"], [1, 2])
        self.assertIn("9", state["movies"])


if __name__ == "__main__":
    unittest.main()
