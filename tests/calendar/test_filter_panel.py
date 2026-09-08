"""The filters panel over HTTP: which services answer each medium, what the save
refuses, and what the page it renders actually contains.

The per-DIMENSION behaviour lives in tests/calendar/test_routes.py's
ViewerFilterTests, and the encoding in tests/calendar/test_vocab.py. What is
covered here is the panel as a whole — the half that is markup, and the half that
writes a second table.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import patch

from app import auth
from app.calendar import vocab
from app.providers.base import Media, Source
from app.sources import prefs as source_prefs
from tests.calendar.test_routes import CalendarRouteTestCase
from tests.support import window_fetch


class PerMediumSourcesTests(CalendarRouteTestCase):
    """One account, two answers. A per-ENDPOINT override was removed once and
    this is not that: it is per MEDIUM, and it exists because the services are
    separately good at the two jobs — a preference no narrowing can express."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("panel_viewer")
        self.sign_in_as(self.user_id)

    def load(self) -> source_prefs.SourcePrefs:
        return asyncio.run(source_prefs.load(self.user_id))

    def test_a_film_selection_leaves_the_show_selection_alone(self):
        resp = self.client.post("/api/me/filters", json={"sources_movie": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        saved = self.load()
        self.assertEqual(saved.movie_calendar_source, "trakt")
        self.assertEqual(saved.calendar_source, source_prefs.AUTO)

    def test_for_media_picks_the_answer_that_governs_that_calendar(self):
        """`admits_calendar` stays the single-selection test it has always been;
        narrowing happens once, before it, so nothing downstream learns that the
        question now has two answers."""
        self.client.post("/api/me/filters", json={"sources_movie": "trakt"})
        saved = self.load()
        self.assertTrue(saved.for_media(Media.SHOW).admits_calendar(Source.SIMKL))
        self.assertFalse(saved.for_media(Media.MOVIE).admits_calendar(Source.SIMKL))

    def test_every_service_ticked_stores_auto_rather_than_a_named_set(self):
        """The difference outlives the request: `auto` means "whatever there is,
        now and later", so an instance that registers a third service starts
        showing it. A named set is a choice made from the menu that existed at
        the time, and a third service is not something the chooser agreed to."""
        self.client.post("/api/me/filters", json={"sources_show": "trakt,simkl"})
        self.assertEqual(self.load().calendar_source, source_prefs.AUTO)

    def test_no_service_at_all_is_refused_rather_than_stored(self):
        """An empty calendar with no explanation reads as a broken app, and the
        honest way to see nothing is to stop opening the page."""
        resp = self.client.post("/api/me/filters", json={"sources_show": ""})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("At least one", resp.json()["error"])

    def test_a_service_this_app_has_never_heard_of_is_refused(self):
        resp = self.client.post("/api/me/filters", json={"sources_show": "letterboxd"})
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_a_refused_service_leaves_the_filters_unwritten(self):
        """The two halves write different tables and the services half is the
        one that can be refused, so it goes first — a viewer pressed one button
        and it either takes both or takes neither."""
        resp = self.client.post("/api/me/filters", json={
            "sources_show": "",
            vocab.field_name(vocab.TV_GENRES, "drama"): "exclude",
        })
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id))["tv_genres"], "")


class ThePanelIsRenderedTests(CalendarRouteTestCase):
    """The panel arrives with its answers already in it, which is what lets it
    open with no request and hold no client copy of the vocabulary."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("panel_render")
        self.sign_in_as(self.user_id)
        # An empty month is enough: what is under test is the panel the shell
        # renders, not the cards beside it. The patch is what keeps the page
        # render off the network.
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, endpoint="shows/new") -> str:
        return self.client.get(f"/?year=2026&month=7&endpoint={endpoint}").text

    def test_the_chips_are_server_rendered_markup(self):
        page = self.page()
        self.assertIn(vocab.field_name(vocab.TV_GENRES, "drama"), page)
        self.assertIn(vocab.field_name(vocab.MOVIE_GENRES, "horror"), page)

    def test_a_stored_answer_arrives_already_checked(self):
        self.client.post("/api/me/filters", json={
            vocab.field_name(vocab.TV_GENRES, "reality"): "exclude"})
        name = vocab.field_name(vocab.TV_GENRES, "reality")
        # The exclude radio for that chip, and only it, carries `checked`. Read
        # per TAG rather than per line: the panel wraps an input over several
        # lines, so a line-wise search would find the name and the attribute in
        # different places and prove nothing.
        tags = re.findall(rf'<input[^>]*name="{re.escape(name)}"[^>]*>', self.page())
        self.assertEqual(len(tags), 3)  # ignored / include / exclude
        checked = [tag for tag in tags if "checked" in tag]
        self.assertEqual(len(checked), 1)
        self.assertIn('value="exclude"', checked[0])

    def test_a_token_the_vocabulary_does_not_name_is_drawn_too(self):
        """Whatever a card badge added has to appear in the panel, or the badge
        writes an answer the one screen that lists them cannot show."""
        self.client.post("/api/me/filters/badge", json={
            "dimension": "network", "token": "tvN", "mode": "exclude", "media": "show"})
        self.assertIn(vocab.field_name(vocab.NETWORK_FILTER, "tvN"), self.page())

    def test_the_tab_that_opens_is_the_calendar_you_are_looking_at(self):
        """A Jinja conditional rather than a boot script: it is decided once when
        the page is built and never changes while it is open."""
        shows = self.page("shows/new")
        self.assertIn('id="ftab_show" class="ftab-radio" autocomplete="off"\n                   checked',
                      shows.replace("\r\n", "\n"))
        movies = self.page("movies")
        self.assertIn('id="ftab_movie" class="ftab-radio" autocomplete="off"\n                   checked',
                      movies.replace("\r\n", "\n"))

    def test_the_switch_is_drawn_the_way_it_reads(self):
        """ON MEANS THE FILTERING IS HAPPENING, which is the only polarity people
        read correctly. The column underneath is `filters_paused` — every account
        starts having paused nothing — so the field is inverted once at the route
        rather than the control being drawn backwards to match a column name."""
        tags = re.findall(r'<input[^>]*id="f_filters_on"[^>]*>', self.page())
        self.assertEqual(len(tags), 1)
        self.assertIn("checked", tags[0])          # nothing paused: switch on

        self.client.post("/api/me/filters", json={"filters_on": False})
        tags = re.findall(r'<input[^>]*id="f_filters_on"[^>]*>', self.page())
        self.assertNotIn("checked", tags[0])       # paused: switch off

    def test_a_case_sensitive_dimension_says_so_in_its_markup(self):
        """The browser decides whether a typed token is one already drawn, and
        it must not decide by guessing which dimension it is looking at: 'TVN'
        and 'tvN' are two broadcasters and both have to be addable, while two
        spellings of a genre are one token."""
        page = self.page()
        networks = re.search(r'<input[^>]*aria-label="Add to networks"[^>]*>', page)
        genres = re.search(r'<input[^>]*aria-label="Add to genres"[^>]*>', page)
        self.assertIsNotNone(networks)
        self.assertIn("data-exact-case", networks.group(0))
        self.assertNotIn("data-exact-case", genres.group(0))

    def test_the_tab_you_are_not_on_advertises_what_it_holds(self):
        """So "why is my movie calendar empty" is answerable without switching
        tabs to find out."""
        self.client.post("/api/me/filters", json={
            vocab.field_name(vocab.MOVIE_GENRES, "horror"): "exclude",
            vocab.field_name(vocab.MOVIE_GENRES, "war"): "exclude"})
        self.assertIn('<span class="ftab-count">2</span>', self.page("shows/new"))
