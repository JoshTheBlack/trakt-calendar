"""What a share link carries and what the page it opens lets a visitor do.

The link's data layer is tested in tests/calendar/test_share_links.py and the
compact code in tests/calendar/test_share_code.py; this file is about the two
ends the visitor sees — that the options an owner set travel in the link and
arrive applied, and that the page's own controls and details modal work for
somebody with no account at all.

No network: the share page reads the calendar cache, and the details modal may
only look a title up when the owner's cached month already shows it — so every
call it could make is stood in for by a double that either records the call or
fails the test on being reached.
"""
from __future__ import annotations

import re
import unittest
import asyncio
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlsplit

from app import auth, cache, db
from app.endpoints import get_endpoint
from app.calendar import (cache as calendar_cache, share_card, share_code,
                          share_links, share_routes)
from app.providers.base import Item, Media, Source
from app.providers.trakt import transport as trakt_transport
from app.config import Settings, save_settings
from app.media import posters
from tests.support import AppTestCase, DetailsPanel, ORIGIN, calendar_records


async def _no_poster_warm(settings, refs) -> int:
    return 0


class SharePageTestCase(AppTestCase):
    def make_settings(self):
        # The configured origin has to match the one the client speaks, or the
        # cross-site rules refuse every save below for an unrelated reason.
        return Settings(public_base_url=ORIGIN)

    def setUp(self):
        super().setUp()
        # Rendering a share page starts resolving the poster artwork its preview
        # picture will want, in the background and without waiting for it. None
        # of the tests in this file are about that, and leaving it live would
        # make them depend on whether a background task happens to get a slice
        # before the client's event loop is torn down — so the one outbound call
        # it can make is stubbed here, deterministically.
        warm = patch.object(posters, "ensure_posters", _no_poster_warm)
        warm.start()
        self.addCleanup(warm.stop)
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
    that they are written into the LINK and nowhere else.

    ASKED OF THE DATA LAYER (share_links.set_link_view), because that is where
    the rule lives: the Share panel's own route turns a form into a view and is
    held to its half in tests/calendar/test_share_panel.py. What is here is what
    a view MEANS once stored — the link it produces and the page that opens."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("linkowner", calendar_approved=True)
        self.sign_in_as(self.user_id)

    def _set(self, view):
        return asyncio.run(share_links.set_link_view(self.user_id, view))

    def _share(self) -> dict:
        row = asyncio.run(share_links.get_or_create(self.user_id))
        return {"link_view": share_links.link_view(row),
                "urls": share_links.generated_urls(row, "linkowner", ORIGIN)}

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
        self.assertIsNone(self._set(view))
        self.assertEqual(self._link_view_of(self._share()["urls"]["token"]), view)

    def test_the_link_is_handed_out_short(self):
        """A link people paste into chat: one short code rather than seven
        params, two of them percent-encoded."""
        self._set({
            "endpoint": "shows/premieres", "card": "horizontal", "packing": "stacked",
            "hidenw": "1", "tz": "America/New_York", "year": "2026", "month": "8",
        })
        query = urlsplit(self._share()["urls"]["token"]).query
        self.assertLess(len(query), 20, query)
        self.assertEqual(list(dict(parse_qsl(query))), ["p"])

    def test_the_link_options_do_not_touch_the_owners_own_view(self):
        """THE POINT OF THE WHOLE DESIGN. Customizing a link somebody else will
        open must not change how the owner's private calendar renders."""
        before = asyncio.run(auth.get_user_prefs(self.user_id))
        before_tz = asyncio.run(auth.get_user(self.user_id))["timezone"]

        self._set({
            "endpoint": "shows/finales", "card": "poster", "packing": "packed",
            "hidenw": "1", "tz": "Pacific/Auckland",
        })

        self.assertEqual(asyncio.run(auth.get_user_prefs(self.user_id)), before)
        self.assertEqual(asyncio.run(auth.get_user(self.user_id))["timezone"], before_tz)

    def test_the_link_options_do_not_touch_the_share_pages_own_defaults(self):
        """The owner-default columns are the fallback for a link that carries no
        params. Writing the chosen options into them would make "use my current
        display" mean the customized view instead."""
        row = asyncio.run(share_links.get_or_create(self.user_id))
        before = {key: row[key] for key in ("endpoint", "card_style", "day_packing",
                                            "hide_not_watching", "timezone")}
        self._set({"endpoint": "shows/finales"})
        after = asyncio.run(share_links.get(self.user_id))
        self.assertEqual({key: after[key] for key in before}, before)

    def test_clearing_goes_back_to_a_bare_link(self):
        self._set({"endpoint": "shows/finales"})
        self.assertIsNone(self._set(None))
        self.assertIsNone(self._share()["link_view"])
        self.assertNotIn("?", self._share()["urls"]["token"])

    def test_an_unusable_option_is_refused_rather_than_silently_dropped(self):
        """These end up in a URL handed to someone else. A value the page would
        ignore is a link that quietly does not do what its author set."""
        for view in ({"endpoint": "shows/imaginary"}, {"card": "hologram"},
                     {"packing": "sideways"}, {"hidenw": "yes"},
                     {"tz": "Mars/Olympus_Mons"}, {"nonsense": "1"},
                     {"source": "netflix"}, {"source": ""}, {"source": "trakt "}):
            with self.subTest(view=view):
                self.assertIsNotNone(self._set(view))
        self.assertIsNone(self._share()["link_view"])

    def test_the_options_apply_to_whichever_link_form_is_generated(self):
        self._set({"endpoint": "shows/premieres"})
        urls = self._share()["urls"]
        for kind in ("token", "username"):
            with self.subTest(kind=kind):
                self.assertEqual(self._link_view_of(urls[kind]), {"endpoint": "shows/premieres"})

    def test_a_named_source_is_written_into_the_link(self):
        """The one view option the panel was missing, and it travels the same
        way every other one does — short code included."""
        self.assertIsNone(self._set({"endpoint": "shows", "source": "simkl"}))
        url = self._share()["urls"]["token"]
        self.assertIn("?p=", url)
        self.assertEqual(self._link_view_of(url), {"endpoint": "shows", "source": "simkl"})

    def test_a_link_naming_no_source_says_nothing_about_sources(self):
        """"My sources" is the ABSENCE of the param, so the page resolves the
        owner's own preference — the standing behaviour of a share page, which a
        link that opted out of choosing must not change."""
        self._set({"card": "poster"})
        self.assertNotIn("source", self._link_view_of(self._share()["urls"]["token"]))

    def test_a_link_may_name_a_combination_of_services(self):
        """The vocabulary is a SET, so a link has to be able to say one — and to
        say it in the short form, since the number of combinations grows with
        every service registered and none of them may be the one that quietly
        stops fitting."""
        self.assertIsNone(self._set({"source": "trakt+simkl"}))
        url = self._share()["urls"]["token"]
        self.assertIn("?p=", url)
        self.assertEqual(self._link_view_of(url), {"source": "trakt+simkl"})

    def test_a_pinned_month_is_written_into_the_link(self):
        self.assertIsNone(self._set({"year": "2026", "month": "8"}))
        self.assertEqual(self._link_view_of(self._share()["urls"]["token"]),
                         {"year": "2026", "month": "8"})

    def test_half_a_pinned_month_is_refused(self):
        """A month with no year means something different once the year turns
        over, and a year with no month is not a month at all."""
        for view in ({"year": "2026"}, {"month": "8"},
                     {"year": "2026", "month": "13"}, {"year": "40000", "month": "8"}):
            with self.subTest(view=view):
                self.assertIsNotNone(self._set(view))
        self.assertIsNone(self._share()["link_view"])

    def test_a_link_with_no_pinned_month_opens_on_the_current_one(self):
        """The default stays what it was: nothing in the URL, so the page lands
        on whatever month it is opened in."""
        self._set({"card": "poster"})
        self.assertEqual(self._link_view_of(self._share()["urls"]["token"]), {"card": "poster"})

    def test_the_pinned_month_is_the_month_the_public_page_opens_on(self):
        """End to end: what the panel pins is where a visitor lands, without
        them having to touch the month arrows."""
        self._set({"year": "2026", "month": "8"})
        url = self._share()["urls"]["token"]
        self.client.cookies.clear()
        resp = self.client.get(url.replace(ORIGIN, ""))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("August 2026", resp.text)

    def test_the_generated_link_actually_opens_on_the_chosen_view(self):
        """End to end: the params the panel writes are the params the public
        page honours."""
        self._set({"card": "poster"})
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
        self.token = asyncio.run(share_links.get_or_create(self.user_id))["token"]
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

    def test_a_coded_source_arrives_as_the_ordinary_param(self):
        """The field the code grew last, through the same expansion as the rest:
        after arrival nothing on the page knows the short form exists."""
        code = share_code.encode({"source": "simkl", "year": "2026", "month": "8"})
        resp = self._arrive(f"p={code}")
        self.assertEqual(dict(parse_qsl(urlsplit(resp.headers["location"]).query)),
                         {"source": "simkl", "year": "2026", "month": "8"})

    def test_a_long_link_still_works_untouched(self):
        """Every link handed out before the short form existed stays valid."""
        resp = self.client.get(f"/s/{self.token}?card=poster&year=2026&month=8",
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("card-poster", resp.text)


class ASharePageOnAnInstanceWithNoCredentialsTests(SharePageTestCase):
    """The third surface reading "is there anybody to ask", and the one where
    widening the answer had to be thought about hardest.

    A share page is the one public path allowed an outbound call, so admitting a
    source that needs NO credential is exactly the case to check: more instances
    now have a calendar to serve a stranger, and none of them may let that
    stranger spend anything. The bound is `allow_fetch=False` in `_read_month`,
    which both the page and its preview picture come through.
    """

    def setUp(self):
        super().setUp()
        # No credential of any kind — the state that used to make every share
        # page an empty month whatever was cached.
        save_settings(Settings(public_base_url=ORIGIN))
        self.user_id = self._make_user("shareowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = asyncio.run(share_links.get_or_create(self.user_id))["token"]
        self.client.cookies.clear()
        entry = {
            "first_aired": "2026-07-15T20:00:00Z",
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "show": {"title": "Cached Show", "year": 2026, "country": "us",
                     "genres": ["drama"],
                     "ids": {"slug": "cached-show", "trakt": 321, "tmdb": 654}},
        }
        start = calendar_cache.window_start(date(2026, 7, 15))
        asyncio.run(calendar_cache.store_window(
            "shows/new", start, calendar_records([entry], get_endpoint("shows/new")),
            600, db.now(), sources=["trakt"]))

    def _no_fetch(self):
        """A window fill that fails the test. The share surfaces read the cache
        and only the cache; reaching this is the regression."""
        async def _boom(*args, **kwargs):
            raise AssertionError("a share request must never fetch a calendar")
        return patch("app.calendar.cache.fetch_window_records", _boom)

    def test_the_page_serves_what_is_cached_instead_of_an_empty_month(self):
        with self._no_fetch():
            resp = self.client.get(f"/s/{self.token}?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Cached Show", resp.text)

    def test_the_preview_picture_still_refuses_to_fetch_one(self):
        """The card draws the same view the page does and warms only artwork for
        titles the cache already holds. Widening who counts as a source must not
        widen what an anonymous request can make this instance spend."""
        with self._no_fetch():
            resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=7")
        self.assertEqual(resp.status_code, 200)


class SharePageViewControlsTests(SharePageTestCase):
    """The visitor's own controls on a public page: GET-only, no session."""

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("pageowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = asyncio.run(share_links.get_or_create(self.user_id))["token"]
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


class _ShareDetailsTestCase(SharePageTestCase):
    """Clicking a card on a public page opens the SAME details panel as the
    calendar page (cast, trailer, episodes) — literally the same template,
    rendered by this route.

    A REFUSAL IS DRAWN, NOT SIGNALLED, so the assertions below read the sentence
    the visitor is shown rather than a status code. That is the same rule every
    fragment in this app follows: a non-200 is never swapped into the page, so a
    status here would leave the panel on its loading line for ever. The refusal
    is ALSO one sentence for two different facts — "nobody can describe this" and
    "this link does not show that" — because a public page that told them apart
    would answer, for any id a stranger cared to try, whether the owner's
    calendar contains it. Every test below therefore asserts BOTH that the
    sentence came back and that the title's own words did not.

    THE ENDPOINT MAY SPEND A LOOKUP, AND ONLY ON A TITLE THIS LINK ALREADY
    SHOWS. A per-title answer is the same for every viewer and is cached
    instance-wide, so one fetch serves everybody — but the route is public and
    unauthenticated, so what may be looked up is bound to the owner's own cached
    month rather than read off the query string. The tests below drive all three
    dimensions of that bound (the id, the medium and the season), each against a
    stand-in that fails the test if it is reached: every one of them is a
    separate call upstream, so a bound that checked only one would still leave an
    amplifier behind.
    """

    # The view the cards below were drawn in. The request has to name it, because
    # the server re-reads that exact month to decide what it may describe — the
    # page's own query string is what share.js sends back.
    VIEW = "year=2026&month=7"

    def setUp(self):
        super().setUp()
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                               trakt_access_token="tok"))
        self.user_id = self._make_user("modalowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = asyncio.run(share_links.get_or_create(self.user_id))["token"]
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
            "shows/new", start, calendar_records([entry], get_endpoint("shows/new")),
            600, db.now(), sources=["trakt"]))

    def _details(self, query: str):
        return self.client.get(f"/s/{self.token}/details?{query}&{self.VIEW}")

    def _seed_detail_cache(self):
        """Write the raw Trakt payloads the OWNER's own detail view would have
        cached, at the exact URLs fetch_details reads."""
        from urllib.parse import urlencode
        base = f"{trakt_transport.API_BASE}"
        ext = urlencode({"extended": "full"})
        asyncio.run(cache.set(f"{base}/shows/123?{ext}", {
            "title": "Test Show", "year": 2026, "overview": "Full overview.",
            "status": "returning_series", "network": "HBO", "runtime": 50,
            "genres": ["drama"], "rating": 8.4}))
        asyncio.run(cache.set(f"{base}/shows/123/videos?", [
            {"title": "Official Trailer", "url": "https://youtube.com/watch?v=dQw4w9WgXcQ",
             "site": "youtube", "type": "trailer", "official": True}]))
        asyncio.run(cache.set(f"{base}/shows/123/people?{ext}", {
            "cast": [{"person": {"name": "A Actor"}, "character": "Lead"}]}))
        asyncio.run(cache.set(f"{base}/shows/123/seasons/2?{ext}", [
            {"number": 5, "title": "The One", "first_aired": "2026-07-15T20:00:00Z",
             "rating": 8.0}]))
        # THE SEASON'S OWN WORDS AND ITS OWN CAST, which the panel asks for now
        # that it describes the season it is open on rather than the title. The
        # owner's own view caches these on the same lookup, which is why a
        # visitor reading them costs nothing.
        asyncio.run(cache.set(f"{base}/shows/123/seasons?{ext}", [
            {"number": 1, "title": "Season 1", "overview": "The first one.",
             "episode_count": 6, "aired_episodes": 6},
            {"number": 2, "title": "Season 2", "overview": "Season two's own words.",
             "episode_count": 6, "aired_episodes": 6}]))
        asyncio.run(cache.set(f"{base}/shows/123/seasons/2/people?{ext}", {
            "cast": [{"person": {"name": "A Actor"}, "character": "Lead"}]}))

    def _no_network(self):
        """A client whose .get fails the test outright.

        THE POINT OF ASSERTING INSIDE THE DOUBLE rather than counting calls
        afterwards: a request that reached Trakt and then failed for some other
        reason would still come back as a refusal, so a test that only checked
        the status code would pass on exactly the hole it exists to catch.
        """
        class _Boom:
            async def get(self, *a, **k):
                raise AssertionError(
                    "a public details request reached Trakt for a title this "
                    "link does not show")
        return patch("app.providers.trakt.transport.shared_client", return_value=_Boom())

    def _recording(self, bodies):
        """A client that answers from `bodies` by path and records what was asked.

        The other half of the same claim: the bound has to ADMIT the titles the
        link shows, or "no outbound call" would be satisfied by an endpoint that
        never works.
        """
        called: list[str] = []

        class _Response:
            status_code = 200
            text = ""
            headers: dict = {}

            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        class _Client:
            async def get(self, url, headers=None, timeout=None):
                path = url.split("?", 1)[0].split("api.trakt.tv/", 1)[-1]
                called.append(path)
                return _Response(bodies.get(path))

        return patch("app.providers.trakt.transport.shared_client",
                     return_value=_Client()), called

    REFUSED = "No source can describe this title"

    def assertRefused(self, resp):
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn(self.REFUSED, resp.text)
        # The other half of the claim: a refusal that still leaked the answer
        # would pass a message check on its own.
        self.assertNotIn("Full overview.", resp.text)
        self.assertNotIn("Fetched live.", resp.text)
        # A refusal publishes no link either — there is no title to link to.
        self.assertNotIn("trakt.tv/shows", resp.text)


