"""Unit tests for the per-user incremental watch-history cache
(app/distrakt/watch_history.py).

Pure state folders/readers are tested directly on the in-memory state dict (they
are unchanged by the move to per-user storage); the gated `sync` is tested with
the three Trakt calls mocked, against a throwaway SQLite file. No network.
"""
from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.distrakt import watch_history as wh
from tests.support import new_db_path

SETTINGS = SimpleNamespace(configured=True)


def _ep_event(tid, season, number, watched_at="2026-07-10T00:00:00.000Z"):
    return {"type": "episode", "watched_at": watched_at,
            "show": {"ids": {"trakt": tid, "tmdb": tid}},
            "episode": {"season": season, "number": number}}


def _mv_event(tid, title, year, watched_at):
    return {"type": "movie", "watched_at": watched_at,
            "movie": {"title": title, "year": year, "ids": {"trakt": tid, "tmdb": tid}}}


async def _make_user(username: str) -> int:
    now = db.now()
    result = await db.execute(
        "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
        "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
        (username, now, now),
    )
    return result.lastrowid


def _show(tid, seasons) -> dict:
    """One cached show entry: the ids a refetch is placed with, and the seasons.

    Entries are filed under the SHARED identity (see app/providers/base.py) so
    plays reported by two services fold into one record. `ids` carries ONLY the
    ids a call is placed with, deliberately — the shared one is already the key,
    and a cache row holding it twice is a second copy of the same fact. The
    fixtures give each show a tmdb equal to its Trakt id, so SHOW(tid) names the
    row a fixture with that Trakt id produces.
    """
    return {"ids": {"trakt": tid}, "seasons": seasons}


def SHOW(tid) -> str:
    return f"show:tmdb:{tid}"


def MOVIE(tid) -> str:
    return f"movie:tmdb:{tid}"


