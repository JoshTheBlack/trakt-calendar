"""Gating + wiring tests for the main calendar route (app/main.py's `/` and
`/api/state`, `/api/me/prefs`, `/api/me/timezone`).

Covers: two signed-in users reading the same month see the same cached shows
with fully independent not-watching overlays; the `/api/state` delta endpoint
is idempotent and does not lose one tab's mark to another's; a non-admin's
card-style choice persists across separate requests through `user_prefs`
instead of settings.json; and the timezone picker persists to `users.timezone`
and changes which month a boundary item renders under.

No network — the Trakt window fetch is patched at app.calendar_cache's own
module boundary, the same way tests/test_calendar_cache.py does it, so the
real per-viewer normalize/trim logic in calendar_cache.read_month runs for
real. TRAKT_DATA_DIR points at a temp dir (set BEFORE importing app modules).

Run: ./.venv/Scripts/python.exe -m unittest tests.test_calendar_route -v
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-calroute-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, calendar_cache, calendar_state, db, trakt  # noqa: E402
from app.config import Settings, save_settings  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


def _configured_settings() -> Settings:
    """`settings.configured` gates the whole read path in index() — without
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


class CalendarRouteTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        CalendarRouteTestCase._counter += 1
        db.set_db_path(TMP / f"calroute-{CalendarRouteTestCase._counter}.db")
        asyncio.run(db.migrate())
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
        fetch = AsyncMock(return_value=entries)
        patcher = patch("app.calendar_cache.fetch_window_raw", fetch)
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
        fetch = AsyncMock(return_value=[])
        patcher = patch("app.calendar_cache.fetch_window_raw", fetch)
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
        patcher = patch("app.calendar_cache.fetch_window_raw",
                        AsyncMock(return_value=[drama, comedy]))
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
        self.assertEqual(prefs["network_filter"], [])

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

        async def fake(endpoint, settings, start):
            if start == target_window:
                return [_entry("boundary", "Boundary Show", "2026-03-01T02:00:00Z")]
            return []

        patcher = patch("app.calendar_cache.fetch_window_raw", side_effect=fake)
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

        async def fake(endpoint, settings, start):
            if start == boom_window:
                raise trakt.TraktError("Trakt unreachable", 503)
            if start == good_window:
                return [_entry("show-good", "Good Show", "2026-07-08T20:00:00Z")]
            return []

        with patch("app.calendar_cache.fetch_window_raw", side_effect=fake):
            resp = self.client.get("/?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Good Show", resp.text)
        self.assertIn("warning-banner", resp.text)
        self.assertIn("showing what we have", resp.text)
        self.assertNotIn("error-banner", resp.text)

    def test_every_window_failing_shows_the_error_banner_not_the_warning(self):
        """No window loaded and nothing cached: there is nothing to show, so the
        month falls to the hard error banner, not the partial warning."""
        async def fake(endpoint, settings, start):
            raise trakt.TraktError("Trakt unreachable", 503)

        with patch("app.calendar_cache.fetch_window_raw", side_effect=fake):
            resp = self.client.get("/?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error-banner", resp.text)
        self.assertNotIn("warning-banner", resp.text)


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
        patcher = patch("app.calendar_cache.fetch_window_raw",
                        AsyncMock(return_value=entries))
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

        async def boom(endpoint, settings, start):
            raise trakt.TraktError("Trakt unreachable", 503)

        with patch("app.calendar_cache.fetch_window_raw", side_effect=boom):
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
        patcher = patch("app.calendar_cache.fetch_window_raw",
                        AsyncMock(return_value=entries))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_day_is_an_addressable_section(self):
        html = self.client.get(self.PAGE).text
        self.assertIn('id="day-2026-07-15"', html)
        self.assertIn('id="day-2026-07-16"', html)

    def test_the_jump_to_strip_links_days_with_items_and_greys_the_rest(self):
        html = self.client.get(self.PAGE).text
        self.assertIn('<a class="day-chip" href="#day-2026-07-15"', html)
        # 1 July has nothing on it, so there is no section to send anyone to.
        self.assertIn('<span class="day-chip empty"', html)
        self.assertNotIn('href="#day-2026-07-01"', html)
        # One chip per day of the month, links and inert ones together.
        self.assertEqual(len(re.findall(r'class="day-chip[ "]', html)), 31)

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
