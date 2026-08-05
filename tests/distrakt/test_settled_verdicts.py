"""A settled verdict the services have stopped backing.

THE CASE THIS FILE IS NAMED FOR, observed on a real account: a season settled as
completed with both services reporting 10 of 10, and then set back to unwatched at
both of them. The stored verdict is not recomputed — that is what settling means,
and it is deliberate — so the row went on rendering 10/10 while every service
reported nothing. A record that asserts a number NEITHER service reports is the
exact thing the tracker's whole two-service design exists to prevent, and a month
earlier than the frozen months that rule was written for.

WHAT IS BUILT IS A QUESTION, NOT A CORRECTION. The record is left alone and the
viewer is offered the two moves the page already offers for a season they gave up
on and went back to: work it out again, or leave the verdict where it is and stop
asking. Nothing here writes anything until one of those is pressed.

THE TWO DIRECTIONS OF THE RULE ARE BOTH ASSERTED, because a question that fires on
noise is worse than no question: it gets dismissed reflexively and then protects
nobody. Only a service that the record credits with FINISHING a season and now
says otherwise raises it; a number moving without crossing the total does not.

No network. The watch state is written through the same baseline the sync uses, so
what these tests compare against is the shape a real pass leaves behind.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app import assets, db, distrakt as distrakt_store
from app.clock import today
from app.config import Settings
from app.distrakt import counts, lifecycle, routes as distrakt_routes, store
from app.distrakt import watch_history as wh
from app.providers.base import ItemKey, LibraryEntry, LibraryRead, UnlistedSeasons
from app.providers.trakt import transport
from tests.support import AppTestCase, ORIGIN, new_db_path

# One service's "last changed at" blob, the gate an incremental sync opens with.
BEACON = {"episodes": {"watched_at": "T1", "removed_at": None},
          "movies": {"watched_at": "T1", "removed_at": None}}

KEY = "show:tmdb:1"
ITEM = ItemKey("show", "tmdb", "1")
SEASON = 2
TOTAL = 10
# The services this account reads, in the registry's declared order — which is
# what a caller hands to season_counts to say which of them answered this pass.
ORDER = ("trakt", "simkl")


def _month_back(count: int) -> str:
    """The key of the month `count` months before the one in progress. Derived
    from the clock because a written-out month would be a different distance from
    today every month and would test the wrong rule most of them."""
    day = today()
    total = day.year * 12 + (day.month - 1) - count
    return distrakt_store.month_key(total // 12, total % 12 + 1)


def _episodes(*numbers) -> dict:
    return {n: "2026-08-0%d" % min(n, 9) for n in numbers}


def _verdict(watched_by_source: dict, *, watched: int = TOTAL) -> dict:
    """One completed record, written the way a settlement writes one: the single
    number every one-number reader takes, plus what each service actually said."""
    return {"media": "show", "match_source": "tmdb", "match_id": "1",
            "season": SEASON, "title": "The Agency", "network": "Paramount+",
            "ids": {"trakt": 7, "simkl": 5, "tmdb": 1, "slug": "the-agency"},
            "kind": store.RecordKind.COMPLETED,
            "watched": watched, "total": TOTAL,
            "started_airing": True, "finished_airing": True,
            "watched_by_source": dict(watched_by_source),
            "total_by_source": {"trakt": TOTAL}}


# ---------------------------------------------------------------------------
# The rule itself, with no database anywhere near it.
# ---------------------------------------------------------------------------

class WhatCountsAsWithdrawnTests(unittest.TestCase):
    """Which changes falsify a completed verdict and which are simply somebody
    getting on with their viewing."""

    def test_a_service_that_said_finished_and_now_says_not_is_named(self):
        """The observed case, reduced to the numbers: both services had the whole
        season and now neither of them has any of it."""
        self.assertEqual(
            counts.no_longer_finished({"trakt": 10, "simkl": 10},
                                      {"trakt": 0, "simkl": 0}, TOTAL),
            ["simkl", "trakt"])

    def test_one_of_two_withdrawing_names_only_that_one(self):
        """The row draws each service's number by name, so a number one named
        service no longer reports is a false claim whatever the other says."""
        self.assertEqual(
            counts.no_longer_finished({"trakt": 10, "simkl": 10},
                                      {"trakt": 10, "simkl": 4}, TOTAL),
            ["simkl"])

    def test_progress_that_never_crossed_the_total_is_not_a_withdrawal(self):
        """The deliberate non-fire. Simkl was recorded three episodes in and is
        now seven: it never said the season was finished, so it has not stopped
        saying it, and the verdict Trakt reached is untouched."""
        self.assertEqual(
            counts.no_longer_finished({"trakt": 10, "simkl": 3},
                                      {"trakt": 10, "simkl": 7}, TOTAL),
            [])

    def test_a_service_catching_up_to_the_others_verdict_is_not_a_withdrawal(self):
        """The other direction of the same non-fire: the disagreement the record
        froze has gone away, which is nobody's problem."""
        self.assertEqual(
            counts.no_longer_finished({"trakt": 10, "simkl": 3},
                                      {"trakt": 10, "simkl": 10}, TOTAL),
            [])

    def test_a_service_that_said_nothing_this_pass_withdraws_nothing(self):
        """Absence is not a zero. A service that was not read, or was read and had
        no answer about the title, has made no statement to contradict."""
        self.assertEqual(
            counts.no_longer_finished({"trakt": 10, "simkl": 10},
                                      {"trakt": 10}, TOTAL),
            [])

    def test_a_record_with_no_breakdown_cannot_be_checked(self):
        """`watched` is one number with no service attached to it, so there is
        nothing to test against a per-service reading — and guessing which service
        it came from would invent the attribution the record is missing."""
        self.assertEqual(counts.no_longer_finished({}, {"trakt": 0}, TOTAL), [])

    def test_an_unknown_total_finishes_nothing(self):
        """A total of 0 means the lookup could not say how long the season is.
        Read as "everything" it would have every service claim to have finished
        every title a provider was briefly unable to answer about."""
        self.assertEqual(counts.finished_by({"trakt": 0}, 0), set())
        self.assertEqual(counts.no_longer_finished({"trakt": 5}, {"trakt": 0}, 0), [])

    def test_a_whole_title_claim_still_counts_as_finished(self):
        """A service can report a title finished without itemizing an episode of
        it; that claim becomes a number here, where the total is in hand."""
        self.assertEqual(counts.finished_by({"simkl": counts.ALL_EPISODES}, TOTAL),
                         {"simkl"})


