"""Unit tests for the network-logo matching + history-based rollout candidates.

Pure/offline: _pick_network is pure; fetch_watched_progress is tested with
fetch_history mocked.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.media import logos
from app.providers.trakt import sync as trakt_sync


class PickNetworkTests(unittest.TestCase):
    NETS = [{"id": 1, "name": "AMC", "logo_path": "/amc.png"},
            {"id": 2, "name": "Paramount+", "logo_path": "/p.png"},
            {"id": 3, "name": "Netflix", "logo_path": "/n.png"}]

    def test_exact_normalized(self):
        self.assertEqual(logos._pick_network(self.NETS, "Netflix")["id"], 3)

    def test_plus_suffix_matches(self):
        # "AMC+" normalizes to "amc" == "AMC" -> exact.
        self.assertEqual(logos._pick_network(self.NETS, "AMC+")["id"], 1)

    def test_combined_brand_prefix(self):
        # "Paramount+ with Showtime" -> starts with "paramount" -> Paramount+.
        self.assertEqual(logos._pick_network(self.NETS, "Paramount+ with Showtime")["id"], 2)

    def test_no_match_falls_back_to_first(self):
        self.assertEqual(logos._pick_network(self.NETS, "Totally Unknown Net")["id"], 1)

    def test_empty(self):
        self.assertIsNone(logos._pick_network([], "AMC"))


class EnsureLogosTests(unittest.IsolatedAsyncioTestCase):
    """Pre-warm: generate the tiles a roster needs, so a show added before tmdb
    was stored doesn't depend on another show requesting its network first."""

    CFG = SimpleNamespace(tmdb_configured=True)

    async def test_one_lookup_per_network_and_missing_tmdb_skipped(self):
        roster = [
            ("AMC", 11), ("AMC", 12),      # same network twice -> one call
            ("Netflix", 20),
            ("HBO", None), ("", 30),       # no tmdb / no name -> skipped
        ]
        seen = []

        async def fake(settings, network, tmdb):
            seen.append((network, tmdb))
            return logos._tile_path(network)

        with patch("app.media.logos._tile_path", side_effect=lambda n: __import__("pathlib").Path(f"/nope/{n}.png")), \
             patch("app.media.logos.is_negative", return_value=False), \
             patch("app.media.logos.ensure_logo", side_effect=fake):
            generated = await logos.ensure_logos(self.CFG, roster)

        self.assertEqual(sorted(seen), [("AMC", 11), ("Netflix", 20)])
        self.assertEqual(generated, 2)

    async def test_already_cached_or_negative_networks_are_skipped(self):
        async def fake(settings, network, tmdb):
            return logos._tile_path(network)

        # AMC has a tile already; HBO is negative-cached; only Netflix is missing.
        with patch("app.media.logos._tile_path") as tile, \
             patch("app.media.logos.is_negative", side_effect=lambda n: n == "HBO"), \
             patch("app.media.logos.ensure_logo", side_effect=fake) as gen:
            tile.side_effect = lambda n: SimpleNamespace(exists=lambda: n == "AMC")
            await logos.ensure_logos(self.CFG, [("AMC", 1), ("HBO", 2), ("Netflix", 3)])

        called = {c.args[1] for c in gen.call_args_list}
        self.assertEqual(called, {"Netflix"})

    async def test_no_tmdb_key_is_a_noop(self):
        with patch("app.media.logos.ensure_logo") as gen:
            n = await logos.ensure_logos(SimpleNamespace(tmdb_configured=False), [("AMC", 1)])
        self.assertEqual(n, 0)
        gen.assert_not_called()

    async def test_a_failure_on_one_network_does_not_sink_the_rest(self):
        async def fake(settings, network, tmdb):
            if network == "AMC":
                raise RuntimeError("tmdb down")
            return logos._tile_path(network)

        with patch("app.media.logos._tile_path", side_effect=lambda n: __import__("pathlib").Path(f"/nope/{n}.png")), \
             patch("app.media.logos.is_negative", return_value=False), \
             patch("app.media.logos.ensure_logo", side_effect=fake):
            generated = await logos.ensure_logos(self.CFG, [("AMC", 1), ("Netflix", 2)])
        self.assertEqual(generated, 1)  # Netflix survived


