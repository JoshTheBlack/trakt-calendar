"""The tracker's landing view: a month chooser, and what it will not offer.

Opening a month is the expensive act — it can pull a month of premieres, sweep
recent viewing and write a roster's worth of rows — so the tracker's bare address
asks which month rather than inheriting one from wherever the user happened to
be. What is pinned here is that ARRIVING costs nothing, that naming a month is
what opens it, and that the grid offers exactly the months
rollover.month_openable allows: one the tracker may still build, or one the user
already has.

Every month below is derived from the clock, because all three rules compare a
month key against today and a written-out one would rot at a month boundary.

No network: nothing here reaches a provider, which is the point of most of it.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

from app import distrakt as distrakt_store
from app.config import Settings
from tests.support import AppTestCase, ORIGIN


def _month(offset: int) -> tuple[int, int]:
    """The {year, month} `offset` months from this one."""
    today = date.today()
    index = today.month - 1 + offset
    return today.year + index // 12, index % 12 + 1


class ChooserTestCase(AppTestCase):
    def make_settings(self):
        # Fully configured deliberately: "arriving builds nothing" is only worth
        # anything if there was something it could have built.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("tracker", calendar_approved=True,
                                      distrakt_approved=True)
        self.link_identity(self.user_id, "trakt", provider_user_id=901,
                           access_token="tok")
        self.sign_in_as(self.user_id)

    def chooser(self, year: int | None = None) -> str:
        path = "/distrakt" if year is None else f"/distrakt?year={year}"
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def offered(self, body: str, year: int, month: int) -> bool:
        """Whether the grid offers {year, month} as somewhere to go."""
        return f'href="/distrakt?month={month}&amp;year={year}"' in body

    def tracked_months(self) -> list[str]:
        return asyncio.run(distrakt_store.list_months(self.user_id))

    def track(self, offset: int) -> str:
        """Give the user a month, as a restore or an earlier visit would have."""
        year, month = _month(offset)
        month_key = distrakt_store.month_key(year, month)
        asyncio.run(distrakt_store.add_show(self.user_id, month_key, {
            "ids": {"trakt": 77, "tmdb": 77, "slug": "a-show"}, "season": 1,
            "title": "A Show", "network": "HBO", "media": "show",
        }))
        return month_key


class ArrivingChoosesNothingTests(ChooserTestCase):
    """The whole reason the chooser exists: reaching the tracker must not commit
    the user to a month."""

    def test_the_bare_address_offers_a_choice_instead_of_a_month(self):
        body = self.chooser()
        self.assertIn("Pick a Month", body)
        # The month view's loader takes the month from these; their absence is
        # what says no month is being loaded.
        self.assertNotIn("window.DISTRAKT_MONTH", body)

    def test_arriving_builds_no_month(self):
        """Nothing is rolled over, and no row is written, until a month is named."""
        with patch("app.distrakt.ensure_month", new_callable=AsyncMock) as ensure:
            self.chooser()
        ensure.assert_not_called()
        self.assertEqual(self.tracked_months(), [])

    def test_naming_a_month_opens_that_month(self):
        year, month = _month(0)
        body = self.client.get(f"/distrakt?year={year}&month={month}").text
        self.assertIn(f"window.DISTRAKT_MONTH = {month};", body)
        self.assertIn(f"window.DISTRAKT_YEAR = {year};", body)
        # The month's own view, not the chooser.
        self.assertIn("Copy blocks", body)


class WhichMonthsAreOfferedTests(ChooserTestCase):
    """The grid is drawn from the same rule the month view would apply, so it
    cannot offer a month the tracker would then decline to build."""

    def test_the_month_in_progress_and_its_preview_are_offered(self):
        for offset in (0, 1):
            year, month = _month(offset)
            with self.subTest(offset=offset):
                self.assertTrue(self.offered(self.chooser(year), year, month))

    def test_a_month_the_calendar_has_not_reached_is_shown_but_not_offered(self):
        """Drawn as plainly unavailable rather than left out or offered and then
        refused after the click — see rollover.month_reachable."""
        year, month = _month(2)
        body = self.chooser(year)
        self.assertFalse(self.offered(body, year, month))
        self.assertIn("month-btn unavailable", body)

    def test_a_past_month_is_still_offered(self):
        """The bound is on BUILDING a month, not on looking at one: a past month
        renders from what is stored, or empty and read-only if nothing is."""
        year, month = _month(-1)
        self.assertTrue(self.offered(self.chooser(year), year, month))

    def test_a_month_already_tracked_is_offered_however_far_ahead_it_is(self):
        """A restored export can hold a month out past the preview. There is
        nothing left to build in it, so refusing to open it would hide a month
        the user already owns."""
        self.track(4)
        year, month = _month(4)
        body = self.chooser(year)
        self.assertTrue(self.offered(body, year, month))
        # Its untracked neighbour, equally far ahead, is not.
        neighbour_year, neighbour_month = _month(5)
        if neighbour_year == year:
            self.assertFalse(self.offered(body, neighbour_year, neighbour_month))

    def test_opening_a_tracked_month_reads_it_without_building_anything(self):
        month_key = self.track(4)
        year, month = _month(4)
        with patch("app.distrakt.ensure_month", new_callable=AsyncMock) as ensure:
            resp = self.client.get(f"/distrakt?year={year}&month={month}")
        self.assertEqual(resp.status_code, 200)
        ensure.assert_not_called()  # the page shell alone asks for nothing
        self.assertEqual(self.tracked_months(), [month_key])


class MonthOpenableTests(AppTestCase):
    """rollover.month_openable on its own: the two ways a month may be opened."""

    def test_a_month_that_may_be_built_may_be_opened(self):
        today = date.today()
        for offset in (-1, 0, 1):
            key = distrakt_store.month_key(*_month(offset))
            with self.subTest(offset=offset):
                self.assertTrue(distrakt_store.month_openable(key, set(), today))

    def test_a_month_past_the_preview_may_not(self):
        today = date.today()
        key = distrakt_store.month_key(*_month(2))
        self.assertFalse(distrakt_store.month_openable(key, set(), today))

    def test_unless_it_is_one_the_user_already_has(self):
        today = date.today()
        key = distrakt_store.month_key(*_month(2))
        self.assertTrue(distrakt_store.month_openable(key, {key}, today))
