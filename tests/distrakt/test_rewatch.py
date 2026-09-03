"""Adding a season you already finished.

THE COMPLAINT: adding a previously-finished season filed it under the month it
was ORIGINALLY finished in, not the month being looked at, so it never appeared
on the list the viewer was standing on. `lifecycle.finish` was doing exactly
what its docstring says — the month comes from the watch history — so this was a
design gap rather than a function misbehaving.

WHY IT IS ASKED AND NOT INFERRED. Somebody starting a re-watch and a tracker
meeting an old completion for the first time produce the IDENTICAL signal: a
finished season being added. There is no definition of "recent" that separates
them without being a guess somebody would have to defend, and being wrong is
silent in both directions. So the viewer is asked, which is the same answer the
untracked-season prompts already give to an indistinguishable pair.

AND THE ANSWER IS A DATE, NOT A SECOND EPISODE MAP. `distrakt_show_progress` has
held {episode: watched_at} since it was written, so filtering that map by a start
day IS "progress through the current pass"; a stored second pass would be a copy
of something derivable.

No network.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import db
from app.distrakt import store, watch_history
from app.providers.base import ItemKey

from tests.support import migrated_db
from tests.distrakt.test_gating import DistraktTestCase

KEY = ItemKey("show", "tmdb", "1396")
OTHER = ItemKey("show", "tmdb", "9999")


def _state(*, watched: dict, key=KEY, season=1) -> dict:
    """A watch state holding one season's plays, in the shape the cache keeps:
    {source: {episode: watched_at}}."""
    return {"shows": {str(key): {"seasons": {str(season): {"trakt": dict(watched)}}}}}


class TheFloorFiltersTheHistoryTests(unittest.TestCase):
    """`apply_history_floor` — one filter over the state, before any reader is
    built, so the three that read those episode maps cannot disagree about
    whether one season is finished."""

    OLD_RUN = {"1": "2020-02-01", "2": "2020-02-08", "3": "2020-02-15"}
    NEW_RUN = {"4": "2026-09-01"}

    def test_plays_before_the_floor_stop_counting(self):
        state = _state(watched={**self.OLD_RUN, **self.NEW_RUN})
        floored = watch_history.apply_history_floor(
            state, {(str(KEY), 1): "2026-08-01"})
        self.assertEqual(watch_history.watched_map(floored)[(str(KEY), 1)],
                         {"trakt": 1})

    def test_without_a_floor_the_whole_history_counts(self):
        state = _state(watched={**self.OLD_RUN, **self.NEW_RUN})
        self.assertEqual(watch_history.watched_map(state)[(str(KEY), 1)],
                         {"trakt": 4})

    def test_the_completion_date_moves_with_the_floor(self):
        """THE WHOLE POINT. Unfiltered, this season reads as finished in
        February 2020 and settles onto that month — which is the reported bug.
        Filtered, the only play that counts is September's."""
        state = _state(watched={**self.OLD_RUN, **self.NEW_RUN})
        self.assertEqual(
            watch_history.season_completed_map(state)[(str(KEY), 1)], "2026-09-01")
        floored = watch_history.apply_history_floor(
            state, {(str(KEY), 1): "2026-08-01"})
        self.assertEqual(
            watch_history.season_completed_map(floored)[(str(KEY), 1)], "2026-09-01")

    def test_a_season_whose_every_play_predates_the_floor_reads_as_unstarted(self):
        state = _state(watched=self.OLD_RUN)
        floored = watch_history.apply_history_floor(
            state, {(str(KEY), 1): "2026-08-01"})
        self.assertEqual(watch_history.watched_map(floored)[(str(KEY), 1)],
                         {"trakt": 0})
        self.assertNotIn((str(KEY), 1), watch_history.season_completed_map(floored))

    def test_an_undated_play_is_kept(self):
        """"Watched, day unknown" is ordinary in both services' history. A floor
        is a claim about WHEN, so it can only act on plays that say when —
        dropping the rest would shrink a count over a fact nobody stated."""
        state = _state(watched={"1": "", "2": "2020-02-08"})
        floored = watch_history.apply_history_floor(
            state, {(str(KEY), 1): "2026-08-01"})
        self.assertEqual(watch_history.watched_map(floored)[(str(KEY), 1)],
                         {"trakt": 1})

    def test_a_floor_touches_only_the_season_it_names(self):
        state = {"shows": {
            str(KEY): {"seasons": {"1": {"trakt": {"1": "2020-02-01"}},
                                   "2": {"trakt": {"1": "2020-03-01"}}}},
            str(OTHER): {"seasons": {"1": {"trakt": {"1": "2020-02-01"}}}},
        }}
        floored = watch_history.apply_history_floor(
            state, {(str(KEY), 1): "2026-08-01"})
        counts = watch_history.watched_map(floored)
        self.assertEqual(counts[(str(KEY), 1)], {"trakt": 0})
        self.assertEqual(counts[(str(KEY), 2)], {"trakt": 1})
        self.assertEqual(counts[(str(OTHER), 1)], {"trakt": 1})

    def test_no_floors_is_the_state_itself(self):
        """Every account that has never answered the question is on this path,
        so it must cost nothing and change nothing."""
        state = _state(watched=self.OLD_RUN)
        self.assertIs(watch_history.apply_history_floor(state, {}), state)

    def test_it_does_not_mutate_the_state_handed_in(self):
        """The cache object is shared with whatever saved it, and a filtered
        view must never be written back as though it were the whole history."""
        state = _state(watched=self.OLD_RUN)
        watch_history.apply_history_floor(state, {(str(KEY), 1): "2026-08-01"})
        self.assertEqual(watch_history.watched_map(state)[(str(KEY), 1)],
                         {"trakt": 3})


