"""What a season's record becomes as its life proceeds.

Every transition in app/distrakt/lifecycle.py, driven directly rather than
through a route: which table a season is in after each step, which month a
verdict lands on, and what survives a migration.

NOTHING IS PATCHED HERE, and that is itself part of what is under test. The
transitions are derived from facts a caller already holds — the season lookup's
air dates, the watch history's completion dates — so a test that had to patch a
fetch would mean one of them had started reaching for something. The one
transition that needs a provider is HANDED one, which is why a reopening is
tested with a plain callable that counts how often it was asked: how many lookups
a reopening costs is part of the rule, and a countable argument is how a test can
say so.

Every month is DERIVED FROM THE CLOCK. These rules compare a month key against
today, so a written-out "2026-08" would be a different distance from today every
month and would test the wrong rule most of them.
"""
from __future__ import annotations

import unittest

from app import clock, db, distrakt
from app.distrakt import lifecycle, store, watch_history
from app.providers.base import ItemKey

from tests.support import new_db_path


def _key(tid: int) -> ItemKey:
    """The identity a fixture's numeric id is filed under. Its tmdb wins the
    identity waterfall, so a test can name a season without spelling the triple
    out and still be reading what the store actually keyed the row on."""
    return ItemKey("show", "tmdb", str(tid))


def _ids(tid: int) -> dict:
    return {"trakt": tid, "tmdb": tid, "slug": f"slug-{tid}"}


def _show(tid: int, season: int = 1, *, kind=None, watched: int = 0, total: int = 8,
          started: bool = True, finished: bool = False, cadence: str = "Mon",
          **extra) -> dict:
    """One season in the flat shape a live pass produces: the stored record's
    fields plus what the provider says about it right now."""
    show = {
        "media": "show", "ids": _ids(tid), "match_source": "tmdb",
        "match_id": str(tid), "key": str(_key(tid)),
        "season": season, "title": f"Show {tid}",
        "network": "Net", "watched": watched, "total": total, "cadence": cadence,
        "premiere": "1/6", "finale": "3/1" if finished else None,
        "started_airing": started, "finished_airing": finished,
    }
    if kind is not None:
        show["kind"] = str(kind)
    show.update(extra)
    return show


def _live_key(tid: int, season: int = 1) -> tuple[str, int]:
    return (str(_key(tid)), season)


def _month_back(count: int) -> str:
    """The key of the month `count` months before the one in progress. Derived
    from the clock so "an earlier month" keeps meaning that after the calendar
    rolls; count=0 is the month in progress."""
    today = clock.today()
    total = today.year * 12 + (today.month - 1) - count
    return store.month_key(total // 12, total % 12 + 1)


class LifecycleTestCase(unittest.IsolatedAsyncioTestCase):
    """Fresh database, one tracker user, and the months named relative to
    today."""

    async def asyncSetUp(self):
        new_db_path("lifecycle")
        await db.migrate()
        self.this_month = _month_back(0)
        self.last_month = _month_back(1)
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
            "created_at, updated_at) VALUES ('tracker', 1, 1, 1, ?, ?)", (now, now))
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def listed_kinds(self) -> dict[tuple[str, int], str]:
        return {(r["key"], r["season"]): r["kind"]
                for r in await store.user_records(self.user_id)}

    async def month_kinds(self, month: str) -> dict[tuple[str, int], str]:
        return {(r["key"], r["season"]): r["kind"]
                for r in await store.month_records(self.user_id, month)}


