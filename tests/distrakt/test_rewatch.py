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
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from app import db
from app.distrakt import routes as distrakt_routes
from app.distrakt import store, watch_history
from app.providers.base import ItemKey

from tests.support import migrated_db
from tests.distrakt.test_gating import DistraktTestCase

KEY = ItemKey("show", "tmdb", "1396")
OTHER = ItemKey("show", "tmdb", "9999")


def _state(*, watched: dict, key=KEY, season=1, source="trakt") -> dict:
    """A watch state holding one season's plays, in the shape the cache keeps:
    {source: {episode: watched_at}}."""
    return {"shows": {str(key): {"seasons": {str(season): {source: dict(watched)}}}}}


class _RosterAddCase(DistraktTestCase):
    """The doubles every test that puts a season on the roster needs.

    ONE PATCH STACK RATHER THAN EIGHT NEAR-COPIES, and the reason is not tidiness.
    Five of those copies stubbed `baseline_show` to a NO-OP while handing
    `load_state` a fully populated history — so they supplied for free the exact
    thing the routes were failing to fetch, and passed against two different
    routes that asked the re-watch question before fetching anything. Both faults
    were found in a browser instead. A double that is generous in one place is a
    hole in every test that reuses it, so there is now one and it is faithful.

    FAITHFUL MEANS THE STATE IS EMPTY UNTIL `baseline_show` HAS BEEN CALLED,
    which is what the real pair does: `distrakt_show_progress` holds nothing for
    a title nobody has ever asked a service about. `self.baselines` counts the
    calls, so a test can assert the fetch happened at all.

    BOTH ROUTES' LOOKUPS ARE PATCHED TOGETHER. `/api/distrakt/add` reaches
    `live.season_detail`; `/api/distrakt/unknown-add` reaches `_season_lookup`
    and `live.network_for`. Patching a name the route under test never calls
    costs nothing, and one stack means a test moving between the two doors does
    not need a different set of doubles to say the same thing.
    """

    IDS = {"trakt": 1396, "tmdb": 1396, "slug": "breaking-bad"}
    SEASON = {"total": 13, "cadence": "Sun", "premiere": "2020-01-20",
              "finale": "2020-02-15", "started_airing": True,
              "finished_airing": True}
    OLD_RUN = {str(n): "2020-02-%02d" % (n + 1) for n in range(1, 14)}
    USER = "adder"

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user(self.USER)
        self.sign_in_as(self.user_id)
        self.baselines = 0

    @contextmanager
    def _services(self, *, history=None, source="trakt", sources=None, detail=None):
        """`sources` names SEVERAL services at once — {name: {episode: day}} —
        for the questions that are about two of them agreeing or disagreeing.
        `history` plus `source` is the one-service shorthand for everything else."""
        seen: dict = {"shows": {}}
        slots = ({name: dict(eps) for name, eps in sources.items()} if sources
                 else {source: dict(history or {})})
        full = {"shows": {str(KEY): {"seasons": {"1": slots}}}}

        async def _baseline(settings, user_id, record):
            self.baselines += 1
            seen["shows"] = dict(full["shows"])

        async def _load(_user_id):
            return {"shows": dict(seen["shows"])}

        async def _sync(settings, user_id, roster, *a, **kw):
            # THE SHARED SEAM, STOOD IN FOR FAITHFULLY: the real one applies the
            # roster's floors to the state it returns, and a double that skipped
            # that would exercise a pipeline this app does not have.
            return watch_history.apply_history_floor(
                {"shows": dict(seen["shows"])},
                watch_history.history_floors(roster))

        # ONE FLAG OVERRIDDEN AND THE REST OF THE OBJECT REAL. unknown-add
        # refuses outright without a catalogue, and that refusal is not what any
        # of these tests is about; a stand-in built from scratch would have to
        # grow an attribute every time anything the month payload touches does.
        real = distrakt_routes._distrakt_settings

        class _WithCatalogue:
            trakt_catalogue_configured = True

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

        async def _settings(user_id):
            return _WithCatalogue(await real(user_id))

        season = dict(self.SEASON) if detail is None else dict(detail)
        with patch("app.distrakt.routes._distrakt_settings", _settings), \
             patch("app.distrakt.live.season_detail",
                   AsyncMock(return_value=season)), \
             patch("app.distrakt.routes._season_lookup",
                   lambda settings: AsyncMock(return_value=season)), \
             patch("app.distrakt.live.network_for", AsyncMock(return_value="AMC")), \
             patch("app.distrakt.watch_history.baseline_show", _baseline), \
             patch("app.distrakt.watch_history.sync_and_baseline", _sync), \
             patch("app.distrakt.watch_history.load_state", _load):
            yield

    def _row(self):
        return asyncio.run(store.find_user_record(self.user_id, KEY, 1))

    def _completed(self, month="2026-09"):
        return asyncio.run(store.find_month_record(
            self.user_id, month, store.RecordKind.COMPLETED, KEY, 1))

    def _search_add(self, *, history=None, source="trakt", sources=None,
                    detail=None, **extra):
        """The search door: /api/distrakt/add."""
        with self._services(history=history, source=source, sources=sources,
                            detail=detail):
            return self.client.post("/api/distrakt/add", json={
                "year": 2026, "month": 9, "ids": dict(self.IDS),
                "title": "Breaking Bad", "network": "AMC", "season": 1, **extra,
            })

    def _prompt_add(self, *, history=None, source="trakt", sources=None,
                    detail=None, **extra):
        """The history door: /api/distrakt/unknown-add."""
        with self._services(history=history, source=source, sources=sources,
                            detail=detail):
            return self.client.post("/api/distrakt/unknown-add", json={
                "key": str(KEY), "season": 1, "ids": dict(self.IDS),
                "title": "Breaking Bad", "year": 2026, "month": 9, **extra,
            })


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


