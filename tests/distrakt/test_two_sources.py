"""The tracker with two services answering for one account.

The rule under test is stated in one sentence: where two services agree about a
season the viewer sees one number, and where they disagree the viewer sees BOTH,
each named. Nothing is unioned, averaged or picked, because the disagreement is
the honest part — one service received a play the other never did, and every way
of collapsing that asserts something neither of them said.

THE REGRESSION THAT MATTERS MOST IS AT THE BOTTOM OF THIS FILE: an account with
one linked service behaves exactly as it did before there could be two. It is
last because everything above it is what could break it.

No network. Both providers' sync modules are patched at the module object, which
is why the app calls across a package through the module rather than binding the
name at import — a name bound at import is a second reference no double reaches.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import db, distrakt
from app.distrakt import counts, lifecycle, live, store, watch_history as wh
from app.providers.base import ItemKey, LibraryEntry, LibraryRead
from app.providers.simkl import SimklError
from tests.support import new_db_path

# What each account's request-scoped Settings looks like. A source is "linked"
# for the tracker exactly when this object carries a usable credential for it —
# see app/distrakt/routes.py's _distrakt_settings, which is what puts one
# person's own tokens on it.
TRAKT_ONLY = SimpleNamespace(trakt_configured=True, simkl_configured=False)
SIMKL_ONLY = SimpleNamespace(trakt_configured=False, simkl_configured=True)
BOTH = SimpleNamespace(trakt_configured=True, simkl_configured=True)

LABELS = {"trakt": "Trakt", "simkl": "Simkl"}
ORDER = ("trakt", "simkl")

BEACON = {"episodes": {"watched_at": "T1", "removed_at": None},
          "movies": {"watched_at": "T1", "removed_at": None}}
# The same service, later, having been told about something. Only the watched
# stamp moves: a removal is a different signal and would re-baseline everything.
MOVED = {"episodes": {"watched_at": "T2", "removed_at": None},
         "movies": {"watched_at": "T1", "removed_at": None}}


def KEY(tid) -> str:
    return f"show:tmdb:{tid}"


def _record(tid, season=1) -> dict:
    """A roster record naming the title to BOTH services, which is what lets each
    of them be asked about it with its own id."""
    return {"media": "show", "match_source": "tmdb", "match_id": str(tid),
            "season": season, "title": f"Show {tid}",
            "ids": {"trakt": tid, "simkl": tid, "tmdb": tid}}


def _episodes(*numbers) -> dict:
    return {n: "2026-07-0%d" % min(n, 9) for n in numbers}


# Which services answer with a whole library rather than one title at a time. It
# is a property of the service and not of this test file: a source that can hand
# over the lot is asked for it and matched on the shared identity, and one that
# cannot is asked per title with its own id. Both paths are exercised here
# precisely because the two live services differ in this.
LIBRARY_SOURCES = ("simkl",)


def _library_read(progress, *, complete=True, events=None) -> LibraryRead:
    """The same scripted progress, as the whole-library answer a source that can
    hand one over gives.

    KEYED BY THE SHARED IDENTITY, not by that service's own id, which is the
    entire difference: a roster record that names only Trakt still matches, and
    the service's own id comes back on the entry rather than being needed to ask.
    """
    return LibraryRead(
        entries={KEY(tid): LibraryEntry(ids={"simkl": tid, "tmdb": tid},
                                        seasons={int(season): dict(episodes)
                                                 for season, episodes in seasons.items()})
                 for tid, seasons in (progress or {}).items()},
        events=list(events or []), complete=complete)


def _patch(source: str, *, progress=None, history=None, activities=BEACON):
    """Patch one provider's sync entry points — and its library read as well
    where it has one, since that is the call the tracker actually places for such
    a source."""
    module = f"app.providers.{source}.sync"
    patches = [
        patch(f"{module}.fetch_last_activities",
              new=AsyncMock(side_effect=activities) if isinstance(activities, Exception)
              else AsyncMock(return_value=activities)),
        patch(f"{module}.fetch_history", new=AsyncMock(return_value=history or [])),
        patch(f"{module}.fetch_progress_details",
              new=AsyncMock(return_value=progress or {})),
    ]
    if source in LIBRARY_SOURCES:
        patches.append(patch(f"{module}.fetch_library",
                             new=AsyncMock(return_value=_library_read(progress))))
    return tuple(patches)


class TwoSourceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("two-sources")
        await db.migrate()
        self.user_id = await self._account("viewer")

    async def _account(self, username: str) -> int:
        """A second (or third) account, so a test can put the same title in front
        of three different sets of linked services without one pass's stored rows
        answering for another's."""
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, distrakt_approved, "
            "created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)", (username, now, now))
        return result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _baseline(self, settings, records, *, trakt=None, simkl=None,
                        simkl_activities=BEACON):
        """Sync both services with a scripted progress record each, and return the
        watch state that came out of it."""
        patches = [*_patch("trakt", progress=trakt),
                   *_patch("simkl", progress=simkl, activities=simkl_activities)]
        for p in patches:
            p.start()
        try:
            return await wh.sync_and_baseline(settings, self.user_id, records)
        finally:
            for p in patches:
                p.stop()


