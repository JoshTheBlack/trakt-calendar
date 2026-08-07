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
import re
import unittest
import warnings
import zlib
from collections import defaultdict
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import anyio
from PIL import Image

from app import db
from app.endpoints import get_endpoint
from app.calendar import (cache as calendar_cache, share_card, share_card_cache,
                          share_code, share_links, share_routes,
                          state as calendar_state)
from app.config import Settings
from app.media import posters, user_images
from tests.support import APP_DIR, AppTestCase, ORIGIN, calendar_records

# Artwork with the frequency content of a real poster, which is what the encoded
# size below is a claim about: flat colour compresses to almost nothing and
# random noise to far more than any photograph, so neither would say anything
# about how big a real card gets. These are the app's own bundled graphics,
# which are at least pictures.
_ARTWORK = sorted((APP_DIR / "static" / "images").glob("*.png"))


async def _no_warm(settings, refs) -> int:
    return 0


class ShareCardTestCase(AppTestCase):
    """A published share with one August airing in the calendar cache, and no
    poster resolution unless the test asks for one."""

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
        self.seed([self.an_entry("Test Show", 15, tmdb=789)])

    # -- seeding -----------------------------------------------------------
    def an_entry(self, title: str, day: int, *, tmdb: int | None = None,
                 network: str = "", season: int = 1, number: int = 1) -> dict:
        # A stable per-title trakt id: the app files a viewer's marks under it,
        # so two runs of the same test have to agree on what it is.
        ids = {"slug": title.lower().replace(" ", "-"), "trakt": zlib.crc32(title.encode())}
        if tmdb is not None:
            ids["tmdb"] = tmdb
        show = {"title": title, "year": 2026, "ids": ids}
        if network:
            show["network"] = network
        return {
            # Midday UTC, so a viewer's timezone cannot move an airing into a
            # neighbouring day and out of the month being counted.
            "first_aired": f"2026-08-{day:02d}T12:00:00Z",
            "episode": {"season": season, "number": number, "title": "Pilot"},
            "show": show,
        }

    def seed(self, entries: list[dict], *, endpoint: str = "shows/new") -> None:
        """Put entries in the calendar cache, grouped into the fixed 7-day
        windows the cache is keyed by."""
        windows: dict[date, list[dict]] = defaultdict(list)
        for entry in entries:
            windows[calendar_cache.window_start(date.fromisoformat(entry["first_aired"][:10]))].append(entry)
        for start, group in windows.items():
            asyncio.run(calendar_cache.store_window(
                endpoint, start, calendar_records(group, get_endpoint(endpoint)),
                600, db.now(), sources=["trakt"]))

    def write_poster(self, tmdb: int, *, media: str = "show", source: Path | None = None) -> Path:
        """A poster on disk for (media, tmdb), as if it had already resolved.

        The poster cache is one directory for the whole process, so anything
        written here is removed again afterwards rather than left to decide what
        another test's month looks like.
        """
        path = posters.POSTER_DIR / media / f"{tmdb}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source or _ARTWORK[tmdb % len(_ARTWORK)]) as raw:
            raw.convert("RGB").resize((posters.POSTER_W, posters.POSTER_H),
                                      Image.LANCZOS).save(path, format="JPEG", quality=85)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def cached_cards(self) -> set[Path]:
        if not share_card_cache.CACHE_DIR.exists():
            return set()
        return set(share_card_cache.CACHE_DIR.iterdir())


