"""What a share link carries and what the page it opens lets a visitor do.

The link's data layer is tested in tests/calendar/test_share_links.py and the
compact code in tests/calendar/test_share_code.py; this file is about the two
ends the visitor sees — that the options an owner set travel in the link and
arrive applied, and that the page's own controls and details modal work for
somebody with no account at all.

No network: the share page reads the calendar cache, and the details modal is
answered from the cache alone.
"""
from __future__ import annotations

import unittest
import asyncio
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from app import auth, cache, calendar_cache, db, share_code, share_links
from app.providers.trakt import transport as trakt_transport
from app.config import Settings, save_settings
from tests.support import AppTestCase, ORIGIN


class SharePageTestCase(AppTestCase):
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


class ShareLinkViewOptionsTests(SharePageTestCase):
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


class ShareCodeArrivalTests(SharePageTestCase):
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


class SharePageViewControlsTests(SharePageTestCase):
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


class SharePageDetailsModalTests(SharePageTestCase):
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


if __name__ == "__main__":
    unittest.main()
