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
import dataclasses
import json
import re
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import auth, db, providers
from app.calendar import cache as calendar_cache, routes as calendar_routes
from app.calendar import state as calendar_state
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

    def test_a_new_account_starts_with_no_filters_at_all(self):
        """A filter removes shows without ever saying one exists, so it is not
        something an account inherits from the instance's configuration."""
        prefs = asyncio.run(auth.get_user_prefs(self.user1))
        self.assertEqual(prefs["genres"], "")
        self.assertEqual(prefs["countries"], "")
        self.assertEqual(prefs["show_certifications"], "")
        self.assertEqual(prefs["movie_certifications"], "")
        self.assertEqual(prefs["movie_release_countries"], "")
        self.assertEqual(prefs["movie_release_types"], "")
        self.assertEqual(prefs["network_filter"], [])

    def test_the_release_filter_round_trips_and_keeps_only_numbers(self):
        """The release types are stored as the numbers the service publishes,
        so a word is dropped on the way IN rather than stored as a preference
        that can never do anything — the read path already ignores one it
        cannot parse."""
        self.sign_in_as(self.user1)
        resp = self.client.post("/api/me/prefs", json={
            "movie_release_countries": " us , , -br ",
            "movie_release_types": "3, theatrical, -1, 3"})
        self.assertEqual(resp.status_code, 200, resp.text)
        prefs = self.client.get("/api/me/prefs").json()["prefs"]
        self.assertEqual(prefs["movie_release_countries"], "us, -br")
        self.assertEqual(prefs["movie_release_types"], "3, -1")

    def test_a_release_filter_does_not_narrow_a_show_calendar(self):
        """It is a films-only dimension, and the specs travel with every read —
        so the show calendars have to be untouched rather than merely
        unaffected by accident."""
        self.sign_in_as(self.user1)
        self.client.post("/api/me/prefs", json={
            "movie_release_countries": "zz", "movie_release_types": "5"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page)
        self.assertIn("The Comedy", page)

    def test_a_viewer_can_filter_shows_by_certification(self):
        """A per-user certification exclude behaves exactly like the existing
        genre/country filters: one viewer's calendar narrows, the other's does
        not, from the same cached window."""
        self.sign_in_as(self.user1)
        resp = self.client.post("/api/me/prefs", json={"show_certifications": "-tv-ma"})
        self.assertEqual(resp.status_code, 200, resp.text)
        prefs = self.client.get("/api/me/prefs").json()["prefs"]
        self.assertEqual(prefs["show_certifications"], "-tv-ma")
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
        resp = self.client.post("/api/me/prefs", json={"genres": "-comedy"})
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

    def test_a_non_admin_can_read_and_write_their_own_filters(self):
        """The whole point: no admin rights involved."""
        self.sign_in_as(self.user1)
        resp = self.client.post("/api/me/prefs", json={
            "genres": " drama , ", "countries": "us", "network_filter": "HBO, hbo , Netflix"})
        self.assertEqual(resp.status_code, 200, resp.text)

        prefs = self.client.get("/api/me/prefs").json()["prefs"]
        self.assertEqual(prefs["genres"], "drama")       # empty token dropped
        self.assertEqual(prefs["countries"], "us")
        # De-duplicated case-insensitively, keeping the spelling first given.
        self.assertEqual(prefs["network_filter"], ["HBO", "Netflix"])

    def test_the_network_filter_narrows_to_the_named_networks(self):
        self.sign_in_as(self.user1)
        self.client.post("/api/me/prefs", json={"network_filter": "HBO"})
        page = self.client.get("/?year=2026&month=7").text
        self.assertIn("The Drama", page)
        self.assertNotIn("The Comedy", page)

    def test_a_filter_can_be_cleared_again(self):
        """Present-but-empty has to mean "no filter" rather than "unchanged", or
        a filter could be set and never taken off."""
        self.sign_in_as(self.user1)
        self.client.post("/api/me/prefs", json={"genres": "-comedy"})
        self.client.post("/api/me/prefs", json={"genres": "", "network_filter": []})
        prefs = self.client.get("/api/me/prefs").json()["prefs"]
        self.assertEqual(prefs["genres"], "")
        self.assertEqual(prefs["network_filter"], [])
        self.assertIn("The Comedy", self.client.get("/?year=2026&month=7").text)

    def test_the_header_button_says_when_a_filter_is_narrowing_the_month(self):
        """A filter's only other evidence is the shows that aren't there, which
        looks exactly like Trakt not listing them."""
        self.sign_in_as(self.user1)
        unfiltered = self.client.get("/?year=2026&month=7").text
        self.assertIn('id="filtersBtn"', unfiltered)
        self.assertNotIn('id="filtersBtn" class="pill-btn active"', unfiltered)

        self.client.post("/api/me/prefs", json={"genres": "-comedy", "network_filter": "HBO"})
        filtered = self.client.get("/?year=2026&month=7").text
        self.assertIn('id="filtersBtn" class="pill-btn active"', filtered)
        self.assertIn("genre, network", filtered)

    def test_the_filter_endpoints_still_need_a_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/me/prefs").status_code, 401)
        self.assertEqual(
            self.client.post("/api/me/prefs", json={"genres": "drama"}).status_code, 401)


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
        self.assertEqual(resp.status_code, 200, resp.text)

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
            precedence={"default": "simkl"})))
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
        self.assertLess(row.index("useDeviceTimezone()"), row.index('id="tzSelect"'))

    def test_the_endpoint_picker_stays_out_of_the_view_panel(self):
        """THE GROUPING RULE, PINNED. The four controls behind 👁️ View answer
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

def _card_class(html: str, item_id: str) -> str:
    m = re.search(r'<div class="([^"]*)" data-id="%s"' % re.escape(item_id), html)
    return m.group(1) if m else ""


def _view_data(html: str) -> dict:
    """The JSON the page embeds for the client — the counts and the is-new set
    as DATA, not only as classes."""
    m = re.search(r'<script id="calendarViewData" type="application/json">(.*?)</script>',
                  html, re.S)
    return json.loads(m.group(1)) if m else {}


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
        data = _view_data(html)
        self.assertEqual(data["watching"], 2)
        self.assertEqual(data["notWatchingCount"], 2)
        # And the per-show card counts the client needs to move those numbers
        # through a toggle without counting the cards it happens to hold.
        self.assertEqual(data["showCounts"], {"show-a": 2, "show-b": 1, "show-c": 1})

    def test_a_marked_show_is_rendered_not_watching_rather_than_marked_after_paint(self):
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-a", "not_watching": True})
        html = self.client.get(self.PAGE).text
        self.assertIn("not-watching", _card_class(html, "show-a"))
        self.assertNotIn("not-watching", _card_class(html, "show-b"))
        self.assertEqual(_view_data(html)["notWatching"], ["show-a"])

    def test_a_first_look_at_a_month_marks_nothing_new(self):
        """No stored baseline means this view has never been seen — which is not
        the same as every show in it having just appeared."""
        html = self.client.get(self.PAGE).text
        for item_id in ("show-a", "show-b", "show-c"):
            self.assertNotIn("is-new", _card_class(html, item_id), item_id)
        self.assertEqual(_view_data(html)["newIds"], [])
        self.assertIn("(Initial Tracking)", html)

    def test_only_shows_missing_from_the_stored_baseline_come_back_new(self):
        self._seed_baseline(last_show_ids=["show-a"], last_count=1)
        html = self.client.get(self.PAGE).text
        self.assertNotIn("is-new", _card_class(html, "show-a"))
        self.assertIn("is-new", _card_class(html, "show-b"))
        self.assertIn("is-new", _card_class(html, "show-c"))
        self.assertEqual(_view_data(html)["newIds"], ["show-b", "show-c"])

    def test_the_render_commits_the_servers_full_id_list_as_the_next_baseline(self):
        """The committed list has to be the month's, not a page's: anything it
        leaves out reads as new on the next visit."""
        self.client.get(self.PAGE)
        stored = self._stored_baseline()
        self.assertEqual(stored["last_show_ids"], ["show-a", "show-b", "show-c"])
        self.assertEqual(stored["last_count"], 4)
        # Committed, so a second load of an unchanged month finds nothing new.
        second = self.client.get(self.PAGE).text
        self.assertEqual(_view_data(second)["newIds"], [])
        self.assertIn("Perfect Match", second)

    def test_a_month_that_could_not_be_loaded_leaves_the_baseline_alone(self):
        """Committing an empty month over a real baseline would make the whole
        month look new the next time it loads properly."""
        self._seed_baseline(last_show_ids=["show-a", "show-b", "show-c"], last_count=4)

        def boom(endpoint, start):
            raise TraktError("Trakt unreachable", 503)

        with patch("app.calendar.cache.fetch_window_records", window_fetch(boom)):
            resp = self.client.get(self.PAGE)
        self.assertIn("error-banner", resp.text)
        stored = self._stored_baseline()
        self.assertEqual(stored["last_show_ids"], ["show-a", "show-b", "show-c"])
        self.assertEqual(stored["last_count"], 4)

    def test_the_delta_line_reports_the_change_since_the_last_run(self):
        self._seed_baseline(last_show_ids=["show-a"], last_count=1)
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
    """A day arrives laid out. Working its width — and whether it shows at all —
    out on the client meant the header painted and then vanished, jumping the
    rest of the month up the page."""

    PAGE = "/calendar?year=2026&month=7&endpoint=shows"
    DAY = "/calendar/day?endpoint=shows&year=2026&month=7&date=2026-07-15"

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("layout_viewer")
        self.sign_in_as(self.user_id)
        entries = [_entry(f"show-{n}", f"Show {n}", "2026-07-15T20:00:00Z") for n in range(3)]
        entries.append(_entry("solo", "Solo", "2026-07-16T20:00:00Z"))
        patcher = patch("app.calendar.cache.fetch_window_records",
                        window_fetch(entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_day_ships_its_own_column_count(self):
        html = self.client.get(self.PAGE).text
        self.assertIn("--cols: 3", _section(html, "2026-07-15"))
        self.assertIn("--cols: 1", _section(html, "2026-07-16"))

    def test_the_column_count_is_capped_by_the_card_style(self):
        """"Poster beside" cards are wide, so a day never grows past two columns
        however many cards it holds."""
        self.client.post("/api/me/prefs", json={"card_style": "horizontal"})
        self.assertIn("--cols: 2", _section(self.client.get(self.PAGE).text, "2026-07-15"))

    def test_hiding_sizes_a_day_to_what_is_actually_visible(self):
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        for show in ("show-0", "show-1"):
            self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                             json={"item_id": show, "not_watching": True})
        self.assertIn("--cols: 1", _section(self.client.get(self.PAGE).text, "2026-07-15"))

    def test_a_day_with_nothing_left_to_show_arrives_collapsed(self):
        """Every item on 15 July marked, hiding on: the day renders no cards, so
        it must not paint its header first and collapse afterwards."""
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        for show in ("show-0", "show-1", "show-2"):
            self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                             json={"item_id": show, "not_watching": True})
        html = self.client.get(self.PAGE).text
        self.assertIn("is-empty-hidden", _section(html, "2026-07-15"))
        self.assertNotIn("is-empty-hidden", _section(html, "2026-07-16"))

    def test_a_day_that_arrives_late_is_laid_out_the_same_way(self):
        self.client.post("/api/me/prefs", json={"hide_not_watching": True})
        self.client.post("/api/state?year=2026&month=7&endpoint=shows",
                         json={"item_id": "show-0", "not_watching": True})
        fragment = self.client.get(self.DAY).text
        self.assertIn("--cols: 2", _section(fragment, "2026-07-15"))
        self.assertNotIn("is-empty-hidden", _section(fragment, "2026-07-15"))


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
        self.assertIn("--skeleton-rows: 1", block)
        self.assertIn('hx-trigger="intersect once"', block)
        self.assertIn("Friday, 10 July", html)

    def test_the_shell_still_states_the_whole_months_numbers(self):
        """The tiles, the chip strip and the is-new answer are claims about the
        month, so they must not shrink to the days that happen to be rendered."""
        html = self.client.get("/calendar?year=2026&month=7").text
        self.assertEqual(_stat(html, "statTotal"), "10")
        self.assertEqual(len(_view_data(html)["showCounts"]), 10)
        # A chip for every day that has something on it, including the un-rendered ones.
        self.assertIn('href="#day-2026-07-10"', html)

    def test_a_month_that_fits_asks_for_nothing(self):
        with patch("app.calendar.cache.fetch_window_records",
                        window_fetch([_entry("solo", "Solo Show", "2026-07-04T20:00:00Z")])):
            html = self.client.get("/calendar?year=2026&month=7").text
        self.assertEqual(_day_sections(html), ["2026-07-04"])
        self.assertEqual(_day_urls(html), [])


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
        inline = self.client.get("/calendar?year=2026&month=7").text
        fragment = self.client.get(self.DAY).text
        card = re.search(r'(<div class="card[^>]*data-id="the-drama".*?)</div>\s*</div>\s*</section>',
                         fragment, re.S)
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

    def test_it_applies_this_viewers_saved_filters(self):
        self.client.post("/api/me/prefs", json={"genres": "-drama"})
        self.assertNotIn("The Drama", self.client.get(self.DAY).text)

    def test_the_query_cannot_widen_or_change_the_filters(self):
        """The filters are the viewer's, read from their session. A query
        parameter naming them would let one link ask for an unfiltered day —
        or for somebody else's view of it."""
        self.client.post("/api/me/prefs", json={"genres": "-drama"})
        text = self.client.get(self.DAY + "&genres=&countries=&network_filter=").text
        self.assertNotIn("The Drama", text)

    def test_not_watching_is_rendered_by_the_server(self):
        asyncio.run(calendar_state.set_not_watching(self.user_id, "the-drama", True))
        self.assertIn("not-watching", _card_class(self.client.get(self.DAY).text, "the-drama"))

    def test_it_never_marks_is_new_itself(self):
        """is-new is a whole-month diff the shell already made and committed. A
        fragment that re-read the baseline would see that commit and mark nothing
        — so it does not try: the shell hands the ids to the page instead."""
        # A first look at the month commits a baseline; these shows are genuinely
        # new relative to a DIFFERENT stored baseline.
        asyncio.run(calendar_state.set_view_state(
            self.user_id, "shows", 2026, 7,
            last_count=1, last_show_ids=["something-else"], history=[]))
        shell = self.client.get("/calendar?year=2026&month=7&endpoint=shows").text
        self.assertIn("the-drama", _view_data(shell)["newIds"])
        # The fragment is fetched after that commit and marks nothing.
        fragment = self.client.get(self.DAY).text
        self.assertNotIn("is-new", _card_class(fragment, "the-drama"))

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
        not less: it reserves 254px rather than 30, so getting it wrong moves the
        month heading and the view control most of a column."""
        html = self.client.get(self.PAGE).text
        self.assertRegex(html, r'<img class="brand-wordmark"[^>]*\swidth="254"[^>]*>')
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
        self.assertLess(decide, html.index("/static/js/calendar/view.js"))
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


class SharePanelSourceControlTests(CalendarRouteTestCase):
    """The Sources control inside the Share panel — what a generated LINK may
    say about which services fill it, as against the toolbar control beside it,
    which says what THIS page shows and stores nothing.

    The two are drawn from one function for one reason: an instance where the
    toolbar has nothing to offer is an instance where the panel has nothing to
    offer either, and a dialog growing a control that can only say one thing
    would be advertising a choice this instance does not have.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("panel_viewer")
        self.sign_in_as(self.user_id)

    def _page(self, url: str = "/calendar?year=2026&month=7") -> str:
        entries = [_entry("show-a", "Show A", "2026-07-15T20:00:00Z")]
        with patch("app.calendar.cache.fetch_window_records", window_fetch(entries)):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def _options(self, html: str) -> list[str]:
        block = re.search(r'<select id="share_view_source".*?</select>', html, re.S)
        self.assertIsNotNone(block, "the Share panel offers no source control")
        return re.findall(r'<option value="([^"]*)"', block.group(0))

    def test_it_offers_my_sources_and_one_entry_per_service(self):
        """"Every service" is deliberately absent: a link narrows the owner's
        own calendar and can never widen it, so that option would either mean
        exactly what the first one means or mean something the page will not
        honour."""
        self.assertEqual(self._options(self._page()), ["", "trakt", "simkl"])

    def test_a_calendar_one_service_publishes_gets_no_control_at_all(self):
        html = self._page("/calendar?year=2026&month=7&endpoint=shows/finales")
        self.assertNotIn('id="share_view_source"', html)
        self.assertIn('id="share_view_endpoint"', html)

    def test_a_service_switched_off_for_the_instance_leaves_nothing_to_choose(self):
        """One service left, so the panel says nothing about sources — by the
        same admission that empties the toolbar, not by a second count of it."""
        save_settings(dataclasses.replace(
            _configured_settings(), simkl_public_calendar_enabled=False))
        html = self._page()
        self.assertNotIn('id="share_view_source"', html)
        self.assertNotIn('id="sourceSelect"', html)

    def test_it_sits_inside_the_panel_s_own_options_block(self):
        """It is a property of the LINK, so it belongs with the other options
        the link carries rather than beside the link box itself."""
        html = self._page()
        block = html[html.index('id="share_view_options"'):]
        block = block[:block.index('class="modal-foot"')]
        self.assertIn('id="share_view_source"', block)

    def test_nothing_is_preselected_from_the_calendar_being_looked_at(self):
        """Which option a link is on is stored per link and set from that. A
        server-marked selection would fight the panel's own render and could
        write a source into a link whose owner never chose one."""
        block = re.search(r'<select id="share_view_source".*?</select>',
                          self._page("/calendar?year=2026&month=7&source=simkl"), re.S)
        self.assertNotIn("selected", block.group(0))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
