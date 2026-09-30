"""The write step: which shows are owed a write to a viewer's own list at a
service, what each answer is filed as, and that the tracker never feels any of
it (app/distrakt/watchlist_writes.py).

Against a fake list (tests/distrakt/watchlist_fake.py) that behaves as Simkl's
was measured to. The months are derived from the clock, never spelled out.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from app import db, distrakt
from app.distrakt import lifecycle, watchlist_writes as ww
from app.providers.base import Source
from tests.distrakt.test_store import DistraktTestCase, a_record, month_back
from tests.distrakt.watchlist_fake import FakeList

SIMKL = str(Source.SIMKL)


def month_ahead(count: int) -> str:
    return month_back(-count)


class WatchlistTestCase(DistraktTestCase):
    """A distrakt user, a fake list standing in for Simkl, and the step's
    in-process state cleared either side of every test."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.fake = FakeList()
        self.linked = True
        self._patches = [
            patch("app.providers.for_watchlist",
                  lambda settings: [(Source.SIMKL, self.fake)] if self.linked else []),
            patch("app.auth.viewer.settings_for_user", AsyncMock(return_value=object())),
        ]
        for started in self._patches:
            started.start()
        self._reset()

    async def asyncTearDown(self):
        for started in self._patches:
            started.stop()
        self._reset()
        await super().asyncTearDown()

    @staticmethod
    def _reset():
        ww._locks.clear()
        ww._running.clear()
        ww._again.clear()
        ww._departures.clear()
        ww._trouble.clear()

    # what is on the tracker

    async def listed(self, tmdb: int, season: int = 1, **fields):
        await distrakt.add_user_record(self.user_id, a_record(
            tmdb, season=season, kind=distrakt.RecordKind.KEEPUP, title=f"Show {tmdb}", **fields))

    async def premiered(self, tmdb: int, month: str, season: int = 1, **fields):
        await distrakt.add_month_record(self.user_id, month, a_record(
            tmdb, season=season, kind=distrakt.premiere_kind(season),
            title=f"Show {tmdb}", **fields))

    async def settled(self, tmdb: int, month: str, kind, season: int = 1):
        await distrakt.add_month_record(self.user_id, month, a_record(
            tmdb, season=season, kind=kind, title=f"Show {tmdb}"))

    async def switch_on(self):
        await ww.switch(self.user_id, SIMKL, True)

    async def states(self) -> dict[str, tuple[str, str]]:
        return {key: (row.state, row.status_before)
                for key, row in (await ww.rows(self.user_id, SIMKL)).items()}


class OnTheTrackerTests(WatchlistTestCase):
    """Which shows count: the viewer's list and the premieres of the month under
    way and after it, and nothing that is history."""

    async def tracked(self) -> set[str]:
        return set(await ww.on_tracker(self.user_id))

    async def test_the_viewers_list_is_on_the_tracker(self):
        await self.listed(1)
        self.assertEqual(await self.tracked(), {"show:tmdb:1"})

    async def test_this_months_and_later_months_premieres_are_and_past_ones_are_not(self):
        await self.premiered(1, month_back(0))
        await self.premiered(2, month_ahead(1))
        await self.premiered(3, month_back(1))
        self.assertEqual(await self.tracked(), {"show:tmdb:1", "show:tmdb:2"})

    async def test_a_finished_or_abandoned_season_is_history(self):
        await self.settled(1, month_back(0), distrakt.RecordKind.COMPLETED)
        await self.settled(2, month_back(0), distrakt.RecordKind.ABANDONED)
        await self.settled(3, month_back(2), distrakt.RecordKind.COMPLETED)
        self.assertEqual(await self.tracked(), set())

    async def test_a_premiere_whose_season_was_settled_is_not_on_the_tracker(self):
        """The tracker's own rule: a season finished or given up on in its month
        is done with, though its premiere record stays to say it premiered."""
        await self.premiered(1, month_back(0))
        await self.settled(1, month_back(0), distrakt.RecordKind.ABANDONED)
        self.assertEqual(await self.tracked(), set())

    async def test_another_season_still_there_keeps_the_show(self):
        await self.premiered(1, month_back(0), season=2)
        await self.settled(1, month_back(0), distrakt.RecordKind.ABANDONED, season=2)
        await self.listed(1, season=1)
        self.assertEqual(await self.tracked(), {"show:tmdb:1"})

    async def test_a_film_is_never_on_it(self):
        await distrakt.add_month_record(self.user_id, month_back(0), a_record(
            7, media="movie", title="A Film"))
        self.assertEqual(await self.tracked(), set())

    async def test_seasons_collapse_into_one_show_carrying_every_id(self):
        await self.listed(1, season=1, ids={"tmdb": 1, "trakt": 11})
        await self.premiered(1, month_back(0), season=2, ids={"tmdb": 1, "simkl": 111})
        found = await ww.on_tracker(self.user_id)
        self.assertEqual(list(found), ["show:tmdb:1"])
        self.assertEqual({k: str(v) for k, v in found["show:tmdb:1"].ids.items()},
                         {"tmdb": "1", "trakt": "11", "simkl": "111"})