class ShareCardRouteTests(ShareCardTestCase):
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
        def _boom(card):
            raise RuntimeError("a corrupt poster, a font that vanished")

        with patch.object(share_card, "build_card", _boom):
            resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8",
                                   follow_redirects=False)
        self.assertEqual(resp.status_code, 302)

    def test_the_cache_is_used_on_a_second_request(self):
        # A month whose only title has no tmdb id has nothing outstanding, so the
        # render is complete and may be kept.
        entry = {"first_aired": "2026-09-15T20:00:00Z",
                 "episode": {"season": 1, "number": 1, "title": "Pilot"},
                 "show": {"title": "No Ids", "year": 2026, "ids": {"trakt": 9}}}
        start = calendar_cache.window_start(date(2026, 9, 15))
        asyncio.run(calendar_cache.store_window(
            "shows/new", start, calendar_records([entry], get_endpoint("shows/new")),
            600, db.now(), sources=["trakt"]))

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
        from app import authz
        from app.auth import AuthLevel

        self.assertEqual(getattr(share_routes.share_card_image, authz.LEVEL_ATTR),
                         AuthLevel.PUBLIC)

    def test_a_disabled_account_serves_the_banner(self):
        """Indistinguishable from an unknown token and from a retired slug, which
        is the share module's standing promise: a link cannot be used to work out
        which of the three it hit."""
        asyncio.run(db.execute("UPDATE users SET is_disabled = 1 WHERE id = ?", (self.user_id,)))
        resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8",
                               follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers["location"], share_routes.STATIC_CARD_URL)


class CardSizeTests(ShareCardTestCase):
    """How big the file an unfurler has to fetch is allowed to get.

    Some unfurlers cap the size they will download, and a card over the cap is
    not a slower preview — it is no preview. The ceiling is asserted as a NUMBER
    so a future layout that doubles the tile count fails here rather than in
    somebody's Slack.
    """

    # Real poster artwork with captions measures around 230 KB for a full grid.
    # 300 KB is enough headroom for unusually busy artwork and still far under
    # every fetch cap known to be in the wild.
    CEILING = 300 * 1024

    def test_a_full_grid_of_artwork_stays_under_the_ceiling(self):
        self.seed([self.an_entry(f"Show {n}", 2 + n, tmdb=6100 + n)
                   for n in range(share_card.MAX_TILES)])
        for n in range(share_card.MAX_TILES):
            self.write_poster(6100 + n)
        resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(resp.status_code, 200)
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))
        self.assertLess(len(resp.content), self.CEILING,
                        f"{len(resp.content) / 1024:.1f} KB")


class CardInvalidationTests(ShareCardTestCase):
    """The key IS the invalidation (see app/calendar/share_card_cache.py). These are the
    same three cases from the route's end, where the bookkeeping this design
    replaced would have been."""

    def setUp(self):
        super().setUp()
        # The month's one title has its artwork on disk, so nothing about it is
        # outstanding and its card is allowed to be kept — which is the state
        # every case here is about.
        self.write_poster(789)

    def month(self, extra: str = "") -> bytes:
        resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8{extra}",
                               follow_redirects=False)
        # NOT just a 200: every unhappy path here redirects to the static banner,
        # and following that redirect would hand back a perfectly good PNG for a
        # request that failed.
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        self.assertEqual(resp.headers["content-type"], "image/jpeg")
        return resp.content

    def renders(self, extra: str = "") -> bool:
        """Whether serving this request drew a card, rather than reading one off
        disk. What "renders anew" means is a render, not different bytes: a key
        term that changed without moving a pixel is still supposed to mint a new
        address."""
        drawn = []
        real = share_card.build_card
        with patch.object(share_card, "build_card",
                          side_effect=lambda card: drawn.append(card) or real(card)):
            self.month(extra)
        return bool(drawn)

    def test_a_month_that_gained_a_title_renders_anew(self):
        before = self.month()
        self.seed([self.an_entry("A Late Arrival", 21)])
        self.assertNotEqual(self.month(), before)

    def test_a_refresh_that_changed_nothing_reuses_the_cached_file(self):
        """The regression test for keying on content rather than on the clock:
        the calendar cache's own timestamp moves several times a day whether or
        not a single title changed, and a key carrying it would burn a render
        each time for a picture nobody could tell apart."""
        before = self.month()
        # Same entry, stored again later — a TTL refresh that found no change.
        self.seed([self.an_entry("Test Show", 15, tmdb=789)])
        self.assertFalse(self.renders())
        self.assertEqual(self.month(), before)

    def test_a_renderer_version_bump_renders_anew(self):
        """Otherwise a layout change keeps being served from disk to everyone who
        unfurled a link before it, and unfurlers cache hard."""
        self.month()
        self.assertFalse(self.renders())
        with patch.object(share_card, "RENDERER_VERSION", share_card.RENDERER_VERSION + 1):
            self.assertTrue(self.renders())