class TheAddAsksBeforeItWritesAnythingTests(_RosterAddCase):
    """The route half: adding a season the viewer's history says they already
    finished writes NOTHING and asks. The answer is what performs the add.

    THE ORDER IS THE FIX, AND THESE ARE THE FAILURES THAT PROVED IT. Asking
    after the add looked equivalent — the season is on the list either way, so
    ignoring the question would leave the old behaviour — and it was not, because
    an add is followed by a recompute and the recompute settles a finished season
    onto the month its history dates it to. A season watched in a month the
    tracker HAD tracked was moved off the list before the question could be
    answered, so answering it said "that season is not on your list". One watched
    in an untracked month stayed and showed its whole episode count as this run's
    progress. Writing nothing until the answer arrives makes both unreachable.
    """

    USER = "rewatcher"

    def _post(self, **kwargs):
        return self._search_add(**kwargs)

    # -- the ordinary add, which none of this may disturb --

    def test_a_season_with_no_history_is_added_without_a_question(self):
        """Which is almost every add, so the question must not appear on one."""
        resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertNotIn("needs_decision", resp.json())
        self.assertIsNotNone(self._row(), "an ordinary add did not land")

    def test_a_partly_watched_season_is_added_without_a_question(self):
        """Somebody two episodes into a season is not re-watching it. The
        question is about a FINISHED one, and those plays already count."""
        resp = self._post(history={"1": "2026-08-30", "2": "2026-08-31"})
        self.assertNotIn("needs_decision", resp.json())
        self.assertIsNotNone(self._row())

    def test_twelve_of_thirteen_episodes_is_not_finished(self):
        """THE DEFECT THIS CLOSES. "Finished" was read off
        `season_completed_map`, which dates the LAST episode watched and says so
        in its own docstring — it answers "when", never "whether". A season with
        one play was therefore reported as finished, so the re-watch question
        appeared on a season somebody was in the middle of and offered to
        discard their progress. The episode total has to be met first.
        """
        nearly = {str(n): "2026-08-%02d" % n for n in range(1, 13)}
        resp = self._post(history=nearly)
        self.assertNotIn("needs_decision", resp.json())
        self.assertIsNotNone(self._row(), "a season in progress was not added")

    def test_all_thirteen_is_finished(self):
        """The boundary from the other side, so the count is a threshold rather
        than a strict inequality nobody checked."""
        whole = {str(n): "2026-08-%02d" % n for n in range(1, 14)}
        self.assertIn("needs_decision", self._post(history=whole).json())

    def test_two_services_reporting_the_same_run_is_still_one_run(self):
        """PER SERVICE AND NOT SUMMED. Trakt and Simkl both listing the same
        seven episodes have each seen seven, not fourteen; summing them would
        call every co-tracked season finished at half way and ask a viewer to
        throw away a run they are in the middle of."""
        half = {str(n): "2026-08-%02d" % n for n in range(1, 8)}
        resp = self._search_add(sources={"trakt": half, "simkl": half})
        self.assertNotIn("needs_decision", resp.json())

    def test_a_season_lookup_that_failed_asks_nothing(self):
        """No total means nothing to compare a count against, and guessing would
        ask about seasons at random. The add proceeds as an ordinary one."""
        whole = {str(n): "2026-08-%02d" % n for n in range(1, 14)}
        resp = self._search_add(history=whole, detail={})
        self.assertNotIn("needs_decision", resp.json())
        self.assertIsNotNone(self._row())

    # -- the question --

    def test_a_finished_season_is_asked_about_and_nothing_is_written(self):
        resp = self._post(history=self.OLD_RUN)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        asked = resp.json().get("needs_decision")
        self.assertIsNotNone(asked, "a finished season was filed without asking")
        self.assertEqual(asked["completed_on"], "2020-02-14")
        self.assertEqual(asked["season"], 1)
        self.assertEqual(asked["title"], "Breaking Bad")
        self.assertIsNone(self._row(),
                          "the season was added before the question was answered")

    def test_the_offered_start_is_the_day_after_the_old_finish(self):
        """The last episode of that run was watched ON the finish day, so a
        floor including it would show a fresh run starting at one."""
        asked = self._post(history=self.OLD_RUN).json()["needs_decision"]
        self.assertEqual(asked["suggested_from"], "2020-02-15")

    def test_asking_writes_no_month_record_either(self):
        """Not merely no roster row: nothing at all. A question is not a change."""
        self._post(history=self.OLD_RUN)
        self.assertEqual(
            asyncio.run(store.month_records(self.user_id, "2026-09")), [])

    # -- answering it --

    def test_a_fresh_run_lands_at_zero_on_the_month_being_looked_at(self):
        """THE REPORTED SYMPTOM, INVERTED. This came out as 13/13 on the current
        month, or vanished onto February 2020. With the floor written as part of
        the add, the previous run's plays are not this pass's progress."""
        resp = self._post(history=self.OLD_RUN, decided=True,
                          history_from="2020-02-15")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row = self._row()
        self.assertIsNotNone(row, "the fresh run never made it onto the list")
        self.assertEqual(row["history_from"], "2020-02-15")
        floored = watch_history.apply_history_floor(
            _state(watched=self.OLD_RUN), watch_history.history_floors([row]))
        self.assertEqual(watch_history.watched_map(floored)[(str(KEY), 1)],
                         {"trakt": 0})

    def test_counting_what_was_already_watched_writes_no_floor(self):
        """The other answer, and the floor half of it needs no mechanism: no
        floor IS counting everything."""
        resp = self._post(history=self.OLD_RUN, decided=True, history_from="")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row = self._row()
        self.assertTrue(row is None or row["history_from"] == "")

    def test_counting_everything_files_the_season_as_completed_here(self):
        """AND THE OTHER HALF, WHICH THE AUTHOR DECIDED AFTER BROWSER-TESTING IT.
        "Count everything I have watched" about a season every episode of which
        has been seen means the season is FINISHED, so it belongs in Completed on
        the month being looked at.

        WHAT IT USED TO DO INSTEAD, and why that was not obviously wrong: the add
        wrote a roster row, and the recompute then asked `finish_if_done` to
        settle it — which refuses a month the tracker never tracked, because it
        is normally reached from an ordinary read and settling there would
        conjure a 2020 month out of merely looking at 2026. So the season stayed
        on the list at 13/13, in Cleanup, with no way to ever leave.
        """
        resp = self._post(history=self.OLD_RUN, decided=True, history_from="")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        done = asyncio.run(store.find_month_record(
            self.user_id, "2026-09", store.RecordKind.COMPLETED, KEY, 1))
        self.assertIsNotNone(done, "a finished season was not filed as completed")
        self.assertEqual((done["watched"], done["total"]), (13, 13))

    def test_the_completed_record_lands_on_the_month_being_looked_at(self):
        """NOT THE MONTH THE HISTORY NAMES. The plays are February 2020's; the
        viewer is standing on September 2026 and said to add it there."""
        self._post(history=self.OLD_RUN, decided=True, history_from="")
        self.assertIsNone(
            asyncio.run(store.find_month_record(
                self.user_id, "2020-02", store.RecordKind.COMPLETED, KEY, 1)),
            "the add invented a month out of the watch history")

    def test_a_completed_season_is_not_also_on_the_list(self):
        """Both would show it twice, and leave the roster row for the live pass
        to settle all over again."""
        self._post(history=self.OLD_RUN, decided=True, history_from="")
        self.assertIsNone(self._row())

    def test_a_declared_fresh_run_is_not_filed_as_completed(self):
        """The floor is the other answer and it must still reach the list: a
        re-watch is a season at zero, not a finished one."""
        self._post(history=self.OLD_RUN, decided=True, history_from="2020-02-15")
        self.assertIsNotNone(self._row(), "the fresh run never made the list")
        self.assertIsNone(asyncio.run(store.find_month_record(
            self.user_id, "2026-09", store.RecordKind.COMPLETED, KEY, 1)))

    def test_a_part_watched_season_answering_the_same_way_is_not_completed(self):
        """The completed filing is guarded by the history and not by the body.
        Twelve of thirteen with `decided` set is an ordinary add, whatever the
        request claims."""
        partial = {str(n): "2020-02-%02d" % (n + 1) for n in range(1, 13)}
        self._post(history=partial, decided=True, history_from="")
        self.assertIsNone(asyncio.run(store.find_month_record(
            self.user_id, "2026-09", store.RecordKind.COMPLETED, KEY, 1)))
        self.assertIsNotNone(self._row(), "an unfinished season left the list")

    def test_a_viewer_chosen_day_is_honoured_over_the_offered_one(self):
        """WHY THE DAY IS EDITABLE. Somebody who watched two episodes last week
        and then added the season wants those counted: they are part of THIS
        pass, and only the viewer knows where it began."""
        recent = {**self.OLD_RUN, "1": "2026-08-25", "2": "2026-08-26"}
        resp = self._post(history=recent, decided=True, history_from="2026-08-20")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row = self._row()
        self.assertEqual(row["history_from"], "2026-08-20")
        floored = watch_history.apply_history_floor(
            _state(watched=recent), watch_history.history_floors([row]))
        self.assertEqual(watch_history.watched_map(floored)[(str(KEY), 1)],
                         {"trakt": 2}, "the two recent plays did not count")

    def test_an_unreadable_day_counts_everything_rather_than_guessing(self):
        """A date this cannot read is a date nobody meant. Falling back to "no
        floor" counts everything, which is the answer that changes nothing and
        is therefore the safe one to be wrong about."""
        resp = self._post(history=self.OLD_RUN, decided=True,
                          history_from="last February")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        row = self._row()
        self.assertTrue(row is None or row["history_from"] == "")

    def test_a_second_add_does_not_ask_again_once_a_run_is_declared(self):
        """The floor is honoured when the question is asked, so a season
        somebody already declared a fresh run on is not interrogated about the
        run they are in the middle of."""
        self._post(history=self.OLD_RUN, decided=True, history_from="2020-02-15")
        again = self._post(history=self.OLD_RUN)
        self.assertNotIn("needs_decision", again.json())

    def test_a_settled_month_record_is_left_where_it_is(self):
        """A re-watch is a SECOND thing, not a move. The old viewing keeps the
        month it settled on and the current run exists beside it — the add writes
        a roster row and touches no month record."""
        month = "2026-02"
        asyncio.run(store.add_month_record(self.user_id, month, {
            "media": "show", "match_source": "tmdb", "match_id": "1396",
            "key": str(KEY), "season": 1, "title": "Breaking Bad",
            "ids": {"tmdb": 1396}, "network": "AMC",
            "kind": str(store.RecordKind.COMPLETED),
        }))
        self._post(history=self.OLD_RUN, decided=True, history_from="2020-02-15")
        kept = asyncio.run(store.find_month_record(
            self.user_id, month, store.RecordKind.COMPLETED, KEY, 1))
        self.assertIsNotNone(kept, "adding a re-watch removed the settled record")