class WhichListASeasonIsOnTests(LifecycleTestCase):
    """Keeping up with something and being behind on it are two states of one
    row, and which one it is comes from the air dates already in hand."""

    def test_a_season_still_airing_weekly_is_kept_up_with(self):
        self.assertEqual(lifecycle.listed_kind(_show(1)), store.RecordKind.KEEPUP)

    def test_a_season_that_has_finished_airing_is_caught_up_on(self):
        self.assertEqual(lifecycle.listed_kind(_show(1, finished=True)),
                         store.RecordKind.CATCHUP)

    def test_a_binge_release_is_caught_up_on_from_the_start(self):
        """The whole season lands at once, so there is no pace to keep — it is a
        pile from the day it arrives, however recent that is."""
        self.assertEqual(lifecycle.listed_kind(_show(1, cadence="b")),
                         store.RecordKind.CATCHUP)

    def test_a_binge_release_still_weeks_away_is_not_a_pile_to_get_through(self):
        """Its cadence is 'b' from the moment the date is known, so reading the
        cadence alone offered a season to catch up on before a single episode of
        it existed — it turned up under Cleanup with its start date still ahead."""
        self.assertEqual(lifecycle.listed_kind(_show(1, cadence="b", started=False)),
                         store.RecordKind.KEEPUP)

    def test_a_season_whose_length_is_unknown_is_never_finished(self):
        """A lookup that could not say how long the season is must not read as a
        season with nothing in it — that would complete every title the provider
        was briefly unable to answer about."""
        self.assertFalse(lifecycle.is_finished(_show(1, watched=0, total=0)))
        self.assertTrue(lifecycle.is_finished(_show(1, watched=8, total=8)))

    async def test_the_finale_airing_moves_the_row_rather_than_adding_one(self):
        await lifecycle.follow(self.user_id, _show(1))
        self.assertEqual(await self.listed_kinds(),
                         {_live_key(1): store.RecordKind.KEEPUP})

        await lifecycle.follow(self.user_id, _show(1, finished=True))
        self.assertEqual(await self.listed_kinds(),
                         {_live_key(1): store.RecordKind.CATCHUP})


class APremiereThatStartsAiringTests(LifecycleTestCase):
    """The first episode airing copies the season onto the viewer's list, and the
    month keeps its announcement."""

    async def _announce(self, tid: int, season: int = 1) -> None:
        await store.add_month_record(self.user_id, self.this_month, _show(
            tid, season, kind=store.premiere_kind(season), started=False, total=0))

    async def test_it_joins_the_list_and_the_premiere_record_stays(self):
        await self._announce(1)
        shape = await lifecycle.advance(
            self.user_id, self.this_month,
            premieres=[_show(1, kind=store.RecordKind.SERIES_PREMIERE, started=True)],
            listed=[], completed_on={})

        self.assertEqual(await self.listed_kinds(),
                         {_live_key(1): store.RecordKind.KEEPUP})
        self.assertEqual(await self.month_kinds(self.this_month),
                         {_live_key(1): store.RecordKind.SERIES_PREMIERE})
        self.assertEqual([s["key"] for s in shape.series_premieres], [str(_key(1))])
        self.assertEqual([s["key"] for s in shape.listed], [str(_key(1))])

    async def test_a_season_that_has_not_aired_stays_only_an_announcement(self):
        await self._announce(2, season=2)
        shape = await lifecycle.advance(
            self.user_id, self.this_month,
            premieres=[_show(2, 2, kind=store.RecordKind.SEASON_PREMIERE, started=False)],
            listed=[], completed_on={})

        self.assertEqual(await self.listed_kinds(), {})
        self.assertEqual([s["key"] for s in shape.season_premieres], [str(_key(2))])
        self.assertEqual(shape.series_premieres, [])

    async def test_an_unaired_season_the_month_announces_comes_off_the_list(self):
        """A season on the list that has not started airing is in the wrong place:
        the list is what the viewer is in the MIDDLE of, and nothing has aired to
        be in the middle of. Held in both, it renders twice — once under its
        announcement and again under Keepup or Cleanup."""
        await self._announce(3)
        await lifecycle.follow(self.user_id, _show(3, started=False))
        self.assertEqual(await self.listed_kinds(), {_live_key(3): "keepup"})

        shape = await lifecycle.advance(
            self.user_id, self.this_month,
            premieres=[_show(3, kind=store.RecordKind.SERIES_PREMIERE, started=False)],
            listed=[_show(3, started=False)], completed_on={})

        self.assertEqual(await self.listed_kinds(), {},
                         "it stayed on the list as well as in the announcement")
        self.assertEqual(shape.listed, [])
        # Off the list, NOT off the page: the announcement still carries it.
        self.assertEqual([s["key"] for s in shape.series_premieres], [str(_key(3))])

    async def test_an_unaired_season_becomes_this_months_announcement(self):
        """The case a split cannot decide and a load can. Nothing announces it
        yet, but the live pass has just been told when it premieres — and that
        date falls in this month — so the month files it as its own announcement
        rather than leaving it sitting on the list as work in hand."""
        this_month_number = store.parse_month_key(self.this_month)[1]
        unaired = _show(4, started=False, premiere=f"{this_month_number}/20")
        await lifecycle.follow(self.user_id, unaired)
        self.assertEqual(await self.listed_kinds(), {_live_key(4): "keepup"})

        shape = await lifecycle.advance(
            self.user_id, self.this_month, premieres=[],
            listed=[unaired], completed_on={})

        self.assertEqual(await self.listed_kinds(), {},
                         "it was left on the list as something in hand")
        self.assertEqual(await self.month_kinds(self.this_month),
                         {_live_key(4): store.RecordKind.SERIES_PREMIERE})
        self.assertEqual([s["key"] for s in shape.series_premieres], [str(_key(4))])
        self.assertEqual(shape.listed, [])

    async def test_an_unaired_season_belonging_to_another_month_is_left_alone(self):
        """No rule here may make a row vanish. This month does not announce it and
        its premiere falls elsewhere, so the list is the only thing holding it —
        taking it off would remove it from the page rather than move it."""
        other = (store.parse_month_key(self.this_month)[1] % 12) + 1
        stray = _show(5, started=False, premiere=f"{other}/20")
        await lifecycle.follow(self.user_id, stray)
        shape = await lifecycle.advance(
            self.user_id, self.this_month, premieres=[],
            listed=[stray], completed_on={})

        self.assertEqual(await self.listed_kinds(), {_live_key(5): "keepup"})
        self.assertEqual([s["key"] for s in shape.listed], [str(_key(5))])
        self.assertEqual(await self.month_kinds(self.this_month), {})

    async def test_a_revised_episode_count_corrects_the_premiere_record(self):
        """A premiere record carries no viewer progress, which is exactly what
        makes the catalogue half of it safe to correct for ever."""
        await self._announce(3)
        await lifecycle.advance(
            self.user_id, self.this_month,
            premieres=[_show(3, kind=store.RecordKind.SERIES_PREMIERE, total=12,
                             watched=9)],
            listed=[], completed_on={})

        record, = await store.month_records(self.user_id, self.this_month)
        self.assertEqual(record["total"], 12)
        self.assertEqual(record["watched"], 0, "viewer progress reached a premiere record")


