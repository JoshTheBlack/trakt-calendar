"""The tracker page's own panels: backup and restore, and taking a row off a
month.

These are seam tests. The machinery each one drives is proven elsewhere — the
export/restore round trip in tests/distrakt/test_backup.py, the store in
tests/distrakt/test_store.py — so what is pinned here is that the control on the
page reaches it, that the destructive one cannot be reached by accident, and
that removing a row does the right thing to the calendar it came from.

No network: the shared calendar cache's read is patched wherever a removal
consults it.
"""
from __future__ import annotations

import unittest
import asyncio
import re
from datetime import date
from unittest.mock import patch

from app import distrakt as distrakt_store
from app.calendar import state as calendar_state
from app.providers.base import Item, ItemKey, Media, Source
from app.providers.trakt import TraktError
from app.config import Settings, load_settings, save_settings
from tests.support import AppTestCase, ORIGIN


def _fake_premiere_read(trakt_id: int, season: int):
    """Stand in for the shared calendar cache with exactly one premiere in it —
    both halves of the tracker's premiere read (shows/new and shows/premieres)
    see the same month.

    A real Item, so this double satisfies the same construction rules the live
    normalizer does rather than only the fields the tracker happens to read."""
    async def read_month(endpoint, settings, **kw):
        item = Item(
            source=Source.TRAKT, media=Media.SHOW, id=f"slug-{trakt_id}",
            ids={"trakt": trakt_id, "tmdb": trakt_id, "slug": f"slug-{trakt_id}"},
            detail_url=f"https://trakt.tv/shows/slug-{trakt_id}",
            air_date="2026-08-01", air_ts=0.0, air_display="01 Aug 2026",
            air_time="20:00", day_of_week="Saturday",
            title=f"Show {trakt_id}", season=season, network="Net",
        )
        return [item], None
    return read_month


class TrackerPanelTestCase(AppTestCase):
    def make_settings(self):
        # The configured origin has to match the one the client speaks, or the
        # cross-site rules refuse every save below for an unrelated reason.
        return Settings(public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        self.admin_id = self._make_user("admin_user", is_admin=True, calendar_approved=True)

    def _make_user(self, username, password="hunter2hunter2", **flags) -> int:
        return self.make_user(username, password, **flags)

    def sign_in_as(self, user_id: int) -> None:
        """As the shared one, but starting from an empty jar — these tests move
        between accounts and a stale cookie of another kind would travel."""
        self.client.cookies.clear()
        super().sign_in_as(user_id)
    def _link_trakt(self, user_id: int, provider_user_id: int, token: str | None = "tok") -> None:
        self.link_identity(user_id, "trakt", provider_user_id, token)

    def tracker_user(self, username="tracker") -> int:
        user_id = self._make_user(username, calendar_approved=True, distrakt_approved=True)
        self._link_trakt(user_id, provider_user_id=900 + user_id)
        return user_id



class BackupPanelTests(TrackerPanelTestCase):
    """Download and restore, and the acknowledgement in front of the destructive
    half."""

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)
        # The panels live on a month's own view; the bare address is the chooser.
        # Taken from the clock so nothing here rots at a month boundary.
        today = date.today()
        self.page = f"/distrakt?year={today.year}&month={today.month}"

    def _add_show(self, user_id: int, title: str) -> None:
        asyncio.run(distrakt_store.add_show(user_id, "2026-07", {
            "ids": {"trakt": 11, "tmdb": 11, "slug": "a-show"}, "season": 1, "title": title,
            "network": "HBO", "media": "show",
        }))

    def test_the_page_offers_a_download_and_a_restore(self):
        body = self.client.get(self.page).text
        self.assertIn('href="/api/distrakt/export"', body)
        self.assertIn('id="restoreFile"', body)

    def test_the_restore_control_demands_a_typed_acknowledgement(self):
        """Restore replaces rather than merges, so the page asks for a phrase
        that has to be read and copied — a confirm dialog can be dismissed by
        reflex, and this cannot."""
        body = self.client.get(self.page).text
        self.assertIn("REPLACE MY DATA", body)
        self.assertIn('id="restoreAck"', body)
        # The button starts unusable, so the phrase is the only way to arm it.
        self.assertRegex(body, r'id="restoreBtn"[^>]*disabled')

    def test_the_export_downloads_as_a_file(self):
        resp = self.client.get("/api/distrakt/export")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp.headers["content-disposition"])
        self.assertEqual(resp.json()["schema"], distrakt_store.EXPORT_SCHEMA)

    def test_download_then_restore_round_trips_through_the_routes(self):
        self._add_show(self.user_id, "Kept Show")
        exported = self.client.get("/api/distrakt/export").json()

        asyncio.run(distrakt_store.remove_show(
            self.user_id, "2026-07", ItemKey("show", "tmdb", "11"), 1))
        self.assertEqual(self.client.get("/api/distrakt/list?year=2026&month=7").json()["shows"], [])

        resp = self.client.post("/api/distrakt/restore", json=exported)
        self.assertEqual(resp.status_code, 200, resp.text)
        listed = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in listed["shows"]], ["Kept Show"])

    def test_a_restore_lands_on_whoever_asked_not_whoever_exported(self):
        """The file names no owner the server will honour: it is restored to the
        session that uploaded it, and the account it came from is untouched."""
        self._add_show(self.user_id, "Mine")
        exported = self.client.get("/api/distrakt/export").json()

        other = self.tracker_user("other_tracker")
        self.sign_in_as(other)
        resp = self.client.post("/api/distrakt/restore", json=exported)
        self.assertEqual(resp.status_code, 200, resp.text)
        landed = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in landed["shows"]], ["Mine"])

        self.sign_in_as(self.user_id)
        still_there = self.client.get("/api/distrakt/list?year=2026&month=7").json()
        self.assertEqual([s["title"] for s in still_there["shows"]], ["Mine"])


