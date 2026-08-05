"""Unit tests for the per-user incremental watch-history cache
(app/distrakt/watch_history.py).

Pure state folders/readers are tested directly on the in-memory state dict (they
are unchanged by the move to per-user storage); the gated `sync` is tested with
the three Trakt calls mocked, against a throwaway SQLite file. No network.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime as real_datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import db
from app.distrakt import watch_history as wh
from app.providers.base import PlayCounts
from tests.support import new_db_path

# One linked service and nothing else, which is what almost every account has.
# The tracker asks each registered source whether this request's settings carry a
# usable credential for it, so a fake settings object has to be able to answer —
# see app/distrakt/routes.py's _distrakt_settings, which is what fills these in
# with one person's own tokens.
SETTINGS = SimpleNamespace(configured=True, trakt_configured=True, simkl_configured=False)


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


def _no_sweep():
    """The play-count sweep answering that it holds nothing.

    A re-baseline asks a source that can sweep its play counts WHICH titles moved,
    so every test that reaches one has to script that call or it goes to the
    network. Answering with an empty, complete sweep and no stored map to compare
    it against means "nothing can be concluded", which is what makes these tests
    still assert the behaviour they were written for: every cached title is asked
    about. The tests that are about the sweep itself script it properly.
    """
    return patch("app.providers.trakt.sync.fetch_play_counts",
                 return_value=PlayCounts({}, True))


def _show(tid, seasons, baselined=None) -> dict:
    """One cached show entry: the ids a refetch is placed with, the seasons, and
    which services have answered about it with nothing watched to report.

    Entries are filed under the SHARED identity (see app/providers/base.py) so
    plays reported by two services fold into one record. `ids` carries ONLY the
    ids a call is placed with, deliberately — the shared one is already the key,
    and a cache row holding it twice is a second copy of the same fact. The
    fixtures give each show a tmdb equal to its Trakt id, so SHOW(tid) names the
    row a fixture with that Trakt id produces.

    `baselined` is only ever set on a title with no seasons at all: a service
    that filled a season slot is already named by the slot, and the entry carries
    the key only for the services that have nothing else to speak for them.
    """
    entry = {"ids": {"trakt": tid}, "seasons": seasons}
    if baselined:
        entry["baselined"] = list(baselined)
    return entry


def _slot(entry: dict, season: str, source: str = "trakt") -> dict:
    """One season's episodes as ONE SERVICE reported them.

    The state files a season's watches per source, because two services can
    legitimately know different things about the same season and neither is
    wrong. The fixtures above hand seasons in the flat shape a single service
    produced before there was a second one — which the state still accepts and
    reads as that service's — so this is how they are read back.
    """
    return entry["seasons"][season][source]


def SHOW(tid) -> str:
    return f"show:tmdb:{tid}"


def MOVIE(tid) -> str:
    return f"movie:tmdb:{tid}"


class PureStateTests(unittest.TestCase):
    def test_watched_map_counts_len(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": "", "2": "", "3": ""},
                                                 "2": {"1": ""}})}}
        self.assertEqual(wh.watched_map(state),
                         {(SHOW(101), 1): {"trakt": 3}, (SHOW(101), 2): {"trakt": 1}})

    def test_apply_episode_dedups_and_skips_untracked(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": "", "2": ""}})}}
        wh._apply_episode(state, SHOW(101), 1, 2)   # already known -> no change
        wh._apply_episode(state, SHOW(101), 1, 3)   # new -> added
        wh._apply_episode(state, SHOW(999), 1, 1)   # untracked title -> ignored
        self.assertEqual(sorted(_slot(state["shows"][SHOW(101)], "1")), ["1", "2", "3"])
        self.assertNotIn(SHOW(999), state["shows"])

    def test_apply_episode_keeps_the_latest_date_for_an_episode(self):
        """Same rule as movies. Re-watching a season's last episode this month is
        finishing it this month, and an undated play never erases a date."""
        state = {"shows": {SHOW(101): _show(101, {"1": {}})}}
        wh._apply_episode(state, SHOW(101), 1, 4, "2026-07-01T00:00:00Z")
        wh._apply_episode(state, SHOW(101), 1, 4, "2026-08-09T00:00:00Z")  # later -> wins
        wh._apply_episode(state, SHOW(101), 1, 4, "2026-06-01T00:00:00Z")  # earlier -> ignored
        wh._apply_episode(state, SHOW(101), 1, 4)                          # undated -> ignored
        self.assertEqual(_slot(state["shows"][SHOW(101)], "1")["4"], "2026-08-09T00:00:00Z")

    def test_apply_episode_new_season_on_tracked_show(self):
        state = {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})}}
        wh._apply_episode(state, SHOW(101), 2, 1, "2026-07-04T00:00:00Z")
        self.assertEqual(_slot(state["shows"][SHOW(101)], "2"), {"1": "2026-07-04T00:00:00Z"})

    def test_a_play_carries_only_what_the_event_said(self):
        """The title and the episode, and nothing looked up. That is what makes a
        play safe to raise for a season nothing knows about."""
        event = _ep_event(101, 2, 5)
        event["show"]["title"] = "Show 101"
        play = wh._episode_play(event)
        self.assertEqual((str(play.key), play.season, play.number, play.title),
                         (SHOW(101), 2, 5, "Show 101"))

    def test_a_play_is_reported_for_a_title_the_cache_has_never_seen(self):
        """_apply_episode drops an untracked title because it has no counts to
        keep; the play is still reported, because "watched something nothing
        knows about" is exactly the question a caller has to answer."""
        state = {"shows": {}}
        wh._apply_episode(state, SHOW(999), 1, 1)
        self.assertEqual(state["shows"], {})
        self.assertIsNotNone(wh._episode_play(_ep_event(999, 1, 1)))

    def test_events_that_name_no_episode_are_not_plays(self):
        self.assertIsNone(wh._episode_play(_mv_event(9, "M", 2026, "2026-07-05T00:00:00Z")))
        # No shared id -> nothing to file it under, so nothing could be said
        # about it either way.
        self.assertIsNone(wh._episode_play(
            {"type": "episode", "show": {"ids": {}}, "episode": {"season": 1, "number": 1}}))
        self.assertIsNone(wh._episode_play(
            {"type": "episode", "show": {"ids": {"trakt": 1, "tmdb": 1}}, "episode": {}}))

    def test_a_state_that_came_out_of_storage_reports_no_plays(self):
        """Plays belong to a sync, not to the cache — a load has nothing to act
        on, which is what keeps a routine page load a read."""
        self.assertEqual(wh.episode_plays({"shows": {}, "movies": {}}), [])

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
        self.assertEqual(list(_slot(state["shows"][SHOW(101)], "1")), ["4"])
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

    def test_watched_changed(self):
        """The other half of the pair, and on its own it says nothing about a
        removal — it moves every time anybody watches anything. What it is FOR is
        the combination the sync reads: this moved, and the history explained
        none of it."""
        old = {"ep_watched": "a", "mv_watched": "b"}
        self.assertFalse(wh._watched_changed(old, {"ep_watched": "a", "mv_watched": "b"}))
        self.assertTrue(wh._watched_changed(old, {"ep_watched": "z", "mv_watched": "b"}))
        self.assertTrue(wh._watched_changed(old, {"ep_watched": "a", "mv_watched": "z"}))
        self.assertFalse(wh._watched_changed(None, {"ep_watched": "z"}))  # nothing to compare


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
        """Everything per source, which is how a state that has been through a
        sync looks: a cursor and a beacon for each service asked, and each
        season's episodes under the service that reported them."""
        state = {
            "cursors": {"trakt": "2026-07-20"},
            "beacons": {"trakt": {"ep_watched": "T1", "ep_removed": None,
                                  "mv_watched": "T1", "mv_removed": None}},
            # The stored play-count sweep, empty here because nothing has swept.
            # It round-trips like the other two documents and is what lets a
            # re-baseline ask about the titles that moved rather than all of them.
            "play_counts": {},
            "shows": {SHOW(101): _show(101, {
                "1": {"trakt": {"1": "2026-07-01T00:00:00Z", "2": "", "3": ""}},
                "2": {"trakt": {"1": ""}}})},
            "movies": {MOVIE(9): {"ids": {"trakt": 9}, "title": "M", "year": 2026,
                                  "watched_at": "2026-07-05T00:00:00Z",
                                  "source": "trakt"}},
        }
        await wh._save(self.user_id, state)
        back = await wh._load(self.user_id)
        self.assertEqual(back, state)

    async def test_the_flat_shape_restores_as_the_service_that_wrote_it(self):
        """A backup taken while one service could answer, or a state a caller
        assembled from plays alone, carries a season's episodes with no source
        name on them. Reading them as that one service's is a statement of fact —
        it is the only one that had written a row — rather than a guess."""
        await wh._save(self.user_id, {
            "cursors": {}, "beacons": {},
            "shows": {SHOW(101): _show(101, {"1": {"1": "", "2": ""}})}, "movies": {}})
        back = await wh._load(self.user_id)
        self.assertEqual(back["shows"][SHOW(101)]["seasons"],
                         {"1": {"trakt": {"1": "", "2": ""}}})

    async def test_the_pre_dates_shape_is_read_as_dates_unknown(self):
        """A backup taken before episodes carried dates still restores: the old
        bare list of numbers keeps its counts and simply has no dates."""
        self.assertEqual(wh.episode_watches([1, 2, 3]), {"1": "", "2": "", "3": ""})
        self.assertEqual(wh.season_completed_map(
            {"shows": {SHOW(101): _show(101, {"1": wh.episode_watches([1, 2, 3])})}}), {})

    async def test_a_title_with_nothing_watched_survives_the_round_trip(self):
        """The table holds one row per (season, service), so a title the viewer
        has seen none of wrote no row at all and came back looking as though it
        had never been baselined — so every load re-fetched it from the provider,
        for ever. A month of new premieres is exactly that case.

        The marker NAMES the service that answered, because being asked is a fact
        per service: a title one of them has nothing to say about is not a title
        the other has been asked about."""
        state = {"cursors": {}, "beacons": {}, "play_counts": {},
                 "shows": {SHOW(77): _show(77, {}, baselined=["trakt"])}, "movies": {}}
        await wh._save(self.user_id, state)
        back = await wh._load(self.user_id)
        self.assertIn(SHOW(77), back["shows"], "it read as never baselined")
        self.assertEqual(back["shows"][SHOW(77)]["seasons"], {},
                         "the marker leaked out as a season of its own")
        self.assertEqual(back, state)

    async def test_the_marker_is_replaced_once_something_is_watched(self):
        await wh._save(self.user_id, {"cursors": {}, "beacons": {},
                                      "shows": {SHOW(77): _show(77, {})}, "movies": {}})
        await wh._save(self.user_id, {"cursors": {}, "beacons": {},
                                      "shows": {SHOW(77): _show(77, {"1": {"1": ""}})},
                                      "movies": {}})
        back = await wh._load(self.user_id)
        self.assertEqual(back["shows"][SHOW(77)]["seasons"], {"1": {"trakt": {"1": ""}}})

    async def test_save_replaces_rather_than_accumulates(self):
        await wh._save(self.user_id, {"cursors": {"trakt": "a"}, "beacons": {},
                                      "shows": {SHOW(1): _show(1, {"1": {"1": ""}})},
                                      "movies": {}})
        await wh._save(self.user_id, {"cursors": {"trakt": "b"}, "beacons": {},
                                      "shows": {SHOW(2): _show(2, {"1": {"5": ""}})},
                                      "movies": {}})
        back = await wh._load(self.user_id)
        self.assertEqual(back["shows"],
                         {SHOW(2): _show(2, {"1": {"trakt": {"5": ""}}})})
        self.assertEqual(back["cursors"], {"trakt": "b"})

    async def test_two_users_keep_independent_watch_state(self):
        other = await _make_user("other")
        await wh._save(self.user_id, {"cursors": {"trakt": "mine"}, "beacons": {},
                                      "shows": {SHOW(101): _show(101, {"1": {"1": "", "2": ""}})},
                                      "movies": {MOVIE(1): {"ids": {"trakt": 1},
                                                            "title": "Mine", "year": 2026,
                                                            "watched_at": "2026-07-01T00:00:00Z"}}})
        await wh._save(other, {"cursors": {"trakt": "theirs"}, "beacons": {},
                               "shows": {SHOW(202): _show(202, {"1": [9]})}, "movies": {}})
        mine, theirs = await wh._load(self.user_id), await wh._load(other)
        self.assertEqual(wh.watched_map(mine), {(SHOW(101), 1): {"trakt": 2}})
        self.assertEqual(wh.watched_map(theirs), {(SHOW(202), 1): {"trakt": 1}})
        self.assertEqual(mine["cursors"], {"trakt": "mine"})
        self.assertEqual(theirs["cursors"], {"trakt": "theirs"})
        self.assertEqual(theirs["movies"], {})