# ---------------------------------------------------------------------------
# What the watch state says about a season nothing is asking about any more.
# ---------------------------------------------------------------------------

class WhatTheServicesSayNowTests(unittest.IsolatedAsyncioTestCase):
    """The reader the comparison runs against. It has to tell a season set back to
    unwatched from a season nothing was ever asked about, and those two look
    identical in storage unless the TITLE is consulted."""

    async def asyncSetUp(self):
        new_db_path("settled-verdicts-state")
        await db.migrate()

    async def asyncTearDown(self):
        db.close_thread_connection()

    def _state(self) -> dict:
        return {"cursors": {}, "beacons": {}, "shows": {}, "movies": {}}

    def test_a_service_that_has_answered_but_holds_no_slot_reports_zero(self):
        """The observed shape. Both services drop their slot when the season is
        set back to unwatched, and the season then leaves the state entirely — so
        a reader that took a missing season for "nothing known" could never see
        the retraction it was asked to look for."""
        state = self._state()
        wh._set_show_baseline(state, KEY, {"trakt": 7}, {1: _episodes(1, 2)}, "trakt",
                              unlisted=UnlistedSeasons.ZERO)
        wh._set_show_baseline(state, KEY, {"simkl": 5}, {1: _episodes(1, 2)}, "simkl",
                              unlisted=UnlistedSeasons.ZERO)
        self.assertNotIn(str(SEASON), state["shows"][KEY]["seasons"])
        self.assertEqual(wh.season_counts(state, KEY, SEASON, ORDER),
                         {"trakt": 0, "simkl": 0})

    def test_a_service_that_has_never_answered_about_the_title_is_absent(self):
        """Zero and never-asked are the two answers that must never be confused:
        one of them retracts a verdict and the other says nothing whatsoever."""
        state = self._state()
        wh._set_show_baseline(state, KEY, {"trakt": 7}, {SEASON: _episodes(*range(1, 11))},
                              "trakt")
        self.assertEqual(wh.season_counts(state, KEY, SEASON, ORDER), {"trakt": 10})

    def test_a_title_the_state_has_never_held_answers_nothing(self):
        self.assertEqual(wh.season_counts(self._state(), KEY, SEASON, ORDER), {})

    def test_only_the_services_asked_for_are_reported(self):
        """The caller names which services actually answered this pass, so one
        that could not be read cannot vouch for a verdict or withdraw one."""
        state = self._state()
        wh._set_show_baseline(state, KEY, {}, {SEASON: _episodes(1)}, "trakt")
        wh._set_show_baseline(state, KEY, {}, {SEASON: _episodes(1, 2)}, "simkl")
        self.assertEqual(wh.season_counts(state, KEY, SEASON, ("trakt",)), {"trakt": 1})

    def test_a_whole_title_claim_answers_for_a_season_it_never_itemized(self):
        state = self._state()
        wh._set_show_baseline(state, KEY, {}, {}, "simkl",
                              unlisted=UnlistedSeasons.WATCHED)
        self.assertEqual(wh.season_counts(state, KEY, SEASON, ("simkl",)),
                         {"simkl": counts.ALL_EPISODES})

    def test_the_claiming_service_is_not_read_as_a_flat_zero(self):
        """The same claim from the service an unlabelled stored season resolves
        to. A season the state does not hold at all must not be read through the
        helper that reads one it does — that helper answers "the legacy service
        has seen none of this season", which here would report a retraction that
        never happened for the very service claiming the whole title."""
        state = self._state()
        wh._set_show_baseline(state, KEY, {}, {}, "trakt",
                              unlisted=UnlistedSeasons.WATCHED)
        self.assertEqual(wh.season_counts(state, KEY, SEASON, ("trakt",)),
                         {"trakt": counts.ALL_EPISODES})
        self.assertEqual(
            counts.no_longer_finished({"trakt": TOTAL},
                                      wh.season_counts(state, KEY, SEASON, ("trakt",)),
                                      TOTAL),
            [])


