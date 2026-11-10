"""A row's star: which of a month's premieres its share picture features
(app/distrakt/routes.py's `api_distrakt_feature`, the store in
app/calendar/share_highlights.py).

What is held here is the tracker's half — which rows carry a star, what a press
answers, the most a month may feature and what the page says at it. Which tiles
the picture then draws is tests/calendar/test_share_highlights.py's.
"""
from __future__ import annotations

import asyncio
import re
from html import unescape

from app import distrakt as distrakt_store
from app.calendar import share_highlights
from app.distrakt import post_memo
from app.providers.base import ItemKey, Media

from tests.distrakt.test_month_fragment import KEY, MonthFragmentTestCase
from tests.support import TrackerMonth

MOST = share_highlights.MAX_HIGHLIGHTS


def _key(n: int) -> str:
    return str(ItemKey("show", "tmdb", str(7000 + n)))


class FeatureStarsTestCase(MonthFragmentTestCase):
    """The month under way, premiering a dozen shows — more than a month may
    feature — with one show on the viewer's list that it did not premiere."""

    def setUp(self):
        super().setUp()
        self.month_key = distrakt_store.month_key(*self.current)
        for n in range(MOST + 2):
            self.premiere(n)
        self.list_one()  # KEY: on the list, premiered nowhere this month

    def premiere(self, n: int, season: int = 1) -> None:
        asyncio.run(distrakt_store.add_month_record(self.user_id, self.month_key, {
            "media": Media.SHOW, "ids": {"tmdb": 7000 + n}, "season": season,
            "title": f"Premiere {n:02d}", "network": "HBO",
            "kind": (distrakt_store.RecordKind.SERIES_PREMIERE if season == 1
                     else distrakt_store.RecordKind.SEASON_PREMIERE)}))

    def press(self, key: str, on: bool = True) -> TrackerMonth:
        year, month = self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            resp = self.client.post(f"/api/distrakt/feature?year={year}&month={month}",
                                    json={"key": key, "on": on})
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        return TrackerMonth(resp.text)

    def stored(self) -> tuple[str, ...]:
        return asyncio.run(share_highlights.for_month(self.user_id, *self.current))

    @staticmethod
    def stars(html: str) -> list[str]:
        """Every star button drawn, as its opening tag."""
        return re.findall(r'<button[^>]*class="btn-ghost small distrakt-feature[^"]*"[^>]*>',
                          html)

    @staticmethod
    def line(html: str) -> str:
        found = re.search(r'<div id="distraktFeatured"[^>]*>(.*?)</div>', html, re.S)
        return unescape(found.group(1).strip()) if found else ""


class WhichRowsCarryAStarTests(FeatureStarsTestCase):

    def test_each_premiere_draws_a_star_posting_itself(self):
        drawn = self.month(*self.current)
        stars = self.stars(drawn.html)
        self.assertEqual(len(stars), MOST + 2)
        year, month = self.current
        self.assertIn(f"/api/distrakt/feature?year={year}&month={month}",
                      [unescape(url) for url in drawn.posts_to()])
        self.assertTrue(all('aria-pressed="false"' in star for star in stars))
        self.assertIn("Star up to 10", self.line(drawn.html))

    def test_a_show_the_month_did_not_premiere_holds_room_and_no_star(self):
        drawn = self.month(*self.current)
        self.assertNotIn(f'{{"key": "{KEY}", "on"', unescape(drawn.html))
        self.assertIn("distrakt-feature is-placeholder", drawn.html)

    def test_a_month_that_premiered_nothing_draws_no_star_and_no_line(self):
        year, month = self.past
        drawn = self.month(year, month)
        self.assertEqual(self.stars(drawn.html), [])
        self.assertNotIn("distraktFeatured", drawn.html)


class PressingAStarTests(FeatureStarsTestCase):

    def setUp(self):
        super().setUp()
        # The page was drawn, as it is before anybody can press anything — which
        # is what keeps the month the answer redraws from.
        self.month(*self.current)

    def test_a_press_features_the_show_and_redraws_every_star_out_of_band(self):
        answer = self.press(_key(3))
        self.assertEqual(self.stored(), (_key(3),))
        stars = self.stars(answer.html)
        self.assertEqual(len(stars), MOST + 2)
        self.assertTrue(all("hx-swap-oob=" in star for star in stars))
        on = [star for star in stars if 'aria-pressed="true"' in star]
        self.assertEqual(len(on), 1)
        self.assertIn("features 1 of up to 10", self.line(answer.html))
        # The list is not redrawn: a feature changes no row.
        self.assertEqual(answer.titles, [])

    def test_pressed_again_it_stops_featuring_the_show(self):
        self.press(_key(3))
        answer = self.press(_key(3), on=False)
        self.assertEqual(self.stored(), ())
        self.assertIn("Star up to 10", self.line(answer.html))

    def test_every_copy_of_a_show_is_addressed_as_one(self):
        """A show the month holds two seasons of draws two rows, and one press
        has to redraw both — by the digest both stars carry."""
        self.premiere(3, season=2)
        drawn = self.month(*self.current)
        refs = re.findall(r'data-title="([0-9a-f]+)"', drawn.html)
        self.assertEqual(len(refs), MOST + 3)
        self.assertEqual(len(set(refs)), MOST + 2)
        answer = self.press(_key(3))
        twin = [ref for ref in set(refs) if refs.count(ref) == 2][0]
        self.assertIn(f"outerHTML:.distrakt-feature[data-title='{twin}']", answer.html)

    def test_at_the_most_every_other_star_is_disabled_and_the_line_says_why(self):
        for n in range(MOST):
            answer = self.press(_key(n))
        stars = self.stars(answer.html)
        refused = [star for star in stars if " disabled" in star]
        self.assertEqual(len(refused), 2)
        self.assertTrue(all('aria-pressed="false"' in star for star in refused))
        self.assertIn("10 of 10", self.line(answer.html))
        self.assertIn("unstar one", self.line(answer.html))

    def test_the_eleventh_is_refused_and_nothing_is_written(self):
        for n in range(MOST):
            self.press(_key(n))
        answer = self.press(_key(MOST))
        self.assertIn("toast err", answer.html)
        self.assertEqual(len(self.stored()), MOST)
        self.assertNotIn(_key(MOST), self.stored())

    def test_a_show_the_month_did_not_premiere_is_refused(self):
        answer = self.press(str(KEY))
        self.assertIn("toast err", answer.html)
        self.assertEqual(self.stored(), ())

    def test_with_nothing_kept_of_the_month_the_whole_month_answers(self):
        """After a restart nothing is kept of the month the page drew, so the
        month's own records say what it premiered and the answer is the month."""
        post_memo.forget_all()
        answer = self.press(_key(4))
        self.assertEqual(self.stored(), (_key(4),))
        self.assertIn("Premiere 04", answer.titles)
        self.assertEqual(len([s for s in self.stars(answer.html)
                              if 'aria-pressed="true"' in s]), 1)


class TakingAShowOffTheMonthTests(FeatureStarsTestCase):

    def test_a_removed_premiere_is_no_longer_featured(self):
        self.month(*self.current)
        self.press(_key(2))
        self.press(_key(5))
        year, month = self.current
        outer, season, calendar = self.offline()
        with outer, season, calendar:
            resp = self.client.post(f"/api/distrakt/remove?year={year}&month={month}",
                                    json={"key": _key(2), "season": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.stored(), (_key(5),))