class WhoIsAskedTests(TwoSourceTestCase):
    async def test_one_linked_service_is_asked_and_the_other_is_not(self):
        self.assertEqual([str(s) for s in await wh.tracker_sources(TRAKT_ONLY, self.user_id)],
                         ["trakt"])

    async def test_both_are_asked_when_both_are_linked_in_the_declared_order(self):
        """Trakt first, and the order is load-bearing: the first service with an
        answer is the account's primary, whose number a frozen month and the
        announcement post carry when there is room for only one."""
        self.assertEqual([str(s) for s in await wh.tracker_sources(BOTH, self.user_id)],
                         ["trakt", "simkl"])

    async def test_two_services_cost_two_beacon_calls_and_not_two_full_syncs(self):
        """Each has its own beacon and its own cursor, so an unchanged history on
        one still returns after a single call while the other is being pulled."""
        first = _patch("trakt"), _patch("simkl")
        for group in first:
            for p in group:
                p.start()
        try:
            await wh.sync(BOTH, self.user_id)
            # Second pass: both beacons unchanged, so neither is read again —
            # whichever call that service's own shape makes it.
            await wh.sync(BOTH, self.user_id)
            trakt_history = first[0][1].get_original()[0]
            simkl_library = first[1][3].get_original()[0]
        finally:
            for group in first:
                for p in group:
                    p.stop()
        self.assertEqual(trakt_history.await_count, 1)
        self.assertEqual(simkl_library.await_count, 1)


class AgreementTests(TwoSourceTestCase):
    async def test_two_services_agreeing_render_one_number(self):
        state = await self._baseline(
            BOTH, [_record(101)],
            trakt={101: {1: _episodes(1, 2, 3)}},
            simkl={101: {1: _episodes(1, 2, 3)}})
        per_source = wh.watched_map(state)[(KEY(101), 1)]
        self.assertEqual(per_source, {"trakt": 3, "simkl": 3})
        self.assertTrue(counts.agreed(per_source))
        self.assertEqual(counts.counts_label(per_source, 8, LABELS, ORDER, ORDER), "3/8")

    async def test_two_services_disagreeing_render_both_with_their_names(self):
        """Neither is wrong. One of them received a play the other never did, and
        a single number would have to throw one of those facts away."""
        state = await self._baseline(
            BOTH, [_record(101)],
            trakt={101: {1: _episodes(1, 2, 3)}},
            simkl={101: {1: _episodes(1, 2, 3, 4)}})
        per_source = wh.watched_map(state)[(KEY(101), 1)]
        self.assertEqual(per_source, {"trakt": 3, "simkl": 4})
        self.assertFalse(counts.agreed(per_source))
        self.assertEqual(counts.counts_label(per_source, 8, LABELS, ORDER, ORDER),
                         "3/8 (Trakt) · 4/8 (Simkl)")

    async def test_a_season_only_one_service_knows_carries_that_services_name(self):
        """It arrives as a single number exactly as agreement does, and it means
        something different: a claim the other service never made."""
        state = await self._baseline(
            BOTH, [_record(101)], trakt={}, simkl={101: {1: _episodes(1, 2)}})
        per_source = wh.watched_map(state)[(KEY(101), 1)]
        self.assertEqual(per_source, {"simkl": 2})
        self.assertEqual(counts.counts_label(per_source, 8, LABELS, ORDER, ORDER),
                         "2/8 (Simkl)")

    async def test_the_one_number_a_month_keeps_is_the_primary_services(self):
        """Not the highest and not the union: those would each be a different
        number depending on which services happened to answer, and a frozen month
        has to keep meaning the same thing years later."""
        per_source = {"simkl": 7, "trakt": 6}
        self.assertEqual(counts.primary_count(per_source, ORDER), 6)


