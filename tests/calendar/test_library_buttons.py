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

import asyncio
import re
import time
from urllib.parse import quote
from unittest.mock import AsyncMock, patch

from app import assets, db
from app.calendar import vocab
from app.config import Settings, load_settings, save_settings
from app.integrations import routes as integrations_routes
from app.providers import detail_source
from app.providers.base import Source

from tests.calendar.test_routes import CalendarRouteTestCase, _entry
from tests.support import window_fetch

# What one redrawn button says, spelled once because both adds answer with it and
# because the selector's shape is load-bearing: htmx splits an out-of-band value
# at the first colon ONLY when it contains no space, so a stray one would silently
# turn this into an ordinary swap.
ADDED_BUTTON_OOB = ("hx-swap-oob=\"outerHTML:.arr-btn[data-arr='sonarr']"
                    "[data-library-id='777']\"")
SEND_COMMIT = "hx-vals='{\"commit\": true}'"


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

    def test_the_button_posts_the_card_and_not_the_title_s_ids(self):
        """A write addressed by an attribute on the page is a write somebody can
        aim somewhere else. The button names the CARD — its shared identity and
        which service — and the route reads the ids off the rows it drew that
        card from."""
        _, tag = self._button(self.client.get(self.PAGE).text, "777")
        self.assertIn('hx-post="/calendar/library/add"', tag)
        self.assertIn("hx-json-enc", tag)
        self.assertIn('"key": "show:tvdb:777"', tag)
        self.assertIn('"target": "sonarr"', tag)
        # Nothing a write depends on beyond those two.
        self.assertNotIn("data-tvdb", tag)
        self.assertNotIn("data-tmdb", tag)
        self.assertNotIn("data-title", tag)

    def test_a_button_that_cannot_add_offers_no_request(self):
        """A disabled button posting nothing is the same statement twice; what
        matters is that the address is simply not on it."""
        for library_id in ("555", ""):
            found = re.search(r'<button type="button" class="arr-btn sonarr[^"]*"[^>]*'
                              r'data-library-id="%s"[^>]*>' % library_id,
                              self.client.get(self.PAGE).text)
            with self.subTest(library_id=library_id or "none"):
                self.assertIsNotNone(found)
                self.assertNotIn("hx-post", found.group(0))

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

    def test_the_calendar_carries_no_add_script_at_all(self):
        """The poll went when the server started drawing each button's state; the
        press went when the button became an `hx-post`. What is left is markup, so
        there is no file for either to come back into — in the calendar's scripts
        or anywhere else, since every page loads all of them."""
        self.assertFalse((assets.BASE_DIR / "static/js/calendar/arr-buttons.js").exists())
        for name in assets.APP_SCRIPTS:
            with self.subTest(script=name):
                source = (assets.BASE_DIR / name).read_text(encoding="utf-8")
                self.assertNotIn("/calendar/library/", source)
                self.assertNotIn("integrations/add", source)


