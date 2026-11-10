"""The shows a shared month's picture features (app/calendar/share_highlights.py)
and what the picture draws of them (app/calendar/share_routes.py's
`tile_candidates`, `card_digest`).

A HIGHLIGHT GUARANTEES A TILE AND NARROWS NOTHING: every featured title the view
shows is drawn, the ranking fills the rest, the grid is in date order, and the
count is the page's. With nothing featured the card is the card it always was.
The tracker's star that writes them is tests/distrakt/test_feature_stars.py's.
"""
from __future__ import annotations

import asyncio
import re
from html import unescape
import unittest
from unittest.mock import patch

from app import db
from app.calendar import share_card, share_card_cache, share_highlights, share_routes
from app.calendar import state as calendar_state
from app.metadata import holders
from app.providers.base import Item, Media, Source

from tests.calendar.test_share_card_route import ShareCardTestCase
from tests.support import ORIGIN

MOST = share_highlights.MAX_HIGHLIGHTS
TMDB = 9100


def _key(n: int) -> str:
    return f"show:tmdb:{TMDB + n}"


class TheStoreTests(ShareCardTestCase):
    """At most ten a month, in the order chosen, decided where it is written."""

    ELIGIBLE = [_key(n) for n in range(MOST + 3)]

    def feature(self, n: int, on: bool = True, eligible=None) -> share_highlights.Written:
        return asyncio.run(share_highlights.feature(
            self.user_id, 2026, 8, _key(n), on,
            eligible=self.ELIGIBLE if eligible is None else eligible))

    def test_features_are_kept_in_the_order_chosen(self):
        for n in (4, 1, 7):
            self.feature(n)
        self.assertEqual(asyncio.run(share_highlights.for_month(self.user_id, 2026, 8)),
                         (_key(4), _key(1), _key(7)))

    def test_featuring_twice_is_once_and_unfeaturing_takes_it_off(self):
        self.feature(2)
        self.assertEqual(self.feature(2).featured, (_key(2),))
        self.assertEqual(self.feature(2, on=False).featured, ())

    def test_the_eleventh_is_refused_and_nothing_written(self):
        for n in range(MOST):
            self.assertFalse(self.feature(n).full)
        refused = self.feature(MOST)
        self.assertTrue(refused.full)
        self.assertEqual(len(refused.featured), MOST)
        self.assertNotIn(_key(MOST), asyncio.run(
            share_highlights.for_month(self.user_id, 2026, 8)))

    def test_a_title_no_longer_eligible_goes_at_the_next_write(self):
        """And so cannot hold a place at the most a month may feature."""
        for n in range(MOST):
            self.feature(n)
        written = self.feature(MOST, eligible=self.ELIGIBLE[1:])
        self.assertFalse(written.full)
        self.assertNotIn(_key(0), written.featured)
        self.assertIn(_key(MOST), written.featured)

    def test_keep_only_drops_what_left_the_month(self):
        self.feature(1)
        self.feature(2)
        self.assertEqual(asyncio.run(share_highlights.keep_only(
            self.user_id, 2026, 8, [_key(2)])), 1)
        self.assertEqual(asyncio.run(share_highlights.for_month(self.user_id, 2026, 8)),
                         (_key(2),))

    def test_only_a_premieres_card_features_anything(self):
        self.feature(1)
        for endpoint, expected in (("shows/new", {_key(1)}), ("shows/premieres", {_key(1)}),
                                   ("shows", set()), ("shows/finales", set()),
                                   ("movies", set())):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(set(asyncio.run(share_highlights.for_card(
                    self.user_id, endpoint, 2026, 8))), expected)

    def test_a_re_key_moves_a_highlight_with_its_title(self):
        asyncio.run(share_highlights.feature(self.user_id, 2026, 8, "show:tvdb:55", True,
                                             eligible=["show:tvdb:55"]))
        asyncio.run(db.transaction(lambda conn: holders.repoint(
            conn, "show:tvdb:55", "show:tmdb:66", now=db.now())))
        self.assertEqual(asyncio.run(share_highlights.for_month(self.user_id, 2026, 8)),
                         ("show:tmdb:66",))