class RegenerateTests(unittest.IsolatedAsyncioTestCase):
    """A regenerate that makes nothing changes nothing: TMDB unreachable and
    TMDB with no logo reach `ensure_logo` alike, as a miss."""

    CFG = SimpleNamespace(tmdb_configured=True)

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        patcher = patch("app.media.logos.LOGO_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tile = logos._tile_path("HBO")
        self.tile.write_bytes(b"old tile")

    async def test_a_new_tile_replaces_the_old_one(self):
        async def made(settings, network, tmdb):
            logos._tile_path(network).write_bytes(b"new tile")
            return logos._tile_path(network)

        with patch("app.media.logos.ensure_logo", side_effect=made):
            self.assertEqual(await logos.regenerate(self.CFG, "HBO", 1), self.tile)
        self.assertEqual(self.tile.read_bytes(), b"new tile")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["hbo.png"])

    async def test_nothing_made_keeps_the_old_tile_and_marks_nothing_missing(self):
        async def missed(settings, network, tmdb):
            logos._none_path(network).write_text("")
            return None

        with patch("app.media.logos.ensure_logo", side_effect=missed):
            self.assertIsNone(await logos.regenerate(self.CFG, "HBO", 1))
        self.assertEqual(self.tile.read_bytes(), b"old tile")
        self.assertFalse(logos.is_negative("HBO"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["hbo.png"])

    async def test_a_failure_puts_the_old_tile_back(self):
        with patch("app.media.logos.ensure_logo", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                await logos.regenerate(self.CFG, "HBO", 1)
        self.assertEqual(self.tile.read_bytes(), b"old tile")

    async def test_the_version_a_page_draws_moves_with_a_new_tile(self):
        before = logos.versions(["HBO", "AMC"])
        self.assertEqual(set(before), {"HBO"})

        async def made(settings, network, tmdb):
            path = logos._tile_path(network)
            path.write_bytes(b"new tile")
            import os
            os.utime(path, ns=(before["HBO"] + 10**9, before["HBO"] + 10**9))
            return path

        with patch("app.media.logos.ensure_logo", side_effect=made):
            await logos.regenerate(self.CFG, "HBO", 1)
        self.assertNotEqual(logos.versions(["HBO"])["HBO"], before["HBO"])


class FetchWatchedProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregates_distinct_episodes_from_history(self):
        events = [
            {"type": "episode", "show": {"title": "A", "ids": {"trakt": 10, "tmdb": 111, "slug": "a"}},
             "episode": {"season": 1, "number": 1}},
            {"type": "episode", "show": {"title": "A", "ids": {"trakt": 10, "tmdb": 111, "slug": "a"}},
             "episode": {"season": 1, "number": 2}},
            {"type": "episode", "show": {"title": "A", "ids": {"trakt": 10, "tmdb": 111, "slug": "a"}},
             "episode": {"season": 1, "number": 2}},  # rewatch -> deduped
            {"type": "movie", "movie": {"title": "M", "ids": {"trakt": 99}}},  # ignored
            {"type": "episode", "show": {"title": "S", "ids": {"trakt": 5}},
             "episode": {"season": 0, "number": 1}},  # special -> skipped
        ]
        with patch("app.providers.trakt.sync.fetch_history", return_value=events):
            out = await trakt_sync.fetch_watched_progress(SimpleNamespace(), since_days=60)
        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertEqual((rec["season"], rec["watched"]), (1, 2))
        # The whole id map travels, because the caller decides which of them the
        # row it writes is keyed on.
        # `trakt_slug` beside `slug`: Trakt's normalizer namespaces the slug it
        # was handed, because Simkl calls that field `slug` too and disagrees on
        # what it holds.
        self.assertEqual(rec["ids"], {"trakt": 10, "tmdb": 111, "slug": "a",
                                      "trakt_slug": "a"})


def _png(colour, size=(200, 80)) -> bytes:
    """A logo-shaped PNG: an opaque block of `colour` inside transparent margins,
    which is what a real network logo looks like to the trim step."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (size[0] - 40, size[1] - 20), colour), (20, 10))
    out = BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class RenderTileTests(unittest.TestCase):
    """The tile is what a card draws, so its background has to keep the logo
    readable whichever colour the logo is."""

    def test_a_dark_logo_sits_on_white_and_a_light_one_on_dark(self):
        for colour, background in (((20, 20, 20, 255), logos._WHITE_BG),
                                   ((250, 250, 250, 255), logos._LIGHT_BG)):
            with self.subTest(colour=colour):
                tile = logos._render_tile(_png(colour))
                self.assertEqual(tile.size, (logos.TILE, logos.TILE))
                # A point inside the rounded corner's straight edge is background.
                self.assertEqual(tile.getpixel((logos.TILE // 2, 3)), background)

    def test_the_logo_is_trimmed_and_fitted_inside_the_padding(self):
        tile = logos._render_tile(_png((200, 0, 0, 255), size=(1000, 300)))
        drawn = [x for x in range(logos.TILE)
                 if tile.getpixel((x, logos.TILE // 2))[:3] == (200, 0, 0)]
        self.assertEqual((min(drawn), max(drawn) + 1),
                         (logos.PAD, logos.TILE - logos.PAD))

    def test_bytes_that_are_not_an_image_are_no_tile(self):
        self.assertIsNone(logos._render_tile(b"<svg>not a raster</svg>"))


class EnsureLogoTests(unittest.IsolatedAsyncioTestCase):
    """Generating a tile from TMDB, with TMDB answered by stubs: which logo is
    fetched, what is written to disk, and what is remembered when there is none."""

    CFG = SimpleNamespace(tmdb_configured=True)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="logos-"))
        self.patches = [patch.object(logos, "LOGO_DIR", self.dir)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _tmdb(self, *, networks, images=None, downloads=None):
        """Stub TMDB: the show's `/tv/{id}` answer carries `networks`,
        /network/{id}/images answers `images`, and a download answers from
        `downloads` by the url's tail."""
        async def title_payload(settings, tmdb_id, media, **kw):
            return {"networks": networks}

        async def cached_get(settings, path, params=None, **kw):
            return images

        fetched = []

        async def download(url):
            fetched.append(url)
            return (downloads or {}).get(url.rsplit("/", 1)[-1])

        stack = [patch.object(logos.tmdb_detail, "title_payload", title_payload),
                 patch.object(logos.tmdb_transport, "cached_get", cached_get),
                 patch.object(logos.tmdb_transport, "download", download)]
        for p in stack:
            p.start()
            self.patches.append(p)
        return fetched

    async def test_a_raster_logo_is_fetched_once_and_kept_on_disk(self):
        fetched = self._tmdb(networks=[{"id": 1, "name": "AMC", "logo_path": "/amc.png"}],
                             downloads={"amc.png": _png((20, 20, 20, 255))})
        tile = await logos.ensure_logo(self.CFG, "AMC", 99)
        self.assertEqual(tile, self.dir / "amc.png")
        with Image.open(tile) as written:
            self.assertEqual(written.size, (logos.TILE, logos.TILE))
        self.assertEqual(await logos.ensure_logo(self.CFG, "AMC", 99), tile)
        self.assertEqual(len(fetched), 1, "a tile on disk is not fetched again")

    async def test_an_svg_logo_prefers_the_networks_raster_alternative(self):
        fetched = self._tmdb(
            networks=[{"id": 7, "name": "HBO", "logo_path": "/hbo.svg"}],
            images={"logos": [{"file_path": "/hbo-alt.svg"}, {"file_path": "/hbo-alt.png"}]},
            downloads={"hbo-alt.png": _png((20, 20, 20, 255))})
        self.assertIsNotNone(await logos.ensure_logo(self.CFG, "HBO", 5))
        self.assertEqual([u.rsplit("/", 1)[-1] for u in fetched], ["hbo-alt.png"])

    async def test_no_usable_logo_is_remembered_so_tmdb_is_not_asked_again(self):
        self._tmdb(networks=[{"id": 1, "name": "Tiny", "logo_path": None}], images={"logos": []})
        self.assertIsNone(await logos.ensure_logo(self.CFG, "Tiny", 3))
        self.assertTrue(logos.is_negative("Tiny"))
        with patch.object(logos, "_tmdb_tv_networks", AsyncMock()) as asked:
            self.assertIsNone(await logos.ensure_logo(self.CFG, "Tiny", 3))
        asked.assert_not_awaited()

    async def test_a_tmdb_that_could_not_be_asked_leaves_no_marker(self):
        """A failure is not a network with no logo: marking it would hide the
        logo until the marker aged out, when the next request could have had it."""
        async def down(*a, **kw):
            raise logos.tmdb_transport.TmdbError("unreachable")
        with patch.object(logos.tmdb_detail, "title_payload", down):
            self.assertIsNone(await logos.ensure_logo(self.CFG, "AMC", 99))
        self.assertFalse(logos.is_negative("AMC"))

    async def test_nothing_is_asked_without_tmdb_or_without_an_id(self):
        with patch.object(logos, "_tmdb_tv_networks", AsyncMock()) as asked:
            self.assertIsNone(await logos.ensure_logo(SimpleNamespace(tmdb_configured=False), "AMC", 1))
            self.assertIsNone(await logos.ensure_logo(self.CFG, "AMC", None))
            self.assertIsNone(await logos.ensure_logo(self.CFG, "", 1))
        asked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class BuildingStaysOffTheLoopTests(unittest.IsolatedAsyncioTestCase):
    """A cold logo cache filled without freezing the app. MEASURED before this:
    a month's warm started all 362 missing networks at once (~700 requests on a
    20-connection pool, one logo waiting up to 12 s); after: eight at a time, the
    warm done in ~3.5 s with one logo's p50 at 66 ms, and the event loop never
    blocked more than 84 ms once the process's trust store had been read."""

    CFG = SimpleNamespace(tmdb_configured=True)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="logos-"))
        patcher = patch.object(logos, "LOGO_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_two_requests_for_one_network_share_one_build(self):
        builds = []

        async def generate(settings, network, tmdb_id, tile):
            builds.append(network)
            await asyncio.sleep(0.05)
            return None
        with patch.object(logos, "_generate", generate):
            await asyncio.gather(logos.ensure_logo(self.CFG, "AMC", 1),
                                 logos.ensure_logo(self.CFG, "AMC", 2))
        self.assertEqual(builds, ["AMC"])

    async def test_a_warm_builds_at_most_eight_at_once(self):
        state = {"now": 0, "max": 0}

        async def generate(settings, network, tmdb_id, tile):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return None
        with patch.object(logos, "_generate", generate):
            await logos.ensure_logos(self.CFG, [(f"Net {n}", n + 1) for n in range(40)])
        self.assertEqual(state["max"], logos._BUILDS_AT_ONCE)
        self.assertEqual(logos._BUILDS_AT_ONCE, 8)

    async def test_the_render_and_the_write_happen_off_the_loop_thread(self):
        import threading
        seen = []
        real = logos._write_tile

        def write_tile(*args):
            seen.append(threading.get_ident())
            return real(*args)

        async def title_payload(settings, tmdb_id, media, **kw):
            return {"networks": [{"id": 1, "name": "AMC", "logo_path": "/amc.png"}]}

        async def download(url):
            return _png((20, 20, 20, 255))
        with patch.object(logos, "_write_tile", write_tile), \
             patch.object(logos.tmdb_detail, "title_payload", title_payload), \
             patch.object(logos.tmdb_transport, "download", download):
            tile = await logos.ensure_logo(self.CFG, "AMC", 99)
        self.assertTrue(tile.exists())
        self.assertNotEqual(seen, [threading.get_ident()])


class ANameWithNoLatinLettersTests(unittest.TestCase):

    def test_a_name_with_no_latin_letters_gets_its_own_file(self):
        """Every such name used to slug to "" — one shared ".png" and ".none"."""
        names = ["哔哩哔哩", "НТВ", "テレビ東京"]
        slugs = [logos.slug(n) for n in names]
        self.assertTrue(all(slugs))
        self.assertEqual(len(set(slugs)), 3)
        self.assertEqual(logos.slug("哔哩哔哩"), slugs[0])  # stable
        self.assertEqual(logos.slug("AMC+"), "amc")          # Latin names unchanged
