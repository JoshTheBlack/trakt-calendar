"""Unit tests for the per-user incremental watch-history cache
(app/watch_history).

Pure state folders/readers are tested directly on the in-memory state dict (they
are unchanged by the move to per-user storage); the gated `sync` is tested with
the three Trakt calls mocked, against a throwaway SQLite file. No network.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_watch_history -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="distrakt-wh-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app import watch_history as wh  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
SETTINGS = SimpleNamespace(configured=True)


def _ep_event(tid, season, number, watched_at="2026-07-10T00:00:00.000Z"):
    return {"type": "episode", "watched_at": watched_at,
            "show": {"ids": {"trakt": tid}}, "episode": {"season": season, "number": number}}


def _mv_event(tid, title, year, watched_at):
    return {"type": "movie", "watched_at": watched_at,
            "movie": {"title": title, "year": year, "ids": {"trakt": tid}}}


async def _make_user(username: str) -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


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


class WatchStateTestCase(unittest.IsolatedAsyncioTestCase):
    _counter = 0

    async def asyncSetUp(self):
        WatchStateTestCase._counter += 1
        db.set_db_path(TMP / f"wh-{WatchStateTestCase._counter}.db")
        await db.migrate()
        self.user_id = await _make_user("viewer")

    async def asyncTearDown(self):
        db.close_thread_connection()


class StorageRoundTripTests(WatchStateTestCase):
    async def test_empty_user_gets_the_default_state(self):
        self.assertEqual(await wh._load(self.user_id), wh._default_state())

    async def test_round_trip_preserves_shows_movies_and_beacons(self):
        state = {
            "last_synced": "2026-07-20",
            "beacons": {"ep_watched": "T1", "ep_removed": None,
                        "mv_watched": "T1", "mv_removed": None},
            "shows": {"101": {"1": [1, 2, 3], "2": [1]}},
            "movies": {"9": {"title": "M", "year": 2026, "watched_at": "2026-07-05T00:00:00Z"}},
        }
        await wh._save(self.user_id, state)
        back = await wh._load(self.user_id)
        self.assertEqual(back, state)

    async def test_save_replaces_rather_than_accumulates(self):
        await wh._save(self.user_id, {"last_synced": "a", "beacons": None,
                                      "shows": {"1": {"1": [1]}}, "movies": {}})
        await wh._save(self.user_id, {"last_synced": "b", "beacons": None,
                                      "shows": {"2": {"1": [5]}}, "movies": {}})
        back = await wh._load(self.user_id)
        self.assertEqual(back["shows"], {"2": {"1": [5]}})
        self.assertEqual(back["last_synced"], "b")

    async def test_two_users_keep_independent_watch_state(self):
        other = await _make_user("other")
        await wh._save(self.user_id, {"last_synced": "mine", "beacons": None,
                                      "shows": {"101": {"1": [1, 2]}},
                                      "movies": {"1": {"title": "Mine", "year": 2026,
                                                       "watched_at": "2026-07-01T00:00:00Z"}}})
        await wh._save(other, {"last_synced": "theirs", "beacons": None,
                               "shows": {"202": {"1": [9]}}, "movies": {}})
        mine, theirs = await wh._load(self.user_id), await wh._load(other)
        self.assertEqual(wh.watched_map(mine), {(101, 1): 2})
        self.assertEqual(wh.watched_map(theirs), {(202, 1): 1})
        self.assertEqual(mine["last_synced"], "mine")
        self.assertEqual(theirs["last_synced"], "theirs")
        self.assertEqual(theirs["movies"], {})


class SyncTests(WatchStateTestCase):
    async def test_gate_skips_history_when_unchanged(self):
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        # First sync establishes the beacon + last_synced.
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history", return_value=[_ep_event(101, 1, 1)]) as hist, \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            # Seed a baselined show so the episode event is applied.
            st = await wh._load(self.user_id)
            st["shows"]["101"] = {"1": []}
            await wh._save(self.user_id, st)
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
            self.assertEqual(hist.call_count, 1)

        # Second sync with the SAME beacon -> no history pull.
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history", return_value=[]) as hist2, \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
            hist2.assert_not_called()

    async def test_change_applies_history_delta(self):
        await wh._save(self.user_id, {"shows": {"101": {"1": [1]}}, "movies": {},
                                      "last_synced": "2026-07-01",
                                      "beacons": {"ep_watched": "OLD"}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history",
                   return_value=[_ep_event(101, 1, 2),
                                 _mv_event(9, "M", 2026, "2026-07-05T00:00:00Z")]), \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        self.assertEqual(state["shows"]["101"]["1"], [1, 2])
        self.assertIn("9", state["movies"])
        # and it was persisted under this user, not just returned
        self.assertEqual((await wh._load(self.user_id))["shows"]["101"]["1"], [1, 2])

    async def test_sync_is_scoped_to_one_user(self):
        """Another user's sync must not fold events into this user's cache."""
        other = await _make_user("other")
        await wh._save(self.user_id, {"shows": {"101": {"1": [1]}}, "movies": {},
                                      "last_synced": "2026-07-01", "beacons": None})
        await wh._save(other, {"shows": {"101": {"1": []}}, "movies": {},
                               "last_synced": "2026-07-01", "beacons": None})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.trakt.fetch_last_activities", return_value=la), \
             patch("app.trakt.fetch_history", return_value=[_ep_event(101, 1, 7)]), \
             patch("app.trakt.fetch_show_progress_detail", return_value={}):
            await wh.sync(SETTINGS, other, today=date(2026, 7, 20))
        self.assertEqual((await wh._load(other))["shows"]["101"]["1"], [7])
        self.assertEqual((await wh._load(self.user_id))["shows"]["101"]["1"], [1])

    async def test_baseline_show_lands_on_the_named_user(self):
        other = await _make_user("other")
        with patch("app.trakt.fetch_show_progress_detail", return_value={1: [1, 2, 3]}):
            await wh.baseline_show(SETTINGS, self.user_id, 404)
        self.assertEqual(wh.watched_map(await wh._load(self.user_id)), {(404, 1): 3})
        self.assertEqual(wh.watched_map(await wh._load(other)), {})


if __name__ == "__main__":
    unittest.main()
