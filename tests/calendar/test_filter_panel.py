"""The filters panel over HTTP: what it draws, what its writes refuse, and what a
save answers with.

The per-DIMENSION behaviour lives in tests/calendar/test_routes.py's
ViewerFilterTests, and the encoding in tests/calendar/test_vocab.py. What is
covered here is the panel as a whole — the half that is markup, the half that
writes a second table, and the half that draws the calendar again.

THE PANEL IS A FRAGMENT, so every test that wants to look at it asks for it the
way the toolbar button does rather than reading it out of the page.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

from app import auth
from app.calendar import vocab
from app.providers.base import Media, Source
from app.sources import prefs as source_prefs
from tests.calendar.test_routes import CalendarRouteTestCase, _entry
from tests.support import window_fetch

SHOW_TRAKT = vocab.source_field_name(Media.SHOW, "trakt")
SHOW_SIMKL = vocab.source_field_name(Media.SHOW, "simkl")
MOVIE_TRAKT = vocab.source_field_name(Media.MOVIE, "trakt")
MOVIE_SIMKL = vocab.source_field_name(Media.MOVIE, "simkl")


class PerMediumSourcesTests(CalendarRouteTestCase):
    """One account, two answers. A per-ENDPOINT override was removed once and
    this is not that: it is per MEDIUM, and it exists because the services are
    separately good at the two jobs — a preference no narrowing can express."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("panel_viewer")
        self.sign_in_as(self.user_id)
        self.serving_month()

    def load(self) -> source_prefs.SourcePrefs:
        return asyncio.run(source_prefs.load(self.user_id))

    def save(self, **body):
        return self.client.post("/calendar/filters", json=body)

    def test_a_film_selection_leaves_the_show_selection_alone(self):
        resp = self.save(**{MOVIE_TRAKT: "1", MOVIE_SIMKL: "0"})
        self.assertEqual(resp.status_code, 200, resp.text)
        saved = self.load()
        self.assertEqual(saved.movie_calendar_source, "trakt")
        self.assertEqual(saved.calendar_source, source_prefs.AUTO)

    def test_for_media_picks_the_answer_that_governs_that_calendar(self):
        """`admits_calendar` stays the single-selection test it has always been;
        narrowing happens once, before it, so nothing downstream learns that the
        question now has two answers."""
        self.save(**{MOVIE_TRAKT: "1", MOVIE_SIMKL: "0"})
        saved = self.load()
        self.assertTrue(saved.for_media(Media.SHOW).admits_calendar(Source.SIMKL))
        self.assertFalse(saved.for_media(Media.MOVIE).admits_calendar(Source.SIMKL))

    def test_every_service_ticked_stores_auto_rather_than_a_named_set(self):
        """The difference outlives the request: `auto` means "whatever there is,
        now and later", so an instance that registers a third service starts
        showing it. A named set is a choice made from the menu that existed at
        the time, and a third service is not something the chooser agreed to."""
        self.save(**{SHOW_TRAKT: "1", SHOW_SIMKL: "1"})
        self.assertEqual(self.load().calendar_source, source_prefs.AUTO)

    def test_every_box_unticked_is_sayable_and_is_refused(self):
        """THE HIDDEN COMPANIONS ARE WHAT MAKE IT SAYABLE. An unticked box sends
        nothing at all, so without them "every service off" would reach the save
        as silence — which means "leave it alone" — and the browser had to patch
        the empty group in by hand. With them it arrives as an answer, and an
        empty calendar with no explanation reads as a broken app, so the answer
        is refused."""
        resp = self.save(**{SHOW_TRAKT: "0", SHOW_SIMKL: "0"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("At least one", resp.text)
        # Retargeted at the panel's own notice line, so the sentence does not
        # replace the calendar the form otherwise answers with.
        self.assertEqual(resp.headers["HX-Retarget"], "#filtersNotice")
        self.assertNotIn("calendarBody", resp.text)

    def test_a_service_this_app_has_never_heard_of_is_refused(self):
        resp = self.save(**{vocab.source_field_name(Media.SHOW, "letterboxd"): "1"})
        self.assertIn("letterboxd", resp.text)
        self.assertEqual(resp.headers["HX-Retarget"], "#filtersNotice")

    def test_a_refused_service_leaves_the_filters_unwritten(self):
        """The two halves write different tables and the services half is the
        one that can be refused, so it goes first — a viewer pressed one button
        and it either takes both or takes neither."""
        resp = self.save(**{
            SHOW_TRAKT: "0", SHOW_SIMKL: "0",
            vocab.field_name(vocab.TV_GENRES, "drama"): "exclude",
        })
        self.assertIn("At least one", resp.text)
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id))["tv_genres"], "")

    def test_a_group_the_panel_never_drew_is_left_alone(self):
        """An instance with nothing to choose between draws no control, so a
        payload naming services on such a calendar is not a viewer's doing."""
        self.save(**{MOVIE_TRAKT: "1", MOVIE_SIMKL: "0"})
        before = self.load()
        with patch("app.calendar.routes._answering_services_for_media", return_value=[]):
            self.save(**{MOVIE_TRAKT: "0", MOVIE_SIMKL: "0"})
        self.assertEqual(self.load(), before)