class EveryServiceAddsThroughTheOneRouteTests(_LibraryTestCase):
    """Sonarr, Radarr and Seerr are ONE button drawn by ONE macro, so all three
    post the same thing to the same place and none of them has a script.

    WHY THIS IS ASSERTED ACROSS ALL THREE rather than left to the Sonarr tests
    above: Seerr is the service a calendar card draws SECOND, beside whichever of
    Sonarr and Radarr the medium calls for, and it is the one whose add takes a
    different code path inside `add_title`. A card that quietly kept an older
    button for it would look identical on the page.
    """

    # Sonarr matches a title on its TVDB id and Seerr on its TMDB one, so a title
    # both will take needs both — which is also why a button is addressed by
    # `data-arr` as well as by the id (a film's Radarr and Seerr buttons share a
    # TMDB id and are not the same button).
    BOTH_IDS = {"tvdb": 777, "tmdb": 4242}

    def setUp(self):
        super().setUp()
        save_settings(Settings(trakt_client_id="test-client-id",
                               trakt_access_token="test-access-token",
                               sonarr_url="http://sonarr.test", sonarr_api_key="key",
                               radarr_url="http://radarr.test", radarr_api_key="key",
                               seer_url="http://seer.test", seer_api_key="key"))
        for kind in ("sonarr", "radarr", "seer"):
            integrations_routes.INTEGRATION_HEALTH[kind] = {"configured": True,
                                                            "reachable": True}
        both = _entry("both", "Both Show", "2026-07-15T21:00:00Z")
        both["show"]["ids"].update(self.BOTH_IDS)
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([both]))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sign_in_as(self.admin)
        # The month has to have been read once for its rows to be stored — the add
        # reads the ids back off them rather than taking them from the page.
        self.page = self.client.get(self.PAGE).text
        self.key = "show:tmdb:4242"

    def test_a_show_draws_sonarr_and_seerr_and_both_post_the_same_way(self):
        for kind, library_id in (("sonarr", 777), ("seer", 4242)):
            with self.subTest(service=kind):
                found = re.search(
                    r'<button type="button" class="arr-btn %s[^"]*"[^>]*'
                    r'data-library-id="%s"[^>]*>' % (kind, library_id), self.page)
                self.assertIsNotNone(found, f"no {kind} button for an addable title")
                tag = found.group(0)
                self.assertIn('hx-post="/calendar/library/add"', tag)
                self.assertIn("hx-json-enc", tag)
                self.assertIn(f'"key": "{self.key}"', tag)
                self.assertIn(f'"target": "{kind}"', tag)
                self.assertNotIn("onclick", tag)

    def test_the_route_takes_each_of_the_three_by_name(self):
        """`add_title` sends Seerr down its own path, so the route has to admit it
        by name — and refuse anything that is not one of the three."""
        seen = []

        async def _arr(target, settings, ids, title):
            seen.append(("arr", target, str(ids.get("tvdb") or ids.get("tmdb"))))
            return {"ok": True, "message": "Added"}

        async def _seer(settings, media, tmdb, title):
            seen.append(("seer", media, str(tmdb)))
            return {"ok": True, "message": "Requested"}

        with patch("app.integrations.arr.add_media", _arr), \
             patch("app.integrations.seer.add_media", _seer):
            for target in ("sonarr", "seer"):
                self.client.post("/calendar/library/add",
                                 json={"key": self.key, "target": target})
        self.assertEqual(seen, [("arr", "sonarr", "777"), ("seer", "show", "4242")])

    def test_the_medium_picks_one_of_the_two_and_seerr_rides_beside_either(self):
        """Asked of the one function that decides, rather than of a rendered film
        calendar: which of Sonarr and Radarr a title gets is `LibraryView.buttons`'
        whole job, and the template draws whatever it is handed."""
        view = integrations_routes.LibraryView(load_settings())
        self.assertEqual([b.kind for b in view.buttons("show", {"tvdb": "1", "tmdb": "2"})],
                         ["sonarr", "seer"])
        self.assertEqual([b.kind for b in view.buttons("movie", {"tvdb": "1", "tmdb": "2"})],
                         ["radarr", "seer"])


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

    URL = ("/calendar/fragments/details?media=show&trakt=1&season=1"
           "&key=show%3Atvdb%3A555&tvdb=555")
    # The same panel for a title that is NOT held, which is the only one whose
    # button carries an add at all — a held one draws no request, so a panel
    # tested on one alone never renders the half that posts.
    ADDABLE = ("/calendar/fragments/details?media=show&trakt=1&season=1"
               "&key=show%3Atvdb%3A777&tvdb=777")

    def _panel(self) -> str:
        chosen = detail_source.Answerer(Source.TRAKT, "1", stored_only=False)
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

    def test_the_panels_button_posts_the_same_card_the_cards_own_does(self):
        """An add made from the panel and one made from the card behind it are the
        same act, so they name the same card — and the answer to either marks both,
        because a redrawn button is addressed by the id the service matches on."""
        self.sign_in_as(self.admin)
        self.URL = self.ADDABLE
        slot = re.search(r'<div id="detailsActions" hx-swap-oob="innerHTML">(.*?)</div>',
                         self._panel(), re.S)
        self.assertIsNotNone(slot, "the panel sent nothing for its header")
        _, tag = self._button(slot.group(1), "777")
        self.assertIn('hx-post="/calendar/library/add"', tag)
        self.assertIn('"key": "show:tvdb:777"', tag)

    def test_anybody_elses_panel_sends_nothing_for_the_header(self):
        self.sign_in_as(self.viewer)
        self.assertNotIn("detailsActions", self._panel())