class DegradationTests(TwoSourceTestCase):
    async def test_a_service_that_could_not_be_read_leaves_the_others_numbers(self):
        """Never a fabricated agreement and never a hard failure of the whole
        tracker: the season shows what answered, and the page says the other one
        could not be read."""
        state = await self._baseline(
            BOTH, [_record(101)], trakt={101: {1: _episodes(1, 2, 3)}},
            simkl_activities=SimklError("Simkl is unreachable"))
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 3})
        self.assertEqual(wh.unreadable_sources(state), ["simkl"])

    async def test_what_the_missing_service_had_already_said_is_not_erased(self):
        """Its absence this pass says nothing about what it holds, so its slot is
        left exactly as it was rather than emptied."""
        await self._baseline(BOTH, [_record(101)],
                             trakt={101: {1: _episodes(1)}},
                             simkl={101: {1: _episodes(1, 2, 3, 4)}})
        state = await self._baseline(
            BOTH, [_record(101)], trakt={101: {1: _episodes(1)}},
            simkl_activities=SimklError("Simkl is unreachable"))
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)],
                         {"trakt": 1, "simkl": 4})

    async def test_a_stored_state_reports_nothing_unreadable(self):
        """The fact is about one sync attempt, not about the account, so it is
        never stored — otherwise a page would keep saying a service was down long
        after it came back."""
        await self._baseline(BOTH, [_record(101)],
                             simkl_activities=SimklError("down"))
        self.assertEqual(wh.unreadable_sources(await wh.load_state(self.user_id)), [])

    async def test_nothing_answering_at_all_is_still_the_trackers_failure(self):
        """With one service linked, "that service failed" and "the tracker
        failed" are the same sentence — which is what keeps an account with one
        service on the path it has always been on."""
        patches = [*_patch("trakt", activities=SimklError("trakt down")),
                   *_patch("simkl", activities=SimklError("simkl down"))]
        for p in patches:
            p.start()
        try:
            with self.assertRaises(SimklError):
                await wh.sync(BOTH, self.user_id)
        finally:
            for p in patches:
                p.stop()

    async def test_one_services_rows_survive_the_others_being_saved(self):
        """Both services' rows live in one table and a save rewrites the whole
        account's half of it, so this is the assertion that the write carries
        every source rather than only the one that just moved."""
        await self._baseline(BOTH, [_record(101)],
                             trakt={101: {1: _episodes(1, 2)}},
                             simkl={101: {1: _episodes(1, 2, 3)}})
        rows = await db.fetch_all(
            "SELECT source, watched_episodes_json FROM distrakt_show_progress "
            "WHERE user_id = ? ORDER BY source", (self.user_id,))
        self.assertEqual([row["source"] for row in rows], ["simkl", "trakt"])