class TheViewerFinishesItTests(LifecycleTestCase):
    """Finishing takes the season off the list and onto the month the WATCH
    HISTORY dates it to — never the month on screen."""

    async def test_it_lands_on_the_month_the_history_names(self):
        await lifecycle.follow(self.user_id, _show(1, watched=8, total=8))
        await lifecycle.advance(
            self.user_id, self.this_month, premieres=[],
            listed=[_show(1, kind=store.RecordKind.CATCHUP, watched=8, total=8,
                          finished=True)],
            completed_on={_live_key(1): f"{self.last_month}-14"})

        self.assertEqual(await self.listed_kinds(), {})
        self.assertEqual(await self.month_kinds(self.last_month),
                         {_live_key(1): store.RecordKind.COMPLETED})
        self.assertEqual(await self.month_kinds(self.this_month), {})

    async def test_the_month_it_lands_on_is_the_one_this_month_reports(self):
        """Finished during the month being read, so this is one of its own
        verdicts and the shape it hands back says so."""
        await lifecycle.follow(self.user_id, _show(2, watched=8, total=8))
        shape = await lifecycle.advance(
            self.user_id, self.this_month, premieres=[],
            listed=[_show(2, kind=store.RecordKind.KEEPUP, watched=8, total=8)],
            completed_on={_live_key(2): f"{self.this_month}-02"})

        self.assertEqual([s["key"] for s in shape.settled], [str(_key(2))])
        self.assertEqual(shape.listed, [])

    async def test_a_finish_with_no_date_leaves_the_season_where_it_is(self):
        """"Finished, month unknown" would have to guess a month, and a completed
        record on the wrong one is worse than a season that lingers on the list."""
        await lifecycle.follow(self.user_id, _show(3, watched=8, total=8))
        shape = await lifecycle.advance(
            self.user_id, self.this_month, premieres=[],
            listed=[_show(3, kind=store.RecordKind.KEEPUP, watched=8, total=8)],
            completed_on={})

        self.assertEqual(list(await self.listed_kinds()), [_live_key(3)])
        self.assertEqual([s["key"] for s in shape.listed], [str(_key(3))])

    async def test_the_counts_it_settled_on_travel_with_the_verdict(self):
        await lifecycle.follow(self.user_id, _show(4, watched=6, total=6))
        await lifecycle.advance(
            self.user_id, self.this_month, premieres=[],
            listed=[_show(4, kind=store.RecordKind.CATCHUP, watched=6, total=6,
                          finished=True)],
            completed_on={_live_key(4): f"{self.this_month}-09"})

        record, = await store.month_records(self.user_id, self.this_month)
        self.assertEqual((record["watched"], record["total"]), (6, 6))


