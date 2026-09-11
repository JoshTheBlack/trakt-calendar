"""The Sonarr / Radarr / Seerr buttons on a calendar card and in its details
panel's header, drawn in their state by the server.

WHAT CHANGED: a button's state — can its service be reached, is the title already
in that library — used to arrive after paint, from copies of the server's two
caches that the page polled every minute. That was true of the cards the server
had just rendered too, and a day that arrived late went without until the next
sweep. Both facts are app/integrations/routes.py's own caches, so the server draws
them onto the card, the late day and the panel's header, and nothing polls.

No network: both caches are set directly and stamped fresh, so drawing a page
never starts a library read of its own.
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import assets
from app.calendar import vocab
from app.config import Settings, save_settings
from app.integrations import routes as integrations_routes
from app.providers.base import Source

from tests.calendar.test_routes import CalendarRouteTestCase, _entry
from tests.support import window_fetch


class _LibraryTestCase(CalendarRouteTestCase):
    """A month with two titles on the 15th — one already in Sonarr, one not —
    on an instance with Sonarr configured and reachable."""

    PAGE = "/calendar?year=2026&month=7&endpoint=shows"
    DAY = "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-15"

    def setUp(self):
        super().setUp()
        save_settings(Settings(trakt_client_id="test-client-id",
                               trakt_access_token="test-access-token",
                               sonarr_url="http://sonarr.test", sonarr_api_key="key"))
        held = _entry("held", "Held Show", "2026-07-15T20:00:00Z")
        held["show"]["ids"]["tvdb"] = 555
        missing = _entry("missing", "Missing Show", "2026-07-15T21:00:00Z")
        missing["show"]["ids"]["tvdb"] = 777
        # No TVDB id at all, which Sonarr matches on.
        unnumbered = _entry("unnumbered", "Unnumbered Show", "2026-07-15T22:00:00Z")
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([held, missing, unnumbered]))
        patcher.start()
        self.addCleanup(patcher.stop)

        cache = dict(integrations_routes.LIBRARY_CACHE)
        health = dict(integrations_routes.INTEGRATION_HEALTH)
        seen = {kind: dict(ids) for kind, ids in integrations_routes._seen_at.items()}
        self.addCleanup(self._restore, cache, health, seen)
        integrations_routes.LIBRARY_CACHE.update({"sonarr": [555], "_ts": time.time()})
        integrations_routes.INTEGRATION_HEALTH["sonarr"] = {"configured": True, "reachable": True}

        self.admin = self._make_user("libraryadmin", is_admin=True)
        self.viewer = self._make_user("plainviewer")

    @staticmethod
    def _restore(cache, health, seen):
        integrations_routes.LIBRARY_CACHE.clear()
        integrations_routes.LIBRARY_CACHE.update(cache)
        integrations_routes.INTEGRATION_HEALTH.clear()
        integrations_routes.INTEGRATION_HEALTH.update(health)
        for kind, ids in seen.items():
            integrations_routes._seen_at[kind].clear()
            integrations_routes._seen_at[kind].update(ids)

    def _button(self, html: str, library_id: str) -> tuple[str, str]:
        """(its classes, the whole opening tag) for the Sonarr button of a title."""
        found = re.search(r'<button type="button" class="(arr-btn sonarr[^"]*)"[^>]*'
                          r'data-library-id="%s"[^>]*>' % library_id, html)
        self.assertIsNotNone(found, f"no Sonarr button for {library_id}")
        return found.group(1), found.group(0)


class TheCardDrawsItsButtonsInTheirStateTests(_LibraryTestCase):

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin)

    def test_a_title_already_in_the_library_arrives_marked(self):
        classes, tag = self._button(self.client.get(self.PAGE).text, "555")
        self.assertIn("in-library", classes)
        self.assertIn('title="Already in Sonarr"', tag)
        self.assertNotIn(" disabled", tag)

    def test_a_title_not_in_it_arrives_offering_the_add(self):
        classes, tag = self._button(self.client.get(self.PAGE).text, "777")
        self.assertNotIn("in-library", classes)
        self.assertIn('title="Add to Sonarr"', tag)
        self.assertIn('data-tvdb="777"', tag)

    def test_a_service_that_cannot_be_reached_arrives_disabled(self):
        integrations_routes.INTEGRATION_HEALTH["sonarr"] = {"configured": True, "reachable": False}
        classes, tag = self._button(self.client.get(self.PAGE).text, "777")
        self.assertIn("unreachable", classes)
        self.assertIn(" disabled", tag)
        self.assertIn('title="Sonarr is unreachable"', tag)

    def test_a_title_the_service_has_no_id_for_arrives_disabled_and_says_why(self):
        """Sonarr matches on TVDB. A show without that id drew an ordinary Add that
        could only fail, and the refusal arrived as a toast after the press."""
        found = re.search(r'<button type="button" class="arr-btn sonarr[^"]*"[^>]*'
                          r'data-library-id=""[^>]*>', self.client.get(self.PAGE).text)
        self.assertIsNotNone(found, "the id-less show drew no Sonarr button")
        self.assertIn(" disabled", found.group(0))
        self.assertIn('title="No TVDB id, so Sonarr can\'t add this"', found.group(0))

    def test_a_day_that_arrives_late_draws_the_same_buttons(self):
        """The case the poll missed: nothing had visited a late day's buttons, so
        a title already in the library showed a plain Add until the next sweep."""
        classes, _ = self._button(self.client.get(self.DAY).text, "555")
        self.assertIn("in-library", classes)

    def test_the_panel_address_carries_the_ids_the_panels_buttons_post(self):
        html = self.client.get(self.PAGE).text
        self.assertRegex(html, r'data-details-open="[^"]*tvdb=555')

    def test_nothing_on_the_page_polls_for_the_buttons_state(self):
        script = (assets.BASE_DIR / "static/js/calendar/arr-buttons.js").read_text(encoding="utf-8")
        for polled in ("/api/integrations/status", "/api/integrations/library", "setInterval"):
            with self.subTest(polled=polled):
                self.assertNotIn(polled, script)


class OnlyAnAdministratorGetsButtonsTests(_LibraryTestCase):

    def test_anybody_else_gets_no_buttons_and_no_library_ids_on_the_address(self):
        """Writes to Sonarr land in the operator's own library, so the buttons are
        left out of everybody else's page rather than drawn into a refusal — and
        the ids only those buttons post are left off everybody else's cards."""
        self.sign_in_as(self.viewer)
        html = self.client.get(self.PAGE).text
        self.assertNotIn("arr-btn", html)
        self.assertNotIn("tvdb=", html)