# ---------------------------------------------------------------------------
# The question the month raises, and what a frozen month does about it.
# ---------------------------------------------------------------------------

class TheQuestionAMonthRaisesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        new_db_path("settled-verdicts-question")
        await db.migrate()
        now = db.now()
        result = await db.execute(
            "INSERT INTO users (username, is_admin, calendar_approved, "
            "distrakt_approved, created_at, updated_at) VALUES (?, 1, 1, 1, ?, ?)",
            ("viewer", now, now))
        self.user_id = result.lastrowid

    async def asyncTearDown(self):
        db.close_thread_connection()

    async def _settled(self, month: str, record: dict) -> list[dict]:
        await store.add_month_record(self.user_id, month, record)
        return await store.month_records(self.user_id, month, store.SETTLED_KINDS)

    async def _ask(self, month: str, record: dict, now: dict):
        settled = await self._settled(month, record)
        return await lifecycle.unbacked_verdicts(
            self.user_id, month, settled, lambda _record, _season: now)

    async def test_a_double_retracted_verdict_is_raised(self):
        """The regression, as the record and the numbers really were."""
        question, = await self._ask(_month_back(0),
                                    _verdict({"trakt": 10, "simkl": 10}),
                                    {"trakt": 0, "simkl": 0})
        self.assertEqual(question.sources, ["simkl", "trakt"])
        self.assertEqual(question.now, {"trakt": 0, "simkl": 0})
        self.assertEqual(question.record["kind"], store.RecordKind.COMPLETED)

    async def test_the_record_is_not_touched_by_being_asked_about(self):
        """A settled record does not recompute itself, and noticing that nothing
        backs it is not an exception to that — it is the reason for the question."""
        month = _month_back(0)
        await self._ask(month, _verdict({"trakt": 10, "simkl": 10}),
                        {"trakt": 0, "simkl": 0})
        record, = await store.month_records(self.user_id, month, store.SETTLED_KINDS)
        self.assertEqual(record["watched_by_source"], {"trakt": 10, "simkl": 10})
        self.assertEqual(record["watched"], 10)
        self.assertEqual(record["kind"], store.RecordKind.COMPLETED)

    async def test_a_verdict_the_services_still_back_raises_nothing(self):
        self.assertEqual(
            await self._ask(_month_back(0), _verdict({"trakt": 10, "simkl": 10}),
                            {"trakt": 10, "simkl": 10}),
            [])

    async def test_progress_short_of_the_total_raises_nothing(self):
        """The deliberate non-fire, through the whole path rather than the rule
        alone: a service getting on with a season it never finished moves a number
        and changes no verdict."""
        self.assertEqual(
            await self._ask(_month_back(0), _verdict({"trakt": 10, "simkl": 3}),
                            {"trakt": 10, "simkl": 7}),
            [])

    async def test_an_abandoned_verdict_is_never_raised(self):
        """Giving up is a decision about the VIEWER rather than a claim about the
        show, so no number can falsify it. A play arriving for one is already
        asked about by the history reconciliation, in its own words."""
        record = {**_verdict({"trakt": 10, "simkl": 10}),
                  "kind": store.RecordKind.ABANDONED}
        self.assertEqual(
            await self._ask(_month_back(0), record, {"trakt": 0, "simkl": 0}), [])

    async def test_a_season_already_refused_is_not_raised_again(self):
        """The refusal the page's other two questions are refused through. All
        three mean the same thing by no — do not put this season on my list, stop
        asking — so one store answers for all of them."""
        month = _month_back(0)
        await store.dismiss_prompt(self.user_id, ITEM, SEASON)
        self.assertEqual(
            await self._ask(month, _verdict({"trakt": 10, "simkl": 10}),
                            {"trakt": 0, "simkl": 0}),
            [])


