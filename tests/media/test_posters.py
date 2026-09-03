"""The poster URL registry (app/media/artwork.py) and the poster tile cache
(app/media/posters.py).

MEDIA NAMESPACING is the property this file cares about most: TMDB ids are
namespaced per media type, so movie 550 and show 550 must never share a row, a
cache path, or a lookup — every test that touches both media types asserts they
stay apart.
"""
from __future__ import annotations

import asyncio
import os
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from app import db
from app.media import artwork, logos, posters
from app.media import tmdb as tmdb_client
from tests.support import TMP, migrated_db

NOT_CONFIGURED = SimpleNamespace(tmdb_configured=False, tmdb_api_key="")
CONFIGURED = SimpleNamespace(tmdb_configured=True, tmdb_api_key="deadbeef" * 5)


def _jpeg_bytes(size=(300, 450)) -> bytes:
    """Real, decodable JPEG bytes at an arbitrary (non-tile) size, so a test
    exercising the resolution chain can prove the normalize step actually ran."""
    buf = BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# app/media/artwork.py — the registry
# ---------------------------------------------------------------------------

class ArtworkTestCase(unittest.TestCase):
    def setUp(self):
        migrated_db("artwork")

    def tearDown(self):
        db.close_thread_connection()

    def rows(self, sql: str, params=()) -> list:
        return asyncio.run(db.fetch_all(sql, params))

    def value(self, sql: str, params=()):
        return asyncio.run(db.fetch_value(sql, params))


class RegistryTests(ArtworkTestCase):
    def test_best_url_prefers_tmdb_over_trakt(self):
        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg"))
        asyncio.run(artwork.record_poster_url("show", 1396, "tmdb", "https://img/tmdb.jpg"))
        self.assertEqual(
            asyncio.run(artwork.best_url("show", 1396)), ("tmdb", "https://img/tmdb.jpg"))

    def test_no_row_is_no_url(self):
        self.assertIsNone(asyncio.run(artwork.best_url("show", 999)))

    def test_same_url_only_bumps_last_seen_at(self):
        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/a.jpg"))
        first_seen = self.value(
            "SELECT first_seen_at FROM show_posters WHERE media='show' AND tmdb=1396")
        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/a.jpg"))
        self.assertEqual(
            self.value("SELECT first_seen_at FROM show_posters WHERE media='show' AND tmdb=1396"),
            first_seen)
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM show_posters WHERE media='show' AND tmdb=1396"), 1)

    def test_a_changed_url_replaces_the_row_and_resets_fail_count(self):
        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/old.jpg"))
        asyncio.run(artwork.record_failure("show", 1396, "trakt"))
        asyncio.run(artwork.record_failure("show", 1396, "trakt"))
        self.assertEqual(
            self.value("SELECT fail_count FROM show_posters WHERE media='show' AND tmdb=1396"), 2)

        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/new.jpg"))
        row = self.rows(
            "SELECT url, fail_count, last_failed_at FROM show_posters "
            "WHERE media='show' AND tmdb=1396")[0]
        self.assertEqual(row["url"], "https://img/new.jpg")
        self.assertEqual(row["fail_count"], 0)
        self.assertIsNone(row["last_failed_at"])

    def test_a_source_past_max_fail_count_is_skipped(self):
        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/a.jpg"))
        for _ in range(artwork.MAX_FAIL_COUNT):
            asyncio.run(artwork.record_failure("show", 1396, "trakt"))
        self.assertIsNone(asyncio.run(artwork.best_url("show", 1396)))

    def test_media_namespacing_show_and_movie_never_collide(self):
        asyncio.run(artwork.record_poster_url("show", 550, "trakt", "https://img/show550.jpg"))
        asyncio.run(artwork.record_poster_url("movie", 550, "trakt", "https://img/movie550.jpg"))
        self.assertEqual(
            asyncio.run(artwork.best_url("show", 550)), ("trakt", "https://img/show550.jpg"))
        self.assertEqual(
            asyncio.run(artwork.best_url("movie", 550)), ("trakt", "https://img/movie550.jpg"))
        self.assertEqual(
            self.value("SELECT COUNT(*) FROM show_posters WHERE tmdb = 550"), 2)

    def test_sweep_drops_only_rows_past_retention(self):
        now = db.now()
        asyncio.run(artwork.record_poster_url("show", 1, "trakt", "https://img/old.jpg"))
        asyncio.run(db.execute(
            "UPDATE show_posters SET last_seen_at = ? WHERE media='show' AND tmdb=1",
            (now - artwork.POSTER_URL_RETENTION_SECONDS - 10,)))
        asyncio.run(artwork.record_poster_url("show", 2, "trakt", "https://img/new.jpg"))

        removed = asyncio.run(artwork.sweep(now))

        self.assertEqual(removed, 1)
        remaining = {row["tmdb"] for row in self.rows("SELECT tmdb FROM show_posters")}
        self.assertEqual(remaining, {2})