class TheHistoryIsFetchedBeforeTheQuestionTests(_RosterAddCase):
    """A title being added for the FIRST time has no stored watch history, and
    the question is asked of stored watch history.

    THE FAULT THIS HOLDS SHUT, found by the author browser-testing against a copy
    of their real database. Adding a season finished years ago went straight onto
    the list without asking. Removing it and adding it AGAIN asked properly — so
    the feature appeared to work whenever it was tested twice, which is how
    testing goes. The first add had baselined the title on its way past, and the
    second add was reading what the first one fetched.

    WHICH IS THE ONLY ADD THE FEATURE HAS. A season you finished years ago is by
    definition not on your list, so every add of one is a first add; the question
    could not see the single case it exists for.

    THE CLASS ABOVE CANNOT CATCH THIS AND THAT IS WORTH SAYING OUT LOUD. Its
    double patches `baseline_show` to a no-op and hands `load_state` a fully
    populated state, so it supplies for free the exact thing the route had failed
    to fetch. The double here is the honest one: the state is EMPTY until
    `baseline_show` has been called, which is what the real pair does.
    """

    USER = "first-adder"

    def _post(self, **extra):
        resp = self._search_add(history=self.OLD_RUN, **extra)
        return resp, self.baselines

    def test_a_first_add_of_a_finished_season_still_asks(self):
        resp, _calls = self._post()
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertIn("needs_decision", resp.json(),
                      "the first add of a finished season did not ask")

    def test_it_writes_nothing_while_it_asks(self):
        """The rule the reordering must not break: the history fetch is allowed
        to happen before the question, the ROSTER ROW is not."""
        self._post()
        self.assertIsNone(
            asyncio.run(store.find_user_record(self.user_id, KEY, 1)),
            "asking the question put the season on the list anyway")

    def test_the_history_is_fetched_once_and_not_twice(self):
        """It used to run after the add. Moving it must not leave both."""
        _resp, calls = self._post()
        self.assertEqual(calls, 1)

    def test_the_answer_still_performs_the_add(self):
        resp, _calls = self._post(decided=True, history_from="2026-09-01")
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertNotIn("needs_decision", resp.json())
        row = asyncio.run(store.find_user_record(self.user_id, KEY, 1))
        self.assertIsNotNone(row, "the answered add did not land")
        self.assertEqual(row["history_from"], "2026-09-01")