class AFrozenMonthIsNeverAskedAboutTests(AppTestCase):
    """A month that is over is history. Its numbers answer "what did that month
    decide", which today's watch state is no evidence about at all — so nothing
    raises a question over one, and the re-add route refuses to reach back into
    one even when asked directly.
    """

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        self.sign_in_as(self.user_id)

    def _settle(self, month: str, *, freeze: bool = False) -> None:
        asyncio.run(distrakt_store.add_month_record(
            self.user_id, month, _verdict({"trakt": 10, "simkl": 10})))
        if freeze:
            doc = asyncio.run(distrakt_store.load_month(self.user_id, month))
            asyncio.run(distrakt_store.save_month(self.user_id, {**doc, "closed": True}))

    def _readd(self):
        return self.client.post("/api/distrakt/verdict-readd",
                                json={"key": KEY, "season": SEASON},
                                headers={"Origin": ORIGIN})

    def test_no_question_is_raised_for_a_month_that_is_over(self):
        """Asked at the layer that decides it, with a reading that WOULD raise one
        for the month under way."""
        month = _month_back(2)
        self._settle(month, freeze=True)
        settled = asyncio.run(distrakt_store.month_records(
            self.user_id, month, distrakt_store.SETTLED_KINDS))
        # Proof the reading is the one that fires: the same numbers against the
        # month in progress raise the question, and only the month is different.
        self.assertEqual(
            len(asyncio.run(lifecycle.unbacked_verdicts(
                self.user_id, _month_back(0), settled,
                lambda _r, _s: {"trakt": 0, "simkl": 0}))),
            1)
        payload = self.client.get(
            f"/api/distrakt/month?year={month[:4]}&month={int(month[5:7])}").json()
        self.assertEqual(payload.get("unbacked_verdicts", []), [])

    def test_the_re_add_route_refuses_an_earlier_months_verdict(self):
        """find_shown_season composes a search that walks back over every month
        that ever settled anything, so without this refusal a key naming a season
        finished three years ago would take that year's record apart."""
        month = _month_back(2)
        self._settle(month)
        self.assertEqual(self._readd().status_code, 404)
        record, = asyncio.run(distrakt_store.month_records(
            self.user_id, month, distrakt_store.SETTLED_KINDS))
        self.assertEqual(record["kind"], distrakt_store.RecordKind.COMPLETED)

    def test_a_season_nothing_settled_this_month_is_a_404(self):
        self.assertEqual(self._readd().status_code, 404)


