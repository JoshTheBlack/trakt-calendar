"""The route a share link's preview picture is fetched from.

What is asserted here is the SHAPE of the thing: that a published share answers
with a real JPEG of the right size, that the compact link code is honoured in
place rather than redirected, that every way of missing degrades to the static
banner instead of to an error an unfurler would render as a broken image, and
that the picture never reaches past the local calendar cache.

The renderer itself is covered by tests/calendar/test_share_card.py and the way
a finished card is addressed and swept by tests/calendar/test_share_card_cache.py.
"""
from __future__ import annotations

import asyncio
import unittest
from datetime import date
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from app import calendar_cache, db, posters, share_code, share_links
from app.config import Settings
from tests.support import AppTestCase, ORIGIN


async def _no_warm(settings, refs) -> int:
    return 0


class ShareCardRouteTests(AppTestCase):
    def make_settings(self):
        return Settings(public_base_url=ORIGIN, trakt_client_id="cid", trakt_access_token="tok")

    def setUp(self):
        super().setUp()
        # The one outbound call this feature is allowed to make is resolving
        # poster artwork. These tests are about the route around it, and a real
        # resolution would reach the network, so it resolves nothing here.
        warm = patch.object(posters, "ensure_posters", _no_warm)
        warm.start()
        self.addCleanup(warm.stop)
        self.user_id = self.make_user("cardowner", calendar_approved=True)
        self.sign_in_as(self.user_id)
        self.token = self.client.get("/api/me/share").json()["token"]
        self.client.cookies.clear()
        entry = {
            "first_aired": "2026-08-15T20:00:00Z",
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "show": {"title": "Test Show", "year": 2026,
                     "ids": {"slug": "test-show", "trakt": 123, "tmdb": 789}},
        }
        start = calendar_cache.window_start(date(2026, 8, 15))
        asyncio.run(calendar_cache.store_window("shows/new", start, [entry], 600, db.now()))

    def test_a_card_renders(self):
        resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(resp.status_code, 200, resp.text[:400])
        self.assertEqual(resp.headers["content-type"], "image/jpeg")
        self.assertEqual(resp.headers["cache-control"], "public, max-age=600")
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (1200, 630))

    def test_the_compact_code_is_honoured_in_place(self):
        """A page redirects a `?p=` code to its expanded form so the visitor gets
        a URL they can edit. An unfurler fetches the picture once and never edits
        it, so a redirect would only be a wasted round trip."""
        code = share_code.encode({"year": "2026", "month": "8"})
        resp = self.client.get(f"/s/{self.token}/og.jpg?p={code}", follow_redirects=False)
        self.assertEqual(resp.status_code, 200)

    def test_a_slug_only_share_still_has_a_card(self):
        """The regression this design invites: the picture is addressed by token
        from all three link forms, so gating it on the token FORM would leave a
        live /c/ page with no preview at all."""
        asyncio.run(share_links.set_custom_slug(self.user_id, "prettyslug"))
        asyncio.run(share_links.set_enabled(self.user_id, "slug", True))
        asyncio.run(share_links.set_enabled(self.user_id, "token", False))
        self.assertEqual(self.client.get(f"/s/{self.token}", follow_redirects=False).status_code, 404)
        resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(resp.status_code, 200)

    def test_everything_disabled_serves_the_banner(self):
        for kind in ("token", "username", "slug"):
            asyncio.run(share_links.set_enabled(self.user_id, kind, False))
        resp = self.client.get(f"/s/{self.token}/og.jpg", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], "/static/images/tvbanner.png")

    def test_an_unknown_token_serves_the_banner(self):
        resp = self.client.get("/s/nope/og.jpg", follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_a_render_that_raises_serves_the_banner(self):
        """Not a 500: an unfurler that gets one renders a broken picture into
        somebody's channel and never comes back."""
        from app import share_card

        def _boom(card):
            raise RuntimeError("a corrupt poster, a font that vanished")

        with patch.object(share_card, "build_card", _boom):
            resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8",
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_the_cache_is_used_on_a_second_request(self):
        from app import share_card

        # A month whose only title has no tmdb id has nothing outstanding, so the
        # render is complete and may be kept.
        entry = {"first_aired": "2026-09-15T20:00:00Z",
                 "episode": {"season": 1, "number": 1, "title": "Pilot"},
                 "show": {"title": "No Ids", "year": 2026, "ids": {"trakt": 9}}}
        start = calendar_cache.window_start(date(2026, 9, 15))
        asyncio.run(calendar_cache.store_window("shows/new", start, [entry], 600, db.now()))

        first = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=9")
        self.assertEqual(first.status_code, 200)
        with patch.object(share_card, "build_card", side_effect=AssertionError("re-rendered")):
            second = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=9")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, first.content)

    def test_read_month_is_never_allowed_to_fetch(self):
        """THE IMPORTANT ONE. A crawler must never spend the owner's calendar
        rate limit; asserted explicitly rather than left to the suite's network
        guard, because a guard that fires is a confusing message where this is a
        named rule."""
        seen = {}
        real = calendar_cache.read_month

        async def _spy(*args, **kwargs):
            seen.update(kwargs)
            return await real(*args, **kwargs)

        with patch.object(calendar_cache, "read_month", _spy):
            self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertIs(seen.get("allow_fetch"), False)

    def test_head_is_answered(self):
        """Some unfurlers check an image before they fetch it, and FastAPI does
        not synthesize HEAD from GET the way Starlette's own router does."""
        resp = self.client.head(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(resp.status_code, 200)

    def test_the_route_is_public(self):
        """A route that ended up gated behind an account would pass the audit
        that only asks whether a level was declared, and break every unfurl."""
        from app import authz, share_routes
        from app.auth import AuthLevel

        self.assertEqual(getattr(share_routes.share_card_image, authz.LEVEL_ATTR),
                         AuthLevel.PUBLIC)


if __name__ == "__main__":
    unittest.main()