class ThePanelIsRenderedTests(CalendarRouteTestCase):
    """The panel arrives with every answer already in it. It is asked for on each
    opening rather than shipped with the page, so what it shows IS what is stored
    and there is no second copy anywhere to fall out of step."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("panel_render")
        self.sign_in_as(self.user_id)
        # An empty month is enough: what is under test is the panel, not the
        # cards behind it. The patch is what keeps the render off the network.
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def panel(self, endpoint="shows/new") -> str:
        resp = self.client.get(f"/calendar/fragments/filters?endpoint={endpoint}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text

    def save(self, **body):
        return self.client.post("/calendar/filters", json=body)

    def test_the_page_carries_no_panel_at_all(self):
        """The toolbar button asks for it, and the slot it lands in is empty
        until it does — which is why a save can close the panel by emptying that
        slot rather than by telling anything to close."""
        page = self.client.get("/?year=2026&month=7&endpoint=shows/new").text
        self.assertIn('<div id="filtersSlot"></div>', page)
        self.assertNotIn('id="filtersForm"', page)
        # And it hands the calendar's own query string on, so the save that
        # answers with the month redrawn knows which month that is.
        asked = re.search(r'hx-get="(/calendar/fragments/filters[^"]*)"', page)
        self.assertIsNotNone(asked)
        for pair in ("month=7", "year=2026", "endpoint=shows/new"):
            with self.subTest(pair=pair):
                self.assertIn(pair, asked.group(1).replace("&amp;", "&"))

    def test_it_arrives_already_open_and_removes_itself_when_it_closes(self):
        panel = self.panel()
        self.assertIn('class="modal-overlay open"', panel)
        self.assertIn("data-modal-discard", panel)

    def test_the_chips_are_server_rendered_markup(self):
        panel = self.panel()
        self.assertIn(vocab.field_name(vocab.TV_GENRES, "drama"), panel)
        self.assertIn(vocab.field_name(vocab.MOVIE_GENRES, "horror"), panel)

    def test_a_stored_answer_arrives_already_checked(self):
        self.save(**{vocab.field_name(vocab.TV_GENRES, "reality"): "exclude"})
        name = vocab.field_name(vocab.TV_GENRES, "reality")
        # The exclude radio for that chip, and only it, carries `checked`. Read
        # per TAG rather than per line: the panel wraps an input over several
        # lines, so a line-wise search would find the name and the attribute in
        # different places and prove nothing.
        tags = re.findall(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', self.panel())
        self.assertEqual(len(tags), 3)  # ignored / include / exclude
        checked = [tag for tag in tags if "checked" in tag]
        self.assertEqual(len(checked), 1)
        self.assertIn('value="exclude"', checked[0])

    def test_a_token_the_vocabulary_does_not_name_is_drawn_too(self):
        """Whatever a card badge added has to appear in the panel, or the badge
        writes an answer the one screen that lists them cannot show."""
        self.client.post("/calendar/filters/badge", json={
            "dimension": "network", "token": "tvN", "mode": "exclude", "media": "show"})
        self.assertIn(vocab.field_name(vocab.NETWORK_FILTER, "tvN"), self.panel())

    def test_the_tab_that_opens_is_the_calendar_you_are_looking_at(self):
        """A Jinja conditional rather than a boot script: it is decided once when
        the panel is drawn and never changes while it is open. The endpoint rides
        on the address because that is what the button knows."""
        shows = self.panel("shows/new").replace("\r\n", "\n")
        self.assertRegex(shows, r'id="ftab_show" class="ftab-radio" autocomplete="off"\s*checked')
        movies = self.panel("movies").replace("\r\n", "\n")
        self.assertRegex(movies, r'id="ftab_movie" class="ftab-radio" autocomplete="off"\s*checked')

    def test_the_switch_is_drawn_the_way_it_reads_and_carries_a_companion(self):
        """ON MEANS THE FILTERING IS HAPPENING, which is the only polarity people
        read correctly. The column underneath is `filters_paused` — every account
        starts having paused nothing — so the field is inverted once at the route
        rather than the control being drawn backwards to match a column name.

        AND IT CAN SAY "off". An unticked box sends nothing, so the hidden
        companion beside it posts "0" under the same name and the ticked box
        overrides it. Without that, switching filters off was a state the panel
        could not express and the browser patched in."""
        panel = self.panel()
        self.assertIn('<input type="hidden" name="filters_on" value="0" form="filtersForm">',
                      panel)
        tags = re.findall(r'<input[^>]*id="f_filters_on"[^>]*>', panel)
        self.assertEqual(len(tags), 1)
        self.assertIn("checked", tags[0])          # nothing paused: switch on

        self.save(**{"filters_on": "0"})
        tags = re.findall(r'<input[^>]*id="f_filters_on"[^>]*>', self.panel())
        self.assertNotIn("checked", tags[0])       # paused: switch off

    def test_every_dimension_it_shows_is_in_the_payload_even_with_no_chips(self):
        """THE ABSENT/EMPTY RULE MADE ENFORCEABLE. Networks offer no vocabulary,
        so an account that has never named one draws that field with no chip at
        all — and without a marker the save could not tell it from a dimension
        the panel never showed."""
        panel = self.panel()
        for field in sorted(vocab.FIELDS):
            with self.subTest(field=field):
                self.assertIn(
                    f'<input type="hidden" name="{vocab.presence_name(field)}" '
                    f'value="" form="filtersForm">', panel)

    def test_every_control_names_the_form_rather_than_sitting_in_it(self):
        """The form is empty and the fields point at it, so the add boxes are NOT
        fields of it — a text input inside a form submits it on Enter, and Enter
        in an add box means "add this token", not "save everything"."""
        panel = self.panel()
        # The calendar's own query string rides with it, so the save answers
        # with the month the page is showing rather than the one today is in.
        self.assertIn('<form id="filtersForm" hx-post="/calendar/filters?endpoint=shows/new"',
                      panel)
        add = re.search(r'<input[^>]*id="fadd-network_filter"[^>]*>', panel)
        self.assertIsNotNone(add)
        self.assertNotIn("form=", add.group(0))

    def test_no_control_in_it_knows_what_a_spec_is(self):
        """The whole reason the panel is markup the server built: not the leading
        '-', not which dimensions fold case, not that networks are a list."""
        panel = self.panel()
        for leak in ("data-exact-case", "data-name-prefix", "fchipTemplate",
                     "network_filter\":", "onsubmit", "onclick"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, panel)

    def test_the_tab_you_are_not_on_advertises_what_it_holds(self):
        """So "why is my movie calendar empty" is answerable without switching
        tabs to find out."""
        self.save(**{
            vocab.field_name(vocab.MOVIE_GENRES, "horror"): "exclude",
            vocab.field_name(vocab.MOVIE_GENRES, "war"): "exclude"})
        self.assertIn('<span class="ftab-count">2</span>', self.panel("shows/new"))


class AddingATokenTheVocabularyDoesNotNameTests(CalendarRouteTestCase):
    """THE SERVER DRAWS THE CHIP, because the server owns every rule in it: the
    field name, the positional ids three labels point `for` at, whether two
    spellings are one token, the colon that would misfile it, and that a
    comma-separated paste is several tokens. All of that was written a second
    time in JavaScript."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("chip_adder")
        self.sign_in_as(self.user_id)
        self.serving_month()

    def add(self, field, token, **extra):
        return self.client.post("/calendar/filters/chip",
                                json={"field": field, "token": token, **extra})

    def chips(self, html, field):
        """Every token drawn for one field, once each, in document order. A chip
        is three radios sharing a name, so the raw matches come in threes."""
        seen = []
        for token in re.findall(
                rf'name="{re.escape(vocab.field_name(field, ""))}([^"]*)"', html):
            if token not in seen:
                seen.append(token)
        return seen

    def test_a_typed_token_comes_back_as_a_chip_set_to_only_this(self):
        """Somebody who went to the trouble of typing a name is naming something
        they want, and it is one press from the other answer either way."""
        html = self.add(vocab.NETWORK_FILTER, "tvN").text
        tag = re.search(
            r'<input[^>]*name="%s"[^>]*>' % re.escape(
                vocab.field_name(vocab.NETWORK_FILTER, "tvN")),
            html)
        self.assertIsNotNone(html)
        self.assertIn("tvN", self.chips(html, vocab.NETWORK_FILTER))
        checked = [t for t in re.findall(
            r'<input[^>]*name="%s"[^>]*>' % re.escape(
                vocab.field_name(vocab.NETWORK_FILTER, "tvN")), html) if "checked" in t]
        self.assertEqual(len(checked), 1)
        self.assertIn('value="include"', checked[0])
        self.assertIsNotNone(tag)

    def test_it_stores_nothing_until_save(self):
        self.add(vocab.NETWORK_FILTER, "tvN")
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id))["network_filter"], [])

    def test_a_comma_separated_paste_is_several_tokens(self):
        """A list of them is what people paste, and splitting one is the same job
        as spelling one — so it belongs beside `field_name` rather than in a
        second language."""
        html = self.add(vocab.NETWORK_FILTER, " HBO , AMC ,, ").text
        drawn = self.chips(html, vocab.NETWORK_FILTER)
        self.assertEqual(drawn, ["HBO", "AMC"])

    def test_a_token_already_drawn_is_lit_rather_than_doubled(self):
        """Two chips sharing a name would be one radio group with two faces, and
        one of them would look unset whatever the viewer pressed."""
        panel = self.add(vocab.TV_GENRES, "drama").text
        self.assertEqual(self.chips(panel, vocab.TV_GENRES).count("drama"), 1)
        checked = [t for t in re.findall(
            r'<input[^>]*name="%s"[^>]*>' % re.escape(
                vocab.field_name(vocab.TV_GENRES, "drama")), panel) if "checked" in t]
        self.assertIn('value="include"', checked[0])

    def test_case_folds_where_the_matching_parser_folds_and_not_where_it_does_not(self):
        """'TVN' and 'tvN' are a Polish broadcaster and a Korean one and both have
        to be addable; two spellings of a genre are one token. The browser used to
        be told which was which on a data attribute, which was that rule written
        twice."""
        networks = self.add(vocab.NETWORK_FILTER, "tvN,TVN").text
        self.assertEqual(sorted(self.chips(networks, vocab.NETWORK_FILTER)), ["TVN", "tvN"])
        genres = self.add(vocab.TV_GENRES, "Drama").text
        self.assertEqual(self.chips(genres, vocab.TV_GENRES).count("drama"), 1)

    def test_a_token_carrying_the_separator_is_dropped(self):
        """It would produce a field name that reads as a different field. Dropped
        silently because the panel comes back drawn: a chip that did not appear
        is the answer."""
        html = self.add(vocab.NETWORK_FILTER, "BBC:Two,HBO").text
        self.assertEqual(self.chips(html, vocab.NETWORK_FILTER), ["HBO"])

    def test_it_keeps_what_the_panel_was_already_holding(self):
        """The answer is drawn from the STORED preferences with the panel's own
        unsaved edits laid over them, which is what makes one renderer serve both
        the first draw and every redraw."""
        html = self.add(vocab.TV_GENRES, "soap", **{
            vocab.field_name(vocab.TV_GENRES, "reality"): "exclude"}).text
        checked = [t for t in re.findall(
            r'<input[^>]*name="%s"[^>]*>' % re.escape(
                vocab.field_name(vocab.TV_GENRES, "reality")), html) if "checked" in t]
        self.assertIn('value="exclude"', checked[0])

    def test_a_field_this_panel_does_not_offer_is_refused(self):
        self.assertEqual(self.add("card_style", "poster").status_code, 400)


