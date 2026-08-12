"""The Sources screen and the one route that writes what it chooses.

WHAT THIS FILE IS ACTUALLY DEFENDING, in the order it matters:

  1. THE THREE CONTROLS ARE THREE. The calendar source, the tracker source and a
     per-CALENDAR override of the calendar source are separate answers, and the
     failure worth catching is one of them quietly writing over another.
  2. `both` NEVER APPEARS AS AN OPTION AND NEVER SURVIVES A SAVE. It is a legacy
     spelling of exactly {trakt, simkl}, so it renders as two ticks and leaves as
     "trakt+simkl" — the value it always meant, said in a way a third service
     cannot leak into.
  3. SAVING COSTS NOTHING. Resolution runs at read, so a preference change must
     refetch nothing and expire nothing. Asserted by forbidding the fetch, in
     tests/calendar/test_precedence.py at the model level and here at the route.

No network.
"""
from __future__ import annotations

import asyncio
import json
import re
import unittest
from unittest.mock import patch

from app.calendar import resolve as calendar_resolve
from app.endpoints import ENDPOINTS
from app.sources import prefs as source_prefs
from app.sources import routes as source_routes
from tests.support import AppTestCase


def _load(user_id: int) -> source_prefs.SourcePrefs:
    return asyncio.run(source_prefs.load(user_id))


def _save(prefs: source_prefs.SourcePrefs) -> None:
    asyncio.run(source_prefs.save(prefs))


class ScreenTestCase(AppTestCase):
    """One signed-in account, on a page that costs no call to any service."""

    def setUp(self):
        super().setUp()
        self.user_id = self.make_user("viewer", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def page(self) -> str:
        response = self.client.get("/sources")
        self.assertEqual(response.status_code, 200)
        return response.text

    def post(self, body, **kwargs):
        return self.client.post("/api/sources", json=body, **kwargs)


class TheScreenRendersWhatIsStoredTests(ScreenTestCase):
    def test_it_needs_a_session(self):
        self.client.cookies.clear()
        response = self.client.get("/sources", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303, 401, 403))

    def test_an_account_that_has_stated_nothing_gets_the_defaults(self):
        """No row is written until somebody saves, so the screen has to render
        from the defaults rather than from a row it created by being opened."""
        html = self.page()
        self.assertIn('data-scope="calendar"', html)
        self.assertIn('data-scope="tracker"', html)
        for key in ENDPOINTS:
            self.assertIn(f'data-scope="endpoint:{key}"', html)

    def test_it_offers_every_registered_service_and_never_the_word_both(self):
        """`both` is a two-service word: offering it would hand a third service
        to somebody who chose from a menu of two, the moment one is registered."""
        html = self.page()
        self.assertIn('data-source="trakt"', html)
        self.assertIn('data-source="simkl"', html)
        self.assertNotIn('value="both"', html)

    def test_the_precedence_rows_are_the_resolve_vocabulary(self):
        """Imported rather than spelled again, so a field added to the record
        cannot leave this screen offering a preference about a field that no
        longer exists — or missing one that does."""
        html = self.page()
        offered = set(re.findall(r'<select data-field="([^"]+)"', html))
        expected = set(calendar_resolve.FIELDS) - set(source_routes._UNOFFERED_FIELDS)
        self.assertEqual(offered, expected)

    def test_a_calendar_only_offers_the_services_that_publish_it(self):
        """A service named for a calendar it does not answer yields nothing,
        which from the screen looks exactly like a service that answered and had
        nothing to say. Season finales are the live case: one source publishes
        them and the other does not."""
        html = self.page()
        finales = re.search(r'data-scope="endpoint:shows/finales".*?</div>\s*</div>',
                            html, re.S).group(0)
        premieres = re.search(r'data-scope="endpoint:shows/premieres".*?</div>\s*</div>',
                              html, re.S).group(0)
        self.assertEqual(re.findall(r'data-source="([^"]+)"', finales), ["trakt"])
        self.assertEqual(re.findall(r'data-source="([^"]+)"', premieres),
                         ["trakt", "simkl"])

    def test_each_calendars_declared_reach_is_stated(self):
        """The honest answer to "why is this month empty" — a rolling window
        cannot answer for last year, and a source with no declared bound says so
        differently from one with a big number."""
        self.assertIn("Simkl reaches about 3 years back and about 3 months forward.",
                      self.page())

    def test_the_union_fields_are_deliberately_not_offered(self):
        """A genre preference only reorders a union that keeps every entry, and
        an air-time preference invites choosing the answer that is wrong for most
        of a calendar. Both stay readable in a stored document; neither is handed
        out here."""
        html = self.page()
        self.assertNotIn('data-field="genres"', html)
        self.assertNotIn('data-field="airing"', html)