class TheViewerGivesUpTests(LifecycleTestCase):
    """Giving up is an ACT: it happens today, whichever month is on screen."""

    async def test_it_leaves_the_list_and_lands_on_the_month_it_is_given(self):
        await lifecycle.follow(self.user_id, _show(1, watched=2))
        record = await lifecycle.give_up(
            self.user_id, _show(1, watched=2), month=self.this_month,
            abandoned_form="`Show 1 S01 (2/8)`")

        self.assertEqual(record["kind"], store.RecordKind.ABANDONED)
        self.assertEqual(record["abandoned_form"], "`Show 1 S01 (2/8)`")
        self.assertEqual(await self.listed_kinds(), {})
        self.assertEqual(await self.month_kinds(self.this_month),
                         {_live_key(1): store.RecordKind.ABANDONED})

    async def test_a_season_that_was_never_on_the_list_is_still_given_up_on(self):
        """A premiere nobody ever started has no listed row to migrate, and
        refusing there would make the control do nothing on exactly the rows it is
        most often pressed on."""
        record = await lifecycle.give_up(
            self.user_id, _show(2, started=False, total=0), month=self.this_month)

        self.assertEqual(record["kind"], store.RecordKind.ABANDONED)
        self.assertEqual(await self.month_kinds(self.this_month),
                         {_live_key(2): store.RecordKind.ABANDONED})

    async def test_a_month_that_premiered_it_holds_both_records(self):
        """Premiered and settled in the same month is two statements about that
        month, not a duplicate."""
        await store.add_month_record(self.user_id, self.this_month, _show(
            3, kind=store.RecordKind.SERIES_PREMIERE))
        await lifecycle.give_up(self.user_id, _show(3), month=self.this_month)

        kinds = sorted(r["kind"] for r in
                       await store.month_records(self.user_id, self.this_month))
        self.assertEqual(kinds, [store.RecordKind.ABANDONED,
                                 store.RecordKind.SERIES_PREMIERE])

    async def test_taking_it_back_migrates_it_and_the_month_row_does_not_survive(self):
        await lifecycle.follow(self.user_id, _show(4, watched=2, finished=True))
        await lifecycle.give_up(self.user_id, _show(4, watched=2, finished=True),
                                month=self.this_month)

        restored = await lifecycle.take_back(self.user_id, _key(4), 1,
                                             month=self.this_month)

        self.assertEqual(restored["kind"], store.RecordKind.CATCHUP,
                         "the air dates decide which list it comes back onto")
        self.assertEqual(await self.month_kinds(self.this_month), {},
                         "the month went on recording a verdict just withdrawn")

    async def test_taking_back_something_no_month_settled_says_so(self):
        self.assertIsNone(
            await lifecycle.take_back(self.user_id, _key(5), 1, month=self.this_month))


class WhereASeasonIsHeldTests(LifecycleTestCase):
    """The search order a watch-history reconciliation has to ask in: the
    viewer's own list, then this month's premieres, then back over what earlier
    months settled."""

    async def _find(self, tid: int):
        return await lifecycle.find_season(self.user_id, _key(tid), 1,
                                           month=self.this_month)

    async def test_the_viewers_own_list_answers_first(self):
        await lifecycle.follow(self.user_id, _show(1))
        placed = await self._find(1)
        self.assertEqual(placed.record["kind"], store.RecordKind.KEEPUP)
        self.assertIsNone(placed.month, "a viewer record was given a month")

    async def test_this_months_premieres_answer_next(self):
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(2, kind=store.RecordKind.SERIES_PREMIERE))
        placed = await self._find(2)
        self.assertEqual(placed.record["kind"], store.RecordKind.SERIES_PREMIERE)
        self.assertEqual(placed.month, self.this_month)

    async def test_an_earlier_months_verdict_is_found_by_walking_back(self):
        await store.add_month_record(self.user_id, self.last_month,
                                     _show(3, kind=store.RecordKind.COMPLETED,
                                           watched=8))
        placed = await self._find(3)
        self.assertEqual(placed.record["kind"], store.RecordKind.COMPLETED)
        self.assertEqual(placed.month, self.last_month)

    async def test_nothing_anywhere_is_a_real_answer(self):
        """Not a failure: it is the case where guessing would cost a provider
        call for every unmatched episode of a viewing life."""
        self.assertIsNone(await self._find(4))

    async def test_the_list_wins_over_a_month_holding_the_same_season(self):
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(5, kind=store.RecordKind.SERIES_PREMIERE))
        await lifecycle.follow(self.user_id, _show(5))
        placed = await self._find(5)
        self.assertIsNone(placed.month)

    async def test_a_verdict_the_month_under_way_reached_is_not_among_them(self):
        """THE BOUNDARY THE OTHER SEARCH EXISTS TO CROSS, pinned here so that
        widening it is a visibly failing test rather than a quiet change of what a
        play does. A reconciliation sets aside everything this month settled before
        it asks, so this search must not offer one back: a completed verdict handed
        to reconcile_history is reopened, and one reached from these very counts
        has nothing to withdraw."""
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(6, kind=store.RecordKind.COMPLETED,
                                           watched=8))
        self.assertIsNone(await self._find(6))