class ClearingOneTabTests(CalendarRouteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("clearer")
        self.sign_in_as(self.user_id)
        self.serving_month()

    def clear(self, medium, **form):
        return self.client.post("/calendar/filters/clear",
                                json={"medium": medium, **form})

    def checked_values(self, html, field, token):
        name = vocab.field_name(field, token)
        return [re.search(r'value="([^"]*)"', tag).group(1)
                for tag in re.findall(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', html)
                if "checked" in tag]

    def test_it_greys_the_named_tab_and_leaves_the_other_alone(self):
        html = self.clear("show", **{
            vocab.field_name(vocab.TV_GENRES, "drama"): "exclude",
            vocab.field_name(vocab.MOVIE_GENRES, "horror"): "exclude",
        }).text
        self.assertEqual(self.checked_values(html, vocab.TV_GENRES, "drama"), [""])
        self.assertEqual(self.checked_values(html, vocab.MOVIE_GENRES, "horror"), ["exclude"])

    def test_it_saves_nothing(self):
        """The same bargain every other field in these modals makes: clear then
        close and the stored filters are untouched."""
        self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.TV_GENRES, "drama"): "exclude"})
        self.clear("show")
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id))["tv_genres"],
                         "-drama")

    def test_it_leaves_the_services_ticked_as_they_were(self):
        """An unticked service is a NARROWING, and clearing it would mean
        switching every service back on — which is the one state the save refuses
        and reads as the button working backwards."""
        html = self.clear("show", **{SHOW_TRAKT: "1", SHOW_SIMKL: "0"}).text
        boxes = re.findall(r'<input type="checkbox" id="fsrc-show-\d+"[^>]*>',
                           html.replace("\n", " "))
        self.assertTrue(boxes)
        self.assertEqual(sum("checked" in box for box in boxes), 1)

    def test_a_medium_it_has_no_tab_for_is_refused(self):
        self.assertEqual(self.clear("everything").status_code, 400)


