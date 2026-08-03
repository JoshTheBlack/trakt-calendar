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
from datetime import date, timedelta
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

# A SECOND SET OF MONTHS, DERIVED FROM THE REAL CLOCK RATHER THAN PINNED, for the
# cases that turn on a calendar turn-away. Those cases have one input that cannot
# be faked: a turn-away is stamped with the real wall clock (calendar.state
# .set_not_watching writes db.now(), and the clock override moves dates only), and
# the whole question they ask is whether that stamp falls before or after the 1st
# of the month being viewed. Pinned months would answer that differently depending
# on when the suite was run, which is the rot this file's header is about — so
# these are anchored to the real calendar instead, and it is the FAKE clock that
# is moved to meet them. The month before the real one is always comfortably
# behind a mark made now, and the real month is then a month that has not begun.
_REAL_FIRST = date.today().replace(day=1)
UNDER_WAY_DAY = (_REAL_FIRST - timedelta(days=1)).replace(day=15)
UNDER_WAY = distrakt_store.month_key(UNDER_WAY_DAY.year, UNDER_WAY_DAY.month)
NOT_BEGUN = distrakt_store.month_key(_REAL_FIRST.year, _REAL_FIRST.month)
_EARLIER_DAY = UNDER_WAY_DAY.replace(day=1) - timedelta(days=1)
EARLIER = distrakt_store.month_key(_EARLIER_DAY.year, _EARLIER_DAY.month)


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
    # Imported into a month that has NOT BEGUN: its premiere is still weeks off,
    # so nothing has aired and nobody has watched anything.
    911: {"total": 10, "started": False, "finished": False, "watched": 0,
          "premiere": f"{_REAL_FIRST.month}/8"},
    # Stored on the month BEFORE the one under way, part-watched and still airing:
    # the user's own work in hand, which is what reaches a later month's page.
    912: {"total": 8, "started": True, "finished": False, "watched": 3,
          "premiere": f"{_EARLIER_DAY.month}/6"},
    # Two rows on the month UNDER WAY, alike in every way the bucketing can see
    # and different only in where they came from — which is the whole of what
    # decides whether giving up on one reaches the calendar.
    921: {"total": 8, "started": True, "finished": False, "watched": 3,
          "premiere": f"{UNDER_WAY_DAY.month}/6"},
    922: {"total": 8, "started": True, "finished": False, "watched": 3,
          "premiere": f"{UNDER_WAY_DAY.month}/6"},
    # A calendar row on the month that CLOSES, for the case where the month is
    # what refuses rather than the row.
    923: {"total": 6, "started": True, "finished": False, "watched": 2},
}


