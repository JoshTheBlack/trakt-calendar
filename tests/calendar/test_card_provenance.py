"""What a card shows when two services described the same airing.

The model half — which fields conflict, and which service won each — is
tests/calendar/test_precedence.py's. This file is about the CARD: that a
disagreement is visible exactly where the resolved record says there is one, that
a card only one service described carries none of it, and that the poster-only
wall degrades to the winner rather than breaking.

RENDERED MARKUP, NOT TEMPLATE SOURCE. Every assertion here goes through the real
Jinja environment: a template that compiles and emits the wrong attribute passes
a source grep, and the attributes are the entire contract between the card and
the script that flips a field.

No network, no database.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from app.calendar import cache as calendar_cache, resolve as calendar_resolve
from app.config import Settings
from app.providers.base import Record, Source, render
from app.templating import TEMPLATES_DIR, templates

UTC = ZoneInfo("UTC")
AIR_TS = 1784145600.0

# The fields the card is willing to draw two answers for, in the two shapes it
# has room for. Read out of the template rather than restated, so a field added
# there without a resolved counterpart fails here instead of drawing nothing.
CARD = (TEMPLATES_DIR / "_card.html").read_text(encoding="utf-8")


def _record(source, **overrides) -> Record:
    name = str(source)
    values = dict(
        source=source, media="show", id=f"{name}-id",
        ids={"tmdb": 900, name: f"{name}-id"},
        detail_url=f"https://{name}.test/show", title="One Title",
        air_ts=AIR_TS, year=2026, network="A Network",
        country="us", language="en", runtime=45, status="returning series",
        rating=8.1, genres=["drama"], certification="TV-14",
        overview="One overview.", poster=f"https://{name}.test/p.jpg",
        season=1, episode_number=1, episode_label="S01E01", episode_title="Pilot",
    )
    values.update(overrides)
    return Record(**values)


def _card_html(*records: Record, prefs=None) -> str:
    """One rendered card for the group `records` form, resolved and rendered the
    way the calendar's own read path does it."""
    groups = calendar_cache.group_records(list(records))
    assert len(groups) == 1, "these records do not match; the matcher would draw two cards"
    resolved = calendar_resolve.resolve(groups[0], prefs)
    item = render(resolved, UTC)
    return templates.env.get_template("_card.html").render(
        item=item, not_watching=set(), new_ids=set(), is_admin=False,
        settings=Settings())


def _swap(html: str, field: str) -> str | None:
    match = re.search(
        r'<button type="button" class="source-swap" data-field="%s".*?</button>' % field,
        html, re.S)
    return match.group(0) if match else None


class ACardOnlyOneServiceDescribedTests(unittest.TestCase):
    """Every card on an instance that has one source, and most of them on one
    that has two. None of the provenance machinery may cost it anything."""

    def setUp(self):
        self.html = _card_html(_record(Source.TRAKT))

    def test_it_carries_no_provenance_attribute_at_all(self):
        self.assertNotIn("data-alternatives", self.html)

    def test_it_draws_no_swap_logo_and_no_dual_chip(self):
        self.assertNotIn("source-swap", self.html)
        self.assertNotIn("chip dual", self.html)

    def test_it_is_not_badged_with_the_service_this_app_has_always_read(self):
        """Badging every card of the original source would put a badge on every
        card on an instance that only ever had it, which says nothing."""
        self.assertNotIn("source-badge", self.html)

    def test_its_outbound_button_is_its_own_services(self):
        self.assertIn('title="View on Trakt"', self.html)
        self.assertEqual(self.html.count("trakt-btn"), 1)

    def test_the_button_wears_the_services_name_rather_than_its_symbol(self):
        """A button is a promise about where a click goes, and the name says
        that where a symbol asks to be recognised."""
        self.assertIn("source-wordmark", self.html)
        self.assertIn("trakt-logo-dark.svg", self.html)


class ACardTheOtherServiceListedTests(unittest.TestCase):
    """The unmatched case: two services listed a title without agreeing it is the
    same title, so it draws twice. The badge is what makes that read as the two
    services disagreeing rather than as the app showing something twice."""

    def setUp(self):
        self.html = _card_html(_record(Source.SIMKL))

    def test_it_says_which_service_listed_it(self):
        self.assertIn("source-badge", self.html)
        self.assertIn('title="Listed by Simkl"', self.html)

    def test_the_outbound_button_wears_the_name_of_where_it_actually_goes(self):
        """It used to be one service's logo on every card, including the ones
        whose link led to the other service entirely."""
        self.assertIn('title="View on Simkl"', self.html)
        self.assertIn("simkl_white_with_shaddow_transparent.png", self.html)
        self.assertNotIn("trakt-logo-dark.svg", self.html)

    def test_it_offers_the_one_page_there_is(self):
        """One service listed it, so there is one place to go."""
        self.assertEqual(self.html.count("trakt-btn"), 1)