# ---------------------------------------------------------------------------
# The two answers, over HTTP.
# ---------------------------------------------------------------------------

class _CatalogueClient:
    """An httpx.AsyncClient stand-in for the season lookup a re-add makes. It
    carries no credential and needs none — a season's episode list is public
    catalogue data."""

    async def get(self, url, headers=None, timeout=None):
        return _Response([{"number": n, "title": f"Ep {n}",
                           "first_aired": "2026-08-01T20:00:00.000Z"}
                          for n in range(1, TOTAL + 1)])


class _Response:
    status_code = 200
    text = ""
    headers: dict = {}

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class AnsweringTheQuestionTests(AppTestCase):
    """What each button actually does to the record."""

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        self.sign_in_as(self.user_id)
        self.month = _month_back(0)
        asyncio.run(distrakt_store.add_month_record(
            self.user_id, self.month, _verdict({"trakt": 10, "simkl": 10})))

    def _settled(self) -> list[dict]:
        return asyncio.run(distrakt_store.month_records(
            self.user_id, self.month, distrakt_store.SETTLED_KINDS))

    def _listed(self) -> list[dict]:
        return asyncio.run(distrakt_store.user_records(self.user_id))

    def test_re_add_withdraws_the_verdict_and_puts_the_season_back(self):
        """The month no longer settled it — a record that has turned out to be
        false is worse than an absent one — and the season carries the marker that
        says it had once been finished."""
        with patch.object(transport, "shared_client", return_value=_CatalogueClient()):
            resp = self.client.post("/api/distrakt/verdict-readd",
                                    json={"key": KEY, "season": SEASON},
                                    headers={"Origin": ORIGIN})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._settled(), [])
        listed, = self._listed()
        self.assertEqual(listed["kind"], distrakt_store.RecordKind.CATCHUP)
        self.assertTrue(listed["came_back"])

    def test_re_add_takes_its_numbers_from_the_lookup_rather_than_the_verdict(self):
        """"Re-derives from what the services say now" is the whole promise: the
        withdrawn record's episode total must not be carried across as though it
        were still the answer."""
        with patch.object(transport, "shared_client", return_value=_CatalogueClient()):
            self.client.post("/api/distrakt/verdict-readd",
                             json={"key": KEY, "season": SEASON},
                             headers={"Origin": ORIGIN})
        listed, = self._listed()
        self.assertEqual(listed["total"], TOTAL)
        self.assertTrue(listed["finished_airing"])

    def test_dismiss_keeps_the_verdict_exactly_as_it_was(self):
        resp = self.client.post("/api/distrakt/unknown-dismiss",
                                json={"key": KEY, "season": SEASON},
                                headers={"Origin": ORIGIN})
        self.assertEqual(resp.status_code, 200)
        record, = self._settled()
        self.assertEqual(record["kind"], distrakt_store.RecordKind.COMPLETED)
        self.assertEqual(record["watched_by_source"], {"trakt": 10, "simkl": 10})
        self.assertEqual(self._listed(), [])

    def test_dismiss_stops_the_question_recurring(self):
        """The question is derived on every load from a comparison nothing stores,
        so with no refusal written down it would come straight back and the ✗ would
        look broken."""
        settled = self._settled()
        self.client.post("/api/distrakt/unknown-dismiss",
                         json={"key": KEY, "season": SEASON},
                         headers={"Origin": ORIGIN})
        for _reload in range(2):
            self.assertEqual(
                asyncio.run(lifecycle.unbacked_verdicts(
                    self.user_id, self.month, settled,
                    lambda _r, _s: {"trakt": 0, "simkl": 0})),
                [])