class SharePageDetailsModalTests(_ShareDetailsTestCase):
    """The panel itself: what it draws, and what the bound refuses.

    A CLASS OF ITS OWN BENEATH THE SCAFFOLDING, so that the season-tab class
    below can share the setup without inheriting these cases — a test class that
    is also somebody's base runs every one of its own assertions again under the
    subclass's fixtures, which proves nothing twice and hides which fixture a
    failure belongs to.
    """

    def test_the_page_wires_cards_to_the_modal(self):
        body = self.client.get(f"/s/{self.token}?year=2026&month=7").text
        self.assertIn('id="detailsModal"', body)
        # THE CARD IS THE OPENER, AS MARKUP. ui.js's delegated listener opens any
        # element carrying the address, so the page ships no opener of its own
        # and no card carries a handler.
        opener = re.search(r'<div class="card[^"]*"\s+data-details-open="([^"]+)"\s+'
                           r'data-details-heading="([^"]*)"', body)
        self.assertIsNotNone(opener, "a share card carries no details address")
        self.assertNotIn("openShareDetails", body)
        self.assertNotIn("/static/js/share.js", body)
        # THE ADDRESS CARRIES the page's own view as well as the card's ids — the
        # bound cannot be evaluated without the view.
        self.assertIn(f"/s/{self.token}/details?media=show&amp;trakt=123", opener.group(1))
        self.assertIn("year=2026&amp;month=7", opener.group(1))

    def test_details_serve_the_owners_cached_data_without_calling_trakt(self):
        """The common case: the owner has opened this title, so the shared cache
        answers and the permission to fetch is never used."""
        self._seed_detail_cache()
        with self._no_network():
            resp = self._details("media=show&trakt=123&season=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        # BOTH DESCRIPTIONS, and they are different ones: what the show is, up
        # with the chips, and what THIS season is about, under the picker.
        self.assertIn("Full overview.", resp.text)
        self.assertIn("Season two&#39;s own words.", resp.text)
        self.assertIn("Returning Series", resp.text)  # normalized like the calendar
        self.assertIn("A Actor", resp.text)
        self.assertIn("Trailer", resp.text)
        self.assertIn("E05", resp.text)
        # THE PICTURE IS THE ONE THE GRID DREW, read out of the month this route
        # has already read rather than off the query string — a public endpoint
        # does not render back an address a stranger spelled.
        self.assertIn("<img src=", resp.text)
        # AND THE OUTBOUND BUTTON IS BUILT FROM THE BOUND IDS, for the same
        # reason: a link is an id rendered into the page.
        # BY NAME, and from the MONTH rather than from the request: the airing
        # this link publishes carries each service's own name for the title, so
        # the button is spelled the way both services ask for rather than by an
        # id they would have to resolve. A stranger's query string reaches none
        # of this — the same rule the picture follows.
        self.assertIn('href="https://trakt.tv/shows/test-show"', resp.text)

    # -- the bound ---------------------------------------------------------

    def test_a_title_this_link_does_not_show_is_never_looked_up(self):
        """THE ONE THIS ENDPOINT EXISTS TO GET RIGHT. An id nobody put on the
        owner's calendar is a stranger's input, and a public route that looked it
        up would turn any id on the internet into a call on the owner's
        credential, enumerable at the per-IP limit."""
        with self._no_network():
            self.assertRefused(self._details("media=show&trakt=999&season=2"))

    def test_the_medium_is_part_of_the_bound(self):
        """`movies/123` and `shows/123` are two different upstream calls, so an
        id the calendar shows as a show may not be asked about as a film."""
        with self._no_network():
            self.assertRefused(self._details("media=movie&trakt=123"))

    def test_the_season_is_part_of_the_bound(self):
        """Each season is another call, so a bound that stopped at the title
        would still let one shown title be turned into as many calls as there are
        integers."""
        with self._no_network():
            self.assertRefused(self._details("media=show&trakt=123&season=9"))

    def test_a_month_the_link_does_not_show_does_not_carry_the_title(self):
        """The bound is per VIEW, not per owner: the same id in a month whose
        cards do not include it is refused exactly like an unknown one."""
        with self._no_network():
            self.assertRefused(self.client.get(
                f"/s/{self.token}/details?media=show&trakt=123&season=2&year=2026&month=9"))

    def test_a_request_naming_no_month_falls_back_rather_than_widening(self):
        """A details URL with no view on it resolves to today's month like every
        other share request, and today is not the month this window holds — so
        the omission narrows to nothing instead of skipping the check."""
        with self._no_network():
            self.assertRefused(
                self.client.get(f"/s/{self.token}/details?media=show&trakt=123&season=2"))

    def test_a_made_up_id_does_not_ride_along_with_a_real_one(self):
        """A request carries one id per service and the server picks between
        them, so the unknown ones are dropped BEFORE that choice — otherwise a
        real id for one service would smuggle a stranger's id for the other past
        the bound."""
        save_settings(Settings(public_base_url=ORIGIN, simkl_client_id="scid"))
        with self._no_network(), \
             patch("app.providers.simkl.transport.cached_get") as simkl_get:
            resp = self._details("media=show&trakt=999&simkl=888&season=2")
        self.assertRefused(resp)
        simkl_get.assert_not_called()

    # -- and the permission it grants --------------------------------------

    def test_a_title_the_link_shows_may_be_looked_up_when_nothing_is_cached(self):
        """The relaxation itself. A detail answer is identical for every viewer
        and is cached instance-wide, so one public lookup serves this title to
        everybody who opens the link — which is the whole reason it is worth
        allowing for a title the owner is already publishing."""
        stub, called = self._recording({
            "shows/123": {"title": "Test Show", "overview": "Fetched live."},
            "shows/123/people": {"cast": []},
            "shows/123/seasons/2": [{"number": 5, "title": "The One"}],
        })
        with stub:
            resp = self._details("media=show&trakt=123&season=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Fetched live.", resp.text)
        self.assertIn("shows/123", called)

    def test_the_other_services_videos_are_asked_only_by_a_bound_id(self):
        """Videos are pooled from every service the ids name, which is a second
        and third question on top of the description. With the cache cold, a
        request naming a real title for one service and a made-up id for the
        other asks the first about its own title — its videos included — and
        never reaches the second at all: the pool runs over the bound ids and
        nothing a stranger spelled."""
        save_settings(Settings(public_base_url=ORIGIN, trakt_client_id="cid",
                               simkl_client_id="scid"))
        stub, called = self._recording({
            "shows/123": {"title": "Test Show", "overview": "Fetched live."},
            "shows/123/videos": [{"title": "Official Teaser", "site": "youtube",
                                  "url": "https://youtube.com/watch?v=4PPodH3i_7k",
                                  "type": "teaser", "official": True}],
            "shows/123/seasons/2": [{"number": 5, "title": "The One"}],
        })
        with stub, patch("app.providers.simkl.transport.cached_get") as simkl_get:
            resp = self._details("media=show&trakt=123&simkl=888&season=2")
        self.assertEqual(resp.status_code, 200, resp.text)
        simkl_get.assert_not_called()
        self.assertIn("shows/123/videos", called)
        self.assertEqual({path.split("/")[1] for path in called}, {"123"})
        self.assertIn("youtube-nocookie.com/embed/4PPodH3i_7k", resp.text)

    def test_a_service_that_cannot_be_reached_is_the_same_refusal_and_not_an_error(self):
        """A lookup the link may make can still fail — the service is down — and
        that used to escape the route as a 500, which the page does not swap in,
        so the panel sat on its loading line. It is drawn as the one refusal a
        stranger always gets: a different sentence for an outage would say, for
        any id tried while a service is down, whether the owner's calendar
        shows it, since only a title the link shows is ever asked about."""
        from app.providers.base import SourceUnavailable
        with patch("app.providers.trakt.detail.fetch_details",
                   side_effect=SourceUnavailable("down")):
            self.assertRefused(self._details("media=show&trakt=123&season=2"))

    def test_details_reach_through_the_username_url_form_too(self):
        self._seed_detail_cache()
        with self._no_network():
            resp = self.client.get(
                f"/u/modalowner/details?media=show&trakt=123&season=2&{self.VIEW}")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("A Actor", resp.text)

    def test_a_link_that_does_not_resolve_refuses_in_the_same_words(self):
        """A token nobody issued must not be told apart from a title the link
        does not show: the two are separate facts and answering either of them
        specifically hands a stranger a way to ask about the other."""
        with self._no_network():
            self.assertRefused(self.client.get(
                f"/s/not-a-real-token/details?media=show&trakt=123&season=2&{self.VIEW}"))


class ShareEmbedTextTests(SharePageTestCase):
    """The WORDS an unfurler prints around a shared link — the <title> and the
    Open Graph tags, not the picture, which draws no name at all.

    The owner's chosen display name is the only name any of this may carry. The
    username is a sign-in identifier they never chose to publish, so where there
    is no chosen name the text names nobody rather than falling back to it.
    """

    def setUp(self):
        super().setUp()
        self.user_id = self._make_user("embedowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = asyncio.run(share_links.get_or_create(self.user_id))["token"]
        self.client.cookies.clear()

    def _head(self, path: str | None = None) -> str:
        """Everything before <body> — the part a crawler reads. The page's own
        body still shows a visitor who the calendar belongs to, which is a
        different audience and a different question."""
        resp = self.client.get(path or f"/s/{self.token}?year=2026&month=8")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.text.split("<body")[0]

    def _meta(self, head: str, prop: str) -> str:
        found = re.findall(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)">', head)
        self.assertEqual(len(found), 1, f"{prop}: {found}")
        return found[0]

    def _set_name(self, value: str | None) -> None:
        asyncio.run(auth.set_display_name(self.user_id, value))

    def test_a_chosen_display_name_is_the_name_the_embed_carries(self):
        self._set_name("Josh Black")
        head = self._head()
        self.assertEqual(self._meta(head, "og:title"), "Josh Black – August 2026")
        self.assertEqual(self._meta(head, "twitter:title"), "Josh Black – August 2026")
        self.assertIn("<title>Josh Black – August</title>", head)

    def test_no_chosen_name_omits_it_rather_than_using_the_username(self):
        """The failure this guards against is the substitution, not the gap: a
        share link is meant to name only somebody who asked to be named."""
        head = self._head()
        self.assertEqual(self._meta(head, "og:title"), "Shared calendar – August 2026")
        self.assertIn("<title>Shared calendar – August</title>", head)

    def test_the_nameless_text_reads_as_deliberate_rather_than_broken(self):
        """No dangling possessive, no leading separator, no doubled dash — the
        nameless branch is a finished phrase of its own, not a title with a hole
        where a name was."""
        title = self._meta(self._head(), "og:title")
        self.assertFalse(title.startswith(("–", "-", "'", "’", " ")), title)
        for fragment in ("'s", "’s", "– –", "  "):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, title)

    def test_the_description_names_nobody_either_way(self):
        for name in (None, "Josh Black"):
            with self.subTest(name=name):
                self._set_name(name)
                head = self._head()
                # Escaped as the environment writes it: the apostrophe in
                # "I'm" is what a quoted attribute value looks like here.
                self.assertEqual(self._meta(head, "og:description"),
                                 "Shows I&#39;m watching this month")

    def test_the_username_never_reaches_the_head_either_way(self):
        """The token page, whose URL names nobody. A /u/ link's own URL contains
        the username by construction and is a separate matter."""
        for name in (None, "Josh Black"):
            with self.subTest(name=name):
                self._set_name(name)
                self.assertNotIn("embedowner", self._head())

    def test_a_whitespace_only_name_counts_as_no_name(self):
        """Trimmed before the decision, so a stored value that predates the
        account page's own normalization cannot render a title made of spaces."""
        asyncio.run(db.execute(
            "UPDATE users SET display_name = ? WHERE id = ?", ("   ", self.user_id)))
        head = self._head()
        self.assertEqual(self._meta(head, "og:title"), "Shared calendar – August 2026")
        self.assertNotIn("embedowner", head)

    def test_the_name_is_escaped_into_the_meta_attributes(self):
        """A display name is free text the owner types, and it lands inside a
        double-quoted HTML attribute. One that closed the attribute would put
        markup of the owner's choosing into every crawler's copy of the page."""
        self._set_name('"><script>alert(1)</script>')
        head = self._head()
        self.assertNotIn("<script>alert(1)", head)
        self.assertEqual(self._meta(head, "og:title"),
                         "&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt; – August 2026")

    def test_every_link_form_says_the_same_thing(self):
        """/s/, /u/ and /c/ open the same page and describe it the same way; the
        username in a /u/ URL is the URL's business, not the text's."""
        self._set_name("Josh Black")
        asyncio.run(share_links.set_custom_slug(self.user_id, "embed-cal"))
        for path in (f"/s/{self.token}", "/u/embedowner", "/c/embed-cal"):
            with self.subTest(path=path):
                head = self._head(f"{path}?year=2026&month=8")
                self.assertEqual(self._meta(head, "og:title"), "Josh Black – August 2026")


class CardTileSelectionTests(unittest.TestCase):
    """Which titles lead a share link's preview picture.

    A pure function of the month's visible items, so this needs no database, no
    client and no renderer — which is the point of it being one.
    """

    def _item(self, title, *, day=1, season=None, episode=None, media=Media.SHOW):
        return Item(
            source=Source.TRAKT, media=media, id=f"{media}:{title}",
            ids={"tmdb": 1}, detail_url="",
            air_date=f"2026-08-{day:02d}", air_ts=float(day), air_display="",
            air_time="", day_of_week="", title=title,
            season=season, episode_number=episode,
        )

    def _series(self, title, day):
        return self._item(title, day=day, season=1, episode=1)

    def _season(self, title, day, season=3):
        return self._item(title, day=day, season=season, episode=1)

    def _titles(self, items):
        return [item.title for item in share_routes.select_tile_items(items)]

    def test_series_premieres_fill_the_card_earliest_first(self):
        """More series premieres than the grid holds, so nothing else gets in."""
        days = range(1, share_routes.MAX_CARD_TILES + 3)
        items = [self._series(f"S{day}", day) for day in reversed(days)]
        items += [self._season(f"P{day}", day) for day in (1, 2, 3, 4)]
        self.assertEqual(self._titles(items),
                         [f"S{day}" for day in days][:share_routes.MAX_CARD_TILES])

    def test_season_premieres_fill_in_behind_them(self):
        """Tier beats date: both series premieres come first even though every
        season premiere airs before either of them."""
        items = [self._series("Series late", 20), self._series("Series later", 28)]
        seasons = range(1, share_routes.MAX_CARD_TILES + 1)
        items += [self._season(f"Season {day}", day) for day in seasons]
        expected = ["Series late", "Series later"]
        expected += [f"Season {day}" for day in seasons]
        self.assertEqual(self._titles(items), expected[:share_routes.MAX_CARD_TILES])

    def test_a_month_of_movies_still_fills_the_card(self):
        days = range(1, share_routes.MAX_CARD_TILES + 4)
        items = [self._item(f"Film {day}", day=day, media=Media.MOVIE) for day in days]
        self.assertEqual(self._titles(items),
                         [f"Film {day}" for day in days][:share_routes.MAX_CARD_TILES])

    def test_a_month_of_mid_season_episodes_still_fills_the_card(self):
        """Every calendar view has to produce a strip; the All Episodes view is
        the one where nothing is a premiere at all."""
        days = range(1, share_routes.MAX_CARD_TILES + 4)
        items = [self._item(f"Ep {day}", day=day, season=4, episode=7) for day in days]
        self.assertEqual(self._titles(items),
                         [f"Ep {day}" for day in days][:share_routes.MAX_CARD_TILES])

    def test_a_movie_is_preferred_over_a_mid_season_episode(self):
        items = [self._item("Episode", day=1, season=2, episode=6),
                 self._item("Movie", day=28, media=Media.MOVIE)]
        self.assertEqual(self._titles(items), ["Movie", "Episode"])

    def test_missing_episode_coordinates_do_not_raise(self):
        """`season` and `episode_number` are optional on Item and absent on
        movies, so nothing here may compare None with an integer."""
        items = [self._item("No coordinates", day=2),
                 self._item("Half", day=3, season=1),
                 self._series("Premiere", 9)]
        self.assertEqual(self._titles(items), ["Premiere", "No coordinates", "Half"])

    def test_a_thin_month_produces_what_it_has(self):
        self.assertEqual(self._titles([]), [])
        self.assertEqual(self._titles([self._series("Only", 3)]), ["Only"])

    def test_a_tile_reads_its_premiere_mark_off_the_same_rule_that_ranked_it(self):
        """The card glows around series premieres, and "series premiere" already
        has exactly one definition here. A second spelling of it is a rule that
        can drift while both copies go on sounding right."""
        premiere = share_routes.tile_tier(self._series("Premiere", 4))
        self.assertEqual(premiere, 0)
        for item in (self._season("Season", 4), self._item("Mid", day=4, season=2, episode=6),
                     self._item("Film", day=4, media=Media.MOVIE)):
            with self.subTest(title=item.title):
                self.assertNotEqual(share_routes.tile_tier(item), premiere)

    def test_the_air_date_is_formatted_for_the_caption(self):
        """The renderer is handed a finished string because it has no calendar
        vocabulary, so this is the one place the card's date format lives."""
        self.assertEqual(share_routes._tile_date_label(self._item("A", day=14)),
                         "Fri 14 Aug")

    def test_an_unparseable_air_date_becomes_no_caption_rather_than_a_guess(self):
        item = self._item("A", day=1)
        item.air_date = "not-a-date"
        self.assertEqual(share_routes._tile_date_label(item), "")


class CardTileOrderTests(unittest.TestCase):
    """The order the tiles are DRAWN in, which is not the order they were picked
    in: the selection ranks by strength, and the grid then reads as a single date
    order across the whole card, left to right and top row first.
    """

    def _pair(self, title, when):
        tile = share_card.Tile(title=title, poster=Path("/posters/show") / f"{title}.jpg",
                               date_label=f"day {when}", is_premiere=False)
        return tile, when

    def _titles(self, pairs):
        return [tile.title for tile in share_routes.arrange_tiles(pairs)]

    def test_the_whole_grid_is_drawn_earliest_first(self):
        """Ten tiles lay out five and five, and the date order runs straight
        through the break: the sixth-earliest airing opens the bottom row. Which
        row a tile lands in is decided by its date alone — the selection's tiers
        chose the ten titles and have no further say."""
        strong = [self._pair(f"d{n}", n) for n in (8, 1, 4, 2, 3)]
        rest = [self._pair(f"d{n}", n) for n in (9, 6, 7, 10, 5)]
        self.assertEqual(self._titles(strong + rest),
                         [f"d{n}" for n in range(1, 11)])

    def test_a_partial_grid_reads_in_order_across_its_rows(self):
        """Seven tiles lay out four then three: earliest four on top, next three
        below. One sorted sequence lands correctly under whatever split the
        renderer picks, so nothing here restates the row widths."""
        pairs = [self._pair(f"t{n}", n) for n in (4, 3, 2, 1, 7, 6, 5)]
        self.assertEqual(self._titles(pairs), ["t1", "t2", "t3", "t4", "t5", "t6", "t7"])
        self.assertEqual(share_card.tile_rows(len(pairs)), [4, 3])

    def test_an_undated_tile_lands_at_the_end_of_the_grid(self):
        """Somewhere deterministic, and after everything that does have a date —
        a zero would sort it to the front of the card, which is the one place a
        title with no air date should not be. The last cells of the bottom row
        are where it goes instead."""
        pairs = [self._pair("no-date", None), self._pair("later", 9),
                 self._pair("earlier", 2)]
        pairs += [self._pair(f"b{n}", n) for n in (1, 3, 5)]
        self.assertEqual(self._titles(pairs),
                         ["b1", "earlier", "b3", "b5", "later", "no-date"])

    def test_undated_tiles_keep_the_order_they_were_selected_in(self):
        pairs = [self._pair("first", None), self._pair("second", None),
                 self._pair("dated", 4)]
        pairs += [self._pair(f"b{n}", n) for n in (1, 2, 3)]
        self.assertEqual(self._titles(pairs),
                         ["b1", "b2", "b3", "dated", "first", "second"])

    def test_a_title_its_date_and_its_artwork_move_together(self):
        """The failure this guards against is the one that looks almost right: a
        card whose posters were re-ordered and whose captions were not. Each tile
        owns its whole caption, so this holds by construction — asserted anyway,
        because "by construction" is exactly what stops being true later."""
        pairs = [self._pair(f"t{n}", n) for n in (3, 1, 2)]
        for tile in share_routes.arrange_tiles(pairs):
            with self.subTest(title=tile.title):
                self.assertEqual(tile.poster.name, f"{tile.title}.jpg")
                self.assertEqual(tile.date_label, f"day {tile.title[1:]}")

    def test_nothing_to_arrange_is_not_an_error(self):
        self.assertEqual(share_routes.arrange_tiles([]), ())


if __name__ == "__main__":
    unittest.main()


class SharePageHasNoSeasonPickerTests(_ShareDetailsTestCase):
    """A shared calendar publishes ONE MONTH, so it offers no season picker.

    THE REASONING, AND IT IS THE OWNER'S DECISION AFTER SEEING IT WORK. The strip
    was built here first and it did work — but what may be looked up on a public
    page is bound to what that view shows, the season included, so on a link
    showing season 3 of a five-season show four of the five tabs refused. A
    control that mostly says no is worse than no control, and the refusal has to
    stay a single unhelpful sentence: telling a stranger which seasons the
    owner's calendar contains is exactly what one message prevents.

    SO THE BOUND IS UNCHANGED AND THE SURFACE IS NARROWER. The season is still a
    dimension of what a details request may ask about — `test_the_season_is_part
    _of_the_bound` above still holds it — there is simply nothing on the page
    offering to cross it.
    """

    def _panel(self, query: str) -> DetailsPanel:
        resp = self._details(query)
        self.assertEqual(resp.status_code, 200, resp.text)
        return DetailsPanel(resp.text)

    def test_no_strip_is_drawn(self):
        self._seed_detail_cache()
        with self._no_network():
            panel = self._panel("media=show&trakt=123&season=2")
        self.assertEqual(panel.season_tabs, [])
        self.assertNotIn("ep-seasons", panel.html)

    def test_the_season_the_card_is_for_is_still_shown_in_full(self):
        """Removing the picker removes navigation, not content: the season the
        visitor clicked still gets its own description, its cast and its rows."""
        self._seed_detail_cache()
        with self._no_network():
            panel = self._panel("media=show&trakt=123&season=2")
        self.assertIn("Season two&#39;s own words.", panel.html)
        self.assertIn("A Actor", panel.html)
        self.assertIn("Season 2 Episodes", panel.html)

    def test_the_season_fragment_endpoint_is_gone(self):
        """It existed only to fill a tab. Left standing it would be a public
        route nothing calls, which is a surface to keep bound for no reason."""
        resp = self.client.get(
            f"/s/{self.token}/details/season"
            f"?media=show&trakt=123&season=2&{self.VIEW}")
        self.assertEqual(resp.status_code, 404)

    def _owner_watched(self, episodes, season: int = 2) -> None:
        """What the OWNER has watched of this season, as their tracker recorded
        it. Keyed on the identity the card resolves to, which is what the share
        route looks it up by."""
        asyncio.run(db.execute(
            "INSERT OR REPLACE INTO distrakt_show_progress "
            "(user_id, media, match_source, match_id, season, source, "
            "watched_episodes_json, trakt_id) VALUES (?,?,?,?,?,?,?,?)",
            (self.user_id, "show", "tmdb", "789", season, "trakt",
             json.dumps({str(e): "2026-07-16T00:00:00Z" for e in episodes}), 123)))

    def test_the_panel_shows_the_owners_watched_episodes(self):
        """WHOSE PROGRESS THIS IS has an answer: the same person whose calendar,
        whose filters and whose not-watching marks the page already publishes.
        A visitor has no account here, so there is nothing else it could be."""
        self._seed_detail_cache()
        self._owner_watched([5])
        with self._no_network():
            panel = self._panel("media=show&trakt=123&season=2")
        self.assertEqual(panel.watched, [5])
        self.assertEqual(panel.by_source, {"trakt": [5]})

    def test_turning_it_off_on_the_account_page_reaches_this_link(self):
        """THE SWITCH IS THE ACCOUNT'S, NOT THE LINK'S, which is what makes it
        able to un-publish a calendar somebody shared months ago. No query
        parameter can turn it back on: it is not a view option."""
        self._seed_detail_cache()
        self._owner_watched([5])
        asyncio.run(auth.update_user_prefs(self.user_id, share_watch_history=False))
        with self._no_network():
            panel = self._panel("media=show&trakt=123&season=2")
        self.assertEqual(panel.watched, [])
        self.assertNotIn("ep-check", panel.html)

    def test_a_season_the_owner_has_watched_none_of_draws_no_ticks(self):
        """The same rule the owner's own calendar follows, and it matters more
        here: a column of hollow marks on every card would publish "this person
        has watched nothing" about their entire month."""
        self._seed_detail_cache()
        with self._no_network():
            panel = self._panel("media=show&trakt=123&season=2")
        self.assertEqual(panel.services, [])
        self.assertNotIn("ep-check", panel.html)

    def test_a_title_this_link_does_not_show_yields_no_progress_either(self):
        """The marks are addressed by the airing the MONTH holds, so a title the
        view does not publish cannot be asked about — the same rule the picture
        and the outbound links follow."""
        self._owner_watched([5])
        with self._no_network():
            self.assertRefused(self._details("media=show&trakt=999&season=2"))

