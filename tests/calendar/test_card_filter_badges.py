"""The badges a card offers to filter on.

Clicking a certification, country, genre or network on a card adds it to the
viewer's own filters. Those four are exactly the dimensions the per-viewer filter
has (app/calendar/filter.py); a card also draws a language and a weekday, and
neither is something this app filters on.

RENDERED MARKUP, NOT TEMPLATE SOURCE, for the reason the provenance tests beside
this give: the data attributes ARE the contract between the card and the script,
and a template that compiles while emitting the wrong one passes a grep.

No network, no database.
"""
from __future__ import annotations

import re
import unittest
from zoneinfo import ZoneInfo

from app.config import Settings
from app.providers.base import Record, Source, render
from app.templating import templates

UTC = ZoneInfo("UTC")
AIR_TS = 1784145600.0


def _item(**overrides):
    values = dict(
        source=Source.TRAKT, media="show", id="t-id", ids={"tmdb": 900},
        detail_url="https://trakt.test/show", title="One Title", air_ts=AIR_TS,
        year=2026, network="tvN", country="us", language="en", runtime=45,
        status="returning series", rating=8.1,
        genres=["game-show", "drama"], certification="TV-14",
        overview="One overview.", poster="https://trakt.test/p.jpg",
        season=1, episode_number=1, episode_label="S01E01", episode_title="Pilot",
    )
    values.update(overrides)
    return render(Record(**values), UTC)


def _html(can_filter=True, **overrides) -> str:
    return templates.env.get_template("_card.html").render(
        item=_item(**overrides), not_watching=set(), new_ids=set(),
        is_admin=False, settings=Settings(), can_filter=can_filter)


def _tokens(html: str, dimension: str) -> list[str]:
    """Every data-token the card offers for one dimension, in document order."""
    return [
        m.group(1) for m in re.finditer(
            r'data-filter="%s" data-token="([^"]*)"' % dimension, html)
    ]


class WhichBadgesOfferToFilterTests(unittest.TestCase):
    def setUp(self):
        self.html = _html()

    def test_the_four_dimensions_the_filter_actually_has_are_offered(self):
        for dimension in ("certification", "country", "genre", "network"):
            with self.subTest(dimension=dimension):
                self.assertTrue(_tokens(self.html, dimension),
                                f"no badge offers to filter on {dimension}")

    def test_a_language_chip_offers_nothing(self):
        """Drawn on the card, and not a dimension this app filters on. Offering
        it would write a preference that could never do anything."""
        self.assertIn("🗣️", self.html)
        self.assertEqual(_tokens(self.html, "language"), [])

    def test_a_weekday_chip_offers_nothing(self):
        self.assertIn("📆", self.html)
        self.assertEqual(_tokens(self.html, "day"), [])


class TheTokenIsWhatTheFilterMatchesTests(unittest.TestCase):
    """The label and the token are not the same string, and the card has to send
    the one the server matches on."""

    def test_a_genre_offers_its_SLUG_and_shows_its_label(self):
        """`render` turns "game-show" into "Game Show" for display, and a filter
        spec is written in slugs. app/calendar/filter.py's own header warns that
        matching the display form breaks every MULTI-WORD genre while leaving
        single-word ones working — which is why the multi-word one is the case
        asserted here."""
        html = _html()
        self.assertIn(">Game Show<", html)
        self.assertIn("game-show", _tokens(html, "genre"))

    def test_a_genre_token_is_never_the_displayed_label(self):
        for token in _tokens(_html(), "genre"):
            with self.subTest(token=token):
                self.assertNotIn(" ", token)
                self.assertEqual(token, token.lower())

    def test_a_network_keeps_its_case_exactly(self):
        """parse_network_spec refuses to fold case because a single week held
        both `TVN` (Polish) and `tvN` (Korean). A token folded here would filter
        the wrong broadcaster."""
        self.assertEqual(_tokens(_html(network="tvN"), "network"), ["tvN"])
        self.assertEqual(_tokens(_html(network="TVN"), "network"), ["TVN"])

    def test_the_certification_token_is_the_rating_itself(self):
        self.assertEqual(_tokens(_html(certification="TV-MA"), "certification"),
                         ["TV-MA"])


class APageWithNoFiltersOfItsOwnTests(unittest.TestCase):
    """The share page draws these same cards for a visitor who has no filters to
    add to — and loads none of the script that would answer a click."""

    def setUp(self):
        self.html = _html(can_filter=False)

    def test_no_badge_offers_to_filter(self):
        self.assertNotIn("filterable", self.html)
        for dimension in ("certification", "country", "genre", "network"):
            with self.subTest(dimension=dimension):
                self.assertEqual(_tokens(self.html, dimension), [])

    def test_nothing_looks_pressable(self):
        """A chip that draws a button role and a pointer, and then does nothing,
        is worse than one that never offered."""
        self.assertNotIn('role="button"', self.html)
        self.assertNotIn('tabindex="0"', self.html)

    def test_the_card_still_draws_all_of_it(self):
        """Only the OFFER is withheld. The badges themselves are what the card is
        for and are unchanged."""
        self.assertIn("TV-14", self.html)
        self.assertIn("Game Show", self.html)
        self.assertIn("tvN", self.html)
        self.assertIn("🌍", self.html)

    def test_the_certification_keeps_the_attribute_its_styling_reads(self):
        """`data-cert` colours the chip and has nothing to do with filtering, so
        it survives on a page that offers no filtering."""
        self.assertIn('data-cert="TV-14"', self.html)


class ACardMissingAFieldOffersNothingForItTests(unittest.TestCase):
    def test_no_network_means_no_network_badge_to_press(self):
        self.assertEqual(_tokens(_html(network=""), "network"), [])

    def test_no_certification_means_no_certification_badge(self):
        self.assertEqual(_tokens(_html(certification=""), "certification"), [])

    def test_no_genres_means_no_genre_badges(self):
        self.assertEqual(_tokens(_html(genres=[]), "genre"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