class TheMonthActuallyRaisesItTests(AppTestCase):
    """The regression end to end, over HTTP, as the failing page really was.

    Everything above tests a piece; this asks the question the viewer asks — load
    the month with a season it settled at 10/10 and both services now reporting
    none of it, and see whether the page is told. It is the only test that
    exercises the wiring: which state the comparison reads, which services it is
    allowed to believe, and that a settled record reaches it at all despite being
    kept out of the live pass on purpose.

    THE WATCH STATE IS SEEDED RATHER THAN SYNCED INTO EXISTENCE, because that is
    what the real one looks like: the title was baselined while the season was on
    the viewer's list, the season left the list when it settled, and the cached
    rows stayed behind. Nothing in an ordinary load re-baselines a title the
    roster no longer names.
    """

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        simkl_client_id="simkl-cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "trakt-token")
        self.link_identity(self.user_id, "simkl", 901, "simkl-token")
        self.sign_in_as(self.user_id)
        self.month = _month_back(0)
        asyncio.run(distrakt_store.add_month_record(
            self.user_id, self.month, _verdict({"trakt": 10, "simkl": 10})))
        # Trakt has answered about the title and has nothing for the season: its
        # slot went when the season was set back to unwatched, and a service that
        # lists only what it has watches in is saying zero about the rest.
        state = asyncio.run(wh.load_state(self.user_id))
        wh._set_show_baseline(state, KEY, {"trakt": 7, "tmdb": 1},
                              {1: _episodes(1, 2)}, "trakt",
                              unlisted=UnlistedSeasons.ZERO)
        asyncio.run(wh._save(self.user_id, state))

    def _month(self):
        # Simkl's half arrives as the whole-library read that source really makes,
        # holding the title and saying nothing about the season — which for a
        # service that itemizes what it is part-way through is a zero.
        library = LibraryRead(
            entries={KEY: LibraryEntry(ids={"simkl": 5, "tmdb": 1},
                                       seasons={1: _episodes(1, 2)},
                                       unlisted_seasons=UnlistedSeasons.ZERO)},
            events=[], complete=True)
        with patch("app.calendar.cache.read_month",
                   new=AsyncMock(return_value=([], None))), \
             patch("app.providers.trakt.sync.fetch_last_activities",
                   new=AsyncMock(return_value=BEACON)), \
             patch("app.providers.trakt.sync.fetch_history",
                   new=AsyncMock(return_value=[])), \
             patch("app.providers.trakt.sync.fetch_progress_details",
                   new=AsyncMock(return_value={})), \
             patch("app.providers.simkl.sync.fetch_last_activities",
                   new=AsyncMock(return_value=BEACON)), \
             patch("app.providers.simkl.sync.fetch_library",
                   new=AsyncMock(return_value=library)), \
             patch("app.providers.simkl.sync.fetch_progress_details",
                   new=AsyncMock(return_value={})):
            day = today()
            return self.client.get(
                f"/api/distrakt/month?year={day.year}&month={day.month}")

    def test_the_page_is_told_the_verdict_has_lost_its_backing(self):
        resp = self._month()
        self.assertEqual(resp.status_code, 200)
        question, = resp.json()["unbacked_verdicts"]
        self.assertEqual(question["key"], KEY)
        self.assertEqual(question["season"], SEASON)
        self.assertIn("Trakt", question["note"])
        self.assertIn("Simkl", question["note"])

    def test_the_row_itself_still_says_what_the_month_recorded(self):
        """The record is not corrected by being questioned. Its numbers are what
        that month settled on, and they stand until somebody says otherwise."""
        row, = self._month().json()["shows"]
        self.assertEqual(row["bucket"], distrakt_store.Bucket.COMPLETED)
        self.assertEqual(row["counts"], "10/10")