class TheCardTestCase(ShareCardTestCase):
    """Thirty series premieres in August, one a day, every one with artwork."""

    def setUp(self):
        super().setUp()
        self.seed([self.an_entry(f"Premiere {n:02d}", 1 + n, tmdb=TMDB + n) for n in range(30)])
        for n in range(30):
            self.write_poster(TMDB + n)
        # The finished picture is cached by what it contains, on a disk shared by
        # every test in this process.
        for path in self.cached_cards():
            path.unlink()

    def feature(self, *ns: int, on: bool = True) -> None:
        for n in ns:
            asyncio.run(share_highlights.feature(self.user_id, 2026, 8, _key(n), on,
                                                 eligible=[_key(m) for m in range(30)]))

    def drawn_card(self, query: str = "year=2026&month=8") -> share_card.Card:
        drawn: list[share_card.Card] = []
        real = share_card.build_card
        with patch.object(share_card, "build_card",
                          side_effect=lambda card: drawn.append(card) or real(card)):
            resp = self.client.get(f"/s/{self.token}/og.jpg?{query}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(drawn), 1, "the card was not rendered for this request")
        for path in self.cached_cards():
            path.unlink()
        return drawn[0]

    def render_key(self, card: share_card.Card) -> str:
        return share_card_cache.render_key(card, owner_id=self.user_id, month=8)

    @staticmethod
    def titles(card: share_card.Card) -> list[str]:
        return [tile.title for tile in card.tiles]


class TheCardFeaturesWhatItIsToldTests(TheCardTestCase):

    def test_with_nothing_featured_the_card_is_the_card_it_always_was(self):
        card = self.drawn_card()
        self.assertEqual(self.titles(card), [f"Premiere {n:02d}" for n in range(MOST)])
        self.feature(25, 26)
        self.feature(25, 26, on=False)
        self.assertEqual(self.render_key(self.drawn_card()), self.render_key(card))

    def test_each_featured_title_gets_a_tile_and_the_ranking_fills_to_ten(self):
        self.feature(27, 21, 25)
        card = self.drawn_card()
        self.assertEqual(len(card.tiles), MOST)
        self.assertEqual(self.titles(card),
                         [f"Premiere {n:02d}" for n in (*range(7), 21, 25, 27)])
        self.assertEqual(card.count, 30)

    def test_ten_featured_are_the_whole_grid_in_date_order(self):
        self.feature(*range(20, 30))
        self.assertEqual(self.titles(self.drawn_card()),
                         [f"Premiere {n:02d}" for n in range(20, 30)])

    def test_a_featured_title_with_no_artwork_gives_its_tile_to_the_ranking(self):
        (share_routes.posters.POSTER_DIR / "show" / f"{TMDB + 28}.trakt.jpg").unlink()
        self.feature(28, 29)
        card = self.drawn_card()
        self.assertEqual(len(card.tiles), MOST)
        self.assertNotIn("Premiere 28", self.titles(card))
        self.assertIn("Premiere 29", self.titles(card))

    def test_a_featured_title_the_view_hides_is_not_drawn_and_the_count_is_the_pages(self):
        """Marked not watching after it was featured (the highlight stays
        stored): the view that hides marks does not show it, so neither does its
        card."""
        self.feature(25)
        asyncio.run(calendar_state.set_not_watching(self.user_id, "premiere-25", True))
        hidden = self.drawn_card("year=2026&month=8&hidenw=1")
        self.assertNotIn("Premiere 25", self.titles(hidden))
        self.assertEqual(len(hidden.tiles), MOST)
        page = self.client.get(f"/s/{self.token}?year=2026&month=8&hidenw=1").text
        self.assertEqual(hidden.count, int(re.search(r"📊\s*(\d+)", page).group(1)))
        self.assertIn("Premiere 25", self.titles(self.drawn_card("year=2026&month=8&hidenw=0")))
        self.assertEqual(asyncio.run(share_highlights.for_month(self.user_id, 2026, 8)),
                         (_key(25),))

    def test_all_episodes_card_is_not_changed_by_a_feature(self):
        self.seed([self.an_entry(f"Premiere {n:02d}", 1 + n, tmdb=TMDB + n) for n in range(30)],
                  endpoint="shows")
        before = self.drawn_card("year=2026&month=8&endpoint=shows")
        self.feature(25, 26, 27)
        self.assertEqual(self.render_key(self.drawn_card("year=2026&month=8&endpoint=shows")),
                         self.render_key(before))


class ThePictureAddressFollowsWhatItDrawsTests(TheCardTestCase):
    """An unfurler keeps a picture by its address — Discord kept a wrong one —
    so the address names what the card draws."""

    def og_image(self) -> str:
        page = self.client.get(f"/s/{self.token}?year=2026&month=8")
        self.assertEqual(page.status_code, 200)
        return unescape(re.search(r'property="og:image" content="([^"]+)"', page.text).group(1))

    def test_the_same_line_up_is_the_same_address(self):
        self.assertEqual(self.og_image(), self.og_image())

    def test_a_feature_changes_the_address_and_taking_it_off_changes_it_back(self):
        before = self.og_image()
        self.feature(25)
        featured = self.og_image()
        self.assertNotEqual(featured, before)
        self.feature(25, on=False)
        self.assertEqual(self.og_image(), before)

    def test_the_digest_rides_after_the_compact_code(self):
        image = self.og_image()
        self.assertRegex(image, r"/og\.jpg\?p=[0-9A-F]+&v=[0-9a-f]{12}$")
        fetched = self.client.get(image[len(ORIGIN):])
        self.assertEqual(fetched.headers["content-type"], "image/jpeg")


class TheSelectionTests(unittest.TestCase):
    """`tile_candidates` on its own: a pure function of the airings and the
    featured titles."""

    @staticmethod
    def an_item(title: str, n: int, *, season: int = 1, number: int = 1):
        return Item(source=Source.TRAKT, media=Media.SHOW, id=f"show:{title}",
                    ids={"tmdb": TMDB + n}, detail_url="", air_date=f"2026-08-{1 + n % 28:02d}",
                    air_ts=float(n), air_display="", air_time="", day_of_week="",
                    title=title, season=season, episode_number=number,
                    card=f"{_key(n)}|S{season}E{number}")

    def items(self) -> list:
        # Season premieres, a series premiere late in the month and a mid-season
        # episode: three tiers, so the ranking is not plain date order.
        return [*(self.an_item(f"Returning {n:02d}", n, season=2) for n in range(12)),
                self.an_item("Brand New", 20),
                self.an_item("Midseason", 21, season=3, number=4)]

    def test_nothing_featured_is_exactly_the_ranking(self):
        items = self.items()
        for limit in (MOST, share_routes.TILE_CANDIDATES):
            self.assertEqual(share_routes.tile_candidates(items, frozenset(), limit),
                             share_routes.select_tile_items(items, limit))

    def test_featured_titles_lead_and_one_title_is_one_tile(self):
        items = [*self.items(), self.an_item("Midseason", 21, season=3, number=5)]
        chosen = share_routes.tile_candidates(items, {_key(21), _key(11)})
        self.assertEqual([item.title for item in chosen[:2]], ["Returning 11", "Midseason"])
        self.assertEqual([item.episode_number for item in chosen if item.title == "Midseason"], [4])
        self.assertEqual(len(chosen), MOST)

    def test_a_featured_title_the_airings_do_not_hold_is_not_drawn(self):
        chosen = share_routes.tile_candidates(self.items(), {"show:tmdb:1"})
        self.assertEqual(chosen, share_routes.select_tile_items(self.items()))