class TheFloorsComeFromTheRosterTests(unittest.TestCase):
    def test_a_row_that_states_one_is_read_and_one_that_does_not_is_absent(self):
        """Absent rather than dated "": "this pass starts here" and "no pass has
        been declared" are different facts and one must not read as the other."""
        floors = watch_history.history_floors([
            {"key": str(KEY), "season": 1, "history_from": "2026-08-01"},
            {"key": str(OTHER), "season": 1, "history_from": ""},
            {"key": str(OTHER), "season": 2},
        ])
        self.assertEqual(floors, {(str(KEY), 1): "2026-08-01"})

    def test_a_row_naming_no_season_is_skipped_rather_than_raised_over(self):
        self.assertEqual(watch_history.history_floors(
            [{"key": str(KEY), "history_from": "2026-08-01"}]), {})


class TheWatermarkIsStoredTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        migrated_db("rewatch-store")
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, created_at, updated_at) VALUES ('w', ?, ?)",
            (now, now))
        self.user_id = result.lastrowid
        await store.add_user_record(self.user_id, {
            "media": "show", "match_source": "tmdb", "match_id": "1396",
            "key": str(KEY), "season": 1, "title": "Breaking Bad",
            "ids": {"tmdb": 1396}, "network": "AMC",
            "kind": str(store.RecordKind.KEEPUP),
        })

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def test_a_row_starts_with_no_floor(self):
        row = await store.find_user_record(self.user_id, KEY, 1)
        self.assertEqual(row["history_from"], "")

    async def test_setting_and_clearing_it_round_trips(self):
        self.assertTrue(
            await store.set_history_from(self.user_id, KEY, 1, "2026-08-02"))
        row = await store.find_user_record(self.user_id, KEY, 1)
        self.assertEqual(row["history_from"], "2026-08-02")

        self.assertTrue(await store.set_history_from(self.user_id, KEY, 1, ""))
        row = await store.find_user_record(self.user_id, KEY, 1)
        self.assertEqual(row["history_from"], "")

    async def test_a_season_not_on_the_list_answers_false(self):
        self.assertFalse(
            await store.set_history_from(self.user_id, OTHER, 1, "2026-08-02"))

    async def test_the_floor_reaches_the_reader_through_the_stored_row(self):
        """The two halves meeting: what `set_history_from` writes is what
        `history_floors` reads back off the roster."""
        await store.set_history_from(self.user_id, KEY, 1, "2026-08-02")
        rows = await store.user_records(self.user_id)
        self.assertEqual(watch_history.history_floors(rows),
                         {(str(KEY), 1): "2026-08-02"})


