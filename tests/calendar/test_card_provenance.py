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
from urllib.parse import quote
from pathlib import Path
from zoneinfo import ZoneInfo

from app.calendar import resolve as calendar_resolve
from app.config import Settings
from app.endpoints import get_endpoint
from app.providers.base import Record, Source, render
from app.templating import TEMPLATES_DIR, templates

from tests.support import CardTestCase, read_card, stored_groups

UTC = ZoneInfo("UTC")


def _resolve(group, prefs=None, endpoint=None, settings=None):
    """The one card `group`'s records make for a viewer, read back through the
    calendar's own read from a store they were written into (support.read_card)."""
    records = list(group["records"]) if isinstance(group, dict) else list(group)
    return read_card(records, prefs, settings=settings,
                     endpoint=getattr(endpoint, "key", endpoint) or "shows")

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
    groups = stored_groups(list(records))
    assert len(groups) == 1, "these records do not match; the matcher would draw two cards"
    resolved = _resolve(groups[0], prefs)
    item = render(resolved, UTC)
    return templates.env.get_template("_card.html").render(
        item=item, not_watching=set(), new_ids=set(), is_admin=False,
        # The month the card is on, which its watch toggle posts: marking a show
        # is answered with every card for it ON THIS MONTH, so the button has to
        # say which month it is asking about.
        month=1, year=2026, endpoint=get_endpoint("shows/new"),
        settings=Settings())


def _swap(html: str, field: str) -> str | None:
    """The flip for `field`: a radio group, one entry per service, each paired
    with that service's own value — see _card.html's `swap` macro."""
    match = re.search(r'<span class="flip" data-field="%s">.*?</span>\s*</span>' % field,
                      html, re.S)
    return match.group(0) if match else None


class ACardOnlyOneServiceDescribedTests(CardTestCase):
    """Every card on an instance that has one source, and most of them on one
    that has two. None of the provenance machinery may cost it anything."""

    def setUp(self):
        super().setUp()
        self.html = _card_html(_record(Source.TRAKT))

    def test_it_carries_no_provenance_attribute_at_all(self):
        self.assertNotIn("data-alternatives", self.html)

    def test_it_draws_no_swap_logo_and_no_dual_chip(self):
        self.assertNotIn("source-swap", self.html)
        self.assertNotIn("chip dual", self.html)

    def test_nothing_at_all_is_drawn_on_its_poster_to_name_a_service(self):
        """A mark on the poster is a CONTROL. There is nothing to flip to here, so
        drawing one would offer a choice that cannot be made, and a reader who
        clicks it gets nothing."""
        self.assertNotIn("poster-marks", self.html)
        self.assertNotIn("source-logo", self.html.split('class="card-body"')[0])

    def test_its_outbound_button_is_its_own_services(self):
        """The one service it was drawn from, in colour — the TMDB id its ids
        name is a title TMDB knows, drawn greyed beside it (ServicesThatOnlyKnow
        TheTitleTests)."""
        self.assertIn('title="View on Trakt"', self.html)
        self.assertEqual(self.html.count('class="trakt-btn"'), 1)
        self.assertEqual(self.html.count('class="trakt-btn unlisted"'), 1)

    def test_the_button_wears_the_services_name_rather_than_its_symbol(self):
        """A button is a promise about where a click goes, and the name says
        that where a symbol asks to be recognised."""
        self.assertIn("source-wordmark", self.html)
        self.assertIn("trakt-logo-dark.svg", self.html)