class TheQuestionDoesNotDependOnWhichServiceSawItTests(_RosterAddCase):
    """A season finished according to SIMKL is asked about exactly as one
    finished according to Trakt.

    WHY THIS IS ASKED SEPARATELY. Only Trakt keeps a play LOG. Measured against
    the author's own account, Simkl returned 19,056 episode rows with not one
    repeated (title, season, episode), because /sync/all-items is a library
    snapshot carrying one date per episode. So the restart DETAIL — "you have
    watched three episodes since" — is something only Trakt's history can
    support.

    THAT MUST NOT LEAK INTO WHETHER THE QUESTION IS ASKED AT ALL, and it is the
    obvious way for this to go wrong. Deciding to re-watch a season BEFORE
    starting it is the common case and there is no repeated play in it: every
    episode has been seen, and that is the whole of the condition. A Simkl-only
    account gets the question in its plainer form, never a silent add.
    """

    USER = "simkl-only"
    RUN = _RosterAddCase.OLD_RUN

    def _post(self, *, source, history, **extra):
        return self._search_add(history=history, source=source, **extra)

    def test_a_season_simkl_says_is_finished_is_asked_about(self):
        body = self._post(source="simkl", history=self.RUN).json()
        self.assertIn("needs_decision", body,
                      "a Simkl-only account was not asked about a finished season")
        self.assertEqual(body["needs_decision"]["completed_on"], "2020-02-14")

    def test_it_asks_the_plainer_version_with_no_restart_detail(self):
        """Nothing in a library snapshot can say a second pass has begun, so the
        question offers the day after the last play and names no episodes."""
        decision = self._post(source="simkl", history=self.RUN).json()["needs_decision"]
        self.assertEqual(decision["restart"], {})
        self.assertEqual(decision["suggested_from"], "2020-02-15")

    def test_answering_it_completes_the_season_the_same_way(self):
        self._post(source="simkl", history=self.RUN, decided=True, history_from="")
        done = asyncio.run(store.find_month_record(
            self.user_id, "2026-09", store.RecordKind.COMPLETED, KEY, 1))
        self.assertIsNotNone(done, "the Simkl answer did not file the season")

    def test_a_season_watched_exactly_once_through_is_still_offered(self):
        """THE COMMON CASE, AND IT HAS NO REPEATED PLAY IN IT. Deciding to
        re-watch something happens BEFORE the second pass exists, so a rule
        wanting evidence of one would refuse the question precisely when it is
        most wanted."""
        for source in ("trakt", "simkl"):
            with self.subTest(source=source):
                body = self._post(source=source, history=self.RUN).json()
                self.assertIn("needs_decision", body)