class ArrivalTests(WatchlistTestCase):
    """What a pass writes, and what it files each answer as."""

    async def test_switch_on_writes_everything_already_there(self):
        await self.listed(1)
        await self.premiered(2, month_back(0))
        self.fake.held = {"show:tmdb:1": "watching"}
        await self.switch_on()
        await ww.run(self.user_id)
        self.assertEqual(sorted(self.fake.sent("put")), ["show:tmdb:1", "show:tmdb:2"])
        self.assertEqual(self.fake.held, {"show:tmdb:1": "plantowatch",
                                          "show:tmdb:2": "plantowatch"})
        self.assertEqual(await self.states(), {"show:tmdb:1": ("written", "watching"),
                                               "show:tmdb:2": ("written", "none")})

    async def test_a_show_already_on_the_list_is_written_as_the_viewers_own(self):
        """ALREADY is WRITTEN with the list recorded as where it was, so an undo
        later finds nothing of the app's to take back."""
        await self.listed(1)
        self.fake.held = {"show:tmdb:1": "plantowatch"}
        await self.switch_on()
        await ww.run(self.user_id)
        self.assertEqual(await self.states(), {"show:tmdb:1": ("written", "plantowatch")})

    async def test_the_service_id_is_kept(self):
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        row = (await ww.rows(self.user_id, SIMKL))["show:tmdb:1"]
        self.assertEqual(row.service_id, "svc-show:tmdb:1")

    async def test_a_written_show_is_never_written_again_while_it_stays(self):
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        self.fake.held["show:tmdb:1"] = "watching"   # a play moved it on
        await self.listed(1, season=2)                # another season arrives
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1"])
        self.assertEqual(self.fake.held["show:tmdb:1"], "watching")

    async def test_not_found_and_refused_are_settled(self):
        await self.listed(1)
        self.fake.unknown = {"show:tmdb:1"}
        await self.switch_on()
        await ww.run(self.user_id)
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1"])
        self.assertEqual(await self.states(), {"show:tmdb:1": ("not_found", "none")})

    async def test_only_what_is_owed_is_sent(self):
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        await self.listed(2)
        await ww.run(self.user_id)
        self.assertEqual([keys for verb, keys in self.fake.calls if verb == "put"],
                         [["show:tmdb:1"], ["show:tmdb:2"]])

    async def test_a_write_the_list_does_not_show_is_failed_and_tried_again(self):
        await self.listed(1)
        self.fake.unconfirmed = {"show:tmdb:1"}
        await self.switch_on()
        await ww.run(self.user_id)
        self.assertEqual(await self.states(), {"show:tmdb:1": ("failed", "none")})
        self.assertIn("could not be confirmed", ww.trouble(self.user_id, SIMKL).message)
        # Quiet for a while: the next pass does not ask again at once.
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1"])
        # Once the quiet spell is over it does, and a success clears the trouble.
        self.fake.unconfirmed = set()
        with patch.object(ww, "QUIET_SECONDS", 0):
            await ww.run(self.user_id)
        self.assertEqual(await self.states(), {"show:tmdb:1": ("written", "none")})
        self.assertIsNone(ww.trouble(self.user_id, SIMKL))

    async def test_a_service_that_cannot_be_asked_fails_the_batch_and_raises_nothing(self):
        await self.listed(1)
        await self.listed(2)
        self.fake.fail = True
        await self.switch_on()
        await ww.run(self.user_id)   # must not raise
        self.assertEqual(await self.states(), {"show:tmdb:1": ("failed", "none"),
                                               "show:tmdb:2": ("failed", "none")})
        self.assertEqual(ww.trouble(self.user_id, SIMKL).message, "The fake service is down.")

    async def test_a_pass_that_breaks_outright_raises_nothing(self):
        await self.listed(1)
        await self.switch_on()
        with patch.object(ww, "on_tracker", AsyncMock(side_effect=RuntimeError("boom"))):
            await ww.run(self.user_id)
        self.assertEqual(self.fake.calls, [])

    async def test_an_unlinked_service_writes_nothing(self):
        await self.listed(1)
        await self.switch_on()
        self.linked = False
        await ww.run(self.user_id)
        self.assertEqual((self.fake.calls, await self.states()), ([], {}))
        # Linked again, the table still says what is owed.
        self.linked = True
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1"])

    async def test_switched_off_writes_nothing_and_keeps_what_it_did(self):
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        await ww.switch(self.user_id, SIMKL, False)
        await self.listed(2)
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1"])
        self.assertEqual(await self.states(), {"show:tmdb:1": ("written", "none")})
        self.assertEqual(await ww.switched_on(self.user_id), [])

    async def test_nothing_owed_asks_nobody(self):
        """The pass every tracker request starts must be free when there is
        nothing to do: not even the viewer's credentials are built."""
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        with patch("app.auth.viewer.settings_for_user",
                   AsyncMock(side_effect=AssertionError("asked"))):
            await ww.run(self.user_id)
        self.assertEqual(len(self.fake.calls), 1)