class EveryChoiceOnTheScreenIsDrawnTheSameWayTests(ScreenTestCase):
    """The screen asks one question seven times, about different scopes. It has to
    LOOK like one question asked seven times, or the row that happens to offer one
    service reads as a different kind of control — or as a mistake."""

    def _rows(self, html: str) -> list[str]:
        return re.findall(r'<div class="row source-row">.*?data-scope="([^"]+)"',
                          html, re.S)

    def test_every_calendar_gets_a_row_of_the_same_shape(self):
        """Including the one only a single service publishes. It offers one tick
        instead of two and nothing else about it differs."""
        html = self.page()
        scopes = self._rows(html)
        for endpoint in ENDPOINTS:
            self.assertIn(f"endpoint:{endpoint}", scopes)
        rows = re.findall(
            r'<div class="row source-row">\s*<span class="label">.*?'
            r'<div class="source-choice" data-scope="endpoint:[^"]+">.*?'
            r'<div class="source-ticks">', html, re.S)
        self.assertEqual(len(rows), len(ENDPOINTS),
                         "a calendar row is built differently from its siblings")

    def test_the_one_service_calendar_is_a_row_like_the_others_not_a_line(self):
        """Season finales are the live case: one source publishes them, so its row
        offers one tick. The label, the modes and the ticks are still the same
        three parts in the same order."""
        html = self.page()
        finales = re.search(
            r'<div class="row source-row">\s*<span class="label">Season Finales</span>'
            r'.*?</div>\s*</div>\s*</div>', html, re.S)
        self.assertIsNotNone(finales, "the finales calendar is not drawn as a row")
        block = finales.group(0)
        self.assertIn('value="inherit"', block)
        self.assertIn('value="auto"', block)
        self.assertIn('value="named"', block)
        self.assertEqual(re.findall(r'data-source="([^"]+)"', block), ["trakt"])

    def test_the_account_wide_calendar_choice_is_a_row_like_the_calendars_below_it(self):
        """It is the same question about a wider scope, and "same as above" on the
        rows underneath is a statement about this one."""
        self.assertIn("calendar", self._rows(self.page()))

    def test_each_section_is_headed_rather_than_starting_with_another_row(self):
        """A heading drawn as a `.row` is the same box, background and type as the
        settings under it, so it reads as one more setting and the page reads as
        one undifferentiated list. These are the settings screens' own heading."""
        html = self.page()
        headings = re.findall(r'<div class="settings-section">([^<]+)</div>', html)
        self.assertEqual(len(headings), 4, headings)
        labels = set(re.findall(r'<span class="label">([^<]+)</span>', html))
        for heading in headings:
            # The glyph leads the heading the way the Settings panel's sections
            # do; the words after it are what must not also be a row's label.
            words = heading.split(" ", 1)[-1].strip()
            with self.subTest(heading=words):
                self.assertNotIn(words, labels)