class TheAnswerNamesTheRowItWroteTests(_RosterAddCase):
    """`highlight` on the response — which row the page should scroll to and mark.

    THE SERVER NAMES IT because the caller posts ids and a season number, and
    which row that becomes is decided by `resolve_key`'s rule about which of a
    title's ids wins. A page working it out from the payload would be a second
    copy of that rule written in another language.
    """

    USER = "pointer"
    # A season still airing, so an ordinary add lands on the list rather than
    # being asked about.
    SEASON = {"total": 13, "cadence": "Sun", "premiere": "2026-09-02",
              "finale": "2026-11-15", "started_airing": True,
              "finished_airing": False}

    def _add(self, **extra):
        return self._search_add(**extra)

    def test_an_add_names_the_season_it_wrote(self):
        body = self._add().json()
        self.assertEqual(body.get("highlight"), {"key": str(KEY), "season": 1})

    def test_the_name_matches_the_row_that_was_actually_written(self):
        """The point of it: a name that does not match what was stored marks the
        wrong row, or nothing. Checked against the store rather than against the
        rendered payload, because the two answer different questions — what was
        written, and what this month happens to draw."""
        body = self._add().json()
        named = body["highlight"]
        row = asyncio.run(store.find_user_record(
            self.user_id, ItemKey(*str(named["key"]).split(":")), named["season"]))
        self.assertIsNotNone(row, "the highlight named a row nothing wrote")

    def test_a_question_names_nothing_because_it_wrote_nothing(self):
        """A `needs_decision` response has no row to point at, and pointing at
        one would scroll the reader away from the question being asked."""
        body = self._search_add(
            history=self.OLD_RUN,
            detail={**self.SEASON, "finished_airing": True}).json()
        self.assertIn("needs_decision", body)
        self.assertNotIn("highlight", body)