class ReturningTests(WatchlistTestCase):
    """A show that leaves the ordinary way is forgotten, so coming back is an
    arrival and is written again."""

    async def test_a_show_that_left_and_came_back_is_written_again(self):
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        # Finished: it leaves the list for a completed record.
        await distrakt.migrate_to_month(self.user_id, distrakt.record_key(a_record(1)), 1,
                                        month=month_back(0), kind=distrakt.RecordKind.COMPLETED)
        await ww.run(self.user_id)
        self.assertEqual(await self.states(), {})
        self.fake.held["show:tmdb:1"] = "completed"
        await self.premiered(1, month_ahead(1), season=2)
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1", "show:tmdb:1"])
        self.assertEqual(await self.states(), {"show:tmdb:1": ("written", "completed")})

    async def test_a_premiere_month_ending_is_a_departure(self):
        await self.premiered(1, month_back(0))
        await self.switch_on()
        await ww.run(self.user_id)
        await db.execute("UPDATE distrakt_month_records SET month = ? WHERE user_id = ?",
                         (month_back(1), self.user_id))
        await ww.run(self.user_id)
        self.assertEqual(await self.states(), {})


class OnePassAtATimeTests(WatchlistTestCase):
    """Two requests landing together must not write one show twice."""

    async def test_two_passes_at_once_write_each_show_once(self):
        await self.listed(1)
        await self.listed(2)
        await self.switch_on()
        self.fake.delay = 0.05
        await asyncio.gather(ww.run(self.user_id), ww.run(self.user_id), ww.run(self.user_id))
        self.assertEqual(sorted(self.fake.sent("put")), ["show:tmdb:1", "show:tmdb:2"])

    async def test_a_nudge_during_a_pass_goes_round_once_more(self):
        await self.listed(1)
        await self.switch_on()
        self.fake.delay = 0.05
        ww.nudge(self.user_id)
        await asyncio.wait_for(self.fake.in_flight.wait(), 5)
        self.assertTrue(ww.busy(self.user_id))
        await self.listed(2)              # arrives while the first pass is out
        ww.nudge(self.user_id)
        ww.nudge(self.user_id)
        while ww.busy(self.user_id):
            await asyncio.sleep(0.01)
        self.assertEqual([keys for verb, keys in self.fake.calls if verb == "put"],
                         [["show:tmdb:1"], ["show:tmdb:2"]])