class ADisagreementIsDrawnWhereThereIsOneTests(unittest.TestCase):
    """`alternatives` names exactly the fields the two services filled in
    differently, and the card's marks are that set and no wider."""

    def setUp(self):
        self.html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title", poster="https://simkl.test/q.jpg"))

    def test_both_values_ship_with_the_card_so_a_flip_costs_no_request(self):
        """AND THE ATTRIBUTE PARSES, which is the half a source grep cannot see:
        `tojson` escapes the quote a single-quoted attribute needs and not the
        one a double-quoted attribute needs, so getting it the wrong way round
        ships an attribute that closes itself on the first key."""
        found = re.search(r"data-alternatives='([^']*)'", self.html)
        self.assertIsNotNone(found)
        carried = json.loads(found.group(1))
        self.assertEqual(set(carried), {"title", "poster"})
        self.assertEqual(set(carried["title"]), {"trakt", "simkl"})

    def test_a_value_carrying_a_quote_cannot_escape_the_attribute(self):
        """These strings are whatever a service wrote, and they land inside an
        attribute on every card of a merged group."""
        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="""A "quoted" <b>title</b>' """))
        carried = json.loads(re.search(r"data-alternatives='([^']*)'", html).group(1))
        self.assertEqual(carried["title"]["simkl"], """A "quoted" <b>title</b>' """)
        self.assertNotIn("<b>title</b>", html)

    def test_a_conflicting_field_draws_a_swap_showing_the_winners_logo(self):
        button = _swap(self.html, "title")
        self.assertIsNotNone(button)
        self.assertIn('data-source="trakt" data-label="Trakt"', button)
        # The other service's logo is here and hidden, which is what makes the
        # flip a class change rather than a fetch.
        self.assertRegex(button, r'data-source="simkl"[^>]*hidden')

    def test_the_element_showing_the_value_names_the_service_it_came_from(self):
        """The script tells the target from the button by this attribute: the
        button names the field and the target names field AND service."""
        self.assertIn('data-field="title" data-source="trakt"', self.html)
        self.assertIn('data-field="poster" data-source="trakt"', self.html)

    def test_a_field_they_agreed_on_draws_nothing(self):
        """A logo inviting a click that changes nothing is worse than no logo."""
        self.assertIsNone(_swap(self.html, "overview"))
        self.assertNotIn('data-field="overview"', self.html)

    def test_the_swap_stops_the_click_reaching_the_card_underneath(self):
        """The card opens the details modal on click; flipping one value must not
        also open it."""
        self.assertIn('onclick="swapCardField(this, event)"', self.html)

    def test_the_preference_decides_which_side_is_shown_first(self):
        """The flip is a view of the same two values whichever way round the
        account's own preference put them."""
        from app.sources import prefs as source_prefs

        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title"),
            prefs=source_prefs.SourcePrefs(user_id=1, precedence={"default": "simkl"}))
        self.assertIn('data-field="title" data-source="simkl"', html)
        self.assertIn("Another Title", html)


class TwoNumbersRatherThanAnAverageTests(unittest.TestCase):
    """A rating, a runtime, a network or a status the two services disagree
    about is shown as both, never as a mean — two user bases produce two
    legitimate numbers and their average is a number nobody reported."""

    def setUp(self):
        self.html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, rating=7.9, runtime=42))

    def test_both_numbers_are_on_the_card(self):
        chip = re.search(r'<span class="chip dual" data-field="rating".*?</span>\s*</span>',
                         self.html, re.S).group(0)
        self.assertIn("8.1", chip)
        self.assertIn("7.9", chip)
        self.assertIn("trakt-circlemark-dark.svg", chip)
        self.assertIn("simkllogo.svg", chip)

    def test_every_conflicting_scalar_it_offers_gets_its_chip(self):
        self.assertIn('data-field="rating"', self.html)
        self.assertIn('data-field="runtime"', self.html)

    def test_the_badge_on_the_poster_stays_the_winner_alone(self):
        """The poster is the whole card on the poster-only wall and has no room
        for two of anything, so the dual chips live in the body and the badges
        do not become dual by being conflicting."""
        badge = re.search(r'<div class="rating-badge">(.*?)</div>', self.html).group(1)
        self.assertIn("8.1", badge)
        self.assertNotIn("7.9", badge)

    def test_a_field_the_two_agreed_on_gets_no_chip(self):
        self.assertNotIn('class="chip dual" data-field="network"', self.html)


