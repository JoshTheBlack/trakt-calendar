"""A month rollover driven through the HTTP API, with the clock moved under it.

WHAT MAKES THESE DIFFERENT FROM tests/distrakt/test_rollover.py. Those call
`ensure_month(..., today=...)` and hand the date in as an argument, which proves
the rollover logic and nothing about whether a REQUEST ever arrives at it. Every
route resolves its own `today`, and until app/clock.py existed there was no way
to make one of them believe the date had changed — so the transition itself, a
real client asking for a month before and after a boundary, had never been
exercised at any level. That is what this file does: the only thing injected is
the environment variable, and everything after it is the app's ordinary path.

DATES ARE ABSOLUTE HERE AND THAT IS DELIBERATE. Elsewhere in this suite a
hardcoded year-month rots — one did, passing all July and failing on 1 August.
Under a clock override it cannot: the app is not reading the real date at all, so
these months mean the same thing whenever they are run. The rule is "never let
the real calendar decide the outcome", and pinning the fake one is one of the two
ways to obey it.

No network. The calendar read, the watch-history sync and the per-season detail
call are all patched, exactly as the unit-level rollover tests patch them.
"""
from __future__ import annotations

import contextlib
import os
from datetime import date
from unittest import mock

from app import clock, distrakt as distrakt_store
from app.config import Settings
from app.providers.base import Item, Media, Source

from ..support import ORIGIN, AppTestCase

# The two months the rollover happens across, and the day either side of the
# boundary between them. Named once so a test reads as a story about a boundary
# rather than a set of string literals.
CLOSING = "2026-07"
OPENING = "2026-08"
BEFORE_THE_FIRST = date(2026, 7, 20)
ON_THE_FIRST = date(2026, 8, 1)


def fake_today(value: date):
    """Run the block with the app believing `value` is today."""
    return mock.patch.dict(os.environ, {clock.FAKE_TODAY_ENV: value.isoformat()})