class SavingOneTabLeavesTheOtherStandingTests(CalendarRouteTestCase):
    """THE ABSENT/EMPTY RULE, END TO END through the markup the panel actually
    draws. The half that could regress silently is the panel's: it renders BOTH
    tabs' chips, so both are in the payload, and a film dimension has to come back
    byte-identical from a save made on the TV tab."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("tab_saver")
        self.sign_in_as(self.user_id)
        self.serving_month()

    def prefs(self):
        return asyncio.run(auth.get_user_prefs(self.user_id))

    def panel_payload(self) -> dict:
        """Exactly what the rendered panel would post, read back off its markup.

        Not a hand-written body: what is under test is that the PANEL mentions
        what it has to, and a payload composed here would be testing the test."""
        html = self.client.get("/calendar/fragments/filters").text
        payload: dict = {}
        for tag in re.findall(r"<input\b[^>]*>", html.replace("\n", " ")):
            name = re.search(r'name="([^"]*)"', tag)
            value = re.search(r'value="([^"]*)"', tag)
            if not name or value is None:
                continue
            is_choice = 'type="radio"' in tag or 'type="checkbox"' in tag
            if is_choice and "checked" not in tag:
                continue
            payload[name.group(1)] = value.group(1)
        return payload

    def test_a_save_from_the_tv_tab_leaves_the_film_dimensions_exactly_as_they_were(self):
        self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.MOVIE_GENRES, "horror"): "exclude",
            vocab.field_name(vocab.MOVIE_CERTIFICATIONS, "R"): "exclude",
            vocab.field_name(vocab.MOVIE_RELEASE_COUNTRIES, "us"): "include",
            vocab.field_name(vocab.MOVIE_RELEASE_TYPES, "3"): "include",
        })
        before = self.prefs()

        # Now the panel is opened and ONE TV chip is cycled, exactly as a viewer
        # would: every other control posts what it was rendered holding.
        payload = self.panel_payload()
        payload[vocab.field_name(vocab.TV_GENRES, "reality")] = "exclude"
        self.assertEqual(
            self.client.post("/calendar/filters", json=payload).status_code, 200)

        after = self.prefs()
        self.assertEqual(after["tv_genres"], "-reality")
        for field in vocab.MOVIE_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field])

    def test_a_cleared_chip_clears(self):
        """A chip back at grey posts an EMPTY value rather than being left out,
        and that is the whole of what makes clearing possible."""
        self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.TV_GENRES, "reality"): "exclude"})
        self.assertEqual(self.prefs()["tv_genres"], "-reality")

        payload = self.panel_payload()
        self.assertEqual(payload[vocab.field_name(vocab.TV_GENRES, "reality")], "exclude")
        payload[vocab.field_name(vocab.TV_GENRES, "reality")] = ""
        self.client.post("/calendar/filters", json=payload)
        self.assertEqual(self.prefs()["tv_genres"], "")

    def test_a_dimension_with_no_chips_still_reaches_the_save(self):
        """Networks offer no vocabulary, so an account that has never named one
        draws that field empty — and the presence marker is what stops the save
        reading "shown and empty" as "a tab nobody opened"."""
        payload = self.panel_payload()
        self.assertIn(vocab.presence_name(vocab.NETWORK_FILTER), payload)
        self.assertIn(vocab.NETWORK_FILTER, vocab.specs_from_form(payload))

    def test_the_pause_travels_with_the_save_in_both_directions(self):
        payload = self.panel_payload()
        self.assertEqual(payload["filters_on"], "1")
        payload["filters_on"] = "0"
        self.client.post("/calendar/filters", json=payload)
        self.assertTrue(self.prefs()["filters_paused"])

        payload = self.panel_payload()
        self.assertEqual(payload.get("filters_on"), "0")
        payload["filters_on"] = "1"
        self.client.post("/calendar/filters", json=payload)
        self.assertFalse(self.prefs()["filters_paused"])


class TheSaveAnswersWithTheCalendarTests(CalendarRouteTestCase):
    """The one user-visible win: the panel no longer reloads the page."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("swapper")
        self.sign_in_as(self.user_id)
        self.serving_month()

    def test_it_answers_with_the_month_and_the_two_marks_above_it(self):
        resp = self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.TV_GENRES, "reality"): "exclude"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn('<div id="calendarBody">', resp.text)
        # The toolbar's two filter-dependent marks, addressed where they sit.
        self.assertIn('id="calendarMeta"', resp.text)
        self.assertIn('id="filtersBtn"', resp.text)
        self.assertEqual(resp.text.count('hx-swap-oob="true"'), 2)
        # And the panel closes by leaving the page, the same act a dismissal is.
        self.assertIn('<div id="filtersSlot" hx-swap-oob="innerHTML"></div>', resp.text)
        # Nothing tells the browser to reload.
        self.assertNotIn("HX-Refresh", resp.headers)

    def test_the_button_it_answers_with_reads_the_state_it_just_saved(self):
        """Drawn by the SAME macro the page draws it with, so the mark after a
        save and the mark after a reload cannot disagree."""
        plain = self.client.post("/calendar/filters", json={}).text
        self.assertNotIn("pill-btn active", plain)

        active = self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.TV_GENRES, "reality"): "exclude"}).text
        self.assertIn("pill-btn active", active)

        paused = self.client.post("/calendar/filters", json={"filters_on": "0"}).text
        self.assertIn("pill-btn paused", paused)
        self.assertIn("Filters off", paused)