class AStoredBothRendersAsTwoTicksTests(ScreenTestCase):
    def setUp(self):
        super().setUp()
        _save(source_prefs.SourcePrefs(
            user_id=self.user_id, calendar_source=source_prefs.BOTH,
            tracker_source=source_prefs.BOTH))

    def test_it_comes_back_as_the_two_services_it_always_meant(self):
        state = source_routes._selection_state(source_prefs.BOTH)
        self.assertEqual(state["mode"], "named")
        self.assertEqual(state["names"], source_prefs.LEGACY_BOTH)

    def test_the_page_ticks_both_services_rather_than_naming_the_word(self):
        block = re.search(r'data-scope="calendar".*?</div>\s*</div>', self.page(), re.S)
        self.assertIsNotNone(block)
        ticked = re.findall(r'data-source="([^"]+)"\s+checked', block.group(0))
        self.assertEqual(sorted(ticked), ["simkl", "trakt"])
        self.assertRegex(block.group(0), r'value="named"\s+checked')

    def test_saving_the_ticked_set_rewrites_it_to_the_named_spelling(self):
        """The screen posts what it drew — the two services — so the legacy word
        leaves the row without a migration and with no change in behaviour."""
        response = self.post({"calendar_source": "trakt+simkl",
                              "tracker_source": "trakt+simkl"})
        self.assertEqual(response.status_code, 200)
        stored = _load(self.user_id)
        self.assertEqual(stored.calendar_source, "trakt+simkl")
        self.assertEqual(stored.tracker_source, "trakt+simkl")
        self.assertEqual(source_prefs.named_sources(stored.calendar_source),
                         source_prefs.LEGACY_BOTH)


class EachControlSavesAndReadsBackTests(ScreenTestCase):
    def test_the_calendar_source_round_trips(self):
        self.assertTrue(self.post({"calendar_source": "simkl"}).json()["ok"])
        self.assertEqual(_load(self.user_id).calendar_source, "simkl")

    def test_the_tracker_source_round_trips(self):
        self.assertTrue(self.post({"tracker_source": "trakt"}).json()["ok"])
        self.assertEqual(_load(self.user_id).tracker_source, "trakt")

    def test_a_per_endpoint_override_changes_only_that_endpoint(self):
        """The whole reason the override exists: one service's movie calendar is
        a global release listing and its show calendar is coverage worth having,
        which is two opposite answers about one service."""
        self.post({"calendar_source": "auto", "endpoint_sources": {"movies": "trakt"}})
        stored = _load(self.user_id)
        self.assertEqual(stored.calendar_selection("movies"), "trakt")
        self.assertEqual(stored.calendar_selection("shows"), "auto")
        self.assertEqual(stored.calendar_source, "auto")

    def test_an_endpoint_left_alone_follows_the_account_wide_choice_afterwards(self):
        """Stored as nothing rather than as a copy, so changing the account-wide
        answer later moves every calendar that never stated one of its own."""
        self.post({"calendar_source": "trakt", "endpoint_sources": {}})
        self.assertEqual(_load(self.user_id).endpoint_sources, {})
        self.post({"calendar_source": "simkl"})
        self.assertEqual(_load(self.user_id).calendar_selection("movies"), "simkl")

    def test_precedence_round_trips_as_a_default_and_per_field_entries(self):
        self.post({"precedence": {"default": "simkl", "fields": {"poster": "trakt"}}})
        stored = _load(self.user_id)
        self.assertEqual(stored.precedence,
                         {"default": "simkl", "fields": {"poster": "trakt"}})
        self.assertEqual(stored.field_order("poster", ["simkl", "trakt"])[0], "trakt")
        self.assertEqual(stored.field_order("overview", ["trakt", "simkl"])[0], "simkl")

    def test_the_app_decides_clears_a_field_rather_than_storing_an_empty_choice(self):
        self.post({"precedence": {"default": "", "fields": {"poster": "trakt"}}})
        self.post({"precedence": {"default": "", "fields": {"poster": ""}}})
        self.assertEqual(_load(self.user_id).precedence, {})

    def test_a_section_the_body_does_not_mention_is_left_alone(self):
        """So a control added later cannot be blanked by an older page that does
        not know about it."""
        self.post({"calendar_source": "simkl", "tracker_source": "trakt"})
        self.post({"precedence": {"default": "simkl", "fields": {}}})
        stored = _load(self.user_id)
        self.assertEqual(stored.calendar_source, "simkl")
        self.assertEqual(stored.tracker_source, "trakt")

    def test_what_was_stored_is_echoed_back_not_what_was_sent(self):
        data = self.post({"calendar_source": "simkl+trakt"}).json()
        self.assertEqual(data["calendar_source"], "trakt+simkl")