class TheAddAsksRatherThanFilingSilentlyTests(DistraktTestCase):
    """The route half: adding a season the history says is finished reports it,
    and answering the question is what writes the floor.

    NOTHING IS DECIDED FOR THE VIEWER. The season goes on the list either way and
    the history is untouched; ignoring the prompt leaves exactly the behaviour
    that existed before it. What changes is that the old month no longer claims
    the season without anybody being told.
    """

    IDS = {"trakt": 1396, "tmdb": 1396, "slug": "breaking-bad"}
    SEASON = {"total": 13, "cadence": "Sun", "premiere": "2020-01-20",
              "finale": "2020-02-15", "started_airing": True,
              "finished_airing": True}

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user("rewatcher")
        self.sign_in_as(self.user_id)

    def _add(self, *, history: dict | None = None):
        """Add the season, with the viewer's history saying whatever `history`
        says. The season lookup is stubbed because this is about the history,
        not about the catalogue."""
        state = _state(watched=history or {})

        async def _load(_user_id):
            return state

        with patch("app.distrakt.live.season_detail",
                   AsyncMock(return_value=dict(self.SEASON))), \
             patch("app.distrakt.watch_history.baseline_show",
                   AsyncMock(return_value=None)), \
             patch("app.distrakt.watch_history.load_state", _load):
            return self.client.post("/api/distrakt/add", json={
                "year": 2026, "month": 9, "ids": dict(self.IDS),
                "title": "Breaking Bad", "network": "AMC", "season": 1,
            })

    def test_a_season_with_no_history_asks_nothing(self):
        """Which is almost every add, so the question must not appear on one."""
        resp = self._add()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertNotIn("rewatch_prompt", resp.json())

    def test_a_season_the_history_says_is_finished_is_reported(self):
        resp = self._add(history={"1": "2020-02-01", "2": "2020-02-15"})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        prompt = resp.json().get("rewatch_prompt")
        self.assertIsNotNone(prompt, "a finished season was filed without asking")
        self.assertEqual(prompt["completed_on"], "2020-02-15")
        self.assertEqual(prompt["season"], 1)
        self.assertEqual(prompt["key"], str(KEY))

    def test_answering_fresh_floors_the_history_at_the_day_after(self):
        """THE DAY AFTER, not the day itself: the last episode of the old run was
        watched ON that day, and a floor including it would carry one episode of
        the finished pass into the new one."""
        self._add(history={"1": "2020-02-15"})
        resp = self.client.post("/api/distrakt/rewatch", json={
            "year": 2026, "month": 9, "key": str(KEY), "season": 1,
            "completed_on": "2020-02-15", "fresh": True,
        })
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row = asyncio.run(store.find_user_record(self.user_id, KEY, 1))
        self.assertEqual(row["history_from"], "2020-02-16")

    def test_answering_keep_leaves_the_history_alone(self):
        self._add(history={"1": "2020-02-15"})
        resp = self.client.post("/api/distrakt/rewatch", json={
            "year": 2026, "month": 9, "key": str(KEY), "season": 1,
            "completed_on": "2020-02-15", "fresh": False,
        })
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row = asyncio.run(store.find_user_record(self.user_id, KEY, 1))
        self.assertEqual(row["history_from"], "")

    def test_a_second_add_does_not_ask_again_once_a_run_is_declared(self):
        """The floor is honoured when the question is asked, so a viewer who has
        already answered is not asked about the same old completion for ever."""
        self._add(history={"1": "2020-02-15"})
        self.client.post("/api/distrakt/rewatch", json={
            "year": 2026, "month": 9, "key": str(KEY), "season": 1,
            "completed_on": "2020-02-15", "fresh": True,
        })
        resp = self._add(history={"1": "2020-02-15"})
        self.assertNotIn("rewatch_prompt", resp.json())

    def test_a_fresh_run_with_no_date_is_refused_rather_than_guessed(self):
        self._add(history={"1": "2020-02-15"})
        resp = self.client.post("/api/distrakt/rewatch", json={
            "year": 2026, "month": 9, "key": str(KEY), "season": 1, "fresh": True,
        })
        self.assertEqual(resp.status_code, 400, resp.text[:200])

    def test_an_unreadable_date_is_refused(self):
        self._add(history={"1": "2020-02-15"})
        resp = self.client.post("/api/distrakt/rewatch", json={
            "year": 2026, "month": 9, "key": str(KEY), "season": 1,
            "completed_on": "last February", "fresh": True,
        })
        self.assertEqual(resp.status_code, 400, resp.text[:200])

    def test_a_season_not_on_the_list_is_refused(self):
        resp = self.client.post("/api/distrakt/rewatch", json={
            "year": 2026, "month": 9, "key": str(OTHER), "season": 1,
            "completed_on": "2020-02-15", "fresh": True,
        })
        self.assertEqual(resp.status_code, 400, resp.text[:200])


if __name__ == "__main__":
    unittest.main()