class WhereARowThePageDrewIsHeldTests(LifecycleTestCase):
    """The page's own question, which covers one place more than a
    reconciliation's: what the month under way has already settled.

    A season finished this month has left the viewer's list, is not a premiere,
    and sits on a month the settled walk starts one step after — so all three of
    find_season's places miss it while the row is on screen with a modal to open.
    """

    async def _find(self, tid: int):
        return await lifecycle.find_shown_season(self.user_id, _key(tid), 1,
                                                 month=self.this_month)

    async def test_a_season_this_month_completed_is_found(self):
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(1, kind=store.RecordKind.COMPLETED,
                                           watched=8))
        placed = await self._find(1)
        self.assertEqual(placed.record["kind"], store.RecordKind.COMPLETED)
        self.assertEqual(placed.month, self.this_month)

    async def test_a_season_this_month_was_given_up_on_is_found_too(self):
        """Both verdicts are rows the page draws, so both are rows the modal has
        to be able to answer about."""
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(2, kind=store.RecordKind.ABANDONED))
        placed = await self._find(2)
        self.assertEqual(placed.record["kind"], store.RecordKind.ABANDONED)
        self.assertEqual(placed.month, self.this_month)

    async def test_an_earlier_months_verdict_still_answers(self):
        """The composition, not a replacement: everything the reconciliation
        search already found is still found, through that search."""
        await store.add_month_record(self.user_id, self.last_month,
                                     _show(3, kind=store.RecordKind.COMPLETED,
                                           watched=8))
        placed = await self._find(3)
        self.assertEqual(placed.month, self.last_month)

    async def test_the_viewers_own_list_still_answers_first(self):
        await lifecycle.follow(self.user_id, _show(4))
        self.assertIsNone((await self._find(4)).month)

    async def test_nothing_anywhere_is_still_a_real_answer(self):
        self.assertIsNone(await self._find(5))


class CountingLookup:
    """A season lookup that answers a fixed detail and counts how often it was
    asked. The counting IS the test in places: a reopening costs ONE lookup for
    the season, never one per episode watched of it."""

    def __init__(self, detail: dict | None = None):
        self.detail = detail or {}
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, record: dict, season: int) -> dict:
        self.calls.append((record["key"], season))
        return dict(self.detail)


def _play(tid: int, season: int = 1, number: int = 1, title: str = "") -> watch_history.EpisodePlay:
    """One episode play in the shape the history pull hands over — the identity,
    the episode, the title as the event spelled it, and the ids it carried.
    Nothing looked up."""
    return watch_history.EpisodePlay(_key(tid), season, number,
                                     title or f"Show {tid}",
                                     {"trakt": tid, "tmdb": tid})