class SayingYesToAnUnplacedPlayAsksTooTests(_RosterAddCase):
    """`/api/distrakt/unknown-add` — the OTHER door onto the roster, and it now
    asks the same question the search door does.

    WHY IT MATTERS MORE HERE, NOT LESS. That row exists because a play arrived
    that nothing could place, and for a season already watched right through
    that is exactly what beginning it again looks like. Saying yes used to add it
    silently at its full episode count — so a re-watch reached the list already
    finished, which is the precise failure the question was written to prevent,
    arrived at by the door nobody had checked.

    ONE IMPLEMENTATION, ASKED TWICE. Both routes go through
    `_rewatch_question`; a second copy would drift on which answer counts as
    "count everything", and that is a question about whose viewing gets recorded.
    """

    USER = "unplaced"

    def _post(self, **kwargs):
        return self._prompt_add(**kwargs)

    def test_the_history_is_fetched_before_the_question_here_too(self):
        """THE FAULT THIS HOLDS SHUT, found by the author in a browser after the
        search route had already been fixed for it. This route never baselined at
        all, so the question was asked of an empty watch state, found nothing
        finished, and said yes silently — the season then came back 18/18 in
        Completed because the month recompute baselined it a moment later. A
        second attempt worked, which is the signature of this bug: the first
        attempt is what fetches the history the second one reads.

        THE FETCH NOW LIVES INSIDE `_rewatch_question`, so the order cannot be
        got wrong by a caller that forgets it."""
        self._post(history=self.OLD_RUN)
        self.assertEqual(self.baselines, 1,
                         "the question was asked without fetching the history")

    def test_a_finished_season_is_asked_about_rather_than_added(self):
        resp = self._post(history=self.OLD_RUN)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        self.assertIn("needs_decision", resp.json(),
                      "saying yes to an unplaced play added a finished season silently")

    def test_it_writes_nothing_while_it_asks(self):
        self._post(history=self.OLD_RUN)
        self.assertIsNone(self._row(), "the question put the season on the list anyway")

    def test_an_ordinary_unplaced_play_is_still_added_without_a_question(self):
        """Which is what this row is for almost every time it appears."""
        resp = self._post(history={"1": "2026-09-01"})
        self.assertNotIn("needs_decision", resp.json())
        self.assertIsNotNone(self._row(), "an ordinary yes did not add the season")

    def test_answering_with_a_floor_adds_it_at_zero(self):
        self._post(history=self.OLD_RUN, decided=True, history_from="2026-09-01")
        row = self._row()
        self.assertIsNotNone(row, "the answered add did not land")
        self.assertEqual(row["history_from"], "2026-09-01")

    def test_answering_count_everything_files_it_as_completed_here(self):
        self._post(history=self.OLD_RUN, decided=True, history_from="")
        done = asyncio.run(store.find_month_record(
            self.user_id, "2026-09", store.RecordKind.COMPLETED, KEY, 1))
        self.assertIsNotNone(done, "the finished season was not filed")
        self.assertEqual((done["watched"], done["total"]), (13, 13))
        self.assertIsNone(self._row(), "it was filed as completed AND left on the list")

    def test_the_answer_names_the_row_it_wrote(self):
        body = self._post(history={"1": "2026-09-01"}).json()
        self.assertEqual(body.get("highlight"), {"key": str(KEY), "season": 1})


