"""The tracker's landing view: a month chooser, and what it will not offer.

Opening a month is the expensive act — it can pull a month of premieres, sweep
recent viewing and write a roster's worth of rows — so the tracker's bare address
asks which month rather than inheriting one from wherever the user happened to
be. What is pinned here is that ARRIVING costs nothing, that naming a month is
what opens it, and that the grid offers every month there is — opening one
gathers nothing on its own, so there is none it can offer that the month view
would then refuse.

Every month below is derived from the clock, because these rules compare a month
key against today and a written-out one would rot at a month boundary.

No network: nothing here reaches a provider, which is the point of most of it.
"""
from __future__ import annotations

import asyncio
import re
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

    def test_a_month_well_out_ahead_is_offered_too(self):
        """Arriving on one costs nothing — it renders empty with the control that
        builds it — so there is no distance at which the grid should stop."""
        for offset in (2, 5):
            year, month = _month(offset)
            with self.subTest(offset=offset):
                self.assertTrue(self.offered(self.chooser(year), year, month))

    def test_a_past_month_is_still_offered(self):
        """Looking at one costs nothing either: a past month renders from what is
        stored, or empty and read-only if nothing is."""
        year, month = _month(-1)
        self.assertTrue(self.offered(self.chooser(year), year, month))

    def test_every_tile_in_the_year_is_somewhere_to_go(self):
        """No tile is drawn as unavailable any more, in any year — there is no
        month the grid can offer that the month view would then decline."""
        year, _ = _month(4)
        body = self.chooser(year)
        self.assertNotIn("unavailable", body)
        for month in range(1, 13):
            with self.subTest(month=month):
                self.assertTrue(self.offered(body, year, month))

    def test_a_month_already_tracked_says_so(self):
        """A restored export can hold a month out ahead; the grid marks the ones
        the user already owns rather than merely letting them in."""
        self.track(4)
        year, month = _month(4)
        self.assertIn("· kept", self.chooser(year))

    def test_opening_a_tracked_month_reads_it_without_building_anything(self):
        month_key = self.track(4)
        year, month = _month(4)
        with patch("app.distrakt.ensure_month", new_callable=AsyncMock) as ensure:
            resp = self.client.get(f"/distrakt?year={year}&month={month}")
        self.assertEqual(resp.status_code, 200)
        ensure.assert_not_called()  # the page shell alone asks for nothing
        self.assertEqual(self.tracked_months(), [month_key])


class EveryWayInLandsOnTheChooserTests(ChooserTestCase):
    """The other half of the chooser, and the half that was missing: a correct
    route is only reachable through the links that point at it.

    The chooser is what /distrakt serves when the query names no month, so any
    link that supplies one walks straight past it. The calendar's menu item did
    exactly that — it pasted the month the calendar was showing onto the address
    — so the chooser shipped working and unreachable from the page most people
    open the tracker from.

    These follow the links rather than reading them: the assertion is where the
    href LANDS, which stays true however the address is later spelled.
    """

    def _href(self, body: str, pattern: str) -> str:
        match = re.search(pattern, body)
        self.assertIsNotNone(match, f"no link matching {pattern} on the page")
        return match.group(1).replace("&amp;", "&")

    def _lands_on_the_chooser(self, href: str) -> None:
        self.assertNotIn("month=", href)
        body = self.client.get(href).text
        self.assertIn("Pick a Month", body)
        # The month view's loader takes its month from this; its absence is what
        # says nothing was opened.
        self.assertNotIn("window.DISTRAKT_MONTH", body)

    def test_the_calendars_menu_item_asks_which_month(self):
        """The reported bug: opening the tracker from a calendar showing some
        other month went straight into that month, chooser and all."""
        year, month = _month(1)
        body = self.client.get(f"/calendar?year={year}&month={month}").text
        href = self._href(body, r'<a[^>]*\bid="distraktNav"[^>]*\bhref="([^"]*)"')
        self.assertIn(f"year={year}", href)  # the year travels; the month does not
        self._lands_on_the_chooser(href)

    def test_the_trackers_own_menu_item_asks_which_month(self):
        """From inside a month, a link back to that same month is a reload
        dressed as a destination."""
        year, month = _month(0)
        body = self.client.get(f"/distrakt?year={year}&month={month}").text
        href = self._href(body, r'<a[^>]*\bid="distraktNav"[^>]*\bhref="([^"]*)"')
        self.assertIn(f"year={year}", href)
        self._lands_on_the_chooser(href)

    def test_the_trackers_brand_asks_which_month(self):
        """The wordmark in the corner is the other link the same value feeds."""
        year, month = _month(0)
        body = self.client.get(f"/distrakt?year={year}&month={month}").text
        href = self._href(body, r'<a[^>]*\bclass="distrakt-brand"[^>]*\bhref="([^"]*)"')
        self._lands_on_the_chooser(href)

    def test_the_month_label_asks_which_month(self):
        """The way out of a month that the month view offers by name."""
        year, month = _month(0)
        body = self.client.get(f"/distrakt?year={year}&month={month}").text
        href = self._href(body, r'<a[^>]*\bclass="distrakt-nav-label"[^>]*\bhref="([^"]*)"')
        self._lands_on_the_chooser(href)