def _item(trakt_id: int, season: int, title: str, air_date: str) -> Item:
    """One calendar premiere as the shared cache hands it over.

    A real Item rather than a lookalike dict, so a field renamed out from under
    this double fails here instead of passing and then failing in production.
    """
    return Item(
        source=Source.TRAKT, media=Media.SHOW, id=f"slug-{trakt_id}",
        ids={"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
        detail_url=f"https://trakt.tv/shows/slug-{trakt_id}",
        air_date=air_date, air_ts=0.0, air_display=air_date,
        air_time="20:00", day_of_week="Saturday",
        title=title, season=season, network="Net",
    )


# Season shapes for every id these tests use. `started`/`finished` are what the
# bucketing turns on, so each id is chosen to land in one bucket and stay there.
_SEASONS = {
    # July's carry-forward candidate: airing, part-watched — stays live.
    701: {"total": 8, "started": True, "finished": False, "watched": 3},
    # Finished in July and fully watched — Completed, must not travel.
    702: {"total": 6, "started": True, "finished": True, "watched": 6},
    # Marked abandoned by hand in July — must not travel either.
    703: {"total": 6, "started": True, "finished": False, "watched": 1},
    # August's own premiere.
    801: {"total": 10, "started": True, "finished": False, "watched": 0},
}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    spec = _SEASONS[int(trakt_id)]
    return {
        "season": int(season), "total": spec["total"], "cadence": "Mon",
        "premiere": "7/6", "finale": "7/28" if spec["finished"] else None,
        "started_airing": spec["started"], "finished_airing": spec["finished"],
        "air_dates": [],
    }


async def _fake_sync_and_baseline(settings, user_id, roster, force=False, today=None):
    """Watch-history cache stand-in: report `watched` episodes for each id in the
    roster it is handed, keyed the way the real one keys them."""
    wanted = {int((rec.get("ids") or {}).get("tmdb") or 0) for rec in roster}
    shows: dict = {}
    for trakt_id, spec in _SEASONS.items():
        if trakt_id in wanted:
            shows[f"show:tmdb:{trakt_id}"] = {
                "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
                "seasons": {"1": list(range(spec["watched"]))},
            }
    return {"shows": shows, "movies": {}, "last_synced": "2026-01-01", "beacons": None}


async def _no_progress(settings, since_days=60):
    return []


class RolloverOverHttpTestCase(AppTestCase):
    """A signed-in tracker user with Trakt configured, and no live calls."""

    def make_settings(self):
        # trakt_configured is a property over these two fields, and a month is not
        # built at all without it (rollover.ensure_month). public_base_url has to
        # match the client's origin or the cross-site rules refuse the writes.
        return Settings(public_base_url=ORIGIN,
                        trakt_client_id="cid", trakt_access_token="tok")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user(
            "tracker", calendar_approved=True, distrakt_approved=True)
        self.link_identity(self.user_id, "trakt", provider_user_id=901, access_token="tok")
        self.sign_in_as(self.user_id)

    @contextlib.contextmanager
    def offline(self, premieres: dict[str, list[Item]] | None = None):
        """Every outbound read the tracker makes, replaced. `premieres` maps
        "YYYY-MM" to what the calendar holds for that month, so a test says which
        month has what rather than writing a dispatcher each time.

        Any request that builds a month has to run inside this — conftest's guard
        turns an unpatched one into a connection refusal rather than a silent
        network call, which is how the two omissions in this file were caught.
        """
        held = premieres or {}

        async def read_month(endpoint, settings, year=None, month=None, **kw):
            key = distrakt_store.month_key(int(year), int(month))
            return list(held.get(key, [])), None

        with contextlib.ExitStack() as stack:
            for target, fake in (
                ("app.calendar.cache.read_month", read_month),
                ("app.providers.trakt.sync.fetch_watched_progress", _no_progress),
                ("app.providers.trakt.detail.fetch_season_detail", _fake_season_detail),
                ("app.distrakt.watch_history.sync_and_baseline", _fake_sync_and_baseline),
            ):
                stack.enter_context(mock.patch(target, side_effect=fake))
            yield

    def get_month(self, key: str, premieres: dict[str, list[Item]] | None = None) -> dict:
        """GET /api/distrakt/month for a "YYYY-MM", with the reads patched."""
        year, month = int(key[:4]), int(key[5:7])
        with self.offline(premieres):
            resp = self.client.get(f"/api/distrakt/month?year={year}&month={month}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def import_month(self, key: str, premieres: dict[str, list[Item]] | None = None) -> dict:
        """POST /api/distrakt/import — the control that BUILDS a month that has
        not begun. Opening one no longer builds it, so this is the only way in."""
        year, month = int(key[:4]), int(key[5:7])
        with self.offline(premieres):
            resp = self.client.post("/api/distrakt/import",
                                    json={"year": year, "month": month})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def stored_ids(self, key: str) -> set[int]:
        """The (numeric id) set actually persisted for a month, read from the
        store rather than from a rendered payload — what got WRITTEN is the thing
        a rollover is about."""
        import asyncio

        doc = asyncio.run(distrakt_store.load_month(self.user_id, key))
        return {int(s["match_id"]) for s in (doc or {}).get("shows", [])}

    def seed_july(self) -> None:
        """July as a lived-in open month: one show still going, one finished, one
        the user abandoned by hand."""
        import asyncio

        from app.providers.base import ItemKey

        for trakt_id, title in ((701, "Still Going"), (702, "All Done"), (703, "Gave Up")):
            asyncio.run(distrakt_store.add_show(self.user_id, CLOSING, {
                "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
                "season": 1, "title": title, "network": "Net",
            }))
        asyncio.run(distrakt_store.set_abandoned(
            self.user_id, CLOSING, ItemKey("show", "tmdb", "703"), 1, True,
            abandoned_form="`Gave Up S01 (1/6)`"))


class MonthFreezesWhenItsDatePassesTests(RolloverOverHttpTestCase):
    """The month under way stays editable; once the calendar has passed it, it
    closes — whether or not any later month has been built or looked at.

    This is the transition the author could not watch happen: it needs two
    requests separated by a date change, which is precisely what could not be
    arranged before.
    """

    def test_july_is_open_while_july_is_the_month_under_way(self):
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            payload = self.get_month(CLOSING)
        self.assertFalse(payload.get("closed"), "July froze while it was still July")

    def test_july_freezes_when_august_is_first_opened(self):
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        # Nothing about the request changes — only what day it is.
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        with fake_today(ON_THE_FIRST):
            july = self.get_month(CLOSING)
        self.assertTrue(july["closed"], "July did not freeze once August opened")

    def test_july_freezes_on_its_own_without_august_ever_being_opened(self):
        # The reason the rule is the clock's and not the next month's: a tracker
        # nobody touches for three weeks used to keep July open and editable the
        # whole time, because the thing that closed it was a side effect of
        # opening a month the user had no reason to open.
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.assertFalse(self.get_month(CLOSING)["closed"])
        with fake_today(date(2026, 8, 21)):
            july = self.get_month(CLOSING)
        self.assertTrue(july["closed"], "July stayed open because nobody opened August")
        self.assertEqual(self.stored_ids(OPENING), set(),
                         "freezing July built the month after it")

    def test_a_month_two_months_back_freezes_the_first_time_it_is_looked_at(self):
        # The snapshot is lazy, not scheduled: this app runs no background job, so
        # a month that settled while nobody was looking materialises whenever
        # somebody first is — however long that takes.
        self.seed_july()
        with fake_today(date(2026, 9, 30)):
            july = self.get_month(CLOSING)
        self.assertTrue(july["closed"])

    def test_a_frozen_month_stays_frozen_on_a_later_day(self):
        # A freeze is a one-way door; re-reading it later must not reopen it, or
        # a closed month would start moving again every time somebody looked.
        self.seed_july()
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        with fake_today(date(2026, 9, 15)):
            july = self.get_month(CLOSING)
        self.assertTrue(july["closed"])


class AMonthHoldsOnlyItsOwnPremieresTests(RolloverOverHttpTestCase):
    """A month holds what STARTS in it, whether or not it has begun.

    What the user is in the middle of is a fact about the user, read across every
    month at once, so there is nothing for a new month to take from the one before
    it. Taking it anyway made a season that began in July get announced as new in
    August, gave a month built ahead a roster frozen at build time, and read a
    calendar turn-away made during that wait as giving up on a show that had never
    started.
    """

    def test_opening_a_month_that_has_not_begun_does_not_build_it(self):
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            payload = self.get_month(OPENING)
        self.assertEqual(payload.get("shows", []), [])
        self.assertEqual(self.stored_ids(OPENING), set(),
                         "merely looking at next month wrote a roster")

    def test_importing_it_early_takes_premieres_and_nothing_else(self):
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
            self.import_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        self.assertEqual(
            self.stored_ids(OPENING), {801},
            "a month that has not begun took titles from July")

    def test_the_same_month_takes_nothing_extra_once_it_has_begun(self):
        # The month opening changes nothing about what it HOLDS. It used to be the
        # moment July's roster was copied across, which is how a preview built the
        # month before could never gain what opening was supposed to bring it.
        self.seed_july()
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        self.assertEqual(self.stored_ids(OPENING), {801},
                         "the month under way copied July's roster in")

    def test_july_s_live_title_is_still_on_the_page_in_august(self):
        # And this is why nothing needs copying: the show the user is part-way
        # through is on August's page as THEIR list, read off the month it is
        # stored on. Its finished and given-up neighbours are not.
        self.seed_july()
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(
                OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        shown = {int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"]}
        self.assertIn(701, shown, "the user's own list lost the title July held")
        self.assertNotIn(702, shown)  # finished: July's verdict, not August's work
        self.assertNotIn(703, shown)  # given up on: off the list for good
        self.assertEqual(self.stored_ids(OPENING), {801}, "showing it wrote it in")


class CompletedAndAbandonedStayInTheMonthTheyHappenedTests(RolloverOverHttpTestCase):
    """A verdict belongs to the month it was reached in and does not travel."""

    def _roll_into_august(self) -> None:
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})

    def test_a_completed_show_does_not_arrive_in_the_new_month(self):
        self._roll_into_august()
        self.assertNotIn(702, self.stored_ids(OPENING))

    def test_an_abandoned_show_does_not_arrive_in_the_new_month(self):
        self._roll_into_august()
        self.assertNotIn(703, self.stored_ids(OPENING))

    def test_both_are_still_on_july_after_the_rollover(self):
        # "Does not travel" has to mean stayed put, not disappeared — a rollover
        # that dropped the row from both months would pass the two tests above.
        self._roll_into_august()
        self.assertEqual(self.stored_ids(CLOSING), {701, 702, 703})

    def test_july_keeps_their_verdicts_in_its_frozen_snapshot(self):
        # Read off the stored doc rather than the rendered payload: the freeze's
        # job is to WRITE each row's final bucket, and a month that renders
        # correctly today from live data while having stored nothing would look
        # identical from the outside and go wrong the moment Trakt is unreachable.
        import asyncio

        self._roll_into_august()
        doc = asyncio.run(distrakt_store.load_month(self.user_id, CLOSING))
        self.assertTrue(doc["closed"])
        buckets = {int(s["match_id"]): s.get("bucket") for s in doc["shows"]}
        self.assertEqual(buckets[702], "completed")
        self.assertEqual(buckets[703], "abandoned")

    def test_the_live_title_is_still_the_user_s_and_not_given_up_on(self):
        # July's verdict on 703 travels nowhere, and neither does its absence of
        # one on 701: the title the user is part-way through reads as work in
        # hand on August's page, not as something they walked away from.
        self._roll_into_august()
        with fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING)
        row = next(s for s in payload["shows"] if int((s.get("ids") or {}).get("tmdb")) == 701)
        self.assertFalse(row["abandoned"])
        self.assertIn(row["bucket"], ("cleanup", "keepup"))


class ASeasonThatGrewComesBackAndSaysSoTests(RolloverOverHttpTestCase):
    """A show known to have six episodes that the user watched all six of is
    finished — until the provider learns of an eighth. July's record of having
    finished it is still true and is not edited; the show being back on the pile
    is a fact about the user now, and it arrives by itself because their own list
    is derived from live counts rather than from month membership.
    """

    def _roll_into_august(self) -> None:
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})

    @contextlib.contextmanager
    def _season_grew(self, trakt_id: int, total: int):
        """The provider revising a season's episode count upward, which is the
        one number this whole case turns on."""
        was = _SEASONS[trakt_id]["total"]
        _SEASONS[trakt_id]["total"] = total
        try:
            yield
        finally:
            _SEASONS[trakt_id]["total"] = was

    def test_it_reappears_on_the_user_s_own_list(self):
        self._roll_into_august()
        with self._season_grew(702, 8), fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING)
        row = next(s for s in payload["shows"] if int((s.get("ids") or {}).get("tmdb")) == 702)
        self.assertIn(row["bucket"], ("cleanup", "keepup"))

    def test_it_is_marked_as_being_back(self):
        # Unannounced, a title the user remembers finishing reads as the page
        # having got it wrong.
        self._roll_into_august()
        with self._season_grew(702, 8), fake_today(ON_THE_FIRST):
            payload = self.get_month(OPENING)
        marked = {int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"] if s["returned"]}
        self.assertEqual(marked, {702})

    def test_july_still_says_it_was_finished_there(self):
        # The half the split already resolves: "finished in July" is a fact about
        # July and is STILL TRUE — the user did finish what was known then.
        self._roll_into_august()
        with self._season_grew(702, 8), fake_today(ON_THE_FIRST):
            self.get_month(OPENING)
            july = self.get_month(CLOSING)
        row = next(s for s in july["shows"] if int((s.get("ids") or {}).get("tmdb")) == 702)
        self.assertEqual(row["bucket"], "completed")


class ActingOnARowStoredSomewhereElseTests(RolloverOverHttpTestCase):
    """The user's own list shows titles stored on other months, so the two
    controls on such a row have to reach the month it actually lives on."""

    def _roll_into_august(self) -> None:
        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})

    def _post(self, path: str, body: dict) -> dict:
        # Patched on the ROUTE module, not on the provider one: routes.py binds
        # both names at import time, so patching where they are defined does not
        # reach the copies it calls.
        with self.offline(), \
                mock.patch("app.distrakt.routes.fetch_watched_map", return_value={}), \
                mock.patch("app.distrakt.routes.fetch_season_detail",
                           side_effect=_fake_season_detail):
            resp = self.client.post(path, json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_giving_up_on_it_is_recorded_against_the_month_being_viewed(self):
        # "I stopped following this" is a fact about the month it happened in,
        # and July has already settled.
        self._roll_into_august()
        with fake_today(ON_THE_FIRST):
            self._post("/api/distrakt/abandon", {
                "year": 2026, "month": 8, "key": "show:tmdb:701", "season": 1,
                "abandoned": True})
        self.assertIn(701, self.stored_ids(OPENING))
        import asyncio
        row, = [s for s in asyncio.run(
            distrakt_store.load_month(self.user_id, OPENING))["shows"]
            if int(s["match_id"]) == 701]
        self.assertTrue(row["abandoned"])

    def test_july_s_own_row_is_left_as_it_was(self):
        self._roll_into_august()
        with fake_today(ON_THE_FIRST):
            self._post("/api/distrakt/abandon", {
                "year": 2026, "month": 8, "key": "show:tmdb:701", "season": 1,
                "abandoned": True})
        import asyncio
        row, = [s for s in asyncio.run(
            distrakt_store.load_month(self.user_id, CLOSING))["shows"]
            if int(s["match_id"]) == 701]
        self.assertFalse(row["abandoned"])

    def test_removing_it_takes_every_copy_so_it_does_not_come_back(self):
        # Taking it off only the month being viewed leaves the copy that put it on
        # the list, and it returns on the next load with the ✕ looking broken.
        self._roll_into_august()
        with fake_today(ON_THE_FIRST):
            self._post("/api/distrakt/remove", {
                "year": 2026, "month": 8, "key": "show:tmdb:701", "season": 1})
            payload = self.get_month(OPENING)
        self.assertNotIn(701, self.stored_ids(CLOSING))
        self.assertNotIn(701, {int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"]})


class AFrozenMonthIsFilteredAsAtFreezeTests(RolloverOverHttpTestCase):
    """A turn-away made today must never reach backwards and edit what an
    already-frozen month announced. Season 1 was watched and finished in July;
    turning season 2 away in August says nothing about July."""

    def test_july_keeps_what_it_announced_after_a_mark_made_later(self):
        import asyncio

        from app.calendar import state as calendar_state

        self.seed_july()
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        with fake_today(ON_THE_FIRST):
            self.get_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
            asyncio.run(calendar_state.set_not_watching(self.user_id, "slug-701", True))
            july = self.get_month(CLOSING)

        shown = {int((s.get("ids") or {}).get("tmdb")) for s in july["shows"]}
        self.assertEqual(shown, {702, 703})  # July's own verdicts, unchanged
        self.assertEqual(self.stored_ids(CLOSING), {701, 702, 703})


class TheRealClockStillGovernsWithoutTheVariableTests(RolloverOverHttpTestCase):
    """None of the above is reachable on an ordinary instance.

    The whole file sets an environment variable; this asserts that with it unset
    the app answers about the real month, so a green run here is not evidence
    that the seam leaks into a normal deployment.
    """

    def test_the_month_api_answers_about_the_real_month_by_default(self):
        # No `fake_today` around this one, and no year/month in the query either:
        # the route falls back to its own idea of today, which must be the real
        # one. DERIVED, not written down — a literal here would agree with the
        # real calendar for one month and then start failing, which is the rot
        # this file's header is about and which the months above escape only
        # because an override is in force for them.
        real = date.today()
        with self.offline():
            resp = self.client.get("/api/distrakt/month")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["month"],
                         distrakt_store.month_key(real.year, real.month))

    def test_a_faked_date_reaches_the_route_over_http(self):
        # The other direction, and the reason the rest of the file means
        # anything: with the variable set the route answers about the faked month.
        # Ten years out so it can never coincide with the real one.
        far = date(date.today().year + 10, 3, 1)
        with fake_today(far), self.offline():
            resp = self.client.get("/api/distrakt/month")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["month"], distrakt_store.month_key(far.year, far.month))