async def _fake_season_detail(settings, trakt_id, season, fresh=False, client=None):
    spec = _SEASONS[int(trakt_id)]
    return {
        "season": int(season), "total": spec["total"], "cadence": "Mon",
        # "M/D" with no year, as the real season lookup gives it. Most ids here
        # premiered in the closing month and take the default; an id whose
        # premiere MONTH is what a test is about names its own.
        "premiere": spec.get("premiere", "7/6"),
        "finale": "7/28" if spec["finished"] else None,
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

    def post(self, path: str, body: dict) -> dict:
        """POST one of the row controls, with every read it makes patched.

        Patched on the ROUTE module, not on the provider one: routes.py binds
        both names at import time, so patching where they are defined does not
        reach the copies it calls.
        """
        with self.offline(), \
                mock.patch("app.distrakt.routes.fetch_watched_map", return_value={}), \
                mock.patch("app.distrakt.routes.fetch_season_detail",
                           side_effect=_fake_season_detail):
            resp = self.client.post(path, json=body)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def row_on(self, month_key: str, trakt_id: int) -> dict:
        """One month's STORED row for an id — what a request wrote, rather than
        what the response happened to render."""
        import asyncio

        doc = asyncio.run(distrakt_store.load_month(self.user_id, month_key))
        row, = [s for s in (doc or {}).get("shows", []) if int(s["match_id"]) == trakt_id]
        return row

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


class AnyMonthAheadCanBeAskedForTests(RolloverOverHttpTestCase):
    """No bound on how far ahead the ask may point, and no order the months have
    to be built in. What the ask gathers is only what is already known about that
    month's calendar, so distance costs nothing."""

    FAR_AHEAD = "2026-11"

    def test_a_month_months_ahead_is_built_by_asking_for_it(self):
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(self.FAR_AHEAD,
                              {self.FAR_AHEAD: [_item(801, 1, "November New", "2026-11-05")]})
        self.assertEqual(self.stored_ids(self.FAR_AHEAD), {801})

    def test_the_months_it_skipped_are_not_stranded_behind_it(self):
        # The reason the bound could not simply be widened: the store grew forward
        # only, so a month built out ahead put every month between here and it
        # permanently out of reach — the month under way included.
        with fake_today(BEFORE_THE_FIRST):
            self.import_month(self.FAR_AHEAD,
                              {self.FAR_AHEAD: [_item(801, 1, "November New", "2026-11-05")]})
            self.import_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
        self.assertEqual(self.stored_ids(OPENING), {801})

    def test_a_month_the_calendar_has_passed_is_still_refused(self):
        # The one refusal left: working out a month nobody was tracking from its
        # premieres would be inventing it, and there is a sweep of what was
        # actually watched for that.
        with fake_today(ON_THE_FIRST):
            self.import_month(OPENING, {OPENING: [_item(801, 1, "August New", "2026-08-05")]})
            with self.offline():
                resp = self.client.post("/api/distrakt/import", json={"year": 2026, "month": 7})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("never tracked", resp.json()["error"])
        self.assertEqual(self.stored_ids(CLOSING), set())


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

    def test_giving_up_on_it_is_recorded_against_the_month_being_viewed(self):
        # "I stopped following this" is a fact about the month it happened in,
        # and July has already settled.
        self._roll_into_august()
        with fake_today(ON_THE_FIRST):
            self.post("/api/distrakt/abandon", {
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
            self.post("/api/distrakt/abandon", {
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
            self.post("/api/distrakt/remove", {
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


class ATurnAwayReachesOnlyWhatIsWorkInHandTests(RolloverOverHttpTestCase):
    """A turn-away made during the month under way gives up on the title IN that
    month — and the set of titles it can say that about is exactly the user's own
    work in hand: what has finished airing and is waiting to be caught up on, and
    what is still airing and being kept up with.

    THE PAIR OF RULES HERE PULL AGAINST EACH OTHER AND THAT IS THE POINT.
    A season stored on an EARLIER month that the user is part-way through is on
    this month's page as their own list, and turning it away is them dropping it:
    "I meant to watch that and ran out of time." A season imported into a month
    still AHEAD has not aired, nobody is behind on it, and turning it away is them
    declining a premiere they were offered — it belongs to the month it starts in
    and this month has no verdict to record about it.

    Both were reached through the same list, so both were abandoned against the
    month under way: 26 titles premiering next month were written onto this one,
    turned away, and given up on, all in the time it takes to open the page. The
    row was hidden afterwards by the filter that decides what a month may show,
    which is why nothing looked wrong — the write had already happened.
    """

    def seed(self) -> None:
        """One title on each side of the line, and neither on the month under way:
        a part-watched season on the month BEFORE it, and an unaired premiere
        imported into the month AFTER it."""
        import asyncio

        asyncio.run(distrakt_store.add_show(self.user_id, EARLIER, {
            "ids": {"trakt": 912, "tmdb": 912, "slug": "slug-912"},
            "season": 1, "title": "Behind On This", "network": "Net",
        }))
        with fake_today(UNDER_WAY_DAY):
            self.import_month(NOT_BEGUN, {NOT_BEGUN: [
                _item(911, 1, "Not Aired Yet", f"{NOT_BEGUN}-08")]})

    def turn_away(self, slug: str) -> None:
        """The calendar's own control, marked NOW — which is after the 1st of the
        month under way, and so reads as giving up rather than never starting."""
        import asyncio

        from app.calendar import state as calendar_state

        asyncio.run(calendar_state.set_not_watching(self.user_id, slug, True))

    def view_the_month_under_way(self) -> dict:
        with fake_today(UNDER_WAY_DAY):
            return self.get_month(UNDER_WAY)

    def shown_ids(self, payload: dict) -> set[int]:
        return {int((s.get("ids") or {}).get("tmdb")) for s in payload["shows"]}

    def test_the_part_watched_season_is_on_the_page_before_anything_is_turned_away(self):
        # Guards the fixture rather than the behaviour: every assertion below about
        # the title being abandoned would also pass if it had simply never reached
        # the page at all.
        self.seed()
        payload = self.view_the_month_under_way()
        self.assertIn(912, self.shown_ids(payload))
        row = next(s for s in payload["shows"] if int((s.get("ids") or {}).get("tmdb")) == 912)
        self.assertIn(row["bucket"], ("cleanup", "keepup"))

    def test_the_unaired_premiere_never_reaches_the_month_under_way(self):
        self.seed()
        payload = self.view_the_month_under_way()
        self.assertNotIn(911, self.shown_ids(payload))
        self.assertEqual(self.stored_ids(UNDER_WAY), set(),
                         "a title that has not started airing was written onto "
                         "the month under way")

    def test_turning_away_an_unaired_premiere_does_not_give_up_on_it(self):
        self.seed()
        self.turn_away("slug-911")
        payload = self.view_the_month_under_way()
        self.assertNotIn(911, self.shown_ids(payload))
        self.assertEqual(self.stored_ids(UNDER_WAY), set(),
                         "turning away next month's premiere wrote it onto this one")
        self.assertFalse(self.row_on(NOT_BEGUN, 911)["abandoned"],
                         "next month's premiere was given up on without ever airing")

    def test_turning_away_a_part_watched_season_does_give_up_on_it_here(self):
        # The case the write exists for, and it must not be lost to the fix above:
        # a season the user is in the middle of, turned away today, is one they
        # meant to keep watching and stopped — and that is a fact about the month
        # they stopped in, not about the month the row happens to be stored on.
        self.seed()
        self.turn_away("slug-912")
        self.view_the_month_under_way()
        self.assertIn(912, self.stored_ids(UNDER_WAY),
                      "giving up on a title held elsewhere left no record here")
        self.assertTrue(self.row_on(UNDER_WAY, 912)["abandoned"])

    def test_the_earlier_month_s_own_row_is_left_alone(self):
        # The month it is stored on has settled; the verdict belongs to the month
        # the user reached it in.
        self.seed()
        self.turn_away("slug-912")
        self.view_the_month_under_way()
        self.assertFalse(self.row_on(EARLIER, 912)["abandoned"])

    def test_both_marks_at_once_are_told_apart(self):
        # The two rules pinned against each other in one pass, which is the shape
        # the real data had: one turn-away recorded, the other declined, from the
        # same list on the same request.
        self.seed()
        self.turn_away("slug-911")
        self.turn_away("slug-912")
        self.view_the_month_under_way()
        self.assertEqual(self.stored_ids(UNDER_WAY), {912})
        self.assertTrue(self.row_on(UNDER_WAY, 912)["abandoned"])
        self.assertFalse(self.row_on(NOT_BEGUN, 911)["abandoned"])


class GivingUpOnTheTrackerSaysSoOnTheCalendarTests(RolloverOverHttpTestCase):
    """Abandon on the tracker turns the title away on the calendar, and taking the
    abandon back brings it straight back — the two halves of one switch.

    THE SECOND HALF IS WHAT MAKES THE FIRST SAFE, and it has to be exercised by
    reloading the month rather than by reading the response to the un-abandon. A
    mark made after a month opened is read on the next load as having given up on
    the title (see routes._apply_not_watching), so a mark left standing behind an
    un-abandon re-abandons the row the moment anybody looks at the month again:
    the button reports success, the row comes back given up on, and nothing in
    between says why. Every assertion here that matters is made after a fresh
    read of the month.

    Which rows may speak to the calendar at all is the ✕'s rule, unchanged: only
    one the calendar put on the month, and never on a month the calendar has
    passed.
    """

    CALENDAR_ROW = 921
    HAND_ADDED_ROW = 922
    CLOSED_MONTH_ROW = 923

    def seed(self) -> None:
        """Two rows on the month under way, identical to the bucketing and
        different only in where each came from."""
        self.add_row(UNDER_WAY, self.CALENDAR_ROW, distrakt_store.ADDED_BY_CALENDAR)
        self.add_row(UNDER_WAY, self.HAND_ADDED_ROW, distrakt_store.ADDED_BY_MANUAL)

    def add_row(self, month_key: str, trakt_id: int, added_by: str) -> None:
        import asyncio

        asyncio.run(distrakt_store.add_show(self.user_id, month_key, {
            "ids": {"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
            "season": 1, "title": f"Show {trakt_id}", "network": "Net",
            "added_by": added_by,
        }))

    def marks(self) -> set[str]:
        import asyncio

        from app.calendar import state as calendar_state

        return asyncio.run(calendar_state.not_watching_ids(self.user_id))

    def abandon(self, trakt_id: int, abandoned: bool,
                month_key: str = UNDER_WAY, on_day: date = UNDER_WAY_DAY) -> dict:
        year, month = int(month_key[:4]), int(month_key[5:7])
        with fake_today(on_day):
            return self.post("/api/distrakt/abandon", {
                "year": year, "month": month, "key": f"show:tmdb:{trakt_id}",
                "season": 1, "abandoned": abandoned})

    def reload_the_month(self) -> dict:
        with fake_today(UNDER_WAY_DAY):
            return self.get_month(UNDER_WAY)

    def test_giving_up_on_a_calendar_row_turns_it_away_there_too(self):
        # The row is one the calendar is still offering, so a month before its 1st
        # would hand it straight back on the next load; and either way the two
        # views would otherwise disagree about a title just decided about.
        self.seed()
        self.abandon(self.CALENDAR_ROW, True)
        self.assertIn(f"slug-{self.CALENDAR_ROW}", self.marks())

    def test_taking_it_back_clears_the_mark(self):
        self.seed()
        self.abandon(self.CALENDAR_ROW, True)
        self.abandon(self.CALENDAR_ROW, False)
        self.assertEqual(self.marks(), set())

    def test_the_row_is_still_taken_back_after_the_month_is_read_again(self):
        # THE ROUND TRIP, AND THE POINT OF ALL OF IT. Un-abandoning and then
        # opening the month is what a person does, and it is where a mark left
        # behind would undo the click they just made.
        self.seed()
        self.abandon(self.CALENDAR_ROW, True)
        self.abandon(self.CALENDAR_ROW, False)
        payload = self.reload_the_month()
        self.assertFalse(self.row_on(UNDER_WAY, self.CALENDAR_ROW)["abandoned"],
                         "the row was given up on again by being looked at")
        row = next(s for s in payload["shows"]
                   if int((s.get("ids") or {}).get("tmdb")) == self.CALENDAR_ROW)
        self.assertFalse(row["abandoned"])
        self.assertIn(row["bucket"], ("cleanup", "keepup"))

    def test_giving_up_on_a_hand_added_row_says_nothing_to_the_calendar(self):
        # A title somebody added themselves need not be on the calendar at all,
        # and hiding it there would take away something they never said they were
        # not watching.
        self.seed()
        self.abandon(self.HAND_ADDED_ROW, True)
        self.assertEqual(self.marks(), set())
        self.assertTrue(self.row_on(UNDER_WAY, self.HAND_ADDED_ROW)["abandoned"],
                        "the row itself was not given up on")

    def test_a_month_the_calendar_has_passed_says_nothing_to_it_either(self):
        # Correcting what a frozen month records is a statement about that month
        # alone. The row is a calendar row, so provenance is not what refuses
        # here — the month is.
        self.add_row(CLOSING, self.CLOSED_MONTH_ROW, distrakt_store.ADDED_BY_CALENDAR)
        with fake_today(BEFORE_THE_FIRST):
            self.get_month(CLOSING)
        with fake_today(ON_THE_FIRST):
            self.get_month(CLOSING)  # the read that freezes it
        self.abandon(self.CLOSED_MONTH_ROW, True,
                     month_key=CLOSING, on_day=ON_THE_FIRST)
        self.assertEqual(self.marks(), set())
        self.assertTrue(self.row_on(CLOSING, self.CLOSED_MONTH_ROW)["abandoned"])


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