class ASeasonThatTurnedOutNotToBeFinishedTests(LifecycleTestCase):
    """A season is completed on the episode count known at the time, and
    providers learn of more later. The watch history is the signal."""

    async def _settle(self, tid: int, month: str, *, watched: int = 8, total: int = 8):
        return await store.add_month_record(
            self.user_id, month, _show(tid, kind=store.RecordKind.COMPLETED,
                                       watched=watched, total=total, finished=True))

    async def _reconcile(self, plays, lookup):
        """The plays nothing could place, which is what most of these assert on.
        The questions a pull raises are `_questions`."""
        return (await self._questions(plays, lookup)).unknown

    async def _questions(self, plays, lookup):
        return await lifecycle.reconcile_history(
            self.user_id, plays, month=self.this_month, look_up=lookup)

    async def test_a_verdict_on_a_past_month_is_withdrawn_and_the_season_relisted(self):
        await self._settle(1, self.last_month)
        lookup = CountingLookup({"total": 10, "finished_airing": False, "cadence": "Mon"})
        self.assertEqual(await self._reconcile([_play(1)], lookup), [])
        self.assertEqual(await self.month_kinds(self.last_month), {},
                         "the month went on recording a verdict it no longer settled")
        listed = await store.find_user_record(self.user_id, _key(1), 1)
        self.assertEqual(listed["kind"], store.RecordKind.KEEPUP)
        self.assertEqual(listed["total"], 10, "the lookup's new total was not written")

    async def test_the_came_back_marker_is_what_survives_the_migration(self):
        """The completed record was the only thing that remembered the season had
        ever been finished, and it is gone. The flag is all that is left to draw
        the marker from."""
        await self._settle(1, self.last_month)
        await self._reconcile([_play(1)], CountingLookup({"total": 10}))
        self.assertTrue((await store.find_user_record(self.user_id, _key(1), 1))["came_back"])

    async def test_a_later_counts_write_does_not_dismiss_the_marker(self):
        """Only the viewer clears it. A routine refresh writing the row back
        without the flag must not do it for them."""
        await self._settle(1, self.last_month)
        await self._reconcile([_play(1)], CountingLookup({"total": 10}))
        await lifecycle.follow(self.user_id, _show(1, watched=9, total=10))
        self.assertTrue((await store.find_user_record(self.user_id, _key(1), 1))["came_back"])

    async def test_a_lookup_that_knows_of_nothing_more_leaves_the_counts_alone(self):
        """It may go straight back to being completed on the next transition, and
        that is the honest answer when nothing further is known to exist."""
        await self._settle(1, self.last_month, watched=8, total=8)
        await self._reconcile([_play(1)], CountingLookup())
        listed = await store.find_user_record(self.user_id, _key(1), 1)
        self.assertEqual((listed["watched"], listed["total"]), (8, 8))
        self.assertTrue(lifecycle.is_finished(listed))

    async def test_a_whole_seasons_worth_of_episodes_costs_one_lookup(self):
        await self._settle(1, self.last_month)
        lookup = CountingLookup({"total": 9})
        await self._reconcile([_play(1, 1, n) for n in range(1, 10)], lookup)
        self.assertEqual(lookup.calls, [(str(_key(1)), 1)])

    async def test_two_seasons_of_one_show_are_two_questions(self):
        await self._settle(1, self.last_month)
        await store.add_month_record(
            self.user_id, self.last_month,
            {**_show(1, 2, kind=store.RecordKind.COMPLETED, watched=8), "season": 2})
        lookup = CountingLookup({"total": 9})
        await self._reconcile([_play(1, 1, 9), _play(1, 2, 9)], lookup)
        self.assertEqual(lookup.calls, [(str(_key(1)), 1), (str(_key(1)), 2)])

    async def test_a_season_already_on_the_list_is_left_where_it_is(self):
        """It is already the right place for it, so there is nothing to move and
        nothing to ask a provider."""
        await lifecycle.follow(self.user_id, _show(1, watched=3))
        lookup = CountingLookup({"total": 10})
        self.assertEqual(await self._reconcile([_play(1)], lookup), [])
        self.assertEqual(lookup.calls, [])
        self.assertFalse((await store.find_user_record(self.user_id, _key(1), 1))["came_back"])

    async def test_this_months_premiere_is_left_to_the_ordinary_transitions(self):
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(2, kind=store.RecordKind.SERIES_PREMIERE))
        lookup = CountingLookup({"total": 10})
        self.assertEqual(await self._reconcile([_play(2)], lookup), [])
        self.assertEqual(lookup.calls, [])
        self.assertIsNone(await store.find_user_record(self.user_id, _key(2), 1))

    async def test_a_season_given_up_on_is_offered_back_rather_than_taken_back(self):
        """Watching it again is a real signal but not proof of a changed mind the
        way an extra episode is proof a season grew: a completed record is a claim
        about the show that the play disproves, an abandoned one is a decision only
        the viewer can withdraw. Reopening it silently would fight the calendar
        too — the turn-away still stands — so it is asked about."""
        await store.add_month_record(self.user_id, self.last_month,
                                     _show(3, kind=store.RecordKind.ABANDONED))
        lookup = CountingLookup({"total": 10})
        questions = await self._questions([_play(3)], lookup)
        self.assertEqual([str(p.key) for p in questions.given_up], [str(_key(3))])
        self.assertEqual(questions.unknown, [],
                         "a season the tracker plainly knows about was raised as unknown")
        self.assertEqual(lookup.calls, [], "asking must cost no lookup")
        self.assertIsNone(await store.find_user_record(self.user_id, _key(3), 1))
        self.assertIn((str(_key(3)), 1), await self.month_kinds(self.last_month),
                      "the verdict was withdrawn without the viewer saying so")

    async def test_a_season_this_month_settled_is_left_entirely_alone(self):
        """The verdict was reached this month from these very counts, so there is
        nothing to withdraw — and it is plainly known, so there is nothing to ask
        the viewer about either."""
        await self._settle(4, self.this_month)
        lookup = CountingLookup({"total": 10})
        self.assertEqual(await self._reconcile([_play(4)], lookup), [])
        self.assertEqual(lookup.calls, [])
        self.assertIn((str(_key(4)), 1), await self.month_kinds(self.this_month))

    async def test_a_season_this_month_was_given_up_on_raises_no_question(self):
        """The other half of the same rule, and the half that fails loudest if the
        month under way's verdicts ever start reaching the search: an abandoned
        record found there is offered back to the viewer, so a season they gave up
        on this morning would ask to be re-added this afternoon, every load."""
        await store.add_month_record(self.user_id, self.this_month,
                                     _show(5, kind=store.RecordKind.ABANDONED))
        lookup = CountingLookup({"total": 10})
        questions = await self._questions([_play(5)], lookup)
        self.assertEqual(questions.given_up, [])
        self.assertEqual(questions.unknown, [])
        self.assertEqual(lookup.calls, [])

    async def test_the_second_pass_over_the_same_plays_writes_nothing(self):
        """Once a season has come back, a page load that sees the same plays must
        not rewrite it or ask again — the row is on the viewer's list now, which
        is the first thing the search asks."""
        await self._settle(1, self.last_month)
        first = CountingLookup({"total": 10})
        await self._reconcile([_play(1)], first)
        before = await store.find_user_record(self.user_id, _key(1), 1)

        again = CountingLookup({"total": 10})
        await self._reconcile([_play(1)], again)
        self.assertEqual(again.calls, [])
        self.assertEqual(await store.find_user_record(self.user_id, _key(1), 1), before)

    async def test_an_empty_pull_asks_nothing_and_reads_nothing(self):
        """The ordinary load: the activity beacon had not moved, so no history was
        fetched and there is nothing to reconcile."""
        lookup = CountingLookup({"total": 10})
        self.assertEqual(await self._reconcile([], lookup), [])
        self.assertEqual(lookup.calls, [])