class ThePanelHeaderCarriesTheSameButtonsTests(_LibraryTestCase):
    """The details panel's add buttons are drawn by the same macro from the same
    caches, and ride out-of-band on the panel's own answer."""

    URL = "/calendar/fragments/details?media=show&trakt=1&season=1&title=Held+Show&tvdb=555"

    def _panel(self) -> str:
        chosen = SimpleNamespace(source=Source.TRAKT, source_id="1")
        with patch("app.providers.detail_source.choose", return_value=chosen), \
             patch("app.providers.detail_source.describe",
                   AsyncMock(return_value={"title": "Held Show", "overview": "A show."})):
            return self.client.get(self.URL).text

    def test_an_administrators_panel_fills_the_header_out_of_band(self):
        self.sign_in_as(self.admin)
        slot = re.search(r'<div id="detailsActions" hx-swap-oob="innerHTML">(.*?)</div>',
                         self._panel(), re.S)
        self.assertIsNotNone(slot, "the panel sent nothing for its header")
        classes, tag = self._button(slot.group(1), "555")
        self.assertIn("in-library", classes)
        self.assertIn("Add to Sonarr", slot.group(1))

    def test_anybody_elses_panel_sends_nothing_for_the_header(self):
        self.sign_in_as(self.viewer)
        self.assertNotIn("detailsActions", self._panel())


class ThePanelDrawsTheCardsDescriptionTests(ThePanelHeaderCarriesTheSameButtonsTests):
    """A card whose services wrote different descriptions sends the one on screen,
    flipped or not, and the panel draws it — the poster's rule."""

    def test_the_cards_description_wins_over_the_answers(self):
        self.sign_in_as(self.viewer)
        self.URL = ThePanelHeaderCarriesTheSameButtonsTests.URL + "&overview=The+words+on+the+card."
        panel = self._panel()
        self.assertIn("The words on the card.", panel)
        self.assertNotIn("A show.", panel)

    def test_without_one_the_answers_description_is_drawn(self):
        self.sign_in_as(self.viewer)
        self.assertIn("A show.", self._panel())

    test_an_administrators_panel_fills_the_header_out_of_band = None
    test_anybody_elses_panel_sends_nothing_for_the_header = None