class ACardTheOtherServiceListedTests(CardTestCase):
    """The unmatched case: two services listed a title without agreeing it is the
    same title, so it draws twice. It still has to say which service it came
    from — otherwise the two cards read as the app showing one thing twice."""

    def setUp(self):
        super().setUp()
        self.html = _card_html(_record(Source.SIMKL))

    def test_it_says_which_service_listed_it_on_its_outbound_button(self):
        """AND NOWHERE ON THE POSTER. This is the whole attribution a
        single-source card has, so it is asserted here rather than left to the
        button's own tests: if the button ever stopped naming its service, a
        merged instance would have cards nothing could attribute."""
        self.assertIn("Simkl", self.html)
        self.assertNotIn("poster-marks", self.html)

    def test_the_outbound_button_wears_the_name_of_where_it_actually_goes(self):
        """It used to be one service's logo on every card, including the ones
        whose link led to the other service entirely."""
        self.assertIn('title="View on Simkl"', self.html)
        self.assertIn("simkl_white_with_shaddow_transparent.png", self.html)
        self.assertNotIn("trakt-logo-dark.svg", self.html)

    def test_it_offers_the_one_page_there_is(self):
        """One service listed it, so there is one place to go in colour — and
        TMDB, whose id it names, greyed beside it."""
        self.assertEqual(self.html.count('class="trakt-btn"'), 1)
        self.assertEqual(self.html.count('class="trakt-btn unlisted"'), 1)


class ADisagreementIsDrawnWhereThereIsOneTests(CardTestCase):
    """`alternatives` names exactly the fields the two services filled in
    differently, and the card's marks are that set and no wider."""

    def setUp(self):
        super().setUp()
        self.html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title", poster="https://simkl.test/q.jpg"))

    def test_both_values_ship_with_the_card_so_a_flip_costs_no_request(self):
        """Each service's own answer is DRAWN, once, in the place it is shown —
        so flipping is a radio changing which of two elements the stylesheet
        displays and never a request."""
        for field in ("title", "poster"):
            with self.subTest(field=field):
                flip = _swap(self.html, field)
                self.assertIsNotNone(flip)
                self.assertIn('value="trakt"', flip)
                self.assertIn('value="simkl"', flip)
        self.assertIn("One Title", self.html)
        self.assertIn("Another Title", self.html)
        # Each picture is a real <img>, proxied the way every poster on a card
        # is; both are in the page and the radio decides which is displayed.
        self.assertIn(quote("https://simkl.test/q.jpg", safe=""), self.html)
        self.assertIn(quote("https://trakt.test/p.jpg", safe=""), self.html)

    def test_a_value_carrying_a_quote_cannot_break_out_of_the_card(self):
        """These strings are whatever a service wrote, and they are rendered onto
        every card of a merged group — as text and inside attributes both."""
        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title='''A "quoted" <b>title</b>\' '''))
        self.assertNotIn("<b>title</b>", html)
        self.assertIn("&lt;b&gt;title&lt;/b&gt;", html)

    def test_a_conflicting_field_draws_a_mark_per_service_and_checks_the_winner(self):
        flip = _swap(self.html, "title")
        self.assertIsNotNone(flip)
        # The winner is the checked one, and each service's mark travels inside
        # its own value — so only the shown service's mark is drawn, with no rule
        # anywhere naming a service.
        self.assertRegex(flip, r'value="trakt"[^>]*checked')
        self.assertNotRegex(flip, r'value="simkl"[^>]*checked')
        self.assertEqual(flip.count('class="source-swap"'), 2)

    def test_the_element_showing_the_value_names_the_service_it_came_from(self):
        """The script tells the target from the button by this attribute: the
        button names the field and the target names field AND service."""
        self.assertIn('data-field="title" data-source="trakt"', self.html)
        self.assertIn('data-field="poster" data-source="trakt"', self.html)

    def test_a_field_they_agreed_on_draws_nothing(self):
        """A logo inviting a click that changes nothing is worse than no logo."""
        self.assertIsNone(_swap(self.html, "overview"))
        self.assertNotIn('data-field="overview"', self.html)

    def test_a_merged_card_is_marked_on_exactly_the_fields_that_conflict(self):
        """The poster is where the rule is easiest to break, because the mark
        drawn there used to be two things at once — a flip AND a badge — and only
        one of them depended on there being an alternative."""
        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, overview="Another overview.",
                    poster="https://trakt.test/p.jpg"))
        self.assertIsNotNone(_swap(html, "overview"))
        self.assertIsNone(_swap(html, "poster"))
        self.assertIsNone(_swap(html, "title"))
        self.assertNotIn("poster-marks", html)

    def test_the_swap_does_not_also_open_the_details_panel(self):
        """The card opens the details modal on click; flipping one value must not
        also open it. The mark is a <label>, which static/js/ui.js's opener
        already leaves alone as a control acting for itself — so nothing here
        stops the click and nothing has to."""
        self.assertIn('<label class="source-swap"', self.html)
        self.assertNotIn("stopPropagation", self.html)
        self.assertNotIn("onclick", _swap(self.html, "title"))

    def test_the_preference_decides_which_side_is_shown_first(self):
        """The flip is a view of the same two values whichever way round the
        account's own preference put them."""
        from app.sources import prefs as source_prefs

        html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title"),
            prefs=source_prefs.SourcePrefs(user_id=1, metadata_order=["simkl"]))
        self.assertIn('data-field="title" data-source="simkl"', html)
        self.assertIn("Another Title", html)


