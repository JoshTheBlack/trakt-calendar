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
from app.config import Settings
from app.providers.base import Source
from app.media import artwork, logos, posters
from app.providers.tmdb import transport as tmdb_client
from tests.support import TMP, migrated_db

# Trakt's catalogue is on in both — its lookup by tmdb id is the one a title
# with no TMDB answer falls to — and TMDB's only in CONFIGURED.
NOT_CONFIGURED = Settings(trakt_client_id="cid")
CONFIGURED = Settings(trakt_client_id="cid",
                      tmdb_api_key="eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJwb3N0ZXJzIn0.c2ln")


def trakt_lookup(fake):
    """Stand `fake` in for Trakt's entry in the per-service lookup table."""
    return patch.dict(posters._ADDRESS_LOOKUPS, {"trakt": fake})


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
    def test_every_source_recorded_is_offered_and_the_caller_picks(self):
        asyncio.run(artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg"))
        asyncio.run(artwork.record_poster_url("show", 1396, "tmdb", "https://img/tmdb.jpg"))
        self.assertEqual(asyncio.run(artwork.urls_for("show", 1396)),
                         {"trakt": "https://img/trakt.jpg", "tmdb": "https://img/tmdb.jpg"})

    def test_no_row_is_no_url(self):
        self.assertEqual(asyncio.run(artwork.urls_for("show", 999)), {})

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
        self.assertEqual(asyncio.run(artwork.urls_for("show", 1396)), {})

    def test_media_namespacing_show_and_movie_never_collide(self):
        asyncio.run(artwork.record_poster_url("show", 550, "trakt", "https://img/show550.jpg"))
        asyncio.run(artwork.record_poster_url("movie", 550, "trakt", "https://img/movie550.jpg"))
        self.assertEqual(
            asyncio.run(artwork.urls_for("show", 550)), {"trakt": "https://img/show550.jpg"})
        self.assertEqual(
            asyncio.run(artwork.urls_for("movie", 550)), {"trakt": "https://img/movie550.jpg"})
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

    def test_a_row_tmdb_supplied_goes_at_tmdbs_six_month_ceiling(self):
        """TMDB's terms forbid keeping anything obtained from it for longer than
        six months, and these rows used to stay three years like any other. Rows
        from other services keep the long retention; the same age from TMDB is
        gone. Both registries."""
        now = db.now()
        aged = now - tmdb_client.MAX_CACHE_SECONDS - 10
        for source in ("tmdb", "trakt"):
            asyncio.run(artwork.record_poster_url("show", 7, source, f"https://img/{source}.jpg"))
            asyncio.run(artwork.record_season_poster_urls(
                [("show", 7, 1, source, f"https://img/{source}-s1.jpg")]))
        for table in ("show_posters", "season_posters"):
            asyncio.run(db.execute(f"UPDATE {table} SET last_seen_at = ?", (aged,)))

        self.assertEqual(asyncio.run(artwork.sweep(now)), 2)
        for table in ("show_posters", "season_posters"):
            with self.subTest(table=table):
                self.assertEqual({r["source"] for r in self.rows(f"SELECT source FROM {table}")},
                                 {"trakt"})


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
        # need a database.
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

        with patch("app.media.posters.tmdb_detail.title_payload") as get_json, \
             patch("app.media.posters.tmdb_transport.download") as download, \
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

        with patch("app.media.posters.tmdb_transport.download") as download:
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

        with patch("app.media.posters.tmdb_transport.download", new=_download):
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

        with patch("app.media.posters.tmdb_detail.title_payload") as get_json, \
             patch("app.media.posters.tmdb_transport.download") as download:
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

        with patch("app.media.posters.tmdb_detail.title_payload") as get_json, \
             patch("app.media.posters.tmdb_transport.download",
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

        with patch("app.media.posters.tmdb_transport.download", new=_download):
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

        with patch("app.media.posters.tmdb_transport.download",
                   new=AsyncMock(return_value=body)):
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertEqual(result.read_bytes(), body)

    async def test_an_oversized_picture_is_refused(self):
        """The decompression-bomb guard is the reason a verify step survived the
        pipeline's removal: this is the only place bytes from somebody else's
        host are opened before the share card composites them."""
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")
        huge = _jpeg_bytes(size=(posters.MAX_SOURCE_DIMENSION + 1, 10))

        with patch("app.media.posters.tmdb_transport.download",
                   new=AsyncMock(return_value=huge)), \
             patch("app.media.artwork.record_failure", new=AsyncMock()), \
             trakt_lookup(AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)

    async def test_tmdb_answers_where_the_order_reaches_it_and_records_the_url(self):
        """The lookup follows the order: Trakt, first here, has nothing, and
        TMDB's answer is the picture."""
        with trakt_lookup(AsyncMock(return_value=None)),              patch("app.media.posters.tmdb_detail.title_payload",
                   new=AsyncMock(return_value={"poster_path": "/x.jpg"})), \
             patch("app.media.posters.tmdb_transport.download",
                   new=AsyncMock(return_value=_jpeg_bytes())), \
             patch("app.media.artwork.record_poster_url", new=AsyncMock()) as record:
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNotNone(result)
        self.assertEqual(result.name, "1396.tmdb.jpg")
        record.assert_awaited_once_with(
            "show", 1396, "tmdb", f"{posters.tmdb_transport.IMAGE_BASE}/w500/x.jpg")

    async def test_tmdb_is_not_asked_when_it_is_not_in_the_order(self):
        """An order is a statement about which services' artwork this viewer
        wants. Reaching past it to TMDB anyway would make the preference
        advisory."""
        with patch("app.media.posters.tmdb_detail.title_payload") as get_json, \
             trakt_lookup(AsyncMock(return_value=None)):
            result = await posters.ensure_poster(CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)
        get_json.assert_not_called()

    async def test_a_failing_registry_url_falls_through_and_increments_fail_count(self):
        await artwork.record_poster_url("show", 1396, "trakt", "https://dead/x.jpg")

        with patch("app.media.posters.tmdb_transport.download", new=AsyncMock(return_value=None)), \
             patch("app.media.artwork.record_failure", new=AsyncMock()) as record_failure, \
             trakt_lookup(AsyncMock(return_value=None)):
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

        with patch("app.media.posters.tmdb_transport.download",
                   new=AsyncMock(return_value=b"not an image")), \
             patch("app.media.artwork.record_failure", new=AsyncMock()) as record_failure, \
             trakt_lookup(AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, ("trakt",))

        self.assertIsNone(result)
        record_failure.assert_awaited_once_with("show", 1396, "trakt")

    async def test_a_services_own_lookup_is_the_last_resort(self):
        with trakt_lookup(AsyncMock(return_value="https://fresh/x.jpg")), \
             patch("app.media.posters.tmdb_transport.download",
                   new=AsyncMock(return_value=_jpeg_bytes())):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNotNone(result)
        self.assertTrue(result.exists())

    async def test_nothing_resolved_writes_a_negative_marker(self):
        """Against the FIRST source in the order, not against every one tried:
        enough to stop this order retrying, while leaving a viewer who prefers
        the other service free to try it."""
        with trakt_lookup(AsyncMock(return_value=None)):
            result = await posters.ensure_poster(NOT_CONFIGURED, "show", 1396, self.ORDER)

        self.assertIsNone(result)
        self.assertTrue(posters._none_path("show", 1396, self.ORDER[0]).exists())
        self.assertFalse(posters._none_path("show", 1396, self.ORDER[1]).exists())

    async def test_media_namespacing_show_and_movie_never_share_a_file(self):
        with trakt_lookup(AsyncMock(return_value=None)),              patch("app.media.posters.tmdb_detail.title_payload",
                   new=AsyncMock(return_value={"poster_path": "/x.jpg"})), \
             patch("app.media.posters.tmdb_transport.download",
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

    async def test_a_tile_is_written_off_the_loop_thread(self):
        """~95 KB a tile, dozens per export: a disk write every other request
        would wait behind if it ran on the event loop."""
        import threading
        await artwork.record_poster_url("show", 1396, "trakt", "https://img/trakt.jpg")
        seen = []
        real = posters._store

        def store(*args):
            seen.append(threading.get_ident())
            return real(*args)

        with patch.object(posters, "_store", store),              patch("app.media.posters.tmdb_transport.download",
                   new=AsyncMock(return_value=_jpeg_bytes())):
            await posters.ensure_poster(CONFIGURED, "show", 1396, self.ORDER)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], threading.get_ident())

    async def test_invalid_pairs_are_a_clean_none_not_an_error(self):
        self.assertIsNone(await posters.ensure_poster(CONFIGURED, "book", 1))
        self.assertIsNone(await posters.ensure_poster(CONFIGURED, "show", None))
        self.assertIsNone(await posters.ensure_poster(CONFIGURED, "show", "not-a-number"))


class SeasonRegistryTests(ArtworkTestCase):
    """season_posters, the known miss, the batched read and the one picker."""

    def test_a_season_address_is_its_own_row_beside_its_shows(self):
        asyncio.run(artwork.record_poster_url("show", 7, "trakt", "https://x/show.jpg"))
        asyncio.run(artwork.record_season_poster_urls([
            ("show", 7, 2, "trakt", "https://x/s2.jpg"),
            ("show", 7, 3, "trakt", ""),                 # no artwork: nothing filed
        ]))
        self.assertEqual(asyncio.run(artwork.season_urls_for("show", 7, 2)),
                         {"trakt": "https://x/s2.jpg"})
        self.assertEqual(asyncio.run(artwork.season_urls_for("show", 7, 3)), {})
        self.assertEqual(asyncio.run(artwork.urls_for("show", 7)), {"trakt": "https://x/show.jpg"})

    def test_pick_takes_the_season_first_then_the_show_in_the_viewers_order(self):
        show = {"trakt": "t.jpg", "simkl": "s.jpg"}
        self.assertEqual(artwork.pick(show, {"trakt": "s2.jpg"}, ("simkl", "trakt")), "s2.jpg")
        self.assertEqual(artwork.pick(show, {}, ("simkl", "trakt")), "s.jpg")
        self.assertEqual(artwork.pick(show, {}, ("trakt", "simkl")), "t.jpg")
        self.assertEqual(artwork.pick({}, {}, ("trakt",)), "")

    def test_ranked_is_picks_order_whole_and_each_address_once(self):
        """What stands behind the picture a page draws: the next of these is what
        the viewer sees when the first has died at its host."""
        show = {"trakt": "t.jpg", "simkl": "s.jpg"}
        self.assertEqual(artwork.ranked(show, {"trakt": "s2.jpg"}, ("simkl", "trakt")),
                         ["s2.jpg", "s.jpg", "t.jpg"])
        self.assertEqual(artwork.ranked(show, {"trakt": "t.jpg"}, ("trakt", "simkl")),
                         ["t.jpg", "s.jpg"])
        self.assertEqual(artwork.ranked({}, {}, ("trakt",)), [])

    def test_a_miss_is_invisible_to_every_address_reader_and_known_to_known(self):
        asyncio.run(artwork.record_miss("show", 8))
        self.assertEqual(asyncio.run(artwork.urls_for("show", 8)), {})
        known = asyncio.run(artwork.known([("show", 8, None)]))
        self.assertEqual((known.shows, known.missed), ({}, {("show", 8)}))

    def test_a_real_sighting_after_a_miss_is_found_first(self):
        asyncio.run(artwork.record_miss("show", 8))
        asyncio.run(artwork.record_poster_url("show", 8, "trakt", "https://x/8.jpg"))
        known = asyncio.run(artwork.known([("show", 8, None)]))
        self.assertEqual(known.address("show", 8, None, ("trakt", "tmdb")), "https://x/8.jpg")

    def test_known_reads_a_batch_in_two_queries_and_keeps_media_apart(self):
        asyncio.run(artwork.record_poster_url("show", 550, "trakt", "https://x/show550.jpg"))
        asyncio.run(artwork.record_poster_url("movie", 550, "trakt", "https://x/film550.jpg"))
        asyncio.run(artwork.record_season_poster_urls([("show", 550, 1, "trakt", "https://x/s1.jpg")]))
        known = asyncio.run(artwork.known([("show", 550, 1), ("movie", 550, None)]))
        self.assertEqual(known.address("show", 550, 1, ("trakt",)), "https://x/s1.jpg")
        self.assertEqual(known.address("show", 550, 2, ("trakt",)), "https://x/show550.jpg")
        self.assertEqual(known.address("movie", 550, None, ("trakt",)), "https://x/film550.jpg")

    def test_a_season_picture_that_keeps_failing_stops_being_offered(self):
        """The season half of record_failure: a source whose season artwork will
        not download is skipped once it has failed MAX_FAIL_COUNT times, so the
        show's own picture is tried instead of the dead address on every warm.
        Only that season's row is counted against."""
        asyncio.run(artwork.record_season_poster_urls([
            ("show", 7, 2, "trakt", "https://x/s2.jpg"),
            ("show", 7, 3, "trakt", "https://x/s3.jpg")]))
        for _ in range(artwork.MAX_FAIL_COUNT - 1):
            asyncio.run(artwork.record_season_failure("show", 7, 2, "trakt"))
        self.assertEqual(asyncio.run(artwork.season_urls_for("show", 7, 2)),
                         {"trakt": "https://x/s2.jpg"}, "gave up before the ceiling")
        asyncio.run(artwork.record_season_failure("show", 7, 2, "trakt"))
        self.assertEqual(asyncio.run(artwork.season_urls_for("show", 7, 2)), {})
        self.assertEqual(asyncio.run(artwork.season_urls_for("show", 7, 3)),
                         {"trakt": "https://x/s3.jpg"})

    def test_the_sweep_ages_out_both_registries(self):
        asyncio.run(artwork.record_poster_url("show", 1, "trakt", "u"))
        asyncio.run(artwork.record_season_poster_urls([("show", 1, 1, "trakt", "u")]))
        removed = asyncio.run(artwork.sweep(now=db.now() + artwork.POSTER_URL_RETENTION_SECONDS + 1))
        self.assertEqual(removed, 2)


class FindAddressTests(ArtworkTestCase):
    """posters.find_address: what a page draws, with AT MOST ONE LOOKUP per title
    ever, and nothing written to disk."""

    ORDER = ("trakt", "tmdb")

    def setUp(self):
        super().setUp()
        posters.POSTER_DIR = TMP / f"posters-address-{id(self)}"

    def find(self, tmdb=9, season=None):
        return asyncio.run(posters.find_address(NOT_CONFIGURED, "show", tmdb, self.ORDER, season))

    def test_a_recorded_address_costs_no_lookup(self):
        asyncio.run(artwork.record_poster_url("show", 9, "trakt", "https://x/9.jpg"))
        with trakt_lookup(AsyncMock(side_effect=AssertionError("looked up"))):
            self.assertEqual(self.find(), "https://x/9.jpg")
        self.assertFalse(posters.POSTER_DIR.exists(), "an address writes no tile")

    def test_an_unknown_title_is_looked_up_once_and_recorded(self):
        lookup = AsyncMock(return_value="https://x/found.jpg")

        with trakt_lookup(lookup):
            self.assertEqual(self.find(), "https://x/found.jpg")
            self.assertEqual(self.find(), "https://x/found.jpg")
        self.assertEqual(lookup.await_count, 1)

    def test_a_known_miss_costs_no_lookup(self):
        lookup = AsyncMock(return_value=None)
        with trakt_lookup(lookup):
            self.assertEqual(self.find(), "")
            self.assertEqual(self.find(), "")
        self.assertEqual(lookup.await_count, 1)

    def test_a_failed_lookup_is_tried_again_next_time(self):
        lookup = AsyncMock(side_effect=posters._LookupFailed("unreachable"))
        with trakt_lookup(lookup):
            self.assertEqual(self.find(), "")
            self.assertEqual(self.find(), "")
        self.assertEqual(lookup.await_count, 2)

    def test_a_season_takes_its_own_then_its_shows(self):
        asyncio.run(artwork.record_poster_url("show", 9, "trakt", "https://x/9.jpg"))
        asyncio.run(artwork.record_season_poster_urls([("show", 9, 2, "trakt", "https://x/9s2.jpg")]))
        self.assertEqual(self.find(season=2), "https://x/9s2.jpg")
        self.assertEqual(self.find(season=3), "https://x/9.jpg")

    def test_two_requests_arriving_together_share_one_lookup(self):
        """Measured in the browser: a row's picture and a second request for the
        same title arrived together and each paid for a lookup."""
        calls = []

        async def _slow(settings, media, tmdb, order):
            calls.append(tmdb)
            await asyncio.sleep(0.05)
            await artwork.record_poster_url(media, tmdb, "trakt", "https://x/9.jpg")
            return "trakt", "https://x/9.jpg"

        async def _both():
            return await asyncio.gather(
                posters.find_address(NOT_CONFIGURED, "show", 9, self.ORDER),
                posters.find_address(NOT_CONFIGURED, "show", 9, self.ORDER, 2))

        with patch.object(posters, "_look_up_address", _slow):
            self.assertEqual(asyncio.run(_both()), ["https://x/9.jpg", "https://x/9.jpg"])
        self.assertEqual(calls, [9])

    def test_ensure_addresses_looks_up_only_what_nothing_knows(self):
        asyncio.run(artwork.record_poster_url("show", 1, "trakt", "https://x/1.jpg"))
        asyncio.run(artwork.record_miss("show", 2))
        asked = []

        async def _look(settings, media, tmdb, order):
            asked.append((media, tmdb))
            return None

        with patch.object(posters, "_look_up_address", _look):
            count = asyncio.run(posters.ensure_addresses(
                NOT_CONFIGURED, [("show", 1), ("show", 2), ("show", 3), ("show", 3)], self.ORDER))
        self.assertEqual((count, asked), (1, [("show", 3)]))

    def test_the_viewers_order_is_the_order(self):
        """No service is appended: the viewer's metadata order already places
        every service they did not name after the ones they did, TMDB included,
        and appending TMDB again listed it twice."""
        from app.sources import prefs
        stated = prefs.SourcePrefs(user_id=1, metadata_order=["simkl", "trakt"])
        self.assertEqual(posters.order_for(stated.source_order(list(Source))),
                         ("simkl", "trakt", "tmdb"))
        self.assertEqual(posters.DEFAULT_ORDER, ("tmdb", "trakt", "simkl"))

    def test_every_registered_source_has_a_lookup_entry(self):
        self.assertEqual(set(posters._ADDRESS_LOOKUPS), {str(s) for s in Source})

    def test_the_lookup_walks_the_order_and_skips_a_service_it_cannot_ask(self):
        """TMDB first where it leads, Simkl never (its lookup by id needs a
        viewer's token), and a service this instance holds no credential for is
        not asked."""
        asked = []

        def recorder(name, answer):
            async def lookup(settings, media, tmdb):
                asked.append(name)
                return answer
            return lookup

        with patch.dict(posters._ADDRESS_LOOKUPS, {"tmdb": recorder("tmdb", None),
                                                   "trakt": recorder("trakt", "https://x/t.jpg")}):
            found = asyncio.run(posters.find_address(
                CONFIGURED, "show", 31, ("tmdb", "simkl", "trakt")))
            asked_with_tmdb = list(asked)
            asked.clear()
            asyncio.run(posters.find_address(NOT_CONFIGURED, "show", 32, ("tmdb", "trakt")))
        self.assertEqual(found, "https://x/t.jpg")
        self.assertEqual(asked_with_tmdb, ["tmdb", "trakt"])
        self.assertEqual(asked, ["trakt"])


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

        async def fake_ensure(settings, media, tmdb, order=posters.DEFAULT_ORDER, season=None):
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
        async def fake_ensure(settings, media, tmdb, order=posters.DEFAULT_ORDER, season=None):
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