class ThePanelDrawsWhatTheCardIsShowingTests(ThePanelHeaderCarriesTheSameButtonsTests):
    """A card shows the name, picture and description this viewer's field order
    picked, and the panel has to open on those rather than on whatever the
    describing service says.

    THE ADDRESS NAMES A SERVICE AND NOT THE VALUE. `?overview=simkl` is read back
    out of the row the card was drawn from, so nothing a caller WROTE is rendered
    into a page served from the owner's origin, and a card whose services
    disagreed ships one word where it used to ship a description.
    """

    test_an_administrators_panel_fills_the_header_out_of_band = None
    test_anybody_elses_panel_sends_nothing_for_the_header = None

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.viewer)
        # Two services' rows for one title, describing it differently. The card's
        # key is what both are filed under.
        self.key = "show:tvdb:555"
        for source, overview, title in (("trakt", "Trakt words.", "Trakt Name"),
                                        ("simkl", "Simkl words.", "Simkl Name")):
            asyncio.run(db.execute(
                "INSERT INTO calendar_titles (source, media, source_id, title_key, title,"
                " title_fold, overview, poster, fetched_at) VALUES (?,?,?,?,?,?,?,?,0)",
                (source, "show", source + "-1", self.key, title, title.lower(),
                 overview, "/" + source + ".jpg")))

    def _panel_for(self, **params) -> str:
        # Built from scratch rather than appended to the parent's address: a
        # second `key` would be ignored in favour of the first, and the case that
        # names a title nothing holds would then be reading the held one.
        key = params.pop("key", self.key)
        self.URL = ("/calendar/fragments/details?media=show&trakt=1&season=1"
                    + "&key=" + quote(key)
                    + "".join("&%s=%s" % pair for pair in params.items()))
        return self._panel()

    def test_the_named_services_words_are_drawn_and_not_the_answers(self):
        panel = self._panel_for(overview="simkl")
        self.assertIn("Simkl words.", panel)
        self.assertNotIn("A show.", panel)

    def test_flipping_the_name_flips_which_row_is_read(self):
        """What a card's flip overrides is one short parameter, so the panel
        follows a flip without a description ever travelling."""
        self.assertIn("Trakt words.", self._panel_for(overview="trakt"))
        self.assertIn("Simkl words.", self._panel_for(overview="simkl"))

    def test_the_three_fields_are_read_together(self):
        panel = self._panel_for(title="simkl", poster="trakt", overview="simkl")
        self.assertIn("Simkl words.", panel)
        # THROUGH THE SHARED IMAGE PROXY, not the stored column's raw address —
        # this route reads calendar_titles.poster directly (stored_values, not
        # render()) and has to proxy it itself before handing it to an <img src>,
        # the same way render() already does for the calendar CARD's own poster.
        self.assertIn('src="https://wsrv.nl/?url=%2Ftrakt.jpg&amp;q=90&amp;w=440&amp;we"', panel)
        self.assertIn('alt="Simkl Name poster"', panel)

    def test_no_parameter_draws_the_answers_own(self):
        self.assertIn("A show.", self._panel_for())

    def test_a_service_with_no_row_for_this_title_draws_the_answers_own(self):
        self.assertIn("A show.",
                      self._panel_for(key="show:tvdb:nothing", overview="simkl"))

    def test_a_name_this_app_has_not_registered_is_dropped_rather_than_rendered(self):
        """A name is a closed set, so an address a stranger spelled cannot put its
        own text into the owner's page — the share side's rule, finally holding on
        the signed-in panel too."""
        panel = self._panel_for(overview="%3Cb%3Epwned%3C%2Fb%3E")
        self.assertNotIn("pwned", panel)
        self.assertIn("A show.", panel)


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

    def test_the_first_press_counts_adds_nothing_and_draws_the_confirmation(self):
        """Both steps answer MARKUP: the sentence and the button that commits,
        drawn where the Add all button was, so nothing in the browser composes a
        count or knows what a confirmation looks like."""
        text = self.client.post(self.URL, json={}).text
        self.assertIn("Add 1 watching title from this month to Sonarr?", text)
        self.assertIn("1 already there", text)
        self.assertIn(SEND_COMMIT, text)
        self.assertEqual(self.added, [])

    def test_cancel_asks_for_the_button_back(self):
        """The confirmation replaced the control, so the way back is to draw it —
        a read that changes nothing, at a GET like every other fragment here."""
        text = self.client.post(self.URL, json={}).text
        self.assertIn('hx-get="/calendar/fragments/add-all', text)
        again = self.client.get(
            "/calendar/fragments/add-all?year=2026&month=7&endpoint=shows").text
        self.assertIn('id="addAllBtn"', again)
        self.assertNotIn("commit", again)

    def test_a_title_airing_on_several_days_is_added_once(self):
        again = _entry("missing", "Missing Show", "2026-07-22T21:00:00Z")
        again["show"]["ids"]["tvdb"] = 777
        with self._month(again):
            text = self.client.post(self.URL, json={"commit": True}).text
        self.assertEqual(self.added, [("sonarr", "777", "Missing Show")])
        self.assertIn("Added 1 to Sonarr.", text)

    def test_what_was_added_comes_back_as_a_redrawn_button_per_title(self):
        """One response marks every button on the page for those titles, wherever
        they were drawn — addressed by the id the service matches on."""
        text = self.client.post(self.URL, json={"commit": True}).text
        self.assertIn(ADDED_BUTTON_OOB, text)
        self.assertIn("in-library", text)
        # And the control comes back, so there is something to press again.
        self.assertIn('id="addAllBtn"', text)

    def test_a_day_the_page_never_loaded_is_still_added(self):
        """Every day past the first few is a placeholder until scrolled to; the
        month is read whole regardless."""
        late = _entry("late", "Late Show", "2026-07-30T21:00:00Z")
        late["show"]["ids"]["tvdb"] = 888
        with self._month(late):
            self.client.post(self.URL, json={"commit": True})
        self.assertIn(("sonarr", "888", "Late Show"), self.added)

    def test_a_not_watching_title_is_left_out(self):
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show:tvdb:777", "not_watching": True})
        text = self.client.post(self.URL, json={}).text
        self.assertIn("Nothing to add", text)
        self.assertEqual(self.added, [])

    def test_a_title_this_viewers_filters_remove_is_left_out(self):
        filtered = _entry("dramatic", "Dramatic Show", "2026-07-16T21:00:00Z")
        filtered["show"]["ids"]["tvdb"] = 999
        filtered["show"]["genres"] = ["drama"]
        self.client.post("/calendar/filters",
                         json={vocab.field_name(vocab.TV_GENRES, "drama"): "exclude"})
        with self._month(filtered):
            self.client.post(self.URL, json={"commit": True})
        self.assertNotIn("999", [tvdb for _, tvdb, _ in self.added])

    def test_an_unreachable_service_adds_nothing_and_puts_the_control_back(self):
        """A refusal that was only a sentence would leave the page holding neither
        the button nor the confirmation, since the answer replaces what was
        pressed."""
        integrations_routes.INTEGRATION_HEALTH["sonarr"] = {"configured": True,
                                                            "reachable": False}
        text = self.client.post(self.URL, json={"commit": True}).text
        self.assertIn("can&#39;t be reached", text)
        self.assertIn('id="addAllBtn"', text)
        self.assertEqual(self.added, [])

    def test_only_an_administrator_may(self):
        self.sign_in_as(self.viewer)
        for body in ({}, {"commit": True}):
            self.assertEqual(self.client.post(self.URL, json=body).status_code, 403)
        self.assertEqual(self.client.get(
            "/calendar/fragments/add-all?year=2026&month=7").status_code, 403)
        self.assertEqual(self.added, [])