class TwoNumbersRatherThanAnAverageTests(CardTestCase):
    """A rating, a runtime, a network or a status the two services disagree
    about is shown as both, never as a mean — two user bases produce two
    legitimate numbers and their average is a number nobody reported."""

    def setUp(self):
        super().setUp()
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


class WhenTheServicesDisagreeAboutTheDayTests(CardTestCase):
    """The card sits on ONE service's day — the viewer's first — and says what
    the others gave, under each one's mark, wherever they differ for this
    viewer (Item.air_choices). Yellowjackets S04E01, Simkl's 21 November 01:00
    UTC against Trakt's 23 November 02:00 UTC, read in UTC."""

    def _chip(self, html: str) -> str | None:
        found = re.search(r'<span class="chip dual day" data-field="airing".*?</span>\s*</span>',
                          html, re.S)
        return found.group(0) if found else None

    def test_it_takes_the_day_chips_place(self):
        """It names the days itself, so the card does not also draw "📆 Monday"."""
        html = _card_html(_record(Source.TRAKT, air_ts=1795399200.0),
                          _record(Source.SIMKL, air_ts=1795222800.0))
        self.assertNotIn('<span class="chip day">', html)
        self.assertIn('<span class="chip day">', _card_html(_record(Source.TRAKT)))

    def test_both_days_are_on_the_card_under_their_services_marks(self):
        html = _card_html(_record(Source.TRAKT, air_ts=1795399200.0),
                          _record(Source.SIMKL, air_ts=1795222800.0))
        chip = self._chip(html)
        self.assertIn("Mon 23 Nov 02:00", chip)
        self.assertIn("Sat 21 Nov 01:00", chip)
        self.assertIn("trakt-circlemark-dark.svg", chip)
        self.assertIn("simkllogo.svg", chip)

    def test_services_that_agree_draw_no_chip(self):
        self.assertIsNone(self._chip(_card_html(_record(Source.TRAKT), _record(Source.SIMKL))))

    def test_a_date_with_no_time_agrees_with_a_time_on_that_date(self):
        """TMDB's bare date and Trakt's evening on it are one claim, said at two
        resolutions — nothing to choose between."""
        html = _card_html(_record(Source.TRAKT, air_ts=AIR_TS + 3600),
                          _record(Source.SIMKL, air_ts=AIR_TS - AIR_TS % 86400, date_only=True))
        self.assertIsNone(self._chip(html))

    def test_a_card_only_one_service_described_draws_none(self):
        self.assertIsNone(self._chip(_card_html(_record(Source.TRAKT))))