class SyncTests(WatchStateTestCase):
    async def test_gate_skips_history_when_unchanged(self):
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        # First sync establishes the beacon + last_synced.
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[_ep_event(101, 1, 1)]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            # Seed a baselined show so the episode event is applied.
            st = await wh._load(self.user_id)
            st["shows"][SHOW(101)] = _show(101, {"1": []})
            await wh._save(self.user_id, st)
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
            self.assertEqual(hist.call_count, 1)

        # Second sync with the SAME beacon -> no history pull.
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist2, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
            hist2.assert_not_called()

    async def _seed_for_whole_month(self, la: dict) -> None:
        """A settled cache: two titles baselined, the beacon matching, the cursor
        part-way through the month, and a stored play-count sweep agreeing with
        what the service is about to report. A plain sync would be gated here, and
        a pass that gets past the gate for some other reason finds nothing moved.
        """
        await wh._save(self.user_id, {
            "shows": {SHOW(101): _show(101, {"1": {"1": ""}}),
                      SHOW(102): _show(102, {"1": {"1": ""}})},
            "movies": {}, "cursors": {"trakt": "2026-07-20"},
            "play_counts": {"trakt": {"101": 1, "102": 1}},
            "beacons": {"trakt": wh._beacons(la)}})

    async def test_a_named_month_is_read_without_re_asking_every_title(self):
        """The half of a forced sync a freeze actually needs: read that month
        again, don't re-fetch the progress of every title ever tracked. A settled
        record already holds the counts it settled on.

        WHAT ANSWERS THAT NOW IS THE SWEEP rather than the branch being skipped: a
        pass that reaches this asks the service which titles have moved, is told
        none have, and names none of them. The property is unchanged and is now
        arrived at from the service's own answer instead of from an inference."""
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        await self._seed_for_whole_month(la)
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={}) as progress, \
             patch("app.providers.trakt.sync.fetch_play_counts",
                   return_value=PlayCounts({"101": 1, "102": 1}, True)):
            await wh.sync(SETTINGS, self.user_id, since_month="2026-07",
                          today=date(2026, 7, 20))
        self.assertEqual([sorted(call.args[1]) for call in progress.await_args_list], [[]])
        self.assertEqual(hist.call_args.kwargs["start_at"], "2026-07-01")

    async def test_the_month_read_is_the_one_named_not_the_one_today_is_in(self):
        """A month is closed from the month AFTER it, so the two are never the
        same — and the only start the sync could work out for itself was today's.
        Closing October during November read November's history and never touched
        October's, which is the one month a freeze exists to write down."""
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        await self._seed_for_whole_month(la)
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            await wh.sync(SETTINGS, self.user_id, since_month="2026-10",
                          today=date(2026, 11, 1))
        self.assertEqual(hist.call_args.kwargs["start_at"], "2026-10-01")

    async def test_a_malformed_month_is_refused_before_it_reaches_the_provider(self):
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        await self._seed_for_whole_month(la)
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            with self.assertRaises(ValueError):
                await wh.sync(SETTINGS, self.user_id, since_month="2026-7",
                              today=date(2026, 11, 1))
        hist.assert_not_called()

    async def test_forcing_still_re_asks_every_title(self):
        """The other half is unchanged: an explicit refresh is the one caller that
        does want every title re-read."""
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        await self._seed_for_whole_month(la)
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={}) as progress, _no_sweep():
            await wh.sync(SETTINGS, self.user_id, force=True, today=date(2026, 7, 20))
        progress.assert_called_once()

    async def test_change_applies_history_delta(self):
        await wh._save(self.user_id, {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})},
                                      "movies": {},
                                      "cursors": {"trakt": "2026-07-01"},
                                      "beacons": {"trakt": {"ep_watched": "OLD"}}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history",
                   return_value=[_ep_event(101, 1, 2),
                                 _mv_event(9, "M", 2026, "2026-07-05T00:00:00Z")]), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        self.assertEqual(sorted(_slot(state["shows"][SHOW(101)], "1")), ["1", "2"])
        self.assertIn(MOVIE(9), state["movies"])
        # and it was persisted under this user, not just returned
        reloaded = await wh._load(self.user_id)
        self.assertEqual(sorted(_slot(reloaded["shows"][SHOW(101)], "1")), ["1", "2"])

    async def test_a_sync_reports_the_plays_it_folded_in_and_stores_none_of_them(self):
        """What the history has just reported is a signal to act on once. A stored
        copy would have every later load replay a decision already taken."""
        await wh._save(self.user_id, {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})},
                                      "movies": {}, "cursors": {"trakt": "2026-07-01"},
                                      "beacons": {"trakt": {"ep_watched": "OLD"}}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history",
                   return_value=[_ep_event(101, 1, 2), _ep_event(777, 3, 1),
                                 _mv_event(9, "M", 2026, "2026-07-05T00:00:00Z")]), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        self.assertEqual([(str(p.key), p.season, p.number) for p in wh.episode_plays(state)],
                         [(SHOW(101), 1, 2), (SHOW(777), 3, 1)])
        self.assertEqual(wh.episode_plays(await wh._load(self.user_id)), [])

    async def test_a_gated_sync_reports_no_plays_at_all(self):
        """The beacon had not moved, so no history was pulled and there is nothing
        for a caller to reconcile."""
        la = {"episodes": {"watched_at": "T1", "removed_at": None},
              "movies": {"watched_at": "T1", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[_ep_event(101, 1, 1)]), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        hist.assert_not_called()
        self.assertEqual(wh.episode_plays(state), [])

    async def test_sync_is_scoped_to_one_user(self):
        """Another user's sync must not fold events into this user's cache."""
        other = await _make_user("other")
        await wh._save(self.user_id, {"shows": {SHOW(101): _show(101, {"1": {"1": ""}})},
                                      "movies": {},
                                      "cursors": {"trakt": "2026-07-01"}, "beacons": {}})
        await wh._save(other, {"shows": {SHOW(101): _show(101, {"1": {}})}, "movies": {},
                               "cursors": {"trakt": "2026-07-01"}, "beacons": {}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[_ep_event(101, 1, 7)]), \
             patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
            await wh.sync(SETTINGS, other, today=date(2026, 7, 20))
        self.assertEqual(
            list(_slot((await wh._load(other))["shows"][SHOW(101)], "1")), ["7"])
        self.assertEqual(
            list(_slot((await wh._load(self.user_id))["shows"][SHOW(101)], "1")), ["1"])

    async def _seed_a_finished_season(self) -> None:
        """One title with a whole season stored, the cursor part-way through the
        month and a beacon to move. This is what the cache looks like the moment
        before somebody removes those plays at the service."""
        await wh._save(self.user_id, {
            "shows": {SHOW(101): _show(101, {"1": {str(n): "2026-07-0%d" % n
                                                   for n in range(1, 10)}})},
            "movies": {}, "cursors": {"trakt": "2026-07-20"},
            "beacons": {"trakt": {"ep_watched": "OLD", "ep_removed": None,
                                  "mv_watched": "OLD", "mv_removed": None}}})

    async def test_a_removal_the_beacon_will_not_admit_to_is_still_corrected(self):
        """The measured case, reduced to its beacons: every play of a season was
        removed at the service, which moved `episodes.watched_at` and left
        `episodes.removed_at` null. So the sync is not gated, the history pull
        finds nothing (a removal is not an event), and nothing said an unwatch had
        happened — the stored count kept the pre-removal number across restarts and
        only an explicit refresh ever corrected it.

        A moved watched stamp that no new event accounts for IS that removal, and
        the counts have to come out right without anybody pressing anything.
        """
        await self._seed_a_finished_season()
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "OLD", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={101: {}}) as progress, _no_sweep():
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        progress.assert_called_once()
        self.assertEqual(wh.watched_map(state), {},
                         "the cache went on reporting a season nobody watched")
        self.assertEqual(wh.watched_map(await wh._load(self.user_id)), {})

    async def test_an_ordinary_evenings_viewing_re_reads_no_other_title(self):
        """The commonest event there is, and what it may cost. The watched stamp
        moves every time anybody watches anything, so re-reading every tracked
        title on each of those is one provider call per title on every ordinary
        evening — which is the cost this whole mechanism exists to remove.

        WHAT KEEPS IT CHEAP IS THE SWEEP, NOT A SKIPPED BRANCH. The old reasoning
        was that new events ACCOUNT for the movement and leave nothing to infer,
        and that reasoning is unsound: an evening containing both viewing and an
        un-marking produces events for one and silence for the other. So the sweep
        is asked, it names the one title that moved, and nothing else is read."""
        await self._seed_a_finished_season()
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "OLD", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history",
                   return_value=[_ep_event(101, 1, 10)]), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={101: {1: {n: "" for n in range(1, 11)}}}) as progress, \
             patch("app.providers.trakt.sync.fetch_play_counts",
                   return_value=PlayCounts({"101": 9}, True)):
            state = await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        # Nothing stored to compare against on this first sweep, so the one cached
        # title is read once — and never a title the sweep did not name.
        self.assertEqual([sorted(call.args[1]) for call in progress.await_args_list], [[101]])
        self.assertEqual(wh.watched_map(state), {(SHOW(101), 1): {"trakt": 10}})

    async def test_a_first_pass_with_nothing_stored_asks_once_and_remembers(self):
        """A first pass has no previous sweep to compare against, so it can
        conclude nothing about what moved and asks about every cached title — once.
        What it must NOT do is leave itself in that state: the sweep it just made
        is stored, so the next pass has something to compare against and costs
        nothing."""
        await wh._save(self.user_id, {
            "shows": {SHOW(101): _show(101, {"1": {"1": ""}})}, "movies": {},
            "cursors": {}, "beacons": {}})
        la = {"episodes": {"watched_at": "NEW", "removed_at": None},
              "movies": {"watched_at": "NEW", "removed_at": None}}
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={101: {1: {1: ""}}}) as progress, \
             patch("app.providers.trakt.sync.fetch_play_counts",
                   return_value=PlayCounts({"101": 1}, True)):
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        self.assertEqual([sorted(call.args[1]) for call in progress.await_args_list], [[101]])
        self.assertEqual((await wh._load(self.user_id))["play_counts"],
                         {"trakt": {"101": 1}})

    async def test_a_beacon_that_did_not_move_at_all_is_still_gated(self):
        """The inference reads a beacon that MOVED. An unchanged one never reaches
        it, so the cheapest path there is stays the cheapest path there is."""
        la = {"episodes": {"watched_at": "OLD", "removed_at": None},
              "movies": {"watched_at": "OLD", "removed_at": None}}
        await self._seed_a_finished_season()
        with patch("app.providers.trakt.sync.fetch_last_activities", return_value=la), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist, \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={}) as progress:
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        hist.assert_not_called()
        progress.assert_not_called()

    async def test_baseline_show_lands_on_the_named_user(self):
        other = await _make_user("other")
        # Baselining takes the whole record: the source id places the call, the
        # shared identity is what the answer is filed under.
        record = {"media": "show", "ids": {"trakt": 404, "tmdb": 404}, "season": 1}
        with patch("app.providers.trakt.sync.fetch_progress_details",
                   return_value={404: {1: {1: "2026-07-01T00:00:00Z", 2: "", 3: ""}}}):
            await wh.baseline_show(SETTINGS, self.user_id, record)
        self.assertEqual(wh.watched_map(await wh._load(self.user_id)),
                         {(SHOW(404), 1): {"trakt": 3}})
        self.assertEqual(wh.watched_map(await wh._load(other)), {})


