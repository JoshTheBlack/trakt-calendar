"""fetch_details' poster and cast headshots are Trakt's ORIGIN addresses, made
absolute — Trakt states them without a scheme. The title store keeps what Trakt
said, and the panel proxies both when it draws them, because this app's CSP
img-src admits only the shared image proxy (tests/metadata/test_panel.py holds
that half).

No network: transport.cached_get is patched, the seam every other detail test
in this package patches too.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.providers.trakt import detail

SETTINGS = SimpleNamespace(trakt_client_id="id", trakt_access_token="",
                           cache_ttl_minutes=10, timezone="UTC")

INFO = {
    "title": "A Movie", "ids": {"trakt": 99},
    "images": {"poster": ["walter.trakt.tv/images/movies/000/000/099/posters/original/abc.jpg"]},
}
PEOPLE = {
    "cast": [{"person": {"name": "Cast One",
                         "images": {"headshot": ["walter.trakt.tv/images/people/000/000/001/headshots/medium/def.jpg"]}},
              "character": "Lead"}],
}


class DetailImageProxyTests(unittest.IsolatedAsyncioTestCase):
    async def _details(self):
        async def _get(client, settings, path, params=None, **kwargs):
            return INFO if path == "movies/99" else PEOPLE

        with patch("app.providers.trakt.transport.cached_get", new=AsyncMock(side_effect=_get)):
            return await detail.fetch_details(SETTINGS, "movie", 99, None)

    async def test_the_poster_is_trakts_own_address_made_absolute(self):
        got = await self._details()
        self.assertEqual(got["poster"].split("://", 1)[0], "https")
        self.assertIn("walter.trakt.tv", got["poster"])
        self.assertNotIn("wsrv.nl", got["poster"])

    async def test_a_cast_headshot_is_trakts_own_address_made_absolute(self):
        got = await self._details()
        headshot = got["cast"][0]["headshot"]
        self.assertEqual(headshot,
                         "https://walter.trakt.tv/images/people/000/000/001/headshots/medium/def.jpg")

if __name__ == "__main__":
    unittest.main()