class AFlipIsAViewFlipAndNothingElseTests(unittest.TestCase):
    """Clicking a service mark changes one value on one card, on screen, until the
    page is left. It is not a preference, and the reason is not tidiness: whose
    value wins is stated once for every card on the Sources screen, so a click
    that wrote through would change every other card on the page as a side effect
    of looking at one of them."""

    SCRIPT = (Path(__file__).resolve().parents[2] / "app" / "static" / "js"
              / "calendar" / "source-swap.js").read_text(encoding="utf-8")

    def test_it_sends_nothing_anywhere(self):
        for way in ("fetch(", "XMLHttpRequest", "sendBeacon", "new WebSocket"):
            with self.subTest(way=way):
                self.assertNotIn(way, self.SCRIPT)

    def test_it_remembers_nothing_across_a_reload(self):
        """Nowhere to put it is what makes that true, rather than a promise."""
        for store in ("localStorage", "sessionStorage", "document.cookie", "indexedDB"):
            with self.subTest(store=store):
                self.assertNotIn(store, self.SCRIPT)

    def test_both_values_are_already_in_the_page_so_a_flip_needs_no_request(self):
        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title"))
        carried = json.loads(re.search(r"data-alternatives='([^']*)'", html).group(1))
        self.assertEqual(carried["title"], {"trakt": "One Title", "simkl": "Another Title"})


class BothServicesPagesAreOfferedTests(unittest.TestCase):
    """A card several services described has several real destinations, and they
    are not competing answers: the card is attributed to one service, but the
    other's page for the same title exists and is just as good a place to go."""

    def setUp(self):
        self.html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, rating=7.9))

    def _buttons(self) -> list[str]:
        return re.findall(r'<a class="trakt-btn".*?</a>', self.html, re.S)

    def test_there_is_one_button_per_service_that_has_a_page(self):
        buttons = self._buttons()
        self.assertEqual(len(buttons), 2)
        self.assertIn("https://trakt.test/show", buttons[0])
        self.assertIn("https://simkl.test/show", buttons[1])

    def test_each_wears_its_own_services_name(self):
        trakt, simkl = self._buttons()
        self.assertIn('title="View on Trakt"', trakt)
        self.assertIn("trakt-logo-dark.svg", trakt)
        self.assertIn('title="View on Simkl"', simkl)
        self.assertIn("simkl_white_with_shaddow_transparent.png", simkl)

    def test_they_are_offered_in_declared_service_order_however_the_card_resolved(self):
        """The order is the app's own, not "whoever won this card" — which would
        move the buttons around from card to card on one page."""
        from app.sources import prefs as source_prefs

        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, rating=7.9),
            prefs=source_prefs.SourcePrefs(user_id=1, precedence={"default": "simkl"}))
        order = re.findall(r'title="View on (\w+)"', html)
        self.assertEqual(order, ["Trakt", "Simkl"])

    def test_where_a_service_goes_is_never_stored_with_the_window(self):
        """These are the same kind of fact as the provenance maps: they only
        exist once several records have been compared, which happens at read over
        a window filled without knowing who would read it."""
        groups = calendar_cache.group_records(
            [_record(Source.TRAKT), _record(Source.SIMKL, rating=7.9)])
        resolved = calendar_resolve.resolve(groups[0], None)
        self.assertEqual(set(resolved.source_links), {"trakt", "simkl"})
        self.assertNotIn("source_links", resolved.to_dict())


class TheCardAgreesWithTheResolutionVocabularyTests(unittest.TestCase):
    """The card decides which fields are worth drawing twice; app/calendar/
    resolve.py decides which fields can conflict at all. The first has to be a
    subset of the second, or the card carries a control that can never appear."""

    def test_every_field_the_card_marks_is_one_resolution_can_report(self):
        marked = set(re.findall(r"swap\('([a-z_]+)'\)", CARD))
        marked |= set(re.findall(r"\('([a-z_]+)', '[^']+'\)", CARD))
        self.assertTrue(marked, "the card marks no fields at all")
        self.assertLessEqual(marked, set(calendar_resolve.SCALAR_FIELDS))

    def test_the_card_markup_does_not_depend_on_the_card_style(self):
        """The poster-only wall is a body class over the SAME markup — the
        degrade to the winner alone is where the marks are placed, not a second
        template. A card that branched on the style would give the two layouts
        different cards and only one of them would be tested."""
        self.assertNotIn("card_style", CARD)


class ThePosterOnlyWallRendersAMergedCardTests(unittest.TestCase):
    """The layout with the least room, over a card carrying the most marks."""

    def test_a_merged_card_renders_under_every_card_style(self):
        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title", poster="https://simkl.test/q.jpg",
                    rating=7.9, overview="Another overview."))
        for style in ("vertical", "horizontal", "poster"):
            with self.subTest(style=style):
                page = templates.env.from_string(
                    '<body class="card-{{ style }}">{{ card | safe }}</body>').render(
                    style=style, card=html)
                self.assertIn("source-swap", page)
                self.assertIn("rating-badge", page)


if __name__ == "__main__":
    unittest.main()