class AddAllAddsTheWholeMonthTests(_LibraryTestCase):
    """The calendar's Add all: the month the page shows, not the cards it has
    drawn — this viewer's filters applied, not-watching titles left out, one add
    per title, and nothing already held."""

    URL = "/calendar/library/add-month?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin)
        self.added = []

        async def _add(target, settings, ids, title):
            self.added.append((target, str(ids.get("tvdb")), title))
            return {"ok": True, "message": "Added"}

        patcher = patch("app.integrations.arr.add_media", _add)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _month(self, *extra):
        held = _entry("held", "Held Show", "2026-07-15T20:00:00Z")
        held["show"]["ids"]["tvdb"] = 555
        missing = _entry("missing", "Missing Show", "2026-07-15T21:00:00Z")
        missing["show"]["ids"]["tvdb"] = 777
        return patch("app.calendar.cache.fetch_window_records",
                     window_fetch([held, missing, *extra]))

    def test_a_dry_run_counts_what_is_not_held_and_adds_nothing(self):
        body = self.client.post(self.URL, json={"dry_run": True}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["count"], 1)
        self.assertIn("1 already there", body["message"])
        self.assertEqual(self.added, [])

    def test_a_title_airing_on_several_days_is_added_once(self):
        again = _entry("missing", "Missing Show", "2026-07-22T21:00:00Z")
        again["show"]["ids"]["tvdb"] = 777
        with self._month(again):
            body = self.client.post(self.URL, json={}).json()
        self.assertEqual(self.added, [("sonarr", "777", "Missing Show")])
        self.assertEqual(body["added"], ["777"])

    def test_a_day_the_page_never_loaded_is_still_added(self):
        """Every day past the first few is a placeholder until scrolled to; the
        month is read whole regardless."""
        late = _entry("late", "Late Show", "2026-07-30T21:00:00Z")
        late["show"]["ids"]["tvdb"] = 888
        with self._month(late):
            self.client.post(self.URL, json={})
        self.assertIn(("sonarr", "888", "Late Show"), self.added)

    def test_a_not_watching_title_is_left_out(self):
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show:tvdb:777", "not_watching": True})
        body = self.client.post(self.URL, json={"dry_run": True}).json()
        self.assertEqual(body["count"], 0)

    def test_a_title_this_viewers_filters_remove_is_left_out(self):
        filtered = _entry("dramatic", "Dramatic Show", "2026-07-16T21:00:00Z")
        filtered["show"]["ids"]["tvdb"] = 999
        filtered["show"]["genres"] = ["drama"]
        self.client.post("/api/me/filters", json={vocab.field_name(vocab.TV_GENRES, "drama"): "exclude"})
        with self._month(filtered):
            self.client.post(self.URL, json={})
        self.assertNotIn("999", [tvdb for _, tvdb, _ in self.added])

    def test_an_unreachable_service_adds_nothing_and_says_so(self):
        integrations_routes.INTEGRATION_HEALTH["sonarr"] = {"configured": True, "reachable": False}
        resp = self.client.post(self.URL, json={})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("can't be reached", resp.json()["error"])
        self.assertEqual(self.added, [])

    def test_only_an_administrator_may(self):
        self.sign_in_as(self.viewer)
        self.assertEqual(self.client.post(self.URL, json={}).status_code, 403)
        self.assertEqual(self.added, [])


class TheAddRouteRemembersUnderTheIdTheButtonIsMarkedByTests(_LibraryTestCase):

    def test_a_successful_add_is_drawn_held_on_the_next_page(self):
        self.sign_in_as(self.admin)
        with patch("app.integrations.arr.add_media",
                   AsyncMock(return_value={"ok": True, "message": "Added"})):
            resp = self.client.post("/api/integrations/add", json={
                "target": "sonarr", "media": "show", "tvdb": "777", "tmdb": "999",
                "title": "Missing Show"})
        self.assertEqual(resp.status_code, 200, resp.text)
        classes, _ = self._button(self.client.get(self.PAGE).text, "777")
        self.assertIn("in-library", classes)