class TheOfferedStartFindsARestartTests(unittest.TestCase):
    """`watch_history.restart_day` — where a second pass through a season began.

    THE ANSWER IT REPLACES WAS THE WORST ONE FOR THIS SHAPE. The day offered was
    the day AFTER the last play, which is right for somebody who has not begun
    again and wrong for everybody who has: their most recent play is INSIDE the
    new run, so the offer landed after it and reported the run as empty. A viewer
    three episodes into a re-watch was shown zero.

    ORDER IS THE SIGNAL AND NOT ELAPSED TIME. Episodes are watched forwards, so
    the sequence going BACKWARDS is somebody returning to the start. That is a
    fact about the plays rather than a threshold on a gap — and no threshold is
    defensible, which is why this feature refused to define one.
    """

    def _state(self, plays, key=KEY, season=1, source="trakt"):
        return {"shows": {str(key): {"seasons": {str(season): {source: dict(plays)}}}}}

    def test_it_finds_where_the_sequence_turned_back(self):
        """THE REPORTED SHAPE, from a real library: fifteen episodes watched
        across two years, then episodes one to three on a single later day."""
        plays = {str(n): "2021-1%d-01" % (n % 10) for n in range(4, 10)}
        plays.update({str(n): "2022-0%d-01" % (n % 9 + 1) for n in range(10, 19)})
        plays.update({"1": "2023-04-30", "2": "2023-04-30", "3": "2023-04-30"})
        self.assertEqual(
            watch_history.restart_day(self._state(plays), KEY, 1), "2023-04-30")

    def test_a_season_watched_forwards_reports_no_restart(self):
        """The ordinary case, and the one that must not be disturbed: a viewer
        who has watched a season once still gets the day after their last play.
        """
        plays = {str(n): "2026-08-%02d" % n for n in range(1, 9)}
        self.assertEqual(watch_history.restart_day(self._state(plays), KEY, 1), "")

    def test_a_season_finished_over_two_sittings_is_not_a_restart(self):
        """MEASURED AGAINST A REAL LIBRARY, where this was the near miss:
        episodes 1-11 one autumn and 12-20 the next. The gap is long and the
        order never turns back, so nothing restarted — which is exactly why the
        rule reads the order rather than the gap."""
        plays = {str(n): "2018-11-%02d" % n for n in range(1, 12)}
        plays.update({str(n): "2019-09-%02d" % (n - 11) for n in range(12, 21)})
        self.assertEqual(watch_history.restart_day(self._state(plays), KEY, 1), "")

    def test_the_most_recent_turn_back_wins(self):
        """Somebody may have gone round more than once, and the pass they are in
        now is the last one."""
        plays = {"1": "2020-01-01", "2": "2020-01-02", "3": "2020-01-03"}
        state = self._state(plays)
        state["shows"][str(KEY)]["seasons"]["1"]["trakt"].update(
            {"1": "2024-06-01", "2": "2024-06-02"})
        # The map holds one date per episode, so a re-watch moves the date; what
        # survives is the later pass, and the turn-back is where it starts.
        self.assertEqual(watch_history.restart_day(state, KEY, 1), "2024-06-01")

    def test_a_season_with_no_dated_plays_answers_nothing(self):
        self.assertEqual(
            watch_history.restart_day(self._state({"1": "", "2": None}), KEY, 1), "")

    def test_each_service_is_read_on_its_own(self):
        """TWO SERVICES CAN DATE THE SAME EPISODES QUITE DIFFERENTLY — one may
        carry a bulk import stamped a single day — so mixing their dates into one
        sequence would invent an order neither reported."""
        state = {"shows": {str(KEY): {"seasons": {"1": {
            "simkl": {str(n): "2011-11-29" for n in range(1, 9)},
            "trakt": {**{str(n): "2026-02-%02d" % n for n in range(4, 9)},
                      "1": "2026-03-01", "2": "2026-03-01"},
        }}}}}
        self.assertEqual(watch_history.restart_day(state, KEY, 1), "2026-03-01")


