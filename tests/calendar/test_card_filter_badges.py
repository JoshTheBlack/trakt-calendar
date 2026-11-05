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

from app.calendar import vocab
from app.config import Settings
from app.endpoints import get_endpoint
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


def _html(can_filter=True, prefs=None, **overrides) -> str:
    """One card's markup. `prefs` is what this viewer has already filtered on, so
    the badges can be asked whether they draw the STORED answer -- the card and
    the answer to a press are drawn by the same macro from the same lookup."""
    return templates.env.get_template("_card.html").render(
        item=_item(**overrides), not_watching=set(), new_ids=set(),
        is_admin=False, settings=Settings(), can_filter=can_filter,
        # The month the card is on, which its watch toggle posts — see
        # _card.html; nothing about the badges reads it.
        month=1, year=2026, endpoint=get_endpoint("shows/new"),
        badge_modes=vocab.BadgeModes(prefs or {}))


def _offer(dimension: str, token: str, media: str) -> str:
    """A badge's three buttons, as /calendar/fragments/badge draws them."""
    return templates.env.get_template("_filter_offer.html").render(
        dimension=dimension, token=token, media=media, badge_modes=vocab.BadgeModes({}))


def _tokens(html: str, dimension: str) -> list[str]:
    """Every data-token the card offers for one dimension, in document order."""
    return [
        m.group(1) for m in re.finditer(
            r'data-filter="%s" data-token="([^"]*)"' % dimension, html)
    ]


class WhichBadgesOfferToFilterTests(unittest.TestCase):
    def setUp(self):
        self.html = _html()

    def test_the_five_dimensions_the_filter_actually_has_are_offered(self):
        for dimension in ("certification", "country", "language", "genre", "network"):
            with self.subTest(dimension=dimension):
                self.assertTrue(_tokens(self.html, dimension),
                                f"no badge offers to filter on {dimension}")

    def test_a_language_chip_offers_its_code(self):
        """Language is a dimension the filter has, so the card's chip offers it
        like the country beside it, with the code the filter matches."""
        self.assertIn("🗣️", self.html)
        self.assertEqual(_tokens(self.html, "language"), ["en"])

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



class ABadgeDrawsWhatIsStoredTests(unittest.TestCase):
    """THE CARD AND THE ANSWER TO A PRESS ARE ONE MACRO. Both draw the offer from
    `vocab.BadgeModes`, so a badge cannot say "filtered out" until the next reload
    and then stop saying it while the filter goes on applying -- which is what it
    did while the browser lit the radio and the server drew every badge blank."""

    def _checked(self, html: str, dimension: str, token: str) -> str | None:
        """What one badge's offer says is stored, off its own attribute."""
        offer = re.search(
            r'data-filter="%s" data-token="%s".*?</details>' % (dimension, re.escape(token)),
            html, re.S)
        self.assertIsNotNone(offer, f"no {dimension} badge for {token!r}")
        mode = re.search(r'<i class="fmark"[^>]*data-mode="([^"]*)"', offer.group(0))
        return mode.group(1) if mode else None

    def test_nothing_stored_draws_the_undo_answer(self):
        html = _html()
        self.assertEqual(self._checked(html, "genre", "drama"), "")

    def test_an_excluded_genre_draws_itself_excluded(self):
        html = _html(prefs={"tv_genres": "-drama"})
        self.assertEqual(self._checked(html, "genre", "drama"), "exclude")
        # And only that one: the other genre on the same card is untouched.
        self.assertEqual(self._checked(html, "genre", "game-show"), "")

    def test_a_film_cards_badge_reads_the_film_column(self):
        """A genre excluded from a show must not draw a film card's badge, or the
        card would claim a filter that will never act on it."""
        stored = {"tv_genres": "-drama", "movie_genres": ""}
        self.assertEqual(
            self._checked(_html(prefs=stored, media="movie"), "genre", "drama"), "")
        self.assertEqual(
            self._checked(_html(prefs=stored), "genre", "drama"), "exclude")

    def test_a_network_is_matched_with_its_case_kept(self):
        """tvN and TVN are a Korean broadcaster and a Polish one, and the badge
        folds exactly the way parse_network_spec does."""
        self.assertEqual(
            self._checked(_html(prefs={"network_filter": ["-TVN"]}), "network", "tvN"), "")
        self.assertEqual(
            self._checked(_html(prefs={"network_filter": ["-tvN"]}), "network", "tvN"),
            "exclude")

    def test_a_genre_stored_in_another_case_still_lights_its_badge(self):
        """parse_spec lowercases, so a spec holding "Drama" is the same answer as
        one holding "drama" and the badge has to agree with the filter."""
        self.assertEqual(self._checked(_html(prefs={"tv_genres": "Drama"}), "genre", "drama"),
                         "include")

    def test_every_badge_posts_the_dimension_the_token_and_the_media(self):
        """One press sends one fact, and the MERGE is the server's -- nothing in
        the browser knows what a spec is."""
        html = _offer("genre", "game-show", "show")
        self.assertIn('hx-post="/calendar/filters/badge"', html)
        self.assertIn('"dimension": "genre"', html)
        self.assertIn('"media": "show"', html)
        self.assertIn('"token": "game-show"', html)

    def test_the_buttons_arrive_when_the_badge_is_first_opened(self):
        """The card carries the badge and its stored mark; the three buttons are
        asked for by the <summary> the first time it is pressed and land right
        after it — never with the card (_filter_badge.html has the measurement)."""
        html = _html()
        self.assertNotIn("filter-offer", html)
        self.assertNotIn('hx-post="/calendar/filters/badge"', html)
        summary = re.search(r'data-filter="genre" data-token="game-show"[^>]*>\s*(<summary[^>]*>)',
                            html, re.S).group(1)
        self.assertIn('hx-get="/calendar/fragments/badge?d=genre&amp;t=game-show&amp;m=show"',
                      summary)
        self.assertIn('hx-trigger="click once"', summary)
        self.assertIn('hx-swap="afterend"', summary)

    def test_one_offer_open_at_a_time_is_the_browsers_own_grouping(self):
        """Every badge shares one `name`, which is HTML's exclusive-accordion
        rule and does what a capture listener for `toggle` used to."""
        self.assertEqual(_html().count('name="filter-badge"'),
                         _html().count("<details class=\"filter-badge"))

    def test_the_offer_holds_no_grouped_control_of_its_own(self):
        """A radio group could not hold this state and the reason is written in
        _filter_badge.html: every badge for one genre shared a name, and a name
        holds ONE checked radio in the whole document. Buttons belong to no
        group, so twelve cards can all say what is stored."""
        html = _offer("genre", "drama", "show")
        self.assertNotIn('type="radio"', html)
        self.assertIn('<button type="button" class="filter-offer-btn out"', html)

    def test_the_mark_is_inside_the_summary_a_closed_details_still_renders(self):
        """Firefox will not re-resolve an ancestor's `:has()` from an element
        inside a CLOSED <details>, so what a press replaces has to be somewhere
        the engine is still rendering. _filter_badge.html has the measurement."""
        html = _html(prefs={"tv_genres": "-drama"})
        summary = re.search(
            r'data-filter="genre" data-token="drama".*?<summary[^>]*>(.*?)</summary>',
            html, re.S)
        self.assertIsNotNone(summary)
        self.assertIn('<i class="fmark"', summary.group(1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