class DepartureTests(WatchlistTestCase):
    """Giving up and the ✕: the only two departures that have an answer at the
    service, each only when it took the show's LAST season off the tracker."""

    KEY = "show:tmdb:1"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.listed(1)
        await self.switch_on()

    async def depart(self, departure, season: int = 1):
        """What a request does: say it, take the season off, finish, and the pass
        that follows."""
        pending = ww.expect(self.user_id, self.KEY, departure)
        key = distrakt.record_key(a_record(1))
        if departure is ww.Departure.ABANDON:
            record = await distrakt.find_user_record(self.user_id, key, season)
            await lifecycle.give_up(self.user_id, record, month=month_back(0))
        else:
            await distrakt.remove_user_record(self.user_id, key, season)
        await self.finish([pending])

    async def finish(self, departed):
        with patch.object(ww, "nudge", lambda user_id: None):
            await ww.after_request(self.user_id, departed)
        await ww.run(self.user_id)

    # abandon

    async def test_giving_up_on_the_last_season_drops_the_show(self):
        self.fake.held = {self.KEY: "watching"}
        await ww.run(self.user_id)
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(self.fake.held, {self.KEY: "dropped"})
        self.assertEqual(await self.states(), {self.KEY: ("dropped", "plantowatch")})
        self.assertEqual(ww._departures, {})

    async def test_giving_up_drops_it_whatever_the_viewer_had_it_as(self):
        await ww.run(self.user_id)
        self.fake.held[self.KEY] = "watching"     # a play moved it on since
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(self.fake.held, {self.KEY: "dropped"})

    async def test_another_season_still_there_is_not_a_departure(self):
        await self.listed(1, season=2)
        await ww.run(self.user_id)
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(self.fake.sent("drop"), [])
        self.assertEqual(self.fake.held, {self.KEY: "plantowatch"})
        self.assertEqual(ww._departures, {})

    async def test_a_show_the_service_never_held_is_not_added_to_drop_it(self):
        self.fake.unknown = {self.KEY}
        await ww.run(self.user_id)               # not found: nothing held there
        self.fake.unknown = set()
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(self.fake.held, {})
        self.assertEqual(await self.states(), {self.KEY: ("dropped", "none")})

    async def test_taking_it_back_writes_it_again(self):
        await ww.run(self.user_id)
        await self.depart(ww.Departure.ABANDON)
        await lifecycle.take_back(self.user_id, distrakt.record_key(a_record(1)), 1,
                                  month=month_back(0))
        await ww.run(self.user_id)
        self.assertEqual(self.fake.held, {self.KEY: "plantowatch"})
        self.assertEqual(await self.states(), {self.KEY: ("written", "dropped")})

    # the ✕

    async def test_the_x_puts_back_what_the_viewer_had(self):
        self.fake.held = {self.KEY: "watching"}
        await ww.run(self.user_id)
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.fake.held, {self.KEY: "watching"})
        self.assertEqual(await self.states(), {self.KEY: ("undone", "plantowatch")})

    async def test_the_x_leaves_a_show_the_viewer_listed_themselves(self):
        self.fake.held = {self.KEY: "plantowatch"}
        await ww.run(self.user_id)
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.fake.held, {self.KEY: "plantowatch"})
        self.assertEqual(await self.states(), {self.KEY: ("undone", "plantowatch")})

    async def test_the_x_takes_a_show_it_added_out_again_when_nothing_is_watched(self):
        await ww.run(self.user_id)
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.fake.held, {})

    async def test_the_x_keeps_a_show_it_added_once_something_is_watched(self):
        await ww.run(self.user_id)
        self.fake.watched = {self.KEY}
        self.fake.held[self.KEY] = "watching"
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.fake.held, {self.KEY: "watching"})
        self.assertEqual(await self.states(), {self.KEY: ("undone", "watching")})

    async def test_the_x_undoes_nothing_the_app_did_not_write(self):
        self.fake.unknown = {self.KEY}
        await ww.run(self.user_id)
        await self.depart(ww.Departure.REMOVE)
        self.assertEqual(self.fake.sent("undo"), [])

    async def test_the_x_on_one_of_two_seasons_changes_nothing(self):
        await self.listed(1, season=2)
        self.fake.held = {self.KEY: "watching"}
        await ww.run(self.user_id)
        await self.depart(ww.Departure.REMOVE, season=2)
        self.assertEqual(self.fake.sent("undo"), [])
        self.assertEqual(self.fake.held, {self.KEY: "plantowatch"})

    async def test_an_x_and_its_return_write_it_again(self):
        await ww.run(self.user_id)
        await self.depart(ww.Departure.REMOVE)
        await self.listed(1)
        await ww.run(self.user_id)
        self.assertEqual(self.fake.held, {self.KEY: "plantowatch"})

    # failures and timing

    async def test_a_failed_drop_changes_no_row_and_is_tried_again(self):
        await ww.run(self.user_id)
        self.fake.fail = True
        await self.depart(ww.Departure.ABANDON)
        self.assertEqual(await self.states(), {self.KEY: ("written", "none")})
        self.assertIn(self.KEY, ww._departures[self.user_id])
        self.fake.fail = False
        with patch.object(ww, "QUIET_SECONDS", 0):
            await ww.run(self.user_id)
        self.assertEqual(self.fake.held, {self.KEY: "dropped"})
        self.assertEqual(ww._departures, {})

    async def test_a_pass_before_the_request_finishes_neither_acts_nor_forgets(self):
        """A pass that read the tracker before this request's removal was written
        would find the show still there. It must leave the departure waiting and
        the row alone, and the pass after the request answers it."""
        self.fake.held = {self.KEY: "watching"}
        await ww.run(self.user_id)
        pending = ww.expect(self.user_id, self.KEY, ww.Departure.REMOVE)
        await ww.run(self.user_id)                         # show still there
        await distrakt.remove_user_record(self.user_id, distrakt.record_key(a_record(1)), 1)
        await ww.run(self.user_id)                         # gone, request unfinished
        self.assertEqual(await self.states(), {self.KEY: ("written", "watching")})
        self.assertEqual(self.fake.sent("undo"), [])
        await self.finish([pending])
        self.assertEqual(self.fake.held, {self.KEY: "watching"})
        self.assertEqual(await self.states(), {self.KEY: ("undone", "plantowatch")})

    async def test_a_withdrawn_departure_is_never_answered(self):
        await ww.run(self.user_id)
        pending = ww.expect(self.user_id, "show:tmdb:5", ww.Departure.ABANDON)
        ww.withdraw(self.user_id, pending)
        await self.finish([pending])
        self.assertEqual(self.fake.sent("drop"), [])

    async def test_ten_departures_in_a_row_go_out_in_few_batches(self):
        """Ten shows given up on as fast as requests can land: the first starts a
        pass, the other nine wait for it, and the ONE pass they fold into sends
        all nine together — every show dropped, never one call per show."""
        keys = [f"show:tmdb:{n}" for n in range(1, 11)]
        for n in range(2, 11):
            await self.listed(n)
        await ww.run(self.user_id)
        self.fake.delay = 0.2
        for n in range(1, 11):
            pending = ww.expect(self.user_id, keys[n - 1], ww.Departure.ABANDON)
            record = await distrakt.find_user_record(
                self.user_id, distrakt.record_key(a_record(n)), 1)
            await lifecycle.give_up(self.user_id, record, month=month_back(0))
            await ww.after_request(self.user_id, [pending])
        while ww.busy(self.user_id):
            await asyncio.sleep(0.05)
        drops = [len(batch) for verb, batch in self.fake.calls if verb == "drop"]
        self.assertEqual(sum(drops), 10)
        self.assertLessEqual(len(drops), 3, drops)
        self.assertEqual({self.fake.held[key] for key in keys}, {"dropped"})

    async def test_with_nothing_switched_on_a_finished_departure_is_let_go(self):
        await ww.switch(self.user_id, SIMKL, False)
        pending = ww.expect(self.user_id, self.KEY, ww.Departure.ABANDON)
        await self.finish([pending])
        self.assertEqual(ww._departures, {})