class TheSaveGoesThroughTheSharedGuardTests(ScreenTestCase):
    """Mutating requests are application/json app-wide and the body is read by
    one shared helper. Both are checked from the outside, because a route that
    grew its own copy of either would pass every test that only calls it
    correctly."""

    def test_a_form_encoded_post_never_reaches_the_route(self):
        response = self.client.post("/api/sources", data={"calendar_source": "trakt"})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(_load(self.user_id).calendar_source, source_prefs.AUTO)

    def test_a_malformed_body_is_refused_in_the_standard_shape(self):
        response = self.client.post(
            "/api/sources", content="{not json",
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertTrue(response.json()["error"])

    def test_a_body_that_is_not_an_object_is_refused(self):
        response = self.client.post(
            "/api/sources", content=json.dumps(["trakt"]),
            headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_a_selection_nobody_can_satisfy_is_refused_rather_than_coerced(self):
        """Silently rewriting it to `auto` would leave somebody looking at a
        screen showing a choice that was never stored."""
        response = self.post({"calendar_source": "netflix"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(_load(self.user_id).calendar_source, source_prefs.AUTO)

    def test_a_precedence_field_this_version_does_not_have_is_refused(self):
        """Tolerated at READ, because a row from a newer version must not stop a
        page rendering — but refused on the way IN, or this screen would store a
        preference that silently does nothing forever."""
        response = self.post({"precedence": {"fields": {"vibes": "trakt"}}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(_load(self.user_id).precedence, {})

    def test_a_precedence_naming_a_service_this_app_does_not_have_is_refused(self):
        response = self.post({"precedence": {"fields": {"poster": "netflix"}}})
        self.assertEqual(response.status_code, 400)

    def test_an_endpoint_this_app_does_not_have_is_refused(self):
        response = self.post({"endpoint_sources": {"podcasts": "trakt"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(_load(self.user_id).endpoint_sources, {})

    def test_one_account_cannot_write_anothers_preferences(self):
        other = self.make_user("someone-else")
        self.post({"calendar_source": "simkl"})
        self.assertEqual(_load(other).calendar_source, source_prefs.AUTO)


class SavingAPreferenceCostsNothingTests(ScreenTestCase):
    """THE DESIGN'S CENTRAL CLAIM, AT THE ROUTE. The model-level proof lives in
    tests/calendar/test_precedence.py; this is the half that could be undone
    without touching resolution at all — a save that helpfully cleared the
    viewer's cached months would turn every control on that screen into a
    refetch, and nothing in the model would notice."""

    def test_saving_asks_no_source_for_anything(self):
        def refuse(*args, **kwargs):
            raise AssertionError("saving a preference asked a source for data")

        with patch("app.calendar.cache.fetch_window_records", refuse):
            self.assertTrue(self.post({"calendar_source": "simkl"}).json()["ok"])

    def test_saving_expires_no_cached_window(self):
        """Byte-identical rows, cached at the same instant: nothing was rewritten,
        so nothing expired early either."""
        from app import db

        payload = b"a stored window"
        asyncio.run(db.execute(
            "INSERT INTO api_cache (cache_key, payload, cached_at, byte_size) "
            "VALUES (?, ?, ?, ?)",
            ("calendar:shows:2026-07-06", payload, 1000, len(payload))))
        before = asyncio.run(db.fetch_one("SELECT payload, cached_at FROM api_cache"))
        self.post({"calendar_source": "simkl", "precedence": {"default": "simkl"}})
        after = asyncio.run(db.fetch_one("SELECT payload, cached_at FROM api_cache"))
        self.assertEqual((bytes(after["payload"]), after["cached_at"]),
                         (bytes(before["payload"]), before["cached_at"]))


if __name__ == "__main__":
    unittest.main()
