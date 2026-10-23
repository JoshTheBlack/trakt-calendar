"""Gating + wiring tests for the main calendar route (app/calendar/routes.py's `/` and
`/api/state`, `/api/me/prefs`, `/api/me/timezone`).

Covers: two signed-in users reading the same month see the same cached shows
with fully independent not-watching overlays; the `/api/state` delta endpoint
is idempotent and does not lose one tab's mark to another's; a non-admin's
card-style choice persists across separate requests through `user_prefs`
instead of settings.json; and the timezone picker persists to `users.timezone`
and changes which month a boundary item renders under.

No network — the Trakt window fetch is patched at app.calendar.cache's own
module boundary, the same way tests/calendar/test_cache.py does it, so the
real per-viewer normalize/trim logic in calendar_cache.read_month runs for
real.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
import dataclasses
import json
import re
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from app import auth, db, providers
from app.calendar import cache as calendar_cache, routes as calendar_routes
from app.calendar import state as calendar_state
from app.calendar import vocab
from app.endpoints import ENDPOINTS
from app.providers.base import Capabilities
from app.providers.trakt import TraktError
from app.config import Settings, save_settings
from app.sources import prefs as source_prefs
from app.main import app
from tests.support import ORIGIN, migrated_db, window_fetch


def _configured_settings() -> Settings:
    """`settings.calendar_source_configured` gates the whole read path in index() — without
    credentials it never calls calendar_cache.read_month at all."""
    return Settings(trakt_client_id="test-client-id", trakt_access_token="test-access-token")


def _entry(slug: str, title: str, first_aired: str) -> dict:
    """A raw (pruned-shape) calendar entry, mid-month and inside the default
    country allowlist, so it survives the default read-time filter untouched."""
    return {
        "first_aired": first_aired,
        "episode": {"season": 1, "number": 1, "title": f"{title} pilot"},
        "show": {
            "title": title, "country": "us", "genres": [],
            "ids": {"slug": slug, "trakt": abs(hash(slug)) % 100000},
        },
    }


class _ThirdSource:
    """A service the app does not have, for the tests that check a rule was
    DERIVED rather than written down for the two that exist. It is deliberately
    not a `Source` member: the point is that nothing in the code under test may
    reach for one by name."""

    source = "mercury"
    label = "Mercury"
    sync_port = None
    calendar_port = object()
    capabilities = Capabilities(
        endpoints=frozenset({"shows"}), days_before=None, days_after=None,
        private_user_data=False)

    def is_configured(self, settings) -> bool:
        return True


def _third_source() -> _ThirdSource:
    return _ThirdSource()


class CalendarRouteTestCase(unittest.TestCase):
    def setUp(self):
        migrated_db("calroute")
        save_settings(_configured_settings())
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    def serving_month(self, entries=()):
        """Hold the calendar fetch still for the life of the test.

        NEEDED BY EVERY TEST THAT SAVES A FILTER, which is new: the save answers
        with the month redrawn rather than telling the browser to reload, so it
        assembles a month the way the page does. A subclass that already patches
        the fetch for its own entries does not call this.
        """
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch(list(entries)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_user(self, username: str, **flags) -> int:
        flags.setdefault("calendar_approved", True)
        return asyncio.run(auth.create_user(
            username=username, password="hunter2hunter2", settings=_configured_settings(), **flags))

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)


# ---------------------------------------------------------------------------
# shared cache, independent overlays
# ---------------------------------------------------------------------------

class SharedCalendarIndependentOverlayTests(CalendarRouteTestCase):
    def setUp(self):
        super().setUp()
        self.user1 = self._make_user("viewer_one")
        self.user2 = self._make_user("viewer_two")
        entries = [
            _entry("show-a", "Show A", "2026-07-15T20:00:00Z"),
            _entry("show-b", "Show B", "2026-07-16T20:00:00Z"),
        ]
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch(entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_users_see_the_same_shows(self):
        for user_id in (self.user1, self.user2):
            self.sign_in_as(user_id)
            resp = self.client.get("/?year=2026&month=7")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Show A", resp.text)
            self.assertIn("Show B", resp.text)

    def test_not_watching_marks_are_independent_per_viewer(self):
        self.sign_in_as(self.user1)
        resp = self.client.post(
            "/api/state?year=2026&month=7",
            json={"item_id": "show-a", "not_watching": True},
        )
        self.assertEqual(resp.status_code, 200, resp.text)

        state = self.client.get("/api/state?year=2026&month=7").json()
        self.assertEqual(state["notWatching"], ["show-a"])

        # A second viewer, same month, same endpoint: their own state is empty —
        # the mark did not leak across accounts.
        self.sign_in_as(self.user2)
        state2 = self.client.get("/api/state?year=2026&month=7").json()
        self.assertEqual(state2["notWatching"], [])

        # And the first viewer's mark is still there, unaffected by the second
        # viewer's request.
        self.sign_in_as(self.user1)
        state1_again = self.client.get("/api/state?year=2026&month=7").json()
        self.assertEqual(state1_again["notWatching"], ["show-a"])


# ---------------------------------------------------------------------------
# the delta endpoint: idempotent, no lost update between two tabs
# ---------------------------------------------------------------------------

class DeltaStateEndpointTests(CalendarRouteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("delta_viewer")
        self.sign_in_as(self.user_id)

    def _toggle(self, item_id: str, not_watching: bool):
        return self.client.post(
            "/api/state?year=2026&month=7",
            json={"item_id": item_id, "not_watching": not_watching},
        )

    def test_marking_the_same_item_twice_is_idempotent(self):
        self._toggle("show-a", True)
        self._toggle("show-a", True)
        rows = asyncio.run(db.fetch_all(
            "SELECT item_id FROM not_watching_shows WHERE user_id = ?", (self.user_id,)))
        self.assertEqual([r["item_id"] for r in rows], ["show-a"])

    def test_a_mark_made_in_one_view_shows_up_in_every_other(self):
        """Not-watching is a fact about the show, so marking a series premiere
        also hides it under All Episodes and in every other month."""
        self._toggle("show-a", True)
        for query in ("year=2026&month=7&endpoint=shows",
                      "year=2027&month=1&endpoint=shows/premieres"):
            state = self.client.get(f"/api/state?{query}").json()
            self.assertEqual(state["notWatching"], ["show-a"], query)

    def test_two_tabs_toggling_different_items_do_not_lose_either_mark(self):
        """The old whole-array save was a read-modify-write of one shared
        document: a second tab's save, built from a stale read, would silently
        drop whatever the first tab had just added. A delta can't do that —
        each toggle is its own INSERT/DELETE against one item_id."""
        self._toggle("show-a", True)   # "tab A"
        self._toggle("show-b", True)   # "tab B", with no knowledge of tab A's write
        state = self.client.get("/api/state?year=2026&month=7").json()
        self.assertEqual(set(state["notWatching"]), {"show-a", "show-b"})

    def test_toggling_off_removes_only_that_item(self):
        self._toggle("show-a", True)
        self._toggle("show-b", True)
        self._toggle("show-a", False)
        state = self.client.get("/api/state?year=2026&month=7").json()
        self.assertEqual(state["notWatching"], ["show-b"])

    def test_view_state_write_is_a_separate_payload_shape(self):
        resp = self.client.post(
            "/api/state?year=2026&month=7",
            json={"last_count": 7, "last_show_ids": ["show-a", "show-b"]},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        state = self.client.get("/api/state?year=2026&month=7").json()
        self.assertEqual(state["lastCount"], 7)
        self.assertEqual(state["lastShowIds"], ["show-a", "show-b"])

    def test_missing_item_id_and_missing_view_fields_is_a_400(self):
        resp = self.client.post("/api/state?year=2026&month=7", json={"unrelated": True})
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# per-user view preferences persist through user_prefs
# ---------------------------------------------------------------------------

def _body_class(html: str) -> str:
    m = re.search(r'<body[^>]*\bclass="([^"]*)"', html)
    return m.group(1) if m else ""


class ViewPrefsPersistenceTests(CalendarRouteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("plain_viewer", is_admin=False)
        self.sign_in_as(self.user_id)
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_non_admin_card_style_change_persists_across_requests(self):
        first = self.client.get("/?year=2026&month=7")
        self.assertIn("card-vertical", _body_class(first.text))  # the default

        resp = self.client.post("/api/me/prefs", json={"card_style": "poster"})
        self.assertEqual(resp.status_code, 200, resp.text)

        second = self.client.get("/?year=2026&month=7")
        self.assertIn("card-poster", _body_class(second.text))
        self.assertNotIn("card-vertical", _body_class(second.text))

        # And a THIRD, independent request still reflects it — not a one-load
        # client-side toggle, but a server-rendered read of user_prefs.
        third = self.client.get("/?year=2026&month=7")
        self.assertIn("card-poster", _body_class(third.text))

    def test_hide_not_watching_persists_through_user_prefs(self):
        resp = self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        page = self.client.get("/?year=2026&month=7")
        self.assertIn("hide-not-watching", _body_class(page.text))

        prefs = asyncio.run(auth.get_user_prefs(self.user_id))
        self.assertTrue(prefs["hide_not_watching"])

    def test_unrecognized_or_empty_update_is_rejected(self):
        resp = self.client.post("/api/me/prefs", json={"card_style": "not-a-real-style"})
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# the genre/country/network filters: per viewer, over one shared cache
# ---------------------------------------------------------------------------

class ViewerFilterTests(CalendarRouteTestCase):
    """These used to be editable only on the admin Settings screen, which wrote
    the app-wide SEED — so they changed nothing for the admin's own calendar and
    left every other account with no way to filter at all."""

    def setUp(self):
        super().setUp()
        self.user1 = self._make_user("filter_one")
        self.user2 = self._make_user("filter_two")
        drama = _entry("the-drama", "The Drama", "2026-07-15T20:00:00Z")
        drama["show"]["genres"] = ["drama"]
        drama["show"]["network"] = "HBO"
        drama["show"]["certification"] = "TV-14"
        comedy = _entry("the-comedy", "The Comedy", "2026-07-16T20:00:00Z")
        comedy["show"]["genres"] = ["comedy"]
        comedy["show"]["network"] = "Netflix"
        comedy["show"]["certification"] = "TV-MA"
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([drama, comedy]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_filters(self, **fields):
        """POST the panel the way the browser does: ONE FIELD PER TOKEN.

        The panel has no "genres" input to fill in any more — it renders a chip
        per token and each chip posts its own answer, which is what keeps the
        spec format entirely in app/calendar/filter.py. Spelling the names
        through `vocab.field_name` rather than by hand is the same discipline: a
        test that hard-coded "chip:tv_genres:drama" would be a second statement
        of the encoding.
        """
        payload = {}
        for field, tokens in fields.items():
            for token, mode in tokens.items():
                payload[vocab.field_name(field, token)] = mode
        return self.client.post("/calendar/filters", json=payload)

    def test_a_new_account_starts_with_no_filters_at_all(self):
        """A filter removes shows without ever saying one exists, so it is not
        something an account inherits from the instance's configuration."""
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        for field in (*vocab.TV_FIELDS, *vocab.MOVIE_FIELDS):
            with self.subTest(field=field):
                self.assertFalse(prefs[field])
        self.assertFalse(prefs["filters_paused"])

    def test_the_release_filter_round_trips_and_keeps_only_numbers(self):
        """The release types are stored as the numbers the service publishes, so
        a chip can only ever offer one — but a token typed into the box beside
        them can be anything, and is dropped on the way IN rather than stored as
        a preference that could never act."""
        self.sign_in_as(self.user1)
        resp = self.set_filters(
            movie_release_countries={"us": "include", "br": "exclude"},
            movie_release_types={"3": "include", "theatrical": "include", "1": "exclude"})
        self.assertEqual(resp.status_code, 200, resp.text)
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["movie_release_countries"], "us, -br")
        self.assertEqual(prefs["movie_release_types"], "3, -1")

    def test_a_release_filter_does_not_narrow_a_show_calendar(self):
        """It is a films-only dimension, and the specs travel with every read —
        so the show calendars have to be untouched rather than merely
        unaffected by accident."""
        self.sign_in_as(self.user1)
        self.set_filters(movie_release_countries={"zz": "include"},
                         movie_release_types={"5": "include"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page)
        self.assertIn("The Comedy", page)

    def test_a_viewer_can_filter_shows_by_certification(self):
        """A per-user certification exclude behaves exactly like the genre and
        country filters: one viewer's calendar narrows, the other's does not,
        from the same cached window."""
        self.sign_in_as(self.user1)
        resp = self.set_filters(show_certifications={"TV-MA": "exclude"})
        self.assertEqual(resp.status_code, 200, resp.text)
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page)
        self.assertNotIn("The Comedy", page)

        # The second viewer set nothing and still sees everything.
        self.sign_in_as(self.user2)
        page2 = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page2)
        self.assertIn("The Comedy", page2)

    def test_each_viewer_filters_the_same_cached_month_their_own_way(self):
        self.sign_in_as(self.user1)
        resp = self.set_filters(tv_genres={"comedy": "exclude"})
        self.assertEqual(resp.status_code, 200, resp.text)
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page)
        self.assertNotIn("The Comedy", page)

        # The second viewer set nothing and still sees everything — one cache,
        # two answers.
        self.sign_in_as(self.user2)
        page2 = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page2)
        self.assertIn("The Comedy", page2)

    def test_a_shows_genre_filter_leaves_the_film_calendar_alone(self):
        """THE WHOLE POINT OF THE SPLIT. Excluding a genre from the show
        calendar used to exclude it from films as well, which is why neither
        question could be answered properly."""
        self.sign_in_as(self.user1)
        self.set_filters(tv_genres={"comedy": "exclude"})
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["tv_genres"], "-comedy")
        self.assertEqual(prefs["movie_genres"], "")

    def test_saving_one_tab_leaves_the_other_tabs_answers_standing(self):
        """A tab posts every one of ITS chips, including the ones set to
        nothing, and mentions none of the other tab's — so clearing works and a
        tab nobody opened is left exactly as it was."""
        self.sign_in_as(self.user1)
        self.set_filters(movie_genres={"horror": "exclude"})
        self.set_filters(tv_genres={"comedy": "exclude"})
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["movie_genres"], "-horror")
        self.assertEqual(prefs["tv_genres"], "-comedy")

    def test_a_non_admin_can_read_and_write_their_own_filters(self):
        """The whole point: no admin rights involved."""
        self.sign_in_as(self.user1)
        resp = self.set_filters(
            tv_genres={"drama": "include"}, tv_countries={"us": "include"},
            network_filter={"HBO": "include", "Netflix": "exclude"})
        self.assertEqual(resp.status_code, 200, resp.text)

        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["tv_genres"], "drama")
        self.assertEqual(prefs["tv_countries"], "us")
        self.assertEqual(prefs["network_filter"], ["HBO", "-Netflix"])

    def test_two_spellings_of_a_network_are_two_networks(self):
        """`tvN` and `TVN` are a Korean broadcaster and a Polish one, and the read
        path matches them exactly for that reason. The de-duplication on the way
        IN folded case, so a viewer could set both and find only the first
        stored — the panel let them add two chips and the save quietly kept
        one."""
        self.sign_in_as(self.user1)
        self.set_filters(network_filter={"tvN": "include", "TVN": "exclude"})
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["network_filter"], ["tvN", "-TVN"])

    def test_the_same_spelling_twice_is_still_one_network(self):
        """Exact de-duplication, not none: a name repeated verbatim is one
        entry, which is what the check was there for before it started folding
        case as well."""
        self.sign_in_as(self.user1)
        asyncio.run(auth.update_user_prefs(
            self.user1, network_filter=calendar_routes._network_list("HBO, HBO , Netflix")))
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["network_filter"], ["HBO", "Netflix"])

    def test_the_network_filter_narrows_to_the_named_networks(self):
        self.sign_in_as(self.user1)
        self.set_filters(network_filter={"HBO": "include"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page)
        self.assertNotIn("The Comedy", page)

    def test_a_filter_can_be_cleared_again(self):
        """A chip set back to grey posts an EMPTY value rather than being left
        out, and that is what makes clearing possible: absent means "this tab
        was not open", empty means "stop filtering on this"."""
        self.sign_in_as(self.user1)
        self.set_filters(tv_genres={"comedy": "exclude"})
        self.set_filters(tv_genres={"comedy": ""})
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["tv_genres"], "")
        self.assertIn("The Comedy", self.client.get("/?year=2026&month=7").text)

    def test_the_switch_stops_a_filter_acting_without_forgetting_it(self):
        """The stash. Nothing is copied aside and nothing is restored — the
        answers stay where they are and the read path asks the switch first, so
        turning filters back on cannot have lost one."""
        self.sign_in_as(self.user1)
        self.set_filters(tv_genres={"comedy": "exclude"})
        self.assertNotIn("The Comedy", self.client.get("/?year=2026&month=7").text)

        self.client.post("/calendar/filters", json={"filters_on": "0"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Comedy", page)
        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user1))["tv_genres"],
                         "-comedy")

        self.client.post("/calendar/filters", json={"filters_on": "1"})
        self.assertNotIn("The Comedy", self.client.get("/?year=2026&month=7").text)

    def test_the_month_says_how_many_cards_the_filters_removed(self):
        """ON EVERY CALENDAR, not only the film one. The count started as a
        films-only number because the release rule is the one that can empty a
        month outright — but a genre exclude removes cards just as silently, and
        a month that is short with nothing on the page to say why reads as the
        app being broken whichever filter did it."""
        self.sign_in_as(self.user1)
        page = self.client.get("/?year=2026&month=7").text
        self.assertNotIn('id="statFiltered"', page)

        self.set_filters(tv_genres={"comedy": "exclude"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn('id="statFiltered"', page)
        self.assertIn(">1</strong>", page)

    def test_the_count_covers_the_network_filter_too(self):
        """The network filter runs later than the others — it needs the rendered
        item, which is the only shape a network exists on — so its removals are
        the ones most easily left out of a total assembled before it ran."""
        self.sign_in_as(self.user1)
        self.set_filters(network_filter={"HBO": "include"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn('id="statFiltered"', page)
        self.assertIn(">1</strong>", page)

    def test_the_header_button_says_when_a_filter_is_narrowing_the_month(self):
        """A filter's only other evidence is the shows that are not there, which
        looks exactly like the service not listing them."""
        self.sign_in_as(self.user1)
        unfiltered = self.client.get("/?year=2026&month=7").text
        self.assertIn('id="filtersBtn"', unfiltered)
        self.assertNotIn('class="pill-btn active"', unfiltered)

        self.set_filters(tv_genres={"comedy": "exclude"},
                         network_filter={"HBO": "include"})
        filtered = self.client.get("/?year=2026&month=7").text
        self.assertIn('class="pill-btn active"', filtered)
        self.assertIn("genre, network", filtered)

    def test_the_button_looks_different_again_while_the_filters_are_off(self):
        """THREE STATES, NOT TWO. "Nothing set" and "set but switched off" both
        show a calendar with everything on it, and if they looked alike a viewer
        would forget their filters existed."""
        self.sign_in_as(self.user1)
        self.set_filters(tv_genres={"comedy": "exclude"})
        self.client.post("/calendar/filters", json={"filters_on": "0"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("pill-btn paused", page)
        self.assertIn("Filters off", page)

    def test_the_filter_endpoints_still_need_a_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/calendar/fragments/filters").status_code, 401)
        self.assertEqual(
            self.client.post("/calendar/filters", json={"filters_on": "0"}).status_code,
            401)


class SeriesPremieresAreToldFromReturningSeasonsTests(CalendarRouteTestCase):
    """On the Season Premieres calendar every card is somebody's episode 1, and
    they all looked identical — the only way to tell a brand-new show from a
    fifth season was to read the season number off a small blue label.

    ONE RULE ACROSS EVERY CALENDAR: episode 1 gets a label, and a first season
    gets the accent. That is what lets All Episodes mark the premieres inside it
    without marking every episode of a first season, with no endpoint named
    anywhere in the template.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("premiere_viewer")
        self.sign_in_as(self.user_id)
        first = _entry("brand-new", "Brand New", "2026-07-15T20:00:00Z")
        first["episode"]["season"] = 1
        first["episode"]["number"] = 1
        fifth = _entry("long-runner", "Long Runner", "2026-07-16T20:00:00Z")
        fifth["episode"]["season"] = 5
        fifth["episode"]["number"] = 1
        midseason = _entry("mid-season", "Mid Season", "2026-07-17T20:00:00Z")
        midseason["episode"]["season"] = 1
        midseason["episode"]["number"] = 7
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([first, fifth, midseason]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def cards(self, endpoint="shows/premieres") -> dict[str, str]:
        """{title: that card's markup} for the month."""
        page = self.client.get(f"/?year=2026&month=7&endpoint={endpoint}").text
        out = {}
        for chunk in page.split('<div class="card')[1:]:
            card = '<div class="card' + chunk.split("</div>\n</div>")[0]
            for title in ("Brand New", "Long Runner", "Mid Season"):
                if f'data-details-heading="{title}"' in card:
                    out[title] = card
        return out

    def test_a_first_season_says_so_and_carries_the_accent(self):
        card = self.cards()["Brand New"]
        self.assertIn("SERIES PREMIERE", card)
        self.assertIn("is-series-premiere", card)
        self.assertIn("premiere-badge first-season", card)

    def test_a_later_season_is_labelled_but_not_accented(self):
        """A returning season is the ordinary case on this calendar, so it gets
        the word and not the gold — making both of them loud would leave the pair
        as hard to tell apart as no labels at all."""
        card = self.cards()["Long Runner"]
        self.assertIn("RETURNING", card)
        self.assertNotIn("SERIES PREMIERE", card)
        self.assertNotIn("is-series-premiere", card)

    def test_an_episode_that_is_not_a_premiere_is_not_labelled(self):
        """THE PART THAT MAKES ONE RULE WORK EVERYWHERE. An S01E07 on the All
        Episodes calendar is in a first season and is not a series premiere, so
        the first-season test alone would have been wrong there."""
        card = self.cards("shows")["Mid Season"]
        self.assertNotIn("premiere-badge", card)
        self.assertNotIn("is-series-premiere", card)

    def test_the_premieres_inside_all_episodes_are_still_marked(self):
        cards = self.cards("shows")
        self.assertIn("SERIES PREMIERE", cards["Brand New"])
        self.assertIn("RETURNING", cards["Long Runner"])


# ---------------------------------------------------------------------------
# the timezone picker: persists, and changes which month a boundary item lands in
# ---------------------------------------------------------------------------

class TimezonePickerTests(CalendarRouteTestCase):
    def setUp(self):
        super().setUp()
        # The bootstrap default (Europe/Athens) is what a fresh account is
        # seeded with; the boundary item lands in March for it (UTC+2 in Feb,
        # so 02:00 UTC 1 Mar is already 04:00 1 Mar local).
        self.user_id = self._make_user("tz_viewer")
        self.sign_in_as(self.user_id)
        target_window = calendar_cache.window_start(date(2026, 3, 1))

        def fake(endpoint, start):
            if start == target_window:
                return [_entry("boundary", "Boundary Show", "2026-03-01T02:00:00Z")]
            return []

        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch(fake))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_timezone_places_the_item_in_march(self):
        march = self.client.get("/?year=2026&month=3")
        self.assertIn("Boundary Show", march.text)
        feb = self.client.get("/?year=2026&month=2")
        self.assertNotIn("Boundary Show", feb.text)

    def test_changing_timezone_persists_and_moves_the_item_to_february(self):
        resp = self.client.post("/api/me/timezone", json={"timezone": "America/Los_Angeles"})
        # 204 with `HX-Refresh`, because every day header and air time on the
        # page was rendered for the OLD zone: the page genuinely has to come
        # again, and htmx turns that header into the reload the caller used to
        # do for itself.
        self.assertEqual(resp.status_code, 204, resp.text)
        self.assertEqual(resp.headers.get("HX-Refresh"), "true")

        user_row = asyncio.run(auth.get_user(self.user_id))
        self.assertEqual(user_row["timezone"], "America/Los_Angeles")

        # 02:00 UTC 1 Mar 2026 is still PST (DST starts 8 Mar): 18:00, 28 Feb local.
        feb = self.client.get("/?year=2026&month=2")
        self.assertIn("Boundary Show", feb.text)
        march = self.client.get("/?year=2026&month=3")
        self.assertNotIn("Boundary Show", march.text)

    def test_an_unknown_timezone_name_is_rejected(self):
        resp = self.client.post("/api/me/timezone", json={"timezone": "Not/AZone"})
        self.assertEqual(resp.status_code, 400)
        user_row = asyncio.run(auth.get_user(self.user_id))
        self.assertEqual(user_row["timezone"], "Europe/Athens")


# ---------------------------------------------------------------------------
# partial-data degradation: a window Trakt can't supply warns rather than fails
# ---------------------------------------------------------------------------

class PartialDataBannerTests(CalendarRouteTestCase):
    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("partial_viewer")
        self.sign_in_as(self.user_id)

    def test_a_failed_window_renders_the_month_with_a_distinct_warning_banner(self):
        """One window failing drops that window's days but still renders the
        rest, under an amber warning banner — not the red error banner and not
        the whole-page error path."""
        good_window = calendar_cache.window_start(date(2026, 7, 8))
        boom_window = calendar_cache.window_start(date(2026, 7, 20))

        def fake(endpoint, start):
            if start == boom_window:
                raise TraktError("Trakt unreachable", 503)
            if start == good_window:
                return [_entry("show-good", "Good Show", "2026-07-08T20:00:00Z")]
            return []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            resp = self.client.get("/?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Good Show", resp.text)
        self.assertIn("warning-banner", resp.text)
        self.assertIn("showing what we have", resp.text)
        self.assertNotIn("error-banner", resp.text)

    def test_every_window_failing_shows_the_error_banner_not_the_warning(self):
        """No window loaded and nothing cached: there is nothing to show, so the
        month falls to the hard error banner, not the partial warning."""
        def fake(endpoint, start):
            raise TraktError("Trakt unreachable", 503)

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            resp = self.client.get("/?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error-banner", resp.text)
        self.assertNotIn("warning-banner", resp.text)


class AnEmptyMonthSaysWhyTests(CalendarRouteTestCase):
    """OBSERVED by the owner on months far ahead: every empty month said "No
    items matched your filters" — including ones whose every week had been fetched
    and simply held nothing, with no filter removing anything at all."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("empty_viewer"))

    def test_a_month_that_holds_nothing_does_not_blame_the_filters(self):
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            html = self.client.get("/calendar?year=2026&month=7").text
        self.assertIn("Nothing is on this calendar for this month yet.", html)
        self.assertNotIn("matched your filters", html)

    def test_a_partly_unreadable_empty_month_says_so_rather_than_blaming_filters(self):
        """The banner says part of the month could not be loaded; the empty
        state says what an empty month says, rather than a second, more alarming
        sentence about the same gap (the owner's call, on a real May 2027)."""
        boom_window = calendar_cache.window_start(date(2026, 7, 20))

        def fake(endpoint, start):
            if start == boom_window:
                raise TraktError("Trakt unreachable", 503)
            return []

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            html = self.client.get("/calendar?year=2026&month=7").text
        self.assertIn("warning-banner", html)
        self.assertIn("Nothing is on this calendar for this month yet.", html)
        self.assertNotIn("No items matched your filters", html)


def _unreachable_client() -> httpx.AsyncClient:
    """A real httpx client whose every connection fails, the way a DNS failure
    or a service outage does — BELOW the stand-ins the tests above use, which
    raise TraktError themselves and so could never see a network error arrive
    untranslated."""
    def refuse(request):
        raise httpx.ConnectError("All connection attempts failed", request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(refuse))


class UnreachableServiceTests(CalendarRouteTestCase):
    """MEASURED BEFORE THE FIX: an instance whose outbound traffic went to a
    closed proxy answered GET /calendar with a 500 for any month it had nothing
    stored for, because the ConnectError reached the fill as an httpx exception
    rather than as a source that could not answer. Unreachable is the ordinary
    way a service is unavailable, and the page has a banner for exactly that."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("offline_viewer"))

    def test_a_calendar_nobody_can_reach_renders_the_error_banner_not_a_500(self):
        with patch("app.providers.trakt.transport.shared_client",
                   return_value=_unreachable_client()), \
             patch("app.providers.simkl.transport.cdn_client",
                   return_value=_unreachable_client()):
            resp = self.client.get("/calendar?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error-banner", resp.text)


# ---------------------------------------------------------------------------
# A Simkl-only month outside the declared coverage window
# ---------------------------------------------------------------------------

class CoverageGapTests(CalendarRouteTestCase):
    """Naming ONE calendar source that does not reach the requested
    month must render an explicit state, never a blank calendar — and it is
    the ROUTE's job, since only the route knows a source was picked on
    purpose rather than merely admitted by 'auto'."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("gap_viewer")
        self.sign_in_as(self.user_id)

    def test_naming_simkl_for_a_month_it_does_not_reach_says_so(self):
        """March 2015 is far outside Simkl's declared ~36 months back —
        real Capabilities, no stub needed for the coverage question itself."""
        # fetch_window_records is patched even though the coverage-gap branch
        # returns before calling it, so a regression that removed the early
        # return would fail LOUD (a real fetch) rather than quietly passing
        # against a fixture that happens to render nothing.
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            resp = self.client.get("/calendar?year=2015&month=3&source=simkl")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Simkl", resp.text)
        self.assertIn("doesn&#39;t reach", resp.text.replace("’", "'"))
        self.assertIn("warning-banner", resp.text)
        self.assertNotIn("error-banner", resp.text)
        self.assertNotIn("empty-state", resp.text)  # not the plain "nothing matched" state

    def test_the_gap_offers_a_link_to_the_other_source(self):
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            resp = self.client.get("/calendar?year=2015&month=3&source=simkl")
        self.assertIn("source=trakt", resp.text)

    def test_naming_both_never_reports_a_gap(self):
        """'both' is never one source's promise to keep — a month outside
        Simkl's window under `both` just renders Trakt-only, silently, exactly
        as it did before Simkl had a calendar at all."""
        entries = [_entry("show-a", "Show A", "2015-03-05T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(entries)):
            resp = self.client.get("/calendar?year=2015&month=3&source=both")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("doesn&#39;t reach", resp.text)
        self.assertIn("Show A", resp.text)

    def test_auto_never_reports_a_gap_either(self):
        entries = [_entry("show-a", "Show A", "2015-03-05T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(entries)):
            resp = self.client.get("/calendar?year=2015&month=3")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("doesn&#39;t reach", resp.text)

    def test_a_month_inside_simkls_window_renders_normally(self):
        """Naming Simkl for a month it DOES cover is not a gap at all — the
        source is asked like any other, through the real fetch."""
        entries = [_entry("show-a", "Show A", "2026-07-15T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(entries)):
            resp = self.client.get("/?year=2026&month=7&source=simkl")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Show A", resp.text)
        self.assertNotIn("doesn&#39;t reach", resp.text)


class SourceSelectorTests(CalendarRouteTestCase):
    """The toolbar control that reaches `?source=`.

    IT IS A VIEW CONTROL AND THE ASSERTION THAT MATTERS IS WHAT IT DOES NOT DO.
    The account's answer to "which services fill my calendar" is stated on one
    screen, and a control on the calendar that quietly rewrote it would change
    every other view the account has as a side effect of a look — so the stored
    row is checked to be exactly as it was after the control has been used.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("selector_viewer")
        self.sign_in_as(self.user_id)

    def _options(self, html: str) -> list[tuple[str, str]]:
        block = re.search(r'<select id="sourceSelect".*?</select>', html, re.S)
        self.assertIsNotNone(block, "the toolbar offers no source control")
        return re.findall(r'<option value="([^"]*)"([^>]*)>', block.group(0))

    def _page(self, url: str) -> str:
        entries = [_entry("show-a", "Show A", "2026-07-15T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(entries)):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_it_offers_the_services_that_answer_this_calendar(self):
        shows = [value for value, _ in
                 self._options(self._page("/calendar?year=2026&month=7&endpoint=shows"))]
        self.assertEqual(shows, ["", "auto", "trakt", "simkl"])

    def test_a_calendar_one_service_publishes_gets_no_control_at_all(self):
        """Season finales are published by one service, so "my sources", "every
        service" and "that one only" are three labels for one outcome. A control
        offering them says a choice is available when none is."""
        html = self._page("/calendar?year=2026&month=7&endpoint=shows/finales")
        self.assertNotIn('id="sourceSelect"', html)
        self.assertIn('id="endpointSelect"', html)

    def test_a_service_switched_off_for_the_instance_is_not_offered(self):
        """The reported fault, and the reason the admission is asked of
        app/providers rather than counted here: with the instance-wide switch off
        the control was still offering a service the calendar underneath it would
        refuse. One service is left, so there is nothing to choose and nothing is
        drawn."""
        save_settings(dataclasses.replace(
            _configured_settings(), simkl_public_calendar_enabled=False))
        html = self._page("/calendar?year=2026&month=7&endpoint=shows")
        self.assertNotIn('id="sourceSelect"', html)
        self.assertIn('id="endpointSelect"', html)

    def test_what_it_offers_is_derived_and_never_a_list_of_service_names(self):
        """The rule has to keep working for a service nobody has written yet, so
        this asks the function with a registry that has one. Anything spelling a
        service name in the logic would leave the hypothetical one out while the
        two real ones survived."""
        from app.endpoints import get_endpoint

        endpoint = get_endpoint("shows")
        settings = _configured_settings()
        real = calendar_routes._source_choices(endpoint, "", settings)
        with patch("app.providers.registered", return_value=dict(
                providers.registered(), **{"mercury": _third_source()})):
            widened = calendar_routes._source_choices(endpoint, "", settings)
        self.assertEqual([c["value"] for c in widened],
                         [c["value"] for c in real] + ["mercury"])
        self.assertIn({"value": "mercury", "label": "Mercury only", "selected": False},
                      widened)

    def test_a_hypothetical_service_switched_off_leaves_the_other_two(self):
        """The two halves compose: the registry widens the offer and the
        instance's own admission narrows it, and neither knows a name."""
        from app.endpoints import get_endpoint

        registry = dict(providers.registered(), **{"mercury": _third_source()})
        with patch("app.providers.registered", return_value=registry):
            with patch("app.calendar.resolve.instance_sources",
                       return_value=frozenset({"trakt", "mercury"})):
                choices = calendar_routes._source_choices(
                    get_endpoint("shows"), "", _configured_settings())
        self.assertEqual([c["value"] for c in choices],
                         ["", "auto", "trakt", "mercury"])

    def test_with_nothing_overridden_it_sits_on_the_accounts_own_answer(self):
        options = self._options(self._page("/calendar?year=2026&month=7"))
        selected = [value for value, attrs in options if "selected" in attrs]
        self.assertEqual(selected, [""])

    def test_it_reflects_what_the_query_string_currently_says(self):
        options = self._options(self._page("/calendar?year=2026&month=7&source=simkl"))
        selected = [value for value, attrs in options if "selected" in attrs]
        self.assertEqual(selected, ["simkl"])

    def test_an_override_this_calendar_does_not_offer_is_still_shown(self):
        """It is in force, so reading as the stored answer it is overriding would
        be the control disagreeing with the page under it."""
        options = self._options(
            self._page("/calendar?year=2026&month=7&source=trakt%2Bsimkl"))
        self.assertIn(("trakt+simkl", " selected"), options)

    def test_using_it_changes_what_the_page_reads_and_nothing_else(self):
        """THE WHOLE POINT. The override applies to this view; the stored
        preference is untouched — including for an account that had no row at
        all, which must still have none afterwards."""
        before = asyncio.run(source_prefs.load(self.user_id))
        row_before = asyncio.run(db.fetch_one(
            "SELECT * FROM source_prefs WHERE user_id = ?", (self.user_id,)))
        self._page("/calendar?year=2026&month=7&source=simkl")
        self._page("/calendar?year=2026&month=7&source=trakt")
        self.assertIsNone(row_before)
        self.assertIsNone(asyncio.run(db.fetch_one(
            "SELECT * FROM source_prefs WHERE user_id = ?", (self.user_id,))))
        self.assertEqual(asyncio.run(source_prefs.load(self.user_id)), before)

    def test_an_account_that_did_state_a_preference_keeps_it_byte_for_byte(self):
        asyncio.run(source_prefs.save(source_prefs.SourcePrefs(
            user_id=self.user_id, calendar_source="simkl",
            metadata_order=["simkl"])))
        stored = asyncio.run(source_prefs.load(self.user_id))
        self._page("/calendar?year=2026&month=7&source=trakt")
        self.assertEqual(asyncio.run(source_prefs.load(self.user_id)), stored)

    def test_the_control_sits_between_the_calendar_and_the_card_layout(self):
        """Where the author asked for it, and where it belongs: the two controls
        either side of it also decide what this one page shows. Still true now
        that three of the four share a panel — the order within it is the order
        they were in when they were loose."""
        html = self._page("/calendar?year=2026&month=7")
        self.assertLess(html.index('id="endpointSelect"'), html.index('id="sourceSelect"'))
        self.assertLess(html.index('id="sourceSelect"'), html.index('id="cardStyleSelect"'))

    def test_the_device_timezone_button_sits_before_its_select(self):
        """The 📍 acts ON the timezone control, so it reads as a prefix to it
        rather than as something trailing the row — and keeping it out of the
        select's own cell is what lets that select stay the same width as the
        three above it. Asserted by DOM order because that is what decides it."""
        html = self._page("/calendar?year=2026&month=7")
        row = html[html.index('class="view-row tz"'):]
        row = row[:row.index("</div>")]
        self.assertLess(row.index("data-device-timezone"), row.index('id="tzSelect"'))

    def test_the_endpoint_picker_stays_out_of_the_view_panel(self):
        """THE GROUPING RULE, PINNED. The four controls behind 🎛️ View answer
        "how should this be drawn"; the endpoint picker answers "which calendar
        am I reading", which is navigation and stays in the bar. Asserted because
        the cheapest future tidy-up of that row is to sweep the picker in too,
        and that would put the most-used control on the page behind a click."""
        html = self._page("/calendar?year=2026&month=7")
        panel = html[html.index('class="nav-menu view-menu"'):]
        panel = panel[:panel.index("</details>")]
        self.assertIn('id="sourceSelect"', panel)
        self.assertIn('id="cardStyleSelect"', panel)
        self.assertIn('id="dayPackSelect"', panel)
        self.assertIn('id="tzSelect"', panel)
        self.assertNotIn('id="endpointSelect"', panel)


# ---------------------------------------------------------------------------
# the view logic the server now owns: counts, is-new, the committed baseline
# ---------------------------------------------------------------------------

def _mark_key(slug: str) -> str:
    """The data-id a card built from `_entry(slug, ...)` carries.

    NOT THE SLUG ANY MORE. A card's identity is its group's, not the winning
    source's id, because the winner moves with a per-viewer preference and a
    per-viewer MARK cannot be allowed to move with it — see Item.mark_key.

    These fixtures carry a slug and a trakt id and NO SHARED id, so the
    cross-source waterfall finds nothing to key on and the identity falls back to
    source-and-id. That is the right answer for them: a title no shared id space
    names can never merge with another source's record, so there is only ever one
    description of it and nothing for a preference to move.
    """
    return f"trakt:{slug}"


def _card_class(html: str, item_id: str) -> str:
    m = re.search(r'<div class="([^"]*)" data-id="%s"' % re.escape(item_id), html)
    return m.group(1) if m else ""


def _stat(html: str, element_id: str) -> str:
    m = re.search(r'id="%s">([^<]*)<' % element_id, html)
    return m.group(1) if m else ""


class ServerRenderedViewTests(CalendarRouteTestCase):
    """The tile counts, the is-new marks and the change-detection baseline used
    to be computed in the browser from the cards it was holding. They are the
    server's now, so they are right at first paint and stay right however much of
    a month is actually on screen."""

    ENDPOINT = "shows"
    PAGE = "/?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("view_logic_viewer")
        self.sign_in_as(self.user_id)
        # show-a airs twice this month, which is what separates "shows" from
        # "cards": one mark on it moves two items' worth of the tiles.
        entries = [
            _entry("show-a", "Show A", "2026-07-15T20:00:00Z"),
            _entry("show-b", "Show B", "2026-07-16T20:00:00Z"),
            _entry("show-a", "Show A", "2026-07-17T20:00:00Z"),
            _entry("show-c", "Show C", "2026-07-20T20:00:00Z"),
        ]
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch(entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed_baseline(self, *, last_show_ids, last_count):
        asyncio.run(calendar_state.set_view_state(
            self.user_id, self.ENDPOINT, 2026, 7,
            last_count=last_count, last_show_ids=last_show_ids, history=[]))

    def _stored_baseline(self) -> dict:
        return asyncio.run(calendar_state.load_view_state(
            self.user_id, self.ENDPOINT, 2026, 7))

    def test_the_stat_tiles_carry_the_whole_months_counts(self):
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-a", "not_watching": True})
        html = self.client.get(self.PAGE).text
        # Four airings, two of them the marked show's.
        self.assertEqual(_stat(html, "statTotal"), "4")
        self.assertEqual(_stat(html, "statWatching"), "2")
        self.assertEqual(_stat(html, "statNotWatching"), "2")
        # THE TILES ARE THE ONLY STATEMENT OF THESE NUMBERS. The page used to
        # embed them again as JSON, with the month's per-show card counts beside
        # them, so that marking a show could move the tiles by arithmetic; a
        # mark is answered with the counts now, so a second copy in the page
        # would be one more thing to keep in step.

    def test_a_marked_show_is_rendered_not_watching_rather_than_marked_after_paint(self):
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-a", "not_watching": True})
        html = self.client.get(self.PAGE).text
        # THE MARK WAS MADE UNDER A LEGACY ID and is still honoured: it lands on
        # the card by the mark KEY, which is what cards carry. See
        # calendar/state.py's `marked`.
        self.assertIn("not-watching", _card_class(html, _mark_key("show-a")))
        self.assertNotIn("not-watching", _card_class(html, _mark_key("show-b")))

    def test_a_first_look_at_a_month_marks_nothing_new(self):
        """No stored baseline means this view has never been seen — which is not
        the same as every show in it having just appeared."""
        html = self.client.get(self.PAGE).text
        for key in ("show-a", "show-b", "show-c"):
            self.assertNotIn("is-new", _card_class(html, _mark_key(key)), key)
        self.assertIn("(Initial Tracking)", html)

    def test_only_shows_missing_from_the_stored_baseline_come_back_new(self):
        self._seed_baseline(last_show_ids=[_mark_key("show-a")], last_count=1)
        html = self.client.get(self.PAGE).text
        self.assertNotIn("is-new", _card_class(html, _mark_key("show-a")))
        self.assertIn("is-new", _card_class(html, _mark_key("show-b")))
        self.assertIn("is-new", _card_class(html, _mark_key("show-c")))

    def test_the_render_commits_the_servers_full_id_list_as_the_next_baseline(self):
        """The committed list has to be the month's, not a page's: anything it
        leaves out reads as new on the next visit."""
        self.client.get(self.PAGE)
        stored = self._stored_baseline()
        self.assertEqual(stored["last_show_ids"],
                         [_mark_key("show-a"), _mark_key("show-b"), _mark_key("show-c")])
        self.assertEqual(stored["last_count"], 4)
        # Committed, so a second load of an unchanged month finds nothing new.
        second = self.client.get(self.PAGE).text
        for key in ("show-a", "show-b", "show-c"):
            self.assertNotIn("is-new", _card_class(second, _mark_key(key)), key)
        self.assertIn("Perfect Match", second)

    def test_a_month_that_could_not_be_loaded_leaves_the_baseline_alone(self):
        """Committing an empty month over a real baseline would make the whole
        month look new the next time it loads properly."""
        self._seed_baseline(last_show_ids=[_mark_key("show-a"), _mark_key("show-b"), _mark_key("show-c")], last_count=4)

        def boom(endpoint, start):
            raise TraktError("Trakt unreachable", 503)

        with patch("app.calendar.cache.fetch_window_records", window_fetch(boom)):
            resp = self.client.get(self.PAGE)
        self.assertIn("error-banner", resp.text)
        stored = self._stored_baseline()
        self.assertEqual(stored["last_show_ids"],
                         [_mark_key("show-a"), _mark_key("show-b"), _mark_key("show-c")])
        self.assertEqual(stored["last_count"], 4)

    def test_the_delta_line_reports_the_change_since_the_last_run(self):
        self._seed_baseline(last_show_ids=[_mark_key("show-a")], last_count=1)
        html = self.client.get(self.PAGE).text
        self.assertIn("(+3 since last run)", html)
        self.assertIn('class="delta-msg up"', html)

    def test_the_history_log_is_rendered_with_the_page(self):
        html = self.client.get(self.PAGE).text
        self.assertIn("History:", html)
        self.assertIn("4 items", html)


class CalendarMarkupTests(CalendarRouteTestCase):
    """The day/card partials and the two page-level affordances they enable."""

    PAGE = "/?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("markup_viewer")
        self.sign_in_as(self.user_id)
        entries = [
            _entry("show-a", "Show A", "2026-07-15T20:00:00Z"),
            _entry("show-b", "Show B", "2026-07-16T20:00:00Z"),
        ]
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch(entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_day_is_an_addressable_section(self):
        html = self.client.get(self.PAGE).text
        self.assertIn('id="day-2026-07-15"', html)
        self.assertIn('id="day-2026-07-16"', html)

    def test_the_jump_to_strip_links_days_with_items_and_greys_the_rest(self):
        html = self.client.get(self.PAGE).text
        self.assertIn('<a class="day-chip" data-date="2026-07-15" href="#day-2026-07-15"', html)
        # 1 July has nothing on it, so there is no section to send anyone to.
        self.assertIn('<span class="day-chip empty"', html)
        self.assertNotIn('href="#day-2026-07-01"', html)
        # One chip per day of the month, links and inert ones together.
        self.assertEqual(len(re.findall(r'class="day-chip[ "]', html)), 31)
        # The hover band that brings the strip back has to be the strip's
        # immediately preceding sibling for the CSS reveal to select it.
        self.assertIn('<div class="day-chips-peek" aria-hidden="true"></div>\n<nav class="day-chips"', html)

    def test_a_day_showing_nothing_to_this_viewer_gets_an_inert_chip(self):
        """With hiding on, a day whose every item is marked renders no cards at
        all — so its chip must not offer to scroll to it. The day still has
        items, so this is not the same as the empty-day case."""
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-a", "not_watching": True})
        html = self.client.get(self.PAGE).text
        # 15 July is show-a's only day and show-a is hidden; 16 July still shows.
        self.assertIn('<a class="day-chip unreachable" data-date="2026-07-15"', html)
        self.assertIn('<a class="day-chip" data-date="2026-07-16"', html)
        # Showing everything again makes it a destination once more.
        self.client.post("/api/me/prefs", json={"hide_not_watching": False})
        shown = self.client.get(self.PAGE).text
        self.assertIn('<a class="day-chip" data-date="2026-07-15"', shown)

    def test_a_mark_under_a_key_the_card_has_moved_off_still_redraws_it(self):
        """REPORTED: a Simkl title was drawn as `show:tvdb:…`, enrichment then
        added a tmdb id and the same card keyed as `show:tmdb:…`, and pressing
        the eye stored the mark but redrew nothing until a reload. The answer
        finds its cards by the mark rule, which knows every key a title's ids
        spell."""
        entry = _entry("show-c", "Show C", "2026-07-17T20:00:00Z")
        entry["show"]["ids"].update({"tvdb": 482228, "tmdb": 777})
        with patch("app.calendar.cache.fetch_window_records", window_fetch([entry])):
            self.client.get(self.PAGE)
            answer = self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                                      json={"item_id": "show:tvdb:482228",
                                            "not_watching": True})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("Show C", answer.text, "the card the mark covers was not redrawn")

    def test_a_title_the_read_no_longer_draws_is_taken_off_the_page(self):
        """REPORTED: a card was on screen, its details then named a country the
        viewer filters out, and the eye redrew nothing — a reload showed the
        title gone. The answer now says what the reload would."""
        answer = self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                                  json={"item_id": "show:tmdb:4040", "not_watching": True})
        self.assertIn("""hx-swap-oob="delete:.card[data-id='show:tmdb:4040']\"""", answer.text)

    def test_a_title_still_drawn_is_redrawn_and_not_removed(self):
        self.client.get(self.PAGE)
        answer = self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                                  json={"item_id": "show-a", "not_watching": True})
        self.assertIn("Show A", answer.text)
        self.assertNotIn("delete:", answer.text)

    def test_a_key_that_is_not_key_shaped_never_reaches_the_selector(self):
        """The key is the page's own, but it is still a value a request carried,
        and it lands inside a quoted selector."""
        answer = self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                                  json={"item_id": "x'],body", "not_watching": True})
        self.assertNotIn("delete:", answer.text)

    def test_a_hidden_days_chip_keeps_its_href_so_a_toggle_can_revive_it(self):
        """The client toggles hiding without a reload and answers by adding or
        removing a class, so a day that merely LOOKS empty to this viewer must
        still be an <a> with its href — a class cannot restore one to a <span>.
        Only a day holding nothing at all, which no toggle can change, is a
        span."""
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-a", "not_watching": True})
        html = self.client.get(self.PAGE).text
        self.assertIn('<a class="day-chip unreachable" data-date="2026-07-15" '
                      'href="#day-2026-07-15"', html)
        self.assertNotIn('<span class="day-chip empty" data-date="2026-07-15"', html)
        # 1 July genuinely holds nothing: no section exists to link to, ever.
        self.assertIn('<span class="day-chip empty" data-date="2026-07-01"', html)
        self.assertNotIn('href="#day-2026-07-01"', html)

    def test_the_picker_has_one_address_and_every_link_uses_it(self):
        """The picker answered at a second URL for a while, rendering the same
        template from the same context, so a link, a bookmark and a history entry
        could disagree about where it lives. Nothing points at the old one, and
        the page's own year arrows and endpoint switcher stay on this one."""
        picker = self.client.get("/?year=2026&endpoint=shows")
        self.assertEqual(picker.status_code, 200)
        self.assertIn("Pick a Month", picker.text)
        self.assertNotIn("/pick", picker.text)
        self.assertIn('href="/?year=2025&amp;endpoint=shows"', picker.text)
        self.assertIn('href="/?year=2027&amp;endpoint=shows"', picker.text)
        self.assertEqual(self.client.get("/pick").status_code, 404)

    def test_the_month_heading_links_back_to_the_picker_at_the_root(self):
        """Stepping back out of a month lands on the address somebody would type
        or bookmark, rather than on the second URL that renders the same page."""
        html = self.client.get(self.PAGE).text
        self.assertIn('<a class="month-title-link" href="/?year=2026&amp;endpoint=shows"', html)

    def test_the_eye_icons_are_defined_once_and_referenced_per_card(self):
        html = self.client.get(self.PAGE).text
        self.assertEqual(html.count('<symbol id="eye-open"'), 1)
        self.assertEqual(html.count('<symbol id="eye-closed"'), 1)
        # Two cards, each referencing both symbols rather than repeating them.
        self.assertEqual(html.count('href="#eye-open"'), 2)
        self.assertEqual(html.count('href="#eye-closed"'), 2)
        # The two drawings are NOT the same: closed keeps its strike line.
        closed = re.search(r'<symbol id="eye-closed".*?</symbol>', html, re.S).group(0)
        self.assertIn('<line x1="2" y1="2" x2="22" y2="22">', closed)


# ---------------------------------------------------------------------------
# the split: a picker at "/", a shell at "/calendar", content at "/calendar/day"
# ---------------------------------------------------------------------------

def _day_sections(html: str) -> list[str]:
    """The dates of the day blocks a response actually rendered with their cards
    — placeholders carry a class of their own and are deliberately not counted."""
    return re.findall(r'<section class="day-block" id="day-([\d-]+)"', html)


def _placeholder_dates(html: str) -> list[str]:
    """The dates a response announced but did not render cards for."""
    return re.findall(r'<section class="day-block is-skeleton[^"]*"\s*\n?\s*id="day-([\d-]+)"', html)


def _day_urls(html: str) -> list[str]:
    """Every per-day content request the page's placeholders would make."""
    return [u.replace("&amp;", "&") for u in re.findall(r'hx-get="(/calendar/day[^"]+)"', html)]


def _section(html: str, day: str) -> str:
    m = re.search(r'<section class="[^"]*"\s*\n?\s*id="day-%s"[^>]*>' % re.escape(day),
                  html, re.S)
    return m.group(0) if m else ""


class DayLayoutTests(CalendarRouteTestCase):
    """What the server still owes a day it has not sent the cards for.

    HOW WIDE A DAY IS AND WHETHER IT SHOWS AT ALL ARE THE STYLESHEET'S, asked of
    the block's own cards (style.css's `--cols` and the hide-mode collapse rule),
    and neither is markup any more — so neither is testable here, and a test
    pretending otherwise would be asserting a string rather than a layout. What IS
    the server's is the count a PLACEHOLDER carries, because it has no cards to be
    asked about; the stylesheet does the same arithmetic on it that it does on a
    block that has them.
    """

    PAGE = "/calendar?year=2026&month=7&endpoint=shows"
    DAY = "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-15"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("layout_viewer")
        self.sign_in_as(self.user_id)
        # Past the inline window, so every one of these days is a placeholder.
        entries = [_entry(f"show-{n}", f"Show {n}", "2026-07-15T20:00:00Z") for n in range(3)]
        entries.append(_entry("solo", "Solo", "2026-07-16T20:00:00Z"))
        entries += [_entry(f"early-{n}", f"Early {n}", f"2026-07-0{n + 1}T20:00:00Z")
                    for n in range(5)]
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch(entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_placeholder_says_how_many_cards_it_holds_and_how_many_show(self):
        html = self.client.get(self.PAGE).text
        self.assertIn('data-held="3"', _section(html, "2026-07-15"))
        self.assertIn('data-visible="3"', _section(html, "2026-07-15"))
        self.assertIn('data-held="1"', _section(html, "2026-07-16"))

    def test_a_placeholder_counts_a_marked_title_out_of_what_shows(self):
        """`data-visible` follows this viewer's marks and `data-held` does not: the
        stylesheet reserves space from one or the other depending on whether
        hiding is on, and the switch is thrown without a reload."""
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-0", "not_watching": True})
        block = _section(self.client.get(self.PAGE).text, "2026-07-15")
        self.assertIn('data-held="3"', block)
        self.assertIn('data-visible="2"', block)

    def test_a_placeholder_this_viewer_sees_nothing_of_says_so(self):
        """Zero is the selector the stylesheet collapses a placeholder on, which is
        why the count is an ATTRIBUTE — see _day_skeleton.html."""
        for show in ("show-0", "show-1", "show-2"):
            self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                             json={"item_id": show, "not_watching": True})
        block = _section(self.client.get(self.PAGE).text, "2026-07-15")
        self.assertIn('data-visible="0"', block)
        self.assertIn('data-visible="1"', _section(self.client.get(self.PAGE).text, "2026-07-16"))

    def test_a_day_that_arrives_late_carries_no_layout_at_all(self):
        """It has its cards, so there is nothing left for the server to say about
        how they sit — and a `--cols` written here would out-specify the
        stylesheet's own answer and freeze the day at the width it arrived with."""
        fragment = self.client.get(self.DAY).text
        block = _section(fragment, "2026-07-15")
        self.assertNotIn("--cols", block)
        self.assertNotIn("data-held", block)
        self.assertNotIn("is-empty-hidden", block)

    def test_a_late_day_draws_its_own_chip_again_out_of_band(self):
        """The shell drew the chip from the marks it read when the page loaded. A
        day fetched afterwards has read them again, so its chip rides out-of-band
        on the day's own answer and replaces the shell's."""
        fragment = self.client.get(self.DAY).text
        chip = re.search(r'<a class="day-chip[^"]*"[^>]*id="day-chip-2026-07-15"[^>]*>', fragment)
        self.assertIsNotNone(chip, "the day did not draw its chip")
        self.assertIn('hx-swap-oob="true"', chip.group(0))
        self.assertNotIn("unreachable", chip.group(0))

    def test_a_day_whose_every_show_was_marked_since_arrives_with_its_chip_greyed(self):
        """With hiding on, a day whose shows were all marked after the page loaded
        arrives collapsed — and its chip must stop offering to scroll to it, which
        it went on doing when the chip was only ever drawn by the shell. The chip
        is the one half of this the stylesheet cannot reach: it sits above <main>,
        so no selector runs from the day to it."""
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        for show in ("show-0", "show-1", "show-2"):
            self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                             json={"item_id": show, "not_watching": True})
        fragment = self.client.get(self.DAY).text
        self.assertRegex(fragment, r'<a class="day-chip unreachable"[^>]*id="day-chip-2026-07-15"')

    def test_a_day_with_nothing_on_it_draws_no_chip(self):
        """Its placeholder simply goes, and the shell's chip for it already said
        there was nothing there."""
        empty = self.client.get(
            "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-20").text
        self.assertNotIn("day-chip", empty)


class RouteSplitTests(CalendarRouteTestCase):
    """The one route that was both the front page and the calendar is now two,
    and the calendar's own content is a third."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("split_viewer"))

    def test_the_root_is_the_month_picker(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("month-btn", page.text)
        self.assertNotIn('id="statsBar"', page.text)

    def test_an_old_calendar_link_is_forwarded_to_the_calendar(self):
        """Bookmarks and shared URLs from when "/" served the calendar too must
        keep working rather than dropping someone on a month picker."""
        resp = self.client.get("/?month=7&year=2026&endpoint=shows", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/calendar?month=7&year=2026&endpoint=shows")

    def test_the_calendar_lives_at_its_own_path(self):
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            page = self.client.get("/calendar?year=2026&month=7")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="statsBar"', page.text)
        # And the endpoint switcher submits to it, not to the picker.
        self.assertIn('action="/calendar"', page.text)

    def test_the_numbers_card_is_a_popover_a_phone_opens_with_a_pill(self):
        """On a phone the card covered most of a card's width; it is a popover
        there, opened by a pill holding the month's total (style.css hides it at
        700px and below and draws it as an ordinary card above). The pill sits
        right before the card, which is what turns its arrow while it is open.
        MEASURED in Chromium and Firefox at 1280 (card where it always was, no
        pill) and 390 (hidden; the pill, a tap outside, Escape and the pill again
        open and close it)."""
        with patch("app.calendar.cache.fetch_window_records", window_fetch([])):
            html = self.client.get("/calendar?year=2026&month=7").text
        pill = re.search(r'<button[^>]*class="stats-pill"[^>]*>', html).group(0)
        self.assertIn('popovertarget="statsBar"', pill)
        self.assertRegex(html, r'(?s)class="stats-pill".*?</button>\s*<div id="statsBar" class="stats-bar" popover>')


class CalendarShellTests(CalendarRouteTestCase):
    """The page ships the first few days and asks for the rest itself."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("shell_viewer"))
        # One item on each of the month's first ten days: more days than the shell
        # renders inline, so the split is actually exercised.
        entries = [_entry(f"show-{day}", f"Show {day}", f"2026-07-{day:02d}T20:00:00Z")
                   for day in range(1, 11)]
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch(entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_shell_renders_the_first_days_and_announces_the_rest_one_by_one(self):
        html = self.client.get("/calendar?year=2026&month=7&endpoint=shows").text
        inline = [f"2026-07-{day:02d}" for day in range(1, calendar_routes.INITIAL_DAY_BLOCKS + 1)]
        later = [f"2026-07-{day:02d}" for day in range(calendar_routes.INITIAL_DAY_BLOCKS + 1, 11)]
        self.assertEqual(_day_sections(html), inline)
        # Every later day stands there as itself and fetches only itself, so no day
        # is fetched twice, none is missed, and one nobody reaches costs nothing.
        self.assertEqual(_placeholder_dates(html), later)
        self.assertEqual(
            _day_urls(html),
            [f"/calendar/day?endpoint=shows&year=2026&month=7&date={day}" for day in later])

    def test_a_placeholder_carries_what_the_page_needs_before_its_cards_exist(self):
        """Its heading, its anchor, the reserved height, and what this viewer would
        see of it — the day is a usable part of the page before it has loaded."""
        html = self.client.get("/calendar?year=2026&month=7&endpoint=shows").text
        block = _section(html, "2026-07-10")
        self.assertIn('data-date="2026-07-10"', block)
        self.assertIn('data-visible="1"', block)
        self.assertIn('data-held="1"', block)
        self.assertIn('hx-trigger="intersect once"', block)
        self.assertIn("Friday, 10 July", html)

    def test_the_search_box_types_unbounced_and_queues_behind_enter(self):
        """NOT DEBOUNCED and QUEUED, not replaced, on the box, the form and the
        widening button: measured in Chromium and Firefox, the debounce plus
        `replace` aborted the services' request on every one of 64 type-then-Enter
        trials each, and this shape lost none. A server test cannot run htmx, so
        this pins the attributes the measurement was made with."""
        html = self.client.get("/calendar?year=2026&month=7").text
        box = re.search(r'<input[^>]*id="searchInput"[^>]*>', html, re.S).group(0)
        form = re.search(r'<form[^>]*hx-get="/calendar/search"[^>]*>', html, re.S).group(0)
        widen = re.search(r'<button[^>]*hx-get="/calendar/search"[^>]*>',
                          self.client.get("/calendar/search?q=show").text, re.S).group(0)
        self.assertIn('hx-trigger="input changed"', box)
        for element in (box, form, widen):
            with self.subTest(element=element[:40]):
                self.assertIn('hx-sync="#searchResults:queue last"', element)

    def test_the_search_box_has_a_clear_button_right_after_it(self):
        """A RESET button, which empties the box with no script, asking the typed
        route with an empty query, which answers with the resting hint — in the
        box's queue. It is the box's next sibling because the stylesheet hides it
        while the box is empty with `input:placeholder-shown + .search-clear`.
        MEASURED in both engines at 1280 and 390: one request (`q=`), box and
        list emptied, no console errors."""
        html = self.client.get("/calendar?year=2026&month=7").text
        clear = re.search(r'id="searchInput"[^>]*>\s*(<button[^>]*>)', html, re.S).group(1)
        self.assertIn('type="reset"', clear)
        self.assertIn('class="search-clear"', clear)
        self.assertIn('hx-get="/calendar/search"', clear)
        self.assertIn('''hx-vals='{"q": ""}\'''', clear)
        self.assertIn('hx-include="#searchEndpoint"', clear)
        self.assertIn('hx-sync="#searchResults:queue last"', clear)
        self.assertIn("Type a title to find it",
                      self.client.get("/calendar/search?q=&endpoint=shows").text)

    def test_the_shell_still_states_the_whole_months_numbers(self):
        """The tiles, the chip strip and the is-new answer are claims about the
        month, so they must not shrink to the days that happen to be rendered."""
        html = self.client.get("/calendar?year=2026&month=7").text
        self.assertEqual(_stat(html, "statTotal"), "10")
        # A chip for every day that has something on it, including the un-rendered ones.
        self.assertIn('href="#day-2026-07-10"', html)

    def test_a_month_that_fits_asks_for_nothing(self):
        with patch("app.calendar.cache.fetch_window_records",
                        window_fetch([_entry("solo", "Solo Show", "2026-07-04T20:00:00Z")])):
            html = self.client.get("/calendar?year=2026&month=7").text
        self.assertEqual(_day_sections(html), ["2026-07-04"])
        self.assertEqual(_day_urls(html), [])

    def test_every_later_day_carries_ONE_TOKEN_naming_the_shells_is_new_answer(self):
        """The shell's diff commits this load as the next baseline, so a day that
        fetches itself later cannot work out which of its cards are new. The
        answer is FILED and every placeholder carries the token naming it — one
        short opaque word, the same one on each, rather than the answer itself
        spread across thirty addresses as data in a GET."""
        user_id = self._make_user("shell_newcomer")
        self.sign_in_as(user_id)
        asyncio.run(calendar_state.set_view_state(
            user_id, "shows", 2026, 7, last_count=9,
            last_show_ids=[_mark_key(f"show-{day}") for day in range(1, 10)], history=[]))
        html = self.client.get("/calendar?year=2026&month=7&endpoint=shows").text
        urls = {re.search(r"date=([\d-]+)", url).group(1): url for url in _day_urls(html)}
        tokens = {re.search(r"&new=([^&]+)", url).group(1) for url in urls.values()}
        self.assertEqual(len(tokens), 1, urls)
        # Nothing about a card is in it — that is the whole point of a reference.
        self.assertNotIn("show-10", tokens.pop())

    def test_a_month_with_nothing_new_files_nothing_and_carries_no_token(self):
        """Much the commonest case, because every load commits the baseline the
        next one is diffed against. No answer is not worth a key."""
        user_id = self._make_user("shell_returner")
        self.sign_in_as(user_id)
        asyncio.run(calendar_state.set_view_state(
            user_id, "shows", 2026, 7, last_count=10,
            last_show_ids=[_mark_key(f"show-{day}") for day in range(1, 11)], history=[]))
        html = self.client.get("/calendar?year=2026&month=7&endpoint=shows").text
        for url in _day_urls(html):
            self.assertNotIn("new=", url)



class TheJumpAnchorIsRenderedByTheServerTests(CalendarRouteTestCase):
    """Landing on a searched card needs no script, and this is what makes that
    true.

    THE SHAPE OF THE OLD BUG. A card lives in a day block, and every day past
    the first few ships as a placeholder that fetches itself when scrolled to.
    A browser scrolls to a fragment only if the element is in the document when
    it parses the page — so the card was not there, script had to wait for its
    day to arrive, and then keep correcting as the days ABOVE it swapped short
    placeholders for tall blocks. It landed at the top of the right day, or
    several days early, depending on how much grew above it.

    THE FIX IS A SERVER DECISION: mark the card the link named, so the browser's
    own fragment scrolling has something to reach.
    """

    MONTH = "/calendar?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("jumper"))
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([_entry("the-drama", "The Drama",
                                             "2026-07-15T20:00:00Z")]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mark(self):
        """The card's own identity, asked of the search rather than spelled out
        here — the two must agree or the anchor names nothing."""
        found = self.client.get("/calendar/search?endpoint=shows&q=drama")
        match = re.search(r"highlight=([^\"&#]+)", found.text)
        self.assertIsNotNone(match, found.text[:300])
        return match.group(1)

    def test_the_named_card_carries_the_anchor(self):
        self.client.get(self.MONTH)
        resp = self.client.get(f"{self.MONTH}&highlight={self._mark()}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="jump-target"', resp.text)

    def test_no_card_is_marked_when_nothing_was_asked_for(self):
        """Which is every ordinary load, so the anchor must not appear on one —
        an `id` on a page nobody jumped to would move the scroll position of a
        plain visit."""
        resp = self.client.get(self.MONTH)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('id="jump-target"', resp.text)

    def test_an_unknown_card_marks_nothing_rather_than_guessing(self):
        """A stale link names a card this month no longer holds. Marking the
        nearest thing would send a reader somewhere confidently wrong."""
        resp = self.client.get(f"{self.MONTH}&highlight=trakt%3Ano-such-title")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('id="jump-target"', resp.text)

    def test_the_month_stays_in_date_order_around_the_day_it_ships_early(self):
        """THE DEFECT THIS PINS: the jump's day was rendered with its cards and
        APPENDED to the ones that already were, so a jump to the 28th put that day
        between the 5th and the 6th — the reader landed on the right card with the
        rest of the month loading BELOW it, out of sequence.

        <main> IS THE MONTH. Which days arrive with their cards is a property of
        each day, not of which list it came out of."""
        month = [_entry(f"show-{day}", f"Show {day}", f"2026-07-{day:02d}T20:00:00Z")
                 for day in range(1, 29)]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(month)):
            self.client.get(self.MONTH)
            html = self.client.get(
                f"{self.MONTH}&highlight={_mark_key('show-28')}").text
        # Every day of the month, in order, whether drawn or still a placeholder.
        drawn = re.findall(r'id="day-(\d{4}-\d{2}-\d{2})"', html)
        self.assertEqual(drawn, sorted(drawn), "the month is out of date order")
        self.assertEqual(len(drawn), 28)
        # The jump's day is drawn with its cards; the days before it are not.
        self.assertIn("2026-07-28", _day_sections(html))
        self.assertIn("2026-07-20", _placeholder_dates(html))

    def test_exactly_one_card_carries_it_however_often_the_title_airs(self):
        """`highlight` names a TITLE and a title airs as often as it airs — twice
        in one day on the all-episodes calendar. Every card for it used to be
        given the same id, and a document holding an id twice sends the browser to
        whichever came first rather than to the one that was searched for."""
        twice_a_day = [_entry("the-drama", "The Drama", "2026-07-15T20:00:00Z"),
                       _entry("the-drama", "The Drama", "2026-07-15T21:30:00Z"),
                       _entry("the-drama", "The Drama", "2026-07-22T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(twice_a_day)):
            self.client.get(self.MONTH)
            html = self.client.get(f"{self.MONTH}&highlight={self._mark()}").text
        self.assertEqual(html.count('id="jump-target"'), 1, "the anchor is drawn twice")
        # And it is on the FIRST airing's day, so a reader lands where the month
        # starts showing the title rather than at some later repeat: the anchor
        # falls between the 15th's section and the 22nd's.
        self.assertLess(html.index(_section(html, "2026-07-15")),
                        html.index('id="jump-target"'))
        self.assertLess(html.index('id="jump-target"'),
                        html.index(_section(html, "2026-07-22")))

    def test_two_airings_of_one_title_in_one_month_are_two_destinations(self):
        """MEASURED BEFORE THE FIX, on a copy of the owner's data: a search for a
        weekly show drew four August rows and all four addressed the same page,
        landing on the 6th. The row knows which airing it is about — its day —
        and now says so; the route anchors THAT day's card."""
        weekly = [_entry("the-drama", "The Drama", "2026-07-08T20:00:00Z"),
                  _entry("the-drama", "The Drama", "2026-07-22T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(weekly)):
            self.client.get(self.MONTH)
            found = self.client.get("/calendar/search?endpoint=shows&q=drama").text
            links = sorted({link.replace("&amp;", "&") for link in
                            re.findall(r'href="(/calendar\?[^"]*highlight[^"]*)"', found)})
            self.assertEqual(len(links), 2, links)
            for link, day, other in ((links[0], "2026-07-08", "2026-07-22"),
                                     (links[1], "2026-07-22", "2026-07-08")):
                self.assertIn(f"day={day}", link)
                html = self.client.get(link).text
                anchor = html.index('id="jump-target"')
                self.assertLess(html.index(_section(html, day)), anchor, link)
                if day < other:
                    self.assertLess(anchor, html.index(_section(html, other)), link)
            # A link that names no day — an old bookmark — keeps landing on the
            # first airing, which is where every link used to land.
            html = self.client.get(f"{self.MONTH}&highlight={self._mark()}").text
            self.assertLess(html.index('id="jump-target"'),
                            html.index(_section(html, "2026-07-22")))

    def test_a_jump_to_a_title_hidden_by_the_viewers_own_switch_still_lands(self):
        """MEASURED BEFORE THE FIX in Chromium and Firefox: a search result for a
        title marked not-watching, with hiding on, left the reader at the top of
        the month — the anchor was drawn and then hidden, so the browser had
        nowhere to scroll. The stylesheet now exempts the anchor (and its day)
        from hiding, which a string test cannot assert; what the SERVER owes is
        that the search still offers it, the card is still the anchor and still
        marked — a jump must not edit the reader's own statement about the show —
        and its day's chip still offers to scroll there, since the chip sits
        above <main> where no selector reaches."""
        self.client.get(self.MONTH)
        mark = self._mark()
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": mark.replace("%3A", ":"), "not_watching": True})
        self.assertEqual(self._mark(), mark, "the search stopped offering a hidden title")
        html = self.client.get(f"{self.MONTH}&highlight={mark}&day=2026-07-15").text
        card = re.search(r'<[^>]*id="jump-target"[^>]*>', html).group(0)
        self.assertIn("not-watching", card)
        self.assertRegex(html, r'<a class="day-chip"[^>]*id="day-chip-2026-07-15"')
        # And an ordinary load of the same month greys that day, as before.
        plain = self.client.get(self.MONTH).text
        self.assertRegex(plain, r'<a class="day-chip unreachable"[^>]*id="day-chip-2026-07-15"')


class FollowingASearchResultWritesOneAiringTests(CalendarRouteTestCase):
    """`/calendar/jump` — a result's season picker submitted, and the only thing
    that writes.

    WHY A WRITE HAPPENS AT ALL. A service can be missing its own title: Trakt's
    show record dates Half Man's first season to 2026-04-28T20:00Z and Trakt's
    premieres calendar for that week does not list it. So a result could name a
    month, the month would fill correctly, and there would still be no such card.

    WHY IT HAPPENS HERE AND NOT IN THE SEARCH. The write first ran over every row
    a search returned, which turned typing "traitors" and pressing Enter into
    sixteen premieres written to a calendar everybody on the instance shares.
    The picker is the act that names what is written — and calendar/search.py's
    `jump` (tested there) writes exactly that.

    THE ADDRESS NAMES A RESULT BY REFERENCE: the query, the medium, the search's
    own reference and its place, and the seasons ticked. Nothing about the title.
    """

    JUMP = ("/calendar/jump?q=half%20man&media=show&i=0&also=trakt"
            "&season:1=1&season:3=1&endpoint=shows")

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("jumper2"))

    def _go(self, url=None, *, hit="found", destination="/calendar?year=2026&month=4"):
        from app.providers import title_search

        self.named = []
        self.jumped = []

        async def _resolve(settings, media, query, index, ref):
            self.named.append((media, query, index, ref))
            return SimpleNamespace(title="Half Man") if hit else None

        async def _jump(settings, found, media, seasons, *, tz, now):
            self.jumped.append((media, seasons))
            return destination

        with patch.object(title_search, "resolve_hit", _resolve), \
             patch("app.calendar.search.jump", _jump):
            return self.client.get(url or self.JUMP, follow_redirects=False)

    def test_it_re_reads_the_named_result_and_jumps_to_what_was_ticked(self):
        resp = self._go()
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/calendar?year=2026&month=4")
        (media, query, index, ref), = self.named
        self.assertEqual((str(media), query, index, ref.also), ("show", "half man", "0", ("trakt",)))
        self.assertEqual(self.jumped, [(self.jumped[0][0], [1, 3])])

    def test_a_films_one_choice_is_its_release(self):
        self._go("/calendar/jump?q=heat&media=movie&i=0&season:=1&endpoint=movies")
        self.assertEqual(self.jumped[0][1], [None])

    def test_a_result_the_store_no_longer_holds_opens_the_calendar_and_writes_nothing(self):
        resp = self._go(hit=None)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/calendar?endpoint=shows")
        self.assertEqual(self.jumped, [])

    def test_a_jump_with_nowhere_to_land_still_opens_the_calendar(self):
        """The month is worth opening either way; with nothing ticked there is no
        month, so the reader stays on the calendar they were searching."""
        resp = self._go(destination="")
        self.assertEqual(resp.headers["location"], "/calendar?endpoint=shows")

    def test_a_medium_this_app_does_not_know_is_not_an_error(self):
        resp = self._go("/calendar/jump?q=x&media=album&i=0&endpoint=shows")
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.jumped, [])


class ASearchResultsSeasonPickerTests(CalendarRouteTestCase):
    """`/calendar/fragments/search-seasons` — the app's one season checklist for
    a calendar search result, whose submit is the jump."""

    URL = "/calendar/fragments/search-seasons?q=half%20man&media=show&i=0&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.sign_in_as(self._make_user("picker2"))

    def _get(self, *, hit=True, options=()):
        from app.calendar import search as calendar_search
        from app.providers import title_search

        async def _resolve(settings, media, query, index, ref):
            return SimpleNamespace(title="Half Man") if hit else None

        async def _picker(settings, found, media, *, prefs, tz, source_selection=None):
            return calendar_search.Picker(list(options), [])

        with patch.object(title_search, "resolve_hit", _resolve),              patch("app.calendar.search.picker", _picker):
            return self.client.get(self.URL).text

    def test_it_draws_the_checklist_whose_submit_is_the_jump(self):
        from app.providers.season_choices import SeasonOption
        html = self._get(options=[SeasonOption(1), SeasonOption(2, suggested=True),
                                  SeasonOption(3, blocked="your filters hide it")])
        self.assertIn('action="/calendar/jump#jump-target"', html)
        self.assertIn('method="get"', html)
        for field in ('name="q" value="half man"', 'name="i" value="0"',
                      'name="media" value="show"', 'name="endpoint" value="shows"'):
            self.assertIn(field, html)
        # The suggestion arrives ticked; a refused season says why and cannot be.
        self.assertIn('name="season:2" value="1" checked', html)
        self.assertIn('name="season:3" value="1" disabled', html)
        self.assertIn("your filters hide it", html)

    def test_a_result_the_store_no_longer_holds_says_so(self):
        self.assertIn("That search has expired", self._get(hit=False))


class CalendarSearchRouteTests(CalendarRouteTestCase):
    """The search route: a fragment, and one that spends nothing unless asked.

    THE TYPING PATH IS THE ONE THAT MATTERS HERE. It runs while somebody is still
    typing, so it may not reach a service — and the autouse network guard in
    tests/conftest.py is what would catch it if it did.
    """

    # THE ENDPOINT IS NAMED, because the search is scoped to one calendar now:
    # a search is nearly always about the page in front of somebody, and the
    # five endpoints are five different questions.
    URL = "/calendar/search?endpoint=shows"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("search_viewer")
        self.sign_in_as(self.user_id)
        self.drama = _entry("the-drama", "The Drama", "2026-07-15T20:00:00Z")
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([self.drama]))
        patcher.start()
        self.addCleanup(patcher.stop)
        # One page load, so the month is stored for the search to find.
        self.client.get("/calendar?year=2026&month=7&endpoint=shows")

    def test_it_finds_a_stored_title_and_offers_a_jump(self):
        resp = self.client.get(f"{self.URL}&q=drama")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("The Drama", resp.text)
        # ANCHORED ON THE CARD, not the day: `highlight=` is what the calendar
        # route marks and ships the day for, and `#jump-target` is what the
        # browser scrolls to on its own.
        self.assertIn("highlight=", resp.text)
        self.assertIn("#jump-target", resp.text)
        self.assertIn("On All Episodes", resp.text)

    def test_it_is_a_fragment_and_not_a_page(self):
        """The shell embeds this partial and the typing path swaps it in, so a
        result cannot look different depending on how it was asked for."""
        resp = self.client.get(f"{self.URL}&q=drama")
        for chrome in ("<html", "<header", 'id="statsBar"', "calendarViewData"):
            self.assertNotIn(chrome, resp.text)

    def test_typing_never_asks_a_service(self):
        """`live` is absent, so the catalogue half must not run at all — not even
        to discover there is nothing to add."""
        with patch("app.calendar.search.catalogue",
                   side_effect=AssertionError("typing reached a service")):
            resp = self.client.get(f"{self.URL}&q=drama")
        self.assertEqual(resp.status_code, 200)

    def test_a_query_matching_nothing_says_so_and_offers_the_services(self):
        resp = self.client.get(f"{self.URL}&q=nothingmatchesthis")
        # IT NAMES THE CALENDAR IT SEARCHED, because "nothing matches" is a
        # different claim depending on which one was asked.
        self.assertIn("Nothing on All Episodes", resp.text)
        # ...and offers the widening this fragment is the right place for.
        # Asking the SERVICES is the panel's own always-visible Search button,
        # not a control the results repeat.
        self.assertIn("Search every calendar", resp.text)

    def test_no_tmdb_token_says_so_and_still_offers_a_linked_service(self):
        """Nothing is asked in TMDB's place; the box names it and offers the
        services the viewer linked, each asked only when its button is pressed."""
        from app import db
        from app.providers import title_search
        # The viewer has linked Trakt, which is what makes it theirs to add.
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=self.user_id, provider="trakt", provider_user_id=1,
            access_token="viewer-trakt-token")))
        asked = []

        async def _catalogue(entries, settings, media, query, *, pages=1, cached_only=False):
            asked.append([str(entry.source) for entry in entries])
            return title_search.SearchMergeResult(hits=[], failed=frozenset())
        with patch.object(title_search, "search_catalogue", _catalogue):
            plain = self.client.get(f"{self.URL}&q=drama&live=1").text
            added = self.client.get(f"{self.URL}&q=drama&live=1&also=trakt").text
        self.assertIn("is not configured", plain)
        self.assertIn(">Also search Trakt<", plain)
        self.assertEqual(asked, [[], ["trakt"]])
        self.assertIn("is not configured", added)

    def test_an_empty_query_asks_for_one_rather_than_listing_everything(self):
        resp = self.client.get(f"{self.URL}&q=")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("The Drama", resp.text)

    def test_it_needs_a_signed_in_viewer(self):
        """The results are one viewer's own calendar, filtered by their own
        preferences, so this is gated exactly as the calendar is."""
        self.client.cookies.clear()
        self.assertIn(self.client.get(f"{self.URL}&q=drama").status_code,
                      (302, 303, 401, 403))

class CalendarDayRouteTests(CalendarRouteTestCase):
    """The content route: ONE day's block, and nothing else."""

    DAY = "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-15"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("day_viewer")
        self.sign_in_as(self.user_id)
        self.drama = _entry("the-drama", "The Drama", "2026-07-15T20:00:00Z")
        self.drama["show"]["genres"] = ["drama"]
        self.comedy = _entry("the-comedy", "The Comedy", "2026-07-16T20:00:00Z")
        self.comedy["show"]["genres"] = ["comedy"]
        # Outside the requested span, so a route that read the whole month and
        # forgot to trim would be caught.
        self.early = _entry("the-early", "The Early", "2026-07-02T20:00:00Z")
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([self.early, self.drama, self.comedy]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_returns_only_the_requested_day_as_a_bare_day_block(self):
        resp = self.client.get(self.DAY)
        self.assertEqual(resp.status_code, 200)
        # Only the 15th: the 2nd and the 16th are in the same cached window and a
        # route that assembled the month and forgot to trim would ship them too.
        self.assertEqual(_day_sections(resp.text), ["2026-07-15"])
        self.assertNotIn("The Comedy", resp.text)
        # A fragment, not a page: nothing the shell already owns comes back with it.
        for chrome in ("<html", "<header", 'id="statsBar"', "day-chips", "calendarViewData"):
            self.assertNotIn(chrome, resp.text)

    def test_a_day_that_arrives_late_is_the_same_markup_as_an_inline_one(self):
        """Both render through the one card partial, which is what keeps a day that
        arrived late indistinguishable from one that shipped with the page."""
        # THE SAME CALENDAR AS THE DAY: a card's details address names the
        # calendar it is on, so a card from another calendar is not the same
        # markup and should not be.
        inline = self.client.get("/calendar?endpoint=shows&year=2026&month=7").text
        fragment = self.client.get(self.DAY).text
        card = re.search(
            r'(<div class="card[^>]*data-id="%s".*?)</div>\s*</div>\s*</section>'
            % re.escape(_mark_key("the-drama")), fragment, re.S)
        self.assertIsNotNone(card)
        # The Drama is inline on the full-month shell above (only three days have
        # items), so the same card markup must appear in both responses.
        self.assertIn(card.group(1)[:400], inline)

    def test_a_day_with_nothing_on_it_renders_nothing(self):
        """Its placeholder is replaced by the empty response, so the day simply
        disappears — which is what an empty day should look like."""
        resp = self.client.get(
            "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-20")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_day_sections(resp.text), [])
        self.assertEqual(resp.text.strip(), "")

    def _exclude_drama(self):
        return self.client.post("/calendar/filters", json={
            vocab.field_name(vocab.TV_GENRES, "drama"): "exclude"})

    def test_it_applies_this_viewers_saved_filters(self):
        self._exclude_drama()
        self.assertNotIn("The Drama", self.client.get(self.DAY).text)

    def test_the_query_cannot_widen_or_change_the_filters(self):
        """The filters are the viewer's, read from their session. A query
        parameter naming them would let one link ask for an unfiltered day —
        or for somebody else's view of it."""
        self._exclude_drama()
        text = self.client.get(self.DAY + "&genres=&countries=&network_filter=").text
        self.assertNotIn("The Drama", text)

    def test_not_watching_is_rendered_by_the_server(self):
        asyncio.run(calendar_state.set_not_watching(self.user_id, "the-drama", True))
        self.assertIn("not-watching", _card_class(self.client.get(self.DAY).text, _mark_key("the-drama")))

    def test_a_late_day_draws_is_new_from_the_answer_the_token_names(self):
        """is-new is a whole-month diff the shell makes and commits, so a day
        fetched afterwards cannot make it again — re-reading the baseline would
        find the shell's own commit and conclude nothing is new. The shell FILES
        its answer and the day resolves the token naming it."""
        drama = _mark_key("the-drama")
        token = asyncio.run(calendar_routes._remember_new_marks(
            self.user_id, "shows", 2026, 7, {drama}))
        marked = self.client.get(f"{self.DAY}&new={token}").text
        self.assertIn("is-new", _card_class(marked, drama))
        # Re-reading the baseline is not how it decides: with no token, nothing is
        # marked, whatever the stored baseline says.
        asyncio.run(calendar_state.set_view_state(
            self.user_id, "shows", 2026, 7,
            last_count=1, last_show_ids=["something-else"], history=[]))
        self.assertNotIn("is-new", _card_class(self.client.get(self.DAY).text, drama))

    def test_a_token_naming_nothing_marks_nothing(self):
        """Expired, invented, or simply never written: a missing answer is no
        marks, which is what a late day did before one was carried at all —
        honest rather than wrong."""
        text = self.client.get(f"{self.DAY}&new=not-a-real-token").text
        self.assertIn("The Drama", text)
        self.assertNotIn("is-new", text)

    def test_one_accounts_token_reads_nothing_for_another(self):
        """The answer says which titles were new to ONE account, so the user is in
        the key as well as the token — a token copied out of somebody else's page
        resolves to nothing rather than to their answer."""
        drama = _mark_key("the-drama")
        stranger = self._make_user("is_new_stranger")
        token = asyncio.run(calendar_routes._remember_new_marks(
            stranger, "shows", 2026, 7, {drama}))
        self.assertNotIn("is-new", self.client.get(f"{self.DAY}&new={token}").text)

    def test_an_is_new_key_naming_nothing_on_the_day_marks_nothing(self):
        """The day's cards ask whether THEIR key is among the answer, so a key for
        a title on another day marks no card here."""
        token = asyncio.run(calendar_routes._remember_new_marks(
            self.user_id, "shows", 2026, 7, {_mark_key("the-comedy")}))
        text = self.client.get(f"{self.DAY}&new={token}").text
        self.assertIn("The Drama", text)
        self.assertNotIn("is-new", text)

    def test_a_failed_day_keeps_the_token_for_the_retry(self):
        """The retry re-requests the day's own address, so the token has to
        survive a failure — and it is the whole of what a retry carries forward
        now, which is one opaque word instead of a list of keys rebuilt into the
        page."""
        token = asyncio.run(calendar_routes._remember_new_marks(
            self.user_id, "shows", 2026, 7, {_mark_key("the-drama")}))

        def fake(endpoint, start):
            raise TraktError("Trakt unreachable", 503)

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            text = self.client.get(f"{self.DAY}&new={token}").text
        self.assertIn(f"&amp;new={token}", text)

    def test_a_retry_writes_back_nothing_a_hand_edited_address_invented(self):
        """The retry button is built from the address, so what the address carries
        is rendered into the page. A token is quoted into it and nothing else is
        carried at all."""
        with patch("app.calendar.cache.fetch_window_records",
                   window_fetch(lambda endpoint, start: (_ for _ in ()).throw(
                       TraktError("Trakt unreachable", 503)))):
            text = self.client.get(f"{self.DAY}&new=%3Cb%3Ehello").text
        self.assertNotIn("<b>hello", text)

    def test_a_bad_date_is_refused_before_it_reaches_the_cache(self):
        for bad in ("date=nope",
                    "",                       # no date at all
                    "date=2026-06-10",        # outside the viewed month
                    "date=2026-08-10",
                    "date=2026-07-10T00:00:00",
                    "date=2026-W28-1"):       # a week date, not YYYY-MM-DD
            with self.subTest(query=bad):
                resp = self.client.get(f"/calendar/day?endpoint=shows&year=2026&month=7&{bad}")
                self.assertEqual(resp.status_code, 400, resp.text)

    def test_an_unknown_endpoint_key_falls_back_instead_of_being_used(self):
        resp = self.client.get(
            "/calendar/day?endpoint=../../etc/passwd&year=2026&month=7&date=2026-07-15")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_day_sections(resp.text), ["2026-07-15"])

    def test_a_day_that_fails_says_so_and_offers_to_try_again(self):
        """The rest of the month is already on screen and correct, so one day that
        couldn't be loaded is a gap, not a broken page — and it must not sit there
        looking like it is still loading."""
        def fake(endpoint, start):
            raise TraktError("Trakt unreachable", 503)

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            resp = self.client.get(self.DAY)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("warning-banner", resp.text)
        self.assertNotIn("error-banner", resp.text)
        # Still the same day, still addressable, and the retry asks for exactly it.
        self.assertIn('id="day-2026-07-15"', resp.text)
        self.assertIn("Wednesday, 15 July", resp.text)
        self.assertIn('hx-get="/calendar/day?endpoint=shows&amp;year=2026&amp;month=7'
                      '&amp;date=2026-07-15"', resp.text)

    def test_one_failed_window_still_returns_the_items_that_loaded(self):
        """A day near a window boundary is assembled from two windows, since a
        viewer-local day can straddle them. One failing leaves a day that is
        short and says so, rather than a day that is missing."""
        # 13 July sits at a boundary: its own window plus the one before it.
        straddling = date(2026, 7, 13)
        boom = calendar_cache.window_start(date(2026, 7, 6))
        self.assertNotEqual(calendar_cache.window_start(straddling), boom)

        def fake(endpoint, start):
            if start == boom:
                raise TraktError("Trakt unreachable", 503)
            return [_entry("the-late", "The Late", "2026-07-13T20:00:00Z")]

        with patch("app.calendar.cache.fetch_window_records", window_fetch(fake)):
            resp = self.client.get(
                "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-13")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("The Late", resp.text)
        self.assertIn("warning-banner", resp.text)

    def test_the_route_needs_a_signed_in_calendar_account(self):
        self.client.cookies.clear()
        resp = self.client.get(self.DAY, follow_redirects=False)
        self.assertNotEqual(resp.status_code, 200)


class HeaderPaintStabilityTests(CalendarRouteTestCase):
    """The header has to be the same shape in the first paint as it is a second
    later. Both things below used to land after paint and shove it sideways on a
    load where the assets were not already cached."""

    PAGE = "/calendar?year=2026&month=7&endpoint=shows"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("header_viewer")
        self.sign_in_as(self.user_id)
        patcher = patch("app.calendar.cache.fetch_window_records", window_fetch([]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_stylesheet_is_requested_before_the_font_preloads(self):
        """The stylesheet is the only thing gating first paint. Behind ~86 KB of
        high-priority font preloads it competes for the connection pool on an
        uncached load, and the page can paint before any of it applies."""
        html = self.client.get(self.PAGE).text
        head = html[:html.index("</head>")]
        self.assertLess(head.index("/static/css/style.css"),
                        head.index("/static/fonts/"))

    def test_the_brand_wordmark_reserves_its_box_in_the_markup(self):
        """With no intrinsic size in the markup the element is zero-wide until it
        downloads and everything beside it then shifts. The header carries the
        WORDMARK now rather than the square mark, which makes this matter more,
        not less: it reserves 175px rather than 30, so getting it wrong moves the
        month heading and the view control most of a column. The number must
        track the file's own box — 565x110 at the 34px height the stylesheet
        draws it at — or the reservation is wrong in the other direction."""
        html = self.client.get(self.PAGE).text
        self.assertRegex(html, r'<img class="brand-wordmark"[^>]*\swidth="175"[^>]*>')
        self.assertRegex(html, r'<img class="brand-wordmark"[^>]*\sheight="34"[^>]*>')

    def test_the_wordmark_links_home(self):
        """A site's name in a header is the one thing everybody already expects to
        be clickable, and this is the only place the product is named on a page a
        signed-in person actually visits."""
        html = self.client.get(self.PAGE).text
        self.assertRegex(html, r'<a class="brand-home" href="/calendar"')

    def test_the_head_decides_the_optional_nav_link_before_the_body_is_parsed(self):
        """The deciding script must be inline and ahead of the deferred bundle —
        deferred means after first paint, which is the shift being prevented."""
        html = self.client.get(self.PAGE).text
        decide = html.index("classList.add('has-distrakt')")
        self.assertLess(decide, html.index("/static/js/calendar/layout.js"))
        self.assertLess(decide, html.index("<body"))

    def test_the_optional_nav_link_is_not_shown_by_clearing_an_attribute(self):
        """It is gated by CSS off a class the head script sets. A `hidden` the
        deferred bundle clears is exactly the late reveal that shifted the bar."""
        html = self.client.get(self.PAGE).text
        link = re.search(r'<a id="distraktNav".*?</a>', html, re.S).group(0)
        self.assertNotIn("hidden", link)

    def test_the_markup_says_nothing_about_who_has_found_it(self):
        """Deciding this server-side would fix the shift too, and is why it was
        left in local storage: the response must not differ between an account
        that has used the easter egg and one that has not."""
        plain = self.client.get(self.PAGE).text
        finder = self._make_user("header_finder", distrakt_approved=True)
        self.sign_in_as(finder)
        self.assertIn('<a id="distraktNav"', self.client.get(self.PAGE).text)
        self.assertIn('<a id="distraktNav"', plain)


class TheCalendarDoesNotGoDarkOverOneServicesCredentialTests(CalendarRouteTestCase):
    """OBSERVED, 2026-08-19: blanking `trakt_client_id` emptied the calendar and
    said "Trakt API credentials aren't set yet" on an instance whose other source
    was reading months perfectly well.

    `calendar_sources` states outright that it does not ask `is_configured`,
    because a calendar read does not use a viewer's token — and
    `for_calendar_sources` put that filter straight back. The question belongs to
    the port, which is the only thing that knows what its own calendar costs.
    """

    def setUp(self):
        super().setUp()
        # NO TRAKT CREDENTIAL OF ANY KIND. Everything below is what an instance
        # reading its calendar from the credential-free source alone must still
        # get, and the fill is stubbed at the cache's own boundary so the read
        # path runs for real without a service being reached.
        save_settings(Settings())
        self.user_id = self._make_user("simkl_only_viewer")
        self.sign_in_as(self.user_id)
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch([_entry("show-a", "Show A", "2026-07-15T20:00:00Z")]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_month_still_renders_its_titles(self):
        resp = self.client.get("/?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Show A", resp.text)
        self.assertNotIn(calendar_routes.NOT_CONFIGURED, resp.text)

    def test_a_day_fragment_is_served_too(self):
        """The second of the three gates reading this answer, and it refuses with
        a 400 rather than a message — so a page that renders and days that do not
        would be a worse state than the outage it used to report."""
        resp = self.client.get("/calendar/day?year=2026&month=7&date=2026-07-15")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_the_refusal_that_remains_names_no_service(self):
        """It is reachable only with every source unavailable, and by then Trakt
        is not the reason — naming it sent an operator to the wrong settings
        field, which is the same fault as the gate it sat on."""
        self.assertNotIn("Trakt", calendar_routes.NOT_CONFIGURED)
        save_settings(Settings(simkl_public_calendar_enabled=False))
        # Compared on a fragment rather than the whole sentence: the template
        # escapes it on the way out, so the punctuation is not the same bytes.
        self.assertIn("No calendar source is available",
                      self.client.get("/?year=2026&month=7").text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheCoverageGapSwitchTests(unittest.TestCase):
    """_coverage_gap: a viewer who named one calendar source and asked for a month
    that source does not reach is told so, and offered a service that DOES fill
    calendars here, publishes this one and reaches the month.

    It used to offer whichever registered service came first, so a service that
    describes titles and publishes no calendar would have been the link — to an
    empty month with nothing saying why.
    """

    FAR = date.today().year + 3   # past Simkl's declared forward reach

    def _gap(self, registry=None):
        from app.endpoints import get_endpoint
        prefs = source_prefs.SourcePrefs(user_id=1, calendar_source="simkl")
        registry = registry or providers.registered()
        with patch.object(providers, "registered", lambda: registry):
            return calendar_routes._coverage_gap(
                prefs, self.FAR, 1, Settings(), endpoint=get_endpoint("shows/new"))

    def test_the_switch_names_a_service_that_reaches_the_month(self):
        message, url = self._gap()
        self.assertIn("doesn't reach", message)
        self.assertIn("source=trakt", url)

    def test_a_service_with_no_calendar_is_never_the_switch(self):
        describer = type("Describer", (), {
            "source": "mercury", "label": "Mercury", "calendar_port": None,
            "capabilities": Capabilities(endpoints=frozenset(), days_before=None,
                                         days_after=None, private_user_data=False)})()
        _message, url = self._gap({"mercury": describer, **providers.registered()})
        self.assertNotIn("mercury", url or "")
        self.assertIn("source=trakt", url)
