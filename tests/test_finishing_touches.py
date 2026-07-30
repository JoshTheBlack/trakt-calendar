"""The last round of surfaces: the tracker's backup panel, the share link's
display options, the share page's own view controls, and self-service
credentials.

Each of these closes a gap where the machinery already existed and only the way
in was missing, so these tests are mostly about the SEAM — that the control
reaches the function behind it, and that the destructive one cannot be reached
by accident.

No network anywhere: the Trakt window fetch is patched where a read would
otherwise reach for it, and token revocation is patched wherever an unlink runs.

Run: ./.venv/Scripts/python.exe -m unittest tests.test_finishing_touches -v
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

os.environ["TRAKT_DATA_DIR"] = tempfile.mkdtemp(prefix="tns-finishing-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, cache, calendar_cache, calendar_state, db, distrakt as distrakt_store, share_code, share_links  # noqa: E402
from app.providers.trakt import TraktError  # noqa: E402
from app.providers.trakt import transport as trakt_transport  # noqa: E402
from app.config import Settings, load_settings, save_settings  # noqa: E402
from app.providers.base import Item, ItemKey, Media, Source  # noqa: E402
from app.main import app  # noqa: E402

TMP = Path(os.environ["TRAKT_DATA_DIR"])
ORIGIN = "https://testserver"


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


class FinishingTestCase(unittest.TestCase):
    _counter = 0

    def setUp(self):
        FinishingTestCase._counter += 1
        db.set_db_path(TMP / f"finishing-{FinishingTestCase._counter}.db")
        asyncio.run(db.migrate())
        # The configured origin has to match the one the client speaks, or the
        # cross-site rules refuse every save below for an unrelated reason.
        save_settings(Settings(public_base_url=ORIGIN))
        self.client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        self.admin_id = self._make_user("admin_user", is_admin=True, calendar_approved=True)

    def tearDown(self):
        self.client.close()
        db.close_thread_connection()

    def _make_user(self, username, password="hunter2hunter2", **flags) -> int:
        return asyncio.run(auth.create_user(
            username=username, password=password, settings=Settings(), **flags))

    def _link_trakt(self, user_id: int, provider_user_id: int, token: str | None = "tok") -> None:
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="trakt", provider_user_id=provider_user_id,
            access_token=token)))

    def sign_in_as(self, user_id: int) -> None:
        session_id = asyncio.run(auth.create_session(user_id))
        self.client.cookies.clear()
        self.client.cookies.set(auth.COOKIE_NAME_SECURE, session_id)

    def tracker_user(self, username="tracker") -> int:
        user_id = self._make_user(username, calendar_approved=True, distrakt_approved=True)
        self._link_trakt(user_id, provider_user_id=900 + user_id)
        return user_id


class BackupPanelTests(FinishingTestCase):
    """Download and restore, and the acknowledgement in front of the destructive
    half."""

    def setUp(self):
        super().setUp()
        self.user_id = self.tracker_user()
        self.sign_in_as(self.user_id)

    def _add_show(self, user_id: int, title: str) -> None:
        asyncio.run(distrakt_store.add_show(user_id, "2026-07", {
            "ids": {"trakt": 11, "tmdb": 11, "slug": "a-show"}, "season": 1, "title": title,
            "network": "HBO", "media": "show",
        }))

    def test_the_page_offers_a_download_and_a_restore(self):
        body = self.client.get("/distrakt").text
        self.assertIn('href="/api/distrakt/export"', body)
        self.assertIn('id="restoreFile"', body)

    def test_the_restore_control_demands_a_typed_acknowledgement(self):
        """Restore replaces rather than merges, so the page asks for a phrase
        that has to be read and copied — a confirm dialog can be dismissed by
        reflex, and this cannot."""
        body = self.client.get("/distrakt").text
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


class RemovingFromTheTrackerTests(FinishingTestCase):
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
        with patch("app.distrakt_routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
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

        with patch("app.calendar_cache.read_month", side_effect=_fake_premiere_read(202, 1)):
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
        with patch("app.calendar_cache.read_month", side_effect=_fake_premiere_read(606, 1)):
            asyncio.run(distrakt_store.import_premieres(self.user_id, "2026-08", load_settings()))
        doc = asyncio.run(distrakt_store.load_month(self.user_id, "2026-08"))
        added = next(s for s in doc["shows"] if s["ids"]["trakt"] == 606)
        self.assertEqual(added["added_by"], distrakt_store.ADDED_BY_CALENDAR)


class LegacyRowRemovalTests(FinishingTestCase):
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
        with patch("app.distrakt_routes._distrakt_month_payload", return_value=({"ok": True}, 200)):
            return self.client.post("/api/distrakt/remove", json={
                "year": 2026, "month": 8, "key": f"show:tmdb:{tid}", "season": 1})

    def test_a_legacy_row_the_calendar_would_re_add_is_marked(self):
        self._add_legacy(707, "slug-707")
        with patch("app.calendar_cache.read_month", side_effect=_fake_premiere_read(707, 1)):
            resp = self._remove(707)
        self.assertTrue(resp.json()["hidden_on_calendar"])
        self.assertIn("slug-707", asyncio.run(calendar_state.not_watching_ids(self.user_id)))

    def test_a_legacy_row_the_calendar_knows_nothing_about_is_left_alone(self):
        self._add_legacy(808, "slug-808")
        with patch("app.calendar_cache.read_month", side_effect=_fake_premiere_read(707, 1)):
            resp = self._remove(808)
        self.assertFalse(resp.json()["hidden_on_calendar"])
        self.assertEqual(asyncio.run(calendar_state.not_watching_ids(self.user_id)), set())

    def test_an_unreachable_calendar_marks_nothing_rather_than_guessing(self):
        """Not knowing means not marking: the row comes back, which is annoying
        and undoable, where a wrong mark hides a show the user still wants."""
        async def boom(endpoint, settings, **kw):
            raise TraktError("unreachable")

        self._add_legacy(909, "slug-909")
        with patch("app.calendar_cache.read_month", side_effect=boom):
            resp = self._remove(909)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["hidden_on_calendar"])
        self.assertEqual(asyncio.run(calendar_state.not_watching_ids(self.user_id)), set())


class ShareLinkViewOptionsTests(FinishingTestCase):
    """The display options written into the generated link — and the promise
    that they are written into the LINK and nowhere else."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("linkowner", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def _share(self) -> dict:
        return self.client.get("/api/me/share").json()

    def test_the_default_link_carries_no_view_params(self):
        """"Use my current display" is the absence of params: the page then
        resolves the owner's own defaults, which is exactly what the owner is
        currently looking at."""
        payload = self._share()
        self.assertIsNone(payload["link_view"])
        self.assertNotIn("?", payload["urls"]["token"])

    def _link_view_of(self, url: str) -> dict:
        """What a generated link actually says, however it says it: the short
        `?p=` code these are handed out as, or the long query it falls back to."""
        query = dict(parse_qsl(urlsplit(url).query))
        return share_code.decode(query["p"]) if "p" in query else query

    def test_chosen_options_are_written_into_the_link(self):
        view = {"endpoint": "shows/premieres", "card": "poster", "packing": "packed",
                "hidenw": "1", "tz": "America/New_York"}
        resp = self.client.post("/api/me/share/view", json={"view": view})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._link_view_of(resp.json()["urls"]["token"]), view)

    def test_the_link_is_handed_out_short(self):
        """A link people paste into chat: one short code rather than seven
        params, two of them percent-encoded."""
        self.client.post("/api/me/share/view", json={"view": {
            "endpoint": "shows/premieres", "card": "horizontal", "packing": "stacked",
            "hidenw": "1", "tz": "America/New_York", "year": "2026", "month": "8",
        }})
        query = urlsplit(self._share()["urls"]["token"]).query
        self.assertLess(len(query), 20, query)
        self.assertEqual(list(dict(parse_qsl(query))), ["p"])

    def test_the_link_options_do_not_touch_the_owners_own_view(self):
        """THE POINT OF THE WHOLE DESIGN. Customizing a link somebody else will
        open must not change how the owner's private calendar renders."""
        before = asyncio.run(auth.get_user_prefs(self.user_id))
        before_tz = asyncio.run(auth.get_user(self.user_id))["timezone"]

        self.client.post("/api/me/share/view", json={"view": {
            "endpoint": "shows/finales", "card": "poster", "packing": "packed",
            "hidenw": "1", "tz": "Pacific/Auckland",
        }})

        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id)), before)
        self.assertEqual(asyncio.run(auth.get_user(self.user_id))["timezone"], before_tz)

    def test_the_link_options_do_not_touch_the_share_pages_own_defaults(self):
        """The owner-default columns are the fallback for a link that carries no
        params. Writing the chosen options into them would make "use my current
        display" mean the customized view instead."""
        row = asyncio.run(share_links.get_or_create(self.user_id))
        before = {key: row[key] for key in ("endpoint", "card_style", "day_packing",
                                            "hide_not_watching", "timezone")}
        self.client.post("/api/me/share/view", json={"view": {"endpoint": "shows/finales"}})
        after = asyncio.run(share_links.get(self.user_id))
        self.assertEqual({key: after[key] for key in before}, before)

    def test_clearing_goes_back_to_a_bare_link(self):
        self.client.post("/api/me/share/view", json={"view": {"endpoint": "shows/finales"}})
        resp = self.client.post("/api/me/share/view", json={"view": None})
        self.assertIsNone(resp.json()["link_view"])
        self.assertNotIn("?", resp.json()["urls"]["token"])

    def test_an_unusable_option_is_refused_rather_than_silently_dropped(self):
        """These end up in a URL handed to someone else. A value the page would
        ignore is a link that quietly does not do what its author set."""
        for view in ({"endpoint": "shows/imaginary"}, {"card": "hologram"},
                     {"packing": "sideways"}, {"hidenw": "yes"},
                     {"tz": "Mars/Olympus_Mons"}, {"nonsense": "1"}):
            with self.subTest(view=view):
                resp = self.client.post("/api/me/share/view", json={"view": view})
                self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._share()["link_view"])

    def test_the_options_apply_to_whichever_link_form_is_generated(self):
        self.client.post("/api/me/share/enabled", json={"kind": "username", "enabled": True})
        self.client.post("/api/me/share/view", json={"view": {"endpoint": "shows/premieres"}})
        urls = self._share()["urls"]
        for kind in ("token", "username"):
            with self.subTest(kind=kind):
                self.assertEqual(self._link_view_of(urls[kind]), {"endpoint": "shows/premieres"})

    def test_a_pinned_month_is_written_into_the_link(self):
        resp = self.client.post("/api/me/share/view", json={"view": {"year": "2026", "month": "8"}})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._link_view_of(resp.json()["urls"]["token"]),
                         {"year": "2026", "month": "8"})

    def test_half_a_pinned_month_is_refused(self):
        """A month with no year means something different once the year turns
        over, and a year with no month is not a month at all."""
        for view in ({"year": "2026"}, {"month": "8"},
                     {"year": "2026", "month": "13"}, {"year": "40000", "month": "8"}):
            with self.subTest(view=view):
                resp = self.client.post("/api/me/share/view", json={"view": view})
                self.assertEqual(resp.status_code, 400)
        self.assertIsNone(self._share()["link_view"])

    def test_a_link_with_no_pinned_month_opens_on_the_current_one(self):
        """The default stays what it was: nothing in the URL, so the page lands
        on whatever month it is opened in."""
        self.client.post("/api/me/share/view", json={"view": {"card": "poster"}})
        self.assertEqual(self._link_view_of(self._share()["urls"]["token"]), {"card": "poster"})

    def test_the_pinned_month_is_the_month_the_public_page_opens_on(self):
        """End to end: what the panel pins is where a visitor lands, without
        them having to touch the month arrows."""
        self.client.post("/api/me/share/view", json={"view": {"year": "2026", "month": "8"}})
        url = self._share()["urls"]["token"]
        self.client.cookies.clear()
        resp = self.client.get(url.replace(ORIGIN, ""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("August 2026", resp.text)

    def test_the_generated_link_actually_opens_on_the_chosen_view(self):
        """End to end: the params the panel writes are the params the public
        page honours."""
        self.client.post("/api/me/share/view", json={"view": {"card": "poster"}})
        url = self._share()["urls"]["token"]
        self.client.cookies.clear()
        resp = self.client.get(url.replace(ORIGIN, "") + "&year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card-poster", resp.text)


class ShareCodeArrivalTests(FinishingTestCase):
    """A `?p=` code is only how a link is handed out. On arrival it becomes the
    ordinary query params, so everything else on the page — the month arrows,
    the view controls, a visitor's own bookmark — deals only with those."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("codeowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = self.client.get("/api/me/share").json()["token"]
        self.client.cookies.clear()

    def _arrive(self, query: str):
        return self.client.get(f"/s/{self.token}?{query}", follow_redirects=False)

    def test_a_code_redirects_to_the_plain_url(self):
        code = share_code.encode({"card": "poster", "year": "2026", "month": "8"})
        resp = self._arrive(f"p={code}")
        self.assertEqual(resp.status_code, 302)
        target = urlsplit(resp.headers["location"])
        self.assertEqual(target.path, f"/s/{self.token}")
        self.assertEqual(dict(parse_qsl(target.query)),
                         {"card": "poster", "year": "2026", "month": "8"})

    def test_the_code_never_survives_the_redirect(self):
        """Including when it decoded to nothing — a `p` left in the URL would be
        a param the page silently ignores from then on."""
        for query in ("p=nonsense", "p=", f"p={share_code.encode({'card': 'poster'})}"):
            with self.subTest(query=query):
                resp = self._arrive(query)
                self.assertEqual(resp.status_code, 302)
                self.assertNotIn("p=", resp.headers["location"])

    def test_a_param_the_visitor_typed_wins_over_the_code(self):
        """The month arrows work this way: they carry the URL they were built
        from and set their own year/month on top."""
        code = share_code.encode({"card": "poster", "year": "2026", "month": "8"})
        resp = self._arrive(f"p={code}&month=9")
        self.assertEqual(dict(parse_qsl(urlsplit(resp.headers["location"]).query)),
                         {"card": "poster", "year": "2026", "month": "9"})

    def test_an_unusable_link_is_still_a_flat_404(self):
        """Not a redirect that then 404s: a miss stays identical whatever the
        reason, and costs a stranger nothing to discover."""
        code = share_code.encode({"card": "poster"})
        resp = self.client.get(f"/s/nosuchtoken?p={code}", follow_redirects=False)
        self.assertEqual(resp.status_code, 404)

    def test_following_the_redirect_lands_on_the_coded_view(self):
        code = share_code.encode({"card": "poster", "year": "2026", "month": "8"})
        resp = self.client.get(f"/s/{self.token}?p={code}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card-poster", resp.text)
        self.assertIn("August 2026", resp.text)

    def test_a_long_link_still_works_untouched(self):
        """Every link handed out before the short form existed stays valid."""
        resp = self.client.get(f"/s/{self.token}?card=poster&year=2026&month=8",
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card-poster", resp.text)


class SharePageViewControlsTests(FinishingTestCase):
    """The visitor's own controls on a public page: GET-only, no session."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("pageowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = self.client.get("/api/me/share").json()["token"]
        self.client.cookies.clear()

    def test_the_page_renders_view_controls(self):
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('name="endpoint"', body)
        self.assertIn('name="card"', body)
        self.assertIn('name="packing"', body)
        self.assertIn('name="tz"', body)
        self.assertIn('name="hidenw"', body)

    def test_the_controls_are_a_get_form_and_add_no_write_surface(self):
        """A public page has no session to write with. The controls are the same
        whitelisted query params a hand-edited URL already carries."""
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('method="get"', body)
        self.assertNotIn('method="post"', body.lower())

    def test_they_reflect_what_the_url_asked_for(self):
        body = self.client.get(f"/s/{self.token}?year=2026&month=7&card=poster").text
        self.assertRegex(body, r'<option value="poster" selected>')

    def test_the_month_stays_put_when_a_view_option_changes(self):
        """The form carries year/month as hidden fields, so switching the card
        style does not bounce the visitor back to today."""
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('<input type="hidden" name="year" value="2026">', body)
        self.assertIn('<input type="hidden" name="month" value="7">', body)

    def test_hide_not_watching_always_sends_a_value(self):
        """A select rather than a checkbox: an unchecked box is omitted from the
        query entirely, which reads as "unspecified" and falls back to the
        owner's default instead of to "show everything"."""
        body = self.client.get(f"/s/{self.token}?year=2026&month=7&hidenw=1").text
        self.assertIn('<option value="0"', body)
        self.assertIn('<option value="1" selected>', body)


class SharePageDetailsModalTests(FinishingTestCase):
    """Clicking a card on a public page opens the SAME details modal as the
    calendar page (cast, trailer, episodes) — served CACHE-ONLY from what the
    owner's own views already fetched, so a public click never calls Trakt."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                               trakt_access_token="tok"))
        self.user_id = self._make_user("modalowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = self.client.get("/api/me/share").json()["token"]
        self.client.cookies.clear()
        # A cached window so the page renders a card to wire up.
        entry = {
            "first_aired": "2026-07-15T20:00:00Z",
            "episode": {"season": 2, "number": 5, "title": "The One"},
            "show": {"title": "Test Show", "year": 2026, "network": "HBO",
                     "country": "us", "language": "en", "runtime": 50,
                     "status": "returning series", "rating": 8.4, "genres": ["drama"],
                     "overview": "A tense plot.",
                     "ids": {"slug": "test-show", "trakt": 123, "tmdb": 789}},
        }
        start = calendar_cache.window_start(date(2026, 7, 15))
        asyncio.run(calendar_cache.store_window(
            "shows/new", start, [entry], 600, db.now()))

    def _seed_detail_cache(self):
        """Write the raw Trakt payloads the OWNER's own detail view would have
        cached, at the exact URLs fetch_details reads."""
        from urllib.parse import urlencode
        base = f"{trakt_transport.API_BASE}"
        ext = urlencode({"extended": "full"})
        asyncio.run(cache.set(f"{base}/shows/123?{ext}", {
            "title": "Test Show", "year": 2026, "overview": "Full overview.",
            "status": "returning_series", "network": "HBO", "runtime": 50,
            "genres": ["drama"], "rating": 8.4,
            "trailer": "https://youtu.be/dQw4w9WgXcQ"}))
        asyncio.run(cache.set(f"{base}/shows/123/people?{ext}", {
            "cast": [{"person": {"name": "A Actor"}, "character": "Lead"}]}))
        asyncio.run(cache.set(f"{base}/shows/123/seasons/2?{ext}", [
            {"number": 5, "title": "The One", "first_aired": "2026-07-15T20:00:00Z",
             "rating": 8.0}]))

    def _no_network(self):
        """A client whose .get fails the test — cache_only must never reach it."""
        class _Boom:
            async def get(self, *a, **k):
                raise AssertionError("share details must not call Trakt")
        return patch("app.providers.trakt.transport.shared_client", return_value=_Boom())

    def test_the_page_wires_cards_to_the_modal(self):
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('onclick="openShareDetails(this, event)"', body)
        self.assertIn('id="detailsModal"', body)
        self.assertIn("/static/js/share.js", body)

    def test_details_serve_the_owners_cached_data_without_calling_trakt(self):
        self._seed_detail_cache()
        with self._no_network():
            resp = self.client.get(f"/s/{self.token}/details?media=show&id=123&season=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        d = resp.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["overview"], "Full overview.")
        self.assertEqual(d["status"], "Returning Series")  # normalized like the calendar
        self.assertEqual(d["cast"][0]["name"], "A Actor")
        self.assertTrue(d["trailer"])
        self.assertEqual(d["episodes"][0]["number"], 5)

    def test_an_uncached_show_returns_ok_with_empty_fields(self):
        """A show the owner never opened has nothing cached — the modal renders
        around the blanks rather than triggering a fetch."""
        with self._no_network():
            resp = self.client.get(f"/s/{self.token}/details?media=show&id=555&season=1")
        self.assertEqual(resp.status_code, 200, resp.text)
        d = resp.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["overview"], "")
        self.assertEqual(d["cast"], [])

    def test_details_reach_through_the_username_url_form_too(self):
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        self._seed_detail_cache()
        with self._no_network():
            resp = self.client.get("/u/modalowner/details?media=show&id=123&season=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["cast"][0]["name"], "A Actor")

    def test_a_bad_token_details_request_is_a_404(self):
        with self._no_network():
            resp = self.client.get("/s/not-a-real-token/details?media=show&id=123&season=2")
        self.assertEqual(resp.status_code, 404)


class SelfServiceCredentialsTests(FinishingTestCase):
    """Claiming a username and setting a password without an administrator."""

    def test_an_oauth_only_account_can_claim_a_username(self):
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4141)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/username", json={"username": "claimed"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(asyncio.run(auth.get_user(user_id))["username"], "claimed")

    def test_changing_an_existing_username_is_not_self_service(self):
        """A username is a public identifier — it is what /u/<name> links are
        built from — so handing it over would break links already shared and
        free the old name for someone else."""
        user_id = self._make_user("settled", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/username", json={"username": "different"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(asyncio.run(auth.get_user(user_id))["username"], "settled")

    def test_a_taken_or_reserved_username_is_refused(self):
        self._make_user("taken_name", calendar_approved=True)
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4242)
        self.sign_in_as(user_id)
        for name in ("taken_name", "admin", "!!"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.post("/api/me/username", json={"username": name}).status_code, 400)

    def test_a_username_may_not_collide_with_someone_elses_slug(self):
        """The cross-namespace rule holds on this path too, not just on
        registration."""
        owner = self._make_user("slugowner", calendar_approved=True)
        self.sign_in_as(owner)
        self.client.post("/api/me/share/slug", json={"slug": "wanted-name"})

        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4343)
        self.sign_in_as(user_id)
        self.assertEqual(
            self.client.post("/api/me/username", json={"username": "wanted-name"}).status_code, 400)

    def test_an_oauth_only_account_can_set_a_first_password(self):
        """No current password is asked for, because there is none — the live
        session is the only credential such an account has, and demanding one
        would make this unreachable for exactly the accounts that need it."""
        user_id = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=4444)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/password", json={
            "password": "brand-new-secret", "password_confirm": "brand-new-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "brand-new-secret")).ok)

    def test_changing_a_password_requires_the_current_one(self):
        user_id = self._make_user("haspw", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/password", json={
            "current_password": "wrong-one-entirely",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 403)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "hunter2hunter2")).ok)

    def test_the_right_current_password_changes_it(self):
        user_id = self._make_user("haspw2", calendar_approved=True)
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)
        stored = asyncio.run(auth.get_user(user_id))["password_hash"]
        self.assertTrue(asyncio.run(auth.verify_password(stored, "replacement-secret")).ok)

    def test_a_mismatch_or_a_short_password_is_refused(self):
        user_id = self._make_user("haspw3", calendar_approved=True)
        self.sign_in_as(user_id)
        for body in ({"password": "long-enough-here", "password_confirm": "something-else"},
                     {"password": "short", "password_confirm": "short"}):
            with self.subTest(body=body):
                resp = self.client.post("/api/me/password",
                                        json={"current_password": "hunter2hunter2", **body})
                self.assertEqual(resp.status_code, 400)

    def test_a_password_change_evicts_other_sessions_but_not_this_one(self):
        """A change after a compromise has to actually remove the other party,
        and signing the person out of the tab they just used would read as a
        failure."""
        user_id = self._make_user("evicter", calendar_approved=True)
        elsewhere = asyncio.run(auth.create_session(user_id))
        self.sign_in_as(user_id)

        resp = self.client.post("/api/me/password", json={
            "current_password": "hunter2hunter2",
            "password": "replacement-secret", "password_confirm": "replacement-secret"})
        self.assertEqual(resp.status_code, 200, resp.text)

        self.assertIsNone(asyncio.run(auth.validate_session(elsewhere)))
        # Still signed in here, on the cookie the response reissued.
        self.assertEqual(self.client.get("/me").status_code, 200)

    def test_both_routes_need_a_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/me/username", json={"username": "x"}).status_code, 401)
        self.assertEqual(self.client.post("/api/me/password", json={"password": "y"}).status_code, 401)

    def test_the_account_page_offers_the_forms_that_apply(self):
        oauth_only = self._make_user(None, password=None, calendar_approved=True)
        self._link_trakt(oauth_only, provider_user_id=4545)
        self.sign_in_as(oauth_only)
        body = self.client.get("/me").text
        self.assertIn('id="usernameForm"', body)
        self.assertIn("Set a password", body)
        self.assertNotIn('id="currentPassword"', body)

        self.sign_in_as(self._make_user("named", calendar_approved=True))
        body = self.client.get("/me").text
        self.assertNotIn('id="usernameForm"', body)
        self.assertIn('id="currentPassword"', body)


class CacheSettingsWidgetTests(FinishingTestCase):
    """The two cache settings that had no control on the admin screen."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_screen_renders_both_inputs(self):
        body = self.client.get("/?month=1&year=2026").text
        self.assertIn('name="calendar_cache_ttl_minutes"', body)
        self.assertIn('name="api_cache_max_bytes"', body)

    def test_saving_them_persists(self):
        resp = self.client.post("/api/settings", json={
            "calendar_cache_ttl_minutes": 25,
            "api_cache_max_bytes": 512 * 1024 * 1024,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = load_settings()
        self.assertEqual(settings.calendar_cache_ttl_minutes, 25)
        self.assertEqual(settings.api_cache_max_bytes, 512 * 1024 * 1024)

    def test_they_are_readable_back_through_the_settings_endpoint(self):
        """The screen loads its values from here, so a field missing from the
        response is a field that renders blank and then saves a zero."""
        payload = self.client.get("/api/settings").json()
        self.assertIn("calendar_cache_ttl_minutes", payload)
        self.assertIn("api_cache_max_bytes", payload)


class PrewarmSettingWidgetTests(FinishingTestCase):
    """calendar_prewarm_enabled: the checkbox and its round trip through
    /api/settings, coerced to a real bool the way hide_not_watching is."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_the_settings_screen_renders_the_toggle(self):
        body = self.client.get("/?month=1&year=2026").text
        self.assertIn('name="calendar_prewarm_enabled"', body)

    def test_saving_it_persists_as_a_bool(self):
        resp = self.client.post("/api/settings", json={"calendar_prewarm_enabled": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = load_settings()
        self.assertIs(settings.calendar_prewarm_enabled, True)

        resp = self.client.post("/api/settings", json={"calendar_prewarm_enabled": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(load_settings().calendar_prewarm_enabled, False)

    def test_a_checkbox_style_string_value_coerces_to_a_bool(self):
        """A form posts "true"/"false", not a JSON boolean; _as_bool must still
        turn that into a real bool rather than storing the truthy string."""
        resp = self.client.post("/api/settings", json={"calendar_prewarm_enabled": "true"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(load_settings().calendar_prewarm_enabled, True)

    def test_it_is_readable_back_through_the_settings_endpoint(self):
        payload = self.client.get("/api/settings").json()
        self.assertIn("calendar_prewarm_enabled", payload)


class SettingsTabsTests(FinishingTestCase):
    """Settings is four tabbed groups in one form."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def _body(self) -> str:
        return self.client.get("/?month=1&year=2026").text

    def test_every_tab_has_a_panel_and_only_the_first_is_showing(self):
        body = self._body()
        tabs = re.findall(r'data-tab="([\w-]+)"', body)
        panels = re.findall(r'data-tab-panel="([\w-]+)"', body)
        self.assertEqual(tabs, ["server", "trakt", "calendar", "integrations"])
        self.assertEqual(panels, tabs)
        # Three of the four start hidden; the CSS cannot be relied on to hide
        # them, so the attribute has to be in the markup.
        self.assertEqual(len(re.findall(r'data-tab-panel="\w+" role="tabpanel" hidden', body)), 3)

    def test_no_field_was_dropped_on_the_way_into_the_tabs(self):
        """The regrouping moved markup around every input the save path reads by
        id, and a field left behind would save as a blank or a zero."""
        body = self._body()
        for field_id in ("s_base_url", "s_trusted_proxies", "s_client_id", "s_client_secret",
                         "s_access_token", "s_timezone", "s_endpoint", "s_limit", "s_cache",
                         "s_calcache", "s_cachecap", "s_hide", "s_sonarr_url", "s_sonarr_key",
                         "s_radarr_url", "s_radarr_key", "s_seer_url", "s_seer_key",
                         "s_tmdb_key"):
            with self.subTest(field=field_id):
                self.assertIn(f'id="{field_id}"', body)

    def test_the_reconnect_notice_sits_outside_the_tabs(self):
        """It is an alert about the instance, and an alert that only appears on
        the tab you happen to be standing on is one you can miss."""
        body = self._body()
        notice = body.index('id="s_reconnect_box"')
        first_panel = body.index('data-tab-panel="server"')
        self.assertLess(notice, first_panel)

    def test_one_save_still_writes_fields_from_several_tabs(self):
        """Tabs are presentation only — the panels share a single form, so a
        value from the Server tab and one from Integrations go together."""
        resp = self.client.post("/api/settings", json={
            "public_base_url": ORIGIN, "sonarr_url": "http://localhost:8989",
            "calendar_cache_ttl_minutes": 30,
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        settings = load_settings()
        self.assertEqual(settings.sonarr_url, "http://localhost:8989")
        self.assertEqual(settings.calendar_cache_ttl_minutes, 30)


class ErrorPageTests(FinishingTestCase):
    """A mistyped address gets a page, not Starlette's raw JSON."""

    def setUp(self):
        super().setUp()
        self.sign_in_as(self.admin_id)

    def test_a_browser_gets_the_themed_page(self):
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Let's all go to the lobby", resp.text)
        self.assertIn("Back to the calendar", resp.text)

    def test_the_lobby_page_carries_our_own_favicon(self):
        """The page came from somewhere else. Its own icon did not come with it."""
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertIn("/static/images/favicon.ico", resp.text)

    def test_the_lobby_page_stands_up_without_the_stylesheet(self):
        """Styles are inline on purpose: the page that renders when something is
        already wrong cannot depend on a second request succeeding."""
        resp = self.client.get("/no-such-page", headers={"Accept": "text/html"})
        self.assertNotIn("css/style.css", resp.text)

    def test_a_refused_page_keeps_the_plain_card(self):
        """403 is not a place to be charming. It also keeps error.html wired to
        something, so the fallback cannot rot unnoticed while the lobby page is
        the one everybody looks at.

        Driven through the handler rather than a URL because the routes that
        refuse a signed-in reader answer by rendering their own page, not by
        raising — there is no request that reaches this branch with a 403.
        """
        from fastapi import Request
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from app.main import handle_http_exception

        request = Request({"type": "http", "method": "GET", "path": "/nope",
                           "headers": [(b"accept", b"text/html")],
                           "query_string": b"", "app": app})
        resp = asyncio.run(handle_http_exception(
            request, StarletteHTTPException(status_code=403)))
        self.assertEqual(resp.status_code, 403)
        body = resp.body.decode()
        self.assertIn("error-card", body)
        self.assertNotIn("Let's all go to the lobby", body)

    def test_a_script_still_gets_json(self):
        """fetch() sends Accept: */*, and a caller that parses the body has to
        keep getting something parseable."""
        resp = self.client.get("/no-such-page")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("application/json", resp.headers["content-type"])
        self.assertFalse(resp.json()["ok"])

    def test_a_wrong_method_reads_as_not_found(self):
        """Answering "that exists but not like that" tells a stranger which
        addresses are real."""
        resp = self.client.post("/no-such-page", json={}, headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("405", resp.text)

    def test_the_page_names_no_route_and_offers_no_inventory(self):
        """It says the same thing for a never-existed path and for one that is
        simply not this account's to open."""
        secret = self.client.get("/api/admin/hidden-thing", headers={"Accept": "text/html"}).text
        typo = self.client.get("/calender", headers={"Accept": "text/html"}).text
        self.assertNotIn("hidden-thing", secret)
        self.assertNotIn("calender", typo)

    def test_the_share_pages_keep_their_own_wording(self):
        """A dead share link says so specifically — it is a different question
        from a mistyped address, and the answer is more useful."""
        resp = self.client.get("/s/not-a-real-token", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("shared calendar", resp.text)


class TokenRevocationOnUnlinkTests(FinishingTestCase):
    """Unlinking asks Trakt to forget the authorization rather than leaving it
    standing in the user's connected-apps list."""

    def setUp(self):
        super().setUp()
        save_settings(Settings(
            public_base_url=ORIGIN, trakt_client_id="cid", trakt_client_secret="secret"))
        self.revoked: list[str] = []

        async def _revoke(client_id, client_secret, access_token):
            self.revoked.append(access_token)

        patcher = patch("app.trakt_auth.revoke_token", side_effect=_revoke)
        self.revoke_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_admin_unlink_revokes_too(self):
        victim = self._make_user("victim", calendar_approved=True)
        self._link_trakt(victim, provider_user_id=5151, token="victim-token")
        self.sign_in_as(self.admin_id)
        resp = self.client.post(
            f"/api/admin/users/{victim}/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, ["victim-token"])

    def test_an_admin_unlink_that_asks_for_confirmation_revokes_nothing_yet(self):
        """The first call comes back asking for `force` and the identity stays.
        Killing its token on the way past would leave the account linked to a
        credential that no longer works."""
        orphan = self._make_user(None, password=None)
        self._link_trakt(orphan, provider_user_id=5252, token="orphan-token")
        self.sign_in_as(self.admin_id)
        resp = self.client.post(
            f"/api/admin/users/{orphan}/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.revoked, [])
        self.assertEqual(len(asyncio.run(auth.list_identities(orphan))), 1)

        forced = self.client.post(
            f"/api/admin/users/{orphan}/identities/unlink",
            json={"provider": "trakt", "force": True})
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual(self.revoked, ["orphan-token"])

    def test_unlinking_plex_asks_trakt_nothing(self):
        user_id = self._make_user("plexy", calendar_approved=True)
        asyncio.run(db.run(lambda conn: auth.insert_linked_identity(
            conn, user_id=user_id, provider="plex", provider_user_id=6161, access_token="p")))
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "plex"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, [])

    def test_without_configured_credentials_it_says_so_rather_than_calling(self):
        """Revocation is authenticated with the app's own credentials. Without
        them there is no call to make, and saying so beats silently doing
        nothing."""
        save_settings(Settings(public_base_url=ORIGIN))
        user_id = self._make_user("uncfg", calendar_approved=True)
        self._link_trakt(user_id, provider_user_id=7171, token="stranded")
        self.sign_in_as(user_id)
        resp = self.client.post("/api/me/identities/unlink", json={"provider": "trakt"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.revoked, [])
        self.assertIn("trakt.tv", resp.json()["warning"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