# ---------------------------------------------------------------------------
# app/media/posters.py — the tile cache and resolution chain
# ---------------------------------------------------------------------------

class PosterCacheTests(unittest.IsolatedAsyncioTestCase):
    """The tile cache and the resolution chain.

    THE KEY CARRIES THE SOURCE, which is the property most of this class is
    about. Two viewers with opposite artwork precedence want different pictures
    for the same title, so a file keyed on (media, tmdb) alone would let one
    viewer's preference decide what the other sees — a per-viewer value reaching
    shared storage.
    """

    ORDER = ("trakt", "tmdb")

    def setUp(self):
        # A fresh corner of the shared temp DATA_DIR per test, so tests never
        # see each other's tiles.
        posters.POSTER_DIR = TMP / f"posters-{id(self)}"
        # The registry is read for real now that it leads the chain, so these
        # need a database where they used to patch `best_url` and never touch one.
        migrated_db(f"posters-{id(self)}")

    def tearDown(self):
        db.close_thread_connection()

    def _place(self, media, tmdb, source, body=b"already generated"):
        tile = posters._tile_path(media, tmdb, source)
        tile.parent.mkdir(parents=True, exist_ok=True)
        tile.write_bytes(body)
        return tile

    async def test_disk_hit_short_circuits_the_whole_chain(self):
        tile = self._place("show", 1396, "trakt")

        with patch("app.media.posters.tmdb_client.get_json") as get_json, \
             patch("app.media.posters.tmdb_client.download") as download, \
             patch("app.media.artwork.urls_for") as urls_for:
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertEqual(result, tile)
        get_json.assert_not_called()
        download.assert_not_called()
        urls_for.assert_not_called()

    async def test_a_tile_from_a_later_source_still_counts_as_a_hit(self):
        """The order is a fall-through, so a picture stored under the second
        choice answers rather than being re-resolved from the first."""
        tile = self._place("show", 1396, "tmdb")

        with patch("app.media.posters.tmdb_client.download") as download:
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertEqual(result, tile)
        download.assert_not_called()

    async def test_two_orders_get_two_different_files(self):
        """THE WHOLE REASON THE SOURCE IS IN THE KEY. One owner prefers Trakt's
        picture and another Simkl's; under one key the second would be served
        the first one's choice for as long as it sat on disk."""
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")
        await artwork.record_poster_url("show", 1396, "simkl", "https://img/simkl.jpg")
        asked = []

        async def _download(url):
            asked.append(url)
            return _jpeg_bytes()

        with patch("app.media.posters.tmdb_client.download", new=_download):
            trakt_tile = await posters.ensure_poster(
                CONFIGURED, "show", 1396, ("trakt", "simkl"))
            simkl_tile = await posters.ensure_poster(
                CONFIGURED, "show", 1396, ("simkl", "trakt"))

        self.assertNotEqual(trakt_tile, simkl_tile)
        self.assertTrue(trakt_tile.exists() and simkl_tile.exists())
        self.assertIn("trakt.jpg", asked[0])
        self.assertIn("simkl.jpg", asked[1])

    async def test_negative_marker_short_circuits_the_whole_chain(self):
        for source in self.ORDER:
            marker = posters._none_path("show", 1396, source)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("", encoding="utf-8")

        with patch("app.media.posters.tmdb_client.get_json") as get_json, \
             patch("app.media.posters.tmdb_client.download") as download:
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNone(result)
        get_json.assert_not_called()
        download.assert_not_called()

    async def test_one_source_given_up_on_does_not_blind_the_others(self):
        """`is_negative` asks whether EVERY source has been given up on. One
        service having no artwork says nothing about the next, and reading it as
        the answer would strand a poster the second one is holding."""
        marker = posters._none_path("show", 1396, "trakt")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")

        self.assertFalse(posters.is_negative("show", 1396, self.ORDER))

    async def test_the_registry_is_consulted_before_any_lookup_is_paid_for(self):
        """THE ORDER OF THE CHAIN CHANGED AND THIS IS THE ASSERTION THAT SAYS SO.
        A TMDB detail call used to be made for every cold poster before the
        registry was read at all, which spent a request to learn something a
        calendar fill had already written down — and decided the picture would
        be TMDB's whatever the page was showing."""
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")

        with patch("app.media.posters.tmdb_client.get_json") as get_json, \
             patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=_jpeg_bytes())):
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "1396.trakt.jpg")
        get_json.assert_not_called()

    async def test_the_fetch_goes_through_the_image_proxy_at_the_tile_size(self):
        """The resize lives at the proxy now, so the URL asked for is the whole
        of what makes the stored bytes the right shape."""
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")
        asked = []

        async def _download(url):
            asked.append(url)
            return _jpeg_bytes()

        with patch("app.media.posters.tmdb_client.download", new=_download):
            await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0].startswith("https://wsrv.nl/"))
        self.assertIn("w=500", asked[0])
        self.assertIn("h=750", asked[0])
        # Contain-with-a-canvas is a PAD. A wrong-aspect poster from a fallback
        # source must come out letterboxed, never stretched or cropped.
        self.assertIn("fit=contain", asked[0])

    async def test_what_the_proxy_returns_is_stored_unchanged(self):
        """No re-encode. Asking the proxy for the exact canvas and then decoding
        and re-encoding it here would be paying twice for one resize."""
        body = _jpeg_bytes(size=(500, 750))
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")

        with patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=body)):
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertEqual(result.read_bytes(), body)

    async def test_an_oversized_picture_is_refused(self):
        """The decompression-bomb guard is the reason a verify step survived the
        pipeline's removal: this is the only place bytes from somebody else's
        host are opened before the share card composites them."""
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")
        huge = _jpeg_bytes(size=(posters.MAX_SOURCE_DIMENSION + 1, 10))

        with patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=huge)), \
             patch("app.media.artwork.record_failure", new=AsyncMock()), \
             patch("app.media.posters._fresh_provider_lookup",
                   new=AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)

    async def test_tmdb_is_the_fallback_and_records_the_url(self):
        with patch("app.media.posters.tmdb_client.get_json",
                   new=AsyncMock(return_value={"poster_path": "/x.jpg"})), \
             patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=_jpeg_bytes())), \
             patch("app.media.artwork.record_poster_url", new=AsyncMock()) as record:
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "1396.tmdb.jpg")
        record.assert_awaited_once_with(
            "show", 1396, "tmdb", f"{posters.tmdb_client.IMG}/w500/x.jpg")

    async def test_tmdb_is_not_asked_when_it_is_not_in_the_order(self):
        """An order is a statement about which services' artwork this viewer
        wants. Reaching past it to TMDB anyway would make the preference
        advisory."""
        with patch("app.media.posters.tmdb_client.get_json") as get_json, \
             patch("app.media.posters._fresh_provider_lookup",
                   new=AsyncMock(return_value=None)):
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)
        get_json.assert_not_called()

    async def test_a_failing_registry_url_falls_through_and_increments_fail_count(self):
        await artwork.record_poster_url("show", 1396, "trakt", "https://dead/x.jpg")

        with patch("app.media.posters.tmdb_client.download", new=AsyncMock(return_value=None)), \
             patch("app.media.artwork.record_failure", new=AsyncMock()) as record_failure, \
             patch("app.media.posters._fresh_provider_lookup", new=AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)
        record_failure.assert_awaited_once_with("show", 1396, "trakt")
        # The negative marker is what makes the next request skip resolution.
        self.assertTrue(posters.is_negative("show", 1396, ("trakt",)))

    async def test_a_non_image_registry_body_also_falls_through(self):
        """"Unreachable cached URLs fall through" covers a non-image body, not
        just a network failure — the difference only shows up once Pillow tries
        to open it, so this goes through `_verify` for real."""
        await artwork.record_poster_url("show", 1396, "trakt", "https://dead/x.jpg")

        with patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=b"not an image")), \
             patch("app.media.artwork.record_failure", new=AsyncMock()) as record_failure, \
             patch("app.media.posters._fresh_provider_lookup", new=AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)
        record_failure.assert_awaited_once_with("show", 1396, "trakt")

    async def test_fresh_provider_lookup_is_the_last_resort(self):
        with patch("app.media.posters._fresh_provider_lookup",
                   new=AsyncMock(return_value="https://fresh/x.jpg")), \
             patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=_jpeg_bytes())):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNotNone(result)
        self.assertTrue(result.exists())

    async def test_nothing_resolved_writes_a_negative_marker(self):
        """Against the FIRST source in the order, not against every one tried:
        enough to stop this order retrying, while leaving a viewer who prefers
        the other service free to try it."""
        with patch("app.media.posters._fresh_provider_lookup", new=AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNone(result)
        self.assertTrue(posters._none_path("show", 1396, self.ORDER[0]).exists())
        self.assertFalse(posters._none_path("show", 1396, self.ORDER[1]).exists())

    async def test_media_namespacing_show_and_movie_never_share_a_file(self):
        with patch("app.media.posters.tmdb_client.get_json",
                   new=AsyncMock(return_value={"poster_path": "/x.jpg"})), \
             patch("app.media.posters.tmdb_client.download",
                   new=AsyncMock(return_value=_jpeg_bytes())), \
             patch("app.media.artwork.record_poster_url", new=AsyncMock()):
            show_tile = await posters.ensure_poster(CONFIGURED, "show", 550, self.ORDER)
            movie_tile = await posters.ensure_poster(CONFIGURED, "movie", 550, self.ORDER)

        self.assertNotEqual(show_tile, movie_tile)
        self.assertTrue(show_tile.exists())
        self.assertTrue(movie_tile.exists())
        self.assertEqual(show_tile.parent.name, "show")
        self.assertEqual(movie_tile.parent.name, "movie")

        # A negative marker for one media/tmdb pair must never blind the other.
        marker = posters._none_path("show", 551, "trakt")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
        self.assertFalse(posters.is_negative("movie", 551, ("trakt",)))

    async def test_invalid_pairs_are_a_clean_none_not_an_error(self):
        self.assertIsNone(await posters.ensure_poster(CONFIGURED, "book", 1))
        self.assertIsNone(await posters.ensure_poster(CONFIGURED, "show", None))
        self.assertIsNone(await posters.ensure_poster(CONFIGURED, "show", "not-a-number"))


class EnsurePostersTests(unittest.IsolatedAsyncioTestCase):
    ORDER = ("trakt", "tmdb")

    def setUp(self):
        posters.POSTER_DIR = TMP / f"posters-warm-{id(self)}"

    async def test_dedupes_skips_cached_and_bounds_fanout(self):
        cached_tile = posters._tile_path("show", 1, "trakt")
        cached_tile.parent.mkdir(parents=True, exist_ok=True)
        cached_tile.write_bytes(b"x")
        for source in self.ORDER:
            posters._none_path("show", 2, source).write_text("", encoding="utf-8")

        seen = []

        async def fake_ensure(settings, media, tmdb, order=posters.DEFAULT_ORDER):
            seen.append((media, tmdb))
            return posters._tile_path(media, tmdb, order[0])

        with patch("app.media.posters.ensure_poster", side_effect=fake_ensure):
            generated = await posters.ensure_posters(
                CONFIGURED,
                [("show", 1), ("show", 1), ("show", 2), ("show", 3), ("movie", 3)],
                self.ORDER,
            )

        self.assertEqual(sorted(seen), [("movie", 3), ("show", 3)])
        self.assertEqual(generated, 2)

    async def test_a_failure_on_one_does_not_sink_the_rest(self):
        async def fake_ensure(settings, media, tmdb, order=posters.DEFAULT_ORDER):
            if tmdb == 1:
                raise RuntimeError("boom")
            return posters._tile_path(media, tmdb, order[0])

        with patch("app.media.posters.ensure_poster", side_effect=fake_ensure):
            generated = await posters.ensure_posters(
                CONFIGURED, [("show", 1), ("show", 2)], self.ORDER)

        self.assertEqual(generated, 1)


class SweepTests(unittest.TestCase):
    def setUp(self):
        posters.POSTER_DIR = TMP / f"posters-sweep-{id(self)}"
        posters.POSTER_DIR.mkdir(parents=True)

    def _write(self, name: str, size: int, mtime: float) -> Path:
        p = posters.POSTER_DIR / name
        p.write_bytes(b"x" * size)
        os.utime(p, (mtime, mtime))
        return p

    # A CLOCK JUST AFTER THE FILES, so these stay about the SIZE rule. Left at
    # the real one, every fixture below is decades past the age ceiling and would
    # be reclaimed before the LRU pass ever ran — which is the age rule working,
    # but it is not what this class is for.
    NOW = 4000

    def test_evicts_oldest_first_until_under_the_cap(self):
        oldest = self._write("a.jpg", 100, mtime=1000)
        middle = self._write("b.jpg", 100, mtime=2000)
        newest = self._write("c.jpg", 100, mtime=3000)

        removed = posters.sweep(max_bytes=150, now=self.NOW)

        self.assertEqual(removed, 2)
        self.assertFalse(oldest.exists())
        self.assertFalse(middle.exists())
        self.assertTrue(newest.exists())

    def test_under_the_cap_is_a_noop(self):
        self._write("a.jpg", 100, mtime=1000)
        self.assertEqual(posters.sweep(max_bytes=1_000_000, now=self.NOW), 0)

    def test_missing_directory_is_a_noop(self):
        posters.POSTER_DIR = TMP / "does-not-exist"
        self.assertEqual(posters.sweep(max_bytes=0, now=self.NOW), 0)



class TheAgeCeilingTests(unittest.TestCase):
    """TMDB's terms cap how long anything obtained from them may be kept, and a
    SIZE cap does not satisfy that.

    An instance comfortably under its byte budget would keep a tile for ever,
    which is exactly the case the terms are about. So the age rule runs first and
    unconditionally, and the byte cap is this app's own housekeeping behind it.
    """

    def setUp(self):
        posters.POSTER_DIR = TMP / f"posters-age-{id(self)}"
        posters.POSTER_DIR.mkdir(parents=True)
        self.now = 2_000_000_000.0

    def _write(self, name: str, *, age_days: float, size: int = 100) -> Path:
        path = posters.POSTER_DIR / name
        path.write_bytes(b"x" * size)
        when = self.now - age_days * 86400
        os.utime(path, (when, when))
        return path

    def test_a_tile_past_the_ceiling_goes_even_when_nothing_is_full(self):
        old = self._write("old.jpg", age_days=200)
        fresh = self._write("fresh.jpg", age_days=10)
        removed = posters.sweep(max_bytes=1_000_000, now=self.now)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_a_negative_marker_ages_out_too(self):
        """A marker records that nothing could be resolved AT THE TIME. Keeping
        it past the ceiling would make one absent answer permanent, which is the
        same thing the ceiling exists to prevent for a picture that IS there."""
        marker = self._write("1396.trakt.none", age_days=200, size=0)
        posters.sweep(max_bytes=1_000_000, now=self.now)
        self.assertFalse(marker.exists())

    def test_the_size_cap_still_runs_after_the_age_rule(self):
        old = self._write("old.jpg", age_days=200)
        a = self._write("a.jpg", age_days=30)
        b = self._write("b.jpg", age_days=20)
        c = self._write("c.jpg", age_days=10)
        removed = posters.sweep(max_bytes=150, now=self.now)
        # One aged out, then two more evicted oldest-first to get under the cap.
        self.assertEqual(removed, 3)
        self.assertFalse(old.exists())
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())
        self.assertTrue(c.exists())

    def test_nothing_old_and_nothing_over_budget_is_a_noop(self):
        self._write("fresh.jpg", age_days=1)
        self.assertEqual(posters.sweep(max_bytes=1_000_000, now=self.now), 0)