class TheRowTests(TwoSourceTestCase):
    """What a live row actually carries, built where the rule lives rather than
    in the browser — a copy in JavaScript could not be tested against the one a
    month is frozen with."""

    async def _rows(self, watched_lookup, sources_read):
        async def _season(settings, trakt_id, season, fresh=False, client=None):
            return {"total": 8, "cadence": "Tue", "premiere": "7/1", "finale": None,
                    "started_airing": True, "finished_airing": False}
        with patch("app.providers.trakt.detail.fetch_season_detail", _season):
            return await live.compute_live_shows(
                self.user_id, [_record(101)], BOTH, watched_lookup=watched_lookup,
                sources_read=sources_read)

    async def test_a_disagreement_reaches_the_row_as_both_numbers(self):
        row, = await self._rows({(KEY(101), 1): {"trakt": 3, "simkl": 4}}, ORDER)
        self.assertEqual(row["counts"], "3/8 (Trakt) · 4/8 (Simkl)")
        self.assertEqual(row["watched_by_source"], {"trakt": 3, "simkl": 4})
        # The single number every existing reader asks for is the primary's.
        self.assertEqual(row["watched"], 3)

    async def test_agreement_reaches_the_row_as_one_number(self):
        row, = await self._rows({(KEY(101), 1): {"trakt": 3, "simkl": 3}}, ORDER)
        self.assertEqual(row["counts"], "3/8")

    async def test_one_linked_service_puts_no_badge_on_anything(self):
        row, = await self._rows({(KEY(101), 1): {"trakt": 3}}, ("trakt",))
        self.assertEqual(row["counts"], "3/8")
        self.assertEqual(row["watched"], 3)

    async def test_the_episode_total_is_recorded_against_the_service_asked(self):
        """A season's total comes from ONE service — it is catalogue data and
        paying a second call to learn the two count episodes slightly differently
        would spend the instance's budget on a disagreement nothing renders."""
        row, = await self._rows({(KEY(101), 1): {"trakt": 3, "simkl": 4}}, ORDER)
        self.assertEqual(row["total_by_source"], {"trakt": 8})


class FrozenMonthTests(TwoSourceTestCase):
    """A month that closed while the two disagreed keeps both numbers, and can
    still render the disagreement years later."""

    async def _listed(self, tid=101) -> dict:
        show = {**_record(tid), "kind": store.RecordKind.KEEPUP, "watched": 3,
                "total": 8, "network": "Net", "cadence": "Mon",
                "started_airing": True, "finished_airing": True,
                "premiere": "7/1", "finale": "7/29",
                "watched_by_source": {"trakt": 3, "simkl": 4},
                "total_by_source": {"trakt": 8}}
        await store.add_user_record(self.user_id, show)
        return show

    async def test_a_month_frozen_on_a_disagreement_reads_back_with_both(self):
        show = await self._listed()
        await lifecycle.finish(self.user_id, ItemKey("show", "tmdb", "101"), 1,
                               month="2026-07", by_source=lifecycle.by_source_of(show))
        record, = await store.month_records(self.user_id, "2026-07")
        self.assertEqual(record["watched_by_source"], {"trakt": 3, "simkl": 4})
        self.assertEqual(record["total_by_source"], {"trakt": 8})
        # And the single number every existing reader asks for is unchanged.
        self.assertEqual((record["watched"], record["total"]), (3, 8))

    async def test_the_frozen_breakdown_is_what_the_row_is_drawn_from(self):
        show = await self._listed()
        await lifecycle.finish(self.user_id, ItemKey("show", "tmdb", "101"), 1,
                               month="2026-07", by_source=lifecycle.by_source_of(show))
        record, = await store.month_records(self.user_id, "2026-07")
        self.assertEqual(
            counts.counts_label(record["watched_by_source"], record["total"],
                                LABELS, ORDER, ORDER),
            "3/8 (Trakt) · 4/8 (Simkl)")

    async def test_a_month_frozen_with_one_service_reads_back_as_one_number(self):
        """Identical to every month written before a second service existed, and
        so is a record that never carried a breakdown at all."""
        show = {**await self._listed(102), "watched_by_source": {"trakt": 3}}
        await store.add_user_record(self.user_id, show)
        await lifecycle.finish(self.user_id, ItemKey("show", "tmdb", "102"), 1,
                               month="2026-07", by_source=lifecycle.by_source_of(show))
        record, = await store.month_records(self.user_id, "2026-07")
        self.assertEqual(record["watched_by_source"], {"trakt": 3})
        self.assertEqual(counts.counts_label(record["watched_by_source"], 8, LABELS,
                                             ORDER, ("trakt",)), "3/8")

    async def test_a_record_written_before_the_breakdown_existed_reads_as_empty(self):
        """NULL means "nobody wrote a breakdown down", which is true — not "the
        services agreed on nothing"."""
        await store.add_month_record(self.user_id, "2026-06", {
            **_record(103), "kind": store.RecordKind.COMPLETED, "watched": 5, "total": 5})
        record, = await store.month_records(self.user_id, "2026-06")
        self.assertEqual(record["watched_by_source"], {})
        self.assertEqual(counts.counts_label(record["watched_by_source"] or record["watched"],
                                             5, LABELS, ORDER, ORDER), "5/5")


