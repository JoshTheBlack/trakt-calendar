"""The tracker page's own panels: backup and restore, and taking a row off a
month.

These are seam tests. The machinery each one drives is proven elsewhere — the
export/restore round trip in tests/distrakt/test_backup.py, the store in
tests/distrakt/test_store.py — so what is pinned here is that the control on the
page reaches it, that the destructive one cannot be reached by accident, and
that ending a title's run here says so on the viewer's main calendar.

EVERY MONTH IN THIS FILE COMES FROM THE CLOCK. Written down instead, one rots the
moment the real calendar moves past it — this suite has been bitten twice by
exactly that, once by a month that passed all July and failed on the 1st of
August. Nothing here may name a month that has to be true on the day it is run.

No network: the shared calendar cache's read is patched wherever a removal
consults it.
"""
from __future__ import annotations

import unittest
import asyncio
from datetime import date
from unittest.mock import patch

from app import distrakt as distrakt_store
from app.calendar import state as calendar_state
from app.providers.base import Item, ItemKey, Media, Source
from app.config import Settings, load_settings
from tests.support import AppTestCase, ORIGIN


def _month_offset(today: date, offset: int) -> tuple[int, int]:
    """The (year, month) `offset` months from `today`, negative for behind.

    One helper because every class below needs it and each writing its own is how
    two of them came to disagree about what "last month" was in January.
    """
    index = today.year * 12 + (today.month - 1) + offset
    return divmod(index, 12)[0], divmod(index, 12)[1] + 1


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
        self.year, self.month = today.year, today.month
        self.list_url = f"/api/distrakt/list?year={self.year}&month={self.month}"

    def _add_show(self, user_id: int, title: str) -> None:
        """One record on the month the panels are being driven from — a settled
        one, so it is a month FACT and travels with the month rather than sitting
        on the viewer's own list, which belongs to no month for the export to put
        it under."""
        asyncio.run(distrakt_store.add_month_record(
            user_id, distrakt_store.month_key(self.year, self.month), {
                "media": Media.SHOW,
                "ids": {"trakt": 11, "tmdb": 11, "slug": "a-show"},
                "season": 1, "title": title, "network": "HBO",
                "kind": distrakt_store.RecordKind.COMPLETED,
                "watched": 6, "total": 6,
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

        asyncio.run(distrakt_store.remove_season_everywhere(
            self.user_id, ItemKey("show", "tmdb", "11"), 1))
        self.assertEqual(self.client.get(self.list_url).json()["shows"], [])

        resp = self.client.post("/api/distrakt/restore", json=exported)
        self.assertEqual(resp.status_code, 200, resp.text)
        listed = self.client.get(self.list_url).json()
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
        landed = self.client.get(self.list_url).json()
        self.assertEqual([s["title"] for s in landed["shows"]], ["Mine"])

        self.sign_in_as(self.user_id)
        still_there = self.client.get(self.list_url).json()
        self.assertEqual([s["title"] for s in still_there["shows"]], ["Mine"])


class ImportingAMonthTests(TrackerPanelTestCase):
    """The ⤓ Import control: the months it acts on, and the one it refuses.

    Pressing it is a deliberate ask about the month on screen, which is why it may
    point at any month the calendar has not passed, however far ahead and whatever
    it leaves unbuilt behind it.
    """

    def make_settings(self):
        # The route refuses outright when the INSTANCE has no calendar to import
        # from, so it carries a working credential here for the month rule to be
        # what is under test. It used to be enough to carry a client id and let
        # the signed-in viewer's own token do the fetching — which is exactly the
        # coupling that refused the button to somebody signed in with Simkl
        # alone, and the calendar page itself has never accepted that shape.
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                        trakt_access_token="instance-token")

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

    def _key(self, offset: int) -> str:
        index = self.today.month - 1 + offset
        return distrakt_store.month_key(self.today.year + index // 12, index % 12 + 1)

    def test_a_month_well_out_ahead_is_built_by_asking(self):
        """No bound on how far ahead the ask may point. What it gathers is only
        what is already known about that month's calendar, so a distant one costs
        nothing extra — and the click is what stops a month being built by
        accident."""
        resp = self._import(4)
        self.assertEqual(resp.status_code, 200)
        doc = asyncio.run(distrakt_store.load_month(self.user_id, self._key(4)))
        self.assertEqual([s["ids"]["trakt"] for s in doc["shows"]], [303])

    def test_a_month_skipped_over_can_still_be_filled_in_afterwards(self):
        """The reason the bound had to go rather than merely be widened: a store
        that only grew forward let a month built out ahead strand every month
        between here and it."""
        self.assertEqual(self._import(4).status_code, 200)
        self.assertEqual(self._import(2).status_code, 200)
        doc = asyncio.run(distrakt_store.load_month(self.user_id, self._key(2)))
        self.assertEqual([s["ids"]["trakt"] for s in doc["shows"]], [303])

    def test_a_past_month_that_was_never_tracked_is_still_refused(self):
        """The one refusal left. Filling in months nobody was tracking is what the
        watch-history backfill is for; an import would invent them from premieres
        instead."""
        asyncio.run(distrakt_store.add_month_record(self.user_id, self._key(0), {
            "media": Media.SHOW,
            "ids": {"trakt": 1, "tmdb": 1, "slug": "seed"}, "season": 1,
            "title": "Seed", "network": "Net",
            "kind": distrakt_store.RecordKind.SERIES_PREMIERE}))
        resp = self._import(-1)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("never tracked", resp.json()["error"])
        self.assertNotIn(self._key(-1), asyncio.run(distrakt_store.list_months(self.user_id)))

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


class AcknowledgingASeasonThatCameBackTests(TrackerPanelTestCase):
    """The control that dismisses the marker on a season that had been finished
    and grew.

    THE VIEWER CLEARS IT AND NOTHING ELSE DOES — not time, not the next load — so
    it needs a request of its own, and that request is all this covers. What sets
    the marker in the first place is tested in tests/distrakt/test_lifecycle.py.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)

    def _list_season(self, tid: int, season: int = 1, *, came_back: bool = True) -> None:
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "media": Media.SHOW, "ids": {"trakt": tid, "tmdb": tid},
            "season": season, "title": f"Show {tid}", "network": "Net",
            "kind": distrakt_store.RecordKind.KEEPUP, "watched": 8, "total": 10,
        }))
        if came_back:
            asyncio.run(distrakt_store.set_came_back(
                self.user_id, ItemKey(Media.SHOW, "tmdb", str(tid)), season, True))

    def _flag(self, tid: int, season: int = 1) -> bool:
        record = asyncio.run(distrakt_store.find_user_record(
            self.user_id, ItemKey(Media.SHOW, "tmdb", str(tid)), season))
        return bool(record["came_back"])

    def _acknowledge(self, tid: int, season: int = 1):
        return self.client.post("/api/distrakt/acknowledge-return",
                                json={"key": f"show:tmdb:{tid}", "season": season})

    def test_pressing_it_clears_the_marker(self):
        self._list_season(808)
        resp = self._acknowledge(808)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])
        self.assertFalse(self._flag(808))

    def test_it_answers_with_the_acknowledgement_and_not_a_whole_month(self):
        """Recomputing a month to drop one word would cost a season lookup per
        listed title — and there is no provider configured in this test, so a
        month rebuild here would also be a different failure."""
        self._list_season(808)
        self.assertEqual(self._acknowledge(808).json(), {"ok": True})

    def test_only_the_named_season_is_cleared(self):
        self._list_season(808, 1)
        self._list_season(808, 2)
        self._acknowledge(808, 1)
        self.assertFalse(self._flag(808, 1))
        self.assertTrue(self._flag(808, 2))

    def test_a_season_that_is_not_on_the_list_says_so(self):
        resp = self._acknowledge(909)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.json()["ok"])

    def test_a_malformed_row_address_is_refused_rather_than_queried(self):
        resp = self.client.post("/api/distrakt/acknowledge-return",
                                json={"key": "nonsense", "season": 1})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.json()["error"])


class EndingATitlesRunReachesTheCalendarTests(TrackerPanelTestCase):
    """The ✕ and Abandon, and the mark each of them leaves on the main calendar.

    A ROW TAKEN OFF HERE USED TO COME STRAIGHT BACK. On a month that has not begun
    the premieres re-import on every load, so deleting the row and nothing else
    put the show back in the same response — the button looked broken, and the
    only way to be rid of it was to go and hide it on the calendar by hand.

    IT NOW WRITES THAT MARK ITSELF, FOR EVERY ROW. The rule this replaces only
    spoke for rows the calendar had put there, and otherwise fell back to asking
    whether the title was one of the VIEWED month's premieres — so a show that
    premiered in one month and was given up on in a later one wrote nothing at
    all, because it was not that later month's premiere. A mark on a show the
    calendar never shows is inert: it hides nothing and re-adds nothing, so
    gating it bought nothing and cost the marks that mattered.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)
        self.today = date.today()
        self.month = distrakt_store.month_key(self.today.year, self.today.month)
        self._announce(202, 1, "slug-202", distrakt_store.ADDED_BY_CALENDAR)

    def _announce(self, tid, season, slug, added_by):
        """A premiere record on the month under way, as an import or a hand-add
        leaves one."""
        asyncio.run(distrakt_store.add_month_record(self.user_id, self.month, {
            "media": Media.SHOW,
            "ids": {"trakt": tid, "tmdb": tid, "slug": slug}, "season": season,
            "title": f"Show {tid}", "network": "Net", "added_by": added_by,
            "kind": distrakt_store.premiere_kind(season),
        }))

    def _put_on_the_list(self, tid, season, slug, added_by):
        """A season on the viewer's OWN list, which belongs to no month at all —
        the case the replaced rule could never write a mark for, because there is
        no month whose premieres it could have been one of."""
        asyncio.run(distrakt_store.add_user_record(self.user_id, {
            "media": Media.SHOW,
            "ids": {"trakt": tid, "tmdb": tid, "slug": slug}, "season": season,
            "title": f"Show {tid}", "network": "Net", "added_by": added_by,
            "kind": distrakt_store.RecordKind.KEEPUP, "watched": 2, "total": 8,
        }))

    def _post(self, path, **body):
        # The payload rebuild at the end of each of these routes is a whole live
        # month and is not what is under test; the mark is.
        with patch("app.distrakt.routes._distrakt_month_payload",
                   return_value=({"ok": True}, 200)):
            return self.client.post(path, json=body)

    def _remove(self, tid=202, season=1, offset=0):
        year, month = _month_offset(self.today, offset)
        return self._post("/api/distrakt/remove", year=year, month=month,
                          key=f"show:tmdb:{tid}", season=season)

    def _abandon(self, tid, season, abandoned, offset=0):
        year, month = _month_offset(self.today, offset)
        return self._post("/api/distrakt/abandon", year=year, month=month,
                          key=f"show:tmdb:{tid}", season=season, abandoned=abandoned)

    def _marks(self) -> set:
        return asyncio.run(calendar_state.not_watching_ids(self.user_id))

    def test_removing_a_calendar_row_marks_the_show_not_watching(self):
        resp = self._remove()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("slug-202", self._marks())
        self.assertTrue(resp.json()["hidden_on_calendar"])

    def test_removing_a_hand_added_row_marks_it_just_the_same(self):
        """WHAT PUT THE ROW THERE IS NOT ASKED. This asserted the opposite while
        the provenance column decided it, and the show it declined to mark was one
        the viewer had just said outright they were done with."""
        self._announce(404, 1, "slug-404", distrakt_store.ADDED_BY_MANUAL)
        resp = self._remove(tid=404)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("slug-404", self._marks())
        self.assertTrue(resp.json()["hidden_on_calendar"])

    def test_removing_a_row_the_tracker_picked_up_from_viewing_marks_it_too(self):
        self._put_on_the_list(505, 3, "slug-505", distrakt_store.ADDED_BY_HISTORY)
        self._remove(tid=505, season=3)
        self.assertIn("slug-505", self._marks())

    def test_giving_up_marks_the_show_from_whichever_month_is_on_screen(self):
        """NOT BOUNDED TO A MONTH. The season is on the viewer's own list, so it
        belongs to no month, and the month being looked at is not even the one the
        verdict is filed against — the mark is a statement about the SHOW."""
        self._put_on_the_list(606, 2, "slug-606", distrakt_store.ADDED_BY_HISTORY)
        resp = self._abandon(606, 2, True, offset=-1)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("slug-606", self._marks())

    def test_taking_the_verdict_back_takes_the_mark_back_with_it(self):
        """The fourth movement of the mirror, and the one that keeps the two views
        from arguing: a season put back in hand is not one the calendar should go
        on hiding."""
        self._put_on_the_list(707, 1, "slug-707", distrakt_store.ADDED_BY_HISTORY)
        self._abandon(707, 1, True)
        self.assertIn("slug-707", self._marks())
        self._abandon(707, 1, False)
        self.assertNotIn("slug-707", self._marks())

    def test_pressing_it_again_reports_that_nothing_changed(self):
        """`hidden_on_calendar` is what the toast speaks from, so it says whether
        a mark was actually made rather than whether one is there."""
        self.assertTrue(self._remove().json()["hidden_on_calendar"])
        self._announce(202, 1, "slug-202", distrakt_store.ADDED_BY_CALENDAR)
        self.assertFalse(self._remove().json()["hidden_on_calendar"])

    def test_a_row_with_no_slug_is_marked_by_its_source_id(self):
        """The calendar keys an item by slug and falls back to the source's own
        id; a mark written under the wrong one would silently match nothing. Note
        that is NOT the id the tracker row is keyed on — the row is filed under
        the shared id, and the mark has to be written in the calendar's terms."""
        self._announce(303, 2, "", distrakt_store.ADDED_BY_CALENDAR)
        self._remove(tid=303, season=2)
        self.assertIn("303", self._marks())

    def test_a_row_that_was_never_there_marks_nothing(self):
        """A 404 must not leave a calendar mark behind for a show the tracker
        never held."""
        resp = self._remove(tid=999, season=1)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self._marks(), set())

    def test_the_removed_calendar_row_does_not_come_straight_back(self):
        """The bug, end to end: remove it, then let the month re-import its
        premieres exactly as a load of a month still ahead does."""
        self._remove()

        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(202, 1)):
            asyncio.run(distrakt_store.import_premieres(
                self.user_id, self.month, load_settings()))

        doc = asyncio.run(distrakt_store.load_month(self.user_id, self.month))
        self.assertEqual([s["key"] for s in doc["shows"]], [])

    def test_an_imported_premiere_records_where_it_came_from(self):
        """Provenance no longer decides whether a mark is written, but it is still
        what tells a reader — and the restore path — where a row came from."""
        with patch("app.calendar.cache.read_month", side_effect=_fake_premiere_read(808, 1)):
            asyncio.run(distrakt_store.import_premieres(
                self.user_id, self.month, load_settings()))
        doc = asyncio.run(distrakt_store.load_month(self.user_id, self.month))
        added = next(s for s in doc["shows"] if s["ids"]["trakt"] == 808)
        self.assertEqual(added["added_by"], distrakt_store.ADDED_BY_CALENDAR)


if __name__ == "__main__":
    unittest.main()
