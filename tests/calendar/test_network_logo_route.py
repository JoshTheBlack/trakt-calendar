"""GET /api/network-logo — the tile a card or a tracker row draws for a network.

Every page that shows one points an <img> here, and a 404 is what tells the
page to fall back to the emoji, so which answer comes back when matters more
than the picture: a tile on disk is served without asking TMDB, a network known
to have none is refused without asking TMDB either, and only a first request
carrying a tmdb id may go and make one.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.media import logos
from tests.support import AppTestCase


class NetworkLogoRouteTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in_as(self.make_user("viewer", calendar_approved=True))
        directory = Path(tempfile.mkdtemp(prefix="logo-route-"))
        patcher = patch.object(logos, "LOGO_DIR", directory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tile(self, network: str) -> Path:
        logos.LOGO_DIR.mkdir(parents=True, exist_ok=True)
        path = logos.LOGO_DIR / f"{logos.slug(network)}.png"
        Image.new("RGBA", (logos.TILE, logos.TILE), (255, 255, 255, 255)).save(path)
        return path

    def test_a_tile_on_disk_is_served_privately_without_asking_tmdb(self):
        self._tile("HBO")
        with patch.object(logos, "ensure_logo", AsyncMock()) as made:
            resp = self.client.get("/api/network-logo", params={"name": "HBO"})
        made.assert_not_awaited()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")
        # Private: a shared cache in front of the app has no business holding it.
        self.assertEqual(resp.headers["cache-control"], "private, max-age=86400")
        self.assertNotIn("content-disposition", resp.headers)

    def test_download_hands_it_over_as_a_named_file(self):
        """The tracker's logo tools offer the tile to save, named for the network."""
        self._tile("Apple TV+")
        resp = self.client.get("/api/network-logo", params={"name": "Apple TV+", "download": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('filename="apple-tv.png"', resp.headers["content-disposition"])

    def test_a_first_request_with_an_id_makes_the_tile(self):
        made_path = self._tile("Max")
        made_path.unlink()

        async def make(settings, network, tmdb):
            self.assertEqual((network, tmdb), ("Max", "1399"))
            return self._tile(network)

        with patch.object(logos, "ensure_logo", side_effect=make) as made:
            resp = self.client.get("/api/network-logo", params={"name": "Max", "tmdb": "1399"})
        made.assert_awaited_once()
        self.assertEqual(resp.status_code, 200)

    def test_a_network_known_to_have_no_logo_is_refused_without_asking(self):
        logos.LOGO_DIR.mkdir(parents=True, exist_ok=True)
        (logos.LOGO_DIR / f"{logos.slug('Tiny')}.none").write_text("", encoding="utf-8")
        with patch.object(logos, "ensure_logo", AsyncMock()) as made:
            resp = self.client.get("/api/network-logo", params={"name": "Tiny", "tmdb": "5"})
        made.assert_not_awaited()
        self.assertEqual(resp.status_code, 404)

    def test_no_name_or_no_tile_to_be_had_is_a_404(self):
        self.assertEqual(self.client.get("/api/network-logo").status_code, 404)
        with patch.object(logos, "ensure_logo", AsyncMock(return_value=None)):
            resp = self.client.get("/api/network-logo", params={"name": "Nowhere", "tmdb": "9"})
        self.assertEqual(resp.status_code, 404)