class ImportingAMonthTests(TrackerPanelTestCase):
    """The ⤓ Import control, and the one month it is allowed to act on.

    The tracker opens on whichever month the page it was reached from was
    showing, so the month in the request is only as sensible as that page was.
    """

    def make_settings(self):
        # The route refuses outright without Trakt credentials, so the instance
        # has to carry a client id for the month rule to be what is under test.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid")

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)
        self.today = date.today()

    def _import(self, offset: int):
        """Import the month `offset` after this one. Derived from the clock: the
        route decides by comparing the month against today."""
        index = self.today.month - 1 + offset
        year, month = self.today.year + index // 12, index % 12 + 1
        # Building the month also consults recent viewing, and the route ends by
        # recomputing the whole month; neither is what is under test here.
        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(303, 1)), \
             patch("app.providers.trakt.sync.fetch_watched_progress", return_value=[]), \
             patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/import", json={"year": year, "month": month})

    def test_a_month_the_calendar_has_not_reached_is_refused(self):
        """Not a silent no-op: the month cannot be built out there, so an import
        into it would write nothing while the page said it had imported."""
        resp = self._import(2)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("hasn't reached", resp.json()["error"])
        self.assertEqual(asyncio.run(distrakt_store.list_months(self.user_id)), [])

    def test_the_month_in_progress_imports(self):
        resp = self._import(0)
        self.assertEqual(resp.status_code, 200)
        month = distrakt_store.month_key(self.today.year, self.today.month)
        doc = asyncio.run(distrakt_store.load_month(self.user_id, month))
        self.assertEqual([s["ids"]["trakt"] for s in doc["shows"]], [303])

    def test_the_month_ahead_is_built_by_asking(self):
        """Opening a month that has not begun no longer builds it, so this button
        is how it comes into existence at all — and what it gets is what premieres
        in it."""
        index = self.today.month
        year, month = self.today.year + index // 12, index % 12 + 1
        resp = self._import(1)
        self.assertEqual(resp.status_code, 200)
        doc = asyncio.run(distrakt_store.load_month(
            self.user_id, distrakt_store.month_key(year, month)))
        self.assertEqual([s["ids"]["trakt"] for s in doc["shows"]], [303])