class OneServiceIsUnchangedTests(TwoSourceTestCase):
    """The regression that matters most. An account that has linked one service
    reads, renders and freezes exactly as it did before there could be two — and
    it takes no code path of its own to do it."""

    async def test_the_other_service_is_never_called(self):
        patches = [*_patch("trakt", progress={101: {1: _episodes(1, 2, 3)}}),
                   *_patch("simkl")]
        for p in patches:
            p.start()
        try:
            state = await wh.sync_and_baseline(TRAKT_ONLY, self.user_id, [_record(101)])
            simkl_beacon = patches[3].get_original()[0]
        finally:
            for p in patches:
                p.stop()
        simkl_beacon.assert_not_awaited()
        self.assertEqual(wh.watched_map(state), {(KEY(101), 1): {"trakt": 3}})
        self.assertEqual(wh.unreadable_sources(state), [])

    async def test_the_row_shows_one_number_with_no_badge(self):
        state = await self._baseline(TRAKT_ONLY, [_record(101)],
                                     trakt={101: {1: _episodes(1, 2, 3)}})
        per_source = wh.watched_map(state)[(KEY(101), 1)]
        self.assertEqual(counts.counts_label(per_source, 8, LABELS, ORDER, ("trakt",)),
                         "3/8")

    async def test_every_reader_that_wants_one_number_still_gets_one(self):
        """`watched` keeps meaning what it always meant — the bucket rule, the
        announcement post and a frozen month's column all read it."""
        state = await self._baseline(TRAKT_ONLY, [_record(101)],
                                     trakt={101: {1: _episodes(1, 2, 3)}})
        self.assertEqual(counts.primary_count(wh.watched_map(state)[(KEY(101), 1)], ORDER), 3)

    async def test_a_month_it_freezes_is_written_exactly_as_before(self):
        await store.add_user_record(self.user_id, {
            **_record(101), "kind": store.RecordKind.KEEPUP, "watched": 3, "total": 8,
            "network": "Net", "started_airing": True, "finished_airing": True})
        await lifecycle.finish(self.user_id, ItemKey("show", "tmdb", "101"), 1,
                               month="2026-07")
        record, = await store.month_records(self.user_id, "2026-07")
        self.assertEqual((record["watched"], record["total"]), (3, 8))
        self.assertEqual(record["watched_by_source"], {})