class AFlipIsAViewFlipAndNothingElseTests(CardTestCase):
    """Choosing a service mark changes one value on one card, on screen, until the
    page is left. It is not a preference, and the reason is not tidiness: whose
    value wins is stated once for every card on the Sources screen, so a choice
    that wrote through would change every other card on the page as a side effect
    of looking at one of them.

    IT IS MARKUP, so these are properties of what is rendered rather than of a
    script's contents. A radio group cannot send anything anywhere and has
    nowhere to remember anything; `autocomplete="off"` is what stops the BROWSER
    remembering it across a reload, which would otherwise put a card back showing
    a service the page did not render it with.
    """

    def setUp(self):
        super().setUp()
        self.html = _card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, title="Another Title"))

    def test_it_sends_nothing_anywhere(self):
        flip = _swap(self.html, "title")
        for way in ("hx-post", "hx-get", "hx-put", "hx-patch", "hx-delete", "onchange"):
            with self.subTest(way=way):
                self.assertNotIn(way, flip)

    def test_it_remembers_nothing_across_a_reload(self):
        self.assertIn('autocomplete="off"', _swap(self.html, "title"))

    def test_both_values_are_drawn_rather_than_carried_as_data(self):
        """Each service's answer is an ELEMENT, once, where it is shown. It used
        to be a JSON copy of every alternative on every card's attribute, read
        back by a script — one real database had a 1,790-character description
        in one of them."""
        self.assertNotIn("data-alternatives", self.html)
        self.assertIn("One Title", self.html)
        self.assertIn("Another Title", self.html)

    def test_the_stylesheet_decides_which_is_shown_and_names_no_service(self):
        """The pairing is the mechanism: each radio is immediately followed by
        its own value, so `input:checked + .flip-value` is the whole rule and a
        third service registering costs the stylesheet nothing."""
        flip = _swap(self.html, "title")
        pairs = re.findall(r'<input type="radio"[^>]*>\s*<span class="flip-value">', flip)
        self.assertEqual(len(pairs), 2, flip)
        css = (Path(__file__).resolve().parents[2] / "app" / "static" / "css"
               / "style.css").read_text(encoding="utf-8")
        self.assertIn(".flip-pick:checked + .flip-value", css)
        for service in (str(source) for source in Source):
            with self.subTest(service=service):
                self.assertNotIn(f'.flip-pick[value="{service}"]', css)


class BothServicesPagesAreOfferedTests(CardTestCase):
    """A card several services described has several real destinations, and they
    are not competing answers: the card is attributed to one service, but the
    other's page for the same title exists and is just as good a place to go."""

    def setUp(self):
        super().setUp()
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
            prefs=source_prefs.SourcePrefs(user_id=1, metadata_order=["simkl"]))
        order = re.findall(r'title="View on (\w+)"', html)
        self.assertEqual(order, ["Trakt", "Simkl"])

    def test_where_a_service_goes_is_never_stored_with_the_window(self):
        """These are the same kind of fact as the provenance maps: they only
        exist once several records have been compared, which happens at read over
        a window filled without knowing who would read it."""
        groups = stored_groups(
            [_record(Source.TRAKT), _record(Source.SIMKL, rating=7.9)])
        resolved = _resolve(groups[0], None)
        self.assertEqual([(link.source, link.listed) for link in resolved.links],
                         [("tmdb", False), ("trakt", True), ("simkl", True)])
        self.assertNotIn("links", resolved.to_dict())


class ServicesThatOnlyKnowTheTitleTests(CardTestCase):
    """A service the title store knows the title at (Record.known_ids) has a page
    for it whether or not it lists this airing: its button is drawn, greyed, in
    its declared place — the colour stays with the services the card is drawn
    from. The owner's case: TMDB-only Season Premieres whose Trakt or Simkl id a
    lookup by shared id had learned, and no card offered."""

    def setUp(self):
        super().setUp()
        self.tmdb_only = _record(
            Source.TMDB, ids={"tmdb": 900}, detail_url="https://tmdb.test/show",
            known_ids={"tmdb": 900, "trakt": 77, "trakt_slug": "one-title",
                       "simkl": 55})
        self.html = _card_html(self.tmdb_only)

    def _button(self, label: str) -> str:
        return re.search(r'<a class="trakt-btn[^"]*"[^>]*title="View on %s[^"]*".*?</a>'
                         % label, self.html, re.S).group(0)

    def test_every_service_that_knows_it_gets_a_button_in_declared_order(self):
        self.assertEqual(re.findall(r'title="View on (\w+)', self.html),
                         ["TMDB", "Trakt", "Simkl"])

    def test_only_the_service_it_was_drawn_from_is_in_colour(self):
        self.assertIn('class="trakt-btn"', self._button("TMDB"))
        self.assertIn("https://tmdb.test/show", self._button("TMDB"))
        for label in ("Trakt", "Simkl"):
            with self.subTest(service=label):
                self.assertIn('class="trakt-btn unlisted"', self._button(label))
                self.assertIn(f"this card isn't drawn from {label}", self._button(label))

    def test_a_known_page_is_built_from_the_services_own_id(self):
        self.assertIn("https://trakt.tv/shows/one-title", self._button("Trakt"))
        self.assertIn("https://simkl.com/tv/55", self._button("Simkl"))

    def test_the_known_ids_never_become_the_cards_own(self):
        """`ids` is what the card is keyed and grouped by, and a service's own id
        is that service's to state (identity.complete); the store's knowledge
        only addresses a button."""
        groups = stored_groups([self.tmdb_only])
        resolved = _resolve(groups[0], None)
        self.assertNotIn("trakt", resolved.ids)
        self.assertEqual(resolved.known_ids["trakt"], 77)

    def test_a_service_the_viewer_left_out_is_greyed_too(self):
        """The card is drawn from the services this viewer's calendar admits; a
        listing it left out is a service it knows the title at."""
        from app.sources import prefs as source_prefs

        html = _card_html(
            _record(Source.TRAKT), _record(Source.SIMKL, rating=7.9),
            prefs=source_prefs.SourcePrefs(user_id=1, calendar_source="trakt"))
        self.assertIn('class="trakt-btn unlisted" href="https://simkl.com/tv/simkl-id"', html)