class CardPosterBoundTests(ShareCardTestCase):
    """The bounds that keep a link preview from becoming an outbound-call
    amplifier. Each one is a way this goes wrong in production rather than a
    property worth having for its own sake."""

    def warm_spy(self):
        """Patches ensure_posters and returns the list of ref batches it saw."""
        seen: list[list] = []

        async def _spy(settings, refs) -> int:
            seen.append(list(refs))
            return 0

        patcher = patch.object(posters, "ensure_posters", _spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return seen

    def test_a_busy_month_still_resolves_at_most_a_grid_of_posters(self):
        """A thirty-airing August must not become thirty lookups. The bound is
        the number of tiles the card can draw, read from the renderer."""
        self.seed([self.an_entry(f"Show {n}", 2 + n, tmdb=6200 + n) for n in range(20)])
        seen = self.warm_spy()
        self.assertEqual(self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8").status_code, 200)
        self.assertTrue(seen)
        for batch in seen:
            self.assertLessEqual(len(batch), share_routes.MAX_CARD_TILES)

    def test_warming_artwork_does_not_license_fetching_the_calendar(self):
        """Two independent rules, and this is the test that says so: artwork may
        be resolved outbound, the month itself is only ever read from what is
        already cached."""
        seen = self.warm_spy()
        read: dict = {}
        real = calendar_cache.read_month

        async def _spy(*args, **kwargs):
            read.update(kwargs)
            return await real(*args, **kwargs)

        with patch.object(calendar_cache, "read_month", _spy):
            self.assertEqual(self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8").status_code, 200)
        self.assertTrue(seen, "no artwork warm happened, so this proved nothing")
        self.assertIs(read.get("allow_fetch"), False)

    def test_a_title_with_no_tmdb_id_is_dropped_rather_than_looked_up(self):
        """Going hunting for an id a calendar entry never carried is the one
        lookup a link preview is not worth making."""
        self.seed([self.an_entry("No Ids At All", 9)])
        seen = self.warm_spy()
        self.assertEqual(self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8").status_code, 200)
        self.assertTrue(seen)
        for batch in seen:
            self.assertEqual([ref for ref in batch if ref[1] in (None, "", 0)], [])
            self.assertNotIn("No Ids At All", [ref[1] for ref in batch])

    def test_an_incomplete_card_is_served_but_not_kept(self):
        """The month's one title has artwork on its way and nothing on disk yet.
        Caching that render would pin a half-empty picture for the whole
        retention window; not caching it is what makes the next request — by
        which time the warm has usually landed — store the finished one."""
        before = self.cached_cards()
        first = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.cached_cards(), before)

        rendered = []
        real = share_card.build_card
        with patch.object(share_card, "build_card",
                          side_effect=lambda card: rendered.append(card) or real(card)):
            self.assertEqual(self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8").status_code, 200)
        self.assertEqual(len(rendered), 1, "an uncached card was served from the cache")

    def test_a_settled_month_is_kept(self):
        """The other half of the rule, and the reason "incomplete" is narrower
        than "a tile is missing": a title whose artwork is on disk has nothing
        outstanding, so its card caches rather than re-rendering on every crawl
        forever."""
        self.write_poster(789)
        first = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(first.status_code, 200)
        with patch.object(share_card, "build_card",
                          side_effect=AssertionError("re-rendered a complete card")):
            second = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(second.content, first.content)

    def test_artwork_that_outruns_the_budget_still_produces_a_card(self):
        """An unfurler gives up in a handful of seconds, so a card that arrives
        after Discord stopped listening is not a slower embed — it is no embed.
        What lands inside the budget is drawn; the rest is next request's
        problem."""
        async def _slow(settings, refs) -> int:
            await anyio.sleep(30)
            return 0

        with patch.object(share_routes, "POSTER_BUDGET_SECONDS", 0.05), \
                patch.object(posters, "ensure_posters", _slow):
            resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(resp.status_code, 200)
        with Image.open(BytesIO(resp.content)) as img:
            self.assertEqual(img.size, (share_card.CARD_W, share_card.CARD_H))


class PosterWarmTests(unittest.IsolatedAsyncioTestCase):
    """The page's own pre-warm: a crawler fetches the page first and the picture
    second, so the artwork is usually already on disk by the time anyone asks for
    the card.

    Driven directly rather than through the client, because what is being
    asserted is that a real task runs — through a request it would be a race
    against the client tearing its event loop down.
    """

    def a_view(self) -> share_routes.ShareView:
        from zoneinfo import ZoneInfo

        from app.endpoints import get_endpoint

        return share_routes.ShareView(
            year=2026, month=8, endpoint=get_endpoint("shows/new"), tz=ZoneInfo("UTC"),
            hide_not_watching=False, network_filter=None,
            card_style="vertical", day_packing="stacked")

    def an_item(self, title: str, tmdb: int):
        from app.providers.base import Item, Media, Source

        return Item(source=Source.TRAKT, media=Media.SHOW, id=f"show:{title}",
                    ids={"tmdb": tmdb}, detail_url="", air_date="2026-08-04",
                    air_ts=4.0, air_display="", air_time="", day_of_week="",
                    title=title, season=1, episode_number=1)

    async def test_the_warm_runs_and_leaves_no_un_awaited_coroutine_behind(self):
        """A bare coroutine nobody awaits is a RuntimeWarning and no warm at all,
        which is the failure this shape exists to avoid."""
        seen: list[list] = []

        async def _spy(settings, refs) -> int:
            seen.append(list(refs))
            return 0

        share_routes._warming.clear()
        with warnings.catch_warnings(record=True) as caught, \
                patch.object(posters, "ensure_posters", _spy):
            warnings.simplefilter("always")
            share_routes._spawn_poster_warm(self.a_view(), 1, [self.an_item("Show", 4242)], None)
            self.assertTrue(share_routes._warm_tasks, "no task was created")
            await asyncio.gather(*list(share_routes._warm_tasks))
        self.assertEqual(seen, [[("show", 4242)]])
        self.assertEqual([w for w in caught if "never awaited" in str(w.message)], [])
        self.assertEqual(share_routes._warming, set())

    async def test_the_same_month_is_not_warmed_twice_at_once(self):
        """A crawler fetching a page, then its picture, then the same page again
        is the ordinary shape, and the set that dedupes it is also the ceiling on
        how many a burst can spawn."""
        started = anyio.Event()
        release = anyio.Event()

        async def _blocking(settings, refs) -> int:
            started.set()
            await release.wait()
            return 0

        share_routes._warming.clear()
        with patch.object(posters, "ensure_posters", _blocking):
            view, items = self.a_view(), [self.an_item("Show", 4243)]
            share_routes._spawn_poster_warm(view, 1, items, None)
            await started.wait()
            share_routes._spawn_poster_warm(view, 1, items, None)
            self.assertEqual(len(share_routes._warm_tasks), 1)
            release.set()
            await asyncio.gather(*list(share_routes._warm_tasks))


class CardAvatarTests(ShareCardTestCase):
    """The card carries the owner's avatar and no name at all, so neither the
    picture nor the URL that fetches it says who the owner is."""

    def avatar_path(self) -> Path:
        path = user_images.avatar_path(self.user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def a_card(self) -> bytes:
        resp = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8")
        self.assertEqual(resp.status_code, 200)
        return resp.content

    def test_an_avatar_is_drawn_when_there_is_one(self):
        without = self.a_card()
        path = self.avatar_path()
        Image.new("RGB", (256, 256), (60, 120, 200)).save(path, format="WEBP")
        self.assertNotEqual(self.a_card(), without)

    def test_a_file_that_is_not_an_image_costs_the_card_its_avatar_and_nothing_else(self):
        """A missing avatar is the ordinary case rather than an error, and an
        unreadable one is treated identically: there is no silhouette to fall
        back to, so the header simply closes up."""
        without = self.a_card()
        self.avatar_path().write_bytes(b"not a picture")
        self.assertEqual(self.a_card(), without)

    def test_no_username_reaches_the_picture_or_its_address(self):
        page = self.client.get(f"/s/{self.token}?year=2026&month=8").text
        image = re.search(r'property="og:image" content="([^"]+)"', page)
        self.assertIsNotNone(image)
        self.assertNotIn("cardowner", image.group(1))


class CardRateLimitTests(ShareCardTestCase):
    def test_a_throttled_request_is_refused_rather_than_redirected(self):
        """The one unhappy path that is not the static banner: redirecting a
        throttled request to a file is a redirect the throttle was trying not to
        serve. An unfurler that gets a 429 shows no picture, which is correct
        when load is being shed."""
        with patch.object(share_routes, "SHARE_RATE_MAX_ATTEMPTS", 2):
            for _ in range(2):
                self.assertEqual(self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8").status_code, 200)
            refused = self.client.get(f"/s/{self.token}/og.jpg?year=2026&month=8",
                                      follow_redirects=False)
        self.assertEqual(refused.status_code, 429)


class SharePageOpenGraphTests(ShareCardTestCase):
    """What a pasted link tells an unfurler to fetch."""

    def og_image(self, url: str) -> str | None:
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200, page.text[:200])
        found = re.search(r'property="og:image" content="([^"]+)"', page.text)
        return found.group(1) if found else None

    def test_the_page_advertises_the_picture_of_its_own_month(self):
        image = self.og_image("/s/%s?year=2026&month=8" % self.token)
        self.assertTrue(image.startswith(f"{ORIGIN}/s/{self.token}/og.jpg"), image)
        fetched = self.client.get(image[len(ORIGIN):])
        self.assertEqual(fetched.headers["content-type"], "image/jpeg")

    def test_the_advertised_url_renders_the_month_the_page_is_showing(self):
        """The URL is built from the RESOLVED view, so a visitor who typed
        nothing still gets a picture of what they are looking at rather than of
        whatever the defaults resolve to when a crawler arrives."""
        image = self.og_image("/s/%s?year=2026&month=8" % self.token)
        params = share_routes._effective_params(_FakeRequest(image))
        self.assertEqual(params.get("year"), "2026")
        self.assertEqual(params.get("month"), "8")

    def test_every_link_form_advertises_the_token_form_of_the_picture(self):
        """One picture route for all three link forms, addressed by the most
        anonymous identifier this app has — a username or a slug printed into
        markup pasted into other people's channels names the owner, which is what
        the picture itself is built not to do."""
        asyncio.run(share_links.set_custom_slug(self.user_id, "prettyslug"))
        for kind in ("username", "slug"):
            asyncio.run(share_links.set_enabled(self.user_id, kind, True))
        for url in ("/u/cardowner?year=2026&month=8", "/c/prettyslug?year=2026&month=8"):
            with self.subTest(url=url):
                image = self.og_image(url)
                self.assertIn(f"/s/{self.token}/og.jpg", image)
                self.assertNotIn("cardowner", image)
                self.assertNotIn("prettyslug", image)

    def test_the_size_is_advertised_with_the_picture(self):
        page = self.client.get(f"/s/{self.token}?year=2026&month=8").text
        self.assertIn(f'property="og:image:width" content="{share_card.CARD_W}"', page)
        self.assertIn(f'property="og:image:height" content="{share_card.CARD_H}"', page)


class NoBaseUrlTests(ShareCardTestCase):
    """An instance with no configured public origin has nothing safe to
    advertise: the request Host is not trustworthy enough to publish as the place
    to find somebody's calendar."""

    def make_settings(self):
        return Settings(trakt_client_id="cid", trakt_access_token="tok")

    def test_no_base_url_means_no_image_tag_at_all(self):
        page = self.client.get(f"/s/{self.token}?year=2026&month=8")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("og:image", page.text)
        self.assertIn('name="twitter:card" content="summary"', page.text)


class PageAndCardAgreeTests(ShareCardTestCase):
    """A card that says twelve shows over a page showing five is worse than no
    card, so both resolve the view through one function and read the month
    through one read. This is the test that catches them drifting apart."""

    def page_total(self, query: str) -> int:
        page = self.client.get(f"/s/{self.token}?{query}")
        self.assertEqual(page.status_code, 200)
        found = re.search(r"📊\s*(\d+)", page.text)
        self.assertIsNotNone(found, "the page stopped printing its own count")
        return int(found.group(1))

    def card_count(self, query: str) -> int:
        drawn: list[share_card.Card] = []
        real = share_card.build_card
        with patch.object(share_card, "build_card",
                          side_effect=lambda card: drawn.append(card) or real(card)):
            resp = self.client.get(f"/s/{self.token}/og.jpg?{query}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(drawn), 1, "the card was not rendered for this request")
        return drawn[0].count

    def assert_agrees(self, query: str) -> None:
        self.assertEqual(self.card_count(query), self.page_total(query), query)

    def setUp(self):
        super().setUp()
        self.seed([self.an_entry("Netflix One", 4, tmdb=6301, network="Netflix"),
                   self.an_entry("Netflix Two", 6, tmdb=6302, network="Netflix"),
                   self.an_entry("Elsewhere", 11, tmdb=6303, network="Some Other Channel"),
                   self.an_entry("Not Watched", 18, tmdb=6304)])

    def test_they_agree_on_a_plain_month(self):
        self.assert_agrees("year=2026&month=8")

    def test_they_agree_with_the_marks_filter_on(self):
        # A mark is filed under the item's own id, which for a Trakt show is its
        # slug.
        asyncio.run(calendar_state.set_not_watching(self.user_id, "not-watched", True))
        self.assert_agrees("year=2026&month=8&hidenw=1")
        self.assertLess(self.page_total("year=2026&month=8&hidenw=1"),
                        self.page_total("year=2026&month=8&hidenw=0"))

    def test_they_agree_with_a_network_filter(self):
        self.assert_agrees("year=2026&month=8&networks=Netflix")
        self.assertLess(self.page_total("year=2026&month=8&networks=Netflix"),
                        self.page_total("year=2026&month=8"))

    def test_the_advertised_url_carries_a_filter_the_compact_code_cannot_say(self):
        """`networks` has no field in the code's vocabulary, so the URL goes out
        verbose rather than going out short and wrong — a dropped param would
        resolve back to the owner's default and put a different count on the
        picture than the page it previews."""
        page = self.client.get("/s/%s?year=2026&month=8&networks=Netflix" % self.token)
        image = re.search(r'property="og:image" content="([^"]+)"', page.text).group(1)
        self.assertIn("networks=Netflix", image)
        self.assertEqual(self.card_count("year=2026&month=8&networks=Netflix"),
                         self.page_total("year=2026&month=8&networks=Netflix"))


class _FakeRequest:
    """Just enough of a Request for `_effective_params`: the query string of a
    URL, in the multi-item shape it reads."""

    def __init__(self, url: str):
        from urllib.parse import parse_qsl, urlsplit

        from starlette.datastructures import QueryParams

        self.query_params = QueryParams(parse_qsl(urlsplit(url).query))


if __name__ == "__main__":
    unittest.main()


class TheOwnersSourcePreferenceReachesBothSurfacesTests(ShareCardTestCase):
    """A share link is a public view of ONE person's month, so which services
    fill it is the owner's choice — the same editorial choice as the genres and
    countries a share page has always read from them. Without this, an owner who
    had narrowed their own calendar to one service handed strangers a link
    showing the titles they had narrowed it to exclude.

    IT IS READ IN ONE PLACE FOR BOTH SURFACES, which is what keeps the count
    invariant: the page and its preview picture go through one month read, so a
    preference cannot apply to one and not the other."""

    def seed_two_sources(self) -> None:
        """One window holding one airing from each service, which no matcher
        would merge — two different titles, two different id spaces."""
        from app.providers.base import Media, Record, Source

        day = date(2026, 8, 12)
        records = [
            Record(source=Source.TRAKT, media=Media.SHOW, id="trakt-only",
                   ids={"trakt": 55, "slug": "trakt-only"},
                   detail_url="https://trakt.tv/shows/trakt-only", title="Trakt Only",
                   air_ts=1786276800.0, season=1, episode_number=1, episode_label="S01E01"),
            Record(source=Source.SIMKL, media=Media.SHOW, id="simkl-only",
                   ids={"simkl": 77}, detail_url="https://simkl.com/tv/77",
                   title="Simkl Only", air_ts=1786276800.0, season=1,
                   episode_number=1, episode_label="S01E01"),
        ]
        asyncio.run(calendar_cache.store_window(
            "shows/new", calendar_cache.window_start(day), records, 600, db.now(),
            sources=["trakt", "simkl"]))

    def page_total(self, query: str) -> int:
        page = self.client.get(f"/s/{self.token}?{query}")
        self.assertEqual(page.status_code, 200)
        return int(re.search(r"📊\s*(\d+)", page.text).group(1))

    def card_count(self, query: str) -> int:
        drawn: list[share_card.Card] = []
        real = share_card.build_card
        with patch.object(share_card, "build_card",
                          side_effect=lambda card: drawn.append(card) or real(card)):
            resp = self.client.get(f"/s/{self.token}/og.jpg?{query}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(drawn), 1, "the card was not rendered for this request")
        return drawn[0].count

    def narrow_the_owner_to(self, selection: str) -> None:
        from app.sources import prefs as source_prefs

        asyncio.run(source_prefs.save(source_prefs.SourcePrefs(
            user_id=self.user_id, calendar_source=selection)))

    def setUp(self):
        super().setUp()
        self.seed_two_sources()
        self.query = "year=2026&month=8"

    def test_an_owner_who_has_said_nothing_sees_every_service(self):
        page = self.client.get(f"/s/{self.token}?{self.query}").text
        self.assertIn("Trakt Only", page)
        self.assertIn("Simkl Only", page)

    def test_the_owners_narrowing_applies_to_the_public_page(self):
        self.narrow_the_owner_to("trakt")
        page = self.client.get(f"/s/{self.token}?{self.query}").text
        self.assertIn("Trakt Only", page)
        self.assertNotIn("Simkl Only", page)

    def test_the_picture_narrows_with_the_page_it_previews(self):
        """The count invariant, under the one preference that can change what a
        month holds without changing a single view option in the URL."""
        wide = self.page_total(self.query)
        self.assertEqual(self.card_count(self.query), wide)
        self.narrow_the_owner_to("trakt")
        narrow = self.page_total(self.query)
        self.assertLess(narrow, wide)
        self.assertEqual(self.card_count(self.query), narrow)

    def test_narrowing_rewrites_no_stored_window(self):
        """Applied at read over the shared row, so the next visitor of a
        different owner's link still finds everything the fill stored."""
        self.narrow_the_owner_to("trakt")
        self.page_total(self.query)
        window, _ = asyncio.run(calendar_cache.read_cached_window(
            "shows/new", calendar_cache.window_start(date(2026, 8, 12))))
        self.assertEqual(sorted(s for g in window.groups for s in g["by_source"]),
                         ["simkl", "trakt"])