class LogoAgeCeilingTests(unittest.TestCase):
    """The logo cache had NO sweep at all, and its tiles are TMDB-sourced.

    AGE ONLY AND NO SIZE CAP, which is the deliberate asymmetry with the poster
    cache: there are as many logos as there are networks, measured at a few
    megabytes, so a byte budget would be a setting nobody could have a reason to
    change.
    """

    def setUp(self):
        logos.LOGO_DIR = TMP / f"logos-age-{id(self)}"
        logos.LOGO_DIR.mkdir(parents=True)
        self.now = 2_000_000_000.0

    def _write(self, name: str, *, age_days: float) -> Path:
        path = logos.LOGO_DIR / name
        path.write_bytes(b"x" * 50)
        when = self.now - age_days * 86400
        os.utime(path, (when, when))
        return path

    def test_a_logo_past_the_ceiling_goes(self):
        old = self._write("hbo.png", age_days=200)
        fresh = self._write("netflix.png", age_days=5)
        self.assertEqual(logos.sweep(now=self.now), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_a_negative_marker_ages_out_too(self):
        marker = self._write("obscure.none", age_days=200)
        logos.sweep(now=self.now)
        self.assertFalse(marker.exists())

    def test_a_missing_directory_is_a_noop(self):
        logos.LOGO_DIR = TMP / "logos-that-do-not-exist"
        self.assertEqual(logos.sweep(now=self.now), 0)

    def test_the_ceiling_is_the_one_tmdb_states(self):
        """Read from the client that does the obtaining rather than restated, so
        the two caches cannot come to different answers about the same rule."""
        self.assertEqual(tmdb_client.MAX_CACHE_SECONDS, 180 * 24 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