class TheCardAgreesWithTheResolutionVocabularyTests(CardTestCase):
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


class TheIdsThePanelIsOpenedWithTests(CardTestCase):
    """A card asks for its panel with EVERY service's own id, not just Trakt's.

    The server picks who to ask from what the card's details address names — so
    an id missing from it is a card that opens on an apology instead of on the
    one service that could describe it. The failure is invisible from the
    calendar page, which renders identically either way.
    """

    def _address(self, html: str) -> str:
        found = re.search(r'data-details-open="([^"]+)"', html)
        self.assertIsNotNone(found, "the card carries no details address")
        return found.group(1).replace("&amp;", "&")

    def test_a_merged_card_asks_with_both_services_ids(self):
        address = self._address(_card_html(_record(Source.TRAKT), _record(Source.SIMKL)))
        self.assertIn("trakt=trakt-id", address)
        self.assertIn("simkl=simkl-id", address)

    def test_the_three_shown_fields_travel_as_a_SERVICE_NAME(self):
        """The panel opens on the name, picture and description the card is
        showing, which the describing service's own answer may disagree with. It
        used to be told them by VALUE — a card whose services wrote different
        descriptions shipped the whole of one in an attribute and asked for it
        back in a URL, measured at 1,790 characters at its longest on one real
        database. A service's name is one word, and the route reads the row."""
        address = self._address(_card_html(
            _record(Source.TRAKT),
            _record(Source.SIMKL, overview="Another overview.", title="Another Title")))
        for field in ("title", "poster", "overview"):
            with self.subTest(field=field):
                self.assertRegex(address, field + "=(trakt|simkl)(&|$)")
        self.assertNotIn("Another overview.", address)

    def test_the_name_is_the_service_whose_value_is_on_screen(self):
        """Which is the same question the card asks to decide which service's mark
        to draw beside the field, so the two cannot disagree — see
        Record.shown_source."""
        html = _card_html(_record(Source.TRAKT),
                          _record(Source.SIMKL, overview="Another overview."))
        shown = re.search(r'class="overview" data-field="overview" data-source="(\w+)"', html)
        self.assertIsNotNone(shown, "the card drew no source on its description")
        self.assertIn("overview=" + shown.group(1), self._address(html))

    def test_a_card_only_one_service_described_names_that_one(self):
        """There is nothing to disagree about, and the answer is still worth
        naming: which service DESCRIBES the title is chosen separately from which
        one listed it, so the two can differ even here."""
        self.assertIn("overview=simkl", self._address(_card_html(_record(Source.SIMKL))))

    def test_a_simkl_only_card_asks_with_simkls_alone(self):
        """A service the card has no id for is left off the address rather than
        sent blank, and the server reads the two the same way."""
        address = self._address(_card_html(_record(Source.SIMKL)))
        self.assertIn("simkl=simkl-id", address)
        self.assertNotIn("trakt=", address)


class ThePosterOnlyWallRendersAMergedCardTests(CardTestCase):
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
