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

import re
import unittest
import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from app import auth, cache, db
from app.calendar import (cache as calendar_cache, share_card, share_code,
                          share_links, share_routes)
from app.providers.base import Item, Media, Source
from app.providers.trakt import transport as trakt_transport
from app.config import Settings, save_settings
from app.media import posters
from tests.support import AppTestCase, ORIGIN


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
        self.token = self.client.get("/api/me/share").json()["token"]
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
        asyncio.run(share_links.set_enabled(self.user_id, "username", True))
        asyncio.run(share_links.set_custom_slug(self.user_id, "embed-cal"))
        asyncio.run(share_links.set_enabled(self.user_id, "slug", True))
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