class ThePlaysSweepTests(WatchStateTestCase):
    """RE-READING A ROSTER USED TO COST ONE CALL PER TITLE EVER TRACKED, and the
    number was measured rather than feared: 146 sequential progress calls, six and
    a half seconds, inside a page that took eight and a half. Every one of them
    asked "what has changed about this title", and almost every answer was
    "nothing".

    THE SOURCE CAN ANSWER THAT FOR THE WHOLE LIBRARY IN A HANDFUL OF CALLS, in a
    per-title PLAY COUNT — no episodes, no seasons, just a number that moves with
    the watched set in both directions. So the re-baseline sweeps that, compares
    it against what it stored last time, and asks properly about the titles that
    actually moved.

    WHAT MUST NOT BE LOST, and each of these has its own test below: a shrinking
    count is a removal and has to bring the stored numbers DOWN; a title that
    vanishes from the listing has lost its last play and is a removal too, not an
    absence to ignore; and nothing about what a single-service account sees may
    change at all, because this is meant to be cheaper and not different.
    """

    def _sweep(self, counts, complete=True):
        return patch("app.providers.trakt.sync.fetch_play_counts",
                     return_value=PlayCounts(dict(counts), complete))

    async def _seed(self):
        """Two tracked titles, each with a season the service has reported. This
        is the roster a re-baseline would otherwise ask about in full."""
        await wh._save(self.user_id, {
            "cursors": {"trakt": "2026-07-20"}, "beacons": {}, "play_counts": {},
            "shows": {SHOW(101): _show(101, {"1": {"1": "", "2": ""}}),
                      SHOW(102): _show(102, {"1": {"1": ""}})},
            "movies": {}})

    async def _load(self, counts, progress, events=(), beacon="T2"):
        """One ORDINARY load — no force — with the beacon moved, the history
        answering `events`, and the sweep scripted."""
        details = AsyncMock(return_value=progress)
        with patch("app.providers.trakt.sync.fetch_last_activities",
                   return_value={"episodes": {"watched_at": beacon},
                                 "movies": {"watched_at": "T1"}}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=list(events)), \
             patch("app.providers.trakt.sync.fetch_progress_details", new=details), \
             self._sweep(counts):
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        return details

    async def test_a_removal_is_found_on_an_evening_that_also_had_viewing(self):
        """THE DEFECT THIS PHASE WAS BUILT FOR, AND THE ONE THE FIRST ATTEMPT
        MISSED. Somebody watches an episode of one show and un-marks a season of
        another in the same sitting. The history reports the first and CANNOT
        report the second — a removal is not an event — so the feed comes back
        non-empty, and a re-read gated on "the history was empty" concludes there
        is nothing to explain and skips the removal entirely. Observed exactly
        that way against a live account: two events on the load, and a season's
        plays gone with the stored count still reading the old number.

        The sweep answers the question instead of inferring it, so it runs on any
        pass that got past the beacon gate: 101 went down, 102 went up, and both
        are read."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        details = await self._load(
            {"101": 1, "102": 2}, {101: {1: {1: ""}}, 102: {1: {1: "", 2: ""}}},
            events=[_ep_event(102, 1, 2)])
        self.assertEqual(self._asked_about(details), [[101, 102]])
        counts = wh.watched_map(await wh._load(self.user_id))
        self.assertEqual(counts[(SHOW(101), 1)], {"trakt": 1},
                         "the removal was missed because the evening had viewing in it")
        self.assertEqual(counts[(SHOW(102), 1)], {"trakt": 2})

    async def test_an_ordinary_evening_costs_the_sweep_and_the_one_title(self):
        """The cost of asking every pass, pinned so it cannot grow back. One
        episode watched means one sweep and ONE title read — not the roster, which
        is what the whole phase exists to stop."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        details = await self._load({"101": 3, "102": 1},
                                   {101: {1: {1: "", 2: "", 3: ""}}},
                                   events=[_ep_event(101, 1, 3)])
        self.assertEqual(self._asked_about(details), [[101]])

    async def test_an_unchanged_beacon_still_reaches_no_sweep_at_all(self):
        """The cheapest path there is stays the cheapest path there is: nothing
        moved, so the sync is gated before any of this and the sweep is never
        placed."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        sweep = AsyncMock()
        with patch("app.providers.trakt.sync.fetch_last_activities",
                   return_value={"episodes": {"watched_at": "T1"},
                                 "movies": {"watched_at": "T1"}}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   new=AsyncMock(return_value={})), \
             patch("app.providers.trakt.sync.fetch_play_counts", new=sweep):
            await self._load({"101": 2, "102": 1}, {}, beacon="T1")
            await wh.sync(SETTINGS, self.user_id, today=date(2026, 7, 20))
        sweep.assert_not_awaited()

    async def _force(self, counts, progress, complete=True):
        """One forced re-baseline with the sweep and the progress read scripted.
        Answers with the mock, so what was ASKED ABOUT is what gets asserted."""
        details = AsyncMock(return_value=progress)
        with patch("app.providers.trakt.sync.fetch_last_activities",
                   return_value={"episodes": {"watched_at": "T1"}}), \
             patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
             patch("app.providers.trakt.sync.fetch_progress_details", new=details), \
             self._sweep(counts, complete):
            await wh.sync(SETTINGS, self.user_id, force=True, today=date(2026, 7, 20))
        return details

    def _asked_about(self, details) -> list[list]:
        return [sorted(call.args[1]) for call in details.await_args_list]

    async def test_the_first_sweep_has_nothing_to_compare_and_asks_about_everything(self):
        """There is no stored map, so the sweep can say nothing about what moved —
        and saying nothing has to mean "ask about all of them". Reading a first
        sweep as proof that nothing has changed is the one answer it cannot
        support."""
        await self._seed()
        details = await self._force({"101": 2, "102": 1},
                                    {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        self.assertEqual(self._asked_about(details), [[101, 102]])

    async def test_a_play_on_one_show_re_reads_that_show_and_no_other(self):
        """THE DELIVERABLE, asserted by counting calls. One title's count moved,
        so one title is asked about — the other is not touched however long the
        roster is."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        details = await self._force({"101": 3, "102": 1},
                                    {101: {1: {1: "", 2: "", 3: ""}}})
        self.assertEqual(self._asked_about(details), [[101]])
        self.assertEqual(wh.watched_map(await wh._load(self.user_id)),
                         {(SHOW(101), 1): {"trakt": 3}, (SHOW(102), 1): {"trakt": 1}})

    async def test_nothing_moving_asks_about_no_title(self):
        """The common case, and the whole saving: a forced refresh over an
        unchanged library names not one title.

        The batch call is still placed, holding nothing — every source's
        implementation returns immediately for an empty list without a request,
        so what matters is the ids and there are none."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        details = await self._force({"101": 2, "102": 1}, {})
        self.assertEqual(self._asked_about(details), [[]])

    async def test_a_count_that_shrank_brings_the_stored_numbers_down(self):
        """THE REGRESSION §4.8k's inference could only ever guess at. A count only
        ever compared for growth would read a removal as nothing at all, which is
        the defect being fixed reintroduced one layer down."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        details = await self._force({"101": 1, "102": 1}, {101: {1: {1: ""}}})
        self.assertEqual(self._asked_about(details), [[101]])
        self.assertEqual(wh.watched_map(await wh._load(self.user_id))[(SHOW(101), 1)],
                         {"trakt": 1})

    async def test_a_show_that_vanished_is_a_removal_and_not_an_absence(self):
        """A title whose last play is removed stops being listed at all rather than
        being listed at zero. Read as "it simply is not in this sweep" it would
        keep its stored count for ever."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        details = await self._force({"102": 1}, {101: {}})
        self.assertEqual(self._asked_about(details), [[101]])
        self.assertNotIn((SHOW(101), 1), wh.watched_map(await wh._load(self.user_id)))

    async def test_a_re_watch_costs_one_read_and_changes_no_count(self):
        """The known false positive, pinned as BOUNDED rather than suppressed:
        watching something again raises the count without changing the watched
        set. Suppressing it would need the per-episode data this sweep does not
        carry, which is the whole reason the sweep is cheap."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}})
        before = wh.watched_map(await wh._load(self.user_id))
        details = await self._force({"101": 4, "102": 1},
                                    {101: {1: {1: "", 2: ""}}})
        self.assertEqual(self._asked_about(details), [[101]])
        self.assertEqual(wh.watched_map(await wh._load(self.user_id)), before)

    async def test_an_incomplete_sweep_asks_about_everything_and_is_not_stored(self):
        """A sweep that lost a page may say what it FOUND and never what is
        missing: a title on a page nobody fetched looks exactly like a title with
        no plays left. So it decides nothing, and it must not be stored either —
        stored, the NEXT comparison would read that whole page as removals."""
        await self._seed()
        await self._force({"101": 2, "102": 1},
                          {101: {1: {1: "", 2: ""}}, 102: {1: {1: ""}}},
                          complete=False)
        self.assertEqual((await wh._load(self.user_id))["play_counts"], {})
        details = await self._force({"101": 2, "102": 1}, {}, complete=False)
        self.assertEqual(self._asked_about(details), [[101, 102]])

    async def test_a_title_whose_read_failed_is_asked_again_next_time(self):
        """The stored map claims the app's counts are up to date with those
        numbers. Recording one for a title whose progress read came back with
        nothing to say would tell the next sweep there was nothing to do, and the
        wrong number would stand until something else happened to that title."""
        await self._seed()
        await self._force({"101": 2, "102": 1}, {102: {1: {1: ""}}})
        details = await self._force({"101": 2, "102": 1}, {101: {1: {1: "", 2: ""}}})
        self.assertEqual(self._asked_about(details), [[101]])

    async def test_a_source_that_cannot_sweep_is_still_asked_about_everything(self):
        """The path any source without such an endpoint still takes, unchanged."""
        await self._seed()
        details = AsyncMock(return_value={101: {1: {1: ""}}, 102: {1: {1: ""}}})
        port = SimpleNamespace(
            fetch_last_activities=AsyncMock(return_value={"episodes": {"watched_at": "T1"}}),
            fetch_history=AsyncMock(return_value=[]),
            fetch_progress_details=details)
        state = await wh._load(self.user_id)
        await wh._rebaseline_by_id(SETTINGS, state, "trakt", "trakt", port,
                                   _span_noop, reason="force")
        self.assertEqual(self._asked_about(details), [[101, 102]])