class EveryWriteInThisPanelIsGuardedTests(CalendarRouteTestCase):
    """The four writes these two surfaces make, held to the app-wide rules at
    once. Asked per ROUTE rather than once for the panel, because a route that
    forgot one of them would still look right from the markup."""

    WRITES = ("/calendar/filters", "/calendar/filters/chip",
              "/calendar/filters/clear", "/calendar/filters/badge")

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("guarded")
        self.serving_month()

    def test_a_form_encoded_body_is_refused(self):
        """A form-encoded cross-origin POST is a CORS "simple request": the
        browser sends it with cookies and asks nobody's permission first. Not
        accepting the shape at all is what removes the whole class."""
        self.sign_in_as(self.user_id)
        for path in self.WRITES:
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.post(path, data={"medium": "show"}).status_code, 415)

    def test_a_body_with_no_content_type_is_refused(self):
        self.sign_in_as(self.user_id)
        for path in self.WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, content=b"{}").status_code, 415)

    def test_an_account_without_the_calendar_grant_is_refused(self):
        unapproved = self._make_user("waiting", calendar_approved=False)
        self.sign_in_as(unapproved)
        for path in self.WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 403)
        self.assertEqual(
            self.client.get("/calendar/fragments/filters").status_code, 403)

    def test_no_session_at_all_is_refused(self):
        self.client.cookies.clear()
        for path in self.WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}).status_code, 401)
        self.assertEqual(
            self.client.get("/calendar/fragments/filters").status_code, 401)

    def test_a_body_that_is_not_an_object_is_refused(self):
        self.sign_in_as(self.user_id)
        for path in self.WRITES:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json=["drama"]).status_code, 400)

    def test_a_value_it_cannot_act_on_is_refused_rather_than_stored(self):
        self.sign_in_as(self.user_id)
        self.assertEqual(
            self.client.post("/calendar/filters/chip",
                             json={"field": "hide_not_watching", "token": "x"}).status_code,
            400)
        self.assertEqual(
            self.client.post("/calendar/filters/clear",
                             json={"medium": "podcast"}).status_code, 400)
        self.assertEqual(
            self.client.post("/calendar/filters/badge",
                             json={"dimension": "language", "token": "en",
                                   "mode": "exclude"}).status_code, 400)
        # And the save ignores a chip mode it has no meaning for rather than
        # storing it — vocab.specs_from_form is where that is argued.
        self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.TV_GENRES, "drama"): "maybe"})
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id))["tv_genres"], "")