class BaseliningEachSourceTests(TwoSourceTestCase):
    """WHO HAS BEEN ASKED ABOUT A TITLE IS A FACT PER (title, service).

    A season's counts come from the BASELINE — one complete answer per service
    about what it has seen of a title — and not from the history sweep, which
    only ever reaches back to the start of the month it is run in and exists to
    place plays in a month. So a service that is never baselined for a title
    reports whatever it happens to have aired this month and nothing else, which
    reads as a nearly-empty library rather than as a service that was not asked.

    That is exactly what a title-only "already baselined" test produced: every
    title the first service had filed in an earlier session was skipped for the
    second one for ever, and the batched progress call that would have answered
    was never placed.
    """

    def _sources(self, *, trakt_progress=None, simkl_progress=None,
                 simkl_progress_error=None, simkl_beacon=BEACON):
        """Both services patched, with a handle on the call each of them is
        actually asked through, so a test can assert it was placed — or that it
        was NOT, which is the half that catches a roster being re-fetched on every
        page load.

        THE TWO HANDLES ARE DIFFERENT CALLS, deliberately. Trakt answers per title
        and is asked with its own id; Simkl hands over its whole library and is
        matched on the shared identity. That asymmetry is the thing under test in
        the first case below, not an accident of the fixture.
        """
        trakt_details = AsyncMock(return_value=trakt_progress or {})
        simkl_library = AsyncMock(side_effect=simkl_progress_error) \
            if simkl_progress_error is not None \
            else AsyncMock(return_value=_library_read(simkl_progress))
        patches = [
            patch("app.providers.trakt.sync.fetch_last_activities",
                  new=AsyncMock(return_value=BEACON)),
            patch("app.providers.trakt.sync.fetch_history", new=AsyncMock(return_value=[])),
            patch("app.providers.trakt.sync.fetch_progress_details", new=trakt_details),
            patch("app.providers.simkl.sync.fetch_last_activities",
                  new=AsyncMock(return_value=simkl_beacon)),
            patch("app.providers.simkl.sync.fetch_history", new=AsyncMock(return_value=[])),
            patch("app.providers.simkl.sync.fetch_progress_details",
                  new=AsyncMock(return_value={})),
            patch("app.providers.simkl.sync.fetch_library", new=simkl_library),
        ]
        return patches, trakt_details, simkl_library

    async def _pass(self, settings, records, **scripted):
        patches, trakt_details, simkl_library = self._sources(**scripted)
        for p in patches:
            p.start()
        try:
            state = await wh.sync_and_baseline(settings, self.user_id, records)
        finally:
            for p in patches:
                p.stop()
        return state, trakt_details, simkl_library

    async def test_a_title_one_service_baselined_is_baselined_by_the_next_one_linked(self):
        """The failure this class is named for. The first session files the title
        under the only service linked; the second session links another, and the
        title must be asked about again — of the NEW service, which has never
        answered about it."""
        await self._pass(TRAKT_ONLY, [_record(101)],
                         trakt_progress={101: {1: _episodes(*range(1, 20))}})
        state, _, simkl_library = await self._pass(
            BOTH, [_record(101)], trakt_progress={101: {1: _episodes(*range(1, 20))}},
            simkl_progress={101: {1: _episodes(*range(1, 20))}})
        simkl_library.assert_awaited_once()
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)],
                         {"trakt": 19, "simkl": 19})

    async def test_the_second_baseline_does_not_erase_the_first_services_slots(self):
        """A baseline is one service's complete answer about ITSELF and no
        statement at all about the other, so filling in the newcomer must leave
        what the incumbent reported standing — including where they disagree,
        which is the only honest thing to render."""
        await self._pass(TRAKT_ONLY, [_record(101)],
                         trakt_progress={101: {1: _episodes(1, 2, 3)}})
        state, _, _ = await self._pass(
            BOTH, [_record(101)], trakt_progress={101: {1: _episodes(1, 2, 3)}},
            simkl_progress={101: {1: _episodes(1)}})
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 3, "simkl": 1})
        self.assertEqual(counts.counts_label(wh.watched_map(state)[(KEY(101), 1)],
                                             8, LABELS, ORDER, ORDER),
                         "3/8 (Trakt) · 1/8 (Simkl)")

    async def test_a_service_that_knows_nothing_of_a_title_is_not_asked_again(self):
        """"Asked, and it had nothing" has to stay distinguishable from "never
        asked" PER SERVICE, or every page load re-fetches the whole roster from
        every service for ever. A service with nothing to say still leaves a mark
        saying it answered."""
        _, _, first = await self._pass(
            BOTH, [_record(101)], trakt_progress={101: {1: _episodes(1, 2)}},
            simkl_progress={})
        first.assert_awaited_once()
        state, _, second = await self._pass(
            BOTH, [_record(101)], trakt_progress={101: {1: _episodes(1, 2)}},
            simkl_progress={})
        second.assert_not_awaited()
        # And the title still reads as Trakt's alone, which is what it is.
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 2})

    async def test_a_service_that_could_not_be_read_is_asked_again_next_time(self):
        """An outage is not an answer. The slot is left as it was, the other
        service's numbers still render, and the title stays un-baselined for the
        silent one so the next load tries it again."""
        state, _, _ = await self._pass(
            BOTH, [_record(101)], trakt_progress={101: {1: _episodes(1, 2, 3)}},
            simkl_progress_error=SimklError("Simkl is unreachable"))
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 3})
        self.assertEqual(wh.unreadable_sources(state), ["simkl"])
        state, _, retried = await self._pass(
            BOTH, [_record(101)], trakt_progress={101: {1: _episodes(1, 2, 3)}},
            simkl_progress={101: {1: _episodes(1, 2, 3, 4)}})
        retried.assert_awaited_once()
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 3, "simkl": 4})