class PureStateTests(unittest.TestCase):
    def test_watched_map_counts_len(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": "", "2": "", "3": ""},
                                                 "2": {"1": ""}})}}
        self.assertEqual(wh.watched_map(state), {(SHOW(101), 1): 3, (SHOW(101), 2): 1})

    def test_apply_episode_dedups_and_skips_untracked(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": "", "2": ""}})}}
        wh._apply_episode(state, SHOW(101), 1, 2)   # already known -> no change
        wh._apply_episode(state, SHOW(101), 1, 3)   # new -> added
        wh._apply_episode(state, SHOW(999), 1, 1)   # untracked title -> ignored
        self.assertEqual(sorted(state["shows"][SHOW(101)]["seasons"]["1"]), ["1", "2", "3"])
        self.assertNotIn(SHOW(999), state["shows"])

    def test_apply_episode_keeps_the_latest_date_for_an_episode(self):
        """Same rule as movies. Re-watching a season's last episode this month is
        finishing it this month, and an undated play never erases a date."""
        state = {"shows": {SHOW(101): _show(101, {"1": {}})}}
        wh._apply_episode(state, SHOW(101), 1, 4, "2026-07-01T00:00:00Z")
        wh._apply_episode(state, SHOW(101), 1, 4, "2026-08-09T00:00:00Z")  # later -> wins
        wh._apply_episode(state, SHOW(101), 1, 4, "2026-06-01T00:00:00Z")  # earlier -> ignored
        wh._apply_episode(state, SHOW(101), 1, 4)                          # undated -> ignored
        self.assertEqual(state["shows"][SHOW(101)]["seasons"]["1"]["4"],
                         "2026-08-09T00:00:00Z")

    def test_apply_episode_new_season_on_tracked_show(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})}}
        wh._apply_episode(state, SHOW(101), 2, 1, "2026-07-04T00:00:00Z")
        self.assertEqual(state["shows"][SHOW(101)]["seasons"]["2"],
                         {"1": "2026-07-04T00:00:00Z"})

    def test_apply_movie_keeps_latest_watched_at(self):
        state = {"movies": {}}
        ids = {"trakt": 5, "tmdb": 5}
        wh._apply_movie(state, MOVIE(5), ids, "Film", 2025, "2026-07-01T00:00:00Z")
        wh._apply_movie(state, MOVIE(5), ids, "Film", 2025, "2026-07-09T00:00:00Z")  # later -> wins
        wh._apply_movie(state, MOVIE(5), ids, "Film", 2025, "2026-06-01T00:00:00Z")  # earlier -> ignored
        self.assertEqual(state["movies"][MOVIE(5)]["watched_at"], "2026-07-09T00:00:00Z")

    def test_apply_event_dispatch(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {}})}, "movies": {}}
        wh._apply_event(state, _ep_event(101, 1, 4))
        wh._apply_event(state, _mv_event(7, "Movie", 2024, "2026-07-15T00:00:00Z"))
        self.assertEqual(list(state["shows"][SHOW(101)]["seasons"]["1"]), ["4"])
        self.assertEqual(state["movies"][MOVIE(7)]["title"], "Movie")

    def test_an_event_naming_no_shared_id_is_dropped(self):
        """There is nothing to file it under — and nothing the tracker could have
        been counting for it either, since a roster row needs the same id."""
        state = {"shows": {SHOW(101): _show(101, {"1": {}})}, "movies": {}}
        event = _ep_event(101, 1, 4)
        event["show"]["ids"] = {"trakt": 101}   # a provider id and nothing shared
        wh._apply_event(state, event)
        self.assertEqual(state["shows"][SHOW(101)]["seasons"]["1"], {})

    def test_season_completed_map_is_the_day_the_last_episode_was_watched(self):
        state = {"shows": {SHOW(101): _show(101, {
            "1": {"1": "2026-07-02T00:00:00Z", "2": "2026-08-06T12:00:00Z"},
            "2": {"1": "", "2": ""},          # nothing dated -> no answer at all
        })}}
        self.assertEqual(wh.season_completed_map(state), {(SHOW(101), 1): "2026-08-06"})

    def test_season_completed_map_says_nothing_about_completeness(self):
        """It reports WHEN, not WHETHER: the episode total lives on the show
        record, so the caller decides (see compute_live_shows)."""
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": "2026-08-06T00:00:00Z"}})}}
        self.assertEqual(wh.season_completed_map(state), {(SHOW(101), 1): "2026-08-06"})

    def test_movies_in_range(self):
        state = {"movies": {
            MOVIE(1): {"title": "Jul", "year": 2026, "watched_at": "2026-07-15T00:00:00Z"},
            MOVIE(2): {"title": "Jun", "year": 2026, "watched_at": "2026-06-30T00:00:00Z"},
            MOVIE(3): {"title": "Aug", "year": 2026, "watched_at": "2026-08-01T00:00:00Z"},
        }}
        got = {m["title"] for m in wh.movies_in_range(state, "2026-07-01", "2026-07-31")}
        self.assertEqual(got, {"Jul"})

    def test_a_film_in_range_carries_the_identity_the_page_needs_to_name_it(self):
        """The page has to be able to ask for a film to be forgotten, and a title
        is not an identifier."""
        state = {"movies": {MOVIE(4): {"ids": {"trakt": 4, "tmdb": 4}, "title": "Jul",
                                       "year": 2026, "watched_at": "2026-07-15T00:00:00Z"}}}
        film = wh.movies_in_range(state, "2026-07-01", "2026-07-31")[0]
        self.assertEqual(film["key"], MOVIE(4))
        self.assertEqual(film["ids"], {"trakt": 4, "tmdb": 4})

    def test_month_bounds(self):
        self.assertEqual(wh.month_bounds("2026-07"), ("2026-07-01", "2026-07-31"))
        self.assertEqual(wh.month_bounds("2026-02"), ("2026-02-01", "2026-02-28"))

    def test_removed_changed(self):
        old = {"ep_removed": "a", "mv_removed": "b"}
        self.assertFalse(wh._removed_changed(old, {"ep_removed": "a", "mv_removed": "b"}))
        self.assertTrue(wh._removed_changed(old, {"ep_removed": "z", "mv_removed": "b"}))
        self.assertFalse(wh._removed_changed(None, {"ep_removed": "z"}))  # first run, not a removal


class WatchStateTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("wh")
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
            "shows": {SHOW(101): _show(101, {"1": {"1": "2026-07-01T00:00:00Z",
                                                   "2": "", "3": ""},
                                             "2": {"1": ""}})},
            "movies": {MOVIE(9): {"ids": {"trakt": 9}, "title": "M",
                                  "year": 2026, "watched_at": "2026-07-05T00:00:00Z"}},
        }
        await wh._save(self.user_id, state)
        back = await wh._load(self.user_id)
        self.assertEqual(back, state)

    async def test_the_pre_dates_shape_is_read_as_dates_unknown(self):
        """A backup taken before episodes carried dates still restores: the old
        bare list of numbers keeps its counts and simply has no dates."""
        self.assertEqual(wh.episode_watches([1, 2, 3]), {"1": "", "2": "", "3": ""})
        self.assertEqual(wh.season_completed_map(
            {"shows": {SHOW(101): _show(101, {"1": wh.episode_watches([1, 2, 3])})}}), {})

    async def test_save_replaces_rather_than_accumulates(self):
        await wh._save(self.user_id, {"last_synced": "a", "beacons": None,
                                      "shows": {SHOW(1): _show(1, {"1": {"1": ""}})},
                                      "movies": {}})
        await wh._save(self.user_id, {"last_synced": "b", "beacons": None,
                                      "shows": {SHOW(2): _show(2, {"1": {"5": ""}})},
                                      "movies": {}})
        back = await wh._load(self.user_id)
        self.assertEqual(back["shows"], {SHOW(2): _show(2, {"1": {"5": ""}})})
        self.assertEqual(back["last_synced"], "b")

    async def test_two_users_keep_independent_watch_state(self):
        other = await _make_user("other")
        await wh._save(self.user_id, {"last_synced": "mine", "beacons": None,
                                      "shows": {SHOW(101): _show(101, {"1": {"1": "", "2": ""}})},
                                      "movies": {MOVIE(1): {"ids": {"trakt": 1},
                                                            "title": "Mine", "year": 2026,
                                                            "watched_at": "2026-07-01T00:00:00Z"}}})
        await wh._save(other, {"last_synced": "theirs", "beacons": None,
                               "shows": {SHOW(202): _show(202, {"1": [9]})}, "movies": {}})
        mine, theirs = await wh._load(self.user_id), await wh._load(other)
        self.assertEqual(wh.watched_map(mine), {(SHOW(101), 1): 2})
        self.assertEqual(wh.watched_map(theirs), {(SHOW(202), 1): 1})
        self.assertEqual(mine["last_synced"], "mine")
        self.assertEqual(theirs["last_synced"], "theirs")
        self.assertEqual(theirs["movies"], {})


class SyncTests(WatchStateTestCase):
    async def test_gate_skips_history_when_unchanged(self):
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        # First sync establishes the beacon + last_synced.
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[_ep_event(101, 1, 1)]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}):
            # Seed a baselined show so the episode event is applied.
            st = await wh._load(self.user_id)
            st["shows"][SHOW(101)] = _show(101, {"1": []})
            await wh._save(self.user_id, st)
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
            self.assertEqual(hist.call_count, 1)

        # Second sync with the SAME beacon -> no history pull.
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist2, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}):
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
            hist2.assert_not_called()

    async def test_change_applies_history_delta(self):
        await wh._save(self.user_id, {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})},
                                      "movies": {},
                                      "last_synced": "2026-07-01",
                                      "beacons": {"ep_watched": "OLD"}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history",
                   return_value=[_ep_event(101, 1, 2),
                                 _mv_event(9, "M", 2026, "2026-07-05T00:00:00Z")]), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}):
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        self.assertEqual(sorted(state["shows"][SHOW(101)]["seasons"]["1"]), ["1", "2"])
        self.assertIn(MOVIE(9), state["movies"])
        # and it was persisted under this user, not just returned
        reloaded = await wh._load(self.user_id)
        self.assertEqual(sorted(reloaded["shows"][SHOW(101)]["seasons"]["1"]), ["1", "2"])

    async def test_sync_is_scoped_to_one_user(self):
        """Another user's sync must not fold events into this user's cache."""
        other = await _make_user("other")
        await wh._save(self.user_id, {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})},
                                      "movies": {},
                                      "last_synced": "2026-07-01", "beacons": None})
        await wh._save(other, {"shows": {SHOW(101): _show(101, {"1": {}})}, "movies": {},
                               "last_synced": "2026-07-01", "beacons": None})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[_ep_event(101, 1, 7)]), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}):
            await wh.sync(SETTINGS, other, today=date(2026, 7, 20))
        self.assertEqual(
            list((await wh._load(other))["shows"][SHOW(101)]["seasons"]["1"]), ["7"])
        self.assertEqual(
            list((await wh._load(self.user_id))["shows"][SHOW(101)]["seasons"]["1"]), ["1"])

    async def test_baseline_show_lands_on_the_named_user(self):
        other = await _make_user("other")
        # Baselining takes the whole record: the source id places the call, the
        # shared identity is what the answer is filed under.
        record = {"media": "show", "ids": {"trakt": 404, "tmdb": 404}, "season": 1}
        with patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={404: {1: {1: "2026-07-01T00:00:00Z", 2: "", 3: ""}}}):
            await wh.baseline_show(SETTINGS, self.user_id, record)
        self.assertEqual(wh.watched_map(await wh._load(self.user_id)), {(SHOW(404), 1): 3})
        self.assertEqual(wh.watched_map(await wh._load(other)), {})


if __name__ == "__main__":
    unittest.main()