class AnEpisodeNothingKnowsAboutTests(LifecycleTestCase):
    """When the search runs out of data the play is handed back untouched, and
    nothing is looked up for it — that is the whole point of handing it back."""

    async def _reconcile(self, plays, lookup):
        """The plays nothing could place. A season the tracker holds a verdict on
        is a different question and is asserted on in its own class."""
        return (await lifecycle.reconcile_history(
            self.user_id, plays, month=self.this_month, look_up=lookup)).unknown

    async def test_it_comes_back_carrying_the_title_and_the_episode(self):
        lookup = CountingLookup({"total": 10})
        unknown = await self._reconcile([_play(9, 2, 5, "Something Else")], lookup)
        self.assertEqual([(str(p.key), p.season, p.number, p.title) for p in unknown],
                         [(str(_key(9)), 2, 5, "Something Else")])
        self.assertEqual(lookup.calls, [],
                         "a season lookup was paid for an episode nothing knows about")

    async def test_one_season_asks_once_however_many_episodes_were_watched(self):
        unknown = await self._reconcile(
            [_play(9, 2, n) for n in range(1, 6)], CountingLookup())
        self.assertEqual([(p.season, p.number) for p in unknown], [(2, 1)],
                         "the first play of the season is the one that carries on")

    async def test_it_is_reported_beside_a_season_that_did_reopen(self):
        await store.add_month_record(
            self.user_id, self.last_month,
            _show(1, kind=store.RecordKind.COMPLETED, watched=8))
        lookup = CountingLookup({"total": 10})
        unknown = await self._reconcile([_play(1), _play(9, 1, 1)], lookup)
        self.assertEqual([str(p.key) for p in unknown], [str(_key(9))])
        self.assertEqual(lookup.calls, [(str(_key(1)), 1)])

    async def test_it_carries_the_ids_the_event_named(self):
        """Saying yes to the row means looking the season up, and a lookup needs
        the id of the service being asked. The play is the last place those ids
        exist — it does not outlive the request that reported it."""
        unknown = await self._reconcile([_play(9, 2, 5)], CountingLookup())
        self.assertEqual(unknown[0].ids, {"trakt": 9, "tmdb": 9})

    async def test_a_season_already_declined_is_not_raised_again(self):
        """The row is re-derived from viewing every time viewing is read, so a
        refusal that was not written down would come straight back."""
        await store.dismiss_prompt(self.user_id, _key(9), 2)
        unknown = await self._reconcile([_play(9, 2, 5)], CountingLookup())
        self.assertEqual(unknown, [])

    async def test_declining_one_season_does_not_silence_another(self):
        await store.dismiss_prompt(self.user_id, _key(9), 2)
        unknown = await self._reconcile(
            [_play(9, 2, 5), _play(9, 3, 1), _play(11, 1, 1)], CountingLookup())
        self.assertEqual({(str(p.key), p.season) for p in unknown},
                         {(str(_key(9)), 3), (str(_key(11)), 1)})

    async def test_a_declined_season_is_not_offered_back_either(self):
        """One refusal covers both questions the page can ask: "nothing knows
        about this" and "you gave this up" are asked for different reasons, but no
        means the same thing to both."""
        await store.add_month_record(self.user_id, self.last_month,
                                     _show(3, kind=store.RecordKind.ABANDONED))
        await store.dismiss_prompt(self.user_id, _key(3), 1)
        questions = await lifecycle.reconcile_history(
            self.user_id, [_play(3)], month=self.this_month, look_up=CountingLookup())
        self.assertEqual((questions.unknown, questions.given_up), ([], []))

    async def test_declining_says_nothing_about_a_season_later_recorded(self):
        """Declining to ADD a season is not a statement about what should happen
        if the tracker comes to hold it anyway — so the refusal is read only
        after the search has come up empty, never instead of it."""
        await store.dismiss_prompt(self.user_id, _key(1), 1)
        await store.add_month_record(
            self.user_id, self.last_month,
            _show(1, kind=store.RecordKind.COMPLETED, watched=8))
        lookup = CountingLookup({"total": 10})
        self.assertEqual(await self._reconcile([_play(1)], lookup), [])
        self.assertEqual(lookup.calls, [(str(_key(1)), 1)],
                         "a declined season that is now on record must still reopen")
        reopened = await store.find_user_record(self.user_id, _key(1), 1)
        self.assertIsNotNone(reopened, "the reopened season never reached the list")
        self.assertTrue(reopened["came_back"])