class TheSentenceTests(unittest.TestCase):
    """What the viewer is actually told. It names the service that changed its
    mind and both readings, because "this no longer adds up" leaves them to open
    the row, remember what it used to say and work out which service moved."""

    def _note(self, sources, now):
        return distrakt_routes._unbacked_note(lifecycle.UnbackedVerdict(
            _verdict({"trakt": 10, "simkl": 10}), "2026-08", sources, now))

    def test_it_names_the_service_and_both_readings(self):
        note = self._note(["simkl", "trakt"], {"trakt": 0, "simkl": 0})
        self.assertIn("Trakt", note)
        self.assertIn("Simkl", note)
        self.assertIn("10/10", note)
        self.assertIn("0/10", note)
        self.assertIn("2026-08", note)

    def test_one_service_withdrawing_reads_as_one(self):
        self.assertIn("Simkl no longer reports finishing this",
                      self._note(["simkl"], {"trakt": 10, "simkl": 4}))

    def test_two_services_withdrawing_read_as_two(self):
        self.assertIn("Trakt and Simkl no longer report finishing this",
                      self._note(["trakt", "simkl"], {"trakt": 0, "simkl": 0}))


class TheControlsACompletedRowOffersTests(unittest.TestCase):
    """Abandon comes off a completed row, and it is not tidying-up: the control
    could never have worked on one.

    ASSERTED AGAINST THE SOURCE because there is no JavaScript runtime in this
    suite and the decision is a single expression — the same way the counts cell
    is pinned. What it protects is that a completed row is EXCLUDED, phrased so it
    fails for the defect rather than for an ordinary edit.
    """

    def setUp(self):
        self.source = (assets.BASE_DIR / "static/js/tracker/rows.js").read_text(
            encoding="utf-8")

    def actions_expression(self) -> str:
        """The expression that decides which buttons a show row ends with."""
        return self.source.split("const abandonable =")[1].split(";")[0]

    def test_a_completed_row_is_kept_out_of_the_abandon_control(self):
        self.assertIn("completed", self.actions_expression())

    def test_a_read_only_month_still_offers_neither(self):
        """The rule it joins rather than replaces: a frozen month's verdicts were
        settled when it froze and are not up for re-deciding now."""
        self.assertIn("monthClosed", self.actions_expression())

    def test_the_delete_control_is_not_conditional_on_any_of_it(self):
        """✕ is the one thing every row keeps, in every bucket and every month:
        what a month RECORDS can always be corrected."""
        actions = self.source.split("const actions =")[1].split("const net")[0]
        self.assertIn("remove", actions)


class TheRouteStillRefusesADirectAbandonTests(AppTestCase):
    """The control is gone from the page and the refusal stays behind it. Nothing
    widens the search that produces it: widened, Abandon on a completed row would
    find the verdict, fail to migrate it — it is on no list — and file an abandoned
    record BESIDE it, putting one season on one page twice under two headings.
    """

    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", distrakt_approved=True,
                                      calendar_approved=True)
        self.link_identity(self.user_id, "trakt", 900, "user-token")
        self.sign_in_as(self.user_id)
        self.month = _month_back(0)
        asyncio.run(distrakt_store.add_month_record(
            self.user_id, self.month, _verdict({"trakt": 10, "simkl": 10})))

    def test_abandoning_a_completed_row_is_still_refused(self):
        resp = self.client.post(
            "/api/distrakt/abandon",
            json={"key": KEY, "season": SEASON, "abandoned": True},
            headers={"Origin": ORIGIN})
        self.assertEqual(resp.status_code, 404)

    def test_and_nothing_was_written_beside_the_verdict(self):
        self.client.post("/api/distrakt/abandon",
                         json={"key": KEY, "season": SEASON, "abandoned": True},
                         headers={"Origin": ORIGIN})
        records = asyncio.run(distrakt_store.month_records(
            self.user_id, self.month, distrakt_store.SETTLED_KINDS))
        self.assertEqual([r["kind"] for r in records],
                         [distrakt_store.RecordKind.COMPLETED])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