class RemovingFromTheTrackerTests(TrackerPanelTestCase):
    """The ✕ on a tracker row, and the one thing it is allowed to touch outside
    the tracker.

    It used to delete the row and nothing else, which on a PREVIEW month (before
    the 1st, when the roster re-imports the month's premieres on every load) put
    the show straight back in the same response — the button looked broken, and
    the only way to get rid of a premiere was to go and hide it on the calendar
    instead. It now makes that calendar mark itself, but ONLY for a row the
    calendar put there: removing something the user added by hand must not hide a
    show they never said they weren't watching.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)
        self._add(202, 1, "slug-202", distrakt_store.ADDED_BY_CALENDAR)

    def _add(self, tid, season, slug, added_by):
        asyncio.run(distrakt_store.add_show(self.user_id, "2026-08", {
            "ids": {"trakt": tid, "tmdb": tid, "slug": slug}, "season": season,
            "title": f"Show {tid}",
            "network": "Net", "media": "show", "added_by": added_by,
        }))

    def _remove(self, tid=202, season=1):
        # The payload rebuild at the end of the route is a whole live month and
        # not what is under test; the removal itself is.
        with patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/remove", json={
                "year": 2026, "month": 8, "key": f"show:tmdb:{tid}", "season": season})

    def _marks(self) -> set:
        return asyncio.run(calendar_state.not_watching_ids(self.user_id))

    def test_removing_a_calendar_row_marks_the_show_not_watching(self):
        resp = self._remove()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("slug-202", self._marks())
        self.assertTrue(resp.json()["hidden_on_calendar"])

    def test_removing_a_hand_added_row_leaves_the_calendar_alone(self):
        """THE POINT OF THE PROVENANCE COLUMN. Undoing a manual add is not a
        statement about watching, and must not take the show off the calendar."""
        self._add(404, 1, "slug-404", distrakt_store.ADDED_BY_MANUAL)
        resp = self._remove(tid=404, season=1)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("slug-404", self._marks())
        self.assertFalse(resp.json()["hidden_on_calendar"])

    def test_removing_a_watch_history_row_leaves_the_calendar_alone(self):
        """Nor is dropping something the tracker picked up from watch history —
        it is not re-imported either, so removing it already sticks."""
        self._add(505, 3, "slug-505", distrakt_store.ADDED_BY_HISTORY)
        self._remove(tid=505, season=3)
        self.assertNotIn("slug-505", self._marks())

    def test_the_removed_calendar_row_does_not_come_straight_back(self):
        """The bug, end to end: remove it, then let a preview month re-import the
        month's premieres exactly as a page load does."""
        self._remove()

        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(202, 1)):
            asyncio.run(distrakt_store.import_premieres(self.user_id, "2026-08", load_settings()))

        doc = asyncio.run(distrakt_store.load_month(self.user_id, "2026-08"))
        self.assertEqual([s["key"] for s in doc["shows"]], [])

    def test_a_row_with_no_slug_is_marked_by_its_source_id(self):
        """The calendar keys an item by slug and falls back to the source's own
        id; a mark written under the wrong one would silently match nothing. Note
        that is NOT the id the tracker row is keyed on — the row is filed under
        the shared id, and the mark has to be written in the calendar's terms."""
        self._add(303, 2, "", distrakt_store.ADDED_BY_CALENDAR)
        self._remove(tid=303, season=2)
        self.assertIn("303", self._marks())

    def test_a_row_that_was_never_there_marks_nothing(self):
        """A 404 must not leave a calendar mark behind for a show the tracker
        never held."""
        resp = self._remove(tid=999, season=1)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._marks(), set())

    def test_an_imported_premiere_records_where_it_came_from(self):
        """The provenance has to be written by the import itself, or every row on
        a preview month would look hand-added the moment it mattered."""
        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(606, 1)):
            asyncio.run(distrakt_store.import_premieres(self.user_id, "2026-08", load_settings()))
        doc = asyncio.run(distrakt_store.load_month(self.user_id, "2026-08"))
        added = next(s for s in doc["shows"] if s["ids"]["trakt"] == 606)
        self.assertEqual(added["added_by"], distrakt_store.ADDED_BY_CALENDAR)


class LegacyRowRemovalTests(TrackerPanelTestCase):
    """Rows written before provenance was recorded, which is every row on every
    instance the day this ships. There is no stored answer, so the calendar is
    asked directly: would it hand this show straight back?"""

    def setUp(self):
        super().setUp()
        # The legacy path asks the calendar, which it only does with Trakt
        # credentials in place; the read itself is patched in each test.
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="id",
                               trakt_access_token="tok"))
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)

    def _add_legacy(self, tid, slug):
        asyncio.run(distrakt_store.add_show(self.user_id, "2026-08", {
            "ids": {"trakt": tid, "tmdb": tid, "slug": slug}, "season": 1, "title": "Legacy",
            "network": "Net", "media": "show",  # no added_by: the pre-column shape
        }))

    def _remove(self, tid):
        with patch("app.distrakt.routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/remove", json={
                "year": 2026, "month": 8, "key": f"show:tmdb:{tid}", "season": 1})

    def test_a_legacy_row_the_calendar_would_re_add_is_marked(self):
        self._add_legacy(707, "slug-707")
        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(707, 1)):
            resp = self._remove(707)
        self.assertTrue(resp.json()["hidden_on_calendar"])
        self.assertIn("slug-707", asyncio.run(calendar_state.not_watching_ids(self.user_id)))

    def test_a_legacy_row_the_calendar_knows_nothing_about_is_left_alone(self):
        self._add_legacy(808, "slug-808")
        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(707, 1)):
            resp = self._remove(808)
        self.assertFalse(resp.json()["hidden_on_calendar"])
        self.assertEqual(asyncio.run(calendar_state.not_watching_ids(self.user_id)), set())

    def test_an_unreachable_calendar_marks_nothing_rather_than_guessing(self):
        """Not knowing means not marking: the row comes back, which is annoying
        and undoable, where a wrong mark hides a show the user still wants."""
        async def boom(endpoint, settings, **kw):
            raise TraktError("unreachable")

        self._add_legacy(909, "slug-909")
        with patch("app.calendar.cache.read_month", side_effect=boom):
            resp = self._remove(909)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["hidden_on_calendar"])
        self.assertEqual(asyncio.run(calendar_state.not_watching_ids(self.user_id)), set())


if __name__ == "__main__":
    unittest.main()