class TheMarkupWritesThroughTheEncoderTests(CalendarRouteTestCase):
    """EVERY WRITE ON THESE TWO SURFACES GOES THROUGH THE ENCODER. A control
    posting without `hx-json-enc` sends its native encoding and is refused by the
    shape guard, which reads to the person pressing it as nothing happening."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("encoder_reader")
        self.sign_in_as(self.user_id)
        entry = _entry("enc-show", "Encoded Show", "2026-07-15T20:00:00Z")
        entry["show"]["genres"] = ["drama"]
        entry["show"]["network"] = "HBO"
        entry["show"]["certification"] = "TV-MA"
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([entry]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def surfaces(self):
        """The panel, and the page — which is where a card's own badges are."""
        return {
            "panel": self.client.get("/calendar/fragments/filters").text,
            "calendar": self.client.get("/?year=2026&month=7").text,
        }

    def test_every_control_that_writes_carries_the_encoder(self):
        for where, html in self.surfaces().items():
            for element in re.findall(
                    r"<(?:form|button|input|select|span|div|a)\b[^>]*hx-post[^>]*>",
                    html.replace("\n", " ")):
                with self.subTest(where=where, element=element[:90]):
                    self.assertIn("hx-json-enc", element)

    def test_a_read_inside_a_writing_form_opts_out_of_the_encoder(self):
        """MEASURED IN THE BROWSER on the rankings board: the encoder is switched
        on through inheritance, a GET has no body to encode, and the request is
        dropped with one console line."""
        for where, html in self.surfaces().items():
            for form in re.findall(r"<form\b[^>]*hx-json-enc[^>]*>(.*?)</form>", html, re.S):
                for element in re.findall(r"<[a-z]+\b[^>]*hx-get[^>]*>", form):
                    with self.subTest(where=where, element=element[:90]):
                        self.assertIn('hx-json-enc="false"', element)

    def test_the_panels_form_is_empty_so_nothing_can_inherit_from_it(self):
        """WHICH IS WHY THE RULE ABOVE HAS NOTHING TO CATCH HERE, and saying so is
        the point: the form is a container for values that live outside it, so no
        read can inherit an encoder it has no body to use — and the add boxes are
        not its fields, which is what keeps Enter meaning "add this token"."""
        form = re.search(r'<form id="filtersForm"[^>]*>(.*?)</form>',
                         self.surfaces()["panel"], re.S)
        self.assertIsNotNone(form)
        self.assertEqual(form.group(1).strip(), "")