class TheQuestionShowsWhatWasWatchedSinceTests(unittest.TestCase):
    """`watch_history.restart_details` — the modal's own content.

    THE QUESTION USED TO BE A DATE AND NOTHING ELSE, so answering it meant
    dating your own viewing from memory. The episodes are already known; naming
    them lets the choice be made by looking.
    """

    def _state(self, plays, key=KEY, season=1, source="trakt"):
        return {"shows": {str(key): {"seasons": {str(season): {source: dict(plays)}}}}}

    def test_it_names_the_episodes_watched_since_the_turn_back(self):
        plays = {str(n): "2022-04-%02d" % n for n in range(4, 19)}
        plays.update({"1": "2023-04-30", "2": "2023-04-30", "3": "2023-05-02"})
        got = watch_history.restart_details(self._state(plays), KEY, 1)
        self.assertEqual(got["began"], "2023-04-30")
        self.assertEqual([e["episode"] for e in got["episodes"]], [1, 2, 3])
        self.assertEqual(got["episodes"][2]["day"], "2023-05-02")

    def test_the_previous_run_ends_before_the_restart_and_not_at_the_last_play(self):
        """THE BUG THIS FIXES IN PASSING. `season_completed_map` answers "when
        was the last episode of this season watched", and for anybody mid-restart
        that is a play from the NEW run — so the page said "you finished this on
        30 April 2023" about the day they watched episode one. The old run ended
        at the last play BEFORE the turn-back.
        """
        plays = {str(n): "2022-04-%02d" % n for n in range(4, 19)}
        plays.update({"1": "2023-04-30", "2": "2023-04-30"})
        got = watch_history.restart_details(self._state(plays), KEY, 1)
        self.assertEqual(got["finished_on"], "2022-04-18")
        self.assertNotEqual(got["finished_on"], got["began"])

    def test_a_season_watched_once_has_nothing_to_show(self):
        """Which is the ordinary case, and the page then asks the plainer
        version of the question rather than an empty list."""
        plays = {str(n): "2026-08-%02d" % n for n in range(1, 9)}
        self.assertEqual(watch_history.restart_details(self._state(plays), KEY, 1), {})

    def test_the_episodes_come_from_the_service_that_saw_the_restart(self):
        """Two services can date the same episodes differently, so the list has
        to come from the same sequence the day came from — otherwise it names
        plays that service never placed there."""
        state = {"shows": {str(KEY): {"seasons": {"1": {
            "simkl": {str(n): "2011-11-29" for n in range(1, 9)},
            "trakt": {**{str(n): "2026-02-%02d" % n for n in range(4, 9)},
                      "1": "2026-03-01"},
        }}}}}
        got = watch_history.restart_details(state, KEY, 1)
        self.assertEqual(got["began"], "2026-03-01")
        self.assertEqual([e["episode"] for e in got["episodes"]], [1])
        self.assertEqual(got["finished_on"], "2026-02-08")