class RestoreTests(WatchlistTestCase):
    """A restore replaces the tracker and leaves what was written at the
    services alone: neither table travels in a backup, and the next pass
    compares them with the restored tracker like any other."""

    async def test_a_backup_carries_neither_table(self):
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        exported = await distrakt.export_user_data(self.user_id)
        self.assertFalse({"watchlist_targets", "watchlist_writes",
                          "distrakt_listed_ids"} & set(exported))

    async def test_both_survive_a_restore_and_the_restored_tracker_is_written(self):
        await self.listed(2)
        backup = await distrakt.export_user_data(self.user_id)   # holds show 2 only
        await distrakt.remove_user_record(self.user_id, distrakt.record_key(a_record(2)), 1)
        await self.listed(1)
        await self.switch_on()
        await ww.run(self.user_id)
        await db.execute("INSERT INTO distrakt_listed_ids (user_id, source, service_id) "
                         "VALUES (?, ?, ?)", (self.user_id, SIMKL, 42))

        await distrakt.restore_user_data(self.user_id, backup)

        self.assertEqual(await ww.switched_on(self.user_id), [SIMKL])
        self.assertEqual(await self.states(), {"show:tmdb:1": ("written", "none")})
        listed = await db.fetch_all("SELECT service_id FROM distrakt_listed_ids "
                                    "WHERE user_id = ?", (self.user_id,))
        self.assertEqual([row["service_id"] for row in listed], [42])
        await ww.run(self.user_id)
        self.assertEqual(self.fake.sent("put"), ["show:tmdb:1", "show:tmdb:2"])
        # Show 1 is not in the restored tracker: its row is let go, so it is
        # written again should it come back. Nothing was taken off the list.
        self.assertEqual(await self.states(), {"show:tmdb:2": ("written", "none")})
        self.assertEqual(self.fake.held, {"show:tmdb:1": "plantowatch",
                                          "show:tmdb:2": "plantowatch"})