class AskingWithoutAnIdTests(TwoSourceTestCase):
    """A SERVICE IS ASKED ABOUT A TITLE IT WAS NEVER NAMED IN.

    Every record on a tracker roster was created from one service, so its ids map
    holds that service's id and no other's. While a baseline could only be placed
    with the asked service's own id, that meant the second service was never asked
    about anything at all: no call went out, no slot was ever filled, and every
    number on the page came from the first service's stored rows — which renders
    as the two services agreeing about everything. A silent false agreement is
    worse than a visible disagreement, because there is nothing on the page to
    notice.

    The way out is not to teach the roster the second service's id first. It is to
    ask that service for its whole library and match it on the identity every row
    is already filed under, so its id arrives as a by-product of the match rather
    than as its precondition.
    """

    def _trakt_only(self, tid=101) -> dict:
        """A roster record as one built from Trakt actually looks: Trakt's id, the
        shared id the identity is keyed on, and nothing of Simkl's."""
        return {"media": "show", "match_source": "tmdb", "match_id": str(tid),
                "season": 1, "title": f"Show {tid}",
                "ids": {"trakt": tid, "tmdb": tid}}

    async def _pass(self, settings, records, *, user_id=None, trakt=None, simkl=None,
                    beacon=BEACON, library=None):
        simkl_library = library if library is not None else AsyncMock(
            return_value=_library_read(simkl))
        patches = [
            *_patch("trakt", progress=trakt),
            *_patch("simkl", activities=beacon)[:3],
            patch("app.providers.simkl.sync.fetch_library", new=simkl_library),
        ]
        for p in patches:
            p.start()
        try:
            state = await wh.sync_and_baseline(settings, user_id or self.user_id, records)
        finally:
            for p in patches:
                p.stop()
        return state, simkl_library

    async def test_a_record_naming_only_trakt_is_still_baselined_from_simkls_library(self):
        """THE REGRESSION. It is matched through the ItemKey, which both sides
        already agree on, and no Simkl id was needed to place the call."""
        state, library = await self._pass(
            BOTH, [self._trakt_only()], trakt={101: {1: _episodes(1, 2, 3)}},
            simkl={101: {1: _episodes(1, 2, 3, 4)}})
        library.assert_awaited_once()
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 3, "simkl": 4})

    async def test_the_matched_entry_learns_simkls_id_as_a_by_product(self):
        """Which is what lets every per-title path ask about it directly from now
        on — but it was never needed to ask the first time, and that is the whole
        change."""
        await self._pass(BOTH, [self._trakt_only()],
                         trakt={101: {1: _episodes(1)}},
                         simkl={101: {1: _episodes(1, 2)}})
        row = await db.fetch_one(
            "SELECT simkl_id FROM distrakt_show_progress WHERE user_id = ? "
            "AND source = 'simkl'", (self.user_id,))
        self.assertEqual(row["simkl_id"], 101)

    async def test_the_three_linkages_do_not_all_report_the_same_number(self):
        """The symptom the failure actually presented as: Trakt-only, Simkl-only
        and both-linked rendered IDENTICAL counts, because all three were reading
        one service's stored rows. For a season the two services genuinely
        disagree about, the three have to differ."""
        simkl_only_id = await self._account("simkl-only")
        both_id = await self._account("both")
        scripted = {"trakt": {101: {1: _episodes(1, 2, 3)}},
                    "simkl": {101: {1: _episodes(1, 2, 3, 4)}}}
        trakt_state, _ = await self._pass(TRAKT_ONLY, [self._trakt_only()], **scripted)
        simkl_state, _ = await self._pass(SIMKL_ONLY, [self._trakt_only()],
                                          user_id=simkl_only_id, **scripted)
        both_state, _ = await self._pass(BOTH, [self._trakt_only()],
                                         user_id=both_id, **scripted)
        labels = [
            counts.counts_label(wh.watched_map(state)[(KEY(101), 1)], 8, LABELS,
                                ORDER, read)
            for state, read in ((trakt_state, ("trakt",)), (simkl_state, ("simkl",)),
                                (both_state, ORDER))]
        self.assertEqual(labels, ["3/8", "4/8", "3/8 (Trakt) · 4/8 (Simkl)"])
        self.assertEqual(len(set(labels)), 3)

    async def test_a_title_the_library_does_not_hold_is_marked_and_not_re_asked(self):
        """The common case rather than an edge one — most of a Trakt-built roster
        is genuinely absent from a Simkl library — so "asked, and it had nothing"
        has to be recorded or the whole roster is re-read on every page load."""
        _, first = await self._pass(BOTH, [self._trakt_only()],
                                    trakt={101: {1: _episodes(1, 2)}}, simkl={})
        first.assert_awaited_once()
        state, second = await self._pass(BOTH, [self._trakt_only()],
                                         trakt={101: {1: _episodes(1, 2)}}, simkl={})
        second.assert_not_awaited()
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 2})

    async def test_a_moved_beacon_lifts_the_mark_and_the_title_is_asked_again(self):
        """THE MARK MEANS "AS OF THIS LIBRARY STATE", NEVER "FOR EVER". A title a
        service has never heard of today is one it may hold tomorrow — an import
        run at the service fills a library in one go — and a permanent mark would
        leave the tracker reporting an empty answer from before it, with no way
        back short of clearing storage by hand.
        """
        await self._pass(BOTH, [self._trakt_only()],
                         trakt={101: {1: _episodes(1, 2)}}, simkl={})
        state, again = await self._pass(
            BOTH, [self._trakt_only()], trakt={101: {1: _episodes(1, 2)}},
            simkl={101: {1: _episodes(1, 2, 3, 4, 5)}}, beacon=MOVED)
        again.assert_awaited_once()
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 2, "simkl": 5})

    async def test_a_partial_read_leaves_a_title_it_did_not_mention_alone(self):
        """A read that skipped the lists that had not changed cannot say a title
        is absent, only that it did not come up. Acting on that silence would
        erase a perfectly good count every time somebody watched one episode."""
        await self._pass(BOTH, [self._trakt_only()], trakt={101: {1: _episodes(1)}},
                         simkl={101: {1: _episodes(1, 2, 3)}})
        state, _ = await self._pass(
            BOTH, [self._trakt_only()], trakt={101: {1: _episodes(1)}}, beacon=MOVED,
            library=AsyncMock(return_value=LibraryRead(entries={}, events=[],
                                                       complete=False)))
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 1, "simkl": 3})

    async def test_a_library_that_cannot_be_read_leaves_the_slot_as_it_was(self):
        """And the other service's counts still render. An outage is not an
        answer, so the title stays un-baselined for the silent service and the
        next load tries again."""
        await self._pass(BOTH, [self._trakt_only()], trakt={101: {1: _episodes(1)}},
                         simkl={101: {1: _episodes(1, 2, 3)}})
        state, _ = await self._pass(
            BOTH, [self._trakt_only()], trakt={101: {1: _episodes(1)}}, beacon=MOVED,
            library=AsyncMock(side_effect=SimklError("Simkl is unreachable")))
        self.assertEqual(wh.watched_map(state)[(KEY(101), 1)], {"trakt": 1, "simkl": 3})
        self.assertEqual(wh.unreadable_sources(state), ["simkl"])


class SourceNamesTests(unittest.TestCase):
    """What a badge says comes off the providers themselves, so it can never
    spell a service differently from the rest of the app."""

    def test_the_labels_are_the_registrys_own(self):
        self.assertEqual(live.source_labels(), LABELS)

    def test_the_order_is_the_declared_one(self):
        self.assertEqual(live.source_order(), ORDER)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