class TheSingleAddNamesTheCardTests(_LibraryTestCase):
    """The add posts the card's shared identity and which service. The ids come
    off the rows this instance drew that card from, so an id edited into the page
    cannot reach a write into the operator's own library."""

    URL = "/calendar/library/add"

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
        # The month has to have been read once for its rows to be stored.
        self.client.get(self.PAGE)

    def test_it_reads_the_ids_off_the_row_the_card_came_from(self):
        text = self.client.post(self.URL, json={"key": "show:tvdb:777",
                                                "target": "sonarr"}).text
        self.assertEqual(self.added, [("sonarr", "777", "Missing Show")])
        self.assertIn("Added", text)

    def test_the_answer_redraws_every_button_for_that_title(self):
        text = self.client.post(self.URL, json={"key": "show:tvdb:777",
                                                "target": "sonarr"}).text
        self.assertIn(ADDED_BUTTON_OOB, text)
        self.assertIn("in-library", text)
        # And the line saying so rides the same answer, into the shell's host.
        self.assertIn('<div id="toastHost" hx-swap-oob="beforeend">', text)

    def test_a_successful_add_is_drawn_held_on_the_next_page(self):
        self.client.post(self.URL, json={"key": "show:tvdb:777", "target": "sonarr"})
        classes, _ = self._button(self.client.get(self.PAGE).text, "777")
        self.assertIn("in-library", classes)

    def test_a_key_this_calendar_does_not_hold_adds_nothing(self):
        text = self.client.post(self.URL, json={"key": "show:tvdb:000000",
                                                "target": "sonarr"}).text
        self.assertEqual(self.added, [])
        self.assertIn("no longer holds that title", text)

    def test_an_unknown_service_adds_nothing(self):
        text = self.client.post(self.URL, json={"key": "show:tvdb:777",
                                                "target": "plex"}).text
        self.assertEqual(self.added, [])
        self.assertIn("Unknown service", text)

    def test_only_an_administrator_may(self):
        self.sign_in_as(self.viewer)
        self.assertEqual(self.client.post(
            self.URL, json={"key": "show:tvdb:777", "target": "sonarr"}).status_code, 403)
        self.assertEqual(self.added, [])