class WhatAMonthHandsItsReadersTests(LifecycleTestCase):
    """The four collections stay apart, and the two premiere kinds stay apart
    inside them."""

    async def test_the_two_premiere_kinds_are_never_one_list(self):
        shape = lifecycle.shape_of([
            _show(1, 1, kind=store.RecordKind.SERIES_PREMIERE),
            _show(2, 4, kind=store.RecordKind.SEASON_PREMIERE),
            _show(3, 1, kind=store.RecordKind.COMPLETED),
            _show(4, 1, kind=store.RecordKind.ABANDONED),
        ])
        self.assertEqual([s["match_id"] for s in shape.series_premieres], ["1"])
        self.assertEqual([s["match_id"] for s in shape.season_premieres], ["2"])
        self.assertEqual([s["match_id"] for s in shape.settled], ["3", "4"])
        self.assertEqual(shape.listed, [])
        self.assertEqual([s["match_id"] for s in shape.premieres()], ["1", "2"])

    async def test_a_month_that_is_not_under_way_has_no_viewer_list(self):
        """A month that is over settled its verdicts and one that has not begun
        has nothing in hand, so neither shows what the viewer is part-way
        through — even though the list itself is unchanged."""
        await lifecycle.follow(self.user_id, _show(1))
        await store.add_month_record(self.user_id, self.last_month,
                                     _show(2, kind=store.RecordKind.COMPLETED,
                                           watched=8))

        shape = await lifecycle.announce(self.user_id, self.last_month, [])

        self.assertEqual(shape.listed, [])
        self.assertEqual([s["match_id"] for s in shape.settled], ["2"])
        self.assertEqual(len(await store.user_records(self.user_id)), 1)