def _span_noop(*_args, **_kwargs):
    """The perf span, reduced to a context manager that does nothing. The tests
    above reach _rebaseline_by_id directly, which is handed one by its caller."""
    from contextlib import nullcontext

    return nullcontext()


class _PinnedClock:
    """`datetime` with `now()` pinned, so a test can put the wall clock at an hour
    where the UTC date and the viewer's local date are not the same day.

    Only `now` is pinned; everything else is the real class, because the module
    parses timestamps with it as well.
    """

    def __init__(self, moment):
        self._moment = moment

    def now(self, tz=None):
        return self._moment

    def __getattr__(self, name):
        return getattr(real_datetime, name)


class CursorTests(WatchStateTestCase):
    """A SWEEP'S CURSOR MUST NEVER BE AHEAD OF THE DATA IT WILL SWEEP.

    Two clocks meet here. Plays come back stamped in UTC and the cursor is
    compared against those stamps; the months this tracker files them into are the
    VIEWER'S months and are named from their local date. So there are two ways for
    the cursor to end up in the future, and they happen in opposite halves of the
    world: taken from UTC it runs ahead through the last hours of a US evening,
    and taken from the local date it runs ahead through a morning in Tokyo. Either
    way the next sweep asks for plays AFTER the ones it is looking for, and they
    are missed until something forces a wider read.

    The pinned clock is the whole point of these: without it they would pass or
    fail depending on what time the suite happened to be run at and in which zone.
    """

    def _at(self, utc_now, today):
        with patch("app.distrakt.watch_history.datetime", _PinnedClock(utc_now)):
            return wh._sweep_cursor(today)

    def test_a_us_evening_does_not_put_the_cursor_on_tomorrow(self):
        """23:04 in New York is already the 5th in UTC, and this is the case that
        was observed: a start date in the future, sweeping nothing."""
        self.assertEqual(
            self._at(real_datetime(2026, 8, 5, 3, 4, tzinfo=timezone.utc),
                     date(2026, 8, 4)),
            "2026-08-04")

    def test_a_tokyo_morning_does_not_put_the_cursor_on_tomorrow_either(self):
        """The same defect with the clocks the other way round, which is what the
        obvious fix — swap UTC for the local date — would have produced."""
        self.assertEqual(
            self._at(real_datetime(2026, 8, 4, 23, 0, tzinfo=timezone.utc),
                     date(2026, 8, 5)),
            "2026-08-04")

    def test_the_two_agreeing_is_that_day(self):
        """Most of the day, and it must not cost anything."""
        self.assertEqual(
            self._at(real_datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc),
                     date(2026, 8, 4)),
            "2026-08-04")

    async def _sweep_from(self, utc_now, today):
        """Sync once at that wall-clock moment, then again with the beacon moved,
        and answer with the `start_at` the second sweep asked for."""
        first = {"episodes": {"watched_at": "T1", "removed_at": None},
                 "movies": {"watched_at": "T1", "removed_at": None}}
        moved = {"episodes": {"watched_at": "T2", "removed_at": None},
                 "movies": {"watched_at": "T1", "removed_at": None}}
        with patch("app.distrakt.watch_history.datetime", _PinnedClock(utc_now)):
            with patch("app.providers.trakt.sync.fetch_last_activities", return_value=first), \
                 patch("app.providers.trakt.sync.fetch_history", return_value=[]), \
                 patch("app.providers.trakt.sync.fetch_progress_details", return_value={}),              _no_sweep():
                await wh.sync(SETTINGS, self.user_id, today=today)
            with patch("app.providers.trakt.sync.fetch_last_activities", return_value=moved), \
                 patch("app.providers.trakt.sync.fetch_history", return_value=[]) as hist, \
                 patch("app.providers.trakt.sync.fetch_progress_details",
                       return_value={}), _no_sweep():
                await wh.sync(SETTINGS, self.user_id, today=today)
        return hist.call_args.kwargs["start_at"]

    async def test_a_play_made_that_evening_is_still_swept_afterwards(self):
        """THE REGRESSION, end to end. A play at 18:00 in New York is stamped the
        4th in UTC; a cursor written from the UTC clock that same evening says the
        5th, and `watched_at < start_at` then drops it for ever."""
        play_day = "2026-08-04"  # 2026-08-04T22:00:00Z — the same evening
        start_at = await self._sweep_from(
            real_datetime(2026, 8, 5, 3, 4, tzinfo=timezone.utc), date(2026, 8, 4))
        self.assertLessEqual(start_at, play_day)

    async def test_a_play_made_that_morning_is_still_swept_afterwards(self):
        """And with the clocks the other way round, where the LOCAL date is the
        one running ahead."""
        play_day = "2026-08-04"  # 2026-08-04T23:00:00Z — that Tokyo morning
        start_at = await self._sweep_from(
            real_datetime(2026, 8, 4, 23, 0, tzinfo=timezone.utc), date(2026, 8, 5))
        self.assertLessEqual(start_at, play_day)


if __name__ == "__main__":
    unittest.main()
